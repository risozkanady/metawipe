"""Tests for the read-only metadata dump (dump.py, --show): every format lists its metadata and survives truncation,
plus the round-5 regressions: JPEG segments after the first scan and data after EOI, bounded big TIFF tags, fill-byte
CPU, the zip decompression budget and UserComment byte order. Files are built here."""
from __future__ import annotations

import contextlib
import io
import shutil
import struct
import tempfile
import time
import unittest
import zipfile
import zlib
from pathlib import Path

from metawipe import cli, dump

try:
    import pypdf  # noqa: F401
except ImportError:  # pragma: no cover
    pypdf = None


def seg(marker, body):
    return bytes([0xFF, marker]) + struct.pack(">H", len(body) + 2) + body


def exif_gps(order="<"):
    """TIFF block: IFD0 with Artist and a GPS pointer, GPS IFD with latitude/longitude."""
    bom = b"II" if order == "<" else b"MM"
    artist = b"Jozko Mrkvicka\x00"
    ifd0 = struct.pack(order + "H", 2) + struct.pack(order + "HHII", 0x013B, 2, len(artist), 38)
    ifd0 += struct.pack(order + "HHII", 0x8825, 4, 1, 38 + len(artist)) + struct.pack(order + "I", 0)
    gps_off = 38 + len(artist)
    data_off = gps_off + 2 + 12 * 4 + 4
    gps = struct.pack(order + "H", 4)
    gps += struct.pack(order + "HHI", 1, 2, 2) + b"N\x00\x00\x00"
    gps += struct.pack(order + "HHII", 2, 5, 3, data_off)
    gps += struct.pack(order + "HHI", 3, 2, 2) + b"E\x00\x00\x00"
    gps += struct.pack(order + "HHII", 4, 5, 3, data_off + 24) + struct.pack(order + "I", 0)
    rationals = struct.pack(order + "12I", 48, 1, 8, 1, 30, 1, 17, 1, 6, 1, 0, 1)
    return bom + struct.pack(order + "HI", 42, 8) + ifd0 + artist + gps + rationals


TINY = b"\xff\xd8" + seg(0xDB, b"\x00" + bytes(64)) + seg(0xC0, b"\x08\x00\x01\x00\x01\x01\x01\x11\x00") + b"\xff\xd9"
GPS_JPEG = b"\xff\xd8" + seg(0xE1, b"Exif\x00\x00" + exif_gps()) + TINY[2:]


def png():
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
            + chunk(b"tEXt", b"Author\x00Jozko Mrkvicka") + chunk(b"IDAT", zlib.compress(b"\x00\x00"))
            + chunk(b"IEND", b""))


def docx():
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("docProps/core.xml", '<?xml version="1.0"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/'
                   'package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   '<dc:creator>Jozko Mrkvicka</dc:creator></cp:coreProperties>')
        z.writestr("word/document.xml", '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
                   'wordprocessingml/2006/main"><w:body><w:p/></w:body></w:document>')
    return b.getvalue()


def pdf():
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 10 10] >>", b"<< /Author (Jozko Mrkvicka) >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    return out + b"trailer\n<< /Size %d /Root 1 0 R /Info 4 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)


def box(btype, payload=b""):
    return struct.pack(">I", 8 + len(payload)) + btype + payload


def heic():
    """ftyp + meta (hdlr, iinf with one Exif item, iloc into mdat) + mdat holding the GPS Exif."""
    exif = b"\x00\x00\x00\x00" + exif_gps()
    ftyp = box(b"ftyp", b"heic\x00\x00\x00\x00mif1heic")
    infe = box(b"infe", b"\x02\x00\x00\x00\x00\x01\x00\x00Exif\x00")
    iinf = box(b"iinf", b"\x00\x00\x00\x00\x00\x01" + infe)

    def meta(offset):
        iloc = box(b"iloc", b"\x00\x00\x00\x00\x44\x00\x00\x01" + b"\x00\x01\x00\x00\x00\x01" + struct.pack(">II", offset, len(exif)))
        return box(b"meta", b"\x00\x00\x00\x00" + box(b"hdlr", bytes(8) + b"pict" + bytes(13)) + iinf + iloc)
    head = ftyp + meta(0)
    return ftyp + meta(len(head) + 8) + box(b"mdat", exif)


FIXTURES = {
    "gps.jpg": (GPS_JPEG, ("Exif GPS", "GPSPosition")),
    "fill.jpg": (b"\xff\xd8\xff\xff" + GPS_JPEG[2:], ("Exif GPS", "GPSPosition")),
    "text.png": (png(), ("PNG", "tEXt Author")),
    "gps.tif": (exif_gps(), ("Exif GPS", "GPSPosition")),
    "core.docx": (docx(), ("Document", "dc:creator")),
    "info.pdf": (pdf(), ("PDF", "Author")),
    "exif.heic": (heic(), ("Exif GPS", "GPSPosition")),
}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="mw-fix-r5-dump-"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def dump(self, name, data):
        p = self.dir / name
        p.write_bytes(data)
        return dump.dump_file(p)


class Formats(Base):
    def test_intact_files_list_their_metadata(self):
        for name, (data, (section, key)) in FIXTURES.items():
            if name.endswith(".pdf") and pypdf is None:
                continue
            with self.subTest(name):
                rows = self.dump(name, data)
                self.assertIn((section, key), [(s, n) for s, n, _v in rows], rows)
                self.assertTrue(any("Mrkvicka" in v or "48.141667" in v for _s, _n, v in rows), rows)

    def test_truncated_files_never_escape_the_section_guards(self):
        for name, (data, _want) in FIXTURES.items():
            if name.endswith(".pdf") and pypdf is None:
                continue
            for cut in sorted({len(data) * k // 20 for k in range(20)} | {1, 2, 3, 8, len(data) - 1}):
                with self.subTest(name, cut=cut):
                    rows = self.dump(name, data[:cut])
                    self.assertTrue(rows)
                    self.assertFalse([r for r in rows if r[0] == "File" and r[1] == "could not read"], rows)


class JpegWalk(Base):
    def test_segments_between_progressive_scans_are_listed(self):
        sos = seg(0xDA, b"\x01\x01\x00\x00\x3f\x00") + b"\x12\xff\x00\x34\xff\xd0\x56"
        data = (b"\xff\xd8" + seg(0xDB, b"\x00" + bytes(64)) + seg(0xC2, b"\x08\x00\x10\x00\x10\x01\x01\x11\x00")
                + sos + seg(0xFE, b"Jozko Mrkvicka") + seg(0xE1, b"Exif\x00\x00" + exif_gps()) + sos + b"\xff\xd9")
        rows = self.dump("prog.jpg", data)
        self.assertIn(("JPEG", "COM comment", "Jozko Mrkvicka (14 bytes)"), rows)
        self.assertIn("GPSPosition", [n for _s, n, _v in rows])
        self.assertNotIn("data after EOI", [n for _s, n, _v in rows])

    def test_appended_jpeg_is_reported_as_data_after_eoi(self):
        rows = self.dump("concat.jpg", TINY + GPS_JPEG)
        self.assertIn(("JPEG", "data after EOI"), [(s, n) for s, n, _v in rows])
        self.assertTrue(any(v.startswith(f"{len(GPS_JPEG)} bytes at offset {len(TINY)}: ff d8 ff e1") for _s, _n, v in rows))

    def test_missing_eoi_and_damage_are_reported(self):
        rows = self.dump("noeoi.jpg", TINY[:-2])
        self.assertIn(("JPEG", "end of image", "no EOI marker found (file truncated?)"), rows)
        rows = self.dump("bad.jpg", TINY[:-2] + b"\x00\x00")
        self.assertIn("could not read", [n for s, n, _v in rows if s == "JPEG"])
        self.assertIn("image", [n for s, n, _v in rows if s == "JPEG"])

    def test_fill_bytes_are_linear(self):
        p = self.dir / "ff.jpg"
        p.write_bytes(b"\xff\xd8" + b"\xff" * (16 << 20))
        t = time.monotonic()
        rows = dump.dump_file(p)
        self.assertLess(time.monotonic() - t, 2.0)
        self.assertIn(("JPEG", "end of image", "no EOI marker found (file truncated?)"), rows)


def tiff_with_entries(entries, size):
    """Little-endian TIFF of `size` bytes whose IFD0 holds `entries` (tag, type, count, offset); the IFD sits at the end."""
    ifd = struct.pack("<H", len(entries)) + b"".join(struct.pack("<HHII", *e) for e in entries) + b"\x00" * 4
    data = bytearray(size)
    data[:8] = b"II*\x00" + struct.pack("<I", size - len(ifd))
    data[size - len(ifd):] = ifd
    return data


class BigTiffTags(Base):
    def test_shared_xmp_value_is_parsed_once(self):
        xmp = (b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
               b'<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/" dc:creator="Jozko"/></rdf:RDF></x:xmpmeta>')
        xmp += b" " * ((1 << 20) - len(xmp))
        data = tiff_with_entries([(0x02BC, 7, len(xmp), 16)] * 50, (1 << 20) + 4096)
        data[16:16 + len(xmp)] = xmp
        rows = self.dump("xmp.tif", bytes(data))
        self.assertEqual(sum(1 for s, n, _v in rows if (s, n) == ("XMP", "XMP packet")), 1)
        self.assertEqual(sum(1 for s, n, _v in rows if (s, n) == ("TIFF", "XMLPacket")), 50)
        self.assertIn(("XMP", "XMP dc:creator", "Jozko"), rows)

    def test_distinct_big_values_stay_within_the_read_budget(self):
        size = 2 << 20
        entries = [(0x02BC, 7, (1 << 20) - 64, 8 + 1000 * i) for i in range(200)]
        rows = self.dump("many.tif", bytes(tiff_with_entries(entries, size)))
        self.assertEqual(sum(1 for s, n, _v in rows if (s, n) == ("TIFF", "XMLPacket")), 200)
        self.assertLessEqual(sum(1 for s, n, _v in rows if (s, n) == ("XMP", "XMP packet")), 40)


class ZipBudget(Base):
    def test_decompression_stops_at_the_package_budget(self):
        part = b"<a>" + b" " * ((8 << 20) - 3)
        p = self.dir / "parts.xlsx"
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
            for i in range(40):
                z.writestr(f"xl/comments{i}.xml", part)
        t = time.monotonic()
        rows = dump.dump_file(p)
        self.assertLess(time.monotonic() - t, 10.0)
        notes = [v for s, n, v in rows if (s, n) == ("Document", "note")]
        self.assertTrue(any("decompressed" in v for v in notes), rows)


class UserComment(Base):
    def test_unicode_follows_the_tiff_byte_order(self):
        for order, enc in (("<", "utf-16-le"), (">", "utf-16-be")):
            uc = b"UNICODE\x00" + "John Smith".encode(enc)
            bom = b"II" if order == "<" else b"MM"
            ifd0 = struct.pack(order + "H", 1) + struct.pack(order + "HHII", 0x8769, 4, 1, 26) + struct.pack(order + "I", 0)
            exif_ifd = struct.pack(order + "H", 1) + struct.pack(order + "HHII", 0x9286, 7, len(uc), 44) + struct.pack(order + "I", 0)
            tiff = bom + struct.pack(order + "HI", 42, 8) + ifd0 + exif_ifd + uc
            with self.subTest(order=order):
                rows = self.dump("uc.jpg", b"\xff\xd8" + seg(0xE1, b"Exif\x00\x00" + tiff) + TINY[2:])
                self.assertIn(("Exif", "UserComment", "(UNICODE) John Smith"), rows)


class ShowCli(Base):
    def test_show_continues_past_bad_files(self):
        (self.dir / "a.jpg").write_bytes(b"\xff\xd8\xff\xe1\x00")
        (self.dir / "b.png").write_bytes(b"not a png")
        (self.dir / "c.jpg").write_bytes(GPS_JPEG)
        for extra, status in (([], 0), ([str(self.dir / "missing.jpg")], 1)):
            with self.subTest(extra=extra):
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    rc = cli.main(["--show", str(self.dir)] + extra)
                text = stdout.getvalue()
                self.assertEqual(rc, status)
                for name in ("a.jpg", "b.png", "c.jpg"):
                    self.assertIn(f"== {self.dir / name}", text)
                self.assertIn("GPSPosition", text.split(f"== {self.dir / 'c.jpg'}")[1])
                self.assertEqual("no such file" in stderr.getvalue(), bool(extra))


if __name__ == "__main__":
    unittest.main()
