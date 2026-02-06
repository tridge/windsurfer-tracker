"""Tests for stop, stop-all, and cancel-assist commands."""

import time

from conftest import create_event, make_packet


def test_stop_command_delivered(udp_client, http_client):
    """POST stop for a user, next UDP ACK should have cmd=stop."""
    eid = create_event(http_client, name="Stop Test", admin_password="stopadmin")
    # Send a position first so user exists
    pkt1 = make_packet(eid=eid, id="STOP01")
    udp_client.send_position(pkt1)

    # Issue stop command
    status, body = http_client.post(
        f"/api/event/{eid}/admin/stop/STOP01",
        headers={"X-Admin-Password": "stopadmin"},
    )
    assert status == 200
    assert body["success"] is True

    # Next UDP packet should get stop command in ACK
    pkt2 = make_packet(eid=eid, id="STOP01")
    ack = udp_client.send_position(pkt2)
    assert ack.get("cmd") == "stop"


def test_stop_delivered_only_once(udp_client, http_client):
    """Stop command should be delivered only once (next ACK after stop has no cmd)."""
    eid = create_event(http_client, name="Stop Once Test", admin_password="stopadmin2")
    pkt1 = make_packet(eid=eid, id="STOP02")
    udp_client.send_position(pkt1)

    http_client.post(
        f"/api/event/{eid}/admin/stop/STOP02",
        headers={"X-Admin-Password": "stopadmin2"},
    )

    # First ACK after stop: has cmd
    pkt2 = make_packet(eid=eid, id="STOP02")
    ack2 = udp_client.send_position(pkt2)
    assert ack2.get("cmd") == "stop"

    # Second ACK: no cmd
    pkt3 = make_packet(eid=eid, id="STOP02")
    ack3 = udp_client.send_position(pkt3)
    assert "cmd" not in ack3


def test_stop_all(udp_client, http_client):
    """stop-all should queue stop for all active users."""
    eid = create_event(http_client, name="Stop All Test", admin_password="stopalladmin")

    # Send positions for multiple users
    for uid in ("SA01", "SA02", "SA03"):
        pkt = make_packet(eid=eid, id=uid)
        udp_client.send_position(pkt)

    # Issue stop-all
    status, body = http_client.post(
        f"/api/event/{eid}/admin/stop-all",
        headers={"X-Admin-Password": "stopalladmin"},
    )
    assert status == 200
    assert body["stopped_count"] >= 3

    # Each user should get stop command
    for uid in ("SA01", "SA02", "SA03"):
        pkt = make_packet(eid=eid, id=uid)
        ack = udp_client.send_position(pkt)
        assert ack.get("cmd") == "stop", f"User {uid} did not get stop command"


def test_cancel_assist(udp_client, http_client):
    """Cancel-assist should deliver cmd=cancel_assist in ACK."""
    eid = create_event(http_client, name="Cancel Assist Test", admin_password="caadmin")
    pkt1 = make_packet(eid=eid, id="CA01", ast=True)
    udp_client.send_position(pkt1)

    # Issue cancel-assist
    status, body = http_client.post(
        f"/api/event/{eid}/admin/cancel-assist/CA01",
        headers={"X-Admin-Password": "caadmin"},
    )
    assert status == 200

    # Next ACK should have cancel_assist
    pkt2 = make_packet(eid=eid, id="CA01")
    ack = udp_client.send_position(pkt2)
    assert ack.get("cmd") == "cancel_assist"


def test_stop_requires_admin_auth(http_client):
    """Stop command should require admin auth."""
    eid = create_event(http_client, name="Stop Auth Test", admin_password="stopauth")
    status, _ = http_client.post(
        f"/api/event/{eid}/admin/stop/NOAUTH",
        headers={"X-Admin-Password": "wrong", "X-Forwarded-For": "10.55.1.1"},
    )
    assert status == 401
