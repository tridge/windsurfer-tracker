"""Tests for the global GT06 default name/hide store (html/gt06_users.json).

It's the fallback for per-event users.json, edited from the GT06 Trackers manage page
(POST /api/manage/gt06/users). A default name/hide must (a) propagate live into a tracker's
current_positions.json, (b) be overridden by a per-event users.json entry, (c) clear when blank.

Uses plain UDP positions (deterministic) rather than the timing-fragile GT06 idle path —
the name/hide fallback in write_current_positions() is source-agnostic.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from conftest import HTTPClient, MANAGER_PASSWORD  # noqa: E402

EID = 1
ADMIN_PW = "admin123"


def _http(server):
    return HTTPClient(f"http://{server.host}:{server.port}")


def _mgr_post(server, path, body):
    return _http(server).post(path, data=body, headers={"X-Manager-Password": MANAGER_PASSWORD})


def _admin_post(server, path, body):
    return _http(server).post(path, data=body, headers={"X-Admin-Password": ADMIN_PW})


def _read_positions(server, eid=EID):
    p = server.data_dir / "html" / str(eid) / "current_positions.json"
    return json.loads(p.read_text()).get("sailors", {}) if p.exists() else {}


def _wait_for(cond, timeout=5.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = cond()
        if last:
            return last
        time.sleep(0.1)
    return last


def _send_pos(udp_client, sid, sq=1):
    return udp_client.send_position({
        "id": sid, "sq": sq, "ts": int(time.time()), "lat": -36.85, "lon": 174.76,
        "spd": 0, "hdg": 0, "ast": False, "bat": 80, "sig": 3,
        "role": "sailor", "ver": "test", "eid": EID})


def test_default_name_and_hide_apply_live(udp_client, server):
    sid = "G900001"
    _send_pos(udp_client, sid)
    assert _wait_for(lambda: _read_positions(server).get(sid)), "position not registered"

    status, body = _mgr_post(server, "/api/manage/gt06/users",
                             {"users": {sid: {"name": "Alice", "hidden": True}}})
    assert status == 200, body
    assert body.get("success") is True

    # name + hidden propagate into current_positions.json without a restart
    pos = _wait_for(lambda: (lambda p: p if p.get("name") == "Alice" else None)(
        _read_positions(server).get(sid, {})))
    assert pos, "default name did not propagate to current_positions.json"
    assert pos.get("name") == "Alice"
    assert pos.get("displayid") == "Alice"
    assert pos.get("hidden") is True

    saved = json.loads((server.data_dir / "html" / "gt06_users.json").read_text())["users"]
    assert saved[sid]["name"] == "Alice" and saved[sid]["hidden"] is True


def test_per_event_override_wins_then_clear(udp_client, server):
    sid = "G900002"
    _send_pos(udp_client, sid)
    assert _wait_for(lambda: _read_positions(server).get(sid)), "position not registered"

    _mgr_post(server, "/api/manage/gt06/users", {"users": {sid: {"name": "DefaultName"}}})
    assert _wait_for(lambda: _read_positions(server).get(sid, {}).get("name") == "DefaultName"), \
        "default name not applied"

    # a per-event users.json override must take precedence over the global default
    status, _ = _admin_post(server, f"/api/event/{EID}/admin/user/{sid}", {"name": "EventName"})
    assert status == 200
    assert _wait_for(lambda: _read_positions(server).get(sid, {}).get("name") == "EventName"), \
        "per-event override did not win over default"

    # clearing the default name (blank) drops it from the store
    status, _ = _mgr_post(server, "/api/manage/gt06/users", {"users": {sid: {"name": ""}}})
    assert status == 200
    saved = json.loads((server.data_dir / "html" / "gt06_users.json").read_text())["users"]
    assert sid not in saved


def test_save_subset_merges_not_replaces(server):
    """A save that posts only a subset (e.g. a search-filtered view) must MERGE,
    not replace — other trackers' defaults must survive."""
    def store():
        return json.loads((server.data_dir / "html" / "gt06_users.json").read_text())["users"]

    _mgr_post(server, "/api/manage/gt06/users", {"users": {"G900010": {"name": "First"}}})
    _mgr_post(server, "/api/manage/gt06/users", {"users": {"G900011": {"name": "Second"}}})
    s = store()
    assert s.get("G900010", {}).get("name") == "First", "subset save wiped an earlier default"
    assert s.get("G900011", {}).get("name") == "Second"

    # clearing one leaves the other intact
    _mgr_post(server, "/api/manage/gt06/users", {"users": {"G900010": {"name": ""}}})
    s = store()
    assert "G900010" not in s and s.get("G900011", {}).get("name") == "Second"
