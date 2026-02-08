"""Tests for idle mode: heartbeat packets, start/shutdown commands, idle_interval in ACK."""

import json
import time
from pathlib import Path

from conftest import create_event, make_packet, make_idle_packet, MANAGER_PASSWORD


def _get_positions(http_client, eid):
    """Fetch current_positions.json for an event, returning sailors as a dict."""
    status, body = http_client.get(f"/{eid}/current_positions.json")
    assert status == 200
    return body["sailors"]  # dict of {id: pos_data}


def test_idle_packet_sets_flags(http_client, udp_client, server):
    """An idle packet should set stopped=True and idle=True in positions."""
    eid = create_event(http_client, name="Idle Flags Test")
    pkt = make_idle_packet(eid=eid, id="IDLE01")
    ack = udp_client.send_position(pkt)
    assert "ack" in ack

    sailors = _get_positions(http_client, eid)
    assert "IDLE01" in sailors
    assert sailors["IDLE01"]["stopped"] is True
    assert sailors["IDLE01"]["idle"] is True


def test_idle_preserves_previous_position(http_client, udp_client, server):
    """If a user tracked first then goes idle, lat/lon should be preserved."""
    eid = create_event(http_client, name="Idle Preserve Pos")

    # Send a normal position first
    pkt = make_packet(eid=eid, id="IDLEP01", lat=-36.85, lon=174.76)
    udp_client.send_position(pkt)

    # Now send idle
    idle_pkt = make_idle_packet(eid=eid, id="IDLEP01")
    udp_client.send_position(idle_pkt)

    sailors = _get_positions(http_client, eid)
    assert "IDLEP01" in sailors
    user = sailors["IDLEP01"]
    assert user["idle"] is True
    assert user["lat"] == -36.85
    assert user["lon"] == 174.76


def test_idle_no_position_no_latlng(http_client, udp_client, server):
    """If a user goes idle without ever tracking, there should be no lat/lon."""
    eid = create_event(http_client, name="Idle No Pos")
    pkt = make_idle_packet(eid=eid, id="IDLENP01")
    udp_client.send_position(pkt)

    sailors = _get_positions(http_client, eid)
    assert "IDLENP01" in sailors
    user = sailors["IDLENP01"]
    assert user["idle"] is True
    assert "lat" not in user
    assert "lon" not in user


def test_idle_not_logged(http_client, udp_client, server):
    """Idle packets should not create JSONL log entries."""
    eid = create_event(http_client, name="Idle No Log")

    # Send idle packet
    pkt = make_idle_packet(eid=eid, id="IDLENL01")
    udp_client.send_position(pkt)

    # Check log directory for this event
    log_dir = server.data_dir / "html" / str(eid) / "logs"
    # Either no log files, or existing log files don't contain this user
    for log_file in log_dir.glob("*.jsonl"):
        content = log_file.read_text()
        for line in content.strip().split("\n"):
            if line:
                entry = json.loads(line)
                assert entry.get("id") != "IDLENL01", "Idle packet should not be logged"


def test_start_all_queues_for_idle(http_client, udp_client, server):
    """start-all should queue start commands for all idle users."""
    eid = create_event(http_client, name="Start All Test")
    admin_pwd = "admin123"

    # Put 3 users in idle mode
    for i in range(3):
        pkt = make_idle_packet(eid=eid, id=f"SA{i:02d}")
        udp_client.send_position(pkt)

    # Call start-all
    status, body = http_client.post(
        f"/api/event/{eid}/admin/start-all",
        headers={"X-Admin-Password": admin_pwd}
    )
    assert status == 200
    assert body["started_count"] == 3

    # Send another packet from each and check for start command in ACK
    for i in range(3):
        pkt = make_idle_packet(eid=eid, id=f"SA{i:02d}")
        ack = udp_client.send_position(pkt)
        assert ack.get("cmd") == "start", f"SA{i:02d} should get start command"


def test_start_all_ignores_active(http_client, udp_client, server):
    """start-all should not queue commands for active (non-idle) users."""
    eid = create_event(http_client, name="Start All Active")
    admin_pwd = "admin123"

    # One active user
    pkt = make_packet(eid=eid, id="ACTIVE01")
    udp_client.send_position(pkt)

    # One idle user
    idle_pkt = make_idle_packet(eid=eid, id="IDLE01")
    udp_client.send_position(idle_pkt)

    # Call start-all
    status, body = http_client.post(
        f"/api/event/{eid}/admin/start-all",
        headers={"X-Admin-Password": admin_pwd}
    )
    assert status == 200
    assert body["started_count"] == 1
    assert "IDLE01" in body["user_ids"]
    assert "ACTIVE01" not in body["user_ids"]

    # Active user should NOT get start command
    pkt2 = make_packet(eid=eid, id="ACTIVE01")
    ack = udp_client.send_position(pkt2)
    assert "cmd" not in ack


def test_shutdown_all_queues_for_idle(http_client, udp_client, server):
    """shutdown-all should queue shutdown commands for all idle users."""
    eid = create_event(http_client, name="Shutdown All Test")
    admin_pwd = "admin123"

    # Put 2 users in idle mode
    for i in range(2):
        pkt = make_idle_packet(eid=eid, id=f"SD{i:02d}")
        udp_client.send_position(pkt)

    # Call shutdown-all
    status, body = http_client.post(
        f"/api/event/{eid}/admin/shutdown-all",
        headers={"X-Admin-Password": admin_pwd}
    )
    assert status == 200
    assert body["shutdown_count"] == 2

    # Verify each gets shutdown command
    for i in range(2):
        pkt = make_idle_packet(eid=eid, id=f"SD{i:02d}")
        ack = udp_client.send_position(pkt)
        assert ack.get("cmd") == "shutdown"


def test_start_single_user(http_client, udp_client, server):
    """Starting a single idle user should send start command only to that user."""
    eid = create_event(http_client, name="Start Single Test")
    admin_pwd = "admin123"

    # Put 2 users in idle
    for uid in ["SS01", "SS02"]:
        pkt = make_idle_packet(eid=eid, id=uid)
        udp_client.send_position(pkt)

    # Start only SS01
    status, body = http_client.post(
        f"/api/event/{eid}/admin/start/SS01",
        headers={"X-Admin-Password": admin_pwd}
    )
    assert status == 200

    # SS01 should get start command
    pkt = make_idle_packet(eid=eid, id="SS01")
    ack = udp_client.send_position(pkt)
    assert ack.get("cmd") == "start"

    # SS02 should NOT get start command
    pkt = make_idle_packet(eid=eid, id="SS02")
    ack = udp_client.send_position(pkt)
    assert "cmd" not in ack


def test_start_delivered_once(http_client, udp_client, server):
    """A start command should be consumed after first delivery."""
    eid = create_event(http_client, name="Start Once Test")
    admin_pwd = "admin123"

    pkt = make_idle_packet(eid=eid, id="ONCE01")
    udp_client.send_position(pkt)

    # Queue start
    http_client.post(
        f"/api/event/{eid}/admin/start/ONCE01",
        headers={"X-Admin-Password": admin_pwd}
    )

    # First packet gets the command
    pkt = make_idle_packet(eid=eid, id="ONCE01")
    ack = udp_client.send_position(pkt)
    assert ack.get("cmd") == "start"

    # Second packet should NOT get the command
    pkt = make_idle_packet(eid=eid, id="ONCE01")
    ack = udp_client.send_position(pkt)
    assert "cmd" not in ack


def test_start_requires_admin_auth(http_client, server):
    """start-all should require admin authentication."""
    eid = create_event(http_client, name="Start Auth Test")

    status, body = http_client.post(
        f"/api/event/{eid}/admin/start-all",
        headers={"X-Forwarded-For": "10.222.1.1"}
    )
    assert status == 401


def test_shutdown_requires_admin_auth(http_client, server):
    """shutdown-all should require admin authentication."""
    eid = create_event(http_client, name="Shutdown Auth Test")

    status, body = http_client.post(
        f"/api/event/{eid}/admin/shutdown-all",
        headers={"X-Forwarded-For": "10.222.1.2"}
    )
    assert status == 401


def test_idle_interval_in_ack(http_client, udp_client, server):
    """Event with idle_interval set should include 'idle' field in ACK."""
    eid = create_event(http_client, name="Idle Interval ACK")

    # Set idle_interval to 30
    status, _ = http_client.patch(
        f"/api/manage/event/{eid}",
        data={"idle_interval": 30},
        headers={"X-Manager-Password": MANAGER_PASSWORD}
    )
    assert status == 200

    # Send a normal packet and check ACK
    pkt = make_packet(eid=eid, id="IACK01")
    ack = udp_client.send_position(pkt)
    assert ack.get("idle") == 30


def test_idle_disabled_by_default(http_client, udp_client, server):
    """New event ACK should not have 'idle' field."""
    eid = create_event(http_client, name="Idle Default Test")

    pkt = make_packet(eid=eid, id="IDEF01")
    ack = udp_client.send_position(pkt)
    assert "idle" not in ack


def test_command_overwrite(http_client, udp_client, server):
    """Queueing stop then start for the same user should deliver only start."""
    eid = create_event(http_client, name="Cmd Overwrite Test")
    admin_pwd = "admin123"

    # Put user active first (so stop-all would target them)
    pkt = make_packet(eid=eid, id="OW01")
    udp_client.send_position(pkt)

    # Queue stop
    http_client.post(
        f"/api/event/{eid}/admin/stop/OW01",
        headers={"X-Admin-Password": admin_pwd}
    )

    # Queue start (should overwrite stop)
    http_client.post(
        f"/api/event/{eid}/admin/start/OW01",
        headers={"X-Admin-Password": admin_pwd}
    )

    # Should get start, not stop
    pkt = make_packet(eid=eid, id="OW01")
    ack = udp_client.send_position(pkt)
    assert ack.get("cmd") == "start"


def test_idle_via_http_post(http_client, server):
    """Idle packets via HTTP POST should also work."""
    eid = create_event(http_client, name="Idle HTTP Test")
    pkt = make_idle_packet(eid=eid, id="HTTPIDL01")

    status, ack = http_client.post("/api/tracker", data=pkt)
    assert status == 200
    assert "ack" in ack

    sailors = _get_positions(http_client, eid)
    assert "HTTPIDL01" in sailors
    user = sailors["HTTPIDL01"]
    assert user["idle"] is True
    assert user["stopped"] is True
