"""Tests for user override set/get/delete."""

import json
import time

from conftest import create_event, make_packet


def test_set_user_override(http_client):
    """Set a user override (name + role) should succeed."""
    eid = create_event(http_client, name="Override Set", admin_password="overrideadmin")
    status, body = http_client.post(
        f"/api/event/{eid}/admin/user/S01",
        data={"name": "Andrew", "role": "support"},
        headers={"X-Admin-Password": "overrideadmin"},
    )
    assert status == 200
    assert body["success"] is True
    assert body["override"]["name"] == "Andrew"
    assert body["override"]["role"] == "support"


def test_get_users(http_client):
    """GET users should list the override."""
    eid = create_event(http_client, name="Override List", admin_password="overridelist")
    http_client.post(
        f"/api/event/{eid}/admin/user/S02",
        data={"name": "Bob"},
        headers={"X-Admin-Password": "overridelist"},
    )

    status, body = http_client.get(
        f"/api/event/{eid}/users",
        headers={"X-Admin-Password": "overridelist"},
    )
    assert status == 200
    assert "S02" in body["users"]
    assert body["users"]["S02"]["name"] == "Bob"


def test_delete_user_override(http_client):
    """DELETE override should remove it."""
    eid = create_event(http_client, name="Override Delete", admin_password="overridedel")
    http_client.post(
        f"/api/event/{eid}/admin/user/S03",
        data={"name": "Charlie"},
        headers={"X-Admin-Password": "overridedel"},
    )

    # Delete
    status, body = http_client.delete(
        f"/api/event/{eid}/admin/user/S03",
        headers={"X-Admin-Password": "overridedel"},
    )
    assert status == 200

    # Verify gone
    status, body = http_client.get(
        f"/api/event/{eid}/users",
        headers={"X-Admin-Password": "overridedel"},
    )
    assert "S03" not in body["users"]


def test_override_applies_to_positions(udp_client, http_client, server):
    """Override should appear in current_positions.json as displayid."""
    eid = create_event(http_client, name="Override Display", admin_password="overridedisp")

    # Set override
    http_client.post(
        f"/api/event/{eid}/admin/user/OVR01",
        data={"name": "Override Name"},
        headers={"X-Admin-Password": "overridedisp"},
    )

    # Send a position
    pkt = make_packet(eid=eid, id="OVR01")
    udp_client.send_position(pkt)
    time.sleep(0.3)

    pos_file = server.data_dir / "html" / str(eid) / "current_positions.json"
    data = json.loads(pos_file.read_text())
    assert data["sailors"]["OVR01"]["name"] == "Override Name"
    assert data["sailors"]["OVR01"]["displayid"] == "Override Name"


def test_override_requires_admin_auth(http_client):
    """User override should require admin auth."""
    eid = create_event(http_client, name="Override Auth", admin_password="overrideauth")
    status, _ = http_client.post(
        f"/api/event/{eid}/admin/user/S04",
        data={"name": "Unauth"},
        headers={"X-Admin-Password": "wrong", "X-Forwarded-For": "10.11.1.1"},
    )
    assert status == 401
