import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime
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
class SettingsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.tmp / "cfg")})
        env.start()
        self.addCleanup(env.stop)
        (self.tmp / "files").mkdir()
        (self.tmp / "files" / ".hidden.png").write_bytes(png_bytes([(b"tEXt", b"k\x00x")]))

    def run_app(self, scenario):
        async def main():
            app = MetawipeApp(self.tmp / "files")
            async with app.run_test(size=(140, 40)) as pilot:
                await wait_for_tree(app, pilot)
                await scenario(app, pilot)
        asyncio.run(main())

    def saved(self):
        return json.loads(config_path().read_text())

    def test_settings_window_changes_apply_and_survive_a_restart(self):
        from metawipe.tui import DateClock, SettingsScreen
        today = datetime.now().strftime("%d.%m.%Y")

        async def change(app, pilot):
            self.assertFalse(app.query_one(DateClock).display)
            self.assertNotIn(".hidden.png", tree_names(app))
            await pilot.press("s")
            self.assertIsInstance(app.screen, SettingsScreen)
            await pilot.press("down", "enter", "space")  # clock: off -> time -> date and time
            await pilot.press("down", "enter")  # hidden files: shown
            await pilot.press("down", "enter")  # preview: everything
            await pilot.pause(0.5)
            self.assertEqual({k: self.saved()[k] for k in ("clock", "show_hidden", "preview_everything")},
                             {"clock": "date", "show_hidden": True, "preview_everything": True})
            self.assertTrue(app.query_one(DateClock).display)
            self.assertIn(today, str(app.query_one(DateClock).render()))
            self.assertIn(".hidden.png", tree_names(app))
            self.assertTrue(app.view_all)
            await pilot.press("escape")
            self.assertNotIsInstance(app.screen, SettingsScreen)
        self.run_app(change)

        async def restarted(app, pilot):
            self.assertIn(today, str(app.query_one(DateClock).render()))
            self.assertIn(".hidden.png", tree_names(app))
            self.assertTrue(app.view_all)
        self.run_app(restarted)

    def test_theme_row_opens_the_theme_picker(self):
        from textual.command import CommandPalette

        async def scenario(app, pilot):
            await pilot.press("s", "enter")
            await pilot.pause(0.3)
            self.assertIsInstance(app.screen, CommandPalette)
        self.run_app(scenario)

    def test_cleaning_options_are_remembered_only_when_asked(self):
        from textual.widgets import Input, Select, Switch

        def set_options(app):
            app.query_one("#mode", Select).value = "folder"
            app.query_one("#outdir", Input).value = "~/clean"
            app.query_one("#full", Switch).value = False
            app.query_one("#force", Switch).value = True

        def options(app):
            return (app.query_one("#mode", Select).value, app.query_one("#outdir", Input).value,
                    app.query_one("#full", Switch).value, app.query_one("#force", Switch).value)

        async def without(app, pilot):
            set_options(app)
            await pilot.pause(0.3)

        async def defaults(app, pilot):
            self.assertEqual(options(app), ("copy", "", True, False))

        self.run_app(without)
        self.run_app(defaults)
        self.assertFalse(config_path().exists())

        async def remember(app, pilot):
            await pilot.press("s", "down", "down", "down", "down", "enter", "escape")
            set_options(app)
            await pilot.pause(0.3)

        async def restored(app, pilot):
            await pilot.pause(0.2)
            self.assertEqual(options(app), ("folder", "~/clean", False, True))
            self.assertTrue(app.query_one("#folder-row").display)

        self.run_app(remember)
        self.assertEqual(self.saved()["options"], {"mode": "folder", "outdir": "~/clean", "full": False, "force": True})
        self.run_app(restored)

    def test_broken_settings_fall_back_to_defaults(self):
        from metawipe.tui import DateClock
        config_path().parent.mkdir(parents=True)
        config_path().write_text(json.dumps({"clock": "sundial", "show_hidden": "yes", "preview_everything": [1],
                                             "remember_options": True, "options": {"mode": "bogus", "full": "no", "outdir": 5}}))

        async def scenario(app, pilot):
            self.assertFalse(app.query_one(DateClock).display)
            self.assertNotIn(".hidden.png", tree_names(app))
            self.assertFalse(app.view_all)
            self.assertEqual(app.query_one("#mode").value, "copy")
            self.assertTrue(app.query_one("#full").value)
        self.run_app(scenario)


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
