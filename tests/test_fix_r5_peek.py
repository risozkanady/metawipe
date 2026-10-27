"""Regression tests for the round-5 preview (peek) findings: JPEG walk, ODF pictures, duplicate HEIF boxes,
non-XMP mime items, control characters, non-regular files, zip member size and the ffprobe timeout."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import tracemalloc
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from metawipe import peek

# Exif TIFF whose IFD0 points at a GPS IFD holding only GPSVersionID: peek reports ("GPS", "present").
GPS_TIFF = (b"MM\x00\x2a\x00\x00\x00\x08" + b"\x00\x01" + b"\x88\x25\x00\x04\x00\x00\x00\x01\x00\x00\x00\x1a"
            + b"\x00\x00\x00\x00" + b"\x00\x01" + b"\x00\x00\x00\x01\x00\x00\x00\x04\x02\x02\x00\x00" + b"\x00\x00\x00\x00")
SOS = b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00" + b"\x12\x34\x56"
CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def seg(marker: int, payload: bytes) -> bytes:
    return b"\xff" + bytes([marker]) + (len(payload) + 2).to_bytes(2, "big") + payload


def jpeg(*segments: bytes) -> bytes:
    return b"\xff\xd8" + b"".join(segments) + SOS + b"\xff\xd9"


GPS_JPEG = jpeg(seg(0xE1, b"Exif\x00\x00" + GPS_TIFF))


def box(btype: bytes, payload: bytes, version=None) -> bytes:
    if version is not None:
        payload = bytes([version, 0, 0, 0]) + payload
    return (8 + len(payload)).to_bytes(4, "big") + btype + payload


def infe(item_id: int, item_type: bytes, content_type: bytes = b"") -> bytes:
    tail = b"\x00" + (content_type + b"\x00" if content_type else b"")
    return box(b"infe", item_id.to_bytes(2, "big") + b"\x00\x00" + item_type + tail, 2)


def iinf(*entries: bytes) -> bytes:
    return box(b"iinf", len(entries).to_bytes(2, "big") + b"".join(entries), 0)


def heif(children, payload: bytes) -> bytes:
    """ftyp + meta(children(iloc)) + mdat(payload); item 1 points at the payload by file offset."""
    def iloc(offset):
        return box(b"iloc", b"\x44\x00" + b"\x00\x01" + b"\x00\x01\x00\x00\x00\x01"
                   + offset.to_bytes(4, "big") + len(payload).to_bytes(4, "big"), 0)

    ftyp = box(b"ftyp", b"heic\x00\x00\x00\x00mif1heic")
    hdlr = box(b"hdlr", b"\x00" * 4 + b"pict" + b"\x00" * 13, 0)
    size = len(box(b"meta", hdlr + children(iloc(0)), 0))
    meta = box(b"meta", hdlr + children(iloc(len(ftyp) + size + 8)), 0)
    return ftyp + meta + box(b"mdat", payload)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="mw-fix-r5-peek-"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def peek(self, name: str, data: bytes):
        path = self.dir / name
        path.write_bytes(data)
        return peek.inspect_file(path)


class JpegWalk(Base):
    def test_fill_byte_before_exif(self):
        data = b"\xff\xd8\xff" + GPS_JPEG[2:]  # one legal 0xFF fill byte before APP1
        self.assertIn(("GPS", "present"), self.peek("fill.jpg", data))

    def test_comment_between_progressive_scans(self):
        data = b"\xff\xd8" + SOS + seg(0xFE, b"Jozko Mrkvicka") + SOS + b"\xff\xd9"
        self.assertIn(("Comment", "Jozko Mrkvicka"), self.peek("prog.jpg", data))

    def test_appended_jpeg_with_gps(self):
        first = jpeg()
        rows = self.peek("concat.jpg", first + GPS_JPEG)
        self.assertIn(("Data after end of image", f"{len(GPS_JPEG)} bytes"), rows)

    def test_comment_control_characters_stripped(self):
        rows = self.peek("esc.jpg", jpeg(seg(0xFE, b"a\x1b[31mred\xc2\x9b2J")))
        self.assertEqual(len(rows), 1)
        self.assertIn("red", rows[0][1])
        self.assertIsNone(CTRL.search(rows[0][0] + rows[0][1]), rows)


class Packages(Base):
    def test_odt_with_gps_picture(self):
        path = self.dir / "a.odt"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("mimetype", "application/vnd.oasis.opendocument.text")
            z.writestr("meta.xml", "<office:document-meta><office:meta/></office:document-meta>")
            z.writestr("content.xml", "<office:document-content/>")
            z.writestr("Pictures/photo.jpg", GPS_JPEG)
        self.assertEqual(peek.inspect_file(path), [("Embedded picture photo.jpg", "GPS")])

    def test_zip_member_read_is_capped(self):
        path = self.dir / "bomb.docx"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", "<Types/>")
            z.writestr("docProps/core.xml", b"<cp:coreProperties>" + b" " * (40 << 20) + b"</cp:coreProperties>")
        tracemalloc.start()
        try:
            peek.inspect_file(path)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 30 << 20)


class NotRegular(Base):
    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs os.mkfifo")
    def test_fifo_does_not_block(self):
        fifo = self.dir / "p.jpg"
        os.mkfifo(fifo)
        result = []
        t = threading.Thread(target=lambda: result.append(peek.inspect_file(fifo)), daemon=True)
        t.start()
        try:
            t.join(5)
            self.assertFalse(t.is_alive(), "peek blocked on a FIFO")
            self.assertEqual(result, [[("could not inspect", "not a regular file")]])
        finally:
            try:  # release a reader stuck in open()
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass
            t.join(5)


class Video(Base):
    def test_ffprobe_has_timeout(self):
        path = self.dir / "a.mp4"
        path.write_bytes(b"")
        done = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
        with mock.patch.object(peek.shutil, "which", return_value="ffprobe"), \
             mock.patch.object(peek.subprocess, "run", return_value=done) as run:
            peek.inspect_file(path)
        self.assertTrue(run.call_args.kwargs.get("timeout"))


class Heif(Base):
    def test_duplicate_iinf_is_flagged(self):
        exif = b"\x00\x00\x00\x00" + GPS_TIFF
        data = heif(lambda iloc: iinf(infe(1, b"Exif")) + iloc + iinf(), exif)
        self.assertIn(("HEIF metadata", "could not be parsed"), self.peek("dup.heic", data))

    def test_non_xmp_mime_item_is_shown(self):
        xmp = b"<x:xmpmeta><dc:creator><rdf:Seq><rdf:li>Jozko</rdf:li></rdf:Seq></dc:creator></x:xmpmeta>"
        rows = self.peek("tp.heic", heif(lambda iloc: iinf(infe(1, b"mime", b"text/plain")) + iloc, xmp))
        self.assertIn(("XMP creator", "Jozko"), rows)
        rows = self.peek("img.heic", heif(lambda iloc: iinf(infe(1, b"mime", b"IMAGE/jpeg")) + iloc, xmp))
        self.assertIn(("XMP creator", "Jozko"), rows)  # exiv2 shows it whatever the content type


if __name__ == "__main__":
    unittest.main()
