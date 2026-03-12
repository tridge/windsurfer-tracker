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


def test_save_named_course(http_client):
    """Save a named course and retrieve it."""
    eid = create_event(http_client, name="Named Save", admin_password="named1")
    course = {"start": {"lat": -36.85, "lon": 174.76}, "marks": [], "finish": {"lat": -36.84, "lon": 174.77}}
    status, body = http_client.post(
        f"/api/event/{eid}/admin/courses/Windward-Leeward",
        data=course,
        headers={"X-Admin-Password": "named1"},
    )
    assert status == 200
    assert body["success"] is True

    # GET courses should list it
    status, body = http_client.get(f"/api/event/{eid}/courses")
    assert status == 200
    assert "Windward-Leeward" in body["courses"]
    assert body["courses"]["Windward-Leeward"]["start"]["lat"] == -36.85


def test_save_multiple_named_courses(http_client):
    """Multiple named courses should coexist."""
    eid = create_event(http_client, name="Named Multi", admin_password="named2")
    headers = {"X-Admin-Password": "named2"}
    course_a = {"start": {"lat": -36.85, "lon": 174.76}, "marks": [], "finish": {"lat": -36.84, "lon": 174.77}}
    course_b = {"start": {"lat": -36.80, "lon": 174.70}, "marks": [{"lat": -36.81, "lon": 174.71}], "finish": {"lat": -36.82, "lon": 174.72}}

    http_client.post(f"/api/event/{eid}/admin/courses/Course%20A", data=course_a, headers=headers)
    http_client.post(f"/api/event/{eid}/admin/courses/Triangle", data=course_b, headers=headers)

    status, body = http_client.get(f"/api/event/{eid}/courses")
    assert status == 200
    assert len(body["courses"]) == 2
    assert "Course A" in body["courses"]
    assert "Triangle" in body["courses"]
    assert len(body["courses"]["Triangle"]["marks"]) == 1


def test_delete_named_course(http_client):
    """Delete a named course, others remain."""
    eid = create_event(http_client, name="Named Del", admin_password="named3")
    headers = {"X-Admin-Password": "named3"}
    course = {"start": {"lat": -36.85, "lon": 174.76}, "marks": [], "finish": {"lat": -36.84, "lon": 174.77}}

    http_client.post(f"/api/event/{eid}/admin/courses/Keep", data=course, headers=headers)
    http_client.post(f"/api/event/{eid}/admin/courses/Remove", data=course, headers=headers)

    status, body = http_client.delete(f"/api/event/{eid}/admin/courses/Remove", headers=headers)
    assert status == 200
    assert body["success"] is True

    status, body = http_client.get(f"/api/event/{eid}/courses")
    assert status == 200
    assert "Keep" in body["courses"]
    assert "Remove" not in body["courses"]


def test_named_courses_empty_by_default(http_client):
    """GET courses on fresh event returns empty dict."""
    eid = create_event(http_client, name="Named Empty", admin_password="named4")
    status, body = http_client.get(f"/api/event/{eid}/courses")
    assert status == 200
    assert body["courses"] == {}


def test_named_course_requires_auth(http_client):
    """Named course save requires admin auth."""
    eid = create_event(http_client, name="Named Auth", admin_password="named5")
    course = {"start": {"lat": -36.85, "lon": 174.76}, "marks": [], "finish": {"lat": -36.84, "lon": 174.77}}
    status, _ = http_client.post(
        f"/api/event/{eid}/admin/courses/Test",
        data=course,
        headers={"X-Admin-Password": "wrong", "X-Forwarded-For": "10.22.2.1"},
    )
    assert status == 401


def test_named_course_overwrite(http_client):
    """Saving with same name overwrites the previous course."""
    eid = create_event(http_client, name="Named Overwrite", admin_password="named6")
    headers = {"X-Admin-Password": "named6"}

    course_v1 = {"start": {"lat": -36.85, "lon": 174.76}, "marks": [], "finish": {"lat": -36.84, "lon": 174.77}}
    course_v2 = {"start": {"lat": -36.80, "lon": 174.70}, "marks": [], "finish": {"lat": -36.79, "lon": 174.69}}

    http_client.post(f"/api/event/{eid}/admin/courses/MyRoute", data=course_v1, headers=headers)
    http_client.post(f"/api/event/{eid}/admin/courses/MyRoute", data=course_v2, headers=headers)

    status, body = http_client.get(f"/api/event/{eid}/courses")
    assert status == 200
    assert body["courses"]["MyRoute"]["start"]["lat"] == -36.80


def test_course_invalid_json(http_client):
    """Invalid JSON should return 400."""
    eid = create_event(http_client, name="Course Bad JSON", admin_password="coursejson")
    status, _ = http_client.post(
        f"/api/event/{eid}/admin/course",
        data=b"not json{{{",
        headers={"X-Admin-Password": "coursejson", "Content-Type": "application/json"},
    )
    assert status == 400
