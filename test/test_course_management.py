"""Tests for course save/get/delete."""

import json

from conftest import create_event


def test_save_course(http_client):
    """Save course JSON should succeed."""
    eid = create_event(http_client, name="Course Save", admin_password="courseadmin")
    course = {"marks": [{"name": "Start", "lat": -36.85, "lon": 174.76}]}
    status, body = http_client.post(
        f"/api/event/{eid}/admin/course",
        data=course,
        headers={"X-Admin-Password": "courseadmin"},
    )
    assert status == 200
    assert body["success"] is True


def test_get_course(http_client):
    """GET course should return saved data."""
    eid = create_event(http_client, name="Course Get", admin_password="courseadmin2")
    course = {"marks": [{"name": "Buoy A", "lat": -36.84, "lon": 174.77}]}
    http_client.post(
        f"/api/event/{eid}/admin/course",
        data=course,
        headers={"X-Admin-Password": "courseadmin2"},
    )

    status, body = http_client.get(f"/api/event/{eid}/course")
    assert status == 200
    assert len(body["marks"]) == 1
    assert body["marks"][0]["name"] == "Buoy A"


def test_delete_course(http_client):
    """DELETE course should make GET return empty."""
    eid = create_event(http_client, name="Course Delete", admin_password="coursedel")
    course = {"marks": [{"name": "Gone", "lat": -36.83, "lon": 174.78}]}
    http_client.post(
        f"/api/event/{eid}/admin/course",
        data=course,
        headers={"X-Admin-Password": "coursedel"},
    )

    # Delete
    status, _ = http_client.delete(
        f"/api/event/{eid}/admin/course",
        headers={"X-Admin-Password": "coursedel"},
    )
    assert status == 200

    # GET should now return null/empty course
    status, body = http_client.get(f"/api/event/{eid}/course")
    assert status == 200
    assert body.get("course") is None


def test_course_requires_admin_auth(http_client):
    """Course save should require admin auth."""
    eid = create_event(http_client, name="Course Auth", admin_password="courseauth")
    course = {"marks": []}
    status, _ = http_client.post(
        f"/api/event/{eid}/admin/course",
        data=course,
        headers={"X-Admin-Password": "wrong", "X-Forwarded-For": "10.22.1.1"},
    )
    assert status == 401


def test_course_invalid_json(http_client):
    """Invalid JSON should return 400."""
    eid = create_event(http_client, name="Course Bad JSON", admin_password="coursejson")
    status, _ = http_client.post(
        f"/api/event/{eid}/admin/course",
        data=b"not json{{{",
        headers={"X-Admin-Password": "coursejson", "Content-Type": "application/json"},
    )
    assert status == 400
