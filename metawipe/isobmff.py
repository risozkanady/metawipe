"""Lossless metadata removal for HEIC/HEIF/AVIF (ISO base media file format).

Removing boxes would shift the absolute offsets stored in `iloc`, so metadata is neutralised in
place instead: Exif/XMP item payloads are overwritten with zeros, their item type is changed to
the unknown type `free` (readers skip unknown item types) and stray xml/uuid/free boxes are
blanked. File size, offsets and the coded images stay exactly as they were.
"""
from __future__ import annotations

import struct
from pathlib import Path

from .common import Context, WipeError

_BLANK_BOXES = {b"xml ", b"bxml", b"uuid", b"free", b"skip"}
_XMP_TYPES = (b"application/rdf+xml",)


def _boxes(buf: bytearray, start: int, end: int):
    """Yield (type, box_start, payload_start, box_end) for the boxes in buf[start:end]."""
    pos = start
    while pos + 8 <= end:
        size = struct.unpack_from(">I", buf, pos)[0]
        btype = bytes(buf[pos + 4:pos + 8])
        header = 8
        if size == 1:
            if pos + 16 > end:
                raise WipeError("corrupt HEIF: truncated box header")
            size = struct.unpack_from(">Q", buf, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            raise WipeError("corrupt HEIF: bad box size")
        yield btype, pos, pos + header, pos + size
        pos += size


def _cstr_end(buf: bytearray, pos: int, end: int) -> int:
    k = buf.find(b"\x00", pos, end)
    if k == -1:
        raise WipeError("corrupt HEIF: unterminated string")
    return k


def _blank(buf: bytearray, start: int, end: int) -> None:
    buf[start:end] = bytes(end - start)


def _uint(buf: bytearray, pos: int, size: int) -> int:
    return int.from_bytes(buf[pos:pos + size], "big") if size else 0


def _parse_infe(buf: bytearray, payload: int, end: int):
    """Return (item_id, item_type, type_pos, tail_pos) of an infe box; None for unsupported versions."""
    version = buf[payload]
    if version < 2:
        return None
    p = payload + 4
    if version == 2:
        item_id = _uint(buf, p, 2)
        p += 2
    else:
        item_id = _uint(buf, p, 4)
        p += 4
    p += 2  # protection index
    item_type = bytes(buf[p:p + 4])
    return item_id, item_type, p, p + 4


def _iloc_extents(buf: bytearray, payload: int, end: int, wanted: set[int]) -> dict[int, list[tuple[int, int, int]]]:
    """Map item_id -> [(construction_method, offset, length)] for the wanted items."""
    version = buf[payload]
    if version > 2:
        raise WipeError(f"unsupported iloc version {version}")
    p = payload + 4
    offset_size, length_size = buf[p] >> 4, buf[p] & 15
    base_size = buf[p + 1] >> 4
    index_size = buf[p + 1] & 15 if version in (1, 2) else 0
    p += 2
    if version < 2:
        count = _uint(buf, p, 2)
        p += 2
    else:
        count = _uint(buf, p, 4)
        p += 4
    result: dict[int, list[tuple[int, int, int]]] = {}
    for _ in range(count):
        if version < 2:
            item_id = _uint(buf, p, 2)
            p += 2
        else:
            item_id = _uint(buf, p, 4)
            p += 4
        method = 0
        if version in (1, 2):
            method = _uint(buf, p, 2) & 15
            p += 2
        p += 2  # data_reference_index
        base = _uint(buf, p, base_size)
        p += base_size
        extents = _uint(buf, p, 2)
        p += 2
        for _ in range(extents):
            p += index_size
            offset = _uint(buf, p, offset_size)
            p += offset_size
            length = _uint(buf, p, length_size)
            p += length_size
            if item_id in wanted:
                result.setdefault(item_id, []).append((method, base + offset, length))
        if p > end:
            raise WipeError("corrupt HEIF: iloc overruns its box")
    return result


def _clean_moov(buf: bytearray, start: int, end: int) -> None:
    """Image sequences keep movie-style boxes: zero creation/modification times, blank udta/meta."""
    for btype, box_start, payload, box_end in _boxes(buf, start, end):
        if btype in (b"mvhd", b"tkhd", b"mdhd"):
            _blank(buf, payload + 4, payload + (20 if buf[payload] == 1 else 12))
        elif btype in (b"udta", b"meta"):
            _blank(buf, payload, box_end)
            buf[box_start + 4:box_start + 8] = b"free"
        elif btype in (b"trak", b"mdia"):
            _clean_moov(buf, payload, box_end)


def clean_heif(data: bytes) -> bytes:
    buf = bytearray(data)
    end = len(buf)
    top = list(_boxes(buf, 0, end))
    if not top or top[0][0] != b"ftyp":
        raise WipeError("not a HEIF/AVIF file (no ftyp box)")
    kinds = {b[0] for b in top}
    if b"mini" in kinds:
        raise WipeError("HEIF 'mini' layout (brand mif3) is not supported")
    if b"meta" not in kinds and b"moov" not in kinds:
        raise WipeError("no meta or moov box found; cannot tell where the metadata is")
    for btype, _, payload, box_end in top:
        if btype == b"moov":
            _clean_moov(buf, payload, box_end)
    metas = [b for b in top if b[0] == b"meta"]
    for btype, box_start, payload, box_end in top:
        if btype in _BLANK_BOXES:
            _blank(buf, payload, box_end)
            buf[box_start + 4:box_start + 8] = b"free"
    for _, _, meta_payload, meta_end in metas:
        children = list(_boxes(buf, meta_payload + 4, meta_end))
        by_type = {c[0]: c for c in children}
        unwanted: dict[int, tuple[int, int, int, int]] = {}
        if b"iinf" in by_type:
            _, _, ip, ie = by_type[b"iinf"]
            first = ip + 4 + (2 if buf[ip] == 0 else 4)
            for btype, bs, pp, be in _boxes(buf, first, ie):
                if btype != b"infe":
                    continue
                info = _parse_infe(buf, pp, be)
                if info is None:
                    continue
                item_id, item_type, type_pos, tail = info
                is_xmp = item_type == b"mime" and any(t in bytes(buf[tail:be]) for t in _XMP_TYPES)
                if item_type == b"Exif" or is_xmp:
                    unwanted[item_id] = (type_pos, tail, be, item_id)
        if unwanted:
            if b"iloc" not in by_type:
                raise WipeError("corrupt HEIF: metadata items without iloc")
            _, _, lp, le = by_type[b"iloc"]
            extents = _iloc_extents(buf, lp, le, set(unwanted))
            idat = by_type.get(b"idat")
            for item_id, (type_pos, tail, be, _) in unwanted.items():
                for method, offset, length in extents.get(item_id, []):
                    if method == 0:
                        start = offset
                    elif method == 1 and idat:
                        start = idat[2] + offset
                    else:
                        raise WipeError("unsupported iloc construction method for a metadata item")
                    if length == 0 or start + length > end:
                        raise WipeError("corrupt HEIF: metadata extent out of range")
                    _blank(buf, start, start + length)
                buf[type_pos:type_pos + 4] = b"free"
                _blank(buf, tail, be)
        for btype, bs, pp, be in children:
            if btype in _BLANK_BOXES:
                _blank(buf, pp, be)
                buf[bs + 4:bs + 8] = b"free"
            elif btype == b"iprp":
                for ptype, ps, pp2, pe in _boxes(buf, pp, be):
                    if ptype != b"ipco":
                        continue
                    for prop, prs, prp, pre in _boxes(buf, pp2, pe):
                        if prop == b"udes":  # user description: name/description/tags
                            _blank(buf, prp, pre)
                            buf[prs + 4:prs + 8] = b"free"  # keeps ipma property indices valid
    return bytes(buf)


def heif(src: Path, dst: Path, ctx: Context) -> None:
    dst.write_bytes(clean_heif(src.read_bytes()))
