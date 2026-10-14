"""Rakun's event stream (`rakunctl events`): one `<name> <json>` line per event, read in a thread and formatted."""
import collections
import json
import subprocess
import threading
import time

MAX_LINES = 500
MAX_TEXT = 200
STOPPED = "-- Rakun stopped --"
MAX_TITLE = 256
DUPLICATE_SECONDS = 10
LATE_PROGRESS_SECONDS = 10  # Rakun's progress may arrive after the end of the operation
# kind: "start" | "progress" | "done" | "fail" | "notice" (quick: just a notification) | "stopped" (the stream ended); fraction: 0..1 or None
Event = collections.namedtuple("Event", "kind app title fraction text installed icon")

# Rakun ends both an installation and an uninstallation with status "done"; what came before tells which one.
DOWNLOADED = "Installed"
UNINSTALLED = "Game uninstalled"
IMPORTED = "Game imported"
ACTIVE = ("installing", "updating", "repairing", "moving", "extracting")
FAILED = ("error", "failed", "canceled", "cancelled", "aborted")


def _clip(text):
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1] + "…"


def _fraction(percent):
    """Rakun's percentage (a number or a numeric string) as 0..1, None if it is not one."""
    if isinstance(percent, str):
        try:
            percent = float(percent.strip().rstrip("%"))
        except ValueError:
            return None
    if isinstance(percent, bool) or not isinstance(percent, (int, float)) or percent != percent:
        return None
    return min(max(percent / 100, 0.0), 1.0)


def _megabytes(value):
    return "%.2f MB/s" % value if isinstance(value, (int, float)) else ""


class Formatter:
    """Turns event lines into readable ones; remembers the games' titles to name them in later events."""

    def __init__(self):
        self.titles = {}
        self.icons = {}
        self.noticed = {}
        self.last_status = {}
        self.ended = {}
        self.queue_state = ""
        self.opened = set()  # games with a start or progress event and no end yet

    def title(self, app):
        return self.titles.get(app) or app or "?"

    def learn(self, info):
        if isinstance(info, dict) and info.get("app_name") and info.get("title"):
            self.titles[info["app_name"]] = info["title"]
            for key in ("art_logo", "art_square", "art_cover"):
                if str(info.get(key, "")).startswith("https://"):
                    self.icons[info["app_name"]] = info[key]
                    break

    def event(self, line, now=time.monotonic):
        """An Event when the line is one a task follows (a game's operation starting, advancing or ending), else None."""
        name, _, rest = line.strip().partition(" ")
        if name == "changedDMQueueInformation":
            try:
                self.queue_state = str(json.loads(rest)[1])
            except (ValueError, IndexError, TypeError):
                pass
            return None
        if name not in ("gameStatusUpdate", "progressUpdate"):
            return None
        try:
            item = json.loads(rest)[0]
            app, status = item["appName"], item["status"]
        except (ValueError, KeyError, IndexError, TypeError):
            return None
        title = self.title(app)[:MAX_TITLE]
        if name == "progressUpdate":
            if now() - self.ended.get(app, -LATE_PROGRESS_SECONDS) < LATE_PROGRESS_SECONDS:
                return None
            progress = item.get("progress") if isinstance(item.get("progress"), dict) else {}
            self.opened.add(app)
            return Event("progress", app, title, _fraction(progress.get("percent")), self.progress_text(item), False, "")
        before = self.last_status.get(app)
        self.last_status[app] = status
        if status in ACTIVE:
            self.opened.add(app)
            return Event("start", app, title, None, status.capitalize(), False, "")
        if status in FAILED:
            self.opened.discard(app)
            return Event("fail", app, title, None, status.capitalize(), False, "")
        gone = status == "uninstalled" or (status == "done" and before in ("uninstalling", "uninstalled"))
        if status != "done" and not gone:
            return None
        # Rakun reports "done" twice (the second time without the runner): notify it once. A repeated "done" is still
        # passed on, without the refresh, while the game has a task that was left open, so that it closes.
        at = now()
        repeated = at - self.noticed.get(app, -DUPLICATE_SECONDS) < DUPLICATE_SECONDS
        if not repeated:
            self.noticed[app] = at
        left_open = app in self.opened
        self.opened.discard(app)
        self.ended[app] = at
        if repeated and not left_open:
            return None
        if left_open and self.queue_state == "paused":  # pausing aborts the download, which Rakun ends with "done"
            return Event("fail", app, title, None, "Paused", False, "")
        if gone:  # an uninstallation is too quick for a task: only a notification
            return None if repeated else Event("notice", app, title, None, UNINSTALLED, False, self.icons.get(app, ""))
        if before == "importing":  # so is an import
            return None if repeated else Event("notice", app, title, None, IMPORTED, True, self.icons.get(app, ""))
        return Event("done", app, title, 1.0, DOWNLOADED, not repeated, "")

    def progress_text(self, item):
        p = item.get("progress") or {}
        parts = [p.get("bytes"), "↓ " + _megabytes(p.get("downSpeed")) if p.get("downSpeed") is not None else None,
                 "ETA " + p["eta"] if p.get("eta") else None]
        return " · ".join(str(x) for x in parts if x)[:MAX_TEXT]

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

    def __init__(self, on_event=None):
        self._on_event = on_event
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

    def _emit(self, event):
        if self._on_event:
            try:
                self._on_event(event)
            except Exception:  # a failing task must not stop the reading
                pass

    def _read(self, proc):
        for raw in proc.stdout:
            text = format_event(raw, self._formatter)
            if text:
                self._add(text)
            if self._on_event:
                event = self._formatter.event(raw)
                if event:
                    self._emit(event)
        proc.stdout.close()
        proc.wait()
        self._add(format_event(STOPPED))
        self._emit(Event("stopped", "", "", None, STOPPED, False, ""))

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
