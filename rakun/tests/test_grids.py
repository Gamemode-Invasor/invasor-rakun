import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rakun import grids  # noqa: E402

ID = 2736524302


class Env:
    def __init__(self, d):
        self.root = Path(d) / "Steam"
        self.shortcuts = Path(d) / "steam_shortcuts.json"
        self.now = 1000.0

    def register(self, app="app1", steam_id=ID):
        self.shortcuts.write_text(json.dumps([{"appId": "other", "steamAppId": 1}, {"appId": app, "steamAppId": steam_id}]))

    def image(self, name, account="123", age=100):
        folder = self.root / "userdata" / account / "config" / "grid"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(b"x")
        import os
        os.utime(path, (self.now - age, self.now - age))
        return path


class GridsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = Env(self.tmp.name)

    def test_steam_app_id(self):
        self.assertIsNone(grids.steam_app_id("app1", self.env.shortcuts))
        self.env.register()
        self.assertEqual(grids.steam_app_id("app1", self.env.shortcuts), ID)
        self.assertIsNone(grids.steam_app_id("missing", self.env.shortcuts))
        self.env.shortcuts.write_text("not json")
        self.assertIsNone(grids.steam_app_id("app1", self.env.shortcuts))

    def test_grid_files_by_kind_and_newest_account(self):
        old = self.env.image("%dp.png" % ID, account="111", age=500)
        capsule = self.env.image("%dp.png" % ID)
        hero = self.env.image("%d_hero.jpg" % ID)
        self.env.image("%d_icon.png" % ID)  # not an artwork kind
        self.env.image("999p.png")
        got = grids.grid_files(ID, self.env.root)
        self.assertEqual(got, {"capsule": capsule, "hero": hero})
        self.assertNotIn(old, got.values())
        self.assertEqual(grids.grid_files(5, self.env.root), {})

    def test_wait_returns_when_registered_and_written(self):
        self.env.register()
        self.env.image("%dp.png" % ID, age=60)
        got = grids.wait_for_grids("app1", shortcuts=self.env.shortcuts, root=self.env.root,
                                   clock=lambda: self.env.now, sleep=lambda s: None)
        self.assertEqual(got[0], ID)
        self.assertEqual(list(got[1]), ["capsule"])

    def test_wait_lets_a_file_still_being_written_settle(self):
        self.env.register()
        self.env.image("%dp.png" % ID, age=0)

        def sleep(seconds):
            self.env.now += seconds

        got = grids.wait_for_grids("app1", shortcuts=self.env.shortcuts, root=self.env.root,
                                   clock=lambda: self.env.now, sleep=sleep, settle=3, poll=2)
        self.assertIsNotNone(got)
        self.assertGreaterEqual(self.env.now, 1004)

    def test_wait_gives_up(self):
        ticks = []

        def sleep(seconds):
            self.env.now += seconds
            ticks.append(seconds)

        self.assertIsNone(grids.wait_for_grids("app1", shortcuts=self.env.shortcuts, root=self.env.root,
                                               clock=lambda: self.env.now, sleep=sleep, timeout=10, poll=3))
        self.assertTrue(ticks)

    def test_wait_stops_with_the_event(self):
        import threading
        stop = threading.Event()
        stop.set()
        self.assertIsNone(grids.wait_for_grids("app1", stop=stop, shortcuts=self.env.shortcuts, root=self.env.root))


if __name__ == "__main__":
    unittest.main()
