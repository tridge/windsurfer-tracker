"""Behaviour tests for the GT06 device simulator + listener interaction.

These tests drive the real GT06Listener via the session-scoped server
fixture (conftest.py:server), using GT06DeviceSim instances that open
real TCP connections. Each test pins down a specific protocol/behaviour
property — most mirror bugs we fixed this week against real hardware,
so a future regression would re-fail the same way.

Tests use short wall-clock sleeps. The VirtualClock plumbing exists for
follow-up tests that need to fast-forward MODE5 wake cycles without
burning real time.
"""

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from gt06_device_sim import GT06DeviceSim  # noqa: E402

from conftest import HTTPClient  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EID = 1
ADMIN_PW = "admin123"


def _http(server):
    return HTTPClient(f"http://{server.host}:{server.port}")


def _admin_post(server, path, body=None):
    return _http(server).post(path, data=body,
                              headers={"X-Admin-Password": ADMIN_PW,
                                       "Content-Type": "application/json"})


def _admin_get(server, path):
    return _http(server).get(path, headers={"X-Admin-Password": ADMIN_PW})


def _read_positions(server, eid=EID):
    pos_file = server.data_dir / "html" / str(eid) / "current_positions.json"
    if not pos_file.exists():
        return {}
    data = json.loads(pos_file.read_text())
    return data.get("sailors", data)


def _wait_for(condition, timeout=4.0, interval=0.1):
    """Poll a condition until it returns truthy. Returns last value or None."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = condition()
        if last:
            return last
        time.sleep(interval)
    return last


def _wait_for_login(server, sailor_id, timeout=4.0):
    """Block until the sailor shows up in current_positions.json."""
    pos = _wait_for(lambda: _read_positions(server).get(sailor_id), timeout)
    if pos is None:
        pytest.fail(f"{sailor_id} never appeared in current_positions.json")
    return pos


def _read_log_tail(server, n_lines=300):
    try:
        text = server.log_file.read_text()
    except FileNotFoundError:
        return ""
    return "\n".join(text.splitlines()[-n_lines:])


def _log_lines_for(server, sailor_id, substr=None, since_marker=None):
    """Return log lines mentioning sailor_id (optionally also containing substr).

    since_marker: if provided, only return lines AFTER the line containing this
    marker (used to scope per-test, since the server log is session-scoped).
    """
    try:
        full = server.log_file.read_text()
    except FileNotFoundError:
        return []
    lines = full.splitlines()
    if since_marker:
        for i in range(len(lines) - 1, -1, -1):
            if since_marker in lines[i]:
                lines = lines[i + 1:]
                break
    matches = [ln for ln in lines if sailor_id in ln]
    if substr:
        matches = [ln for ln in matches if substr in ln]
    return matches


def _log_marker(server, tag):
    """Write a marker line to the server's log via a no-op HTTP call so we can
    bracket per-test slices of the session-scoped log."""
    _http(server).get(f"/api/event/{EID}/status")
    # The most recent HTTP log line is our marker.
    try:
        lines = server.log_file.read_text().splitlines()
    except FileNotFoundError:
        return None
    for ln in reversed(lines):
        if "[HTTP]" in ln and f"/api/event/{EID}/status" in ln:
            return ln
    return None


@pytest.fixture
def gt06_sim_factory(server):
    """Yields a function that spawns + cleans up sims."""
    sims = []

    def factory(imei, **kwargs):
        sim = GT06DeviceSim(imei=imei,
                            host=server.host, port=server.gt06_port,
                            **kwargs)
        sim.start()
        sims.append(sim)
        return sim

    yield factory

    for s in sims:
        s.stop()


@pytest.fixture(autouse=True)
def _reset_state(server):
    _admin_post(server, f"/api/event/{EID}/admin/state", body={"state": "idle"})
    yield


# ---------------------------------------------------------------------------
# Smoke
# ---------------------------------------------------------------------------

def test_sim_login_registers_device(gt06_sim_factory, server):
    """A sim connecting + sending login + first LOC should appear in
    current_positions.json with did=imei."""
    sim = gt06_sim_factory("999010000001001")
    pos = _wait_for_login(server, sim.sailor_id)
    assert pos.get("did") == sim.imei


def test_sim_loc_lands_when_tracking(gt06_sim_factory, server):
    """When the event is in tracking state, sim LOC frames should produce
    a real lat/lon in current_positions.json (not just an idle stub)."""
    # Start in tracking so the first LOC isn't idle-suppressed.
    _admin_post(server, f"/api/event/{EID}/admin/state", body={"state": "tracking"})
    sim = gt06_sim_factory("999010000002001", freq=1)
    _wait_for_login(server, sim.sailor_id)
    time.sleep(1.5)
    pos = _read_positions(server).get(sim.sailor_id, {})
    assert pos.get("lat") is not None, f"no lat after LOC; pos={pos}"


# ---------------------------------------------------------------------------
# This-week's-fix regressions
# ---------------------------------------------------------------------------

def test_v667_course_status_zero_accepted(gt06_sim_factory, server):
    """V667 quirk: LOC with course_status=0x0000 must still register a position.

    Server soft-accept path in protocol_GT06.py — without it, V667 trackers
    would never show on the map. See project_v667_firmware_quirks.md item 1.
    """
    _admin_post(server, f"/api/event/{EID}/admin/state", body={"state": "tracking"})
    sim = gt06_sim_factory("999010000003001",
                           quirks={"course_status_zero": True},
                           freq=1)
    _wait_for_login(server, sim.sailor_id)
    time.sleep(1.5)
    pos = _read_positions(server).get(sim.sailor_id, {})
    assert pos.get("lat") is not None, "course_status=0 LOC was not soft-accepted"


def test_cxzt_in_mode1_does_not_repush_mode1(gt06_sim_factory, server):
    """When the sim is in M:1 and server's desired_mode is also 1, an
    additional cxzt# probe must not trigger a redundant MODE1,30,300# push.
    (Originally codex-followup#P1a — the desired_mode field was added so
    the server doesn't fight modes it's already happy with.)
    """
    sim = gt06_sim_factory("999010000004001")
    _wait_for_login(server, sim.sailor_id)
    # Wait for login burst to settle, then drop our marker.
    time.sleep(0.5)
    marker = _log_marker(server, "before-cxzt-probe")
    # Force a cxzt# via admin endpoint (probe path, no reconnect).
    _http(server).get(
        f"/api/event/{EID}/admin/gt06-cmd/{sim.sailor_id}?cmd=cxzt%23",
        headers={"X-Admin-Password": ADMIN_PW},
    )
    time.sleep(1.0)
    bad = _log_lines_for(server, sim.sailor_id, "pushing MODE1", since_marker=marker)
    assert not bad, f"unexpected MODE1 re-push: {bad}"


def test_storm_does_not_recur(gt06_sim_factory, server):
    """A sim already in MODE5 with the correct Freq should NOT receive
    repeated MODE5,15# pushes when the server probes it. (Issue #42 —
    the storm we fixed Wed.)
    """
    # Mark sailor SLEEP first, then push MODE5,15# to get sim into the right state.
    sim = gt06_sim_factory("999010000006001")
    _wait_for_login(server, sim.sailor_id)
    _admin_post(server, f"/api/event/{EID}/admin/sleep/{sim.sailor_id}")
    # Push MODE5,15# so sim mutates its own state to mode=5, freq=15.
    _http(server).get(
        f"/api/event/{EID}/admin/gt06-cmd/{sim.sailor_id}?cmd=MODE5%2C15%23",
        headers={"X-Admin-Password": ADMIN_PW},
    )
    assert _wait_for(lambda: sim.mode == 5 and sim.freq == 15, timeout=2.0), \
        f"sim never reached MODE5,15: mode={sim.mode} freq={sim.freq}"
    # Now drop a marker and hit the device with repeated cxzt# probes.
    time.sleep(0.3)
    marker = _log_marker(server, "before-storm-test")
    for _ in range(5):
        _http(server).get(
            f"/api/event/{EID}/admin/gt06-cmd/{sim.sailor_id}?cmd=cxzt%23",
            headers={"X-Admin-Password": ADMIN_PW},
        )
        time.sleep(0.3)
    pushes = _log_lines_for(server, sim.sailor_id, "pushing", since_marker=marker)
    pushes = [p for p in pushes if "MODE5" in p or "overnight" in p]
    assert not pushes, (
        f"unexpected re-push of MODE5/overnight setup ({len(pushes)}):\n  "
        + "\n  ".join(pushes))


def test_f540_recovery_triggers_overnight_repush(gt06_sim_factory, server):
    """A sim in MODE5 but with corrupted Freq:540 (the bug we hit Wed where
    TIMER,540,540# clobbered MODE5's F register) should trigger the server's
    F-recovery: cxzt# response with M:5 F:540 → server pushes _overnight_cmds
    to restore F:15.
    """
    sim = gt06_sim_factory("999010000005001")
    _wait_for_login(server, sim.sailor_id)
    _admin_post(server, f"/api/event/{EID}/admin/sleep/{sim.sailor_id}")
    # Get sim into MODE5 cleanly first.
    _http(server).get(
        f"/api/event/{EID}/admin/gt06-cmd/{sim.sailor_id}?cmd=MODE5%2C15%23",
        headers={"X-Admin-Password": ADMIN_PW},
    )
    assert _wait_for(lambda: sim.mode == 5, timeout=2.0)
    # Now corrupt the sim's freq to simulate the TIMER-clobber bug.
    sim.freq = 540
    # Probe cxzt# — server should detect M:5 F:540 and re-push overnight setup.
    _http(server).get(
        f"/api/event/{EID}/admin/gt06-cmd/{sim.sailor_id}?cmd=cxzt%23",
        headers={"X-Admin-Password": ADMIN_PW},
    )
    # Wait for the server's overnight push to propagate back to the sim.
    recovered = _wait_for(lambda: sim.freq == 15, timeout=3.0)
    assert recovered, (
        f"server failed to recover F: sim.freq={sim.freq}\n"
        + _read_log_tail(server, 30))
