"""Tests for GPX file upload."""

import json
import time

from conftest import create_event


def test_upload_gpx_success(http_client, server, sample_data_dir):
    """Upload GPX file with correct password should succeed."""
    eid = create_event(http_client, name="GPX Upload", tracker_password="gpxpwd")

    gpx_path = sample_data_dir / "20260131.gpx"
    assert gpx_path.exists(), f"Sample GPX not found: {gpx_path}"

    gpx_content = gpx_path.read_bytes()
    status, body = http_client.post_multipart(
        f"/api/event/{eid}/upload-track",
        fields={"name": "TestSailor", "password": "gpxpwd"},
        files={"file": ("track.gpx", gpx_content)},
    )
    assert status == 200, f"Upload failed: {body}"
    assert body["success"] is True
    assert body["points"] > 0


def test_upload_gpx_creates_jsonl(http_client, server, sample_data_dir):
    """GPX upload should create JSONL log entries."""
    eid = create_event(http_client, name="GPX JSONL Test", tracker_password="gpxpwd2")

    gpx_content = (sample_data_dir / "20260131.gpx").read_bytes()
    status, body = http_client.post_multipart(
        f"/api/event/{eid}/upload-track",
        fields={"name": "GPXSailor", "password": "gpxpwd2"},
        files={"file": ("track.gpx", gpx_content)},
    )
    assert status == 200

    # Check JSONL files exist in the event's log dir
    event_log_dir = server.data_dir / "html" / str(eid) / "logs"
    jsonl_files = list(event_log_dir.glob("*.jsonl"))
    assert len(jsonl_files) >= 1

    # Check entries have correct format
    entries = []
    for jf in jsonl_files:
        for line in jf.read_text().strip().split("\n"):
            if line:
                entries.append(json.loads(line))

    assert len(entries) > 0
    entry = entries[0]
    assert entry["id"] == "GPXSailor(GPX)"
    assert entry["src"] == "gpx"
    assert entry["displayid"] == "GPXSailor"


def test_upload_gpx_sorted_by_timestamp(http_client, server, sample_data_dir):
    """Uploaded entries should be sorted by timestamp."""
    eid = create_event(http_client, name="GPX Sort Test", tracker_password="gpxsort")

    gpx_content = (sample_data_dir / "20260131.gpx").read_bytes()
    http_client.post_multipart(
        f"/api/event/{eid}/upload-track",
        fields={"name": "Sorted", "password": "gpxsort"},
        files={"file": ("track.gpx", gpx_content)},
    )

    event_log_dir = server.data_dir / "html" / str(eid) / "logs"
    for jf in event_log_dir.glob("*.jsonl"):
        timestamps = []
        for line in jf.read_text().strip().split("\n"):
            if line:
                entry = json.loads(line)
                timestamps.append(entry["ts"])
        # Verify sorted
        assert timestamps == sorted(timestamps), "Entries not sorted by timestamp"


def test_upload_gpx_wrong_password(http_client, sample_data_dir):
    """Upload with wrong tracker password should fail."""
    eid = create_event(http_client, name="GPX Auth Fail", tracker_password="correctpwd")

    gpx_content = (sample_data_dir / "20260131.gpx").read_bytes()
    status, body = http_client.post_multipart(
        f"/api/event/{eid}/upload-track",
        fields={"name": "TestSailor", "password": "wrongpwd"},
        files={"file": ("track.gpx", gpx_content)},
        headers={"X-Forwarded-For": "10.44.1.1"},
    )
    assert status == 401


def test_upload_no_file(http_client):
    """Upload without a file should fail."""
    eid = create_event(http_client, name="GPX No File")
    status, body = http_client.post_multipart(
        f"/api/event/{eid}/upload-track",
        fields={"name": "NoFile"},
    )
    assert status == 400


def test_upload_unsupported_extension(http_client):
    """Upload with unsupported file type should fail."""
    eid = create_event(http_client, name="GPX Bad Ext")
    status, body = http_client.post_multipart(
        f"/api/event/{eid}/upload-track",
        fields={"name": "BadExt"},
        files={"file": ("track.csv", b"lat,lon\n1,2\n")},
    )
    assert status == 400


def test_upload_gpx_dual_password(http_client, server, sample_data_dir):
    """Upload with second of two tracker passwords should succeed."""
    eid = create_event(http_client, name="GPX Dual Pwd", tracker_password=["gpxA", "gpxB"])

    gpx_content = (sample_data_dir / "20260131.gpx").read_bytes()
    status, body = http_client.post_multipart(
        f"/api/event/{eid}/upload-track",
        fields={"name": "DualSailor", "password": "gpxB"},
        files={"file": ("track.gpx", gpx_content)},
    )
    assert status == 200, f"Upload failed: {body}"
    assert body["success"] is True
    assert body["points"] > 0
