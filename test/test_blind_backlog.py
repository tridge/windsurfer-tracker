"""Unit tests for GT06 blind-buffer backlog logging.

A V6.68 GT06 replays its blind buffer interleaved with live data after a
reconnect: 1 live LOC/s + 1 blind LOC/s (older GPS timestamps). The blind fixes
are "dup" vs the live high-water but are real positions filling an outage gap.
process_position must log them to the daily jsonl (at their own GPS time) without
regressing the live map marker. See server/tracker_server.py PositionTracker.
"""
import json
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from tracker_server import PositionTracker, DailyLogger  # noqa: E402


def _tracker(tmp_path):
    logger = DailyLogger(tmp_path / "logs")
    return PositionTracker(tmp_path / "current_positions.json", logger)


# Production GT06 packets reach PositionTracker with an event-prefixed source
# (EventTracker rewrites "GT06" -> "[E{eid}]GT06"); use that shape so the tests
# exercise the real gate, not a bare "GT06" that would mask a == vs endswith bug.
def _pp(tr, ts, *, sailor="G100001", lat=-35.30, lon=149.10, source="[E1]GT06",
        sq=0, idle=False, stopped=False, skip_log=False, pos_array=None):
    return tr.process_position(
        sailor_id=sailor, lat=lat, lon=lon, speed=5.0, heading=90, ts=ts,
        assist=False, battery=80, signal=3, role="sailor", version="gt06",
        flags={}, src_ip="35.156.18.25", source=source, sq=sq, idle=idle,
        stopped=stopped, skip_log=skip_log, pos_array=pos_array)


def _logged(tmp_path):
    """All jsonl track entries written so far (today's file)."""
    out = []
    for f in (tmp_path / "logs").glob("*.jsonl"):
        for line in f.read_text().splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def _ts_for(tmp_path, sailor="G100001"):
    return sorted(e["ts"] for e in _logged(tmp_path) if e["id"] == sailor)


def test_blind_fix_logged_without_map_regression(tmp_path):
    tr = _tracker(tmp_path)
    T = int(time.time())
    _pp(tr, T)            # live
    _pp(tr, T - 45)       # interleaved blind backlog fix
    # both fixes are in the recorded track
    assert _ts_for(tmp_path) == [T - 45, T]
    # live map still shows the newest fix; high-water not pulled back
    assert tr.current_positions["G100001"]["ts"] == T
    assert tr.last_timestamp["G100001"] == T


def test_blind_retransmit_not_double_logged(tmp_path):
    tr = _tracker(tmp_path)
    T = int(time.time())
    _pp(tr, T)
    _pp(tr, T - 45)
    _pp(tr, T - 45)       # app-level retransmit of the same blind fix
    assert _ts_for(tmp_path).count(T - 45) == 1


def test_blind_ts_equal_to_logged_live_not_double_logged(tmp_path):
    tr = _tracker(tmp_path)
    T = int(time.time())
    _pp(tr, T - 45)       # logged live (first fix)
    _pp(tr, T)            # advance high-water
    _pp(tr, T - 45)       # blind fix with a ts already in the track
    assert _ts_for(tmp_path).count(T - 45) == 1


def test_idle_blind_fix_not_logged(tmp_path):
    tr = _tracker(tmp_path)
    T = int(time.time())
    _pp(tr, T)
    _pp(tr, T - 45, idle=True)   # idle: never record lat/lon
    assert _ts_for(tmp_path) == [T]


def test_garbage_lag_not_logged(tmp_path):
    tr = _tracker(tmp_path)
    T = int(time.time())
    _pp(tr, T)
    _pp(tr, T - 90000)           # clock-glitch / default-date packet, over max lag
    assert _ts_for(tmp_path) == [T]


def test_phone_older_fix_not_blind_logged(tmp_path):
    # A UDP/HTTP phone packet also defaults sq=0, so the blind branch must gate on
    # source=="GT06", not sq==0: an older-ts UDP fix is a plain dup, never blind-logged.
    tr = _tracker(tmp_path)
    T = int(time.time())
    _pp(tr, T, source="[E1]UDP", sq=0)
    _pp(tr, T - 45, source="[E1]UDP", sq=0)  # dup-by-timestamp, but UDP → NOT blind-logged
    assert _ts_for(tmp_path) == [T]


def test_batch_packet_not_blind_logged(tmp_path):
    tr = _tracker(tmp_path)
    T = int(time.time())
    _pp(tr, T)
    # a skip_log/pos_array batch must not produce an extra single-fix blind write
    _pp(tr, T - 45, skip_log=True, pos_array=[[T - 45, -35.3, 149.1]])
    assert _ts_for(tmp_path) == [T]
