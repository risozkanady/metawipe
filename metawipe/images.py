"""Lossless metadata stripping for JPEG, PNG (incl. APNG), WebP and TIFF.

Everything works on the container: pixel data is copied byte for byte, so nothing is re-encoded,
animations and multi-page files survive, and colour/bit depth/compression stay as they were.
Only a whitelist of structural segments/chunks/tags is kept.
"""
from __future__ import annotations

import re
import struct
import zlib
from bisect import bisect_left
from pathlib import Path

from .common import KEEP, Context, WipeError, minimal_tiff, real_date, text_ok, tiff_orientation

# ---------------------------------------------------------------- JPEG

_EXIF_PREFIX = b"Exif\x00\x00"


def jpeg_segments(data):
    """Yield (marker, start, end) for each segment after the SOI, up to and including the first EOI.

    data[start:end] is the segment without fill bytes (data[start + 1] == marker); SOS includes its
    entropy-coded data. Returns quietly when the data runs out; a scan running to the end yields end == len(data).
    Raises WipeError on a missing SOI or a damaged segment, after yielding the segments before it (and the
    segment itself, cut at the end of the data, when the data ends inside it).
    """
    n = len(data)
    if data[:2] != b"\xff\xd8":
        raise WipeError("not a JPEG (missing SOI)")
    i = 2
    while i < n:
        if data[i] != 0xFF:
            raise WipeError(f"corrupt JPEG: expected marker at offset {i}")
        while i < n and data[i] == 0xFF:
            i += 1
        if i >= n:
            return
        marker = data[i]
        i += 1
        if marker == 0x01 or 0xD0 <= marker <= 0xD9:
            yield marker, i - 2, i
            if marker == 0xD9:
                return
            continue
        if i + 2 > n:
            raise WipeError("truncated JPEG segment header")
        length = int.from_bytes(data[i:i + 2], "big")
        end = i + length
        if length < 2 or end > n:
            if length >= 2:
                yield marker, i - 2, n  # readers keep what the cut segment still holds; the cleaner stops below
            raise WipeError("truncated JPEG segment")
        if marker == 0xDA:  # SOS: the entropy-coded data runs to the next real marker
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
            end = k
        yield marker, i - 2, end
        i = end


def clean_jpeg(data: bytes, keep: frozenset = frozenset(), kept: set | None = None, warn=None) -> bytes:
    """Rebuild a JPEG keeping only JFIF (without thumbnail), ICC, Adobe and image segments.

    Drops APP1 (Exif/XMP), APP13 (Photoshop/IPTC), MPF, COM and everything after the first EOI.
    If the Exif block rotates the image, a block holding just the Orientation tag stays; the categories in `keep`
    add fields of a freshly built Exif block (see exif_keep) and are reported in `kept`.
    """
    kept = set() if kept is None else kept
    out = [b"\xff\xd8"]
    exif_done = False
    marker = None
    for marker, start, end in jpeg_segments(data):
        payload = data[start + 4:end] if 0xE0 <= marker <= 0xEF else b""
        if marker == 0xE0:
            if payload[:5] == b"JFIF\x00" and len(payload) >= 14:
                out.append(b"\xff\xe0\x00\x10" + payload[:12] + b"\x00\x00")  # thumbnail removed
        elif marker == 0xE1:
            if payload[:6] == _EXIF_PREFIX and not exif_done:
                exif_done = True
                tiff, cats = exif_keep(payload[6:], keep, warn, 0xFFFF - 2 - len(_EXIF_PREFIX))
                if tiff:
                    block = _EXIF_PREFIX + tiff
                    out.append(b"\xff\xe1" + struct.pack(">H", len(block) + 2) + block)
                    kept |= cats
        elif marker == 0xE2:
            if payload[:12] == b"ICC_PROFILE\x00":
                out.append(data[start:end])
        elif marker == 0xEE:
            if payload[:5] == b"Adobe":
                out.append(data[start:end])
        elif 0xE3 <= marker <= 0xEF or marker == 0xFE:
            pass
        else:
            out.append(data[start:end])
    if marker == 0xDA:  # file lacks an EOI; add one
        out.append(b"\xff\xd9")
    elif marker != 0xD9:
        raise WipeError("corrupt JPEG: no EOI/scan found")
    return b"".join(out)


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


def _png_text_exif(ctype: bytes, payload: bytes) -> bytes:
    """The Exif block ImageMagick/ExifTool store as a hex 'Raw profile type exif' text chunk (b"" if none)."""
    try:
        keyword, _, rest = payload.partition(b"\x00")
        if not keyword.lower().startswith(b"raw profile type"):
            return b""
        if ctype == b"zTXt":
            rest = zlib.decompressobj().decompress(rest[1:], 1 << 22)
        elif ctype != b"tEXt":
            return b""
        tokens = rest.split()
        raw = bytes.fromhex(b"".join(tokens[2:]).decode("ascii"))
        return raw[6:] if raw[:6] == _EXIF_PREFIX else raw
    except (ValueError, zlib.error, UnicodeDecodeError):
        return b""


_PNG_TEXT = {"Creation Time": "dates", "date:create": "dates", "date:modify": "dates", "date:timestamp": "dates", "Software": "software", "Author": "people", "Copyright": "people",
             "Title": "descriptions", "Description": "descriptions", "Comment": "descriptions"}
_ISO_DATE = re.compile(r"(\d{4})-(\d\d)-(\d\d)(?:[T ](\d\d):(\d\d)(?::(\d\d)(?:\.\d{1,9})?)?(?:Z|[+-]\d\d:?\d\d)?)?", re.A)
_RFC_DATE = re.compile(r"(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), )?(\d{1,2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
                       r"(\d{4}) (\d\d):(\d\d)(?::(\d\d))? (?:[A-Z]{1,5}|[+-]\d{4})", re.A)
_EXIF_DATE = re.compile(r"(\d{4}):(\d\d):(\d\d) (\d\d):(\d\d):(\d\d)", re.A)
_MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def _date_text(text: str) -> bool:
    """ISO 8601/W3CDTF, RFC 1123 or Exif date text that is a real date, and nothing else."""
    if len(text) > 64:
        return False
    if m := _ISO_DATE.fullmatch(text) or _EXIF_DATE.fullmatch(text):
        return real_date(*m.groups())
    if m := _RFC_DATE.fullmatch(text):
        d, mon, y, h, mi, s = m.groups()
        return real_date(y, _MONTHS.index(mon) + 1, d, h, mi, s)
    return False


def _png_text(ctype: bytes, payload: bytes):
    """(keyword, text) of a tEXt/zTXt/iTXt chunk; None when malformed (compressed text is capped at 4 KiB)."""
    try:
        keyword, _, rest = payload.partition(b"\x00")
        if ctype == b"iTXt":
            flag, method = rest[0], rest[1]
            rest = rest[2:].partition(b"\x00")[2].partition(b"\x00")[2]  # language, translated keyword
        elif ctype == b"zTXt":
            flag, method, rest = 1, rest[0], rest[1:]
        else:
            flag = method = 0
        if flag:
            d = zlib.decompressobj()
            rest = d.decompress(rest, 4096)
            if method or not d.eof:
                return None
        return keyword.decode("ascii"), rest.decode("utf-8" if ctype == b"iTXt" else "latin-1")
    except (IndexError, ValueError, zlib.error):
        return None


def _png_keep_text(ctype: bytes, payload: bytes, keep: frozenset):
    """(category, keyword, fresh chunk) for a text chunk whose keyword and value pass the whitelist, else None."""
    got = _png_text(ctype, payload)
    if got is None:
        return None
    key, text = got
    cat = _PNG_TEXT.get(key)
    if cat not in keep or not text_ok(text, 2000 if cat == "descriptions" else 256):
        return None
    if cat == "dates" and not _date_text(text):
        return None
    try:
        return cat, key, _png_chunk(b"tEXt", key.encode() + b"\x00" + text.encode("latin-1"))
    except UnicodeEncodeError:
        return cat, key, _png_chunk(b"iTXt", key.encode() + b"\x00\x00\x00\x00\x00" + text.encode())


def clean_png(data: bytes, keep: frozenset = frozenset(), kept: set | None = None, warn=None) -> bytes:
    """Keep only critical, colour and APNG chunks; drop tEXt/zTXt/iTXt/tIME/eXIf/C2PA/... and trailing data.

    `keep` re-adds freshly written fields: an Exif block (exif_keep), tIME and the text keywords of the categories.
    """
    n = len(data)
    kept = set() if kept is None else kept
    if data[:8] != _PNG_SIG:
        raise WipeError("not a PNG (bad signature)")
    out = [_PNG_SIG]
    late, keys = [], set()  # kept tIME/text chunks go right before IEND: legal in every chunk order, never between IDATs
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
        elif ctype in (b"eXIf", b"tEXt", b"zTXt", b"iTXt", b"tIME"):
            payload = data[i + 8:i + 8 + length]
            if ctype == b"tIME":
                if "dates" in keep and len(payload) == 7 and real_date(*struct.unpack(">HBBBBB", payload)) and "tIME" not in keys:
                    keys.add("tIME")
                    late.append(_png_chunk(ctype, payload))
                    kept.add("dates")
            elif keep and ctype != b"eXIf" and (got := _png_keep_text(ctype, payload, keep)) and got[1] not in keys:
                keys.add(got[1])
                late.append(got[2])
                kept.add(got[0])
            if ctype in (b"eXIf", b"tEXt", b"zTXt") and not exif_done:
                tiff, cats = exif_keep(payload if ctype == b"eXIf" else _png_text_exif(ctype, payload), keep, warn)
                if tiff:
                    exif_done = True
                    out.append(_png_chunk(b"eXIf", tiff))
                    kept |= cats
                elif ctype == b"eXIf":
                    exif_done = True
        i = end
        if ctype == b"IEND":
            return b"".join(out[:-1] + late + out[-1:])
    raise WipeError("corrupt PNG: no IEND chunk")


# ---------------------------------------------------------------- WebP

_WEBP_KEEP = {b"VP8X", b"VP8 ", b"VP8L", b"ALPH", b"ANIM", b"ANMF", b"ICCP"}


def clean_webp(data: bytes, keep: frozenset = frozenset(), kept: set | None = None, warn=None) -> bytes:
    """Drop EXIF/XMP chunks (and clear their VP8X flags); frames, alpha and ICC are untouched.
    `keep` adds a freshly built EXIF chunk (exif_keep)."""
    kept = set() if kept is None else kept
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise WipeError("not a WebP file")
    end = min(len(data), 8 + int.from_bytes(data[4:8], "little"))
    chunks: list[bytearray] = []
    tiff, cats = None, set()
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
        elif fourcc == b"EXIF" and not tiff:
            payload = data[i + 8:i + 8 + size]
            tiff, cats = exif_keep(payload[6:] if payload[:6] == _EXIF_PREFIX else payload, keep, warn)
        i = stop
    if not any(c[:4] in (b"VP8 ", b"VP8L", b"ANMF") for c in chunks):
        raise WipeError("corrupt WebP: no image data")
    if chunks and chunks[0][:4] == b"VP8X":
        chunks[0][8] &= ~(0x08 | 0x04)  # Exif, XMP flags
        if tiff:
            chunks.append(bytearray(b"EXIF" + struct.pack("<I", len(tiff)) + tiff))
            if len(tiff) & 1:
                chunks[-1].append(0)
            chunks[0][8] |= 0x08
            kept |= cats
    elif cats and warn:  # a plain lossy/lossless file has no VP8X header (canvas size) to flag the chunk
        warn(f"{', '.join(c for c in KEEP if c in cats)} not kept: this WebP has no extended header to carry Exif")
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
_TIFF_MAX_WORK = 3_000_000  # entries + values + ranges parsed per file; crafted files could otherwise cost hours or GBs
_TIFF_MAX_IFDS = 100000  # page stacks of 10000+ pages exist; each IFD is also charged to the work budget


class _Tiff:
    def __init__(self, data: bytes, keep: frozenset = frozenset()):
        self.buf = bytearray(data)
        self.extra = {tag for (kind, tag), (cat, _) in _FIELDS.items() if kind == 0 and cat in keep}  # IFD0 fields to keep
        self.cats: set = set()  # categories really kept (in kept page IFDs)
        self._ok: dict = {}
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
            self.spend(count + 1)
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

    def wanted(self, x) -> bool:
        """Whitelisted tag, or an IFD0-level field of a kept category with a valid value (judged once, before any edit)."""
        e, tag, typ, cnt, _ = x
        if tag in _TIFF_KEEP:
            return True
        if tag not in self.extra:
            return False
        if e not in self._ok:
            try:
                self._ok[e] = _field(self, 0, tag, typ, cnt, e) is not None
            except WipeError:  # value outside the file: dropped as before
                self._ok[e] = False
            if self._ok[e]:
                self.cats.add(_FIELDS[0, tag][0])
        return self._ok[e]

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
                self.spend(len(offs))
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
            if self.wanted((e, tag, typ, cnt, size)):
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

    def is_thumbnail(self, chain: list[int], position: int) -> bool:
        """Exif-style JPEG thumbnail: a later IFD that points at a JPEG stream (513) and has no strips/tiles.

        The first IFD is never one (an old-style JPEG TIFF keeps its only image there). Without
        ImageWidth/ImageLength any later IFD qualifies; some writers put them into the thumbnail
        IFD, so the one right after IFD0 (the Exif IFD1 slot) still counts when it is smaller than IFD0.
        """
        by = self.tags_of(chain[position])
        if position == 0 or 513 not in by or by.keys() & {273, 324}:
            return False
        if not by.keys() & {256, 257}:
            return True
        if position != 1:
            return False
        first = self.tags_of(chain[0])
        w0, h0 = self.scalar(first, 256, 0), self.scalar(first, 257, 0)
        width, length = self.scalar(by, 256, 0), self.scalar(by, 257, 0)
        return not (w0 > 0 and h0 > 0 and width >= w0 and length >= h0)

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
        self.spend(len(bps))
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
        done, covered = set(), {}  # shared strip tables: collected once; geometry verdicts per table
        reason = None
        for ifd in kept:
            entries = [x for x in self.entries(ifd) if self.wanted(x)]
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
                # a table shared by several IFDs is collected once whatever their geometry is
                table = None
                if by[off_tag][3] > 4 and by[len_tag][3] > 4:
                    table = (off_tag, by[off_tag][1], self.u(by[off_tag][0] + 8, 4),
                             by[len_tag][1], self.u(by[len_tag][0] + 8, 4), len(offs))
                if off_tag != 513 and self.scalar(by, 259, 1) == 1:
                    bits = by.get(258)
                    geometry = tuple(self.scalar(by, t, 0) for t in (256, 257, 262, 277, 278, 284, 322, 323)) \
                        + (bits[1:3] + (bytes(self.buf[bits[0] + 8:bits[0] + 12]),) if bits else (),)
                    if (table, geometry) not in covered or table is None:
                        if table in done:  # same table, other geometry: the check scans it again
                            self.spend(len(lens))
                        covered[table, geometry] = self.counts_cover_geometry(by, off_tag == 324, lens)
                    if not covered[table, geometry]:
                        reason = reason or "TIFF byte counts do not match the image geometry"
                if table is not None:
                    if table in done:
                        continue
                    done.add(table)
                self.spend(2 * len(offs))  # every range is a tuple: heavier than a parsed value
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


def clean_tiff(data: bytes, note=None, keep: frozenset = frozenset(), kept: set | None = None, warn=None) -> bytes:
    """Whitelist TIFF tags, unlink thumbnail-only IFDs, then zero all bytes nothing references any more.

    `note` receives a message for everything identifying that could not be erased. `keep` leaves the validated
    IFD0-level fields of those categories in place (the in-place engine cannot carry the Exif/GPS sub-IFDs or the
    thumbnail: `warn` says so); `kept` receives what was really kept.
    """
    t = _Tiff(data, keep)
    chain = t.ifd_chain()
    if not chain:
        raise WipeError("corrupt TIFF: no image IFD")
    kept_ifds = [ifd for n, ifd in enumerate(chain) if not t.is_thumbnail(chain, n)]
    if keep and warn:
        lost = {"thumbnails"} if len(kept_ifds) < len(chain) else set()
        for ifd in kept_ifds:
            by = t.tags_of(ifd)
            for kind, tag in _SUB_TAGS.items():
                try:
                    if tag in by and by[tag][1:3] == (4, 1):
                        lost |= {_FIELDS[kind, g][0] for g in t.tags_of(t.u(by[tag][0] + 8, 4)) if (kind, g) in _FIELDS}
                except WipeError:  # a damaged sub-IFD is simply not kept
                    pass
        for cat in KEEP:
            if cat in keep & lost:
                warn(f"{cat} not kept: a TIFF only keeps its main tags, not the Exif/GPS sub-IFDs or the thumbnail")
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
    if kept is not None:
        kept |= t.cats
    return bytes(t.buf)


# ---------------------------------------------------------------- Exif rebuild (--keep)

# (IFD, tag) -> (category, value rule). IFD 0 = IFD0, 1 = Exif IFD, 2 = GPS IFD (rule = exact (type, count)).
# Anything not listed (MakerNote, Interop, camera settings, unknown tags) is never copied.
_FIELDS: dict = {}
for _kind, _cat, _rule, _tags in (
        (0, "dates", "date", (306,)), (0, "device", "text", (271, 272, 316)), (0, "software", "text", (305, 11)),
        (0, "people", "text", (315, 33432)), (0, "people", "xp", (40093,)), (0, "descriptions", "desc", (270, 269)),
        (0, "descriptions", "xp", (40091, 40092, 40094, 40095)),
        (1, "dates", "date", (36867, 36868)), (1, "dates", "sub", (37520, 37521, 37522)),
        (1, "dates", "off", (36880, 36881, 36882)), (1, "device", "text", (42033, 42035, 42036, 42037)),
        (1, "device", "lens", (42034,)), (1, "people", "text", (42032,)), (1, "descriptions", "ucomment", (37510,))):
    _FIELDS.update({(_kind, _t): (_cat, _rule) for _t in _tags})
# numeric GPS tags only (no Satellites, MapDatum, ProcessingMethod, AreaInformation: free text)
_FIELDS.update({(2, tag): ("dates" if tag in (7, 29) else "location", spec) for tag, spec in {
    1: (2, 2), 2: (5, 3), 3: (2, 2), 4: (5, 3), 5: (1, 1), 6: (5, 1), 7: (5, 3), 9: (2, 2), 10: (2, 2), 11: (5, 1),
    12: (2, 2), 13: (5, 1), 14: (2, 2), 15: (5, 1), 16: (2, 2), 17: (5, 1), 19: (2, 2), 20: (5, 3), 21: (2, 2),
    22: (5, 3), 23: (2, 2), 24: (5, 1), 25: (2, 2), 26: (5, 1), 29: (2, 11), 30: (3, 1), 31: (5, 1)}.items()})
_RULES = {"date": (2, 20), "sub": (2, 10), "off": (2, 7), "text": (2, 257), "desc": (2, 2001), "xp": (1, 4002),
          "ucomment": (7, 4010), "lens": (5, 4)}  # (type, max count)
_EXIF_PARTS = {1: {"dates", "device", "people", "descriptions"}, 2: {"dates", "location"}}  # sub-IFDs and who needs them
_SUB_TAGS = {1: 34665, 2: 34853}


def _exif_date(text: str) -> bool:
    m = _EXIF_DATE.fullmatch(text)
    return bool(m) and real_date(*m.groups())


_CHECKS = {"date": _exif_date, "sub": re.compile("[0-9]{1,9}").fullmatch, "off": re.compile(r"[+-](?:0[0-9]|1[0-4]):[0-5][0-9]").fullmatch}


def _field(t: _Tiff, kind: int, tag: int, typ: int, cnt: int, e: int):
    """(type, count, big-endian value) of a whitelisted Exif field, None when type, count or value do not fit."""
    rule = _FIELDS[kind, tag][1]
    gps = isinstance(rule, tuple)
    want, most = rule if gps else _RULES[rule]
    if typ != want or not 0 < cnt <= most or gps and cnt != most:
        return None
    size = _TIFF_TYPE_SIZE[typ] * cnt
    off = e + 8 if size <= 4 else t.u(e + 8, 4)
    if off + size > len(t.buf):
        raise WipeError("truncated TIFF")
    raw = bytes(t.buf[off:off + size])
    if typ in (3, 5):  # SHORT (GPSDifferential), RATIONAL (a zero denominator is only valid as 0/0 in a lens specification)
        code = "I" if typ == 5 else "H"
        nums = struct.unpack(f"{t.fmt}{len(raw) // (4 if typ == 5 else 2)}{code}", raw)
        if typ == 3 and nums[0] > 1 or typ == 5 and any(not d and (n or rule != "lens") for n, d in zip(nums[::2], nums[1::2])):
            return None
        if (kind, tag) == (2, 7) and any(n >= d * lim for n, d, lim in zip(nums[::2], nums[1::2], (24, 60, 60))):
            return None  # GPSTimeStamp must be a time of day
        return typ, cnt, struct.pack(f">{len(nums)}{code}", *nums)
    if gps:  # AltitudeRef byte, reference letter, date stamp: copied only when they look exactly like that
        ok = (raw[0] in (0, 1) if typ == 1 else raw[0] < 128 and chr(raw[0]).isalpha() and raw[1] == 0 if cnt == 2
              else raw[10] == 0 and _exif_date(raw[:10].decode("latin-1") + " 00:00:00"))
        return (typ, cnt, raw) if ok else None
    head, tail, enc = b"", b"\0", "utf-8"
    try:
        if rule == "xp":  # UTF-16LE in a BYTE array
            enc, tail, text = "utf-16-le", b"\0\0", raw.decode("utf-16-le")
        elif rule == "ucomment":  # 8-byte charset id, then the text (rewritten in the byte order of the new block)
            head, body = raw[:8], raw[8:]
            tail = b""
            if head == b"UNICODE\0":
                enc, text = "utf-16-be", body.decode("utf-16-" + ("be" if t.order == "big" else "le"))
            elif head in (b"ASCII\0\0\0", bytes(8)):
                text = body.decode("utf-8")
            else:
                return None
        else:
            text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    text = text.rstrip("\0")
    if not text_ok(text, 2000 if rule in ("desc", "xp", "ucomment") else 256) \
            or rule in _CHECKS and not _CHECKS[rule](text):
        return None
    value = head + text.encode(enc) + tail
    return typ, len(value), value


def _emit(entries: list, base: int, ptrs: dict, nxt: int) -> bytes:
    """One IFD at offset `base`; an entry's value None means 'offset of the IFD/thumbnail named by ptrs[tag]'."""
    entries = sorted(entries, key=lambda x: x[0])
    table, values = [], b""
    for tag, typ, cnt, data in entries:
        data = struct.pack(">I", ptrs[tag]) if data is None else data
        if len(data) > 4:
            slot = struct.pack(">I", base + 6 + 12 * len(entries) + len(values))
            values += data + b"\0" * (len(data) & 1)
        else:
            slot = data.ljust(4, b"\0")
        table.append(struct.pack(">HHI", tag, typ, cnt) + slot)
    return struct.pack(">H", len(entries)) + b"".join(table) + struct.pack(">I", nxt) + values


def _isize(entries: list) -> int:
    return 6 + 12 * len(entries) + sum(len(d) + (len(d) & 1) for _, _, _, d in entries if d and len(d) > 4)


def _exif_build(ifds: dict, thumb: bytes | None) -> bytes:
    """Big-endian TIFF from {0: IFD0, 1: Exif IFD, 2: GPS IFD} entry lists (+ an IFD1 holding the JPEG thumbnail)."""
    ifd0 = list(ifds[0])
    sub = {k: ifds[k] for k in (1, 2) if ifds[k]}
    ifd1 = [(259, 3, 1, struct.pack(">H", 6)), (513, 4, 1, None), (514, 4, 1, struct.pack(">I", len(thumb)))] if thumb else []
    ifd0 += [(_SUB_TAGS[k], 4, 1, None) for k in sub]
    order = [ifd0, *sub.values(), *([ifd1] if ifd1 else [])]
    offs, pos = [], 8
    for g in order:
        offs.append(pos)
        pos += _isize(g)
    ptrs = {_SUB_TAGS[k]: offs[n + 1] for n, k in enumerate(sub)} | {513: pos}
    body = b"".join(_emit(g, offs[n], ptrs, offs[-1] if ifd1 and g is ifd0 else 0) for n, g in enumerate(order))
    return b"MM\x00\x2a" + struct.pack(">I", 8) + body + (thumb or b"")


def _exif_read(tiff: bytes, keep: frozenset, orientation: int, warn, limit):
    t = _Tiff(tiff)
    ifd0 = t.u(4, 4)
    if ifd0 < 8:
        raise WipeError("corrupt TIFF: bad IFD offset")
    top = t.entries(ifd0)
    by = t.tags_of(ifd0)
    ifds, cats = {0: [(274, 3, 1, struct.pack(">H", orientation))] if orientation > 1 else [], 1: [], 2: []}, set()
    passes = [(0, ifd0)] + [(k, t.u(by[tag][0] + 8, 4)) for k, tag in _SUB_TAGS.items()
                            if tag in by and keep & _EXIF_PARTS[k] and by[tag][1:3] == (4, 1)]
    for kind, ifd in passes:
        seen = set()
        for e, tag, typ, cnt, _ in t.entries(ifd):
            cat = _FIELDS.get((kind, tag), (None,))[0]
            if cat in keep and tag not in seen:
                seen.add(tag)
                if got := _field(t, kind, tag, typ, cnt, e):
                    ifds[kind].append((tag, *got))
                    cats.add(cat)
    if ifds[2]:
        ifds[2].append((0, 1, 4, b"\x02\x03\x00\x00"))
    thumb = None
    if "thumbnails" in keep and (nxt := t.u(ifd0 + 2 + 12 * len(top), 4)):
        one = t.tags_of(nxt)
        start, size = t.scalar(one, 513, 0), t.scalar(one, 514, 0)
        if 0 < size and start + size <= len(tiff) and tiff[start:start + 2] == b"\xff\xd8":
            try:
                thumb = clean_jpeg(tiff[start:start + size])
            except WipeError:
                warn("thumbnails not kept: the Exif thumbnail is not a valid JPEG")
    if not cats and not thumb:
        return None, set()
    out = _exif_build(ifds, thumb)
    if limit and len(out) > limit:
        if thumb:
            warn("thumbnails not kept: the Exif block would be too large")
            thumb, out = None, _exif_build(ifds, None)
        if len(out) > limit:
            raise WipeError("Exif block too large")
    return out, cats | ({"thumbnails"} if thumb else set())


def exif_keep(tiff: bytes, keep: frozenset, warn=None, limit: int | None = None):
    """Fresh minimal Exif block (big-endian TIFF, no 'Exif\\0\\0' prefix) for the Exif/TIFF data `tiff`.

    Returns (block or None, categories really kept). With an empty `keep` the block is just Orientation (None when
    it is 1, as always). Otherwise it holds Orientation plus the whitelisted, validated fields of the categories in
    `keep` (see _FIELDS; GPS 7/29 count as dates, the rest of the GPS IFD as location) and, for "thumbnails",
    the Exif thumbnail cleaned by clean_jpeg. Nothing is ever copied wholesale. `limit` caps the block size (a
    JPEG APP1 holds 65527 bytes after the Exif prefix); the thumbnail is dropped first. On any parse problem it
    falls back to the Orientation-only block and calls `warn` once.
    """
    warn = warn or (lambda msg: None)
    orientation = tiff_orientation(tiff)
    if keep and tiff:
        try:
            block, cats = _exif_read(tiff, keep, orientation, warn, limit)
            if block:
                return block, cats
        except (WipeError, struct.error, ValueError, OverflowError):
            warn("Exif data could not be read safely: none of it was kept")
    return (minimal_tiff(orientation) if orientation > 1 else None), set()


# ---------------------------------------------------------------- handlers

def _bytes_handler(fn):
    def handler(src: Path, dst: Path, ctx: Context) -> None:
        dst.write_bytes(fn(src.read_bytes(), ctx.keep, ctx.kept, ctx.warn))
    return handler


jpeg = _bytes_handler(clean_jpeg)
png = _bytes_handler(clean_png)
webp = _bytes_handler(clean_webp)


def tiff(src: Path, dst: Path, ctx: Context) -> None:
    dst.write_bytes(clean_tiff(src.read_bytes(), ctx.leftover, ctx.keep, ctx.kept, ctx.warn))


# By extension, for pictures embedded in other documents.
BY_EXT = {
    ".jpg": clean_jpeg, ".jpeg": clean_jpeg, ".jpe": clean_jpeg,
    ".png": clean_png, ".apng": clean_png, ".webp": clean_webp, ".tif": clean_tiff, ".tiff": clean_tiff,
}
