"""Validated EMF text helpers. GDI measurement requires Windows; parsing is portable."""
from pathlib import Path
import argparse
import ctypes
from ctypes import wintypes
import os
import struct


def records(data):
    position, result = 0, []
    while position < len(data):
        if len(data) - position < 8:
            raise ValueError(f'Truncated EMF record header at {position}')
        kind, size = struct.unpack_from('<II', data, position)
        if size < 8 or size % 4 or position + size > len(data):
            raise ValueError(f'Invalid EMF record size {size} at {position}')
        result.append((kind, data[position:position + size]))
        position += size
    if not result:
        raise ValueError('Empty EMF data')
    return result


def assemble(rows):
    rows = list(rows)
    if not rows or len(rows[0]) < 88 or struct.unpack_from('<I', rows[0])[0] != 1:
        raise ValueError('Missing EMF header')
    for row in rows:
        if len(records(row)) != 1:
            raise ValueError('Expected one record per row')
    if struct.unpack_from('<I', rows[0], 40)[0] != 0x464D4520:
        raise ValueError('Invalid EMF signature')
    if struct.unpack_from('<I', rows[-1])[0] != 14:
        raise ValueError('Missing EMF EOF record')
    header = bytearray(rows[0])
    struct.pack_into('<II', header, 48, sum(map(len, rows)), len(rows))
    rows[0] = bytes(header)
    return b''.join(rows)


def _text_layout(rec):
    if len(rec) < 76 or struct.unpack_from('<I', rec)[0] != 84:
        raise ValueError('Expected an EMR_EXTTEXTOUTW record')
    count, offset = struct.unpack_from('<II', rec, 44)
    options = struct.unpack_from('<I', rec, 52)[0]
    if options & (0x2000 | 0x10):
        raise ValueError('ETO_PDY and glyph-index records are not supported')
    if count and (offset < 76 or offset + 2 * count > len(rec)):
        raise ValueError('Text offset exceeds record bounds')
    return count, offset


def text_of(rec):
    count, offset = _text_layout(rec)
    return rec[offset:offset + 2 * count].decode('utf-16le')


def _load_gdi():
    if os.name != 'nt':
        raise OSError('Font measurement requires Windows GDI')
    gdi = ctypes.WinDLL('gdi32', use_last_error=True)
    gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi.CreateCompatibleDC.restype = wintypes.HDC
    gdi.CreateFontW.argtypes = [ctypes.c_int] * 5 + [wintypes.DWORD] * 8 + [wintypes.LPCWSTR]
    gdi.CreateFontW.restype = wintypes.HFONT
    gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi.SelectObject.restype = wintypes.HGDIOBJ
    gdi.GetTextExtentExPointW.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.c_int,
                                       ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(wintypes.SIZE)]
    gdi.GetTextExtentExPointW.restype = wintypes.BOOL
    gdi.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi.DeleteDC.argtypes = [wintypes.HDC]
    return gdi


def advances(text, font_record, *, gdi=None):
    if len(font_record) < 104 or struct.unpack_from('<I', font_record)[0] != 82:
        raise ValueError('Expected an EMR_EXTCREATEFONTINDIRECTW record')
    font = font_record[12:104]
    height, width, escapement, orientation, weight = struct.unpack_from('<5i', font)
    flags = list(font[20:28])
    face = font[28:92].decode('utf-16le').split('\0')[0]
    api = gdi if gdi is not None else _load_gdi()
    dc = api.CreateCompatibleDC(None)
    if not dc:
        raise OSError('GDI could not create a device context')
    handle, old = None, None
    try:
        handle = api.CreateFontW(height, width, escapement, orientation, weight, *flags, face)
        if not handle:
            raise OSError('GDI could not create a font')
        old = api.SelectObject(dc, handle)
        if not old or old == ctypes.c_void_p(-1).value:
            old = None
            raise OSError('GDI could not select the font')
        units = len(text.encode('utf-16le')) // 2
        values = (ctypes.c_int * units)()
        size, fit = wintypes.SIZE(), ctypes.c_int()
        # Calls with side effects must never live inside assert (python -O removes them).
        measured = api.GetTextExtentExPointW(dc, text, units, 2**30, ctypes.byref(fit), values, ctypes.byref(size)) if units else True
        if not measured:
            raise OSError('GDI could not measure the text')
        dx = [values[0]] + [values[i] - values[i - 1] for i in range(1, units)] if units else []
        return dx, size.cx, abs(height)
    finally:
        if old is not None:
            api.SelectObject(dc, old)
        if handle:
            api.DeleteObject(handle)
        api.DeleteDC(dc)


def text_record(old, text, font_record, shift=(0, 0)):
    _text_layout(old)
    dx, width, height = advances(text, font_record)
    encoded = text.encode('utf-16le')
    padded = encoded + b'\0' * (-len(encoded) % 4)
    record = bytearray(old[:76] + padded + struct.pack('<' + 'i' * len(dx), *dx))
    struct.pack_into('<I', record, 4, len(record))
    x, y = struct.unpack_from('<2i', old, 36)
    sx, sy = map(round, shift)
    struct.pack_into('<2i', record, 36, x + sx, y + sy)
    struct.pack_into('<II', record, 44, len(encoded) // 2, 76)
    struct.pack_into('<I', record, 72, 76 + len(padded))
    left, top, _, _ = struct.unpack_from('<4i', old, 8)
    struct.pack_into('<4i', record, 8, left + sx, top + sy, left + sx + width, top + sy + height + 5)
    return bytes(record)


def original_width(rec):
    count, _ = _text_layout(rec)
    offset = struct.unpack_from('<I', rec, 72)[0]
    if offset:
        if offset < 76 or offset + 4 * count > len(rec):
            raise ValueError('Advance array exceeds record bounds')
        return sum(struct.unpack_from('<' + 'i' * count, rec, offset))
    left, _, right, _ = struct.unpack_from('<4i', rec, 8)
    return right - left


def centered_record(old, text, font_record, shift_y=0, center=None):
    width = advances(text, font_record)[1]
    left, _, right, _ = struct.unpack_from('<4i', old, 8)
    desired = (left + right) / 2 if center is None else center
    return text_record(old, text, font_record, (desired - left - width / 2, shift_y))


def strip_comments(source, dest):
    source, dest = Path(source), Path(dest)
    if source.resolve() == dest.resolve():
        raise ValueError('Input and output must be different files')
    data = assemble(rec for kind, rec in records(source.read_bytes()) if kind != 70)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open('xb') as stream:
        stream.write(data)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Remove EMF comment records without overwriting the original')
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    try:
        strip_comments(args.source, args.output)
    except (ValueError, OSError) as error:
        parser.exit(2, f'Error: {error}\n')
