"""Regression tests for the round-5 HEIF findings: a second iinf hid Exif/XMP, XMP with an unexpected content
type was kept, a tiny iloc could hang or eat GBs, infe v4 hid the Exif item, a short mvhd made the udta survive.
Also covers XMP removal and Exif in idat / iloc v2 / 8-byte iloc fields. Files are built here."""
import struct
import unittest

from metawipe import isobmff
from metawipe.common import WipeError

FTYP = struct.pack(">I", 20) + b"ftypheic\x00\x00\x00\x00heic"
HDLR = struct.pack(">I", 33) + b"hdlr" + bytes(8) + b"pict" + bytes(13)
EXIF = b"\x00\x00\x00\x00MM\x00*\x00\x00\x00\x08SECRET-EXIF"
XMP = b"<x:xmpmeta><dc:creator>SECRET-XMP</dc:creator></x:xmpmeta>"


def box(btype, payload=b""):
    return struct.pack(">I", 8 + len(payload)) + btype + payload


def infe(item_id, itype, tail=b"\x00", version=2):
    return box(b"infe", bytes([version, 0, 0, 0]) + struct.pack(">H" if version == 2 else ">I", item_id) + b"\x00\x00" + itype + tail)


def iinf(*entries):
    return box(b"iinf", bytes(4) + struct.pack(">H", len(entries)) + b"".join(entries))


def iloc(items, version=1, osz=4, lsz=4, bsz=0, isz=0, extents=None):
    """items: (item_id, method, base, offset, length); one extent each unless `extents` overrides the count."""
    def uint(n, v):
        return v.to_bytes(n, "big") if n else b""
    body = bytes([version, 0, 0, 0, osz << 4 | lsz, bsz << 4 | isz]) + uint(4 if version == 2 else 2, len(items))
    for item_id, method, base, offset, length in items:
        body += uint(4 if version == 2 else 2, item_id) + (struct.pack(">H", method) if version else b"")
        body += b"\x00\x00" + uint(bsz, base) + struct.pack(">H", 1 if extents is None else extents)
        body += uint(isz, 0) + uint(osz, offset) + uint(lsz, length)
    return box(b"iloc", body)


def heif(children, mdat=b""):
    """children(mdat_start) -> meta children; mdat_start is the absolute offset of the mdat payload."""
    size = len(FTYP) + len(box(b"meta", bytes(4) + children(64))) + 8  # field widths do not depend on the value
    return FTYP + box(b"meta", bytes(4) + children(size)) + box(b"mdat", mdat)


class Base(unittest.TestCase):
    def clean(self, data):
        out = isobmff.clean_heif(data)
        self.assertEqual(len(out), len(data))
        return out


class RepeatedMetaChildren(Base):
    """F1: only the last iinf/iloc/idat of a meta was used."""

    def test_second_iinf_is_refused(self):
        d = heif(lambda m: HDLR + iinf(infe(1, b"Exif")) + iinf(infe(2, b"hvc1")) + iloc([(1, 0, 0, m, len(EXIF))]), EXIF)
        with self.assertRaises(WipeError):
            isobmff.clean_heif(d)

    def test_second_iloc_or_idat_is_refused(self):
        for extra in (iloc([]), box(b"idat", b"x")):
            with self.subTest(extra[4:8]):
                d = heif(lambda m: HDLR + iinf(infe(1, b"Exif")) + iloc([(1, 0, 0, m, len(EXIF))]) + box(b"idat") + extra, EXIF)
                with self.assertRaises(WipeError):
                    isobmff.clean_heif(d)

    def test_repeated_free_boxes_are_fine(self):
        d = heif(lambda m: HDLR + box(b"free", b"a") + iinf(infe(1, b"Exif")) + box(b"free", b"b") + box(b"skip")
                 + iloc([(1, 0, 0, m, len(EXIF))]), EXIF)
        self.assertNotIn(b"SECRET", self.clean(d))


class MimeItems(Base):
    """F2 and F7: XMP is a mime item; exiv2 shows any mime item as XMP, whatever its content type."""

    def build(self, content_type, payload=XMP):
        return heif(lambda m: HDLR + iinf(infe(1, b"hvc1"), infe(2, b"mime", b"\x00" + content_type + b"\x00"))
                    + iloc([(2, 0, 0, m, len(payload))]), payload)

    def test_every_mime_item_zeroed(self):
        for ctype in (b"application/rdf+xml", b"Application/RDF+XML", b"text/plain", b"application/octet-stream", b"image/jpeg"):
            with self.subTest(ctype):
                d = self.build(ctype)
                out = self.clean(d)
                self.assertNotIn(b"SECRET", out)
                self.assertNotIn(b"mime", out)
                self.assertTrue(out.endswith(bytes(len(XMP))))
                self.assertIn(b"hvc1", out)

    def test_mime_item_without_content_type_zeroed(self):
        d = heif(lambda m: HDLR + iinf(infe(2, b"mime", b"")) + iloc([(2, 0, 0, m, len(XMP))]), XMP)
        self.assertNotIn(b"SECRET", self.clean(d))


class IlocLayouts(Base):
    """F7: Exif in idat (construction method 1), iloc v0/v1/v2, 8-byte offset/length/base/index fields."""

    def test_exif_in_mdat_and_idat_with_wide_fields_zeroed(self):
        cases = [  # version, offset_size, length_size, base_size, index_size, method
            (0, 4, 4, 0, 0, 0), (1, 8, 8, 8, 8, 0), (2, 4, 4, 4, 4, 0),
            (1, 4, 4, 0, 0, 1), (2, 8, 8, 8, 4, 1), (1, 8, 4, 4, 8, 1),
        ]
        for version, osz, lsz, bsz, isz, method in cases:
            with self.subTest(version=version, osz=osz, lsz=lsz, bsz=bsz, isz=isz, method=method):
                base = 2 if bsz else 0
                pad = b"KEEP!"[:base + 1]

                def children(m):
                    layout = iloc([(1, method, base, (len(pad) if method else m) - base, len(EXIF))], version, osz, lsz, bsz, isz)
                    return HDLR + iinf(infe(1, b"Exif")) + layout + (box(b"idat", pad + EXIF) if method else b"")

                d = heif(children, b"" if method else EXIF)
                out = self.clean(d)
                self.assertNotIn(b"SECRET", out)
                self.assertIn(bytes(len(EXIF)), out)
                if method:
                    self.assertIn(b"idat" + pad + bytes(len(EXIF)), out)


class Malformed(Base):
    def test_iloc_zero_length_size_refused(self):
        """F6: 0-byte fields and 65535 extents never advanced the parser (8 s / 1 GB from a few hundred bytes)."""
        d = heif(lambda m: HDLR + iinf(infe(1, b"Exif")) + iloc([(2, 0, 0, 0, 0)] * 3, osz=0, lsz=0, extents=0xFFFF))
        with self.assertRaises(WipeError):
            isobmff.clean_heif(d)

    def test_iloc_extent_count_past_box_end_refused(self):
        d = heif(lambda m: HDLR + iinf(infe(1, b"Exif")) + iloc([(1, 0, 0, m, len(EXIF))], extents=0xFFFF), EXIF)
        with self.assertRaises(WipeError):
            isobmff.clean_heif(d)

    def test_infe_version_4_refused(self):
        """F17a: version 4 was parsed as version 3, which hid the Exif item type."""
        d = heif(lambda m: HDLR + iinf(infe(1, b"Exif", version=4)) + iloc([(1, 0, 0, m, len(EXIF))]), EXIF)
        with self.assertRaises(WipeError):
            isobmff.clean_heif(d)

    def test_short_movie_header_refused(self):
        """F17b: the header zeroing ran past a short mvhd into the next box, so udta kept its location."""
        udta = box(b"udta", box(b"\xa9xyz", b"\x00\x0c\x00\x00+48.8577+002.2945/"))
        for header in (box(b"mvhd"), box(b"mvhd", b"\x01" + bytes(15)), box(b"tkhd", bytes(8))):
            for moov in (box(b"moov", header + udta), box(b"moov", udta + header)):
                with self.subTest(moov=moov[:24]):
                    with self.assertRaises(WipeError):
                        isobmff.clean_heif(FTYP + moov)


if __name__ == "__main__":
    unittest.main()
