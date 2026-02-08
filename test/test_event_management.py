"""Tests for event creation, listing, updating, and archiving."""

from conftest import create_event, MANAGER_PASSWORD


def test_create_event_returns_eid(http_client):
    """Creating an event should return a new eid."""
    eid = create_event(http_client, name="New Event")
    assert isinstance(eid, int)
    assert eid > 0


def test_create_requires_name(http_client):
    """Creating an event without a name should fail."""
    status, body = http_client.post(
        "/api/manage/event",
        data={"admin_password": "pass"},
        headers={"X-Manager-Password": MANAGER_PASSWORD},
    )
    assert status == 400


def test_create_requires_admin_password(http_client):
    """Creating an event without admin_password should fail."""
    status, body = http_client.post(
        "/api/manage/event",
        data={"name": "No Password Event"},
        headers={"X-Manager-Password": MANAGER_PASSWORD},
    )
    assert status == 400


def test_create_requires_manager_auth(http_client):
    """Creating an event without manager auth should fail."""
    status, body = http_client.post(
        "/api/manage/event",
        data={"name": "Unauth Event", "admin_password": "pass"},
        headers={"X-Forwarded-For": "10.201.1.1"},
    )
    assert status == 401


def test_list_public_events(http_client):
    """GET /api/events should list non-archived events without passwords."""
    eid = create_event(http_client, name="Public List Test")
    status, body = http_client.get("/api/events")
    assert status == 200
    events = body["events"]
    eids = [e["eid"] for e in events]
    assert eid in eids
    # No passwords should be exposed
    for ev in events:
        assert "admin_password" not in ev
        assert "tracker_password" not in ev


def test_list_manage_events(http_client):
    """GET /api/manage/events should return full details with manager auth."""
    eid = create_event(http_client, name="Manage List Test")
    status, body = http_client.get(
        "/api/manage/events",
        headers={"X-Manager-Password": MANAGER_PASSWORD},
    )
    assert status == 200
    events = body["events"]
    eids = [e["eid"] for e in events]
    assert eid in eids


def test_update_event(http_client):
    """PATCH should update event name/description."""
    eid = create_event(http_client, name="Update Me")
    status, body = http_client.patch(
        f"/api/manage/event/{eid}",
        data={"name": "Updated Name", "description": "New desc"},
        headers={"X-Manager-Password": MANAGER_PASSWORD},
    )
    assert status == 200

    # Verify the update
    status, body = http_client.get(
        "/api/manage/events",
        headers={"X-Manager-Password": MANAGER_PASSWORD},
    )
    event = [e for e in body["events"] if e["eid"] == eid][0]
    assert event["name"] == "Updated Name"
    assert event["description"] == "New desc"


def test_archive_event(http_client):
    """Archiving an event should remove it from public list."""
    eid = create_event(http_client, name="Archive Me")

    # Verify it's in public list
    status, body = http_client.get("/api/events")
    eids_before = [e["eid"] for e in body["events"]]
    assert eid in eids_before

    # Archive
    http_client.patch(
        f"/api/manage/event/{eid}",
        data={"archived": True},
        headers={"X-Manager-Password": MANAGER_PASSWORD},
    )

    # Verify removed from public list
    status, body = http_client.get("/api/events")
    eids_after = [e["eid"] for e in body["events"]]
    assert eid not in eids_after


def test_tracker_password_stored_as_list(http_client):
    """Tracker password should be stored and returned as a list."""
    eid = create_event(http_client, name="List Pwd Test", tracker_password=["a", "b"])
    status, body = http_client.get(
        "/api/manage/events",
        headers={"X-Manager-Password": MANAGER_PASSWORD},
    )
    assert status == 200
    event = [e for e in body["events"] if e["eid"] == eid][0]
    assert event["tracker_password"] == ["a", "b"]


def test_tracker_password_string_migrated_to_list(http_client):
    """A single string tracker password should be returned as a one-element list."""
    eid = create_event(http_client, name="String Pwd Test", tracker_password="single")
    status, body = http_client.get(
        "/api/manage/events",
        headers={"X-Manager-Password": MANAGER_PASSWORD},
    )
    assert status == 200
    event = [e for e in body["events"] if e["eid"] == eid][0]
    assert event["tracker_password"] == ["single"]


def test_idle_interval_default_zero(http_client):
    """New event should have idle_interval: 0."""
    eid = create_event(http_client, name="Idle Default")
    status, body = http_client.get(
        "/api/manage/events",
        headers={"X-Manager-Password": MANAGER_PASSWORD},
    )
    assert status == 200
    event = [e for e in body["events"] if e["eid"] == eid][0]
    assert event.get("idle_interval", 0) == 0


def test_update_idle_interval(http_client):
    """PATCH idle_interval should update the event."""
    eid = create_event(http_client, name="Idle Update")
    status, body = http_client.patch(
        f"/api/manage/event/{eid}",
        data={"idle_interval": 30},
        headers={"X-Manager-Password": MANAGER_PASSWORD},
    )
    assert status == 200

    # Verify
    status, body = http_client.get(
        "/api/manage/events",
        headers={"X-Manager-Password": MANAGER_PASSWORD},
    )
    event = [e for e in body["events"] if e["eid"] == eid][0]
    assert event["idle_interval"] == 30


def test_event_data_directory_created(http_client, server):
    """Creating an event should create its data directory with logs subdir."""
    eid = create_event(http_client, name="Dir Test")
    event_dir = server.data_dir / "html" / str(eid)
    assert event_dir.exists()
    assert (event_dir / "logs").exists()
