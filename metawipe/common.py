"""Shared helpers for the metawipe handlers."""
from __future__ import annotations

import struct
import sys
from dataclasses import dataclass, field
from typing import Callable


class WipeError(Exception):
    """The file could not be cleaned safely (malformed, unsupported variant, missing tool)."""


@dataclass
class Context:
    password: str | None = None
    warn: Callable[[str], None] = field(default=lambda msg: print(f"warn   {msg}", file=sys.stderr))


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
