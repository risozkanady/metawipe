"""Lossless metadata stripping for JPEG, PNG (incl. APNG), WebP and TIFF.

Everything works on the container: pixel data is copied byte for byte, so nothing is re-encoded,
animations and multi-page files survive, and colour/bit depth/compression stay as they were.
Only a whitelist of structural segments/chunks/tags is kept.
"""
from __future__ import annotations

import struct
import zlib
from bisect import bisect_left
from pathlib import Path

from .common import Context, WipeError, minimal_tiff, tiff_orientation

# ---------------------------------------------------------------- JPEG

_EXIF_PREFIX = b"Exif\x00\x00"


def clean_jpeg(data: bytes) -> bytes:
    """Rebuild a JPEG keeping only JFIF (without thumbnail), ICC, Adobe and image segments.

    Drops APP1 (Exif/XMP), APP13 (Photoshop/IPTC), MPF, COM and everything after the first EOI.
    If the Exif block rotates the image, a block holding just the Orientation tag stays.
    """
    n = len(data)
    if data[:2] != b"\xff\xd8":
        raise WipeError("not a JPEG (missing SOI)")
    out = [b"\xff\xd8"]
    exif_done = False
    i = 2
    while i < n:
        if data[i] != 0xFF:
            raise WipeError(f"corrupt JPEG: expected marker at offset {i}")
        while i < n and data[i] == 0xFF:
            i += 1
        if i >= n:
            break
        marker = data[i]
        i += 1
        if marker == 0xD9:
            out.append(b"\xff\xd9")
            return b"".join(out)
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            out.append(bytes([0xFF, marker]))
            continue
        if i + 2 > n:
            raise WipeError("truncated JPEG segment header")
        length = int.from_bytes(data[i:i + 2], "big")
        end = i + length
        if length < 2 or end > n:
            raise WipeError("truncated JPEG segment")
        payload = data[i + 2:end]
        if marker == 0xDA:  # SOS: header plus entropy-coded data, copied verbatim
            j = end
            while True:
                k = data.find(b"\xff", j)
                if k == -1 or k + 1 >= n:
                    k = n
                    break
                nxt = data[k + 1]
                if nxt == 0x00 or 0xD0 <= nxt <= 0xD7:
                    j = k + 2
                elif nxt == 0xFF:
                    j = k + 1
                else:
                    break
            out.append(data[i - 2:k])
            if k >= n:  # file lacks an EOI; add one
                out.append(b"\xff\xd9")
                return b"".join(out)
            i = k
            continue
        seg = bytes([0xFF, marker]) + data[i:end]
        if marker == 0xE0:
            if payload[:5] == b"JFIF\x00" and len(payload) >= 14:
                out.append(b"\xff\xe0\x00\x10" + payload[:12] + b"\x00\x00")  # thumbnail removed
        elif marker == 0xE1:
            if payload[:6] == _EXIF_PREFIX and not exif_done:
                exif_done = True
                orientation = tiff_orientation(payload[6:])
                if orientation > 1:
                    block = _EXIF_PREFIX + minimal_tiff(orientation)
                    out.append(b"\xff\xe1" + struct.pack(">H", len(block) + 2) + block)
        elif marker == 0xE2:
            if payload[:12] == b"ICC_PROFILE\x00":
                out.append(seg)
        elif marker == 0xEE:
            if payload[:5] == b"Adobe":
                out.append(seg)
        elif 0xE3 <= marker <= 0xEF or marker == 0xFE:
            pass
        else:
            out.append(seg)
        i = end
    raise WipeError("corrupt JPEG: no EOI/scan found")


# ---------------------------------------------------------------- PNG

_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_PNG_KEEP = {
    b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS",
    b"gAMA", b"cHRM", b"sRGB", b"iCCP", b"cICP", b"mDCV", b"cLLI", b"sBIT", b"pHYs", b"bKGD",
    b"acTL", b"fcTL", b"fdAT",
    b"orNT",  # ImageMagick's private orientation chunk: one byte, nothing identifying
}


def _png_chunk(ctype: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + ctype + payload + struct.pack(">I", zlib.crc32(ctype + payload))


def _png_text_orientation(ctype: bytes, payload: bytes) -> int:
    """Orientation stored by ImageMagick/ExifTool as a hex 'Raw profile type exif' text chunk."""
    try:
        keyword, _, rest = payload.partition(b"\x00")
        if not keyword.lower().startswith(b"raw profile type"):
            return 1
        if ctype == b"zTXt":
            rest = zlib.decompressobj().decompress(rest[1:], 1 << 22)
        elif ctype != b"tEXt":
            return 1
        tokens = rest.split()
        raw = bytes.fromhex(b"".join(tokens[2:]).decode("ascii"))
        return tiff_orientation(raw[6:] if raw[:6] == _EXIF_PREFIX else raw)
    except (ValueError, zlib.error, UnicodeDecodeError):
        return 1


def clean_png(data: bytes) -> bytes:
    """Keep only critical, colour and APNG chunks; drop tEXt/zTXt/iTXt/tIME/eXIf/C2PA/... and trailing data."""
    n = len(data)
    if data[:8] != _PNG_SIG:
        raise WipeError("not a PNG (bad signature)")
    out = [_PNG_SIG]
    exif_done = False
    i = 8
    while i + 12 <= n:
        length = int.from_bytes(data[i:i + 4], "big")
        ctype = data[i + 4:i + 8]
        end = i + 12 + length
        if end > n:
            raise WipeError("truncated PNG chunk")
        if ctype in _PNG_KEEP:
            out.append(data[i:end])
        elif ctype in (b"eXIf", b"tEXt", b"zTXt") and not exif_done:
            payload = data[i + 8:i + 8 + length]
            orientation = tiff_orientation(payload) if ctype == b"eXIf" else _png_text_orientation(ctype, payload)
            if orientation > 1:
                exif_done = True
                out.append(_png_chunk(b"eXIf", minimal_tiff(orientation)))
            elif ctype == b"eXIf":
                exif_done = True
        i = end
        if ctype == b"IEND":
            return b"".join(out)
    raise WipeError("corrupt PNG: no IEND chunk")


# ---------------------------------------------------------------- WebP

_WEBP_KEEP = {b"VP8X", b"VP8 ", b"VP8L", b"ALPH", b"ANIM", b"ANMF", b"ICCP"}


def clean_webp(data: bytes) -> bytes:
    """Drop EXIF/XMP chunks (and clear their VP8X flags); frames, alpha and ICC are untouched."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise WipeError("not a WebP file")
    end = min(len(data), 8 + int.from_bytes(data[4:8], "little"))
    chunks: list[bytearray] = []
    orientation = 1
    i = 12
    while i + 8 <= end:
        fourcc = data[i:i + 4]
        size = int.from_bytes(data[i + 4:i + 8], "little")
        stop = i + 8 + size + (size & 1)
        if i + 8 + size > end:
            raise WipeError("truncated WebP chunk")
        if fourcc in _WEBP_KEEP:
            chunk = bytearray(data[i:min(stop, end)])
            if len(chunk) & 1:
                chunk.append(0)
            chunks.append(chunk)
        elif fourcc == b"EXIF" and orientation == 1:
            payload = data[i + 8:i + 8 + size]
            orientation = tiff_orientation(payload[6:] if payload[:6] == _EXIF_PREFIX else payload)
        i = stop
    if not any(c[:4] in (b"VP8 ", b"VP8L", b"ANMF") for c in chunks):
        raise WipeError("corrupt WebP: no image data")
    if chunks and chunks[0][:4] == b"VP8X":
        chunks[0][8] &= ~(0x08 | 0x04)  # Exif, XMP flags
        if orientation > 1:
            tiff = minimal_tiff(orientation)
            chunks.append(bytearray(b"EXIF" + struct.pack("<I", len(tiff)) + tiff))
            if len(tiff) & 1:
                chunks[-1].append(0)
            chunks[0][8] |= 0x08
    body = b"WEBP" + b"".join(bytes(c) for c in chunks)
    return b"RIFF" + struct.pack("<I", len(body)) + body


# ---------------------------------------------------------------- TIFF

_TIFF_KEEP = {
    254, 255, 256, 257, 258, 259, 262, 263, 264, 265, 266, 273, 274, 277, 278, 279, 280, 281, 282, 283,
    284, 290, 291, 292, 293, 296, 297, 301, 317, 318, 319, 320, 321, 322, 323, 324, 325, 326, 327, 328,
    338, 339, 340, 341, 342, 347, 512, 513, 514, 515, 517, 518, 519, 520, 521, 529, 530, 531, 532, 34675,
}
_TIFF_SUBIFD_TAGS = {330, 34665, 34853, 40965}
_TIFF_DATA_TAGS = ((273, 279), (324, 325), (513, 514))  # (offsets, byte counts)
_TIFF_TABLE_TAGS = {519, 520, 521}  # offsets to JPEG tables of unknown length
_TIFF_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8}
_TIFF_VALUE_CODE = {1: "B", 3: "H", 4: "I", 13: "I"}
_TIFF_MAX_WORK = 2_000_000  # entries + values parsed per file; crafted files could otherwise cost hours or GBs
_TIFF_MAX_IFDS = 10000


class _Tiff:
    def __init__(self, data: bytes):
        self.buf = bytearray(data)
        self.budget = 4 * len(data) + (1 << 20)  # bytes we may zero; guards crafted overlapping data
        self.work = 0
        self.seen: set = set()  # IFDs that are kept or already erased by the fallback
        self.protect([])
        self._entries: dict = {}
        self._by: dict = {}
        self._values: dict = {}
        if data[:2] == b"II":
            self.order = "little"
            self.fmt = "<"
        elif data[:2] == b"MM":
            self.order = "big"
            self.fmt = ">"
        else:
            raise WipeError("not a TIFF file")
        magic = self.u(2, 2)
        if magic == 43:
            raise WipeError("BigTIFF is not supported")
        if magic != 42:
            raise WipeError("not a TIFF file (bad magic)")

    def u(self, off: int, size: int) -> int:
        if off < 0 or off + size > len(self.buf):
            raise WipeError("corrupt TIFF: offset out of range")
        return int.from_bytes(self.buf[off:off + size], self.order)

    def spend(self, n: int) -> None:
        self.work += n
        if self.work > _TIFF_MAX_WORK:
            raise WipeError("corrupt TIFF: too many entries")

    def entries(self, ifd: int) -> list:
        """(entry offset, tag, type, count, byte size) for every entry of an IFD; parsed once."""
        got = self._entries.get(ifd)
        if got is None:
            count = self.u(ifd, 2)
            self.spend(count)
            end = ifd + 2 + 12 * count
            if end > len(self.buf):
                raise WipeError("corrupt TIFF: offset out of range")
            got = [(ifd + 2 + 12 * n, tag, typ, cnt, _TIFF_TYPE_SIZE.get(typ, 1) * cnt)
                   for n, (tag, typ, cnt) in enumerate(struct.iter_unpack(self.fmt + "HHI4x", self.buf[ifd + 2:end]))]
            self._entries[ifd] = got
        return got

    def tags_of(self, ifd: int) -> dict:
        got = self._by.get(ifd)
        if got is None:
            got = self._by[ifd] = {tag: (e, typ, cnt, size) for e, tag, typ, cnt, size in self.entries(ifd)}
        return got

    def values(self, e: int, typ: int, cnt: int, size: int) -> tuple:
        """Integer values of an entry (BYTE/SHORT/LONG/IFD only); shared arrays are read once."""
        code = _TIFF_VALUE_CODE.get(typ)
        if code is None:
            return ()
        step = _TIFF_TYPE_SIZE[typ]
        base = e + 8 if size <= 4 else self.u(e + 8, 4)
        key = (base, typ, cnt)
        got = self._values.get(key) if size > 4 else None
        if got is None:
            if size > len(self.buf):
                raise WipeError("corrupt TIFF: implausible value count")
            if base + size > len(self.buf):
                raise WipeError("truncated TIFF")
            self.spend(cnt)
            got = struct.unpack_from(f"{self.fmt}{cnt}{code}", self.buf, base)
            if size > 4:
                self._values[key] = got
        return got

    def protect(self, ranges: list) -> None:
        """Remember what the kept structure uses; the fallback eraser must never touch it."""
        self._protected = sorted(ranges)
        self._starts = [a for a, _ in self._protected]
        self._reach, top = [], 0
        for _, b in self._protected:
            top = max(top, b)
            self._reach.append(top)

    def zero(self, off: int, size: int) -> None:
        if size <= 0:
            return
        if off < 0 or off + size > len(self.buf):
            raise WipeError("truncated TIFF")
        i = bisect_left(self._starts, off + size)
        if i and self._reach[i - 1] > off:
            raise WipeError("corrupt TIFF: overlapping data")
        self.budget -= size
        if self.budget < 0:
            raise WipeError("corrupt TIFF: overlapping data")
        self.buf[off:off + size] = bytes(size)

    def zero_ifd(self, ifd: int, depth: int = 0) -> None:
        """Fallback eraser: an IFD, its out-of-line values, nested IFDs and the strips it points to."""
        if depth > 8 or ifd == 0 or ifd in self.seen or ifd + 2 > len(self.buf):
            return
        self.seen.add(ifd)
        by_tag = self.tags_of(ifd)
        for off_tag, len_tag in _TIFF_DATA_TAGS:
            if off_tag in by_tag and len_tag in by_tag:
                offs = self.values(*by_tag[off_tag])
                lens = self.values(*by_tag[len_tag])
                for o, ln in zip(offs, lens):
                    self.zero(o, ln)
        for tag, (e, typ, cnt, size) in by_tag.items():
            if tag in _TIFF_SUBIFD_TAGS:
                for sub in self.values(e, typ, cnt, size):
                    self.zero_ifd(sub, depth + 1)
            if size > 4:
                self.zero(self.u(e + 8, 4), size)
        self.zero(ifd, 2 + 12 * len(self.entries(ifd)) + 4)

    def clean_ifd(self, ifd: int, zero_dropped: bool) -> tuple[int, int]:
        """Remove non-whitelisted tags in place; return (next IFD offset, position of its pointer)."""
        entries = self.entries(ifd)
        old_end = ifd + 2 + 12 * len(entries)
        next_ifd = self.u(old_end, 4)
        kept = []
        for e, tag, typ, cnt, size in entries:
            if tag in _TIFF_KEEP:
                kept.append(bytes(self.buf[e:e + 12]))
                continue
            if zero_dropped:
                if tag in _TIFF_SUBIFD_TAGS:
                    for sub in self.values(e, typ, cnt, size):
                        self.zero_ifd(sub)
                if size > 4:
                    self.zero(self.u(e + 8, 4), size)
        self._entries.pop(ifd, None)
        self._by.pop(ifd, None)
        self.buf[ifd + 2:old_end + 4] = bytes(old_end + 4 - ifd - 2)
        self.buf[ifd:ifd + 2] = len(kept).to_bytes(2, self.order)
        for n, entry in enumerate(kept):
            self.buf[ifd + 2 + 12 * n:ifd + 14 + 12 * n] = entry
        ptr = ifd + 2 + 12 * len(kept)
        self.buf[ptr:ptr + 4] = next_ifd.to_bytes(4, self.order)
        return next_ifd, ptr

    def ifd_chain(self) -> list[int]:
        chain, seen, ifd = [], set(), self.u(4, 4)
        while ifd and ifd not in seen:
            if len(chain) >= _TIFF_MAX_IFDS:
                raise WipeError("corrupt TIFF: too many IFDs")
            seen.add(ifd)
            chain.append(ifd)
            ifd = self.u(ifd + 2 + 12 * len(self.entries(ifd)), 4)
        return chain

    def is_thumbnail(self, ifd: int, position: int) -> bool:
        """Exif-style JPEG thumbnail: a later IFD that only points at a JPEG stream and has no image of its own."""
        tags = self.tags_of(ifd).keys()
        return position > 0 and 513 in tags and not tags & {273, 324, 256, 257}

    def scalar(self, by: dict, tag: int, default: int) -> int:
        got = self.values(*by[tag]) if tag in by else ()
        return got[0] if got else default

    def counts_cover_geometry(self, by: dict, tiled: bool, lens: tuple) -> bool:
        """Uncompressed data only: do the byte counts hold at least what width/height/depth require?

        libtiff repairs bogus counts for uncompressed images, so a zero or too small count must not
        make the sweep wipe pixel data. Anything that cannot be verified counts as not covered.
        """
        width, length, spp = self.scalar(by, 256, 0), self.scalar(by, 257, 0), self.scalar(by, 277, 1)
        bps = self.values(*by[258]) if 258 in by else (1,)
        if (not 1 <= spp <= 64 or width <= 0 or length <= 0 or not bps or min(bps) <= 0
                or self.scalar(by, 262, -1) == 6):  # YCbCr is subsampled
            return False
        bps = (tuple(bps) + (bps[-1],) * spp)[:spp]
        planar = self.scalar(by, 284, 1) == 2
        planes = spp if planar else 1
        if tiled:
            tw, tl = self.scalar(by, 322, 0), self.scalar(by, 323, 0)
            if tw <= 0 or tl <= 0:
                return False
            per_plane = -(-width // tw) * -(-length // tl)
        else:
            rps = self.scalar(by, 278, length)
            if rps <= 0 or rps > length:
                rps = length
            per_plane = -(-length // rps)
        if len(lens) != per_plane * planes:
            return False
        for p in range(planes):
            bits = bps[p] if planar else sum(bps)
            chunk = lens[p * per_plane:(p + 1) * per_plane]
            if tiled:
                if min(chunk) < -(-tw * bits // 8) * tl:
                    return False
                continue
            row = -(-width * bits // 8)
            if len(chunk) > 1 and min(chunk[:-1]) < rps * row:
                return False
            if chunk[-1] < (length - (per_plane - 1) * rps) * row:
                return False
        return True

    def referenced(self, kept: list[int]):
        """(ranges, reason): byte ranges used by the kept structure, and why they cannot be trusted
        (None when they can, so that everything outside them may be erased). Anything that points
        beyond the end of the file raises WipeError."""
        size = len(self.buf)
        ranges = [(0, 8)]
        tables, kept_sizes = [], []  # IFD tables are rewritten in place: nothing else may live inside them
        done = set()
        reason = None
        for ifd in kept:
            entries = [x for x in self.entries(ifd) if x[1] in _TIFF_KEEP]
            tables.append((ifd, ifd + 2 + 12 * len(self.entries(ifd)) + 4))
            kept_sizes.append((ifd, len(entries)))
            by = {tag: (e, typ, cnt, sz) for e, tag, typ, cnt, sz in entries}
            for e, tag, typ, cnt, sz in entries:
                if sz > 4:
                    off = self.u(e + 8, 4)
                    if off + sz > size:
                        raise WipeError("truncated TIFF")
                    ranges.append((off, off + sz))
            if by.keys() & _TIFF_TABLE_TAGS:
                reason = reason or "TIFF uses old-style JPEG tables"
            for off_tag, len_tag in _TIFF_DATA_TAGS:
                if off_tag not in by:
                    continue
                if len_tag not in by:
                    reason = reason or "TIFF has strips/tiles without byte counts"
                    continue
                if by[off_tag][1] not in (3, 4) or by[len_tag][1] not in (3, 4):
                    reason = reason or "TIFF strip/tile tables have an unsupported type"
                    continue
                offs, lens = self.values(*by[off_tag]), self.values(*by[len_tag])
                if len(offs) != len(lens):
                    reason = reason or "TIFF strip/tile offsets and byte counts disagree"
                    continue
                geometry = tuple(self.scalar(by, t, 0) for t in (256, 257, 259, 262, 277, 278, 284, 322, 323)) \
                    + (tuple(self.values(*by[258])) if 258 in by else (),)
                if by[off_tag][3] > 4 and by[len_tag][3] > 4:
                    key = (off_tag, self.u(by[off_tag][0] + 8, 4), self.u(by[len_tag][0] + 8, 4), len(offs), geometry)
                    if key in done:  # IFDs sharing one table are checked once
                        continue
                    done.add(key)
                if off_tag != 513 and self.scalar(by, 259, 1) == 1 and \
                        not self.counts_cover_geometry(by, off_tag == 324, lens):
                    reason = reason or "TIFF byte counts do not match the image geometry"
                for o, ln in zip(offs, lens):
                    if not ln:
                        if o:
                            reason = reason or "TIFF has empty strips/tiles with an offset"
                        continue  # sparse tile
                    if o + ln > size:
                        raise WipeError("truncated TIFF")
                    ranges.append((o, o + ln))
        self.check_tables_disjoint(tables, ranges)
        return ranges + [(ifd, ifd + 2 + 12 * n + 4) for ifd, n in kept_sizes], reason

    @staticmethod
    def check_tables_disjoint(tables: list, ranges: list) -> None:
        """Compacting an IFD would corrupt strips/values/IFDs that overlap it (crafted or damaged files)."""
        ordered = sorted(ranges)
        starts = [a for a, _ in ordered]
        reach, top = [], 0
        for _, b in ordered:
            top = max(top, b)
            reach.append(top)
        for a, b in tables:
            i = bisect_left(starts, b)  # ranges starting at or after b cannot overlap
            if i and reach[i - 1] > a:
                raise WipeError("corrupt TIFF: overlapping structures")
        tables = sorted(tables)
        if any(tables[k][0] < tables[k - 1][1] for k in range(1, len(tables))):
            raise WipeError("corrupt TIFF: overlapping structures")

    def sweep(self, ranges) -> None:
        """Zero every byte that the kept structure does not reference (stale tag values, old IFDs, thumbnails)."""
        size = len(self.buf)
        pos = 0
        for start, end in sorted(ranges):
            if start > pos:
                self.buf[pos:start] = bytes(start - pos)
            pos = max(pos, end)
        if pos < size:
            self.buf[pos:] = bytes(size - pos)
        if len(self.buf) != size:
            raise WipeError("internal error: TIFF size changed while cleaning")


def clean_tiff(data: bytes, note=None) -> bytes:
    """Whitelist TIFF tags, unlink thumbnail-only IFDs, then zero all bytes nothing references any more.

    `note` receives a message for everything identifying that could not be erased.
    """
    t = _Tiff(data)
    chain = t.ifd_chain()
    if not chain:
        raise WipeError("corrupt TIFF: no image IFD")
    kept_ifds = [ifd for n, ifd in enumerate(chain) if not t.is_thumbnail(ifd, n)]
    ranges, reason = t.referenced(kept_ifds)  # read-only analysis of the original structure
    t.protect(ranges)
    old_jpeg = any(513 in t.tags_of(ifd) or t.scalar(t.tags_of(ifd), 259, 1) == 6 for ifd in kept_ifds)
    keep = set(kept_ifds)
    t.seen = set(keep)
    ptr_pos = 4
    for ifd in chain:
        if ifd not in keep:  # Exif-style JPEG thumbnail, not a page
            next_ifd = t.u(ifd + 2 + 12 * len(t.entries(ifd)), 4)
            t.buf[ptr_pos:ptr_pos + 4] = next_ifd.to_bytes(4, t.order)
            if reason:
                t.zero_ifd(ifd)
            continue
        next_ifd, ptr_pos = t.clean_ifd(ifd, zero_dropped=bool(reason))
    if not reason:
        t.sweep(ranges)
    elif note:
        note(f"{reason}; stale data between structures could not be erased")
    if note and old_jpeg:
        note("TIFF holds an old-style JPEG stream; Exif/XMP/comment segments inside it were not cleaned")
    return bytes(t.buf)


# ---------------------------------------------------------------- handlers

def _bytes_handler(fn):
    def handler(src: Path, dst: Path, ctx: Context) -> None:
        dst.write_bytes(fn(src.read_bytes()))
    return handler


jpeg = _bytes_handler(clean_jpeg)
png = _bytes_handler(clean_png)
webp = _bytes_handler(clean_webp)


def tiff(src: Path, dst: Path, ctx: Context) -> None:
    dst.write_bytes(clean_tiff(src.read_bytes(), ctx.leftover))


# By extension, for pictures embedded in other documents.
BY_EXT = {
    ".jpg": clean_jpeg, ".jpeg": clean_jpeg, ".jpe": clean_jpeg,
    ".png": clean_png, ".apng": clean_png, ".webp": clean_webp, ".tif": clean_tiff, ".tiff": clean_tiff,
}
