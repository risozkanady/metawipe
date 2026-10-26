"""Regression tests for the round-4 TIFF findings: thumbnail IFDs with dimensions, shared strip tables, IFD limit."""
from __future__ import annotations

import shutil
import struct
import subprocess
import tempfile
import time
import tracemalloc
import unittest
from pathlib import Path

from metawipe import images
from metawipe.common import Context, WipeError
from tests.test_fix_images import build_tiff, rgb_page

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

HAVE = {t: shutil.which(t) is not None for t in ("exiv2", "tiffcp", "tiffinfo")}


def jpeg(size=(16, 12), mark=b"THUMBMARK"):
    import io

    b = io.BytesIO()
    Image.effect_noise(size, 60).convert("RGB").save(b, "JPEG")
    return b.getvalue()[:-2] + b"\xff\xd9" + mark


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="mw-fix-r4-images-"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def assertGone(self, needle, data):
        self.assertFalse(needle in data, "%r survived" % needle[:20])

    def assertKept(self, needle, data):
        self.assertTrue(needle in data, "%r lost" % needle[:20])

    def clean_file(self, data, name="a.tif", full=False):
        src, dst = self.dir / name, self.dir / ("out-" + name)
        src.write_bytes(data)
        ctx = Context(full=full, warn=lambda m: None)
        images.tiff(src, dst, ctx)
        return dst.read_bytes(), ctx


@unittest.skipUnless(Image, "Pillow missing")
class ThumbnailWithDimensions(Base):
    def thumb_page(self, thumb, dims=(16, 12), extra=()):
        tags = [(259, 3, [6]), (296, 3, [2])] + list(extra)
        if dims:
            tags += [(256, 4, [dims[0]]), (257, 4, [dims[1]])]
        return {"tags": tags, "jpeg": thumb}

    def check(self, dims, little=True, **kw):
        thumb = jpeg()
        page = rgb_page(width=32, height=24, rps=8)
        d = build_tiff([page, self.thumb_page(thumb, dims)], little=little)
        self.assertKept(thumb, d)
        out, ctx = self.clean_file(d, full=True, **kw)
        self.assertGone(b"THUMBMARK", out)
        self.assertGone(thumb[:64], out)
        for s in page["strips"]:
            self.assertKept(s, out)
        self.assertEqual(ctx.residue, [])
        return out

    def test_thumbnail_with_dimensions_is_removed(self):
        self.check((16, 12))

    def test_thumbnail_with_dimensions_big_endian(self):
        self.check((16, 12), little=False)

    def test_thumbnail_without_dimensions_is_removed(self):
        self.check(None)

    def test_thumbnail_with_dimensions_in_the_fallback_path(self):
        thumb = jpeg()
        page = rgb_page(width=32, height=24, rps=8, extra=[(519, 4, [8])])  # JPEG tables: sweep impossible, eraser is used
        out, ctx = self.clean_file(build_tiff([page, self.thumb_page(thumb)]), full=True)
        self.assertGone(b"THUMBMARK", out)

    def test_thumbnail_followed_by_another_page(self):
        thumb = jpeg()
        p0, p2 = rgb_page(width=32, height=24, rps=8), rgb_page(width=10, height=4)
        out, ctx = self.clean_file(build_tiff([p0, self.thumb_page(thumb), p2]), full=True)
        self.assertGone(b"THUMBMARK", out)
        for s in p0["strips"] + p2["strips"]:
            self.assertKept(s, out)
        self.assertEqual(ctx.residue, [])

    def test_ifd0_old_style_jpeg_is_kept(self):
        data = jpeg(mark=b"")
        page = self.thumb_page(data, dims=None, extra=[(256, 4, [16]), (257, 4, [12])])
        out, _ = self.clean_file(build_tiff([page]))
        self.assertKept(data, out)
        page = {"tags": [(259, 3, [6])], "jpeg": data}
        out, _ = self.clean_file(build_tiff([page]))
        self.assertKept(data, out)

    def test_ifd0_old_style_jpeg_with_its_thumbnail(self):
        data, thumb = jpeg(mark=b"PAGEJPEG"), jpeg(mark=b"THUMBMARK")
        page0 = self.thumb_page(data, dims=(40, 30))
        out, _ = self.clean_file(build_tiff([page0, self.thumb_page(thumb, (16, 12))]))
        self.assertKept(b"PAGEJPEG", out)
        self.assertGone(b"THUMBMARK", out)

    def test_second_ifd_not_smaller_than_first_is_a_page(self):
        page = rgb_page()  # 8x6
        other = jpeg(mark=b"SECONDPAGE")
        for dims in ((8, 6), (40, 30)):
            out, ctx = self.clean_file(build_tiff([page, self.thumb_page(other, dims)]))
            self.assertKept(b"SECONDPAGE", out)

    def test_third_ifd_with_dimensions_is_a_page(self):
        other = jpeg(mark=b"THIRDPAGE")
        out, _ = self.clean_file(build_tiff([rgb_page(), rgb_page(), self.thumb_page(other)]))
        self.assertKept(b"THIRDPAGE", out)

    def test_ifd_with_strips_is_never_a_thumbnail(self):
        pages = [rgb_page(width=8 + i, height=6) for i in range(3)]
        out, ctx = self.clean_file(build_tiff(pages))
        for p in pages:
            for s in p["strips"]:
                self.assertKept(s, out)

    @unittest.skipUnless(HAVE["exiv2"], "exiv2 missing")
    def test_exiv2_thumbnail_with_dimensions(self):
        p = self.dir / "e.tif"
        Image.new("RGB", (64, 48), "red").save(p, compression="tiff_lzw")
        Image.new("RGB", (16, 12), "black").save(self.dir / "e-thumb.jpg")
        run = lambda *a: subprocess.run(["exiv2", *a, str(p)], capture_output=True, text=True)
        run("-f", "-it")
        run("-M", "set Exif.Thumbnail.ImageWidth Long 16", "-M", "set Exif.Thumbnail.ImageLength Long 12")
        before = run("-pa").stdout
        self.assertIn("Exif.Thumbnail.ImageWidth", before)
        self.assertIn("Exif.Thumbnail.JPEGInterchangeFormat ", before)
        thumb = (self.dir / "e-thumb.jpg").read_bytes()
        self.assertIn(thumb, p.read_bytes())
        pixels = Image.open(p).tobytes()
        out, ctx = self.clean_file(p.read_bytes(), name="e2.tif", full=True)
        self.assertEqual(ctx.residue, [])
        self.assertGone(thumb, out)
        self.assertGone(b"\xff\xd8\xff", out)
        q = self.dir / "out-e2.tif"
        shown = subprocess.run(["exiv2", "-pa", str(q)], capture_output=True, text=True).stdout
        self.assertNotIn("Thumbnail", shown)
        self.assertEqual(Image.open(q).tobytes(), pixels)
        self.assertEqual(Image.open(q).n_frames, 1)

    @unittest.skipUnless(HAVE["tiffcp"] and HAVE["tiffinfo"], "libtiff tools missing")
    def test_real_multipage_tiff_is_untouched(self):
        srcs = []
        for i, size in enumerate(((64, 48), (32, 24), (16, 12))):
            f = self.dir / f"s{i}.tif"
            Image.effect_noise(size, 60 + i).convert("RGB").save(f, compression="tiff_lzw")
            srcs.append(str(f))
        multi = self.dir / "m.tif"
        subprocess.run(["tiffcp", *srcs, str(multi)], check=True, capture_output=True)
        before = Image.open(multi)
        frames = []
        for n in range(before.n_frames):
            before.seek(n)
            frames.append(before.tobytes())
        self.assertEqual(len(frames), 3)
        out, ctx = self.clean_file(multi.read_bytes(), name="m2.tif", full=True)
        self.assertEqual(ctx.residue, [])
        after = Image.open(self.dir / "out-m2.tif")
        self.assertEqual(after.n_frames, 3)
        for n in range(3):
            after.seek(n)
            self.assertEqual(after.tobytes(), frames[n])
        info = subprocess.run(["tiffinfo", str(self.dir / "out-m2.tif")], capture_output=True, text=True)
        self.assertEqual(info.stdout.count("=== TIFF directory"), 3, info.stdout)


class SharedTables(Base):
    @staticmethod
    def shared(m_ifds, n_strips, compression=8, vary="width"):
        """m_ifds IFDs sharing ONE n_strips-entry StripOffsets/StripByteCounts array, differing in one tag."""
        n = 7
        ifd_len = 2 + 12 * n + 4
        a_off = 8 + m_ifds * ifd_len
        a_cnt = a_off + 4 * n_strips
        data = a_cnt + 4 * n_strips
        buf = bytearray(data + n_strips)
        buf[:8] = b"II*\x00" + struct.pack("<I", 8)
        buf[a_off:a_cnt] = b"".join(struct.pack("<I", data + i) for i in range(n_strips))
        buf[a_cnt:data] = struct.pack("<I", 1) * n_strips
        for i in range(m_ifds):
            at = 8 + i * ifd_len
            width = 1 + (i if vary == "width" else 0)
            ent = [(256, 4, 1, width), (257, 4, 1, n_strips), (258, 3, 1, 8), (259, 3, 1, compression),
                   (262, 3, 1, 1), (273, 4, n_strips, a_off), (279, 4, n_strips, a_cnt)]
            buf[at:at + 2] = struct.pack("<H", n)
            for j, (t, typ, c, v) in enumerate(ent):
                buf[at + 2 + 12 * j:at + 14 + 12 * j] = struct.pack("<HHII", t, typ, c, v)
            nxt = at + ifd_len if i + 1 < m_ifds else 0
            buf[at + 2 + 12 * n:at + 6 + 12 * n] = struct.pack("<I", nxt)
        return bytes(buf)

    def run_bounded(self, data, peak_mb=80, seconds=20):
        tracemalloc.start()
        t = time.time()
        try:
            try:
                out = images.clean_tiff(data)
            except WipeError:
                out = None  # refusing a hostile file is fine
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, peak_mb << 20)
        self.assertLess(time.time() - t, seconds)
        return out

    def test_shared_table_different_widths_compressed(self):
        data = self.shared(200, 20000)  # old code: 4 million ranges, 556 MB
        out = self.run_bounded(data)
        if out is not None:
            self.assertEqual(len(out), len(data))

    def test_shared_table_different_widths_uncompressed(self):
        self.run_bounded(self.shared(200, 20000, compression=1))

    def test_shared_table_large(self):
        self.run_bounded(self.shared(1000, 60000), peak_mb=150)  # old code: tens of GB

    def test_shared_table_with_other_geometry_is_still_cleaned(self):
        data = self.shared(3, 50)
        out = images.clean_tiff(data)
        self.assertEqual(len(out), len(data))

    def test_pair_product_of_tables_is_bounded(self):
        """K offset arrays x K count arrays, one IFD per pair: ranges must not grow K*K*N."""
        k, n_strips = 60, 2000
        n_ifd = k * k
        ifd_len = 2 + 12 * 4 + 4
        base = 8 + n_ifd * ifd_len
        arrays = base + 2 * k * 4 * n_strips
        buf = bytearray(arrays + n_strips)
        buf[:8] = b"II*\x00" + struct.pack("<I", 8)
        for a in range(2 * k):
            buf[base + a * 4 * n_strips:base + (a + 1) * 4 * n_strips] = b"".join(
                struct.pack("<I", arrays + i if a < k else 1) for i in range(n_strips))
        for i in range(n_ifd):
            at = 8 + i * ifd_len
            oa, ca = base + (i // k) * 4 * n_strips, base + (k + i % k) * 4 * n_strips
            ent = [(256, 4, 1, 1), (257, 4, 1, n_strips), (273, 4, n_strips, oa), (279, 4, n_strips, ca)]
            buf[at:at + 2] = struct.pack("<H", 4)
            for j, (t, typ, c, v) in enumerate(ent):
                buf[at + 2 + 12 * j:at + 14 + 12 * j] = struct.pack("<HHII", t, typ, c, v)
            buf[at + 50:at + 54] = struct.pack("<I", at + ifd_len if i + 1 < n_ifd else 0)
        self.run_bounded(bytes(buf), peak_mb=250)


class IfdLimit(Base):
    @staticmethod
    def chain(n):
        buf = bytearray(8 + n * 6)
        buf[:8] = b"II*\x00" + struct.pack("<I", 8)
        for i in range(n):
            at = 8 + 6 * i
            struct.pack_into("<HI", buf, at, 0, at + 6 if i + 1 < n else 0)
        return bytes(buf)

    def test_twelve_thousand_pages_are_cleaned(self):
        pages = [rgb_page(width=8, height=4) for _ in range(12000)]
        d = build_tiff(pages)
        t = time.time()
        out = images.clean_tiff(d)
        self.assertLess(time.time() - t, 20)
        self.assertEqual(len(out), len(d))
        for p in (pages[0], pages[-1]):
            for s in p["strips"]:
                self.assertKept(s, out)

    def test_twelve_thousand_ifds_chain(self):
        images.clean_tiff(self.chain(12000))

    def test_absurd_ifd_count_is_still_refused(self):
        with self.assertRaises(WipeError):
            images.clean_tiff(self.chain(images._TIFF_MAX_IFDS + 5))

    def test_overlapping_ifd_bomb_is_rejected_quickly(self):
        k, count = 100, 65535
        tail = 8 + 2 + 12 * count
        buf = bytearray(tail + 4 * k + 16)
        buf[:8] = b"II*\x00" + struct.pack("<I", 8)
        for i in range(k):
            buf[8 + 4 * i:10 + 4 * i] = b"\xff\xff"
            struct.pack_into("<I", buf, tail + 4 * i, 8 + 4 * (i + 1) if i + 1 < k else 0)
        t = time.time()
        with self.assertRaises(WipeError):
            images.clean_tiff(bytes(buf))
        self.assertLess(time.time() - t, 10)


if __name__ == "__main__":
    unittest.main()
