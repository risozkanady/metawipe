"""Shared helpers for the metawipe handlers."""
from __future__ import annotations

import contextlib
import logging
import struct
import sys
import unicodedata
from datetime import datetime
from dataclasses import dataclass, field
from typing import Callable


class WipeError(Exception):
    """The file could not be cleaned safely (malformed, unsupported variant, missing tool)."""


# what --keep (and the TUI's Keep switches) can leave in a file; everything not kept is still removed
KEEP = ("dates", "location", "device", "software", "people", "descriptions", "thumbnails")


def text_ok(v, cap: int) -> bool:
    """A kept text value: a non-blank string within cap, with no control, line-break or bidi-override characters."""
    return (isinstance(v, str) and 0 < len(v.strip()) <= cap and not any(
        unicodedata.category(c) in ("Cc", "Cs", "Zl", "Zp") or "\u202a" <= c <= "\u202e" or "\u2066" <= c <= "\u2069" for c in v))


def real_date(y, mo, d, h=0, mi=0, s=0) -> bool:
    """Whether the numbers (ints or digit strings, missing = 0) are a real calendar date and time."""
    try:
        datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(s or 0))
        return True
    except ValueError:
        return False


@dataclass
class Context:
    password: str | None = None
    warn: Callable[[str], None] = field(default=lambda msg: print(f"warn   {msg}", file=sys.stderr))
    full: bool = False
    residue: list = field(default_factory=list)
    keep: frozenset = frozenset()  # categories from KEEP the user asked to keep
    kept: set = field(default_factory=set)  # categories this file really kept (reported next to the file)

    def leftover(self, msg: str) -> None:
        """Something identifying that this run could not remove (full mode reports the file as incomplete)."""
        self.residue.append(msg)
        self.warn(msg)

    def cannot_keep(self, category: str, why: str) -> None:
        """A kept category this format cannot carry over: it was removed as usual (not residue)."""
        self.warn(f"{category} not kept: {why}")


@contextlib.contextmanager
def quiet_pypdf():
    """pypdf logs its recovery from damaged files to stderr; failures are reported by us instead."""
    logger = logging.getLogger("pypdf")
    old = logger.level
    logger.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        logger.setLevel(old)


def boxes(buf, start: int, end: int):
    """Yield (type, box_start, payload_start, box_end) for the ISO-BMFF boxes in buf[start:end]."""
    pos = start
    while pos + 8 <= end:
        size = struct.unpack_from(">I", buf, pos)[0]
        btype = bytes(buf[pos + 4:pos + 8])
        header = 8
        if size == 1:
            if pos + 16 > end:
                raise WipeError("corrupt file: truncated box header")
            size = struct.unpack_from(">Q", buf, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            raise WipeError("corrupt file: bad box size")
        yield btype, pos, pos + header, pos + size
        pos += size


# Orientation is not identifying and dropping it would turn portrait photos sideways, so image
# handlers keep a single-tag Exif block carrying only this value.
def minimal_tiff(orientation: int) -> bytes:
    entry = struct.pack(">HHIHH", 0x0112, 3, 1, orientation, 0)
    return b"MM\x00\x2a\x00\x00\x00\x08" + b"\x00\x01" + entry + b"\x00\x00\x00\x00"


def tiff_orientation(tiff: bytes) -> int:
    """Return the EXIF Orientation (1-8) of a raw TIFF/Exif block, or 1 when absent or malformed."""
    try:
        if tiff[:2] == b"II":
            order = "little"
        elif tiff[:2] == b"MM":
            order = "big"
        else:
            return 1
        if int.from_bytes(tiff[2:4], order) != 42:
            return 1
        ifd = int.from_bytes(tiff[4:8], order)
        count = int.from_bytes(tiff[ifd:ifd + 2], order)
        for n in range(min(count, 4096)):
            e = ifd + 2 + 12 * n
            if int.from_bytes(tiff[e:e + 2], order) == 0x0112:
                value = int.from_bytes(tiff[e + 8:e + 10], order)
                return value if 1 <= value <= 8 else 1
    except (IndexError, ValueError):
        pass
    return 1
