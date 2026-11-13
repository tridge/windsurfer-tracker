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


def test_deleted_named_course_kept_in_bin(http_client, server):
    """Deleting a named course keeps it in a private recycle bin; removing an entry
    by id removes just that one."""
    eid = create_event(http_client, name="Course Bin", admin_password="bin1")
    headers = {"X-Admin-Password": "bin1"}
    course = {"start": {"lat": -36.85, "lon": 174.76}, "marks": [{"lat": -36.84, "lon": 174.76}],
              "finish": {"lat": -36.84, "lon": 174.77}, "order": "S1F"}
    for name in ("Slalom", "Other"):
        http_client.post(f"/api/event/{eid}/admin/courses/{name}", data=course, headers=headers)
        status, _ = http_client.delete(f"/api/event/{eid}/admin/courses/{name}", headers=headers)
        assert status == 200

    status, body = http_client.get(f"/api/event/{eid}/admin/deleted-courses", headers=headers)
    assert status == 200
    assert [e["name"] for e in body["deleted"]] == ["Other", "Slalom"]
    entry = body["deleted"][1]
    assert entry["course"]["order"] == "S1F" and len(entry["id"]) == 32

    # not in the public web tree
    assert not (server.data_dir / "html" / str(eid) / "deleted_courses.json").exists()
    assert (server.data_dir / "private" / str(eid) / "deleted_courses.json").exists()
    # course files stay readable by a separate web server; the bin doesn't
    import stat
    assert stat.S_IMODE((server.data_dir / "html" / str(eid) / "courses.json").stat().st_mode) == 0o644
    assert stat.S_IMODE((server.data_dir / "private" / str(eid) / "deleted_courses.json").stat().st_mode) == 0o600

    # malformed ids are refused, not treated as "match everything"
    for bad in ("nan", "1234.0", "x" * 32):
        status, _ = http_client.delete(f"/api/event/{eid}/admin/deleted-courses/{bad}", headers=headers)
        assert status == 400
    status, body = http_client.delete(f"/api/event/{eid}/admin/deleted-courses/{entry['id']}", headers=headers)
    assert status == 200 and body["removed"] == 1
    status, body = http_client.get(f"/api/event/{eid}/admin/deleted-courses", headers=headers)
    assert [e["name"] for e in body["deleted"]] == ["Other"]


def test_rename_named_course(http_client):
    """Rename is atomic: 404 if the old name is gone, 409 if the new one exists,
    and it doesn't put anything in the recycle bin."""
    eid = create_event(http_client, name="Course Rename", admin_password="ren1")
    headers = {"X-Admin-Password": "ren1"}
    course = {"start": {"lat": -36.85, "lon": 174.76}, "finish": {"lat": -36.84, "lon": 174.77}}
    for name in ("One", "Two"):
        http_client.post(f"/api/event/{eid}/admin/courses/{name}", data=course, headers=headers)
    url = f"/api/event/{eid}/admin/courses-rename"
    status, _ = http_client.post(url, data={"from": "One", "to": "Two"}, headers=headers)
    assert status == 409
    status, _ = http_client.post(url, data={"from": "Gone", "to": "New"}, headers=headers)
    assert status == 404
    status, body = http_client.post(url, data={"from": "One", "to": "New"}, headers=headers)
    assert status == 200 and body["success"]
    status, body = http_client.get(f"/api/event/{eid}/courses")
    assert sorted(body["courses"]) == ["New", "Two"]
    status, body = http_client.get(f"/api/event/{eid}/admin/deleted-courses", headers=headers)
    assert body["deleted"] == []


def test_named_course_create_and_replace(http_client):
    """?create=1 refuses an existing name; ?replace=1 puts the replaced course in the bin."""
    eid = create_event(http_client, name="Course Create", admin_password="cre1")
    headers = {"X-Admin-Password": "cre1"}
    a = {"start": {"lat": -36.1, "lon": 174.76}, "finish": {"lat": -36.1, "lon": 174.77}}
    b = {"start": {"lat": -36.2, "lon": 174.76}, "finish": {"lat": -36.2, "lon": 174.77}}
    status, _ = http_client.post(f"/api/event/{eid}/admin/courses/X?create=1", data=a, headers=headers)
    assert status == 200
    status, body = http_client.post(f"/api/event/{eid}/admin/courses/X?create=1", data=b, headers=headers)
    assert status == 409 and body.get("exists")
    status, body = http_client.get(f"/api/event/{eid}/courses")
    assert body["courses"]["X"]["start"]["lat"] == -36.1
    status, _ = http_client.post(f"/api/event/{eid}/admin/courses/X?replace=1", data=b, headers=headers)
    assert status == 200
    status, body = http_client.get(f"/api/event/{eid}/courses")
    assert body["courses"]["X"]["start"]["lat"] == -36.2
    status, body = http_client.get(f"/api/event/{eid}/admin/deleted-courses", headers=headers)
    assert [(e["name"], e["course"]["start"]["lat"]) for e in body["deleted"]] == [("X", -36.1)]


def test_concurrent_deletes_keep_every_course(http_client):
    """Many simultaneous deletes: every course ends up in the bin, none resurrected."""
    import concurrent.futures
    eid = create_event(http_client, name="Course Race", admin_password="race1")
    headers = {"X-Admin-Password": "race1"}
    course = {"start": {"lat": -36.1, "lon": 174.76}, "finish": {"lat": -36.1, "lon": 174.77}}
    names = [f"C{i}" for i in range(12)]
    for n in names:
        http_client.post(f"/api/event/{eid}/admin/courses/{n}", data=course, headers=headers)
    with concurrent.futures.ThreadPoolExecutor(12) as ex:
        list(ex.map(lambda n: http_client.delete(f"/api/event/{eid}/admin/courses/{n}", headers=headers), names))
    status, body = http_client.get(f"/api/event/{eid}/courses")
    assert body["courses"] == {}
    status, body = http_client.get(f"/api/event/{eid}/admin/deleted-courses", headers=headers)
    assert sorted(e["name"] for e in body["deleted"]) == sorted(names)


def test_deleted_courses_requires_auth(http_client):
    eid = create_event(http_client, name="Course Bin Auth", admin_password="bin2")
    # a unique client address so these failed logins don't rate-limit other tests
    status, _ = http_client.get(f"/api/event/{eid}/admin/deleted-courses", headers={"X-Forwarded-For": "10.77.0.1"})
    assert status == 401
    status, _ = http_client.get(f"/api/event/{eid}/admin/course-history", headers={"X-Forwarded-For": "10.77.0.2"})
    assert status == 401


def test_course_history_lists_earlier_versions(http_client):
    """Each live-course save rotates the old one; history lists them newest first."""
    eid = create_event(http_client, name="Course History", admin_password="hist1")
    headers = {"X-Admin-Password": "hist1"}
    for lat in (-36.1, -36.2, -36.3):
        course = {"start": {"lat": lat, "lon": 174.76}, "finish": {"lat": lat, "lon": 174.77}}
        http_client.post(f"/api/event/{eid}/admin/course", data=course, headers=headers)
    status, body = http_client.get(f"/api/event/{eid}/admin/course-history", headers=headers)
    assert status == 200
    lats = [h["course"]["start"]["lat"] for h in body["history"]]
    assert lats == [-36.3, -36.2, -36.1]
    assert body["history"][0]["file"] == "course.json"
