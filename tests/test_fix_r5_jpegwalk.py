"""Regression tests for the round-5 shared JPEG walker: fill bytes, segments between scans, appended JPEGs, truncation."""
from __future__ import annotations

import struct
import unittest

from metawipe import images
from metawipe.common import WipeError


def seg(marker, body):
    return bytes([0xFF, marker]) + struct.pack(">H", len(body) + 2) + body


JFIF = seg(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
EXIF = seg(0xE1, b"Exif\x00\x00MM\x00\x2a\x00\x00\x00\x08\x00\x00GPSLatitude 48.14N")
DQT = seg(0xDB, b"\x00" + bytes(64))
SOF2 = seg(0xC2, b"\x08\x00\x10\x00\x10\x01\x01\x11\x00")
SOS1 = seg(0xDA, b"\x01\x01\x00\x00\x3f\x00") + b"\x12\xff\x00\x34\xff\xd0\x56\xff\xff\xd1\x78"
COM = seg(0xFE, b"Jozko Mrkvicka, Bratislava")
SOS2 = seg(0xDA, b"\x01\x01\x00\x00\x3f\x10") + b"\x9a\xff\x00\xbc"
EOI = b"\xff\xd9"
HEAD = b"\xff\xd8" + JFIF + DQT + SOF2


def walk(data):
    return [(m, data[s:e]) for m, s, e in images.jpeg_segments(data)]


class JpegWalker(unittest.TestCase):
    def test_fill_byte_before_app1(self):
        data = b"\xff\xd8" + JFIF + b"\xff" + EXIF + DQT + SOF2 + SOS1 + EOI
        self.assertEqual(walk(data), [(0xE0, JFIF), (0xE1, EXIF), (0xDB, DQT), (0xC2, SOF2), (0xDA, SOS1), (0xD9, EOI)])
        self.assertEqual(images.clean_jpeg(data), HEAD + SOS1 + EOI)

    def test_com_between_scans(self):
        data = HEAD + SOS1 + COM + b"\xff\x01" + SOS2 + EOI
        self.assertEqual([m for m, _ in walk(data)], [0xE0, 0xDB, 0xC2, 0xDA, 0xFE, 0x01, 0xDA, 0xD9])
        self.assertIn((0xFE, COM), walk(data))
        self.assertEqual(images.clean_jpeg(data), HEAD + SOS1 + b"\xff\x01" + SOS2 + EOI)

    def test_appended_jpeg_stops_at_first_eoi(self):
        first = HEAD + SOS1 + EOI
        data = first + b"\xff\xd8" + EXIF + DQT + SOF2 + SOS1 + EOI
        last = list(images.jpeg_segments(data))[-1]
        self.assertEqual(last, (0xD9, len(first) - 2, len(first)))
        self.assertNotIn(0xE1, [m for m, _ in walk(data)])
        self.assertEqual(images.clean_jpeg(data), first)

    def test_scan_cut_off(self):
        data = HEAD + SOS1[:-3]
        self.assertEqual(walk(data)[-1], (0xDA, SOS1[:-3]))
        self.assertEqual(list(images.jpeg_segments(data))[-1][2], len(data))
        self.assertEqual(images.clean_jpeg(data), data + EOI)

    def test_header_cut_off_yields_then_raises(self):
        data = b"\xff\xd8" + JFIF + COM[:-5]
        got = []
        with self.assertRaises(WipeError):
            for m, s, e in images.jpeg_segments(data):
                got.append((m, data[s:e]))
        self.assertEqual(got, [(0xE0, JFIF), (0xFE, COM[:-5])])  # readers still see the cut segment
        with self.assertRaises(WipeError):
            images.clean_jpeg(data)

    def test_headers_only_and_not_jpeg(self):
        self.assertEqual(walk(b"\xff\xd8" + JFIF + b"\xff\xff"), [(0xE0, JFIF)])
        with self.assertRaisesRegex(WipeError, "no EOI/scan found"):
            images.clean_jpeg(b"\xff\xd8" + JFIF + b"\xff\xff")
        with self.assertRaisesRegex(WipeError, "missing SOI"):
            list(images.jpeg_segments(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
