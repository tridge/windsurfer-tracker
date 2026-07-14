"""Unit tests for rotate_copytruncate().

Regression test for the 2026-07 disk-fill: tracker.log was root-owned
(systemd created the append: target before dropping privileges), so the
nightly truncate failed with EACCES — but the copy had already been made,
so a full cumulative multi-GB archive landed in old_logs/ every midnight.
The rotator must not leave an archive behind when it cannot truncate the
source.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from tracker_server import rotate_copytruncate  # noqa: E402


def test_rotate_success(tmp_path):
    src = tmp_path / "tracker.log"
    src.write_text("day one contents\n")
    archive = tmp_path / "old_logs" / "tracker.log.2026-07-14"
    archive.parent.mkdir()

    assert rotate_copytruncate(src, archive) is True
    assert archive.read_text() == "day one contents\n"
    assert src.stat().st_size == 0


def test_rotate_missing_source(tmp_path):
    src = tmp_path / "tracker.log"
    archive = tmp_path / "tracker.log.2026-07-14"
    assert rotate_copytruncate(src, archive) is False
    assert not archive.exists()


def test_rotate_copy_failure_removes_partial_archive(tmp_path, monkeypatch):
    import shutil
    src = tmp_path / "tracker.log"
    src.write_text("day one contents\n")
    archive = tmp_path / "tracker.log.2026-07-14"

    def fail_copy(s, d, **kwargs):
        Path(d).write_text("partial")
        raise OSError("disk full")

    monkeypatch.setattr(shutil, "copy2", fail_copy)
    assert rotate_copytruncate(src, archive) is False
    assert not archive.exists()
    assert src.read_text() == "day one contents\n"


def test_rotate_unwritable_source_leaves_no_archive(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root ignores file permissions")
    src = tmp_path / "tracker.log"
    src.write_text("must not be archived\n")
    src.chmod(0o444)
    archive = tmp_path / "tracker.log.2026-07-14"

    assert rotate_copytruncate(src, archive) is False
    assert not archive.exists()
    assert src.read_text() == "must not be archived\n"
