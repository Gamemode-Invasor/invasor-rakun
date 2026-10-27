"""Rakun: install and update it, start and stop it, see whether it answers and open its web interface."""
import base64
import json
import re
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import grids, texts
from .logs import LogReader

ctx = None

NOTIFY_SOUND = "toast"  # one of Steam's own notification sounds, see the core's ctx.notify
TASK_IDLE_SECONDS = 300  # a task Rakun says nothing about for this long is closed
DEFAULT_PORT = 17999
RAKUNCTL = Path.home() / ".local/opt/rakun/rakunctl"
RAKUN_REPO = "FranjeGueje/rakun"
RELEASES = "https://github.com/%s/releases" % RAKUN_REPO
API_LATEST = "https://api.github.com/repos/%s/releases/latest" % RAKUN_REPO
DOWNLOAD_PREFIX = RELEASES + "/download/"
ASSET = "rakun-%s-linux-x64.tar.gz"
SHA256 = re.compile(r"sha256:([0-9a-f]{64})")
MAX_RELEASE_JSON = 1 << 20
INSTALL_TIMEOUT = 600
LATEST_TTL = 300
VERSION = re.compile(r"\d+\.\d+\.\d+")
TAG = re.compile(r"v?(%s)" % VERSION.pattern)
WEB_MODES = ("local", "network")
DEFAULT_WEB = "local"
LEGACY_DIR = Path.home() / ".config/invasor-rakun"  # where the port and the version were kept before ctx.data
SETTLE_TRIES = 20


SUPERVISE_SECONDS = 5

_stop = threading.Event()
_supervisor = None


_tasks = {}  # a game's app name -> its open task in Invasor's panel
_seen = {}  # a game's app name -> when its task last heard from Rakun
_tasks_lock = threading.Lock()


def _open_task(app, label):
    """The game's task, opened on first sight (the core announces it starting and ending); None if it can't be."""
    task = _tasks.get(app)
    if task is None:
        try:
            task = ctx.task(label)
        except ctx.Unavailable as e:
            ctx.log.warning("cannot open the task %r: %s", label, e)
            return None
        _tasks[app] = task
    _seen[app] = time.monotonic()
    return task


def _close_tasks(text):
    """Fails every task still open (Rakun stopped or the module goes away)."""
    with _tasks_lock:
        tasks = list(_tasks.values())
        _tasks.clear()
        _seen.clear()
    for task in tasks:
        task.fail(text)


def _expire_tasks(now=time.monotonic):
    """Fails the tasks Rakun has said nothing about for TASK_IDLE_SECONDS: its "done" got lost."""
    with _tasks_lock:
        stale = [app for app, at in _seen.items() if now() - at > TASK_IDLE_SECONDS]
        tasks = [_tasks.pop(app) for app in stale if app in _tasks]
        for app in stale:
            _seen.pop(app, None)
    for task in tasks:
        task.fail(texts.NO_NEWS)


def _notify(title, body, icon):
    """Shows a Steam notification; if Steam can't show it nothing else is affected."""
    try:
        try:
            ctx.notify(title, body, icon, sound=NOTIFY_SOUND)
        except ctx.InvalidArgument:
            if not icon:
                raise
            ctx.notify(title, body, sound=NOTIFY_SOUND)
    except (ctx.Unavailable, ctx.InvalidArgument) as e:
        ctx.log.warning("cannot show the notification %r: %s", title, e)


def _follow_task(event):
    if event.kind == "notice":
        with _tasks_lock:
            task = _tasks.pop(event.app, None)
            _seen.pop(event.app, None)
        if task:  # it should not have one: don't leave it running
            task.done(event.text)
        _notify(event.title, event.text, event.icon)
        return
    if event.kind == "stopped":
        _close_tasks(texts.RAKUN_STOPPED)
        return
    with _tasks_lock:
        if event.kind in ("done", "fail"):
            task = _tasks.pop(event.app, None)
            _seen.pop(event.app, None)
            if task:
                (task.done if event.kind == "done" else task.fail)(event.text)
        else:
            task = _open_task(event.app, event.title)
            if task:
                task.update(event.fraction, event.text)


_refreshing = set()
_refreshing_lock = threading.Lock()


def _refresh_grids(app):
    """Tells Steam about the grid images Rakun left for the game it just installed, as the Artwork module does
    after writing one: without it they show only after restarting Steam. Rakun registers the game and downloads
    the images some time after the installation ends, so it waits for them."""
    try:
        found = grids.wait_for_grids(app, ctx.paths.userdata(all=True), stop=_stop)
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
    except ctx.Unavailable as e:  # Steam's folder wasn't found
        ctx.log.info("no grid images to refresh for %s: %s", app, e)
    except Exception as e:  # a thread of its own: nothing may escape
        ctx.log.warning("cannot refresh the grid images of %s: %s", app, e)
    finally:
        with _refreshing_lock:
            _refreshing.discard(app)


def _on_event(event):
    _follow_task(event)
    if not event.installed:
        return
    with _refreshing_lock:
        if event.app in _refreshing:
            return
        _refreshing.add(event.app)
    threading.Thread(target=_refresh_grids, args=(event.app,), daemon=True).start()


reader = LogReader(_on_event)


def _ensure_reader():
    """Opens the event stream if Rakun runs and nobody reads it (it only exists while Rakun runs)."""
    active = running()
    if active and not reader.alive():
        try:
            reader.start([str(RAKUNCTL), "events"])
        except OSError as e:
            raise ctx.Unavailable("cannot read the events: %s" % e)
    return active


def _supervise():
    """Keeps the stream open, so the notifications don't depend on the Logs tab being looked at."""
    while not _stop.wait(SUPERVISE_SECONDS):
        try:
            _ensure_reader()
            _expire_tasks()
        except Exception as e:  # the loop must survive anything
            ctx.log.warning("cannot watch the events: %s", e)


def setup(context):
    global ctx, _supervisor
    ctx = context
    _migrate()
    _tasks.clear()
    _seen.clear()
    _stop.clear()
    _supervisor = threading.Thread(target=_supervise, daemon=True)
    _supervisor.start()


def teardown():
    _stop.set()
    reader.stop()
    _close_tasks(texts.MODULE_UNLOADED)
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
    """Opens rakun's address in Steam's browser (works in Game Mode)."""
    ctx.steam.open_url("http://localhost:%d" % port_get())
    return {"ok": True}


def _run(command, timeout=30):
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout)
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


_latest = {"at": 0.0, "release": None}


def _release(answer):
    """{"version", "url", "sha256"} of the x64 tarball in GitHub's answer for the latest release. sha256 is the
    asset's digest, None if GitHub gives none; the URL must be this repository's own release download."""
    try:
        tag = answer["tag_name"]
        version = TAG.fullmatch(tag).group(1)
        name = ASSET % version
        asset = next(a for a in answer["assets"] if a.get("name") == name)
        url = asset["browser_download_url"]
        if url != "%s%s/%s" % (DOWNLOAD_PREFIX, tag, name):
            raise ValueError(url)
    except (KeyError, TypeError, AttributeError, StopIteration, ValueError):
        raise ctx.Unavailable("GitHub has no usable release")
    digest = SHA256.fullmatch(str(asset.get("digest")))
    return {"version": version, "url": url, "sha256": digest.group(1) if digest else None}


def latest_release(now=time.monotonic, fresh=False):
    """The latest published release (see _release), asked to GitHub at most every LATEST_TTL seconds."""
    if not fresh and _latest["release"] and now() - _latest["at"] < LATEST_TTL:
        return _latest["release"]
    release = _release(ctx.http.get_json(API_LATEST, max_bytes=MAX_RELEASE_JSON))
    _latest.update(at=now(), release=release)
    return release


def _check(done, what):
    if done.returncode != 0:
        raise ctx.Unavailable("%s: %s" % (what, done.stderr.strip() or done.stdout.strip() or done.returncode))


def install_status(fresh=False):
    """Whether Rakun is installed, which version, the latest published one and whether an update is due."""
    version = installed_version()
    try:
        latest = latest_release(fresh=fresh)["version"]
    except (ctx.Unavailable, ctx.InvalidArgument):
        latest = None
    return {"installed": version is not None, "version": version or "", "latest": latest,
            "update": version is not None and latest is not None and version != latest}


def install():
    """Installs or updates Rakun from the tarball of the latest release, checked against GitHub's sha256, with
    the install.sh that tarball carries."""
    with ctx.task(texts.INSTALLING_RAKUN) as task:
        task.update(None, texts.LOOKING_FOR_RELEASE)
        release = latest_release(fresh=True)
        version = release["version"]
        work = RAKUNCTL.parent.parent  # the same filesystem as the installation
        try:
            work.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=work, prefix=".rakun-install-") as tmp:
                tarball = Path(tmp) / (ASSET % version)
                checksum = ("sha256", release["sha256"]) if release["sha256"] else None  # None: the core asks
                got = ctx.http.download(release["url"], tarball, checksum=checksum, task=task)
                # install.sh wants the checksum next to the tarball
                tarball.with_name(tarball.name + ".sha256").write_text("%s  %s\n" % (got["sha256"], tarball.name))
                task.update(None, texts.installing(version))
                script = ctx.archive.extract(tarball, Path(tmp) / "x", single_root=True) / "install.sh"
                if not script.is_file():
                    raise ctx.Unavailable("the release has no install.sh")
                _check(_run(["bash", str(script), str(tarball)], timeout=INSTALL_TIMEOUT), "the installation failed")
        except OSError as e:
            raise ctx.Unavailable("cannot install rakun: %s" % e)
        _remember(version)
        task.update(None, texts.CHECKING_INSTALLATION)
        status = install_status()
        task.done(texts.installed(version))
        return status


def update_check():
    """Asks GitHub for the latest release, skipping the cache."""
    return install_status(fresh=True)


def update():
    """Updates Rakun with `rakunctl self-update` (it restarts Rakun if it runs; only for an install made by
    install.sh)."""
    if not is_installed():
        raise ctx.Unavailable("rakun is not installed")
    with ctx.task(texts.UPDATING_RAKUN) as task:
        task.update(None, texts.RUNNING_SELF_UPDATE)
        _check(_run([str(RAKUNCTL), "self-update"], timeout=INSTALL_TIMEOUT), "rakunctl failed")
        task.update(None, texts.CHECKING_VERSION)
        status = install_status(fresh=True)
        if status["latest"]:
            _remember(status["latest"])
        task.done(texts.updated(status["latest"] or ""))
        return status


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
    _check(done, "rakunctl failed")
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
           "install": install, "update_check": update_check, "update": update, "logs_get": logs_get, "logs_clear": logs_clear}
