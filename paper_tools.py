"""Portable DOCX image replacement, bibliography export, and delivery verification."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path, PurePosixPath
import re
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
from zipfile import ZipFile, BadZipFile

MAX_ZIP_BYTES = 100 * 1024 * 1024


def safe_relative(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or '\\' in name or ':' in name:
        raise ValueError(f"Invalid relative path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or name == '.':
        raise ValueError(f"Invalid relative path: {name!r}")
    resolved = (root / Path(*path.parts)).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"Path leaves the specified directory: {name}")
    return resolved


def write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(data)


def replace_images(source: Path, mapping_file: Path, output: Path) -> dict:
    if source.resolve() == output.resolve():
        raise ValueError("Input and output must be different files")
    mapping = json.loads(mapping_file.read_text(encoding='utf-8-sig'))
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError("Mapping must be a non-empty object: DOCX media path -> replacement file")
    replacements = {}
    with ZipFile(source) as doc:
        infos = doc.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise ValueError("DOCX contains duplicate ZIP entries")
        if sum(info.file_size for info in infos) > MAX_ZIP_BYTES:
            raise ValueError("DOCX uncompressed size exceeds 100 MiB")
        for required in ['[Content_Types].xml', 'word/document.xml']:
            if required not in names:
                raise ValueError(f"Not a DOCX package: missing {required}")
        ET.fromstring(doc.read('word/document.xml'))
        for media, replacement in mapping.items():
            if not isinstance(media, str) or not re.fullmatch(r'word/media/[^/\\:]+', media) or media not in names:
                raise ValueError(f"Unknown DOCX image: {media!r}")
            file = safe_relative(mapping_file.parent, replacement)
            if file.suffix.lower() != Path(media).suffix.lower():
                raise ValueError(f"Image format must match existing DOCX part: {media}")
            if file.stat().st_size > MAX_ZIP_BYTES:
                raise ValueError("Replacement image exceeds 100 MiB")
            replacements[media] = file.read_bytes()
        output.parent.mkdir(parents=True, exist_ok=True)
        created = False
        try:
            with output.open('xb') as stream:
                created = True
                with ZipFile(stream, 'w') as result:
                    result.comment = doc.comment
                    for info in infos:
                        result.writestr(info, replacements.get(info.filename, doc.read(info.filename)))
        except Exception:
            if created:
                output.unlink(missing_ok=True)
            raise
    return {'replaced': sorted(replacements), 'output': output.name}


def digest(file: Path) -> str:
    hasher = hashlib.sha256()
    with file.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            hasher.update(block)
    return hasher.hexdigest()


def make_manifest(root: Path, output: Path) -> dict:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError("Delivery root must be an existing directory")
    if output.resolve().is_relative_to(root):
        raise ValueError("Save the manifest outside the delivery folder")
    files = {}
    for file in sorted(root.rglob('*')):
        if file.is_file():
            name = file.relative_to(root).as_posix()
            checked = safe_relative(root, name)
            if file.is_symlink():
                raise ValueError(f"Symbolic links are not supported: {name}")
            files[name] = digest(checked)
    report = {'version': 1, 'sha256': files}
    write_new(output, (json.dumps(report, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
    return report


def verify_manifest(root: Path, manifest: Path) -> dict:
    if not root.is_dir():
        raise ValueError("Delivery root must be an existing directory")
    data = json.loads(manifest.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('sha256'), dict):
        raise ValueError("Unsupported manifest format")
    missing, changed = [], []
    for name, expected in data['sha256'].items():
        file = safe_relative(root, name)
        if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
            raise ValueError(f"Invalid SHA-256: {name}")
        if not file.is_file():
            missing.append(name)
        elif file.is_symlink() or digest(file) != expected:
            changed.append(name)
    actual = {file.relative_to(root).as_posix() for file in root.rglob('*') if file.is_file()}
    extra = sorted(actual - data['sha256'].keys())
    return {'ok': not (missing or changed or extra), 'missing': missing, 'changed': changed, 'extra': extra}


def text(value) -> str:
    # A metadata field cannot inject an extra RIS record or tag.
    return ' '.join(str(value or '').split())


def bibliography(source: Path, output: Path) -> dict:
    records = json.loads(source.read_text(encoding='utf-8-sig'))
    if not isinstance(records, list) or not records:
        raise ValueError("Bibliography input must be a non-empty JSON list")
    ris, cards = [], []
    for index, item in enumerate(records, 1):
        if not isinstance(item, dict) or not text(item.get('title')):
            raise ValueError(f"Reference {index}: title is required")
        authors = item.get('authors', [])
        if not isinstance(authors, list) or not all(isinstance(author, str) for author in authors):
            raise ValueError(f"Reference {index}: authors must be a list of names")
        title, year, doi, url = (text(item.get(key)) for key in ['title', 'year', 'doi', 'url'])
        if year and not re.fullmatch(r'\d{4}', year):
            raise ValueError(f"Reference {index}: invalid year")
        if url:
            parsed = urlsplit(url)
            if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError(f"Reference {index}: URL must be HTTP(S) without credentials")
        fields = [('TY', 'JOUR'), ('TI', title), *[('AU', text(author)) for author in authors]]
        fields += [(tag, value) for tag, value in [('PY', year), ('DO', doi), ('UR', url)] if value]
        ris.append('\n'.join(f'{tag}  - {value}' for tag, value in fields) + '\nER  -\n')
        link = f'<a href="{html.escape(url, quote=True)}">原文链接</a>' if url else '暂无链接'
        cards.append(f'<li><strong>{html.escape(title)}</strong><p>{html.escape("; ".join(authors))} · {html.escape(year)}</p>{link}</li>')
    ris_path, html_path = output / 'references.ris', output / 'index.html'
    if ris_path.exists() or html_path.exists():
        raise FileExistsError("Bibliography output exists; choose a new directory")
    write_new(ris_path, ('\n'.join(ris)).encode('utf-8'))
    page = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>文献索引</title><body><h1>文献索引</h1><ol>' + ''.join(cards) + '</ol></body></html>'
    write_new(html_path, page.encode('utf-8'))
    return {'references': len(records), 'files': [ris_path.name, html_path.name]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    replace = commands.add_parser('replace-images')
    replace.add_argument('source', type=Path)
    replace.add_argument('mapping', type=Path)
    replace.add_argument('output', type=Path)
    refs = commands.add_parser('bibliography')
    refs.add_argument('source', type=Path)
    refs.add_argument('output', type=Path)
    for command in ['manifest', 'verify']:
        sub = commands.add_parser(command)
        sub.add_argument('root', type=Path)
        sub.add_argument('file', type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'replace-images':
            result = replace_images(args.source, args.mapping, args.output)
        elif args.command == 'bibliography':
            result = bibliography(args.source, args.output)
        elif args.command == 'manifest':
            result = make_manifest(args.root, args.file)
        else:
            result = verify_manifest(args.root, args.file)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get('ok') is False else 0
    except (ValueError, OSError, KeyError, BadZipFile, ET.ParseError) as error:
        parser.exit(2, f"Error: {error}\n")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
