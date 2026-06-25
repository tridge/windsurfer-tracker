"""gt06.log daily rotation must not lose raw frames.

rotate_log_to renames the live log into old_logs/ and opens a fresh one. The
writer (run() select loop) and the rotator run in different threads, so the
swap must never leave self._log_fd None — _log_packet drops a frame whenever
_log_fd is None. These tests pin that the fd stays valid across rotation and
that a frame on either side of the rotation lands (no loss, no dup).
"""
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from protocol_GT06 import GT06Listener  # noqa: E402
from gt06_dump import detect_format, read_packets  # noqa: E402

F1 = b"\x78\x78\x05\x13\x01\x02\x03\x04\x0d\x0a"   # before rotation
F2 = b"\x78\x78\x05\x16\x0a\x0b\x0c\x0d\x0d\x0a"   # after rotation


def _listener(tmp_path):
    lst = GT06Listener(0, 1, "G", lambda *a, **k: None,
                       gt06_config={"default_eid": 1, "devices": {}},
                       log_func=lambda *a, **k: None)
    lst.log_file = tmp_path / "gt06.log"
    return lst


def _frames(p):
    with open(p, "rb") as f:
        return [fr for _ts, _c, _o, fr in read_packets(f, fmt=detect_format(f))]


def test_rotation_loses_no_frames(tmp_path):
    lst = _listener(tmp_path)
    lst._open_log_v2(lst.log_file)
    conn = SimpleNamespace(conn_id=7)
    lst._log_packet(conn, F1)
    assert lst._log_fd is not None
    archive = tmp_path / "gt06.log.2026-06-26"
    lst.rotate_log_to(archive)
    assert lst._log_fd is not None          # never left None by the swap
    lst._log_packet(conn, F2)
    assert archive.exists() and lst.log_file.exists()
    arch, new = _frames(archive), _frames(lst.log_file)
    assert arch == [F1]                     # pre-rotation frame archived, intact
    assert new == [F2]                      # post-rotation frame in the fresh log
    assert lst.log_file.read_bytes().startswith(b"GT06LOG2")  # fresh magic header


def test_fd_valid_and_swapped(tmp_path):
    lst = _listener(tmp_path)
    lst._open_log_v2(lst.log_file)
    fd_before = lst._log_fd
    lst.rotate_log_to(tmp_path / "arch")
    assert lst._log_fd is not None and lst._log_fd is not fd_before
    assert fd_before.closed                 # old fd flushed + closed after swap


def test_concurrent_writes_across_rotations_lose_nothing(tmp_path):
    """A writer thread hammers _log_packet while the main thread rotates the log
    repeatedly. Every frame must survive (no loss, no dup) and every file must be
    an independently parseable v2 log (magic first, never written mid-frame)."""
    lst = _listener(tmp_path)
    lst._open_log_v2(lst.log_file)
    conn = SimpleNamespace(conn_id=3)
    stop = threading.Event()
    written = []

    def writer():
        i = 0
        while not stop.is_set():
            fr = b"\x78\x78\x05\x13" + i.to_bytes(4, "big") + b"\x0d\x0a"  # unique
            lst._log_packet(conn, fr)
            written.append(fr)             # recorded only after a returned write
            i += 1
            time.sleep(0.0002)             # stretch so rotations overlap writes

    t = threading.Thread(target=writer)
    t.start()
    archives = []
    for k in range(6):
        time.sleep(0.004)
        arch = tmp_path / f"gt06.log.arch{k}"
        lst.rotate_log_to(arch)
        archives.append(arch)
    stop.set()
    t.join()

    got = []
    for fpath in [a for a in archives if a.exists()] + [lst.log_file]:
        assert fpath.read_bytes().startswith(b"GT06LOG2")   # never magic-less/mid-frame
        got.extend(_frames(fpath))
    assert len(written) > 50                 # the race window was actually exercised
    assert sorted(got) == sorted(written)    # exactly the written frames, no loss/dup
