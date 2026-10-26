"""Regression tests for the round-3 image findings (TIFF sweep hardening, orNT, HEIF names)."""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import tracemalloc
import unittest
import zlib
from pathlib import Path

from metawipe import images, isobmff
from metawipe.common import Context, WipeError

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

HAVE = {t: shutil.which(t) is not None for t in (
    "magick", "tiffinfo", "tiffdump", "tiff2rgba", "ffmpeg", "ffprobe", "heif-enc", "heif-info", "heif-convert", "exiv2")}
SECRET = b"SECRET-STALE-VALUE-0123456789"

_SIZES = {1: 1, 2: 1, 3: 2, 4: 4}
_CODES = {1: "B", 2: "B", 3: "H", 4: "I"}


def build_tiff(pages, little=True, truncate=0):
    """Build a classic TIFF. A page is {'tags': [(tag, type, values)], 'strips': [bytes], 'counts': [int] or None,
    'strip_tag': 273 or 324, 'jpeg': bytes or None}. Strip offsets/counts (and 513/514) are filled in."""
    o = "<" if little else ">"
    out = bytearray(b"II*\x00" if little else b"MM\x00*")
    out += struct.pack(o + "I", 0)
    ptr = 4
    for page in pages:
        strips = page.get("strips") or []
        jpeg = page.get("jpeg")
        tags = list(page["tags"])
        n = len(tags) + (2 if strips else 0) + (2 if jpeg else 0)
        base = len(out)
        pos = base + 2 + 12 * n + 4
        blobs = []
        entries = []

        def put(tag, typ, vals, at=None):
            nonlocal pos
            if isinstance(vals, str):
                vals = vals.encode() + b"\x00"
            if isinstance(vals, bytes):
                raw, cnt = vals, len(vals)
            else:
                raw, cnt = struct.pack(o + f"{len(vals)}{_CODES[typ]}", *vals), len(vals)
            if len(raw) <= 4:
                field = raw.ljust(4, b"\x00")
            else:
                if at is None:
                    at = pos
                    pos += len(raw) + (len(raw) & 1)
                blobs.append((at, raw))
                field = struct.pack(o + "I", at)
            entries.append((tag, struct.pack(o + "HHI", tag, typ, cnt) + field))

        for tag, typ, vals in tags:
            put(tag, typ, vals)
        if strips:
            counts = page.get("counts") or [len(s) for s in strips]
            st = page.get("strip_tag", 273)
            arr = 4 * len(strips) if len(strips) > 1 else 0
            a_off, a_cnt = pos, pos + arr + (arr & 1)
            pos = a_cnt + arr + (arr & 1)
            offs = []
            for s in strips:
                offs.append(pos)
                blobs.append((pos, s))
                pos += len(s) + (len(s) & 1)
            put(st, 4, offs, a_off)
            put(279 if st == 273 else 325, 4, counts, a_cnt)
        if jpeg:
            blobs.append((pos, jpeg))
            put(513, 4, [pos])
            put(514, 4, [len(jpeg)])
            pos += len(jpeg) + (len(jpeg) & 1)
        entries.sort(key=lambda x: x[0])
        out[ptr:ptr + 4] = struct.pack(o + "I", base)
        ptr = base + 2 + 12 * n
        out += struct.pack(o + "H", n) + b"".join(e for _, e in entries) + struct.pack(o + "I", 0)
        for at, data in blobs:
            if len(out) < at + len(data):
                out += bytes(at + len(data) - len(out))
            out[at:at + len(data)] = data
        if len(out) < pos:
            out += bytes(pos - len(out))
    if truncate:
        del out[len(out) - truncate:]
    return bytes(out)


def rgb_page(width=8, height=6, rps=2, compression=1, extra=(), counts=None, tag_override=None):
    rows = [bytes((x * 7 + y * 13 + c) & 255 for x in range(width) for c in range(3)) for y in range(height)]
    strips = [b"".join(rows[i:i + rps]) for i in range(0, height, rps)]
    tags = [(256, 4, [width]), (257, 4, [height]), (258, 3, [8, 8, 8]), (259, 3, [compression]),
            (262, 3, [2]), (277, 3, [3]), (278, 3, [rps]), (284, 3, [1])] + list(extra)
    return {"tags": tags, "strips": strips, "counts": counts}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="mw-fix-images-"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def clean_file(self, data, name="a.tif", full=False):
        src, dst = self.dir / name, self.dir / ("out-" + name)
        src.write_bytes(data)
        ctx = Context(full=full, warn=lambda m: None)
        images.tiff(src, dst, ctx)
        return dst.read_bytes(), ctx


def _tiff_strip_data(data):
    """Concatenated strip bytes of the first IFD, read without the library under test."""
    little = data[:2] == b"II"
    o = "<" if little else ">"
    ifd = struct.unpack_from(o + "I", data, 4)[0]
    n = struct.unpack_from(o + "H", data, ifd)[0]
    ent = {}
    for k in range(n):
        tag, typ, cnt = struct.unpack_from(o + "HHI", data, ifd + 2 + 12 * k)
        ent[tag] = (typ, cnt, ifd + 2 + 12 * k + 8)

    def vals(tag):
        typ, cnt, at = ent[tag]
        size = (2 if typ == 3 else 4) * cnt
        if size > 4:
            at = struct.unpack_from(o + "I", data, at)[0]
        return struct.unpack_from(o + f"{cnt}{'H' if typ == 3 else 'I'}", data, at)

    return [(a, c) for a, c in zip(vals(273), vals(279))]


def set_tag(data, tag, vals, index=0):
    """Overwrite the value(s) of one tag of IFD 0 in place (same count, same type)."""
    o = "<" if data[:2] == b"II" else ">"
    d = bytearray(data)
    ifd = struct.unpack_from(o + "I", d, 4)[0]
    for k in range(struct.unpack_from(o + "H", d, ifd)[0]):
        e = ifd + 2 + 12 * k
        t, typ, cnt = struct.unpack_from(o + "HHI", d, e)
        if t == tag:
            at = e + 8 if _SIZES[typ] * cnt <= 4 else struct.unpack_from(o + "I", d, e + 8)[0]
            vals = list(vals) if isinstance(vals, (list, tuple)) else [vals]
            struct.pack_into(o + f"{len(vals)}{_CODES[typ]}", d, at + index * _SIZES[typ], *vals)
            return bytes(d)
    raise KeyError(tag)


def pixels_magick(path):
    out = subprocess.run(["magick", str(path), "-depth", "8", "rgb:-"], capture_output=True)
    return out.returncode, out.stdout


class TiffTruncatedAndBogusOffsets(Base):
    """Findings 0: references beyond EOF must fail closed and never change the buffer length."""

    def test_strip_offset_far_beyond_eof_raises(self):
        d = build_tiff([rgb_page(compression=5)])
        bad = set_tag(d, 273, 300000000, index=0)
        with self.assertRaises(WipeError) as cm:
            images.clean_tiff(bad)
        self.assertIn("truncated", str(cm.exception))

    def test_truncated_file_raises_instead_of_zero_padding(self):
        d = build_tiff([rgb_page(height=30, rps=1)], truncate=100)  # IFD first, strips last, cut off
        with self.assertRaises(WipeError):
            images.clean_tiff(d)
        if HAVE["tiffinfo"]:  # an independent reader agrees the file is damaged
            p = self.dir / "t.tif"
            p.write_bytes(d)
            out = subprocess.run(["tiff2rgba", "-c", "none", str(p), str(self.dir / "r.tif")], capture_output=True, text=True) \
                if HAVE["tiff2rgba"] else None
            if out is not None:
                self.assertTrue(out.returncode != 0 or "rror" in out.stderr, out.stderr)

    def test_out_of_line_value_beyond_eof_raises(self):
        page = rgb_page(extra=[(34675, 1, b"ICCPROFILE" * 8)])
        d = build_tiff([page])
        bad = set_tag(d, 34675, 0)  # keep size
        # point the ICC value past the end of the file
        o = "<"
        ifd = struct.unpack_from(o + "I", d, 4)[0]
        buf = bytearray(d)
        for k in range(struct.unpack_from(o + "H", buf, ifd)[0]):
            e = ifd + 2 + 12 * k
            if struct.unpack_from(o + "H", buf, e)[0] == 34675:
                struct.pack_into(o + "I", buf, e + 8, len(buf) + 5000)
        with self.assertRaises(WipeError):
            images.clean_tiff(bytes(buf))

    def test_output_size_never_changes(self):
        d = build_tiff([rgb_page()])
        out, _ = self.clean_file(d)
        self.assertEqual(len(out), len(d))
        d2 = build_tiff([rgb_page(compression=5, extra=[(270, 2, "SECRET-DESCRIPTION-LONG-VALUE")])])
        out, _ = self.clean_file(d2)
        self.assertEqual(len(out), len(d2))
        self.assertNotIn(b"SECRET-DESCRIPTION", out)

    def test_cli_rejects_truncated_tiff_without_output(self):
        p = self.dir / "cut.tif"
        p.write_bytes(build_tiff([rgb_page(height=30, rps=1)], truncate=100))
        res = subprocess.run([sys.executable, "-m", "metawipe", "-o", str(self.dir / "o"), "-f", str(p)],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertEqual([f.name for f in (self.dir / "o").iterdir()] if (self.dir / "o").exists() else [], [])


class TiffBogusByteCounts(Base):
    """Finding 1: byte counts that libtiff repairs must never make the sweep wipe pixel data."""

    def check_pixels_kept(self, page_args, bad_counts, note_expected=True):
        page = rgb_page(**page_args)
        good = build_tiff([page])
        bad = build_tiff([dict(page, counts=bad_counts)])
        out, ctx = self.clean_file(bad)
        before = _tiff_strip_data(bad)
        # all strip bytes must be identical in the output (read with the offsets of the input)
        strips = page["strips"]
        for (off, _), s in zip(before, strips):
            self.assertEqual(out[off:off + len(s)], s)
        if note_expected:
            self.assertTrue(ctx.residue, "sweep skipped: must be reported as leftover")
        if HAVE["magick"]:
            rc0, px0 = pixels_magick(self.dir / "a.tif")
            self.assertEqual(rc0, 0)
            self.assertEqual(px0, pixels_magick(self.dir / "out-a.tif")[1])
        return out

    def test_single_strip_count_zero(self):
        n = 8 * 6 * 3
        self.check_pixels_kept(dict(rps=6), [0])

    def test_single_strip_count_ten(self):
        self.check_pixels_kept(dict(rps=6), [10])

    def test_multi_strip_one_count_too_small(self):
        self.check_pixels_kept(dict(rps=2), [48, 3, 48])

    def test_exact_counts_still_swept(self):
        page = rgb_page(extra=[(270, 2, "SECRET-DESCRIPTION-LONG-VALUE")])
        out, ctx = self.clean_file(build_tiff([page]))
        self.assertNotIn(b"SECRET-DESCRIPTION", out)
        self.assertEqual(ctx.residue, [])

    def test_tiled_uncompressed_small_count_is_not_swept(self):
        w = h = 16
        tile = bytes(range(256)) * 3  # 16x16 RGB
        page = {"tags": [(256, 4, [w]), (257, 4, [h]), (258, 3, [8, 8, 8]), (259, 3, [1]), (262, 3, [2]),
                         (277, 3, [3]), (322, 4, [16]), (323, 4, [16])],
                "strips": [tile], "strip_tag": 324, "counts": [10]}
        d = build_tiff([page])
        out, ctx = self.clean_file(d)
        self.assertTrue(ctx.residue)
        self.assertIn(tile, out)
        good, ctx2 = self.clean_file(build_tiff([dict(page, counts=None)]))
        self.assertEqual(ctx2.residue, [])
        self.assertIn(tile, good)

    def test_planar_uncompressed_exact_counts_ok(self):
        w, h = 8, 4
        planes = [bytes([p + 1]) * (w * h) for p in range(3)]
        page = {"tags": [(256, 4, [w]), (257, 4, [h]), (258, 3, [8, 8, 8]), (259, 3, [1]), (262, 3, [2]),
                         (277, 3, [3]), (278, 3, [h]), (284, 3, [2]), (270, 2, "SECRET-DESCRIPTION-LONG-VALUE")],
                "strips": planes}
        out, ctx = self.clean_file(build_tiff([page]))
        self.assertEqual(ctx.residue, [])
        self.assertNotIn(b"SECRET-DESCRIPTION", out)
        for plane in planes:
            self.assertIn(plane, out)

    def test_missing_geometry_is_not_swept(self):
        page = rgb_page()
        page["tags"] = [t for t in page["tags"] if t[0] not in (256, 257)]
        out, ctx = self.clean_file(build_tiff([page]))
        self.assertTrue(ctx.residue)
        for s in page["strips"]:
            self.assertIn(s, out)


@unittest.skipUnless(Image, "Pillow missing")
class TiffOldStyleJpeg(Base):
    """Findings 2 and 8."""

    @staticmethod
    def jpeg(comment=b""):
        import io

        b = io.BytesIO()
        Image.effect_noise((40, 30), 60).convert("RGB").save(b, "JPEG", subsampling=2, comment=comment)
        return b.getvalue()

    def ojpeg_page(self, jpeg, w=40, h=30):
        return {"tags": [(256, 4, [w]), (257, 4, [h]), (258, 3, [8, 8, 8]), (259, 3, [6]), (262, 3, [6]), (277, 3, [3])],
                "jpeg": jpeg}

    def test_only_ifd_with_513_is_kept(self):
        jpeg = self.jpeg()
        d = build_tiff([self.ojpeg_page(jpeg)])
        out, ctx = self.clean_file(d)
        self.assertGreater(len(out), 8)
        self.assertIn(jpeg, out)
        self.assertNotEqual(out[4:8], bytes(4))
        if HAVE["magick"]:
            self.assertEqual(pixels_magick(self.dir / "a.tif")[1], pixels_magick(self.dir / "out-a.tif")[1])
            self.assertNotEqual(pixels_magick(self.dir / "out-a.tif")[1], b"")

    def test_ifd0_with_513_only_even_without_dimensions_is_kept(self):
        jpeg = self.jpeg()
        d = build_tiff([{"tags": [(259, 3, [6])], "jpeg": jpeg}])
        out, _ = self.clean_file(d)
        self.assertIn(jpeg, out)

    def test_later_513_only_ifd_is_a_thumbnail_and_removed(self):
        jpeg, thumb = self.jpeg(), self.jpeg()[:-2] + b"\xff\xd9" + b"THUMBMARK"
        page = rgb_page()
        d = build_tiff([page, {"tags": [(259, 3, [6])], "jpeg": thumb}])
        out, ctx = self.clean_file(d)
        self.assertNotIn(b"THUMBMARK", out)
        for s in page["strips"]:
            self.assertIn(s, out)
        self.assertEqual(ctx.residue, [])

    def test_later_513_ifd_with_dimensions_is_a_page(self):
        page = rgb_page()
        d = build_tiff([page, self.ojpeg_page(self.jpeg(b"x"))])
        out, ctx = self.clean_file(d)
        self.assertEqual(len(out), len(d))
        self.assertTrue(ctx.residue)

    def test_embedded_jpeg_metadata_is_reported(self):
        d = build_tiff([self.ojpeg_page(self.jpeg(b"SECRET-JPEG-COMMENT"))])
        out, ctx = self.clean_file(d, full=True)
        self.assertIn(b"SECRET-JPEG-COMMENT", out)  # not rewritten
        self.assertTrue(any("old-style JPEG" in m for m in ctx.residue), ctx.residue)

    def test_old_style_jpeg_in_strips_is_reported(self):
        out, ctx = self.clean_file(build_tiff([rgb_page(compression=6)]), full=True)
        self.assertTrue(any("old-style JPEG" in m for m in ctx.residue), ctx.residue)

    def test_chain_without_ifd_fails(self):
        with self.assertRaises(WipeError):
            images.clean_tiff(b"II*\x00\x00\x00\x00\x00" + bytes(16))


class TiffBudgets(Base):
    """Findings 3 and 4: crafted files must not cost gigabytes or hours."""

    @staticmethod
    def shared_arrays(k, m):
        """k IFDs sharing one m-entry StripOffsets/StripByteCounts array (1 x m gray image, one row per strip)."""
        n = 7
        ifd_len = 2 + 12 * n + 4
        a_off = 8 + k * ifd_len
        a_cnt = a_off + 4 * m
        data = a_cnt + 4 * m
        buf = bytearray(data + m)
        buf[:8] = b"II*\x00" + struct.pack("<I", 8)
        buf[a_off:a_cnt] = b"".join(struct.pack("<I", data + i) for i in range(m))
        buf[a_cnt:data] = struct.pack("<I", 1) * m
        for i in range(k):
            at = 8 + i * ifd_len
            ent = [(256, 4, 1, 1), (257, 4, 1, m), (258, 3, 1, 8), (259, 3, 1, 5), (262, 3, 1, 1),
                   (273, 4, m, a_off), (279, 4, m, a_cnt)]
            buf[at:at + 2] = struct.pack("<H", n)
            for j, (t, typ, c, v) in enumerate(ent):
                buf[at + 2 + 12 * j:at + 14 + 12 * j] = struct.pack("<HHII", t, typ, c, v)
            nxt = at + ifd_len if i + 1 < k else 0
            buf[at + 2 + 12 * n:at + 6 + 12 * n] = struct.pack("<I", nxt)
        return bytes(buf)

    def test_shared_strip_arrays_do_not_multiply_memory(self):
        d = self.shared_arrays(100, 20000)  # old sweep built 2 million ranges (about 400 MB)
        tracemalloc.start()
        try:
            t = time.time()
            out = images.clean_tiff(d)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(len(out), len(d))
        self.assertLess(peak, 60 << 20)
        self.assertLess(time.time() - t, 20)

    def test_overlapping_huge_ifds_are_rejected_quickly(self):
        k, count = 100, 65535
        tail = 8 + 2 + 12 * count
        buf = bytearray(tail + 4 * k + 16)
        buf[:8] = b"II*\x00" + struct.pack("<I", 8)
        for i in range(k):
            buf[8 + 4 * i:10 + 4 * i] = b"\xff\xff"  # IFD i at 8+4i claims 65535 entries
            nxt = 8 + 4 * (i + 1) if i + 1 < k else 0
            struct.pack_into("<I", buf, tail + 4 * i, nxt)
        t = time.time()
        with self.assertRaises(WipeError):
            images.clean_tiff(bytes(buf))
        self.assertLess(time.time() - t, 10)  # was about 70 ms per IFD and no error

    def test_ifd_chain_limit(self):
        n = 10005
        buf = bytearray(8 + n * 6)
        buf[:8] = b"II*\x00" + struct.pack("<I", 8)
        for i in range(n):
            at = 8 + 6 * i
            struct.pack_into("<HI", buf, at, 0, at + 6 if i + 1 < n else 0)
        with self.assertRaises(WipeError):
            images.clean_tiff(bytes(buf))

    def test_valid_multipage_still_works(self):
        pages = [rgb_page(width=8 + i, height=4) for i in range(30)]
        d = build_tiff(pages)
        out, ctx = self.clean_file(d)
        self.assertEqual(len(out), len(d))
        self.assertEqual(ctx.residue, [])


class TiffLeftoverReporting(Base):
    """Finding 5: the fallback path must report INCOMPLETE under --full."""

    def stale_with_jpeg_tables(self):
        return build_tiff([rgb_page(compression=5, extra=[(519, 4, [8]), (270, 2, "SECRET-STALE-DESCRIPTION-LONG")])])

    def test_handler_reports_leftover(self):
        out, ctx = self.clean_file(self.stale_with_jpeg_tables(), full=True)
        self.assertTrue(any("old-style JPEG tables" in m for m in ctx.residue), ctx.residue)

    def test_cli_full_exits_2_and_plain_run_exits_0(self):
        p = self.dir / "t.tif"
        p.write_bytes(self.stale_with_jpeg_tables())
        for full, rc in ((True, 2), (False, 0)):
            args = [sys.executable, "-m", "metawipe", "-o", str(self.dir / ("o%d" % full)), "-f"] + (["--full"] if full else []) + [str(p)]
            res = subprocess.run(args, capture_output=True, text=True, timeout=60)
            self.assertEqual(res.returncode, rc, res.stdout + res.stderr)
            if full:
                self.assertIn("INCOMPLETE", res.stdout + res.stderr)


def png_chunk(t, d):
    return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))


class PngOrientationChunk(Base):
    """Finding 6."""

    def make(self):
        ihdr = png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        idat = png_chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        return b"\x89PNG\r\n\x1a\n" + ihdr + png_chunk(b"orNT", b"\x06") + png_chunk(b"tEXt", b"Comment\x00SECRET") + idat + png_chunk(b"IEND", b"")

    def test_ornt_is_kept(self):
        out = images.clean_png(self.make())
        self.assertIn(png_chunk(b"orNT", b"\x06"), out)
        self.assertNotIn(b"SECRET", out)

    @unittest.skipUnless(Image and HAVE["magick"], "needs Pillow and ImageMagick")
    def test_imagemagick_strip_png_keeps_orientation(self):
        jpg, png = self.dir / "r.jpg", self.dir / "r.png"
        exif = Image.Exif()
        exif[0x0112] = 6
        Image.effect_noise((40, 30), 50).convert("RGB").save(jpg, exif=exif)
        subprocess.run(["magick", str(jpg), "-strip", str(png)], check=True, capture_output=True)
        src = png.read_bytes()
        if b"orNT" not in src and b"eXIf" not in src:
            self.skipTest("this ImageMagick does not store the orientation")
        png.write_bytes(images.clean_png(src))
        out = subprocess.run(["identify", "-format", "%[orientation]", str(png)], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "RightTop")


def isobmff_box(btype, payload):
    return struct.pack(">I", 8 + len(payload)) + btype + payload


class HeifNames(Base):
    """Finding 7."""

    def synthetic(self):
        hdlr = isobmff_box(b"hdlr", b"\x00" * 4 + b"\x00" * 4 + b"pict" + b"\x00" * 12 + b"SECRET-HANDLER-NAME\x00")
        infe1 = isobmff_box(b"infe", b"\x02\x00\x00\x00" + struct.pack(">HH", 1, 0) + b"hvc1" + b"SECRET-ITEMNAME-primary\x00")
        infe2 = isobmff_box(b"infe", b"\x02\x00\x00\x00" + struct.pack(">HH", 2, 0) + b"mime" + b"SECRET-MIMENAME\x00image/x-keep\x00")
        infe3 = isobmff_box(b"infe", b"\x00\x00\x00\x00" + struct.pack(">HH", 3, 0) + b"SECRET-V0NAME\x00image/jpeg\x00")
        iinf = isobmff_box(b"iinf", b"\x00\x00\x00\x00" + struct.pack(">H", 3) + infe1 + infe2 + infe3)
        meta = isobmff_box(b"meta", b"\x00\x00\x00\x00" + hdlr + iinf)
        return isobmff_box(b"ftyp", b"heic\x00\x00\x00\x00heic") + meta

    def test_synthetic_names_blanked_in_place(self):
        d = self.synthetic()
        out = isobmff.clean_heif(d)
        self.assertEqual(len(out), len(d))
        for needle in (b"SECRET-HANDLER", b"SECRET-ITEMNAME", b"SECRET-MIMENAME", b"SECRET-V0NAME"):
            self.assertIn(needle, d)
            self.assertNotIn(needle, out)
        self.assertIn(b"image/x-keep\x00", out)  # strings after the name stay where they were
        self.assertIn(b"image/jpeg\x00", out)
        self.assertIn(b"hvc1", out)

    def test_synthetic_corrupt_boxes_do_not_crash(self):
        d = self.synthetic()
        for cut in (60, 80, 100, 120, len(d) - 1):
            try:
                isobmff.clean_heif(d[:cut])
            except WipeError:
                pass

    @unittest.skipUnless(HAVE["ffmpeg"] and HAVE["ffprobe"], "ffmpeg missing")
    def test_avif_sequence_and_still_names_blanked_decode_identical(self):
        enc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
        if "libaom-av1" not in enc:
            self.skipTest("no libaom-av1 encoder")
        base = ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=96x64:rate=5:duration=1",
                "-c:v", "libaom-av1", "-cpu-used", "8", "-crf", "40", "-b:v", "0"]
        seq, still = self.dir / "seq.avif", self.dir / "still.avif"
        subprocess.run(base + ["-metadata:s:v", "handler_name=SECRET-HANDLER", "-f", "avif", str(seq)], check=True)
        subprocess.run(base + ["-frames:v", "1", "-metadata:s:v", "handler_name=SECRET-HANDLER3", "-f", "avif", str(still)], check=True)
        for path in (seq, still):
            raw = path.read_bytes()
            self.assertIn(b"SECRET-HANDLER", raw)
            frames_before = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(path), "-f", "framemd5", "-"],
                                           capture_output=True, text=True).stdout
            out = isobmff.clean_heif(raw)
            self.assertEqual(len(out), len(raw))
            self.assertNotIn(b"SECRET-HANDLER", out)
            self.assertNotIn(b"Lavc", out)
            path.write_bytes(out)
            frames_after = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(path), "-f", "framemd5", "-"],
                                          capture_output=True, text=True).stdout
            self.assertTrue(frames_before)
            self.assertEqual(frames_before, frames_after)
        tags = subprocess.run(["ffprobe", "-loglevel", "error", "-show_entries", "stream_tags:format_tags", "-of", "default=nw=1", str(seq)],
                              capture_output=True, text=True).stdout
        self.assertNotIn("SECRET", tags)
        self.assertNotIn("Lavc", tags)

    @unittest.skipUnless(HAVE["heif-enc"] and HAVE["heif-convert"] and Image, "libheif tools or Pillow missing")
    def test_heic_still_decodes_identically_after_name_blanking(self):
        src = self.dir / "s.jpg"
        Image.effect_mandelbrot((64, 48), (-3, -2.5, 2, 2.5), 100).convert("RGB").save(src)
        heic = self.dir / "a.heic"
        subprocess.run(["heif-enc", "-q", "70", str(src), "-o", str(heic)], check=True, capture_output=True)
        before = self.dir / "b.png"
        subprocess.run(["heif-convert", str(heic), str(before)], check=True, capture_output=True)
        heic.write_bytes(isobmff.clean_heif(heic.read_bytes()))
        after = self.dir / "c.png"
        subprocess.run(["heif-convert", str(heic), str(after)], check=True, capture_output=True)
        self.assertEqual(Image.open(before).tobytes(), Image.open(after).tobytes())


if __name__ == "__main__":
    unittest.main()
