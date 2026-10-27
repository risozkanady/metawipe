"""--keep for HEIC/HEIF/AVIF: the Exif item is rebuilt with only the kept fields, everything else stays blanked."""
import struct
import tempfile
import unittest
from pathlib import Path

from metawipe import isobmff
from metawipe.common import Context
from tests.test_fix_r5_heif import HDLR, XMP, heif, iinf, iloc, infe
from tests.test_keep_images import ASCII, LONG, asc, build, parse, rat

DATE = asc("2020:01:02 03:04:05")
SRC = build([(271, *asc("SECRETMAKE")), (306, *DATE), (315, *asc("SECRETART")), (270, *asc("SECRETDESC"))],
            exif=[(36867, *DATE)], gps=[(1, *asc("N")), (2, *rat((50, 1), (1, 1), (0, 1))), (29, *asc("2020:01:02"))])


def make(tiff=SRC, offset=0, slack=0):
    item = struct.pack(">I", offset) + tiff + bytes(slack)
    return heif(lambda m: HDLR + iinf(infe(1, b"hvc1"), infe(2, b"Exif", b"SECRET-NAME\x00"), infe(3, b"mime", b"\x00application/rdf+xml\x00"))
                + iloc([(2, 0, 0, m, len(item)), (3, 0, 0, m + len(item), len(XMP))]), item + XMP)


def run(data, keep):
    kept, warns = set(), []
    out = isobmff.clean_heif(data, frozenset(keep), kept, warns.append)
    assert len(out) == len(data)
    return out, kept, warns


def exif_of(out):
    i = out.find(b"MM\x00*")
    return parse(out[i:]) if i >= 0 else None


class KeepHeif(unittest.TestCase):
    def test_default_is_unchanged(self):
        d = make()
        out, kept, _ = run(d, ())
        self.assertEqual(out, isobmff.clean_heif(d))
        self.assertEqual(kept, set())
        self.assertNotIn(b"Exif", out[24:])  # item type is `free`
        self.assertIsNone(exif_of(out))

    def test_dates_only(self):
        out, kept, warns = run(make(slack=200), {"dates"})
        t = exif_of(out)
        self.assertEqual(kept, {"dates"})
        self.assertEqual(t["0"][306][2], DATE[2])
        self.assertEqual(t["e"][36867][2], DATE[2])
        self.assertEqual(set(t["g"]) - {0}, {29})  # only the GPS date stamp; 0 is the version tag
        self.assertNotIn(2, t["g"])  # latitude is location
        for secret in (b"SECRET", b"rdf", b"<x:xmpmeta"):
            self.assertNotIn(secret, out)
        self.assertIn(b"Exif", out[:out.find(b"mdat")])  # the item keeps its type
        self.assertEqual(warns, [])

    def test_location_and_device(self):
        out, kept, _ = run(make(), {"location", "device"})
        t = exif_of(out)
        self.assertEqual(kept, {"location", "device"})
        self.assertIn(2, t["g"])
        self.assertEqual(t["0"][271][2], b"SECRETMAKE\0")
        self.assertNotIn(306, t["0"])
        self.assertNotIn(b"SECRETART", out)

    def test_nothing_to_keep_blanks_the_item(self):
        out, kept, _ = run(make(build([(306, *DATE)])), {"people"})
        self.assertEqual(kept, set())
        self.assertIsNone(exif_of(out))
        self.assertNotIn(b"Exif", out[24:out.find(b"mdat")])

    def test_unreadable_exif_is_dropped_with_a_warning(self):
        for item in (b"MM\x00*\xff\xff\xff\xff" + bytes(16), b"short"):
            out, kept, warns = run(make(item), {"dates"})
            self.assertEqual(kept, set())
            self.assertEqual(len(warns), 1)
            self.assertIsNone(exif_of(out))
        out, kept, warns = run(make(offset=10**6), {"dates"})  # tiff_header_offset beyond the item
        self.assertEqual((kept, len(warns)), (set(), 1))

    def test_header_offset_is_honoured(self):
        out, kept, _ = run(make(b"Exif\0\0" + SRC, offset=6), {"dates"})
        self.assertEqual(kept, {"dates"})
        self.assertNotIn(b"SECRET", out)

    def test_xmp_item_stays_blanked(self):
        out, _, _ = run(make(), {"dates", "software", "people", "descriptions"})
        self.assertNotIn(b"xmpmeta", out)

    def test_cli_handler_reports_kept(self):
        with tempfile.TemporaryDirectory() as d:
            src, dst = Path(d, "a.heic"), Path(d, "b.heic")
            src.write_bytes(make())
            ctx = Context(keep=frozenset({"dates"}))
            isobmff.heif(src, dst, ctx)
            self.assertEqual(ctx.kept, {"dates"})
            self.assertIn(b"2020:01:02 03:04:05", dst.read_bytes())
