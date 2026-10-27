"""Regression tests for what the round-5 reviewers still found open: HEIF items hidden outside iinf, mime items with
an image/* content type, overlapping iloc extents, infe v3 with a v2 layout, a short infe; a JPEG cut inside its
Exif segment; unbounded peek work (marker floods, big packages, self-referencing IFDs); dump's mmap; bidi controls,
BOM in UserComment, escapes in --show errors; folder preview, in-place count and view toggle in the TUI; F13 gaps."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from metawipe import cli, dump, images, isobmff, pdf, peek
from metawipe.common import WipeError
from tests.test_fix_pdf import build, document, pypdf, stream
from tests.test_fix_r5_dump import GPS_JPEG, exif_gps, seg
from tests.test_fix_r5_heif import EXIF, HDLR, XMP, box, heif, iinf, iloc, infe
from tests.test_metawipe import run
from tests.test_regressions import png_bytes

try:
    from metawipe.tui import MetawipeApp
    from textual.widgets import DataTable, Select
    from tests.test_fix_r5_ui import until
    from tests.test_tui import wait_for_tree
except ImportError:  # pragma: no cover
    MetawipeApp = None

ROOT = Path(__file__).resolve().parent.parent


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="mw-fix-r5-repair-"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def peek(self, name, data):
        p = self.dir / name
        p.write_bytes(data)
        return peek.inspect_file(p)


class HeifHiddenItems(Base):
    """F1: exiv2 reads an infe (and iinf/iloc) in meta, iprp, ipco or moov; the cleaner only inside iinf."""

    def test_infe_outside_iinf_is_refused(self):
        exif_infe = infe(2, b"Exif")
        cases = {
            "in meta": lambda m: HDLR + iinf(infe(1, b"hvc1")) + exif_infe + iloc([(2, 0, 0, m, len(EXIF))]),
            "in ipco": lambda m: HDLR + iinf(infe(1, b"hvc1"), infe(2, b"mski")) + box(b"iprp", box(b"ipco", exif_infe))
            + iloc([(2, 0, 0, m, len(EXIF))]),
        }
        for name, children in cases.items():
            with self.subTest(name):
                d = heif(children, EXIF)
                with self.assertRaises(WipeError):
                    isobmff.clean_heif(d)
                self.assertIn(("HEIF metadata", "could not be parsed"), self.peek("a.heic", d))

    def test_items_in_their_place_still_clean(self):
        d = heif(lambda m: HDLR + iinf(infe(1, b"hvc1"), infe(2, b"Exif")) + box(b"iprp", box(b"ipco", box(b"ispe", bytes(12))))
                 + iloc([(2, 0, 0, m, len(EXIF))]), EXIF)
        self.assertNotIn(b"SECRET", isobmff.clean_heif(d))


class HeifMimeContentType(Base):
    """F2: exiv2 shows any mime item as XMP, so an image/* content type must not exempt it."""

    def test_image_content_types_are_zeroed_and_previewed(self):
        for ctype in (b"image/jpeg", b"image/xmp+xml", b"IMAGE/rdf+xmlx"):
            with self.subTest(ctype):
                d = heif(lambda m: HDLR + iinf(infe(1, b"hvc1"), infe(2, b"mime", b"\x00" + ctype + b"\x00"))
                         + iloc([(2, 0, 0, m, len(XMP))]), XMP)
                self.assertIn(("XMP", f"{len(XMP)} bytes"), self.peek("a.heic", d))
                out = isobmff.clean_heif(d)
                self.assertNotIn(b"SECRET", out)
                self.assertTrue(out.endswith(bytes(len(XMP))))


class HeifMalformed(Base):
    def test_overlapping_extents_are_refused(self):
        """F6: extents that each cover the whole file were blanked again and again (17 s for 1 MB)."""
        def children(m):
            body = bytes([1, 0, 0, 0, 0x04, 0x00]) + struct.pack(">H", 1) + struct.pack(">HHHH", 1, 0, 0, 400)
            return HDLR + iinf(infe(1, b"Exif")) + box(b"iloc", body + struct.pack(">I", 2000) * 400)
        d = heif(children)
        d += bytes(2000 - len(d))
        with self.assertRaises(WipeError):
            isobmff.clean_heif(d)

    def test_infe_v3_with_a_v2_layout_is_refused(self):
        """F17a: exiv2 ignores the version and still finds the Exif item the cleaner misreads."""
        v3 = box(b"infe", bytes([3, 0, 0, 0]) + struct.pack(">HH", 1, 0) + b"Exif\x00name\x00\x00\x00\x00")  # long enough that the truncation check cannot fire instead
        d = heif(lambda m: HDLR + box(b"iinf", bytes(4) + struct.pack(">H", 1) + v3) + iloc([(1, 0, 0, m, len(EXIF))]), EXIF)
        with self.assertRaises(WipeError):
            isobmff.clean_heif(d)

    def test_short_infe_raises_wipeerror(self):
        """F17b: the item type was read from the next box, which then got 'free' written into its header."""
        short = box(b"infe", bytes([2, 0, 0, 0]) + struct.pack(">H", 7))
        trap = struct.pack(">I", 0x4578) + b"if\x00\x00" + bytes(0x4578 - 8)  # bytes 2-5 of its header read 'Exif'
        d = heif(lambda m: HDLR + box(b"iinf", bytes(4) + struct.pack(">H", 2) + short + trap) + iloc([(7, 0, 0, m, len(EXIF))]), EXIF)
        with self.assertRaises(WipeError):
            isobmff.clean_heif(d)


class JpegCutInsideExif(Base):
    """Round-5 regression: the walker raised before the cut segment, so peek and --show lost its GPS."""

    def cut(self):
        k = GPS_JPEG.index(b"Exif") + 6 + len(exif_gps()) - 4  # inside the last rational
        return GPS_JPEG[:k]

    def test_peek_and_dump_still_show_the_gps(self):
        self.assertTrue(any(k == "GPS" for k, _ in self.peek("cut.jpg", self.cut())))
        rows = dump.dump_file(self.dir / "cut.jpg")
        self.assertTrue(any(name == "GPSLatitude" for _, name, _ in rows), rows)
        self.assertIn(("JPEG", "could not read", "truncated JPEG segment"), rows)

    def test_cleaner_still_refuses_it(self):
        with self.assertRaisesRegex(WipeError, "truncated JPEG segment"):
            images.clean_jpeg(self.cut())

    def test_picture_left_in_a_package_still_flagged(self):
        p = self.dir / "a.docx"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("word/document.xml", "<w:document/>")
            z.writestr("word/media/image9.jpg", self.cut())
        self.assertIn(("Embedded picture image9.jpg", "Artist, GPS"), peek.inspect_file(p))


class PeekBounds(Base):
    """Round-5 regressions and F11e: peek walked every marker of a flood and read every package part in full."""

    def test_marker_flood_is_cut_and_said(self):
        for name, filler in (("rst", b"\xff\xd0" * 50000), ("com", seg(0xFE, b"x") * 50000)):
            with self.subTest(name):
                rows = self.peek("f.jpg", b"\xff\xd8" + filler + b"\xff\xd9")
                self.assertLessEqual(len(rows), peek._SEGMENT_CAP + 1)
                self.assertEqual(rows[-1][0], "Not fully inspected")

    def test_package_has_one_budget_for_all_parts(self):
        p = self.dir / "c.xlsx"
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
            for n in range(8):
                z.writestr(f"xl/comments{n}.xml", "<author>Jozko</author>" + " " * 300000)
        reads = []
        real = zipfile.ZipExtFile.read

        def counting(self, n=-1):
            data = real(self, n)
            reads.append(len(data))
            return data
        with mock.patch.object(peek, "_READ_CAP", 1 << 20), mock.patch.object(zipfile.ZipExtFile, "read", counting):
            rows = peek.inspect_file(p)
        self.assertLessEqual(sum(reads), 1 << 20)
        self.assertIn("Not fully inspected", [k for k, _ in rows])
        self.assertIn(("Comment/revision authors", "Jozko"), rows)

    def test_self_referencing_exif_ifd_is_walked_once(self):
        """IFD0 whose ExifIFD pointers lead back to itself was walked 512^3 times (a 6 KB file hung the preview)."""
        n = 10
        artist = struct.pack(">HHI", 0x013B, 2, 4) + b"Joe\x00"
        tiff = b"MM\x00\x2a\x00\x00\x00\x08" + struct.pack(">H", n + 1) + artist + struct.pack(">HHII", 0x8769, 4, 1, 8) * n + bytes(4)
        self.assertEqual(peek._tiff_fields(tiff), [("Artist", "Joe")])


class DumpAndDisplay(Base):
    @unittest.skipUnless(os.name == "posix", "SIGBUS is POSIX")
    def test_jpeg_cut_by_another_process_during_dump_does_not_kill_it(self):
        """Round-5 regression: dump mmapped the JPEG, and touching pages past the new end raised SIGBUS."""
        p = self.dir / "a.jpg"
        p.write_bytes(GPS_JPEG[:2] + seg(0xFE, bytes(20000)) + GPS_JPEG[2:] + seg(0xFE, bytes(20000)) + b"\xff\xd9")
        code = ("import os, sys; from pathlib import Path; from metawipe import dump\n"
                "real = dump._exif_block\n"
                "def cut(*a):\n    os.truncate(sys.argv[1], 0)\n    return real(*a)\n"
                "dump._exif_block = cut\nrows = dump.dump_file(Path(sys.argv[1]))\n"
                "print('GPS' if any(r[1] == 'GPSLatitude' for r in rows) else 'none')")
        proc = subprocess.run([sys.executable, "-c", code, str(p)], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "GPS")

    def test_bidi_controls_are_stripped(self):
        """F10/F17: U+202E in a JPEG comment flipped how the preview line displays."""
        rows = self.peek("a.jpg", b"\xff\xd8" + seg(0xFE, "x‮gnp.exe⁦".encode()) + b"\xff\xd9")
        self.assertEqual(rows, [("Comment", "x gnp.exe ")])

    def test_unicode_user_comment_honours_a_bom(self):
        """F15: exiv2 reads a BOM; it was decoded in the block's byte order instead."""
        body = b"\xff\xfe" + "John".encode("utf-16-le")
        self.assertEqual(dump._user_comment(b"UNICODE\x00" + body, ">"), "(UNICODE) John")
        self.assertEqual(dump._user_comment(b"UNICODE\x00" + "John".encode("utf-16-be"), ">"), "(UNICODE) John")

    def test_show_error_line_has_no_terminal_escapes(self):
        """F10: the 'no such file' line printed the raw file name."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["--show", str(self.dir / "gone\x1b[2J.png")]), 1)
        self.assertIn("no such file", err.getvalue())
        self.assertNotIn("\x1b", err.getvalue())


class SafetyGaps(Base):
    """F13: mutants that survived the suite."""

    def test_output_equal_to_an_input_given_by_a_non_canonical_path(self):
        (self.dir / "sub").mkdir()
        a, user = self.dir / "a.png", self.dir / "a.clean.png"
        a.write_bytes(png_bytes([(b"tEXt", b"k\x00x")]))
        user.write_bytes(b"precious")
        rc, _, err = run("-f", self.dir / "sub" / ".." / "a.png", user)
        self.assertEqual(rc, 1, err)
        self.assertIn("is also an input", err)
        self.assertEqual(user.read_bytes(), b"precious")

    @unittest.skipIf(pypdf is None, "pypdf missing")
    def test_pdf_verify_checks_the_catalog_and_the_objects_on_their_own(self):
        xml = b"<x:xmpmeta>secret</x:xmpmeta>"
        cases = {
            "catalog only": document(catalog=b"/Metadata 5 0 R", extra={5: stream(b"/Subtype /XML", xml)}),
            "object only": document(extra={5: stream(b"/Type /Metadata /Subtype /XML", xml)}),
        }
        for name, objs in cases.items():
            with self.subTest(name):
                p = self.dir / "a.pdf"
                p.write_bytes(build(objs))
                with self.assertRaisesRegex(WipeError, "verification failed"):
                    pdf._verify(p)


@unittest.skipIf(MetawipeApp is None, "textual missing")
class Tui(Base):
    def run_app(self, scenario):
        async def main():
            app = MetawipeApp(self.dir)
            async with app.run_test(size=(140, 40)) as pilot:
                await wait_for_tree(app, pilot)
                await scenario(app, pilot)
        asyncio.run(main())

    def test_folder_highlight_does_not_walk_the_tree(self):
        """Round-5 regression: every folder highlight walked its whole subtree; the walks piled up on big trees."""
        (self.dir / "photos" / "deep").mkdir(parents=True)
        (self.dir / "photos" / "a.png").write_bytes(png_bytes())
        (self.dir / "photos" / "deep" / "b.png").write_bytes(png_bytes())

        async def scenario(app, pilot):
            table = app.query_one("#preview", DataTable)
            await pilot.press("down")
            self.assertTrue(await until(pilot, lambda: table.row_count and "Folder" in str(table.get_row_at(0)[0])))
            self.assertEqual(str(table.get_row_at(0)[1]), "1 supported file(s) directly inside")
        with mock.patch.object(cli, "_collect", side_effect=AssertionError("walked")) as walk:
            self.run_app(scenario)
        walk.assert_not_called()

    def test_in_place_count_is_planned_off_the_ui_thread(self):
        """Round-5 regression: counting a home-size tree froze the UI for seconds before the dialog."""
        (self.dir / "a.png").write_bytes(png_bytes([(b"tEXt", b"k\x00x")]))
        threads, real = [], cli.plan

        def plan(args, report):
            threads.append(threading.current_thread() is threading.main_thread())
            return real(args, report)

        async def scenario(app, pilot):
            app.marks.add(self.dir / "a.png")
            app.query_one("#mode", Select).value = "inplace"
            await pilot.press("r")
            self.assertTrue(await until(pilot, lambda: hasattr(app.screen, "text")))
            self.assertIn("Overwrite 1 file(s)", app.screen.text)
            await pilot.press("n")
        with mock.patch.object(cli, "plan", plan):
            self.run_app(scenario)
        self.assertEqual(threads, [False])

    def test_slow_everything_view_does_not_replace_the_summary(self):
        """F12: v, v during a slow dump showed 'everything' while the app was back in summary mode."""
        (self.dir / "a.png").write_bytes(png_bytes([(b"tEXt", b"k\x00x")]))
        gate, entered, real = threading.Event(), threading.Event(), dump.dump_file

        def slow(path):
            entered.set()
            gate.wait(10)
            return real(path)

        async def scenario(app, pilot):
            table = app.query_one("#preview", DataTable)
            try:
                await pilot.press("down")
                self.assertTrue(await until(pilot, lambda: str(table.border_title) == "a.png"))
                await pilot.press("v")
                await until(pilot, entered.is_set)
                await pilot.press("v")
                await until(pilot, lambda: "v: everything" in str(table.border_subtitle))
            finally:
                gate.set()
            await until(pilot, lambda: all(w.is_finished for w in app.workers if w.group == "preview"))
            await pilot.pause(0.2)
            self.assertFalse(app.view_all)
            self.assertEqual(str(table.border_title), "a.png")
        with mock.patch.object(dump, "dump_file", slow):
            self.run_app(scenario)


if __name__ == "__main__":
    unittest.main()
