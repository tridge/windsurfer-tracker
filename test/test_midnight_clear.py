"""Unit test for EventTracker.clear_positions_only().

The midnight auto-clear must preserve per-sailor operator-intent state
(sleep / idle / stopped / did / etc.) while dropping yesterday's position
data. Regression test for the 2026-05-29 overnight incident where 26
trackers in event 8 silently fell out of SLEEP at midnight because the
clear discarded the sleep flag — the next MODE5 wake then triggered the
race-day login chain and the trackers spent the rest of the night
TIMER-thrashing the battery.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from tracker_server import EventTracker  # noqa: E402


@pytest.fixture
def tracker(tmp_path):
    cfg = {"name": "Test Event", "timezone": "Australia/Sydney"}
    return EventTracker(eid=1, data_dir=tmp_path, event_config=cfg)


def _seed_sailor(tracker, sailor_id, **fields):
    """Drop a sailor entry straight into the in-memory dict."""
    pt = tracker.position_tracker
    base = {
        "id": sailor_id,
        "lat": -36.8485, "lon": 174.7633,
        "spd": 5.0, "hdg": 90,
        "ts": 1779990000,
        "last_seen": 1779990000.0,
        "last_seen_iso": "2026-05-28T12:00:00",
        "bat": 80, "sig": 3,
        "did": f"86655708{sailor_id[1:]}",
        "role": "sailor",
    }
    base.update(fields)
    with pt._lock:
        pt.current_positions[sailor_id] = base
        pt.last_timestamp[sailor_id] = base["ts"]


def test_midnight_clear_preserves_sleep_flag(tracker):
    _seed_sailor(tracker, "G378848", sleep=True, idle=True, stopped=True, chg=False)
    tracker.clear_positions_only()
    pos = tracker.position_tracker.current_positions["G378848"]
    assert pos.get("sleep") is True, "sleep flag was wiped by midnight clear"
    assert pos.get("idle") is True
    assert pos.get("stopped") is True
    # Position data should be gone.
    assert "lat" not in pos
    assert "lon" not in pos
    assert "ts" not in pos
    # Identity preserved so the WebUI sidebar can still render the entry.
    assert pos.get("did") == "86655708378848"
    assert pos.get("role") == "sailor"


def test_midnight_clear_preserves_idle_stop_when_no_sleep(tracker):
    _seed_sailor(tracker, "G334189", idle=True, stopped=True)
    tracker.clear_positions_only()
    pos = tracker.position_tracker.current_positions["G334189"]
    assert pos.get("idle") is True
    assert pos.get("stopped") is True
    assert pos.get("sleep") is None  # never set, must not appear after clear
    assert "lat" not in pos


def test_midnight_clear_drops_active_position_data(tracker):
    """A tracker that was actively reporting positions before midnight
    should lose its lat/lon (fresh map for the new day) but its identity
    + battery telemetry stays."""
    _seed_sailor(tracker, "G304307", idle=False, stopped=False,
                 lat=-36.84, lon=174.76, spd=12.0, hdg=180,
                 chg=True, bat_v=4.05)
    tracker.clear_positions_only()
    pos = tracker.position_tracker.current_positions["G304307"]
    assert "lat" not in pos
    assert "lon" not in pos
    assert "spd" not in pos
    assert pos.get("idle") is False
    assert pos.get("stopped") is False
    assert pos.get("chg") is True
    assert pos.get("bat_v") == 4.05


def test_midnight_clear_resets_dedup_state(tracker):
    """last_timestamp and last_sq must be cleared so a tracker's first
    packet of the new day isn't dropped as a duplicate."""
    _seed_sailor(tracker, "G226122", idle=True, stopped=True)
    pt = tracker.position_tracker
    pt.last_sq["G226122"] = 12345
    tracker.clear_positions_only()
    assert pt.last_timestamp == {}
    assert pt.last_sq == {}


def test_midnight_clear_writes_stubs_to_disk(tracker):
    """The on-disk JSON must contain the preserved stubs so a server
    restart immediately after midnight still picks up the sleep flag."""
    _seed_sailor(tracker, "G378848", sleep=True, idle=True, stopped=True)
    tracker.clear_positions_only()
    data = json.loads(tracker.positions_file.read_text())
    sailors = data.get("sailors", {})
    assert "G378848" in sailors
    assert sailors["G378848"].get("sleep") is True
    assert "lat" not in sailors["G378848"]
