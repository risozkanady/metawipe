import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import textual
except ImportError:  # pragma: no cover
    textual = None
else:
    from textual.widgets import OptionList, RichLog
    from metawipe.tui import MetawipeApp, SettingsScreen, config_path, load_keep

from tests.test_regressions import png_bytes
from tests.test_tui import wait_for_tree

OLD = 1_000_000_000  # 2001-09-09


@unittest.skipIf(textual is None, "textual missing")
class KeepTuiTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.tmp / "cfg")})
        env.start()
        self.addCleanup(env.stop)

    def test_load_keep_accepts_only_known_names(self):
        self.assertEqual(load_keep({"keep": ["people", "bogus", "dates", 5, ["x"], {"a": 1}]}), ["dates", "people"])
        for bad in (None, "dates", 7, {"dates": True}):
            self.assertEqual(load_keep({"keep": bad}), [])
        self.assertEqual(load_keep({}), [])

    def test_damaged_config_keeps_nothing(self):
        config_path().parent.mkdir(parents=True)
        config_path().write_text(json.dumps({"keep": ["location", "nope", None]}))

        async def scenario():
            app = MetawipeApp(self.tmp)
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(0.2)
                self.assertEqual(app.keep, {"location"})
                self.assertIn("keeps: location", app.sub_title)
        asyncio.run(scenario())

    def test_switch_in_settings_saves_and_shows_in_top_bar(self):
        async def scenario():
            app = MetawipeApp(self.tmp)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause(0.2)
                self.assertNotIn("keeps", app.sub_title)
                await pilot.press("s")
                await pilot.pause(0.2)
                options = app.screen.query_one(OptionList)
                self.assertIsInstance(app.screen, SettingsScreen)
                for name in ("dates", "location", "device", "software", "people", "descriptions", "thumbnails"):
                    self.assertIsNotNone(options.get_option(f"keep:{name}"), name)
                options.highlighted = options.get_option_index("keep:device")
                await pilot.press("space")
                options.highlighted = options.get_option_index("keep:dates")
                await pilot.press("enter")
                await pilot.pause(0.2)
                self.assertEqual(app.keep, {"dates", "device"})
                self.assertIn("keeps: dates, device", app.sub_title)  # catalogue order, updated at once
                self.assertEqual(json.loads(config_path().read_text())["keep"], ["dates", "device"])
                await pilot.press("escape")
                await pilot.press("space")  # marking still updates the title without losing the keeps note
                await pilot.pause(0.2)
                self.assertIn("keeps: dates, device", app.sub_title)
                await pilot.press("s")
                await pilot.pause(0.2)
                options = app.screen.query_one(OptionList)
                options.highlighted = options.get_option_index("keep:dates")
                await pilot.press("space")
                await pilot.pause(0.2)
                self.assertEqual(app.sub_title.count("keeps: device"), 1)
                self.assertNotIn("dates", app.sub_title)
        asyncio.run(scenario())
        self.assertEqual(json.loads(config_path().read_text())["keep"], ["device"])

    def test_run_passes_keep_and_logs_what_was_kept(self):
        src = self.tmp / "in"
        src.mkdir()
        pic = src / "a.png"
        pic.write_bytes(png_bytes([]))
        os.utime(pic, (OLD, OLD))

        async def scenario(keep):
            app = MetawipeApp(src)
            app.keep = set(keep)
            async with app.run_test(size=(140, 40)) as pilot:
                await wait_for_tree(app, pilot)
                await pilot.press("down", "space")
                self.assertEqual(app._args().keep, frozenset(keep))
                self.assertIsInstance(app._args().keep, frozenset)
                await pilot.press("r")
                await pilot.pause(2)
                return "\n".join(line.text for line in app.query_one("#log", RichLog).lines)

        text = asyncio.run(scenario(["dates"]))
        self.assertIn("(kept: dates)", text)
        self.assertEqual(int((src / "a.clean.png").stat().st_mtime), OLD)
        (src / "a.clean.png").unlink()
        text = asyncio.run(scenario([]))
        self.assertNotIn("kept", text)
        self.assertNotEqual(int((src / "a.clean.png").stat().st_mtime), OLD)  # wiped: 1980 (full scrub is on)
