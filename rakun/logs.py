"""Rakun's event stream (`rakunctl events`): one `<name> <json>` line per event, read in a thread and formatted."""
import collections
import json
import subprocess
import threading
import time

MAX_LINES = 500
MAX_TEXT = 200
STOPPED = "-- Rakun stopped --"
MAX_NOTICE = 256
DUPLICATE_SECONDS = 10
Notice = collections.namedtuple("Notice", "title body icon app installed")

# Rakun ends both an installation and an uninstallation with status "done"; what came before tells which one.
DOWNLOADED = "Download finished"
UNINSTALLED = "Game uninstalled"


def _clip(text):
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1] + "…"


def _megabytes(value):
    return "%.2f MB/s" % value if isinstance(value, (int, float)) else ""


class Formatter:
    """Turns event lines into readable ones; remembers the games' titles to name them in later events."""

    def __init__(self):
        self.titles = {}
        self.icons = {}
        self.noticed = {}
        self.last_status = {}

    def title(self, app):
        return self.titles.get(app) or app or "?"

    def learn(self, info):
        if isinstance(info, dict) and info.get("app_name") and info.get("title"):
            self.titles[info["app_name"]] = info["title"]
            for key in ("art_logo", "art_square", "art_cover"):
                if str(info.get(key, "")).startswith("https://"):
                    self.icons[info["app_name"]] = info[key]
                    break

    def notice(self, line, now=time.monotonic):
        """A Notice when the line is an event worth a notification (installed: a game was installed), else None."""
        name, _, rest = line.strip().partition(" ")
        if name != "gameStatusUpdate":
            return None
        try:
            item = json.loads(rest)[0]
            app, status = item["appName"], item["status"]
        except (ValueError, KeyError, IndexError, TypeError):
            return None
        before = self.last_status.get(app)
        self.last_status[app] = status
        if status != "done":
            return None
        # Rakun reports "done" twice (the second time without the runner): tell it once.
        at = now()
        if at - self.noticed.get(app, -DUPLICATE_SECONDS) < DUPLICATE_SECONDS:
            return None
        self.noticed[app] = at
        gone = before in ("uninstalling", "uninstalled")
        return Notice(UNINSTALLED if gone else DOWNLOADED, self.title(app)[:MAX_NOTICE], self.icons.get(app, ""),
                      app, not gone)

    def text(self, line):
        name, _, rest = line.partition(" ")
        try:
            data = json.loads(rest)
        except ValueError:
            return _clip(line)
        try:
            if name == "gameStatusUpdate":
                item = data[0]
                runner = " (%s)" % item["runner"] if item.get("runner") else ""
                return "%s · %s%s" % (item.get("status", "?"), self.title(item.get("appName")), runner)
            if name == "progressUpdate":
                item = data[0]
                p = item.get("progress") or {}
                parts = [item.get("runner"), self.title(item.get("appName")),
                         "%s %s%%" % (item.get("status", "?"), p.get("percent", 0)), p.get("bytes"),
                         "↓ " + _megabytes(p.get("downSpeed")) if p.get("downSpeed") is not None else None,
                         "ETA " + p["eta"] if p.get("eta") else None]
                return " · ".join(str(x) for x in parts if x)
            if name == "pushGameToLibrary":
                info = data[0]
                self.learn(info)
                return "library: %s (%s)" % (info.get("title") or info["app_name"], info.get("runner", "?"))
            if name == "changedDMQueueInformation":
                queue, state = data[0], data[1]
                for entry in queue:
                    self.learn((entry.get("params") or {}).get("gameInfo"))
                titles = [self.title((e.get("params") or {}).get("appName")) for e in queue]
                return "queue: %d (%s)%s" % (len(queue), state, " " + ", ".join(titles) if titles else "")
        except (KeyError, IndexError, TypeError, AttributeError):
            pass
        return _clip(line)


def format_event(line, formatter=None, now=None):
    """"HH:MM:SS text" for an event line, or None for a blank one."""
    line = line.strip()
    if not line:
        return None
    return "%s %s" % (time.strftime("%H:%M:%S", time.localtime(now)), (formatter or Formatter()).text(line))


class LogReader:
    """Runs the events command in a daemon thread and keeps the last formatted lines."""

    def __init__(self, on_notice=None):
        self._on_notice = on_notice
        self._lock = threading.Lock()
        self._lines = collections.deque(maxlen=MAX_LINES)
        self._next = 0
        self._proc = None
        self._thread = None
        self._formatter = Formatter()

    def alive(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, command, env=None):
        if self.alive():
            return
        self._proc = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, errors="replace")
        self._thread = threading.Thread(target=self._read, args=(self._proc,), daemon=True)
        self._thread.start()

    def _add(self, text):
        with self._lock:
            self._lines.append((self._next, text))
            self._next += 1

    def _read(self, proc):
        for raw in proc.stdout:
            text = format_event(raw, self._formatter)
            if text:
                self._add(text)
            if self._on_notice:
                notice = self._formatter.notice(raw)
                if notice:
                    try:
                        self._on_notice(notice)
                    except Exception:  # a failing notification must not stop the reading
                        pass
        proc.stdout.close()
        proc.wait()
        self._add(format_event(STOPPED))

    def stop(self):
        proc, thread = self._proc, self._thread
        if proc and proc.poll() is None:
            proc.terminate()
        if thread:
            thread.join(timeout=2)

    def lines(self, since=0):
        """(id of the next line, the lines with an id of `since` or more)."""
        with self._lock:
            return self._next, [text for i, text in self._lines if i >= since]

    def clear(self):
        with self._lock:
            self._lines.clear()
