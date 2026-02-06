"""Tests for authentication and rate limiting."""

import time

from conftest import create_event, make_packet, MANAGER_PASSWORD


def test_admin_auth_correct(http_client):
    """Admin auth check with correct password should succeed."""
    eid = create_event(http_client, name="Auth OK Test", admin_password="goodpass")
    status, body = http_client.get(
        f"/api/event/{eid}/auth/check",
        headers={"X-Admin-Password": "goodpass"},
    )
    assert status == 200
    assert body["authenticated"] is True


def test_admin_auth_wrong(http_client):
    """Admin auth check with wrong password should fail."""
    eid = create_event(http_client, name="Auth Fail Test", admin_password="correct")
    status, body = http_client.get(
        f"/api/event/{eid}/auth/check",
        headers={"X-Admin-Password": "wrong", "X-Forwarded-For": "10.88.1.1"},
    )
    assert status == 401
    assert body["authenticated"] is False


def test_udp_tracker_password_correct(udp_client, http_client):
    """UDP with correct tracker password should work."""
    eid = create_event(http_client, name="UDP Auth OK", tracker_password="udpsecret")
    pkt = make_packet(eid=eid, id="AUTH01", pwd="udpsecret")
    ack = udp_client.send_position(pkt)
    assert ack["ack"] == pkt["sq"]
    assert "error" not in ack


def test_udp_tracker_password_wrong(udp_client, http_client):
    """UDP with wrong tracker password should get error ACK."""
    eid = create_event(http_client, name="UDP Auth Fail", tracker_password="udpsecret2")
    pkt = make_packet(eid=eid, id="AUTH02", pwd="badpwd")
    ack = udp_client.send_position(pkt)
    assert ack.get("error") == "auth"


def test_rate_limiting(http_client):
    """Wrong password then immediate retry should be rate limited."""
    eid = create_event(http_client, name="Rate Limit Test", admin_password="ratelimitpw")
    fake_ip = "10.77.1.1"

    # First: wrong password
    status, _ = http_client.get(
        f"/api/event/{eid}/auth/check",
        headers={"X-Admin-Password": "wrongpw", "X-Forwarded-For": fake_ip},
    )
    assert status == 401

    # Immediate retry should be rate limited - even correct password fails
    status, _ = http_client.get(
        f"/api/event/{eid}/auth/check",
        headers={"X-Admin-Password": "ratelimitpw", "X-Forwarded-For": fake_ip},
    )
    assert status == 401


def test_manager_auth_required(http_client):
    """Manager endpoints should require manager auth."""
    # No password - use unique IPs to avoid poisoning the rate limiter
    status, _ = http_client.get(
        "/api/manage/events",
        headers={"X-Forwarded-For": "10.200.1.1"},
    )
    assert status == 401

    # Wrong password
    status, _ = http_client.get(
        "/api/manage/events",
        headers={"X-Manager-Password": "wrongmanager", "X-Forwarded-For": "10.200.1.2"},
    )
    assert status == 401


def test_cross_event_admin_passwords(http_client):
    """Admin password from one event should not work for another."""
    eid1 = create_event(http_client, name="Event A", admin_password="passA")
    eid2 = create_event(http_client, name="Event B", admin_password="passB")

    # passA should not work for event B
    status, _ = http_client.get(
        f"/api/event/{eid2}/auth/check",
        headers={"X-Admin-Password": "passA", "X-Forwarded-For": "10.66.1.1"},
    )
    assert status == 401
