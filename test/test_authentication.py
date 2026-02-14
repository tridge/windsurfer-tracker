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


def test_udp_dual_tracker_passwords(udp_client, http_client):
    """Both tracker passwords should work via UDP."""
    # Wait for rate limit from prior wrong-password UDP tests (127.0.0.1 can't use X-Forwarded-For)
    time.sleep(5)
    eid = create_event(http_client, name="Dual UDP", tracker_password=["pwd1", "pwd2"])

    # First password should work
    pkt1 = make_packet(eid=eid, id="DUAL01", pwd="pwd1")
    ack1 = udp_client.send_position(pkt1)
    assert ack1["ack"] == pkt1["sq"]
    assert "error" not in ack1

    # Second password should also work
    pkt2 = make_packet(eid=eid, id="DUAL02", pwd="pwd2")
    ack2 = udp_client.send_position(pkt2)
    assert ack2["ack"] == pkt2["sq"]
    assert "error" not in ack2


def test_http_dual_tracker_passwords(http_client):
    """Both tracker passwords should work via HTTP POST."""
    eid = create_event(http_client, name="Dual HTTP", tracker_password=["httppwd1", "httppwd2"])

    # First password
    pkt1 = make_packet(eid=eid, id="DUALH1", pwd="httppwd1")
    status1, body1 = http_client.post("/api/tracker", data=pkt1)
    assert status1 == 200
    assert "error" not in body1

    # Second password
    pkt2 = make_packet(eid=eid, id="DUALH2", pwd="httppwd2")
    status2, body2 = http_client.post("/api/tracker", data=pkt2)
    assert status2 == 200
    assert "error" not in body2


def test_dual_password_wrong_rejected(udp_client, http_client):
    """Wrong password should still be rejected when dual passwords are set."""
    eid = create_event(http_client, name="Dual Reject", tracker_password=["right1", "right2"])
    pkt = make_packet(eid=eid, id="DUALR1", pwd="wrongpwd")
    ack = udp_client.send_position(pkt)
    assert ack.get("error") == "auth"


def test_rate_limit_per_user_isolation(http_client):
    """Failed auth for user A should not block user B from the same IP."""
    eid = create_event(http_client, name="Rate Isolation", tracker_password=["secret123"])
    shared_ip = "10.99.1.1"

    # User A sends wrong password — triggers rate limit for (shared_ip, "BAD01")
    pkt_bad = make_packet(eid=eid, id="BAD01", pwd="wrongpwd")
    status_bad, body_bad = http_client.post(
        "/api/tracker", data=pkt_bad,
        headers={"X-Forwarded-For": shared_ip},
    )
    assert status_bad == 401
    assert body_bad.get("error") == "auth"

    # User B from same IP with correct password should NOT be rate limited
    pkt_good = make_packet(eid=eid, id="GOOD01", pwd="secret123")
    status_good, body_good = http_client.post(
        "/api/tracker", data=pkt_good,
        headers={"X-Forwarded-For": shared_ip},
    )
    assert status_good == 200
    assert "error" not in body_good
