import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rakun import backend  # noqa: E402
from rakun.logs import Notice  # noqa: E402


class Unavailable(Exception):
    pass


class InvalidArgument(Exception):
    pass


class FakeStore:
    def __init__(self):
        self.d = {}

    def load(self):
        return dict(self.d)

    def update(self, **changes):
        self.d.update(changes)

    def transform(self, fn):
        self.d = fn(dict(self.d))


class FakeCtx:
    Unavailable = Unavailable
    InvalidArgument = InvalidArgument

    def __init__(self):
        self.notify = mock.Mock()
        self.steam_call = mock.Mock()
        self.log = mock.Mock()
        self.data = FakeStore()


def fake_run(code, stderr="", stdout=""):
    return mock.patch.object(backend.subprocess, "run",
                             return_value=mock.Mock(returncode=code, stderr=stderr, stdout=stdout))


class BackendTest(unittest.TestCase):
    def setUp(self):
        self.legacy = tempfile.TemporaryDirectory()
        self.addCleanup(self.legacy.cleanup)
        patcher = mock.patch.object(backend, "LEGACY_DIR", Path(self.legacy.name) / "old")
        patcher.start()
        self.addCleanup(patcher.stop)
        backend.setup(FakeCtx())
        backend._latest.update(at=0.0, tag=None)
        patcher = mock.patch.object(backend, "is_installed", return_value=True)
        self.is_installed = patcher.start()
        self.addCleanup(patcher.stop)

    def test_methods(self):
        self.assertEqual(set(backend.METHODS),
                         {"open_web", "service_status", "service_set", "port_set", "web_set", "install_status", "install",
                          "logs_get", "logs_clear"})

    def test_open_web_hands_the_steam_link_to_xdg_open_with_the_session(self):
        with mock.patch.object(backend.subprocess, "Popen") as popen, \
                mock.patch.object(backend, "session_env", return_value={"DISPLAY": ":7"}), \
                mock.patch.object(backend, "port_get", return_value=18000):
            self.assertTrue(backend.open_web()["ok"])
        self.assertEqual(popen.call_args[0][0], ["xdg-open", "steam://openurl/http://localhost:18000"])
        self.assertEqual(popen.call_args[1]["env"]["DISPLAY"], ":7")

    def test_open_web_reports_a_missing_xdg_open(self):
        with mock.patch.object(backend.subprocess, "Popen", side_effect=OSError("no xdg-open")):
            with self.assertRaises(Unavailable):
                backend.open_web()

    def test_service_set_starts_rakun_on_the_saved_port(self):
        with fake_run(0, stdout='{"running": true}') as run, mock.patch.object(backend, "port_get", return_value=18123), \
                mock.patch.object(backend, "web_get", return_value="network"), \
                mock.patch.object(backend, "reachable", return_value=True):
            self.assertTrue(backend.service_set(True)["active"])
        self.assertEqual(run.call_args_list[0][0][0],
                         [str(backend.RAKUNCTL), "start", "--port", "18123", "--web", "network"])

    def test_service_set_off_stops_rakun(self):
        with fake_run(0, stdout='{"running": false}') as run, mock.patch.object(backend, "reachable", return_value=False):
            self.assertFalse(backend.service_set(False)["active"])
        self.assertEqual(run.call_args_list[0][0][0], [str(backend.RAKUNCTL), "stop"])

    def test_service_set_off_says_when_rakun_is_busy_and_can_force_it(self):
        busy = "rakunctl: rakun is downloading or refreshing the library: pause or cancel first, or use --force"
        with fake_run(1, busy), mock.patch.object(backend, "service_status", return_value={"active": True}):
            self.assertTrue(backend.service_set(False)["busy"])
        with fake_run(0, stdout='{"running": false}') as run, mock.patch.object(backend, "reachable", return_value=False):
            self.assertFalse(backend.service_set(False, force=True)["active"])
        self.assertEqual(run.call_args_list[0][0][0], [str(backend.RAKUNCTL), "stop", "--force"])
        with fake_run(1, "something else"):
            with self.assertRaises(Unavailable):
                backend.service_set(False)

    def test_service_set_waits_for_rakun_to_settle(self):
        answers = [mock.Mock(returncode=0, stdout="", stderr=""),
                   mock.Mock(returncode=0, stdout='{"running": false}'),
                   mock.Mock(returncode=0, stdout='{"running": true}')]
        with mock.patch.object(backend.subprocess, "run", side_effect=answers), \
                mock.patch.object(backend.time, "sleep"), mock.patch.object(backend, "reachable", return_value=True):
            self.assertTrue(backend.service_set(True)["active"])

    def test_service_set_reports_a_failure(self):
        with fake_run(1, "no rakun"):
            with self.assertRaises(Unavailable):
                backend.service_set(True)

    def test_service_status(self):
        with fake_run(0, stdout='{"running": true}') as run, mock.patch.object(backend, "reachable", return_value=True), \
                mock.patch.object(backend, "port_get", return_value=17999), \
                mock.patch.object(backend, "session_env", return_value={"X": "1"}):
            self.assertEqual(backend.service_status(),
                             {"active": True, "reachable": True, "port": 17999, "web": backend.web_get()})
        self.assertEqual(run.call_args[0][0], [str(backend.RAKUNCTL), "status", "--json"])
        self.assertEqual(run.call_args[1]["env"]["X"], "1")
        with fake_run(1, stdout="not json"), mock.patch.object(backend, "reachable", return_value=False):
            self.assertFalse(backend.service_status()["active"])

    def test_service_needs_an_installation(self):
        self.is_installed.return_value = False
        with mock.patch.object(backend.subprocess, "run") as run, mock.patch.object(backend, "reachable", return_value=False):
            self.assertFalse(backend.service_status()["active"])
            with self.assertRaises(Unavailable):
                backend.service_set(True)
        run.assert_not_called()

    def test_installed_version_from_the_json_and_never_with_dash_s(self):
        with fake_run(0, stdout='{"running": true, "health": {"version": "0.2.0"}}') as run:
            self.assertEqual(backend.installed_version(), "0.2.0")
        self.assertEqual(run.call_args[0][0], [str(backend.RAKUNCTL), "status", "--json"])

    def test_the_version_is_remembered_for_when_rakun_is_stopped(self):
        with fake_run(0, stdout='{"running": false}'):
            self.assertEqual(backend.installed_version(), "")
        with fake_run(0, stdout='{"running": true, "health": {"version": "0.2.0"}}'):
            backend.installed_version()
        with fake_run(0, stdout='{"running": false}') as run:
            self.assertEqual(backend.installed_version(), "0.2.0")
        self.assertNotIn("-s", run.call_args[0][0])
        with mock.patch.object(backend, "_stamp", return_value=123):  # Rakun was installed again
            with fake_run(0, stdout='{"running": false}'):
                self.assertEqual(backend.installed_version(), "")

    def test_installed_version_is_none_when_not_installed(self):
        self.is_installed.return_value = False
        self.assertIsNone(backend.installed_version())

    def test_latest_tag_follows_the_redirect_and_is_cached(self):
        answer = mock.MagicMock()
        answer.__enter__.return_value.geturl.return_value = "https://github.com/FranjeGueje/rakun/releases/tag/v0.3.0"
        with mock.patch.object(backend.urllib.request, "urlopen", return_value=answer) as urlopen:
            self.assertEqual(backend.latest_tag(), "v0.3.0")
            self.assertEqual(backend.latest_tag(), "v0.3.0")
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(urlopen.call_args[0][0].get_method(), "HEAD")

    def test_latest_tag_reports_no_network_or_no_release(self):
        with mock.patch.object(backend.urllib.request, "urlopen", side_effect=OSError("offline")):
            with self.assertRaises(Unavailable):
                backend.latest_tag()
        answer = mock.MagicMock()
        answer.__enter__.return_value.geturl.return_value = "https://github.com/FranjeGueje/rakun/releases"
        with mock.patch.object(backend.urllib.request, "urlopen", return_value=answer):
            with self.assertRaises(Unavailable):
                backend.latest_tag()

    def test_install_status(self):
        cases = [(None, "0.2.0", False, False), ("0.2.0", "0.2.0", True, False),
                 ("0.1.0", "0.2.0", True, True), ("", "0.2.0", True, True)]
        for version, latest, installed, update in cases:
            with mock.patch.object(backend, "installed_version", return_value=version), \
                    mock.patch.object(backend, "latest_tag", return_value="v" + latest):
                got = backend.install_status()
            self.assertEqual((got["installed"], got["update"], got["latest"]), (installed, update, latest))
        with mock.patch.object(backend, "installed_version", return_value="0.1.0"), \
                mock.patch.object(backend, "latest_tag", side_effect=Unavailable("offline")):
            got = backend.install_status()
        self.assertEqual((got["latest"], got["update"]), (None, False))

    def test_install_runs_the_script_of_the_latest_release(self):
        with mock.patch.object(backend, "latest_tag", return_value="v0.3.0"), \
                mock.patch.object(backend, "_fetch", return_value="echo hi") as fetch, \
                mock.patch.object(backend, "install_status", return_value={"installed": True}), \
                mock.patch.object(backend, "_remember") as remember, fake_run(0) as run:
            self.assertEqual(backend.install(), {"installed": True})
        remember.assert_called_once_with("0.3.0")
        self.assertEqual(fetch.call_args[0][0],
                         "https://raw.githubusercontent.com/FranjeGueje/rakun/v0.3.0/scripts/install.sh")
        self.assertEqual(run.call_args[0][0], ["bash", "-s", "--",
                         "https://github.com/FranjeGueje/rakun/releases/download/v0.3.0/rakun-0.3.0-linux-x64.tar.gz"])
        self.assertEqual(run.call_args[1]["input"], "echo hi")

    def test_install_reports_a_failing_script(self):
        with mock.patch.object(backend, "latest_tag", return_value="v0.3.0"), \
                mock.patch.object(backend, "_fetch", return_value=""), fake_run(1, "boom"):
            with self.assertRaises(Unavailable):
                backend.install()

    def test_logs_only_open_the_stream_while_rakun_runs(self):
        with mock.patch.object(backend, "running", return_value=False), \
                mock.patch.object(backend.reader, "start") as start:
            self.assertFalse(backend.logs_get()["running"])
        start.assert_not_called()
        with mock.patch.object(backend, "running", return_value=True), \
                mock.patch.object(backend.reader, "alive", return_value=False), \
                mock.patch.object(backend.reader, "start") as start, \
                mock.patch.object(backend, "session_env", return_value={}):
            self.assertTrue(backend.logs_get()["running"])
        self.assertEqual(start.call_args[0][0], [str(backend.RAKUNCTL), "events"])

    def test_notify_shows_the_notification_and_survives_a_refusal(self):
        backend._notify("Download finished", "Game", "https://x/i.png")
        backend.ctx.notify.assert_called_once_with("Download finished", "Game", "https://x/i.png")
        backend.ctx.notify.side_effect = Unavailable("no steam")
        backend._notify("Download finished", "Game", "")
        backend.ctx.log.warning.assert_called_once()

    def test_a_finished_installation_refreshes_the_grids_in_a_thread(self):
        with mock.patch.object(backend.threading, "Thread") as thread:
            backend._on_notice(Notice("Game uninstalled", "G", "", "app1", False))
            thread.assert_not_called()
            backend._on_notice(Notice("Download finished", "G", "", "app1", True))
            backend._on_notice(Notice("Download finished", "G", "", "app1", True))  # one at a time per game
        self.assertEqual(thread.call_count, 1)
        self.assertEqual(thread.call_args[1]["args"], ("app1",))
        backend._refreshing.discard("app1")
        self.assertEqual(backend.ctx.notify.call_count, 3)

    def test_refresh_grids_tells_steam_about_each_image(self):
        with tempfile.TemporaryDirectory() as d:
            files = {}
            for kind, name in (("capsule", "p.png"), ("hero", "_hero.jpg")):
                files[kind] = Path(d) / ("1" + name)
                files[kind].write_bytes(b"img-" + kind.encode())
            with mock.patch.object(backend.grids, "wait_for_grids", return_value=(2736524302, files)):
                backend._refresh_grids("app1")
        calls = [c[0] for c in backend.ctx.steam_call.call_args_list]
        self.assertEqual(calls[0][:2], ("Apps.SetCustomArtworkForApp", 2736524302))
        self.assertEqual([(c[3], c[4]) for c in calls], [("png", 0), ("jpg", 1)])
        self.assertEqual(calls[0][2], "aW1nLWNhcHN1bGU=")

    def test_refresh_grids_gives_up_quietly_without_images_or_steam(self):
        with mock.patch.object(backend.grids, "wait_for_grids", return_value=None):
            backend._refresh_grids("app1")
        backend.ctx.steam_call.assert_not_called()
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "1p.png"
            f.write_bytes(b"x")
            backend.ctx.steam_call.side_effect = Unavailable("no steam")
            with mock.patch.object(backend.grids, "wait_for_grids", return_value=(1, {"capsule": f, "hero": f})):
                backend._refresh_grids("app1")
        self.assertEqual(backend.ctx.steam_call.call_count, 1)

    def test_notify_retries_without_a_bad_icon(self):
        backend.ctx.notify.side_effect = [InvalidArgument("icon"), None]
        backend._notify("Game uninstalled", "Game", "https://x/bad")
        self.assertEqual(backend.ctx.notify.call_args_list[1][0], ("Game uninstalled", "Game"))

    def test_the_stream_is_kept_open_without_the_logs_tab(self):
        with mock.patch.object(backend, "running", return_value=True), \
                mock.patch.object(backend.reader, "alive", return_value=False), \
                mock.patch.object(backend.reader, "start") as start, \
                mock.patch.object(backend, "session_env", return_value={}):
            self.assertTrue(backend._ensure_reader())
        start.assert_called_once()

    def test_logs_get_validates_since(self):
        for bad in (-1, "0", True, None):
            with self.assertRaises(InvalidArgument):
                backend.logs_get(bad)

    def test_reachable_sees_an_open_port(self):
        with socket.socket() as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen()
            port = srv.getsockname()[1]
            self.assertTrue(backend.reachable(port))
        self.assertFalse(backend.reachable(port))

    def test_web_defaults_to_local_and_is_validated(self):
        self.assertEqual(backend.web_get(), "local")
        backend.web_set("network")
        self.assertEqual(backend.web_get(), "network")
        backend.ctx.data.d["web"] = "everyone"
        self.assertEqual(backend.web_get(), "local")
        for bad in ("everyone", "", None, 1):
            with self.assertRaises(InvalidArgument):
                backend.web_set(bad)

    def test_port_and_web_do_not_overwrite_each_other(self):
        backend.web_set("network")
        backend.port_set(18000)
        self.assertEqual((backend.port_get(), backend.web_get()), (18000, "network"))
        backend.web_set("local")
        self.assertEqual((backend.port_get(), backend.web_get()), (18000, "local"))

    def test_port_is_saved_and_validated(self):
        self.assertEqual(backend.port_get(), 17999)
        backend.port_set(18000)
        self.assertEqual(backend.port_get(), 18000)
        for bad in (80, 70000, "1234", True, None):
            with self.assertRaises(InvalidArgument):
                backend.port_set(bad)

    def test_old_files_are_migrated_once(self):
        old = backend.LEGACY_DIR
        old.mkdir()
        (old / "port.json").write_text('{"port": 18500, "web": "network"}')
        (old / "version.json").write_text('{"version": "0.2.0", "stamp": 1}')
        ctx = FakeCtx()
        ctx.data.d = {"port": 19000}
        backend.setup(ctx)
        self.assertEqual(ctx.data.d, {"port": 19000, "web": "network", "version": "0.2.0", "stamp": 1})
        self.assertFalse(old.exists())


if __name__ == "__main__":
    unittest.main()
