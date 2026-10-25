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


if __name__ == "__main__":
    unittest.main()
