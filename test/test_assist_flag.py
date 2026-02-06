"""Tests for assist flag set/cancel."""

import json
import time

from conftest import create_event, make_packet


def test_assist_in_positions_and_log(udp_client, http_client, server):
    """ast: true should appear in positions JSON and JSONL log."""
    eid = create_event(http_client, name="Assist Flag Test")
    pkt = make_packet(eid=eid, id="AST01", ast=True)
    udp_client.send_position(pkt)

    time.sleep(0.3)

    # Check positions JSON
    pos_file = server.data_dir / "html" / str(eid) / "current_positions.json"
    data = json.loads(pos_file.read_text())
    assert data["sailors"]["AST01"]["ast"] is True

    # Check JSONL log
    event_dir = server.data_dir / "html" / str(eid) / "logs"
    entries = []
    for jf in event_dir.glob("*.jsonl"):
        for line in jf.read_text().strip().split("\n"):
            if line:
                entries.append(json.loads(line))

    matching = [e for e in entries if e.get("id") == "AST01"]
    assert len(matching) >= 1
    assert matching[0]["ast"] is True


def test_cancel_assist_delivered(udp_client, http_client):
    """Cancel-assist command should be delivered in ACK."""
    eid = create_event(http_client, name="Cancel Assist Flag", admin_password="astadmin")
    pkt1 = make_packet(eid=eid, id="AST02", ast=True)
    udp_client.send_position(pkt1)

    # Cancel assist
    http_client.post(
        f"/api/event/{eid}/admin/cancel-assist/AST02",
        headers={"X-Admin-Password": "astadmin"},
    )

    # Next ACK should have cancel_assist
    pkt2 = make_packet(eid=eid, id="AST02")
    ack = udp_client.send_position(pkt2)
    assert ack.get("cmd") == "cancel_assist"


def test_assist_disabled_event(udp_client, http_client, server):
    """With assist disabled on event, ast should be cleared."""
    eid = create_event(http_client, name="Assist Disabled")
    # Disable assist
    http_client.patch(
        f"/api/manage/event/{eid}",
        data={"assist_enabled": False},
        headers={"X-Manager-Password": "testmanager"},
    )

    pkt = make_packet(eid=eid, id="AST03", ast=True)
    ack = udp_client.send_position(pkt)
    # ACK should indicate assist is not enabled
    assert ack.get("assist") is False

    time.sleep(0.3)

    # Position should have ast=False since assist is disabled
    pos_file = server.data_dir / "html" / str(eid) / "current_positions.json"
    data = json.loads(pos_file.read_text())
    assert data["sailors"]["AST03"]["ast"] is False
