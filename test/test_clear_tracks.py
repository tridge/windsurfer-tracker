"""Tests for clearing tracks."""

import json
import time

from conftest import create_event, make_packet


def test_clear_tracks(udp_client, http_client, server):
    """Clear tracks should reset positions and rotate JSONL."""
    eid = create_event(http_client, name="Clear Test", admin_password="clearadmin")

    # Send a position
    pkt = make_packet(eid=eid, id="CLR01")
    udp_client.send_position(pkt)
    time.sleep(0.3)

    # Verify position exists
    pos_file = server.data_dir / "html" / str(eid) / "current_positions.json"
    data = json.loads(pos_file.read_text())
    assert "CLR01" in data["sailors"]

    # Clear tracks
    status, body = http_client.post(
        f"/api/event/{eid}/admin/clear-tracks",
        headers={"X-Admin-Password": "clearadmin"},
    )
    assert status == 200
    assert body["success"] is True

    # Positions should be cleared
    data = json.loads(pos_file.read_text())
    assert len(data["sailors"]) == 0

    # Old JSONL should be rotated to .1
    log_dir = server.data_dir / "html" / str(eid) / "logs"
    rotated = list(log_dir.glob("*.jsonl.1"))
    assert len(rotated) >= 1


def test_double_clear(udp_client, http_client, server):
    """Double clear should create .1 and .2 rotated files."""
    eid = create_event(http_client, name="Double Clear", admin_password="dblclear")

    # First round: send position, clear
    pkt = make_packet(eid=eid, id="DC01")
    udp_client.send_position(pkt)
    time.sleep(0.2)
    http_client.post(
        f"/api/event/{eid}/admin/clear-tracks",
        headers={"X-Admin-Password": "dblclear"},
    )

    # Second round: send position, clear again
    pkt2 = make_packet(eid=eid, id="DC02")
    udp_client.send_position(pkt2)
    time.sleep(0.2)
    http_client.post(
        f"/api/event/{eid}/admin/clear-tracks",
        headers={"X-Admin-Password": "dblclear"},
    )

    log_dir = server.data_dir / "html" / str(eid) / "logs"
    rotated_1 = list(log_dir.glob("*.jsonl.1"))
    rotated_2 = list(log_dir.glob("*.jsonl.2"))
    assert len(rotated_1) >= 1
    assert len(rotated_2) >= 1


def test_clear_requires_admin_auth(http_client):
    """Clear tracks should require admin auth."""
    eid = create_event(http_client, name="Clear Auth", admin_password="clearauth")
    status, _ = http_client.post(
        f"/api/event/{eid}/admin/clear-tracks",
        headers={"X-Admin-Password": "wrong", "X-Forwarded-For": "10.9.1.1"},
    )
    assert status == 401


def test_new_positions_after_clear(udp_client, http_client, server):
    """New positions should appear after clear."""
    eid = create_event(http_client, name="After Clear", admin_password="afterclear")

    # Send, clear, send again
    pkt1 = make_packet(eid=eid, id="AC01")
    udp_client.send_position(pkt1)
    time.sleep(0.2)

    http_client.post(
        f"/api/event/{eid}/admin/clear-tracks",
        headers={"X-Admin-Password": "afterclear"},
    )

    pkt2 = make_packet(eid=eid, id="AC02")
    udp_client.send_position(pkt2)
    time.sleep(0.3)

    pos_file = server.data_dir / "html" / str(eid) / "current_positions.json"
    data = json.loads(pos_file.read_text())
    assert "AC02" in data["sailors"]
    # AC01 should not be present (was cleared)
    assert "AC01" not in data["sailors"]
