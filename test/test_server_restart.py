"""Regression test for the manage 'Restart Server' button.

The endpoint POST /api/manage/server/restart calls _schedule_server_restart(), which was
referenced but never defined — clicking Restart returned success then crashed with a
NameError and never restarted. This pins the function down: it must exist and re-exec the
process (os.execv with the same interpreter + argv), without actually replacing the test
process (os.execv is monkeypatched).
"""
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import tracker_server  # noqa: E402


def test_schedule_server_restart_reexecs(monkeypatch):
    captured = {}
    done = threading.Event()

    def fake_execv(path, argv):
        captured["path"] = path
        captured["argv"] = list(argv)
        done.set()

    monkeypatch.setattr(os, "execv", fake_execv)
    tracker_server._schedule_server_restart(delay=0.05)

    assert done.wait(2.0), "_schedule_server_restart did not call os.execv"
    assert captured["path"] == sys.executable
    # re-exec the same interpreter with the same script + args
    assert captured["argv"] == [sys.executable] + sys.argv
