"""Create synthetic fixtures. No private papers or external images are needed."""
import binascii
import json
from pathlib import Path
import struct
import sys
import zlib
from zipfile import ZipFile, ZIP_DEFLATED

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_emf_utils import assemble


def png(color):
    def chunk(name, data):
        return struct.pack('>I', len(data)) + name + data + struct.pack('>I', binascii.crc32(name + data) & 0xffffffff)
    width, height = 320, 100
    pixels = b''.join(b'\0' + bytes(color) * width for _ in range(height))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b'')


def main():
    root = Path(__file__).resolve().parent / 'generated'
    root.mkdir(exist_ok=False)
    before, after = png((80, 100, 150)), png((35, 155, 135))
    (root / 'replacement.png').write_bytes(after)
    (root / 'mapping.json').write_text(json.dumps({'word/media/image1.png': 'replacement.png'}, indent=2), encoding='utf-8')
    document = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">
<w:body><w:p><w:r><w:t>Synthetic figure replacement example</w:t></w:r></w:p>
<w:p><w:r><w:drawing><wp:inline><wp:extent cx="3048000" cy="952500"/><wp:docPr id="1" name="Synthetic figure"/>
<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic><pic:nvPicPr><pic:cNvPr id="0" name="image1.png"/><pic:cNvPicPr/></pic:nvPicPr><pic:blipFill><a:blip r:embed="rId1"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill><pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="3048000" cy="952500"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic></a:graphicData></a:graphic>
</wp:inline></w:drawing></w:r></w:p><w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr></w:body></w:document>'''
    with ZipFile(root / 'source.docx', 'w', compression=ZIP_DEFLATED) as doc:
        doc.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        doc.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        doc.writestr('word/document.xml', document)
        doc.writestr('word/_rels/document.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/></Relationships>')
        doc.writestr('word/media/image1.png', before)
    header = bytearray(88)
    struct.pack_into('<II', header, 0, 1, 88)
    struct.pack_into('<I', header, 40, 0x464D4520)
    struct.pack_into('<I', header, 44, 0x10000)
    struct.pack_into('<H', header, 56, 1)
    data = assemble([bytes(header), struct.pack('<III', 70, 12, 0), struct.pack('<IIIII', 14, 20, 0, 0, 20)])
    (root / 'source.emf').write_bytes(data)
    print(root)


if __name__ == '__main__':
    main()
