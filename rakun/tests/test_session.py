import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rakun.session import session_env  # noqa: E402


def proc(root, pid, comm, env):
    d = Path(root) / str(pid)
    d.mkdir()
    (d / "comm").write_text(comm + "\n")
    (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in env.items()) + b"\0")


class SessionEnvTest(unittest.TestCase):
    def test_prefers_steam_over_other_graphical_processes(self):
        with tempfile.TemporaryDirectory() as root:
            proc(root, 1, "foot", {"DISPLAY": ":0", "HOME": "/x"})
            proc(root, 2, "steam", {"DISPLAY": ":1", "XDG_RUNTIME_DIR": "/run/user/1000", "SECRET": "no"})
            self.assertEqual(session_env(root), {"DISPLAY": ":1", "XDG_RUNTIME_DIR": "/run/user/1000"})

    def test_nothing_graphical_gives_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            proc(root, 1, "sshd", {"HOME": "/x"})
            self.assertEqual(session_env(root), {})


if __name__ == "__main__":
    unittest.main()
