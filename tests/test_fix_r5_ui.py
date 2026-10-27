"""Regression tests for the round-5 UI findings: markup injection, honest in-place count, fsync, double run,
--show header and FIFO, stale preview, marks during a run, planning failures in the tree, unknown ~user."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import os
import shutil
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from metawipe import cli, peek
from tests.test_regressions import png_bytes

try:
    from metawipe.tui import MetawipeApp
    from textual.widgets import DataTable, Input, RichLog, Select
    from tests.test_tui import wait_for_tree
except ImportError:  # pragma: no cover
    MetawipeApp = None


def jpeg_with_comment(com: bytes) -> bytes:
    return b"\xff\xd8\xff\xfe" + (len(com) + 2).to_bytes(2, "big") + com + b"\xff\xd9"


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="mw-fix-r5-ui-"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class CliTests(Base):
    @unittest.skipIf(os.name == "nt", "the fsync happens on the descriptor _finish opens (POSIX)")
    def test_output_is_fsynced_before_the_rename(self):
        src = self.dir / "a.png"
        src.write_bytes(png_bytes([(b"tEXt", b"k\x00x")]))
        args = argparse.Namespace(password=None, full=False)
        with mock.patch.object(cli.os, "fsync", wraps=os.fsync) as fsync:
            cli.execute(cli.Job(src, self.dir / "a.clean.png", False), args)
        self.assertTrue(fsync.called)

    @unittest.skipIf(os.name == "nt", "control characters are not allowed in Windows file names")
    def test_show_header_has_no_terminal_escapes(self):
        f = self.dir / "a\x1b[8m.png"
        f.write_bytes(png_bytes([(b"tEXt", b"k\x00x")]))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--show", str(f)]), 0)
        self.assertNotIn("\x1b", out.getvalue())
        self.assertIn("== ", out.getvalue())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "no FIFOs here")
    def test_show_fifo_is_not_a_regular_file(self):
        fifo = self.dir / "p.png"
        os.mkfifo(fifo)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["--show", str(fifo)]), 1)
        self.assertIn("not a regular file", err.getvalue())


async def until(pilot, cond, timeout=10.0):
    for _ in range(int(timeout / 0.05)):
        if cond():
            return True
        await pilot.pause(0.05)
    return cond()


def log_text(app) -> str:
    return "\n".join(line.text for line in app.query_one("#log", RichLog).lines)


def run_over(app) -> bool:
    return "done:" in log_text(app) and all(w.is_finished for w in app.workers if w.group == "run")


@unittest.skipIf(MetawipeApp is None, "textual missing")
class TuiTests(Base):
    def run_app(self, start, scenario):
        async def main():
            app = MetawipeApp(start)
            async with app.run_test(size=(140, 40)) as pilot:
                await wait_for_tree(app, pilot)
                await scenario(app, pilot)
                self.assertTrue(app.is_running)
        asyncio.run(main())

    def test_markup_in_a_jpeg_comment_is_shown_literally(self):
        (self.dir / "a.jpg").write_bytes(jpeg_with_comment(b"[/] by [b]Joe"))

        async def scenario(app, pilot):
            await pilot.press("down")
            table = app.query_one("#preview", DataTable)
            await until(pilot, lambda: app.highlighted is not None and table.row_count and "Comment" in str(table.get_row_at(0)[0]))
            self.assertEqual(str(table.get_row_at(0)[1]), "[/] by [b]Joe")
        self.run_app(self.dir, scenario)

    def test_markup_in_folder_names_is_logged_literally(self):
        d = self.dir / "[draft]" / "[" / "x]"  # the path reads "[draft]/[/x]"
        d.mkdir(parents=True)
        (d / "a.png").write_bytes(png_bytes([(b"tEXt", b"k\x00x")]))

        async def scenario(app, pilot):
            app.marks.add(self.dir / "[draft]")
            await pilot.press("r")
            self.assertTrue(await until(pilot, lambda: run_over(app)))
            self.assertIn("[draft]/[/x]/a.png -> [draft]/[/x]/a.clean.png".replace("/", os.sep), log_text(app).replace("\n", ""))
        self.run_app(self.dir, scenario)
        self.assertTrue((d / "a.clean.png").exists())

    @unittest.skipIf(os.name == "nt", "a backslash in a name is a separator on Windows")
    def test_backslash_before_bracket_is_logged_literally(self):
        """Windows paths put a backslash before every folder name: rich's escape() lost it before a "[" that does not
        start a tag (C:\\x\\[1]). Here (POSIX) a folder simply named "\\[1]" shows the same."""
        d = self.dir / "\\[1]"
        d.mkdir()
        (d / "a.png").write_bytes(png_bytes([(b"tEXt", b"k\x00x")]))

        async def scenario(app, pilot):
            app.marks.add(d)
            await pilot.press("r")
            self.assertTrue(await until(pilot, lambda: run_over(app)))
            self.assertIn("\\[1]/a.png -> \\[1]/a.clean.png", log_text(app).replace("\n", ""))
        self.run_app(self.dir, scenario)

    def test_markup_in_a_docx_member_name_does_not_kill_the_run(self):
        with zipfile.ZipFile(self.dir / "a.docx", "w") as z:
            z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
            z.writestr("word/document.xml", '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>')
            z.writestr("word/media/[/x].mp4", b"\0" * 16)

        async def scenario(app, pilot):
            app.marks.add(self.dir / "a.docx")
            await pilot.press("r")  # full scrub is the TUI default: the media member draws a warning naming it
            self.assertTrue(await until(pilot, lambda: run_over(app)))
            self.assertIn("[/x].mp4", log_text(app))
        self.run_app(self.dir, scenario)
        self.assertTrue((self.dir / "a.clean.docx").exists())

    def test_in_place_dialog_counts_hidden_files(self):
        d = self.dir / "photos"
        (d / ".thumbs" / "deep").mkdir(parents=True)
        for p in (d / ".old.png", d / ".thumbs" / "y.png", d / ".thumbs" / "deep" / "x.png"):
            p.write_bytes(png_bytes([(b"tEXt", b"k\x00secret")]))

        async def scenario(app, pilot):
            app.marks.add(d)
            app.query_one("#mode", Select).value = "inplace"
            await pilot.press("r")
            await until(pilot, lambda: hasattr(app.screen, "text"))
            self.assertIn("Overwrite 3 file(s)", app.screen.text)
            await pilot.press("n")
        self.run_app(self.dir, scenario)
        self.assertIn(b"secret", (d / ".thumbs" / "deep" / "x.png").read_bytes())

    def test_second_r_during_a_run_does_not_start_another(self):
        (self.dir / "a.png").write_bytes(png_bytes([(b"tEXt", b"k\x00x")]))
        gate, calls, real = threading.Event(), [], cli.execute

        def slow(job, args, warn=None):
            calls.append(job.src.name)
            gate.wait(10)
            return real(job, args, warn)

        async def scenario(app, pilot):
            try:
                app.marks.add(self.dir / "a.png")
                await pilot.press("r")
                await until(pilot, lambda: calls)
                await pilot.press("r")
                await pilot.pause(0.5)
            finally:
                gate.set()
            self.assertTrue(await until(pilot, lambda: run_over(app)))
            self.assertEqual(calls, ["a.png"])
        with mock.patch.object(cli, "execute", slow):
            self.run_app(self.dir, scenario)

    def test_marks_made_during_a_run_survive_it(self):
        for n in "ac":
            (self.dir / f"{n}.png").write_bytes(png_bytes([(b"tEXt", b"k\x00x")]))
        gate, calls, real = threading.Event(), [], cli.execute

        def slow(job, args, warn=None):
            calls.append(job.src.name)
            gate.wait(10)
            return real(job, args, warn)

        async def scenario(app, pilot):
            try:
                app.marks.add(self.dir / "a.png")
                await pilot.press("r")
                await until(pilot, lambda: calls)
                app.marks.add(self.dir / "c.png")
            finally:
                gate.set()
            self.assertTrue(await until(pilot, lambda: run_over(app)))
            self.assertEqual(app.marks, {self.dir / "c.png"})
        with mock.patch.object(cli, "execute", slow):
            self.run_app(self.dir, scenario)

    def test_slow_preview_does_not_replace_the_current_one(self):
        (self.dir / "a.png").write_bytes(png_bytes([(b"tEXt", b"FromA\x00x")]))
        (self.dir / "b.png").write_bytes(png_bytes([(b"tEXt", b"FromB\x00x")]))
        gate, entered, real = threading.Event(), threading.Event(), peek.inspect_file

        def slow(path):
            if path.name == "a.png":
                entered.set()
                gate.wait(10)
            return real(path)

        async def scenario(app, pilot):
            table = app.query_one("#preview", DataTable)
            try:
                await pilot.press("down")  # a.png, held back
                await until(pilot, entered.is_set)
                await pilot.press("down")  # b.png
                await until(pilot, lambda: table.row_count and "FromB" in str(table.get_row_at(0)[0]))
            finally:
                gate.set()
            await until(pilot, lambda: all(w.is_finished for w in app.workers if w.group == "preview"))
            await pilot.pause(0.2)
            self.assertEqual(app.highlighted.name, "b.png")
            self.assertEqual(str(table.border_title), "b.png")
            self.assertIn("FromB", str(table.get_row_at(0)[0]))
        with mock.patch.object(peek, "inspect_file", slow):
            self.run_app(self.dir, scenario)

    def test_file_refused_at_planning_shows_failed(self):
        a = self.dir / "a.png"
        a.write_bytes(png_bytes([(b"tEXt", b"k\x00x")]))
        (self.dir / "a.clean.png").write_bytes(b"earlier output")

        async def scenario(app, pilot):
            app.states[a] = "done"  # from an earlier run
            app.marks.add(a)
            await pilot.press("r")
            self.assertTrue(await until(pilot, lambda: run_over(app)))
            self.assertIn("already exists", log_text(app).replace("\n", " "))  # long (Windows) paths wrap the line
            self.assertEqual(app.states.get(a), "failed")
        self.run_app(self.dir, scenario)

    @unittest.skipIf(os.name == "nt", "Windows expands ~name to a folder next to the home folder, whoever exists")
    def test_unknown_user_in_output_folder_does_not_crash(self):
        (self.dir / "a.png").write_bytes(png_bytes([(b"tEXt", b"k\x00x")]))
        old = os.getcwd()
        os.chdir(self.dir)  # "~nosuchuser..." stays a relative folder name
        self.addCleanup(os.chdir, old)

        async def scenario(app, pilot):
            app.marks.add(self.dir / "a.png")
            app.query_one("#mode", Select).value = "folder"
            app.query_one("#outdir", Input).value = "~nosuchuser-mw/out"
            await pilot.press("r")
            self.assertTrue(await until(pilot, lambda: run_over(app)))
        self.run_app(self.dir, scenario)
        self.assertTrue((self.dir / "~nosuchuser-mw" / "out" / "a.png").exists())


if __name__ == "__main__":
    unittest.main()
