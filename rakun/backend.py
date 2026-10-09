"""Rakun: install and update it, start and stop it, see whether it answers and open its web interface."""
import base64
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import grids
from .logs import LogReader
from .session import session_env

ctx = None

DEFAULT_PORT = 17999
RAKUNCTL = Path.home() / ".local/opt/rakun/rakunctl"
RAKUN_REPO = "FranjeGueje/rakun"
RELEASES = "https://github.com/%s/releases" % RAKUN_REPO
RAW = "https://raw.githubusercontent.com/%s" % RAKUN_REPO
INSTALL_TIMEOUT = 600
LATEST_TTL = 300
VERSION = re.compile(r"\d+\.\d+\.\d+")
WEB_MODES = ("local", "network")
DEFAULT_WEB = "local"
LEGACY_DIR = Path.home() / ".config/invasor-rakun"  # where the port and the version were kept before ctx.data
SETTLE_TRIES = 20


SUPERVISE_SECONDS = 5

_stop = threading.Event()
_supervisor = None


def _notify(title, body, icon):
    """Shows a Steam notification, as Noty does; if Steam can't show it nothing else is affected."""
    try:
        try:
            ctx.notify(title, body, icon)
        except ctx.InvalidArgument:
            if not icon:
                raise
            ctx.notify(title, body)
    except (ctx.Unavailable, ctx.InvalidArgument) as e:
        ctx.log.warning("cannot show the notification %r: %s", title, e)


_refreshing = set()
_refreshing_lock = threading.Lock()


def _refresh_grids(app):
    """Tells Steam about the grid images Rakun left for the game it just installed, as the Artwork module does
    after writing one: without it they show only after restarting Steam. Rakun registers the game and downloads
    the images some time after the installation ends, so it waits for them."""
    try:
        found = grids.wait_for_grids(app, stop=_stop)
        if not found:
            ctx.log.info("no grid images to refresh for %s", app)
            return
        app_id, files = found
        for kind, path in files.items():
            asset = grids.KINDS[kind][1]
            try:
                if path.stat().st_size > grids.MAX_BYTES:
                    continue
                data = path.read_bytes()
                ctx.steam_call("Apps.SetCustomArtworkForApp", app_id, base64.b64encode(data).decode(),
                               path.suffix[1:], asset, timeout=15 + len(data) // (2 << 20))
            except OSError as e:
                ctx.log.warning("cannot read %s: %s", path, e)
            except ctx.Unavailable as e:
                ctx.log.info("no live refresh (%s): the grid images show after restarting Steam", e)
                return
        ctx.log.info("refreshed the grid images of %s (Steam app %s)", app, app_id)
    except Exception as e:  # a thread of its own: nothing may escape
        ctx.log.warning("cannot refresh the grid images of %s: %s", app, e)
    finally:
        with _refreshing_lock:
            _refreshing.discard(app)


def _on_notice(notice):
    _notify(notice.title, notice.body, notice.icon)
    if not notice.installed:
        return
    with _refreshing_lock:
        if notice.app in _refreshing:
            return
        _refreshing.add(notice.app)
    threading.Thread(target=_refresh_grids, args=(notice.app,), daemon=True).start()


reader = LogReader(_on_notice)


def _ensure_reader():
    """Opens the event stream if Rakun runs and nobody reads it (it only exists while Rakun runs)."""
    active = running()
    if active and not reader.alive():
        try:
            reader.start([str(RAKUNCTL), "events"], dict(os.environ, **session_env()))
        except OSError as e:
            raise ctx.Unavailable("cannot read the events: %s" % e)
    return active


def _supervise():
    """Keeps the stream open, so the notifications don't depend on the Logs tab being looked at."""
    while not _stop.wait(SUPERVISE_SECONDS):
        try:
            _ensure_reader()
        except Exception as e:  # the loop must survive anything
            ctx.log.warning("cannot watch the events: %s", e)


def setup(context):
    global ctx, _supervisor
    ctx = context
    _migrate()
    _stop.clear()
    _supervisor = threading.Thread(target=_supervise, daemon=True)
    _supervisor.start()


def teardown():
    _stop.set()
    reader.stop()
    if _supervisor:
        _supervisor.join(timeout=2)


def _load():
    return ctx.data.load()


def _save(**changes):
    try:
        ctx.data.update(**changes)
    except OSError as e:
        raise ctx.Unavailable("cannot save the settings: %s" % e)


def _migrate():
    """Moves what older versions kept in ~/.config/invasor-rakun into the module's own data (once)."""
    old = {}
    for name in ("port.json", "version.json"):
        try:
            data = json.loads((LEGACY_DIR / name).read_text())
        except (OSError, ValueError):
            continue
        old.update(data if isinstance(data, dict) else {})
    if old:
        try:
            ctx.data.transform(lambda data: {**old, **data})
        except OSError as e:
            ctx.log.warning("cannot migrate the old settings: %s", e)
            return
    for name in ("port.json", "version.json"):
        (LEGACY_DIR / name).unlink(missing_ok=True)
    try:
        LEGACY_DIR.rmdir()
    except OSError:
        pass


def port_get():
    """The saved port, or the default one."""
    try:
        port = int(_load()["port"])
    except (KeyError, ValueError, TypeError):
        return DEFAULT_PORT
    return port if 1024 <= port <= 65535 else DEFAULT_PORT


def port_set(port):
    """Saves the port rakun will be started on."""
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ctx.InvalidArgument("the port must be a number between 1024 and 65535")
    _save(port=port)
    return {"port": port}


def web_get():
    """The saved web access ("local" or "network"), or the default one."""
    web = _load().get("web")
    return web if web in WEB_MODES else DEFAULT_WEB


def web_set(web):
    """Saves who can reach rakun's web interface the next time it starts."""
    if web not in WEB_MODES:
        raise ctx.InvalidArgument("the web access must be one of: %s" % ", ".join(WEB_MODES))
    _save(web=web)
    return {"web": web}


def open_web():
    """Opens rakun's address in Steam's browser through a steam://openurl link (works in Game Mode)."""
    url = "http://localhost:%d" % port_get()
    try:
        subprocess.Popen(["xdg-open", "steam://openurl/" + url], env=dict(os.environ, **session_env()),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        raise ctx.Unavailable("cannot open the browser: %s" % e)
    return {"ok": True}


def _run(command, timeout=30, stdin=None):
    """Runs a command in the user's session (the Invasor service has none of its own)."""
    try:
        return subprocess.run(command, env=dict(os.environ, **session_env()), input=stdin,
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ctx.Unavailable("cannot run %s: %s" % (command[0], e))


def reachable(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def is_installed():
    return RAKUNCTL.is_file()


def _stamp():
    """Changes whenever Rakun is installed again."""
    try:
        return RAKUNCTL.with_name("rakun.cjs").stat().st_mtime_ns
    except OSError:
        return 0


def _remember(version):
    try:
        _save(version=version, stamp=_stamp())
    except Exception as e:  # only a cache
        ctx.log.warning("cannot remember the version: %s", e)


def _remembered():
    data = _load()
    return data["version"] if data.get("stamp") == _stamp() and VERSION.fullmatch(str(data.get("version"))) else ""


def installed_version():
    """The installed Rakun's version: None if it is not installed, "" if it cannot tell. Only a running
    Rakun says its version, so it is remembered. Never `status -s`: that starts a stopped Rakun."""
    if not is_installed():
        return None
    done = _run([str(RAKUNCTL), "status", "--json"])
    try:
        version = str(json.loads(done.stdout)["health"]["version"])
        if VERSION.fullmatch(version):
            if version != _remembered():
                _remember(version)
            return version
    except (ValueError, KeyError, TypeError):
        pass
    return _remembered()


_latest = {"at": 0.0, "tag": None}


def latest_tag(now=time.monotonic):
    """The tag of the latest published release (e.g. "v0.2.0"), from where /releases/latest redirects to."""
    if _latest["tag"] and now() - _latest["at"] < LATEST_TTL:
        return _latest["tag"]
    request = urllib.request.Request(RELEASES + "/latest", method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=10) as answer:
            tag = answer.geturl().rstrip("/").rsplit("/", 1)[-1]
    except (OSError, urllib.error.URLError) as e:
        raise ctx.Unavailable("cannot reach GitHub: %s" % e)
    if not VERSION.search(tag):
        raise ctx.Unavailable("GitHub has no published release")
    _latest.update(at=now(), tag=tag)
    return tag


def install_status():
    """Whether Rakun is installed, which version, the latest published one and whether an update is due."""
    version = installed_version()
    try:
        latest = latest_tag().lstrip("v")
    except ctx.Unavailable:
        latest = None
    return {"installed": version is not None, "version": version or "", "latest": latest,
            "update": version is not None and latest is not None and version != latest}


def install():
    """Installs or updates Rakun with the install script of the latest release."""
    tag = latest_tag()
    script = _fetch("%s/%s/scripts/install.sh" % (RAW, tag))
    tarball = "%s/download/%s/rakun-%s-linux-x64.tar.gz" % (RELEASES, tag, tag.lstrip("v"))
    done = _run(["bash", "-s", "--", tarball], timeout=INSTALL_TIMEOUT, stdin=script)
    if done.returncode != 0:
        raise ctx.Unavailable("the installation failed: %s" % (done.stderr.strip() or done.stdout.strip() or done.returncode))
    _remember(tag.lstrip("v"))
    return install_status()


def _fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=30) as answer:
            return answer.read().decode()
    except (OSError, urllib.error.URLError, ValueError) as e:
        raise ctx.Unavailable("cannot download %s: %s" % (url, e))


def running():
    """Whether rakun runs, whatever port it listens on (rakunctl does not need systemd)."""
    if not is_installed():
        return False
    done = _run([str(RAKUNCTL), "status", "--json"])
    try:
        return bool(json.loads(done.stdout).get("running"))
    except (ValueError, AttributeError, TypeError):
        return False


def service_status():
    """Whether rakun runs, whether its port answers, and the saved port."""
    port = port_get()
    return {"active": running(), "reachable": reachable(port), "port": port, "web": web_get()}


def service_set(on, force=False):
    """Starts (on, with the saved port) or stops (off) rakun with rakunctl. Rakun refuses to stop while it
    downloads or refreshes the library, unless forced: then the answer has "busy" and nothing was stopped."""
    if not is_installed():
        raise ctx.Unavailable("rakun is not installed")
    command = [str(RAKUNCTL), "start", "--port", str(port_get()), "--web", web_get()] if on else [str(RAKUNCTL), "stop"]
    if not on and force:
        command.append("--force")
    done = _run(command)
    if done.returncode != 0 and not on and not force and "--force" in done.stderr + done.stdout:
        return dict(service_status(), busy=True)
    if done.returncode != 0:
        raise ctx.Unavailable("rakunctl failed: %s" % (done.stderr.strip() or done.stdout.strip() or done.returncode))
    # The slider shows what really happened, so give rakun a moment to settle.
    for _ in range(SETTLE_TRIES):
        status = service_status()
        if status["active"] == bool(on):
            break
        time.sleep(0.5)
    else:
        ctx.log.warning("rakun did not %s after %.0f s", "start" if on else "stop", SETTLE_TRIES * 0.5)
    return status


def logs_get(since=0):
    """The formatted event lines from `since` on. The stream only exists while Rakun runs, so it is
    opened here, on demand, and ends by itself when Rakun stops."""
    if isinstance(since, bool) or not isinstance(since, int) or since < 0:
        raise ctx.InvalidArgument("since must be a number from 0 on")
    active = _ensure_reader()
    next_id, lines = reader.lines(since)
    return {"id": next_id, "lines": lines, "running": active}


def logs_clear():
    reader.clear()
    return {"ok": True}


METHODS = {"open_web": open_web, "service_status": service_status,
           "service_set": service_set, "port_set": port_set, "web_set": web_set, "install_status": install_status,
           "install": install, "logs_get": logs_get, "logs_clear": logs_clear}
