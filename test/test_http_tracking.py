"""Tests for HTTP POST /api/tracker tracking endpoint."""

import json
import time

from conftest import make_packet, create_event


def test_http_tracker_returns_ack(http_client):
    """POST /api/tracker should return an ACK JSON."""
    eid = create_event(http_client, name="HTTP Track ACK")
    pkt = make_packet(eid=eid, id="H01")
    status, body = http_client.post("/api/tracker", data=pkt)
    assert status == 200
    assert body["ack"] == pkt["sq"]


def test_http_position_logged(http_client, server):
    """Position sent via HTTP should be logged to JSONL."""
    eid = create_event(http_client, name="HTTP Log Test")
    pkt = make_packet(eid=eid, id="H02")
    http_client.post("/api/tracker", data=pkt)

    time.sleep(0.3)

    event_dir = server.data_dir / "html" / str(eid) / "logs"
    entries = []
    for jf in event_dir.glob("*.jsonl"):
        for line in jf.read_text().strip().split("\n"):
            if line:
                entries.append(json.loads(line))

    matching = [e for e in entries if e.get("id") == "H02"]
    assert len(matching) >= 1


def test_http_positions_json_updated(http_client, server):
    """Position sent via HTTP should update current_positions.json."""
    eid = create_event(http_client, name="HTTP Positions JSON")
    pkt = make_packet(eid=eid, id="H03")
    http_client.post("/api/tracker", data=pkt)

    time.sleep(0.3)

    pos_file = server.data_dir / "html" / str(eid) / "current_positions.json"
    assert pos_file.exists()
    data = json.loads(pos_file.read_text())
    assert "H03" in data["sailors"]


def test_http_tracker_password_correct(http_client):
    """With tracker password set, correct password should work."""
    eid = create_event(http_client, name="HTTP Pwd Test", tracker_password="secret123")
    pkt = make_packet(eid=eid, id="H04", pwd="secret123")
    status, body = http_client.post("/api/tracker", data=pkt)
    assert status == 200
    assert body["ack"] == pkt["sq"]


def test_http_tracker_password_wrong(http_client):
    """With tracker password set, wrong password should fail."""
    eid = create_event(http_client, name="HTTP Pwd Fail", tracker_password="secret456")
    pkt = make_packet(eid=eid, id="H05", pwd="wrongpwd")
    status, body = http_client.post("/api/tracker", data=pkt,
                                     headers={"X-Forwarded-For": "10.99.1.1"})
    assert status == 401
    assert body.get("error") == "auth"


def test_http_auth_check(http_client):
    """auth_check: true should validate password without logging position."""
    eid = create_event(http_client, name="HTTP Auth Check", tracker_password="checkme")
    pkt = make_packet(eid=eid, id="H06", pwd="checkme", auth_check=True)
    status, body = http_client.post("/api/tracker", data=pkt)
    assert status == 200
    assert body["ack"] == pkt["sq"]


def test_http_nonexistent_event(http_client):
    """POST to nonexistent event should return 404."""
    pkt = make_packet(eid=99998, id="H07")
    status, body = http_client.post("/api/tracker", data=pkt)
    assert status == 404
    assert body.get("error") == "event"


def test_http_archived_event(http_client):
    """POST to archived event should return 400."""
    eid = create_event(http_client, name="HTTP Archived Test")
    http_client.patch(
        f"/api/manage/event/{eid}",
        data={"archived": True},
        headers={"X-Manager-Password": "testmanager"},
    )
    pkt = make_packet(eid=eid, id="H08")
    status, body = http_client.post("/api/tracker", data=pkt)
    assert status == 400
    assert body.get("error") == "event"
