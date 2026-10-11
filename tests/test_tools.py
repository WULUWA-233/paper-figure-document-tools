import ctypes
from pathlib import Path
import json
import os
import struct
import tempfile
import unittest
from zipfile import ZipFile

from paper_tools import replace_images, make_manifest, verify_manifest, bibliography, safe_relative
from native_emf_utils import records, assemble, advances, text_of, original_width, strip_comments


def emf_header():
    header = bytearray(88)
    struct.pack_into('<II', header, 0, 1, 88)
    struct.pack_into('<I', header, 40, 0x464D4520)
    return bytes(header)


def font_record():
    record = bytearray(104)
    struct.pack_into('<II', record, 0, 82, 104)
    struct.pack_into('<5i', record, 12, -18, 0, 0, 0, 400)
    record[40:50] = 'Arial'.encode('utf-16le')
    return bytes(record)


class FakeGDI:
    def __init__(self, fail_measure=False):
        self.measured, self.deleted_fonts, self.deleted_dcs = 0, 0, 0
        self.fail_measure = fail_measure

    def CreateCompatibleDC(self, _): return 1
    def CreateFontW(self, *args): return 2
    def SelectObject(self, *args): return 3
    def DeleteObject(self, _): self.deleted_fonts += 1
    def DeleteDC(self, _): self.deleted_dcs += 1

    def GetTextExtentExPointW(self, dc, text, units, max_width, fit, values, size):
        self.measured += 1
        for index in range(units): values[index] = (index + 1) * 9
        size._obj.cx = units * 9
        return not self.fail_measure


class ToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_docx_replaces_only_requested_part_and_preserves_other_bytes(self):
        source, mapping, output = [self.root / name for name in ['in.docx', 'mapping.json', 'out.docx']]
        with ZipFile(source, 'w') as doc:
            doc.writestr('[Content_Types].xml', '<Types/>')
            doc.writestr('word/document.xml', '<document/>')
            doc.writestr('word/media/image1.png', b'old image')
            doc.writestr('word/styles.xml', b'unchanged style')
        (self.root / 'new.png').write_bytes(b'new image')
        mapping.write_text(json.dumps({'word/media/image1.png': 'new.png'}), encoding='utf-8')
        replace_images(source, mapping, output)
        with ZipFile(source) as before, ZipFile(output) as after:
            self.assertEqual(set(before.namelist()), set(after.namelist()))
            self.assertEqual(after.read('word/media/image1.png'), b'new image')
            for name in before.namelist():
                if name != 'word/media/image1.png': self.assertEqual(before.read(name), after.read(name))
        with self.assertRaises(FileExistsError): replace_images(source, mapping, output)
        with self.assertRaises(ValueError): replace_images(source, mapping, source)

    def test_manifest_follows_explicit_root_after_moving_delivery(self):
        delivery, manifest = self.root / 'delivery', self.root / 'manifest.json'
        delivery.mkdir()
        (delivery / 'a.txt').write_text('original', encoding='utf-8')
        make_manifest(delivery, manifest)
        moved = self.root / 'another-folder'
        delivery.rename(moved)
        self.assertTrue(verify_manifest(moved, manifest)['ok'])
        (moved / 'a.txt').write_text('changed', encoding='utf-8')
        (moved / 'extra.txt').write_text('extra', encoding='utf-8')
        result = verify_manifest(moved, manifest)
        self.assertFalse(result['ok'])
        self.assertEqual(result['changed'], ['a.txt'])
        self.assertEqual(result['extra'], ['extra.txt'])
        (moved / 'a.txt').unlink()
        self.assertEqual(verify_manifest(moved, manifest)['missing'], ['a.txt'])

    def test_path_traversal_and_manifest_self_inclusion_are_rejected(self):
        for value in ['../secret', '/absolute', 'C:/private', 'a\\b', '.']:
            with self.subTest(value=value), self.assertRaises(ValueError): safe_relative(self.root, value)
        with self.assertRaises(ValueError): make_manifest(self.root, self.root / 'manifest.json')

    def test_bibliography_escapes_html_and_prevents_ris_newline_injection(self):
        source, output = self.root / 'refs.json', self.root / 'refs'
        source.write_text(json.dumps([{'title': '<script>x</script>\nER  - injected', 'authors': ['Demo'], 'year': '2026', 'url': 'https://example.org/?x=1&y=2'}]), encoding='utf-8')
        bibliography(source, output)
        page = (output / 'index.html').read_text(encoding='utf-8')
        ris = (output / 'references.ris').read_text(encoding='utf-8')
        self.assertNotIn('<script>', page)
        self.assertIn('&amp;', page)
        self.assertEqual(ris.count('\nER  -\n'), 1)
        with self.assertRaises(FileExistsError): bibliography(source, output)

    def test_unsafe_reference_url_is_rejected_before_writing(self):
        source, output = self.root / 'refs.json', self.root / 'refs'
        source.write_text(json.dumps([{'title': 'demo', 'url': 'javascript:alert(1)'}]), encoding='utf-8')
        with self.assertRaises(ValueError): bibliography(source, output)
        self.assertFalse(output.exists())

    def test_invalid_emf_sizes_are_rejected_even_under_python_optimization(self):
        for data in [b'', b'abc', struct.pack('<II', 1, 0), struct.pack('<II', 1, 12), struct.pack('<II', 1, 9) + b'x']:
            with self.subTest(data=data), self.assertRaises(ValueError): records(data)

    def test_emf_comment_removal_updates_header_counts(self):
        eof = struct.pack('<IIIII', 14, 20, 0, 0, 20)
        comment = struct.pack('<III', 70, 12, 0)
        source, output = self.root / 'source.emf', self.root / 'out.emf'
        source.write_bytes(assemble([emf_header(), comment, eof]))
        strip_comments(source, output)
        data = output.read_bytes()
        self.assertEqual([kind for kind, _ in records(data)], [1, 14])
        self.assertEqual(struct.unpack_from('<II', data, 48), (len(data), 2))
        with self.assertRaises(FileExistsError): strip_comments(source, output)

    def test_gdi_measurement_runs_and_resources_release_in_normal_and_optimized_python(self):
        api = FakeGDI()
        dx, width, height = advances('test', font_record(), gdi=api)
        self.assertEqual((api.measured, dx, width, height), (1, [9, 9, 9, 9], 36, 18))
        self.assertEqual((api.deleted_fonts, api.deleted_dcs), (1, 1))
        # Windows APIs count UTF-16 code units, including surrogate pairs.
        self.assertEqual(len(advances('😀', font_record(), gdi=api)[0]), 2)

    def test_gdi_failure_still_cleans_up_handles(self):
        api = FakeGDI(fail_measure=True)
        with self.assertRaises(OSError): advances('test', font_record(), gdi=api)
        self.assertEqual((api.deleted_fonts, api.deleted_dcs), (1, 1))

    @unittest.skipUnless(os.name == 'nt', 'real GDI requires Windows')
    def test_actual_windows_font_measurement(self):
        dx, width, height = advances('Hello', font_record())
        self.assertGreater(width, 0)
        self.assertEqual(sum(dx), width)
        self.assertEqual(len(dx), 5)


if __name__ == '__main__':
    unittest.main()
