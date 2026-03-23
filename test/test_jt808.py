"""Tests for JT808 GPS tracker protocol: registration, auth, location, heartbeat, idle mode, SOS, reconnection."""

import json
import struct
import time

from conftest import JT808Client, create_event


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


def _start_device(jt808_client, http_client, imei, sailor_id):
    """Login, send a location (to register), and admin-start the device."""
    jt808_client.send_login(imei)
    jt808_client.drain()
    jt808_client.send_location(minute=0, second=1)  # unique ts
    jt808_client.drain()
    time.sleep(0.2)
    http_client.post(
        f"/api/event/1/admin/start/{sailor_id}",
        headers={"X-Admin-Password": "admin123"},
    )
    jt808_client.drain()


# ---------------------------------------------------------------------------
# Registration & Auth
# ---------------------------------------------------------------------------

def test_registration_response(jt808_client):
    """Server should respond to registration with a 0x8100 frame."""
    frames = jt808_client.send_registration()
    assert len(frames) >= 1, "Expected at least a registration response"
    parsed = jt808_client._parse_frame(frames[0])
    assert parsed is not None
    msg_id = parsed[0]
    assert msg_id == 0x8100, f"Expected 0x8100, got 0x{msg_id:04X}"


def test_registration_returns_auth_code(jt808_client):
    """Registration response should contain a non-empty auth code."""
    jt808_client.send_registration()
    assert jt808_client.auth_code is not None
    assert len(jt808_client.auth_code) > 0


def test_authentication(jt808_client):
    """Server should ACK authentication with 0x8001 success."""
    jt808_client.send_registration()
    frames = jt808_client.send_auth()
    assert len(frames) >= 1
    parsed = jt808_client._parse_frame(frames[0])
    assert parsed is not None
    msg_id = parsed[0]
    assert msg_id == 0x8001, f"Expected 0x8001, got 0x{msg_id:04X}"
    # Check result byte
    body = parsed[3]
    assert len(body) >= 5
    result = body[4]
    assert result == 0, f"Expected success (0), got {result}"


def test_login_defaults_to_idle(jt808_client):
    """First-ever login should default to idle (interval=0 tracking control)."""
    frames = jt808_client.send_login()
    # Look for 0x8202 (tracking control) in responses
    found_tracking = False
    for f in frames:
        parsed = jt808_client._parse_frame(f)
        if parsed and parsed[0] == 0x8202:
            body = parsed[3]
            interval = struct.unpack(">H", body[0:2])[0]
            assert interval == 0, f"Expected idle interval 0, got {interval}"
            found_tracking = True
    assert found_tracking, "Expected 0x8202 tracking control after login"


def test_sailor_id_mapping(jt808_client, server):
    """IMEI should map to sailor_id = prefix + last 6 digits."""
    imei = "862831041694915"
    jt808_client.send_login(imei)
    jt808_client.drain()
    jt808_client.send_location()
    time.sleep(0.2)
    positions = _read_positions(server, eid=1)
    assert "J694915" in positions, f"Expected J694915 in positions, got: {list(positions.keys())}"


# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------

def test_location_in_positions(jt808_client, http_client, server):
    """Location packet should update current_positions.json with lat/lon."""
    imei = "862831041111111"
    _start_device(jt808_client, http_client, imei, "J111111")

    jt808_client.send_location(lat=-36.85, lon=174.76, speed_kmh_10=150, heading=90, second=10)
    time.sleep(0.2)

    positions = _read_positions(server, eid=1)
    sid = "J111111"
    assert sid in positions, f"Expected {sid} in positions"
    pos = positions[sid]
    assert abs(pos["lat"] - (-36.85)) < 0.01
    assert abs(pos["lon"] - 174.76) < 0.01


def test_speed_conversion(jt808_client, http_client, server):
    """Speed should be converted from 1/10 km/h to knots."""
    imei = "862831041222222"
    _start_device(jt808_client, http_client, imei, "J222222")

    # 180 = 18.0 km/h ~ 9.7 knots
    jt808_client.send_location(speed_kmh_10=180, second=10)
    time.sleep(0.2)

    positions = _read_positions(server, eid=1)
    pos = positions["J222222"]
    expected_knots = 18.0 / 1.852
    assert abs(pos["spd"] - expected_knots) < 0.2


def test_gmt8_time_conversion(jt808_client, http_client, server):
    """BCD time in GMT+8 should be converted to UTC unix timestamp."""
    imei = "862831041333333"
    _start_device(jt808_client, http_client, imei, "J333333")

    # Send time 2026-02-21 12:00:00 GMT+8 = 2026-02-21 04:00:00 UTC
    jt808_client.send_location(year=26, month=2, day=21, hour=12, minute=0, second=0)
    time.sleep(0.2)

    positions = _read_positions(server, eid=1)
    pos = positions["J333333"]
    # Expected UTC: 2026-02-21 04:00:00
    from calendar import timegm
    expected_ts = timegm((2026, 2, 21, 4, 0, 0))
    assert abs(pos["ts"] - expected_ts) < 2, f"Expected ts ~{expected_ts}, got {pos['ts']}"


def test_invalid_gps_ignored(jt808_client, server):
    """Location with gps_valid=False should not update positions."""
    imei = "862831041444444"
    jt808_client.send_login(imei)
    jt808_client.drain()

    jt808_client.send_location(gps_valid=False)
    time.sleep(0.2)

    positions = _read_positions(server, eid=1)
    assert "J444444" not in positions


# ---------------------------------------------------------------------------
# Batch location (0x0704)
# ---------------------------------------------------------------------------

def test_batch_location(jt808_client, http_client, server):
    """Batch location upload (0x0704) should process multiple positions."""
    imei = "862831041455555"
    _start_device(jt808_client, http_client, imei, "J455555")

    # Send 3 positions in a batch
    locations = [
        {"lat": -36.80, "lon": 174.70, "speed_kmh_10": 100, "heading": 90, "second": 10},
        {"lat": -36.81, "lon": 174.71, "speed_kmh_10": 120, "heading": 95, "second": 20},
        {"lat": -36.82, "lon": 174.72, "speed_kmh_10": 80, "heading": 100, "second": 30},
    ]
    jt808_client.send_batch_location(locations)
    time.sleep(0.2)

    positions = _read_positions(server, eid=1)
    pos = positions["J455555"]
    # Should have the last position from the batch
    assert abs(pos["lat"] - (-36.82)) < 0.01
    assert abs(pos["lon"] - 174.72) < 0.01


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

def test_heartbeat_ack(jt808_client):
    """Server should ACK heartbeat packets with 0x8001."""
    jt808_client.send_login()
    jt808_client.drain()

    frames = jt808_client.send_heartbeat()
    assert len(frames) >= 1
    parsed = jt808_client._parse_frame(frames[0])
    assert parsed is not None
    assert parsed[0] == 0x8001


# ---------------------------------------------------------------------------
# Idle / Active
# ---------------------------------------------------------------------------

def test_stop_sets_idle(jt808_client, http_client, server):
    """Admin stop should send idle tracking control (interval=0) to JT808 device."""
    imei = "862831041555555"
    _start_device(jt808_client, http_client, imei, "J555555")

    status, body = http_client.post(
        "/api/event/1/admin/stop/J555555",
        headers={"X-Admin-Password": "admin123"},
    )
    assert status == 200

    frames = jt808_client._recv_frames(timeout=0.5)
    found = False
    for f in frames:
        parsed = jt808_client._parse_frame(f)
        if parsed and parsed[0] == 0x8202:
            interval = struct.unpack(">H", parsed[3][0:2])[0]
            assert interval == 0, f"Expected idle interval 0, got {interval}"
            found = True
    assert found, f"Expected 0x8202 tracking control, got: {[jt808_client._parse_frame(f)[0] if jt808_client._parse_frame(f) else None for f in frames]}"


def test_start_sets_active(jt808_client, http_client, server):
    """Admin start should send active tracking control (10s) to JT808 device."""
    imei = "862831041666666"
    jt808_client.send_login(imei)
    jt808_client.drain()
    jt808_client.send_location()
    jt808_client.drain()
    time.sleep(0.2)

    status, body = http_client.post(
        "/api/event/1/admin/start/J666666",
        headers={"X-Admin-Password": "admin123"},
    )
    assert status == 200

    frames = jt808_client._recv_frames(timeout=0.5)
    found = False
    for f in frames:
        parsed = jt808_client._parse_frame(f)
        if parsed and parsed[0] == 0x8202:
            interval = struct.unpack(">H", parsed[3][0:2])[0]
            assert interval == 10, f"Expected active interval 10, got {interval}"
            found = True
    assert found, "Expected 0x8202 tracking control"


def test_idle_shows_stopped(jt808_client, server):
    """When idle, location packets should show stopped=True and idle=True."""
    imei = "862831041777777"
    jt808_client.send_login(imei)  # defaults to idle
    jt808_client.drain()

    jt808_client.send_location(lat=-36.84, lon=174.77)
    time.sleep(0.2)

    positions = _read_positions(server, eid=1)
    pos = positions["J777777"]
    assert pos.get("stopped") is True
    assert pos.get("idle") is True


# ---------------------------------------------------------------------------
# SOS / Assist
# ---------------------------------------------------------------------------

def test_sos_sets_assist(jt808_client, server):
    """SOS alarm (bit 0) should set assist flag in positions."""
    imei = "862831041800001"
    jt808_client.send_login(imei)
    jt808_client.drain()

    jt808_client.send_location(alarm_flags=1, lat=-36.85, lon=174.76)
    time.sleep(0.2)

    positions = _read_positions(server, eid=1)
    pos = positions["J800001"]
    assert pos.get("ast") is True


def test_sos_exits_idle(jt808_client, server):
    """SOS while idle should transition to active tracking."""
    imei = "862831041800002"
    jt808_client.send_login(imei)  # starts idle
    jt808_client.drain()
    jt808_client.send_location(second=1)
    jt808_client.drain()

    # Send SOS — should exit idle and send active tracking control
    frames = jt808_client.send_location(alarm_flags=1, lat=-36.85, lon=174.76, second=2)
    # Also get any additional frames sent
    extra = jt808_client._recv_frames(timeout=0.5)
    all_frames = frames + extra

    found_active = False
    for f in all_frames:
        parsed = jt808_client._parse_frame(f)
        if parsed and parsed[0] == 0x8202:
            interval = struct.unpack(">H", parsed[3][0:2])[0]
            if interval == 10:
                found_active = True
    assert found_active, "Expected active 0x8202 after SOS"


def test_sos_sticky(jt808_client, server):
    """Second SOS should keep assist active (sticky, not toggled off)."""
    imei = "862831041800003"
    jt808_client.send_login(imei)
    jt808_client.drain()

    jt808_client.send_location(alarm_flags=1, lat=-36.85, lon=174.76, second=10)
    jt808_client.drain()
    time.sleep(0.2)

    positions = _read_positions(server, eid=1)
    assert positions["J800003"].get("ast") is True

    # Second SOS — should still be active
    jt808_client.send_location(alarm_flags=1, lat=-36.85, lon=174.76, second=20)
    jt808_client.drain()
    time.sleep(0.2)

    positions = _read_positions(server, eid=1)
    assert positions["J800003"].get("ast") is True


def test_cancel_assist_via_admin(jt808_client, http_client, server):
    """Admin cancel-assist should clear assist flag."""
    imei = "862831041800004"
    jt808_client.send_login(imei)
    jt808_client.drain()

    jt808_client.send_location(alarm_flags=1, lat=-36.85, lon=174.76, second=10)
    jt808_client.drain()
    time.sleep(0.2)

    positions = _read_positions(server, eid=1)
    assert positions["J800004"].get("ast") is True

    # Admin cancels
    status, body = http_client.post(
        "/api/event/1/admin/cancel-assist/J800004",
        headers={"X-Admin-Password": "admin123"},
    )
    assert status == 200
    jt808_client.drain()

    # Send normal location to update
    jt808_client.send_location(second=20)
    time.sleep(0.2)

    positions = _read_positions(server, eid=1)
    assert positions["J800004"].get("ast") is not True


def test_sos_survives_reconnect(jt808_client, server):
    """Sticky SOS should be restored after TCP reconnect."""
    imei = "862831041800005"
    jt808_client.send_login(imei)
    jt808_client.drain()

    jt808_client.send_location(alarm_flags=1, lat=-36.85, lon=174.76, second=10)
    jt808_client.drain()
    time.sleep(0.2)

    positions = _read_positions(server, eid=1)
    assert positions["J800005"].get("ast") is True

    # Disconnect and reconnect
    jt808_client.close()
    jt808_client.connect()

    jt808_client.send_login(imei)
    jt808_client.drain()

    jt808_client.send_location(second=20)
    time.sleep(0.2)

    positions = _read_positions(server, eid=1)
    assert positions["J800005"].get("ast") is True


# ---------------------------------------------------------------------------
# Reconnection
# ---------------------------------------------------------------------------

def test_reconnect_stays_idle(jt808_client, server):
    """Device that reconnects while idle should remain idle."""
    imei = "862831041900001"
    jt808_client.send_login(imei)
    jt808_client.drain()

    # Disconnect and reconnect
    jt808_client.close()
    jt808_client.connect()

    frames = jt808_client.send_login(imei)
    found_idle = False
    for f in frames:
        parsed = jt808_client._parse_frame(f)
        if parsed and parsed[0] == 0x8202:
            interval = struct.unpack(">H", parsed[3][0:2])[0]
            if interval == 0:
                found_idle = True
    assert found_idle, "Expected idle 0x8202 on reconnect"


def test_reconnect_stays_active(jt808_client, http_client, server):
    """Device that was started should remain active after reconnect."""
    imei = "862831041900002"
    _start_device(jt808_client, http_client, imei, "J900002")

    # Disconnect and reconnect
    jt808_client.close()
    jt808_client.connect()

    frames = jt808_client.send_login(imei)
    found_active = False
    for f in frames:
        parsed = jt808_client._parse_frame(f)
        if parsed and parsed[0] == 0x8202:
            interval = struct.unpack(">H", parsed[3][0:2])[0]
            if interval == 10:
                found_active = True
    assert found_active, "Expected active 0x8202 on reconnect"


# ---------------------------------------------------------------------------
# Multi-protocol (stop-all/start-all includes JT808)
# ---------------------------------------------------------------------------

def test_stop_all_includes_jt808(jt808_client, http_client, server):
    """stop-all should idle JT808 devices alongside phone trackers."""
    imei = "862831041950001"
    _start_device(jt808_client, http_client, imei, "J950001")

    jt808_client.send_location(second=10)
    jt808_client.drain()
    time.sleep(0.2)

    status, body = http_client.post(
        "/api/event/1/admin/stop-all",
        headers={"X-Admin-Password": "admin123"},
    )
    assert status == 200

    frames = jt808_client._recv_frames(timeout=0.5)
    found = False
    for f in frames:
        parsed = jt808_client._parse_frame(f)
        if parsed and parsed[0] == 0x8202:
            interval = struct.unpack(">H", parsed[3][0:2])[0]
            if interval == 0:
                found = True
    assert found, "Expected idle 0x8202 from stop-all"


def test_start_all_includes_jt808(jt808_client, http_client, server):
    """start-all should activate idle JT808 devices."""
    imei = "862831041950002"
    jt808_client.send_login(imei)
    jt808_client.drain()
    jt808_client.send_location()
    jt808_client.drain()
    time.sleep(0.2)

    status, body = http_client.post(
        "/api/event/1/admin/start-all",
        headers={"X-Admin-Password": "admin123"},
    )
    assert status == 200

    frames = jt808_client._recv_frames(timeout=0.5)
    found = False
    for f in frames:
        parsed = jt808_client._parse_frame(f)
        if parsed and parsed[0] == 0x8202:
            interval = struct.unpack(">H", parsed[3][0:2])[0]
            if interval == 10:
                found = True
    assert found, "Expected active 0x8202 from start-all"
