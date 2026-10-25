"""Lossless metadata stripping for JPEG, PNG (incl. APNG), WebP and TIFF.

Everything works on the container: pixel data is copied byte for byte, so nothing is re-encoded,
animations and multi-page files survive, and colour/bit depth/compression stay as they were.
Only a whitelist of structural segments/chunks/tags is kept.
"""
from __future__ import annotations

import struct
import zlib
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
}


def _png_chunk(ctype: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + ctype + payload + struct.pack(">I", zlib.crc32(ctype + payload))


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
        elif ctype == b"eXIf" and not exif_done:
            exif_done = True
            orientation = tiff_orientation(data[i + 8:i + 8 + length])
            if orientation > 1:
                out.append(_png_chunk(b"eXIf", minimal_tiff(orientation)))
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
_TIFF_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8}


class _Tiff:
    def __init__(self, data: bytes):
        self.buf = bytearray(data)
        if data[:2] == b"II":
            self.order = "little"
        elif data[:2] == b"MM":
            self.order = "big"
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

    def entries(self, ifd: int):
        count = self.u(ifd, 2)
        for n in range(count):
            e = ifd + 2 + 12 * n
            tag, typ, cnt = self.u(e, 2), self.u(e + 2, 2), self.u(e + 4, 4)
            size = _TIFF_TYPE_SIZE.get(typ, 1) * cnt
            yield e, tag, typ, cnt, size

    def values(self, e: int, typ: int, cnt: int, size: int) -> list[int]:
        base = e + 8 if size <= 4 else self.u(e + 8, 4)
        step = _TIFF_TYPE_SIZE.get(typ, 1)
        return [self.u(base + k * step, step) for k in range(cnt)] if typ in (1, 3, 4, 13) else []

    def zero(self, off: int, size: int) -> None:
        if size > 0 and 0 <= off and off + size <= len(self.buf):
            self.buf[off:off + size] = bytes(size)

    def zero_ifd(self, ifd: int, depth: int = 0) -> None:
        """Erase an IFD, its out-of-line values, nested IFDs and the strips/thumbnails it points to."""
        if depth > 8 or ifd == 0 or ifd + 2 > len(self.buf):
            return
        count = self.u(ifd, 2)
        by_tag = {}
        for e, tag, typ, cnt, size in self.entries(ifd):
            by_tag[tag] = (e, typ, cnt, size)
        for off_tag, len_tag in _TIFF_DATA_TAGS:
            if off_tag in by_tag and len_tag in by_tag:
                offs = self.values(by_tag[off_tag][0], *by_tag[off_tag][1:])
                lens = self.values(by_tag[len_tag][0], *by_tag[len_tag][1:])
                for o, ln in zip(offs, lens):
                    self.zero(o, ln)
        for tag, (e, typ, cnt, size) in by_tag.items():
            if tag in _TIFF_SUBIFD_TAGS:
                for sub in self.values(e, typ, cnt, size):
                    self.zero_ifd(sub, depth + 1)
            if size > 4:
                self.zero(self.u(e + 8, 4), size)
        self.zero(ifd, 2 + 12 * count + 4)

    def clean_ifd(self, ifd: int) -> int:
        """Remove non-whitelisted tags from one IFD in place; return the next IFD offset."""
        count = self.u(ifd, 2)
        old_end = ifd + 2 + 12 * count
        next_ifd = self.u(old_end, 4)
        kept = []
        for e, tag, typ, cnt, size in self.entries(ifd):
            if tag in _TIFF_KEEP:
                kept.append(bytes(self.buf[e:e + 12]))
                continue
            if tag in _TIFF_SUBIFD_TAGS:
                for sub in self.values(e, typ, cnt, size):
                    self.zero_ifd(sub)
            if size > 4:
                self.zero(self.u(e + 8, 4), size)
        self.buf[ifd + 2:old_end + 4] = bytes(old_end + 4 - ifd - 2)
        self.buf[ifd:ifd + 2] = len(kept).to_bytes(2, self.order)
        for n, entry in enumerate(kept):
            self.buf[ifd + 2 + 12 * n:ifd + 14 + 12 * n] = entry
        self.buf[ifd + 2 + 12 * len(kept):ifd + 6 + 12 * len(kept)] = next_ifd.to_bytes(4, self.order)
        return next_ifd


def clean_tiff(data: bytes) -> bytes:
    t = _Tiff(data)
    ifd = t.u(4, 4)
    seen = set()
    while ifd and ifd not in seen and len(seen) < 100000:
        seen.add(ifd)
        ifd = t.clean_ifd(ifd)
    return bytes(t.buf)


# ---------------------------------------------------------------- handlers

def _bytes_handler(fn):
    def handler(src: Path, dst: Path, ctx: Context) -> None:
        dst.write_bytes(fn(src.read_bytes()))
    return handler


jpeg = _bytes_handler(clean_jpeg)
png = _bytes_handler(clean_png)
webp = _bytes_handler(clean_webp)
tiff = _bytes_handler(clean_tiff)


# By extension, for pictures embedded in other documents.
BY_EXT = {
    ".jpg": clean_jpeg, ".jpeg": clean_jpeg, ".jpe": clean_jpeg,
    ".png": clean_png, ".apng": clean_png, ".webp": clean_webp, ".tif": clean_tiff, ".tiff": clean_tiff,
}
