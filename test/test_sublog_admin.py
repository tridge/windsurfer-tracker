"""Tests for hiding logs/sublogs and per-sublog courses (marks as laid)."""

import json
import os
import sys
from pathlib import Path

from conftest import create_event

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from tracker_server import generate_log_summaries, preserve_summary_edits  # noqa: E402

DATE = "2026_11_15"
T0 = 1789000000

COURSE = {
    "start": {"lat": -36.80, "lon": 174.70, "pin": {"dist": 100, "side": "left"}},
    "marks": [{"lat": -36.79, "lon": 174.71, "color": "#ef4444", "rounding": "port"},
              {"lat": -36.795, "lon": 174.72}],
    "finish": {"lat": -36.80, "lon": 174.701},
    "order": "S1212F",
}


def _seed(server, eid, admin):
    """Two log files (base + .1) with a summary and two sublogs on the base log."""
    log_dir = server.data_dir / "html" / str(eid) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    for name, t in ((f"{DATE}.jsonl.1", T0), (f"{DATE}.jsonl", T0 + 7200)):
        with open(log_dir / name, "w") as f:
            for i in range(10):
                f.write(json.dumps({"id": "S01", "ts": t + i * 60, "lat": -36.8, "lon": 174.7}) + "\n")
    generate_log_summaries(log_dir)
    return log_dir


def _add_sublogs(http_client, eid, admin):
    for name, s in (("Race 1", T0 + 7200), ("Race 2", T0 + 7500)):
        status, _ = http_client.post(f"/api/event/{eid}/log/{DATE}.jsonl/sublog",
                                     data={"name": name, "start_ts": s, "end_ts": s + 200},
                                     headers={"X-Admin-Password": admin})
        assert status == 200


def _entry(summary, file):
    return next(e for e in summary["logs"] if e["file"] == file)


def test_hide_and_unhide_log(http_client, server):
    eid = create_event(http_client, name="Hide Log", admin_password="hideadm")
    log_dir = _seed(server, eid, "hideadm")
    url = f"/api/event/{eid}/log/{DATE}.jsonl.1/hidden"

    status, body = http_client.post(url, data={"hidden": True}, headers={"X-Admin-Password": "hideadm"})
    assert status == 200
    assert _entry(body, f"{DATE}.jsonl.1")["hidden"] is True
    assert "hidden" not in _entry(body, f"{DATE}.jsonl")
    on_disk = json.loads((log_dir / f"{DATE}_summary.json").read_text())
    assert _entry(on_disk, f"{DATE}.jsonl.1")["hidden"] is True

    status, body = http_client.post(url, data={"hidden": False}, headers={"X-Admin-Password": "hideadm"})
    assert status == 200
    assert "hidden" not in _entry(body, f"{DATE}.jsonl.1")


def test_hide_sublog_with_start_check(http_client, server):
    eid = create_event(http_client, name="Hide Sublog", admin_password="hidesub")
    _seed(server, eid, "hidesub")
    _add_sublogs(http_client, eid, "hidesub")
    url = f"/api/event/{eid}/log/{DATE}.jsonl/hidden"
    hdr = {"X-Admin-Password": "hidesub"}

    status, body = http_client.post(url, data={"hidden": True, "sublog": 1, "start_ts": T0 + 7500}, headers=hdr)
    assert status == 200
    subs = _entry(body, f"{DATE}.jsonl")["sublogs"]
    assert subs[1]["hidden"] is True and "hidden" not in subs[0]
    assert "hidden" not in _entry(body, f"{DATE}.jsonl")

    # stale index (start_ts doesn't match) is refused
    status, _ = http_client.post(url, data={"hidden": False, "sublog": 0, "start_ts": T0 + 7500}, headers=hdr)
    assert status == 409
    status, _ = http_client.post(url, data={"hidden": True, "sublog": 5}, headers=hdr)
    assert status == 400


def test_hidden_bad_input(http_client, server):
    eid = create_event(http_client, name="Hide Bad", admin_password="hidebad")
    _seed(server, eid, "hidebad")
    hdr = {"X-Admin-Password": "hidebad"}
    base = f"/api/event/{eid}/log"
    assert http_client.post(f"{base}/{DATE}.jsonl/hidden", data={"hidden": "yes"}, headers=hdr)[0] == 400
    assert http_client.post(f"{base}/{DATE}.jsonl/hidden", data={"hidden": True, "sublog": "0"}, headers=hdr)[0] == 400
    assert http_client.post(f"{base}/{DATE}.jsonl/hidden", data=[1], headers=hdr)[0] == 400
    assert http_client.post(f"{base}/{DATE}.jsonl/hidden", data=b"{bad", headers=hdr)[0] == 400
    assert http_client.post(f"{base}/..%2Fx.jsonl/hidden", data={"hidden": True}, headers=hdr)[0] == 400
    assert http_client.post(f"{base}/2026_11_16.jsonl/hidden", data={"hidden": True}, headers=hdr)[0] == 404


def test_admin_endpoints_require_auth(http_client, server):
    eid = create_event(http_client, name="Hide Auth", admin_password="hideauth")
    _seed(server, eid, "hideauth")
    _add_sublogs(http_client, eid, "hideauth")
    base = f"/api/event/{eid}/log/{DATE}.jsonl"
    status, _ = http_client.post(f"{base}/hidden", data={"hidden": True},
                                 headers={"X-Admin-Password": "wrong", "X-Forwarded-For": "10.8.7.1"})
    assert status == 401
    status, _ = http_client.post(f"{base}/sublog/0/course", data={"course": COURSE},
                                 headers={"X-Admin-Password": "wrong", "X-Forwarded-For": "10.8.7.2"})
    assert status == 401
    status, _ = http_client.post(f"{base}/sublog/0/course", data={"course": COURSE},
                                 headers={"X-Forwarded-For": "10.8.7.3"})
    assert status == 401


def test_set_and_reset_sublog_course(http_client, server):
    eid = create_event(http_client, name="Sublog Course", admin_password="crsadm")
    log_dir = _seed(server, eid, "crsadm")
    _add_sublogs(http_client, eid, "crsadm")
    url = f"/api/event/{eid}/log/{DATE}.jsonl/sublog/1/course"
    hdr = {"X-Admin-Password": "crsadm"}

    status, body = http_client.post(url, data={"course": COURSE, "start_ts": T0 + 7500}, headers=hdr)
    assert status == 200
    sub = _entry(body, f"{DATE}.jsonl")["sublogs"][1]
    assert sub["course"] == COURSE and sub["course_saved"] > 0
    assert "course" not in _entry(body, f"{DATE}.jsonl")["sublogs"][0]

    status, _ = http_client.post(url, data={"course": COURSE, "start_ts": T0}, headers=hdr)
    assert status == 409

    status, body = http_client.post(url, data={"course": None}, headers=hdr)
    assert status == 200
    sub = _entry(body, f"{DATE}.jsonl")["sublogs"][1]
    assert "course" not in sub and "course_saved" not in sub
    on_disk = json.loads((log_dir / f"{DATE}_summary.json").read_text())
    assert "course" not in _entry(on_disk, f"{DATE}.jsonl")["sublogs"][1]


def test_sublog_course_validation(http_client, server):
    eid = create_event(http_client, name="Course Valid", admin_password="crsval")
    _seed(server, eid, "crsval")
    _add_sublogs(http_client, eid, "crsval")
    url = f"/api/event/{eid}/log/{DATE}.jsonl/sublog/0/course"
    hdr = {"X-Admin-Password": "crsval"}

    def post(course):
        return http_client.post(url, data={"course": course}, headers=hdr)[0]

    assert http_client.post(url, data={}, headers=hdr)[0] == 400
    assert post([1, 2]) == 400
    assert post({"marks": [{"lat": 95, "lon": 174}]}) == 400
    assert post({"marks": [{"lat": "x", "lon": 174}]}) == 400
    assert post({"start": {"lat": -36.8}}) == 400
    assert post({"marks": "nope"}) == 400
    assert post({"marks": [{"lat": -36.8, "lon": 174.7}] * 101}) == 400
    assert post({"extraCourses": [{}] * 6}) == 400
    assert post({"extraCourses": [{"marks": [{"lat": -36.8, "lon": True}]}]}) == 400
    assert post({"comment": "x" * 300000}) == 400
    deep = {}
    for _ in range(12):
        deep = {"a": deep}
    assert post(deep) == 400
    # NaN would make the summary invalid JSON for browsers
    raw = b'{"course": {"marks": [{"lat": NaN, "lon": 174.7}]}}'
    assert http_client.post(url, data=raw, headers=dict(hdr, **{"Content-Type": "application/json"}))[0] == 400
    raw = b'{"course": {"start": {"lat": -36.8, "lon": 174.7, "pin": {"dist": Infinity}}}}'
    assert http_client.post(url, data=raw, headers=dict(hdr, **{"Content-Type": "application/json"}))[0] == 400

    # a multi-course file with two courses is fine
    assert post(dict(COURSE, extraCourses=[{"marks": [{"lat": -36.7, "lon": 174.6}]}])) == 200


def test_edits_survive_regeneration(http_client, server):
    eid = create_event(http_client, name="Edits Kept", admin_password="keepadm")
    log_dir = _seed(server, eid, "keepadm")
    _add_sublogs(http_client, eid, "keepadm")
    hdr = {"X-Admin-Password": "keepadm"}
    base = f"/api/event/{eid}/log"
    assert http_client.post(f"{base}/{DATE}.jsonl.1/hidden", data={"hidden": True}, headers=hdr)[0] == 200
    assert http_client.post(f"{base}/{DATE}.jsonl/hidden", data={"hidden": True, "sublog": 0}, headers=hdr)[0] == 200
    assert http_client.post(f"{base}/{DATE}.jsonl/sublog/1/course", data={"course": COURSE}, headers=hdr)[0] == 200

    # new data in the live log forces regeneration
    with open(log_dir / f"{DATE}.jsonl", "a") as f:
        f.write(json.dumps({"id": "S02", "ts": T0 + 9000, "lat": -36.8, "lon": 174.7}) + "\n")
    summary_file = log_dir / f"{DATE}_summary.json"
    os.utime(summary_file, (0, 0))
    assert generate_log_summaries(log_dir) >= 1

    summary = json.loads(summary_file.read_text())
    assert "S02" in _entry(summary, f"{DATE}.jsonl")["sailors"]
    assert _entry(summary, f"{DATE}.jsonl.1")["hidden"] is True
    subs = _entry(summary, f"{DATE}.jsonl")["sublogs"]
    assert subs[0]["hidden"] is True
    assert subs[1]["course"] == COURSE


def test_preserve_edits_follow_rotation(tmp_path):
    """A log rotated to .N keeps its hidden flag/sublogs (matched by start_ts)."""
    summary_file = tmp_path / f"{DATE}_summary.json"
    summary_file.write_text(json.dumps({"logs": [
        {"file": f"{DATE}.jsonl", "start_ts": 100, "hidden": True,
         "sublogs": [{"name": "Race 1", "start_ts": 110, "end_ts": 120}]},
    ]}))
    logs = [{"file": f"{DATE}.jsonl", "start_ts": 500}, {"file": f"{DATE}.jsonl.1", "start_ts": 100}]
    preserve_summary_edits(summary_file, logs)
    assert "hidden" not in logs[0] and "sublogs" not in logs[0]
    assert logs[1]["hidden"] is True and logs[1]["sublogs"][0]["name"] == "Race 1"

    # same file with a changed start (merged upload) still keeps its edits
    logs = [{"file": f"{DATE}.jsonl", "start_ts": 50}]
    preserve_summary_edits(summary_file, logs)
    assert logs[0]["hidden"] is True


def test_search_skips_hidden_for_non_admin(http_client, server):
    eid = create_event(http_client, name="Hide Search", admin_password="hidesrch")
    _seed(server, eid, "hidesrch")
    _add_sublogs(http_client, eid, "hidesrch")
    hdr = {"X-Admin-Password": "hidesrch"}
    assert http_client.post(f"/api/event/{eid}/log/{DATE}.jsonl/hidden",
                            data={"hidden": True, "sublog": 0}, headers=hdr)[0] == 200

    status, body = http_client.get(f"/api/event/{eid}/search?q=race")
    assert status == 200
    assert [r["name"] for r in body["results"] if r["type"] == "sublog"] == ["Race 2"]
    status, body = http_client.get(f"/api/event/{eid}/search?q=race", headers=hdr)
    assert sorted(r["name"] for r in body["results"] if r["type"] == "sublog") == ["Race 1", "Race 2"]

    # hiding the whole log hides its sailors and sublogs
    assert http_client.post(f"/api/event/{eid}/log/{DATE}.jsonl/hidden",
                            data={"hidden": True}, headers=hdr)[0] == 200
    status, body = http_client.get(f"/api/event/{eid}/search?q=race")
    assert [r for r in body["results"] if r["type"] == "sublog"] == []
    status, body = http_client.get(f"/api/event/{eid}/search?q=s01")
    assert {r["log_file"] for r in body["results"]} == {f"{DATE}.jsonl.1"}
