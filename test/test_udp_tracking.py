"""Tests for UDP position tracking."""

import json
import time
from pathlib import Path

from conftest import make_packet, create_event


def test_udp_send_and_ack(udp_client, http_client, server):
    """Send a position packet via UDP and verify ACK."""
    eid = create_event(http_client, name="UDP Track Test")
    pkt = make_packet(eid=eid, id="U01")
    ack = udp_client.send_position(pkt)
    assert ack["ack"] == pkt["sq"]
    assert "ts" in ack


def test_ack_contains_event_name(udp_client, http_client):
    """ACK should contain the event name."""
    eid = create_event(http_client, name="My Race Event")
    pkt = make_packet(eid=eid, id="U02")
    ack = udp_client.send_position(pkt)
    assert ack["event"] == "My Race Event"


def test_jsonl_log_created(udp_client, http_client, server):
    """Sending a position should create a JSONL log entry."""
    eid = create_event(http_client, name="JSONL Log Test")
    pkt = make_packet(eid=eid, id="U03")
    udp_client.send_position(pkt)

    # Give server a moment to write
    time.sleep(0.3)

    # Find the event data dir and check for JSONL files
    event_dir = server.data_dir / "html" / str(eid) / "logs"
    jsonl_files = list(event_dir.glob("*.jsonl"))
    assert len(jsonl_files) >= 1, f"No JSONL files found in {event_dir}"

    # Read the log and check entry
    entries = []
    for jf in jsonl_files:
        for line in jf.read_text().strip().split("\n"):
            if line:
                entries.append(json.loads(line))

    matching = [e for e in entries if e.get("id") == "U03"]
    assert len(matching) >= 1
    entry = matching[0]
    assert entry["lat"] == pkt["lat"]
    assert entry["lon"] == pkt["lon"]
    assert entry["spd"] == pkt["spd"]
    assert entry["hdg"] == pkt["hdg"]
    assert entry["role"] == "sailor"


def test_positions_json_updated(udp_client, http_client, server):
    """Sending a position should update current_positions.json."""
    eid = create_event(http_client, name="Positions JSON Test")
    pkt = make_packet(eid=eid, id="U04")
    udp_client.send_position(pkt)

    time.sleep(0.3)

    pos_file = server.data_dir / "html" / str(eid) / "current_positions.json"
    assert pos_file.exists()
    data = json.loads(pos_file.read_text())
    assert "U04" in data["sailors"]
    sailor = data["sailors"]["U04"]
    assert sailor["lat"] == pkt["lat"]
    assert sailor["lon"] == pkt["lon"]


def test_different_roles(udp_client, http_client, server):
    """Different roles (sailor, support, spectator) should all be logged."""
    eid = create_event(http_client, name="Roles Test")

    for role in ("sailor", "support", "spectator"):
        pkt = make_packet(eid=eid, id=f"R_{role}", role=role)
        ack = udp_client.send_position(pkt)
        assert ack["ack"] == pkt["sq"]

    time.sleep(0.3)

    pos_file = server.data_dir / "html" / str(eid) / "current_positions.json"
    data = json.loads(pos_file.read_text())
    assert data["sailors"]["R_sailor"]["role"] == "sailor"
    assert data["sailors"]["R_support"]["role"] == "support"
    assert data["sailors"]["R_spectator"]["role"] == "spectator"


def test_heart_rate_preserved(udp_client, http_client, server):
    """Heart rate field should be preserved in the log."""
    eid = create_event(http_client, name="HR Test")
    pkt = make_packet(eid=eid, id="U_HR", hr=142)
    udp_client.send_position(pkt)

    time.sleep(0.3)

    event_dir = server.data_dir / "html" / str(eid) / "logs"
    entries = []
    for jf in event_dir.glob("*.jsonl"):
        for line in jf.read_text().strip().split("\n"):
            if line:
                entries.append(json.loads(line))

    matching = [e for e in entries if e.get("id") == "U_HR"]
    assert len(matching) >= 1
    assert matching[0]["hr"] == 142


def test_invalid_json_no_crash(udp_client, http_client):
    """Invalid JSON packet should not crash the server."""
    resp = udp_client.send_raw(b"this is not json{{{")
    # Server should not respond to invalid JSON, so timeout is expected
    # Just verify the server is still alive afterward
    eid = create_event(http_client, name="Invalid JSON Recovery")
    pkt = make_packet(eid=eid, id="U_OK")
    ack = udp_client.send_position(pkt)
    assert ack["ack"] == pkt["sq"]


def test_nonexistent_event_error(udp_client):
    """Packet to nonexistent event should get an error ACK."""
    pkt = make_packet(eid=99999, id="U_NOEVENT")
    ack = udp_client.send_position(pkt)
    assert ack.get("error") == "event"


def test_archived_event_error(udp_client, http_client):
    """Packet to an archived event should get an error ACK."""
    eid = create_event(http_client, name="Archive Test Event")
    # Archive the event
    status, _ = http_client.patch(
        f"/api/manage/event/{eid}",
        data={"archived": True},
        headers={"X-Manager-Password": "testmanager"},
    )
    assert status == 200

    pkt = make_packet(eid=eid, id="U_ARCHIVED")
    ack = udp_client.send_position(pkt)
    assert ack.get("error") == "event"
