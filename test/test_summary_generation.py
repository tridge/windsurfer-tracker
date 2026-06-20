"""Tests for summary generation (unit-level, imports function directly)."""

import json
import sys
import time
from pathlib import Path

import pytest

# Add server directory to path so we can import
SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER_DIR))

from tracker_server import generate_log_summaries


def test_generate_summary(tmp_path):
    """Write JSONL manually, call generate_log_summaries, verify summary created."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    # Write a test JSONL file
    log_file = log_dir / "2026_02_01.jsonl"
    entries = [
        {"id": "S01", "ts": 1000000, "lat": -36.85, "lon": 174.76, "spd": 10, "hdg": 90,
         "ast": False, "bat": 80, "sig": 3, "role": "sailor"},
        {"id": "S01", "ts": 1000010, "lat": -36.851, "lon": 174.761, "spd": 11, "hdg": 95,
         "ast": False, "bat": 79, "sig": 3, "role": "sailor"},
        {"id": "S02", "ts": 1000005, "lat": -36.852, "lon": 174.762, "spd": 9, "hdg": 100,
         "ast": False, "bat": 90, "sig": 4, "role": "support"},
    ]
    with open(log_file, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")

    count = generate_log_summaries(log_dir)
    assert count == 1

    summary_file = log_dir / "2026_02_01_summary.json"
    assert summary_file.exists()

    summary = json.loads(summary_file.read_text())
    assert summary["date"] == "2026_02_01"
    assert len(summary["logs"]) == 1
    log_entry = summary["logs"][0]
    assert log_entry["point_count"] == 3
    assert "S01" in log_entry["sailors"]
    assert "S02" in log_entry["sailors"]
    assert log_entry["sailors"]["S01"]["points"] == 2
    assert log_entry["sailors"]["S02"]["points"] == 1


def test_display_name_change_counts_once(tmp_path):
    """A tracker renamed mid-log (displayid null -> name) must be ONE sailor,
    keyed by sailor_id, with the latest display name kept (regression: it used
    to split into two entries and double the sailor count)."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_file = log_dir / "2026_02_02.jsonl"
    entries = [
        {"id": "G226122", "ts": 1000000, "lat": -36.85, "lon": 174.76, "role": "sailor"},
        {"id": "G226122", "ts": 1000010, "lat": -36.85, "lon": 174.76, "role": "sailor"},
        {"id": "G226122", "ts": 1000020, "lat": -36.85, "lon": 174.76, "role": "sailor",
         "displayid": "T3Ah-V663-1"},
    ]
    with open(log_file, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    generate_log_summaries(log_dir)
    summary = json.loads((log_dir / "2026_02_02_summary.json").read_text())
    sailors = summary["logs"][0]["sailors"]
    assert list(sailors.keys()) == ["G226122"]               # one entry, keyed by sailor_id
    assert sailors["G226122"]["points"] == 3                 # all points together
    assert sailors["G226122"]["displayid"] == "T3Ah-V663-1"  # latest name kept


def test_summary_per_sailor_stats(tmp_path):
    """Summary should have correct per-sailor stats."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    log_file = log_dir / "2026_02_02.jsonl"
    entries = [
        {"id": "A", "ts": 2000, "lat": 0, "lon": 0},
        {"id": "A", "ts": 2010, "lat": 0, "lon": 0},
        {"id": "A", "ts": 2020, "lat": 0, "lon": 0},
        {"id": "B", "ts": 2005, "lat": 0, "lon": 0},
    ]
    with open(log_file, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")

    generate_log_summaries(log_dir)
    summary = json.loads((log_dir / "2026_02_02_summary.json").read_text())
    log_entry = summary["logs"][0]

    assert log_entry["sailors"]["A"]["points"] == 3
    assert log_entry["sailors"]["A"]["first_ts"] == 2000
    assert log_entry["sailors"]["A"]["last_ts"] == 2020
    assert log_entry["sailors"]["B"]["points"] == 1


def test_summary_skips_when_uptodate(tmp_path):
    """Summary should not regenerate when already up-to-date."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    log_file = log_dir / "2026_02_03.jsonl"
    log_file.write_text('{"id": "X", "ts": 3000, "lat": 0, "lon": 0}\n')

    # First generation
    count1 = generate_log_summaries(log_dir)
    assert count1 == 1

    # Second generation without changes
    count2 = generate_log_summaries(log_dir)
    assert count2 == 0


def test_summary_handles_rotated_files(tmp_path):
    """Summary should handle rotated JSONL files (.jsonl.1)."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    # Main file
    (log_dir / "2026_02_04.jsonl").write_text(
        '{"id": "S01", "ts": 4000, "lat": 0, "lon": 0}\n'
    )
    # Rotated file
    (log_dir / "2026_02_04.jsonl.1").write_text(
        '{"id": "S02", "ts": 3500, "lat": 0, "lon": 0}\n'
    )

    generate_log_summaries(log_dir)
    summary = json.loads((log_dir / "2026_02_04_summary.json").read_text())

    # Should have 2 log segments
    assert len(summary["logs"]) == 2
    total_points = sum(l["point_count"] for l in summary["logs"])
    assert total_points == 2
