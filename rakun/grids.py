"""The grid images Rakun leaves in Steam's userdata when it adds a game, found so Steam can be told about them.

Pure stdlib, no Invasor imports: tested on its own (tests/test_grids.py). The file names and the asset types are
the same as the Artwork module's.
"""
import json
import os
import time
from pathlib import Path

STEAM_ROOT = Path.home() / ".local/share/Steam"
SHORTCUTS = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "rakun" / "steam_shortcuts.json"
# kind -> (file suffix, Steam's eAssetType)
KINDS = {"capsule": ("p", 0), "hero": ("_hero", 1), "logo": ("_logo", 2), "wide": ("", 3)}
EXTS = (".png", ".jpg", ".jpeg", ".webp")
MAX_BYTES = 128 << 20


def steam_app_id(app_name, path=None):
    """The Steam app id Rakun gave the game (its events don't carry it), or None."""
    try:
        shortcuts = json.loads(Path(path or SHORTCUTS).read_text())
        for entry in shortcuts:
            if entry.get("appId") == app_name:
                return int(entry["steamAppId"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def grid_files(app_id, root=None):
    """{kind: path} of the game's images, from the account folder whose images are the newest."""
    best, newest = {}, 0.0
    for grid in sorted(Path(root or STEAM_ROOT).glob("userdata/*/config/grid")):
        found = {}
        for kind, (suffix, _) in KINDS.items():
            for ext in EXTS:
                path = grid / f"{app_id}{suffix}{ext}"
                if path.is_file():
                    found[kind] = path
                    break
        stamp = max((p.stat().st_mtime for p in found.values()), default=0.0)
        if found and stamp >= newest:
            best, newest = found, stamp
    return best


def wait_for_grids(app_name, stop=None, timeout=180, poll=3, settle=3, shortcuts=None, root=None,
                   clock=time.time, sleep=time.sleep):
    """(Steam app id, {kind: path}) once Rakun has registered the game and finished writing its images, else None.
    `stop` is an Event that ends the wait."""
    deadline = clock() + timeout
    while True:
        app_id = steam_app_id(app_name, shortcuts)
        files = grid_files(app_id, root) if app_id else {}
        if files and all(clock() - p.stat().st_mtime >= settle for p in files.values()):
            return app_id, files
        if clock() >= deadline or (stop is not None and stop.is_set()):
            return None
        if stop is not None:
            stop.wait(poll)
        else:
            sleep(poll)
