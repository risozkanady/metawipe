import asyncio
import tempfile
import unittest
from pathlib import Path

try:
    from metawipe.tui import MetawipeApp
    from textual.widgets import DataTable, Input, RichLog, Select, Switch
except ImportError:  # pragma: no cover
    MetawipeApp = None

from tests.test_regressions import png_bytes


async def wait_for_tree(app, pilot):
    tree = app.query_one("#tree")
    for _ in range(60):
        if tree.root.children:
            break
        await pilot.pause(0.1)
    tree.root.expand()
    await pilot.pause(0.2)
from tests.test_metawipe import SECRET


@unittest.skipIf(MetawipeApp is None, "textual missing")
class TuiTests(unittest.TestCase):
    def test_mark_preview_run_into_output_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, out = Path(tmp) / "in", Path(tmp) / "out"
            src.mkdir()
            (src / "a.png").write_bytes(png_bytes([(b"tEXt", b"Author\x00" + SECRET)]))
            (src / "notes.txt").write_text("hi")

            async def scenario():
                app = MetawipeApp(src)
                async with app.run_test(size=(140, 40)) as pilot:
                    await wait_for_tree(app, pilot)
                    await pilot.press("down")  # a.png
                    await pilot.pause(0.8)
                    preview = app.query_one("#preview", DataTable)
                    shown = [str(preview.get_row_at(i)[0]) for i in range(preview.row_count)]
                    self.assertTrue(any("PNG text" in s for s in shown), shown)
                    await pilot.press("space")
                    self.assertEqual({p.name for p in app.marks}, {"a.png"})
                    app.query_one("#mode", Select).value = "folder"
                    app.query_one("#outdir", Input).value = str(out)
                    await pilot.press("r")
                    await pilot.pause(2)
                    text = "\n".join(line.text for line in app.query_one("#log", RichLog).lines)
                    self.assertIn("done: 1 cleaned, 0 failed", text)

            asyncio.run(scenario())
            self.assertNotIn(SECRET, (out / "a.png").read_bytes())
            self.assertIn(SECRET, (src / "a.png").read_bytes())

    def test_help_screen_opens_and_closes(self):
        with tempfile.TemporaryDirectory() as tmp:
            async def scenario():
                app = MetawipeApp(Path(tmp))
                async with app.run_test(size=(140, 45)) as pilot:
                    await pilot.pause(0.3)
                    await pilot.press("f1")
                    await pilot.pause(0.3)
                    self.assertEqual(type(app.screen).__name__, "HelpScreen")
                    await pilot.press("escape")
                    await pilot.pause(0.3)
                    self.assertEqual(type(app.screen).__name__, "Screen")

            asyncio.run(scenario())

    def test_nothing_marked_does_not_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            async def scenario():
                app = MetawipeApp(Path(tmp))
                async with app.run_test(size=(120, 30)) as pilot:
                    await pilot.pause(0.3)
                    await pilot.press("r")
                    await pilot.pause(0.3)
                    text = "\n".join(line.text for line in app.query_one("#log", RichLog).lines)
                    self.assertNotIn("cleaning", text)

            asyncio.run(scenario())

    def test_in_place_needs_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.png"
            p.write_bytes(png_bytes([(b"tEXt", b"k\x00" + SECRET)]))

            async def scenario():
                app = MetawipeApp(Path(tmp))
                async with app.run_test(size=(140, 40)) as pilot:
                    await wait_for_tree(app, pilot)
                    await pilot.press("down", "space")
                    app.query_one("#mode", Select).value = "inplace"
                    await pilot.press("r")
                    await pilot.pause(0.3)
                    await pilot.press("n")  # decline
                    await pilot.pause(0.5)

            asyncio.run(scenario())
            self.assertIn(SECRET, p.read_bytes())


@unittest.skipIf(MetawipeApp is None, "textual missing")
class TuiDesignTests(unittest.TestCase):
    def test_severity_ranks_where_and_who_first(self):
        from metawipe.tui import severity
        for key in ("GPS", "Artist", "Annotation authors", "Printer name", "Digital signature"):
            self.assertEqual(severity(key), 0, key)
        for key in ("Make", "Model", "Software", "XMP", "Thumbnail", "File ID"):
            self.assertEqual(severity(key), 1, key)
        for key in ("DateTimeOriginal", "Modification time"):
            self.assertEqual(severity(key), 2, key)

    def test_run_button_says_what_will_happen_and_outcome_shows_in_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp).resolve() / "a.png"  # the tree shows resolved paths (macOS: /var -> /private/var)
            p.write_bytes(png_bytes([(b"tEXt", b"k\x00" + SECRET)]))
            (Path(tmp) / "notes.txt").write_text("hi")

            async def scenario():
                from textual.widgets import Button
                app = MetawipeApp(Path(tmp))
                async with app.run_test(size=(140, 40)) as pilot:
                    await wait_for_tree(app, pilot)
                    button = app.query_one("#run", Button)
                    self.assertEqual(str(button.label), "Clean (r)")
                    await pilot.press("down")  # a.png
                    await pilot.press("space")
                    self.assertEqual(str(button.label), "Clean 1 marked (r)")
                    app.query_one("#mode", Select).value = "inplace"
                    await pilot.pause(0.2)
                    self.assertEqual(str(button.label), "Overwrite 1 marked (r)")
                    self.assertEqual(str(button.variant), "error")
                    self.assertTrue(app.query_one("#folder-row").display is False)
                    app.query_one("#mode", Select).value = "folder"
                    await pilot.pause(0.2)
                    self.assertTrue(app.query_one("#folder-row").display)
                    app.query_one("#mode", Select).value = "copy"
                    await pilot.press("r")
                    await pilot.pause(2)
                    self.assertEqual(app.states.get(p), "done")
                    self.assertFalse(app.marks)  # the outcome shows in the tree instead
                    self.assertEqual(str(button.label), "Clean (r)")
                    # files metawipe cannot handle cannot be marked
                    await pilot.press("down", "down", "down")
                    app.marks.clear()
                    node = app.query_one("#tree").cursor_node
                    if node is not None and node.data is not None and node.data.path.suffix == ".txt":
                        await pilot.press("space")
                        self.assertFalse(app.marks)

            asyncio.run(scenario())

    def test_preview_marks_clean_and_unsupported_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "notes.txt").write_text("hi")

            async def scenario():
                app = MetawipeApp(Path(tmp))
                async with app.run_test(size=(120, 30)) as pilot:
                    await wait_for_tree(app, pilot)
                    await pilot.press("down")
                    await pilot.pause(0.8)
                    table = app.query_one("#preview", DataTable)
                    self.assertIn("Not supported", str(table.get_row_at(0)[0]))
                    self.assertEqual(table.border_subtitle, "")

            asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
