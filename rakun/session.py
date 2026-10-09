"""The graphical session's environment, which the Invasor service doesn't have."""
from pathlib import Path

# xdg-open needs these to hand a steam:// link to the running Steam.
SESSION_VARS = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_CURRENT_DESKTOP", "XDG_SESSION_TYPE",
                "XDG_RUNTIME_DIR", "XDG_DATA_DIRS", "DBUS_SESSION_BUS_ADDRESS")


def _environ(pid_dir):
    try:
        raw = (pid_dir / "environ").read_bytes()
    except OSError:
        return {}
    pairs = (item.partition(b"=") for item in raw.split(b"\0") if b"=" in item)
    return {k.decode(errors="replace"): v.decode(errors="replace") for k, _, v in pairs}


def session_env(proc_root="/proc"):
    """The session variables of the user's running Steam (else of any of their graphical processes)."""
    steam, other = {}, {}
    try:
        pids = [d for d in Path(proc_root).iterdir() if d.name.isdigit()]
    except OSError:
        return {}
    for d in pids:
        env = _environ(d)
        if not (env.get("WAYLAND_DISPLAY") or env.get("DISPLAY")):
            continue
        try:
            comm = (d / "comm").read_text().strip()
        except OSError:
            comm = ""
        picked = {k: env[k] for k in SESSION_VARS if k in env}
        if comm in ("steam", "steamwebhelper"):
            steam = steam or picked
        elif not other:
            other = picked
        if steam:
            break
    return steam or other
