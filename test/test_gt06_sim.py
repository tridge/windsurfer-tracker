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
from protocol_GT06 import OVERNIGHT_FREQ_MAX_RETRIES  # noqa: E402

from conftest import HTTPClient, MANAGER_PASSWORD  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EID = 1
ADMIN_PW = "admin123"

# Server's default overnight config (matches gt06_config defaults in
# protocol_GT06.py — MODE4 with 15 min wake cycle = 900 s on the wire).
# Tests use these so they don't break when the default flips again.
OVERNIGHT_MODE = 4
OVERNIGHT_INTERVAL_MIN = 15
OVERNIGHT_F = OVERNIGHT_INTERVAL_MIN * 60 if OVERNIGHT_MODE == 4 else OVERNIGHT_INTERVAL_MIN


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
    """A sim already in the configured overnight MODE with the correct Freq
    should NOT receive repeated MODE pushes when the server probes it.
    (Issue #42 — the storm we fixed Wed; same property holds for MODE4 and
    MODE5 since the cxzt# enforcement path is mode-agnostic now.)
    """
    sim = gt06_sim_factory("999010000006001")
    _wait_for_login(server, sim.sailor_id)
    _admin_post(server, f"/api/event/{EID}/admin/sleep/{sim.sailor_id}")
    # First cxzt# triggers the mode-mismatch push (sim defaults to MODE1).
    # Wait for the sim to settle into the server's overnight mode + freq.
    _http(server).get(
        f"/api/event/{EID}/admin/gt06-cmd/{sim.sailor_id}?cmd=cxzt%23",
        headers={"X-Admin-Password": ADMIN_PW},
    )
    assert _wait_for(
        lambda: sim.mode == OVERNIGHT_MODE and sim.freq == OVERNIGHT_F,
        timeout=3.0), (
        f"sim never reached overnight state: mode={sim.mode} freq={sim.freq}, "
        f"expected mode={OVERNIGHT_MODE} freq={OVERNIGHT_F}")
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
    # Filter to overnight-setup pushes (MODE4 / MODE5 / overnight chain).
    pushes = [p for p in pushes if "MODE" in p or "overnight" in p]
    assert not pushes, (
        f"unexpected re-push of overnight setup ({len(pushes)}):\n  "
        + "\n  ".join(pushes))


def test_mode_command_round_trip(gt06_sim_factory, server):
    """Sim must accept both MODE4 and MODE5 commands and ACK with the right
    on-wire format. Locks in the dual-mode handler in handle_server_cmd —
    the swap to MODE4 default would have silently passed if MODE5 was
    accidentally dropped, since most tests fire MODE4 by default now.
    """
    sim = gt06_sim_factory("999010000007001")
    _wait_for_login(server, sim.sailor_id)
    # MODE4,300# — sim should set mode=4, freq=300, ACK with MODE4 OK.
    _http(server).get(
        f"/api/event/{EID}/admin/gt06-cmd/{sim.sailor_id}?cmd=MODE4%2C300%23",
        headers={"X-Admin-Password": ADMIN_PW},
    )
    assert _wait_for(lambda: sim.mode == 4 and sim.freq == 300, timeout=2.0), \
        f"MODE4,300# not accepted: mode={sim.mode} freq={sim.freq}"
    # MODE5,30# — switch the sim to MODE5 to confirm both branches work.
    _http(server).get(
        f"/api/event/{EID}/admin/gt06-cmd/{sim.sailor_id}?cmd=MODE5%2C30%23",
        headers={"X-Admin-Password": ADMIN_PW},
    )
    assert _wait_for(lambda: sim.mode == 5 and sim.freq == 30, timeout=2.0), \
        f"MODE5,30# not accepted: mode={sim.mode} freq={sim.freq}"


def test_overnight_setup_uses_configured_mode(gt06_sim_factory, server):
    """When the server pushes the overnight chain to a sim in MODE1, the
    sim should end up in the server's configured overnight_mode_number
    (currently 4) with freq matching overnight_interval_min in the chosen
    mode's units."""
    sim = gt06_sim_factory("999010000008001")
    _wait_for_login(server, sim.sailor_id)
    _admin_post(server, f"/api/event/{EID}/admin/sleep/{sim.sailor_id}")
    # First cxzt# triggers the mode-mismatch handler, which pushes the full
    # _overnight_cmds chain (SLPDISCONNECT + ACCLINE + MODE{n},{arg}#).
    _http(server).get(
        f"/api/event/{EID}/admin/gt06-cmd/{sim.sailor_id}?cmd=cxzt%23",
        headers={"X-Admin-Password": ADMIN_PW},
    )
    settled = _wait_for(
        lambda: sim.mode == OVERNIGHT_MODE and sim.freq == OVERNIGHT_F,
        timeout=3.0)
    assert settled, (
        f"sim didn't settle into overnight: mode={sim.mode} freq={sim.freq}, "
        f"expected MODE{OVERNIGHT_MODE} F={OVERNIGHT_F}")
    # ACCLINE should have been set as part of the chain (vibration-wake off).
    assert sim.accline == 1, f"sim.accline={sim.accline}, expected 1"


def test_f540_recovery_triggers_overnight_repush(gt06_sim_factory, server):
    """A sim in the overnight MODE but with a corrupted Freq (the bug we hit
    Wed where TIMER,540,540# clobbered MODE5's F register) should trigger
    the server's F-recovery: cxzt# response with the right MODE but wrong
    Freq → server pushes _overnight_cmds to restore the expected Freq.
    """
    sim = gt06_sim_factory("999010000005001")
    _wait_for_login(server, sim.sailor_id)
    _admin_post(server, f"/api/event/{EID}/admin/sleep/{sim.sailor_id}")
    # First cxzt# pushes the mode-mismatch overnight chain → sim settles.
    _http(server).get(
        f"/api/event/{EID}/admin/gt06-cmd/{sim.sailor_id}?cmd=cxzt%23",
        headers={"X-Admin-Password": ADMIN_PW},
    )
    assert _wait_for(
        lambda: sim.mode == OVERNIGHT_MODE and sim.freq == OVERNIGHT_F,
        timeout=3.0), f"sim never reached overnight state: mode={sim.mode} freq={sim.freq}"
    # Now corrupt the sim's freq to simulate the TIMER-clobber bug.
    sim.freq = 540
    # Probe cxzt# — server should detect right MODE but wrong F and re-push.
    _http(server).get(
        f"/api/event/{EID}/admin/gt06-cmd/{sim.sailor_id}?cmd=cxzt%23",
        headers={"X-Admin-Password": ADMIN_PW},
    )
    # Wait for the server's overnight push to propagate back to the sim.
    recovered = _wait_for(lambda: sim.freq == OVERNIGHT_F, timeout=3.0)
    assert recovered, (
        f"server failed to recover F: sim.freq={sim.freq}, expected {OVERNIGHT_F}\n"
        + _read_log_tail(server, 30))


def test_w07_firmware_overnight_uses_mode1_not_mode4(gt06_sim_factory, server):
    """W07/V6.6x firmware ignores the MODE4 Freq arg, so the server must keep
    those units on MODE1 (long TIMER) overnight instead of MODE4. Relies on
    firmware_overrides {"W07_": {"overnight_mode_number": 1}} in the test config.
    """
    sim = gt06_sim_factory("999010000009001",
                           firmware="W07_MG133_10F8G_B53_V6.68",
                           mode=4, freq=120)
    _wait_for_login(server, sim.sailor_id)
    marker = _log_marker(server, "w07mode1")
    _admin_post(server, f"/api/event/{EID}/admin/sleep/{sim.sailor_id}")
    for _ in range(4):
        _http(server).get(
            f"/api/event/{EID}/admin/gt06-cmd/{sim.sailor_id}?cmd=cxzt%23",
            headers={"X-Admin-Password": ADMIN_PW})
        time.sleep(0.2)
    switched = _wait_for(lambda: sim.mode == 1, timeout=4.0)
    assert switched, (f"W07 sim still in MODE{sim.mode} freq={sim.freq} fw={sim.firmware}\n"
                      + "\n".join(_log_lines_for(server, sim.sailor_id)))
    lines = _log_lines_for(server, sim.sailor_id, since_marker=marker)
    # The MODE4 *command* is "MODE4,900#" (comma); "switching MODE4->1" and
    # "in MODE4" log text are fine — only an actual MODE4 push is a bug here.
    assert not any("MODE4," in l for l in lines), \
        f"server pushed MODE4 to W07 firmware: {[l for l in lines if 'MODE4,' in l]}"
    assert not any("re-pushing overnight setup" in l for l in lines), \
        f"W07 sim triggered an overnight re-push storm: {lines}"


def test_overnight_freq_storm_is_capped(gt06_sim_factory, server):
    """A device whose firmware refuses the overnight Freq (clamps MODE4 to 120,
    like real W07) must not storm: the server caps freq re-pushes at
    OVERNIGHT_FREQ_MAX_RETRIES then gives up."""
    sim = gt06_sim_factory("999010000009002",
                           quirks={"mode4_freq_clamp": 120})
    _wait_for_login(server, sim.sailor_id)
    marker = _log_marker(server, "stormcap")
    _admin_post(server, f"/api/event/{EID}/admin/sleep/{sim.sailor_id}")
    # Reaches MODE4 but Freq stays clamped at 120 (never OVERNIGHT_F).
    reached = _wait_for(
        lambda: sim.mode == OVERNIGHT_MODE and sim.freq == 120, timeout=4.0)
    assert reached, f"clamp sim mode={sim.mode} freq={sim.freq}"
    for _ in range(OVERNIGHT_FREQ_MAX_RETRIES + 4):
        _http(server).get(
            f"/api/event/{EID}/admin/gt06-cmd/{sim.sailor_id}?cmd=cxzt%23",
            headers={"X-Admin-Password": ADMIN_PW})
        time.sleep(0.2)
    lines = _log_lines_for(server, sim.sailor_id, since_marker=marker)
    repushes = [l for l in lines if "re-pushing overnight setup" in l]
    gave_up = [l for l in lines if "giving up after" in l]
    assert len(repushes) <= OVERNIGHT_FREQ_MAX_RETRIES, \
        f"re-pushed {len(repushes)}x (cap {OVERNIGHT_FREQ_MAX_RETRIES}): {repushes}"
    assert gave_up, "storm guard never gave up:\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# GT06 management page (manager-level device admin)
# ---------------------------------------------------------------------------

def _mgr_get(server, path):
    return _http(server).get(path, headers={"X-Manager-Password": MANAGER_PASSWORD})


def _mgr_post(server, path, body=None):
    return _http(server).post(
        path, body, headers={"X-Manager-Password": MANAGER_PASSWORD})


def test_gt06_manage_requires_manager_auth(server):
    # Use a unique IP for this intentional auth failure so the resulting
    # 5s rate-limit doesn't block the shared default IP that other manager
    # tests (e.g. inventory) use right after.
    status, _ = _http(server).get(
        "/api/manage/gt06/trackers",
        headers={"X-Forwarded-For": "203.0.113.77"})  # no manager password
    assert status == 401


def test_gt06_manage_inventory_and_actions(gt06_sim_factory, server):
    """End-to-end through the real server: a connected device shows up in the
    inventory with firmware, and per-device config / refresh / reboot /
    firmware-update endpoints behave. HTTPClient returns (status, body)."""
    sim = gt06_sim_factory("999010000010001",
                           firmware="W07_MG133_10F8G_B53_V6.68")
    _wait_for_login(server, sim.sailor_id)
    _mgr_post(server, f"/api/manage/gt06/tracker/{sim.imei}/refresh")

    def _find():
        status, body = _mgr_get(server, "/api/manage/gt06/trackers")
        if status != 200:
            return None
        for t in body.get("trackers", []):
            if t["imei"] == sim.imei and t.get("firmware"):
                return t
        return None

    assert _wait_for(lambda: _find() is not None, timeout=4.0), \
        "device with firmware never appeared in manager inventory"
    entry = _find()
    assert entry["firmware"].startswith("W07_")

    # Per-device config write always works (persists to gt06.json) regardless of
    # connection state.
    status, body = _mgr_post(server, f"/api/manage/gt06/tracker/{sim.imei}",
                             {"overnight_mode_number": 1})
    assert status == 200 and body["success"] is True

    # Reboot needs a live connection. The sim may have cycled offline; accept
    # either a queued reboot (200) or a not-connected response (404).
    status, body = _mgr_post(server, f"/api/manage/gt06/tracker/{sim.imei}/reboot")
    assert status in (200, 404)

    status, _ = _mgr_post(server, f"/api/manage/gt06/tracker/{sim.imei}/firmware-update")
    assert status == 501

    status, _ = _mgr_post(server, "/api/manage/gt06/tracker/000000000000000/reboot")
    assert status == 404
