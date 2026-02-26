"""Tests for GT06 GPS tracker protocol: login, location, heartbeat, idle mode, SOS, reconnection."""

import json
import time

from conftest import GT06Client, create_event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_positions(server, eid):
    """Read current_positions.json for an event, returning the sailors dict."""
    pos_file = server.data_dir / "html" / str(eid) / "current_positions.json"
    if not pos_file.exists():
        return {}
    data = json.loads(pos_file.read_text())
    return data.get("sailors", data)


def _start_device(gt06_client, http_client, imei, sailor_id):
    """Login, send a location (to register), and admin-start the device."""
    gt06_client.send_login(imei)
    gt06_client.drain()
    gt06_client.send_location(minute=0, second=1)  # unique ts to avoid dedup with test body
    gt06_client.drain()
    time.sleep(0.2)
    http_client.post(
        f"/api/event/1/admin/start/{sailor_id}",
        headers={"X-Admin-Password": "admin123"},
    )
    gt06_client.drain()


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def test_login_response(gt06_client):
    """Server should respond to login with an ACK frame."""
    frames = gt06_client.send_login()
    assert len(frames) >= 1, "Expected at least a login ACK frame"
    protocol, data, serial = gt06_client._parse_frame(frames[0])
    assert protocol == 0x01


def test_login_sends_commands(gt06_client):
    """Login should trigger TIMER, SUP, and HBT commands (queued sequentially)."""
    frames = gt06_client.send_login()
    cmds = gt06_client.recv_all_queued_commands(initial_frames=frames)
    cmd_text = " ".join(cmds)
    assert "TIMER," in cmd_text, f"Expected TIMER command, got: {cmds}"
    assert "HBT," in cmd_text, f"Expected HBT command, got: {cmds}"


def test_login_defaults_to_idle(gt06_client):
    """First-ever login should default to idle (TIMER,60,60#)."""
    frames = gt06_client.send_login()
    cmds = gt06_client.recv_all_queued_commands(initial_frames=frames)
    assert "TIMER,60,60#" in cmds, f"Expected idle TIMER, got: {cmds}"
    assert "SUP,60#" in cmds, f"Expected idle SUP, got: {cmds}"


def test_login_sailor_id_mapping(gt06_client, server):
    """IMEI should map to sailor_id = prefix + last 6 digits."""
    imei = "863874081226122"
    gt06_client.send_login(imei)
    gt06_client.drain()
    gt06_client.send_location()
    time.sleep(0.2)
    positions = _read_positions(server, eid=1)
    assert "G226122" in positions, f"Expected G226122 in positions, got: {list(positions.keys())}"


# ---------------------------------------------------------------------------
# Location (active mode — admin-started so location goes through normal path)
# ---------------------------------------------------------------------------

def test_location_appears_in_positions(gt06_client, http_client, server):
    """Location packet should update current_positions.json with lat/lon."""
    imei = "863874081111111"
    _start_device(gt06_client, http_client, imei, "G111111")

    gt06_client.send_location(lat=-36.85, lon=174.76, speed_kmh=15, heading=90, second=10)
    time.sleep(0.2)

    positions = _read_positions(server, eid=1)
    sid = "G111111"
    assert sid in positions, f"Expected {sid} in positions"
    pos = positions[sid]
    assert abs(pos["lat"] - (-36.85)) < 0.01
    assert abs(pos["lon"] - 174.76) < 0.01


def test_location_speed_converted_to_knots(gt06_client, http_client, server):
    """Speed should be converted from km/h to knots."""
    imei = "863874081222222"
    _start_device(gt06_client, http_client, imei, "G222222")

    gt06_client.send_location(speed_kmh=18, second=10)  # 18 km/h ~ 9.7 knots
    time.sleep(0.2)

    positions = _read_positions(server, eid=1)
    pos = positions["G222222"]
    expected_knots = 18 / 1.852
    assert abs(pos["spd"] - expected_knots) < 0.2


def test_invalid_gps_ignored(gt06_client, server):
    """Location with gps_valid=False should not update positions."""
    imei = "863874081333333"
    gt06_client.send_login(imei)
    gt06_client.drain()

    gt06_client.send_location(gps_valid=False)
    time.sleep(0.2)

    positions = _read_positions(server, eid=1)
    assert "G333333" not in positions


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

def test_heartbeat_ack(gt06_client):
    """Server should ACK heartbeat packets."""
    gt06_client.send_login()
    gt06_client.drain()

    frames = gt06_client.send_heartbeat(battery_level=5, signal=3)
    assert len(frames) >= 1
    protocol, data, serial = gt06_client._parse_frame(frames[0])
    assert protocol == 0x13


def test_heartbeat_updates_battery_signal(gt06_client, server):
    """Heartbeat should update battery/signal in positions when idle."""
    imei = "863874081444444"
    gt06_client.send_login(imei)
    gt06_client.drain()

    # Send a location first to establish last known position
    gt06_client.send_location()
    gt06_client.drain()
    time.sleep(0.2)

    # Now send heartbeat (device is idle by default)
    gt06_client.send_heartbeat(battery_level=3, signal=2)
    time.sleep(0.2)

    positions = _read_positions(server, eid=1)
    pos = positions["G444444"]
    assert pos["bat"] == 30  # battery_level 3 maps to 30%
    assert pos["sig"] == 2


# ---------------------------------------------------------------------------
# Idle mode
# ---------------------------------------------------------------------------

def test_stop_sets_gt06_idle(gt06_client, http_client, server):
    """Admin stop should send idle commands (TIMER,60 + SUP,60) to GT06 device."""
    imei = "863874081555555"
    _start_device(gt06_client, http_client, imei, "G555555")

    # Issue stop
    status, body = http_client.post(
        "/api/event/1/admin/stop/G555555",
        headers={"X-Admin-Password": "admin123"},
    )
    assert status == 200

    cmds = gt06_client.recv_all_queued_commands()
    assert "TIMER,60,60#" in cmds, f"Expected idle TIMER, got: {cmds}"
    assert "SUP,60#" in cmds, f"Expected idle SUP, got: {cmds}"


def test_start_sets_gt06_active(gt06_client, http_client, server):
    """Admin start should send active commands (TIMER,10 + SUP,1) to GT06 device."""
    imei = "863874081666666"
    gt06_client.send_login(imei)
    gt06_client.drain()
    gt06_client.send_location()
    gt06_client.drain()
    time.sleep(0.2)

    # Device starts idle by default; start it
    status, body = http_client.post(
        "/api/event/1/admin/start/G666666",
        headers={"X-Admin-Password": "admin123"},
    )
    assert status == 200

    cmds = gt06_client.recv_all_queued_commands()
    assert "TIMER,10,10#" in cmds, f"Expected active TIMER, got: {cmds}"
    assert "SUP,1#" in cmds, f"Expected active SUP, got: {cmds}"


def test_idle_position_shows_stopped(gt06_client, server):
    """When idle, location packets should show stopped=True and idle=True."""
    imei = "863874081777777"
    gt06_client.send_login(imei)  # defaults to idle
    gt06_client.drain()

    gt06_client.send_location(lat=-36.84, lon=174.77)
    time.sleep(0.2)

    positions = _read_positions(server, eid=1)
    pos = positions["G777777"]
    assert pos.get("stopped") is True
    assert pos.get("idle") is True


def test_active_position_not_stopped(gt06_client, http_client, server):
    """After admin start, location packets should not be stopped/idle."""
    imei = "863874081888888"
    _start_device(gt06_client, http_client, imei, "G888888")

    # Send another location while active
    gt06_client.send_location(lat=-36.83, lon=174.78, second=10)
    time.sleep(0.2)

    positions = _read_positions(server, eid=1)
    pos = positions["G888888"]
    assert pos.get("stopped") is not True
    assert pos.get("idle") is not True


def test_stop_immediately_updates_positions(gt06_client, http_client, server):
    """Admin stop should immediately mark the device as idle in positions."""
    imei = "863874081999999"
    _start_device(gt06_client, http_client, imei, "G999999")

    gt06_client.send_location(second=10)
    gt06_client.drain()
    time.sleep(0.2)

    # Now stop — should immediately show idle
    http_client.post(
        "/api/event/1/admin/stop/G999999",
        headers={"X-Admin-Password": "admin123"},
    )
    gt06_client.drain()
    time.sleep(0.2)

    positions = _read_positions(server, eid=1)
    pos = positions["G999999"]
    assert pos.get("idle") is True
    assert pos.get("stopped") is True


# ---------------------------------------------------------------------------
# SOS / Assist
# ---------------------------------------------------------------------------

def test_sos_sets_assist(gt06_client, server):
    """SOS alarm should set assist flag in positions."""
    imei = "863874081100001"
    gt06_client.send_login(imei)
    gt06_client.drain()

    gt06_client.send_alarm(alarm_type="SOS", lat=-36.85, lon=174.76)
    time.sleep(0.2)

    positions = _read_positions(server, eid=1)
    pos = positions["G100001"]
    assert pos.get("ast") is True


def test_sos_exits_idle(gt06_client, server):
    """SOS while idle should transition to active tracking."""
    imei = "863874081100002"
    gt06_client.send_login(imei)  # starts idle
    gt06_client.drain()
    gt06_client.send_location()
    gt06_client.drain()

    # Send SOS — should exit idle and send active TIMER commands
    frames = gt06_client.send_alarm(alarm_type="SOS", lat=-36.85, lon=174.76)
    cmds = gt06_client.recv_all_queued_commands(initial_frames=frames)
    assert "TIMER,10,10#" in cmds, f"Expected active TIMER after SOS, got: {cmds}"
    assert "SUP,1#" in cmds, f"Expected active SUP after SOS, got: {cmds}"


def test_sos_toggle_off(gt06_client, server):
    """Second SOS press should cancel assist but stay active."""
    imei = "863874081100003"
    gt06_client.send_login(imei)
    gt06_client.drain()

    # First SOS — sets assist
    gt06_client.send_alarm(alarm_type="SOS", lat=-36.85, lon=174.76, second=10)
    gt06_client.drain()
    time.sleep(0.2)

    positions = _read_positions(server, eid=1)
    assert positions["G100003"].get("ast") is True

    # Second SOS — cancels assist (different timestamp to avoid dup detection)
    gt06_client.send_alarm(alarm_type="SOS", lat=-36.85, lon=174.76, second=20)
    gt06_client.drain()
    time.sleep(0.2)

    positions = _read_positions(server, eid=1)
    assert positions["G100003"].get("ast") is not True


def test_cancel_assist_via_admin(gt06_client, http_client, server):
    """Admin cancel-assist should clear assist flag."""
    imei = "863874081100004"
    gt06_client.send_login(imei)
    gt06_client.drain()

    # SOS exits idle, so device is now active
    gt06_client.send_alarm(alarm_type="SOS", lat=-36.85, lon=174.76, second=10)
    gt06_client.drain()
    time.sleep(0.2)

    positions = _read_positions(server, eid=1)
    assert positions["G100004"].get("ast") is True

    # Admin cancels assist
    status, body = http_client.post(
        "/api/event/1/admin/cancel-assist/G100004",
        headers={"X-Admin-Password": "admin123"},
    )
    assert status == 200
    gt06_client.drain()

    # Send a location with different timestamp to avoid dup detection
    gt06_client.send_location(second=20)
    time.sleep(0.2)

    positions = _read_positions(server, eid=1)
    assert positions["G100004"].get("ast") is not True


# ---------------------------------------------------------------------------
# Reconnection
# ---------------------------------------------------------------------------

def test_reconnect_stays_idle(gt06_client, server):
    """Device that reconnects while idle should remain idle."""
    imei = "863874081200001"
    gt06_client.send_login(imei)
    gt06_client.drain()

    # Disconnect and reconnect
    gt06_client.close()
    gt06_client.connect()

    frames = gt06_client.send_login(imei)
    cmds = gt06_client.recv_all_queued_commands(initial_frames=frames)
    assert "TIMER,60,60#" in cmds, f"Expected idle TIMER on reconnect, got: {cmds}"


def test_reconnect_stays_active(gt06_client, http_client, server):
    """Device that was started should remain active after reconnect."""
    imei = "863874081200002"
    _start_device(gt06_client, http_client, imei, "G200002")

    # Disconnect and reconnect
    gt06_client.close()
    gt06_client.connect()

    frames = gt06_client.send_login(imei)
    cmds = gt06_client.recv_all_queued_commands(initial_frames=frames)
    assert "TIMER,10,10#" in cmds, f"Expected active TIMER on reconnect, got: {cmds}"
    assert "SUP,1#" in cmds, f"Expected active SUP on reconnect, got: {cmds}"


# ---------------------------------------------------------------------------
# Stop-all / Start-all
# ---------------------------------------------------------------------------

def test_stop_all_includes_gt06(gt06_client, http_client, server):
    """stop-all should idle GT06 devices alongside phone trackers."""
    imei = "863874081300001"
    _start_device(gt06_client, http_client, imei, "G300001")

    gt06_client.send_location(second=10)
    gt06_client.drain()
    time.sleep(0.2)

    # Now stop-all
    status, body = http_client.post(
        "/api/event/1/admin/stop-all",
        headers={"X-Admin-Password": "admin123"},
    )
    assert status == 200

    cmds = gt06_client.recv_all_queued_commands()
    assert "TIMER,60,60#" in cmds, f"Expected idle TIMER from stop-all, got: {cmds}"


def test_start_all_includes_gt06(gt06_client, http_client, server):
    """start-all should activate idle GT06 devices."""
    imei = "863874081300002"
    gt06_client.send_login(imei)
    gt06_client.drain()
    gt06_client.send_location()
    gt06_client.drain()
    time.sleep(0.2)

    # Device starts idle; start-all should activate it
    status, body = http_client.post(
        "/api/event/1/admin/start-all",
        headers={"X-Admin-Password": "admin123"},
    )
    assert status == 200

    cmds = gt06_client.recv_all_queued_commands()
    assert "TIMER,10,10#" in cmds, f"Expected active TIMER from start-all, got: {cmds}"


# ---------------------------------------------------------------------------
# Non-SOS alarms
# ---------------------------------------------------------------------------

def test_shock_alarm_does_not_set_assist(gt06_client, server):
    """Non-SOS alarm (shock) should not set assist flag."""
    imei = "863874081400001"
    gt06_client.send_login(imei)
    gt06_client.drain()

    gt06_client.send_alarm(alarm_type="Shock", lat=-36.85, lon=174.76)
    time.sleep(0.2)

    positions = _read_positions(server, eid=1)
    pos = positions["G400001"]
    assert pos.get("ast") is not True


# ---------------------------------------------------------------------------
# Heartbeat idle updates
# ---------------------------------------------------------------------------

def test_heartbeat_idle_sends_position(gt06_client, server):
    """Heartbeat while idle should update positions with battery/signal."""
    imei = "863874081500001"
    gt06_client.send_login(imei)  # idle by default
    gt06_client.drain()

    # Send location to establish last known position
    gt06_client.send_location(lat=-36.86, lon=174.75)
    gt06_client.drain()
    time.sleep(0.2)

    # Send heartbeat with different battery
    gt06_client.send_heartbeat(battery_level=2, signal=1)
    time.sleep(0.2)

    positions = _read_positions(server, eid=1)
    pos = positions["G500001"]
    assert pos["bat"] == 15  # level 2 maps to 15%
    assert pos["sig"] == 1
    assert pos.get("idle") is True
    assert pos.get("stopped") is True
