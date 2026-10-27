import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from metawipe.tui import MetawipeApp, config_path
except ImportError:  # pragma: no cover
    MetawipeApp = None

from tests.test_regressions import png_bytes
from tests.test_metawipe import SECRET
from tests.test_tui import wait_for_tree


def tree_names(app):
    names, todo = [], [app.query_one("#tree").root]
    while todo:
        node = todo.pop()
        if node.data is not None:
            names.append(node.data.path.name)
        todo.extend(node.children)
    return names


@unittest.skipIf(MetawipeApp is None, "textual missing")
class ThemeConfigTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.tmp / "cfg")})
        env.start()
        self.addCleanup(env.stop)

    def start_theme(self, change_to=None):
        async def scenario():
            app = MetawipeApp(self.tmp)
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(0.2)
                seen = app.theme
                if change_to:
                    app.theme = change_to
                    await pilot.pause(0.3)
                return seen
        return asyncio.run(scenario())

    def test_theme_is_remembered_across_starts(self):
        self.assertEqual(self.start_theme(), "metawipe")
        self.assertFalse(config_path().exists())  # nothing changed, nothing written
        self.start_theme(change_to="nord")
        self.assertEqual(json.loads(config_path().read_text())["theme"], "nord")
        self.assertEqual(self.start_theme(), "nord")

    def test_other_keys_survive_a_theme_save(self):
        config_path().parent.mkdir(parents=True)
        config_path().write_text(json.dumps({"theme": "metawipe", "later": 1}))
        self.start_theme(change_to="gruvbox")
        self.assertEqual(json.loads(config_path().read_text()), {"theme": "gruvbox", "later": 1})

    def test_broken_or_unknown_config_falls_back_to_default(self):
        config_path().parent.mkdir(parents=True)
        for content in ("{not json", "[1, 2]", json.dumps({"theme": "no-such-theme"}), json.dumps({"theme": 5}), json.dumps({"theme": ["x"]})):
            config_path().write_text(content)
            self.assertEqual(self.start_theme(), "metawipe", content)

    def test_unwritable_config_does_not_crash(self):
        (self.tmp / "cfg").write_text("a file where the folder should be")
        self.start_theme(change_to="nord")  # warns, keeps running


@unittest.skipIf(MetawipeApp is None, "textual missing")
class TreeReloadTests(unittest.TestCase):
    def test_tree_shows_new_files_after_run_and_on_f5(self):
        self.reload_scenario(via_symlink=False)

    @unittest.skipIf(os.name == "nt", "symlinks need extra rights on Windows")
    def test_started_through_a_symlink(self):
        """macOS: /tmp and /var are symlinks; the tree resolves folders, so the start folder must be resolved too."""
        self.reload_scenario(via_symlink=True)

    def reload_scenario(self, via_symlink):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(Path(tmp) / "cfg")}):
            src = Path(tmp) / "in"
            (src / "sub").mkdir(parents=True)
            (src / "a.png").write_bytes(png_bytes([(b"tEXt", b"Author\x00" + SECRET)]))
            start = src
            if via_symlink:
                start = Path(tmp) / "link"
                start.symlink_to(src)

            async def scenario():
                app = MetawipeApp(start)
                async with app.run_test(size=(140, 40)) as pilot:
                    await wait_for_tree(app, pilot)
                    await pilot.press("down", "down")  # sub, a.png
                    await pilot.pause(0.3)
                    await pilot.press("space", "r")
                    for _ in range(40):
                        await pilot.pause(0.1)
                        if "a.clean.png" in tree_names(app):
                            break
                    self.assertIn("a.clean.png", tree_names(app))
                    self.assertEqual(app._current_path().name, "a.png")  # cursor stays where it was
                    self.assertEqual(app.states.get(app._current_path()), "done")  # ✓ in the tree
                    (src / "made-outside").mkdir()
                    await pilot.press("f5")
                    await pilot.pause(0.5)
                    self.assertIn("made-outside", tree_names(app))

            asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
