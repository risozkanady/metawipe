"""Lossless metadata removal for HEIC/HEIF/AVIF (ISO base media file format).

Removing boxes would shift the absolute offsets stored in `iloc`, so metadata is neutralised in
place instead: Exif and mime (XMP, whatever the content type says) item payloads are overwritten
with zeros, their item type is changed to the unknown type `free` (readers skip unknown item types)
and stray xml/uuid/free boxes are blanked. Free-text name fields (handler names, compressor names,
item names) are overwritten too. File size, offsets and the coded images stay exactly as they were.
"""
from __future__ import annotations

from pathlib import Path

from .common import Context, WipeError
from .common import boxes as _boxes

_BLANK_BOXES = {b"xml ", b"bxml", b"uuid", b"free", b"skip"}
_VISUAL_HANDLERS = (b"vide", b"pict", b"auxv")


def _blank(buf: bytearray, start: int, end: int) -> None:
    buf[start:end] = bytes(end - start)


def _uint(buf: bytearray, pos: int, size: int) -> int:
    return int.from_bytes(buf[pos:pos + size], "big") if size else 0


def _parse_infe(buf: bytearray, payload: int, end: int):
    """Return (item_id, item_type, type_pos, tail_pos) of an infe box; None for unsupported versions."""
    version = buf[payload]
    if version < 2:
        return None
    if version > 3:
        raise WipeError("unsupported infe version")
    if version == 3 and bytes(buf[payload + 8:payload + 12]) in (b"Exif", b"mime"):
        raise WipeError("corrupt HEIF: infe version 3 with a version 2 layout")  # exiv2 ignores the version
    p = payload + 4
    if version == 2:
        item_id = _uint(buf, p, 2)
        p += 2
    else:
        item_id = _uint(buf, p, 4)
        p += 4
    p += 2  # protection index
    if p + 4 > end:
        raise WipeError("corrupt HEIF: truncated infe box")
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
    total = 0
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
        if length_size == 0 or p + extents * (index_size + offset_size + length_size) > end:
            raise WipeError("corrupt HEIF: iloc overruns its box")
        for _ in range(extents):
            p += index_size
            offset = _uint(buf, p, offset_size)
            p += offset_size
            length = _uint(buf, p, length_size)
            p += length_size
            if item_id in wanted:
                total += length
                if total > len(buf):  # overlapping extents would be blanked over and over
                    raise WipeError("corrupt HEIF: metadata extents exceed the file")
                result.setdefault(item_id, []).append((method, base + offset, length))
    return result


def _refuse_stray_items(buf: bytearray, start: int, end: int, allowed: tuple = (), top: bool = False) -> None:
    """exiv2 reads iinf/infe/iloc in every meta, iprp, ipco, moov and iinf it meets; the cleaner only in a top-level
    meta, so refuse them anywhere else (they could name Exif/XMP items that would never be blanked)."""
    for btype, _, payload, box_end in _boxes(buf, start, end):
        if btype in (b"iinf", b"infe", b"iloc") and btype not in allowed:
            raise WipeError(f"corrupt HEIF: {btype.decode()} box outside its place in the top-level meta")
        if btype == b"meta":
            inner, payload = ((b"iinf", b"iloc") if top else ()), payload + 4
        elif btype == b"iinf":
            inner, payload = (b"infe",), payload + 4 + (2 if buf[payload:payload + 1] == b"\x00" else 4)
        elif btype in (b"iprp", b"ipco", b"moov"):
            inner = ()
        else:
            continue
        _refuse_stray_items(buf, payload, box_end, inner)


def _blank_hdlr_name(buf: bytearray, payload: int, end: int) -> None:
    """The handler name (free text) follows 24 bytes of fixed fields; zeroing it keeps the box size."""
    if payload + 24 < end:
        _blank(buf, payload + 24, end)


def _blank_item_name(buf: bytearray, pos: int, end: int, keep_structure: bool) -> None:
    """Overwrite an infe item_name up to (not including) its terminating NUL.

    Strings that follow the name (content type, URI type) are located by that NUL, so for such items
    the name is filled with spaces instead of zeros to leave them where they are.
    """
    stop = buf.find(b"\x00", pos, end)
    if stop == -1:
        stop = end
    buf[pos:stop] = (b" " if keep_structure else b"\x00") * (stop - pos)


def _clean_stsd(buf: bytearray, payload: int, end: int) -> None:
    """Visual sample entries carry a 32-byte free-text compressor name (encoder strings such as 'Lavc...')."""
    for _, _, entry, entry_end in _boxes(buf, payload + 8, end):
        if entry + 8 + 34 + 32 <= entry_end:
            _blank(buf, entry + 8 + 34, entry + 8 + 34 + 32)


def _handler_type(buf: bytearray, start: int, end: int) -> bytes:
    for btype, _, payload, box_end in _boxes(buf, start, end):
        if btype == b"hdlr" and payload + 12 <= box_end:
            return bytes(buf[payload + 8:payload + 12])
    return b""


def _clean_moov(buf: bytearray, start: int, end: int, handler: bytes = b"") -> None:
    """Image sequences keep movie-style boxes: zero creation/modification times, handler and
    compressor names, blank udta/meta."""
    for btype, box_start, payload, box_end in _boxes(buf, start, end):
        if btype in (b"mvhd", b"tkhd", b"mdhd"):
            if box_end - payload < 12 or (buf[payload] == 1 and box_end - payload < 20):
                raise WipeError(f"corrupt HEIF: truncated {btype.decode('latin-1')} box")
            _blank(buf, payload + 4, payload + (20 if buf[payload] == 1 else 12))
        elif btype in (b"udta", b"meta"):
            _blank(buf, payload, box_end)
            buf[box_start + 4:box_start + 8] = b"free"
        elif btype == b"hdlr":
            _blank_hdlr_name(buf, payload, box_end)
        elif btype == b"stsd":
            if handler in _VISUAL_HANDLERS:
                _clean_stsd(buf, payload, box_end)
        elif btype in (b"trak", b"mdia", b"minf", b"stbl"):
            _clean_moov(buf, payload, box_end, _handler_type(buf, payload, box_end) if btype == b"mdia" else handler)


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
    _refuse_stray_items(buf, 0, end, top=True)
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
        for name in (b"iinf", b"iloc", b"idat"):
            if sum(c[0] == name for c in children) > 1:
                raise WipeError(f"corrupt HEIF: more than one {name.decode()} box in meta")
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
                    if pp + 8 <= be:  # version 0/1: item_name follows id and protection index
                        _blank_item_name(buf, pp + 8, be, True)
                    continue
                item_id, item_type, type_pos, tail = info
                if item_type in (b"Exif", b"mime"):  # exiv2 shows every mime item as XMP, whatever its content type
                    unwanted[item_id] = (type_pos, tail, be, item_id)
                else:
                    _blank_item_name(buf, tail, be, item_type == b"uri ")
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
            elif btype == b"hdlr":
                _blank_hdlr_name(buf, pp, be)
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
