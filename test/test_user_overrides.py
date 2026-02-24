"""Tests for user override set/get/delete."""

import json
import time

from conftest import create_event, make_packet, make_idle_packet


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


def test_override_applies_to_idle_positions(udp_client, http_client, server):
    """Override should appear in current_positions.json for idle trackers too."""
    eid = create_event(http_client, name="Override Idle", admin_password="overrideidle")

    # Set override
    http_client.post(
        f"/api/event/{eid}/admin/user/IDLE01",
        data={"name": "Idle Name"},
        headers={"X-Admin-Password": "overrideidle"},
    )

    # Send an idle packet
    pkt = make_idle_packet(eid=eid, id="IDLE01")
    udp_client.send_position(pkt)
    time.sleep(0.3)

    pos_file = server.data_dir / "html" / str(eid) / "current_positions.json"
    data = json.loads(pos_file.read_text())
    assert data["sailors"]["IDLE01"]["name"] == "Idle Name"
    assert data["sailors"]["IDLE01"]["displayid"] == "Idle Name"


def test_override_requires_admin_auth(http_client):
    """User override should require admin auth."""
    eid = create_event(http_client, name="Override Auth", admin_password="overrideauth")
    status, _ = http_client.post(
        f"/api/event/{eid}/admin/user/S04",
        data={"name": "Unauth"},
        headers={"X-Admin-Password": "wrong", "X-Forwarded-For": "10.11.1.1"},
    )
    assert status == 401


# ── Device-ID (did) based override tests ──────────────────────────────


def test_override_stored_with_did_key(udp_client, http_client, server):
    """POST override for a device with did should store as did:XXX key."""
    eid = create_event(http_client, name="DID Store", admin_password="didstore")

    # Send a position so the server knows this device's did
    pkt = make_packet(eid=eid, id="DS01", did="dev_store_001")
    udp_client.send_position(pkt)
    time.sleep(0.3)

    # Set override
    status, body = http_client.post(
        f"/api/event/{eid}/admin/user/DS01",
        data={"name": "Device Name"},
        headers={"X-Admin-Password": "didstore"},
    )
    assert status == 200

    # Read users.json directly to verify did:XXX key
    users_file = server.data_dir / "html" / str(eid) / "users.json"
    users_data = json.loads(users_file.read_text())
    assert "did:dev_store_001" in users_data["users"]
    assert users_data["users"]["did:dev_store_001"]["name"] == "Device Name"
    assert users_data["users"]["did:dev_store_001"]["_last_id"] == "DS01"
    # Old sailor_id key should NOT be present
    assert "DS01" not in users_data["users"]


def test_override_survives_sailor_id_change(udp_client, http_client, server):
    """Override set via did should follow the device when sailor_id changes."""
    eid = create_event(http_client, name="DID Survive", admin_password="didsurvive")
    did = "dev_survive_001"

    # Send position as "SV01"
    pkt = make_packet(eid=eid, id="SV01", did=did)
    udp_client.send_position(pkt)
    time.sleep(0.3)

    # Set override for SV01
    http_client.post(
        f"/api/event/{eid}/admin/user/SV01",
        data={"name": "Survivor"},
        headers={"X-Admin-Password": "didsurvive"},
    )

    # Now the same device sends as "SV02" (user changed their sailor_id)
    pkt2 = make_packet(eid=eid, id="SV02", did=did)
    udp_client.send_position(pkt2)
    time.sleep(0.3)

    # Check current_positions.json — SV02 should have the override applied
    pos_file = server.data_dir / "html" / str(eid) / "current_positions.json"
    data = json.loads(pos_file.read_text())
    assert data["sailors"]["SV02"]["displayid"] == "Survivor"


def test_get_users_resolves_did_to_sailor_id(udp_client, http_client, server):
    """GET /api/users should resolve did:XXX entries to current sailor_id."""
    eid = create_event(http_client, name="DID Resolve", admin_password="didresolve")
    did = "dev_resolve_001"

    # Send position
    pkt = make_packet(eid=eid, id="RES01", did=did)
    udp_client.send_position(pkt)
    time.sleep(0.3)

    # Set override
    http_client.post(
        f"/api/event/{eid}/admin/user/RES01",
        data={"name": "Resolved"},
        headers={"X-Admin-Password": "didresolve"},
    )

    # GET users should show it keyed by sailor_id, not did:XXX
    status, body = http_client.get(
        f"/api/event/{eid}/users",
        headers={"X-Admin-Password": "didresolve"},
    )
    assert status == 200
    assert "RES01" in body["users"]
    assert body["users"]["RES01"]["name"] == "Resolved"
    # Internal _last_id should be stripped from the response
    assert "_last_id" not in body["users"]["RES01"]
    # did:XXX key should NOT appear in the response
    assert f"did:{did}" not in body["users"]


def test_delete_removes_did_key(udp_client, http_client, server):
    """DELETE should remove both did:XXX and sailor_id entries."""
    eid = create_event(http_client, name="DID Delete", admin_password="diddelete")
    did = "dev_delete_001"

    # Send position
    pkt = make_packet(eid=eid, id="DEL01", did=did)
    udp_client.send_position(pkt)
    time.sleep(0.3)

    # Set override (stored as did:XXX)
    http_client.post(
        f"/api/event/{eid}/admin/user/DEL01",
        data={"name": "Deletable"},
        headers={"X-Admin-Password": "diddelete"},
    )

    # Delete
    status, body = http_client.delete(
        f"/api/event/{eid}/admin/user/DEL01",
        headers={"X-Admin-Password": "diddelete"},
    )
    assert status == 200

    # Verify both keys gone from users.json
    users_file = server.data_dir / "html" / str(eid) / "users.json"
    users_data = json.loads(users_file.read_text())
    assert "DEL01" not in users_data["users"]
    assert f"did:{did}" not in users_data["users"]


def test_fallback_to_sailor_id_when_no_did(udp_client, http_client, server):
    """Override should fall back to sailor_id lookup when device has no did."""
    eid = create_event(http_client, name="DID Fallback", admin_password="didfallback")

    # Send position WITHOUT did
    pkt = make_packet(eid=eid, id="FB01")
    del pkt["did"]
    udp_client.send_position(pkt)
    time.sleep(0.3)

    # Set override (should be stored as sailor_id since no did)
    http_client.post(
        f"/api/event/{eid}/admin/user/FB01",
        data={"name": "Fallback"},
        headers={"X-Admin-Password": "didfallback"},
    )

    # Verify stored as sailor_id key
    users_file = server.data_dir / "html" / str(eid) / "users.json"
    users_data = json.loads(users_file.read_text())
    assert "FB01" in users_data["users"]
    assert users_data["users"]["FB01"]["name"] == "Fallback"

    # Override should apply to positions
    pkt2 = make_packet(eid=eid, id="FB01")
    del pkt2["did"]
    udp_client.send_position(pkt2)
    time.sleep(0.3)

    pos_file = server.data_dir / "html" / str(eid) / "current_positions.json"
    data = json.loads(pos_file.read_text())
    assert data["sailors"]["FB01"]["displayid"] == "Fallback"


def test_get_users_resolves_after_id_change(udp_client, http_client, server):
    """GET /api/users resolves did:XXX to new sailor_id after device changes id."""
    eid = create_event(http_client, name="DID Remap", admin_password="didremap")
    did = "dev_remap_001"

    # Device starts as "RM01"
    pkt = make_packet(eid=eid, id="RM01", did=did)
    udp_client.send_position(pkt)
    time.sleep(0.3)

    # Set override
    http_client.post(
        f"/api/event/{eid}/admin/user/RM01",
        data={"name": "Remapped"},
        headers={"X-Admin-Password": "didremap"},
    )

    # Device changes to "RM02"
    pkt2 = make_packet(eid=eid, id="RM02", did=did)
    udp_client.send_position(pkt2)
    time.sleep(0.3)

    # GET users should now show it under RM02
    status, body = http_client.get(
        f"/api/event/{eid}/users",
        headers={"X-Admin-Password": "didremap"},
    )
    assert status == 200
    assert "RM02" in body["users"]
    assert body["users"]["RM02"]["name"] == "Remapped"
    # RM01 should not appear (no separate entry)
    assert "RM01" not in body["users"]
