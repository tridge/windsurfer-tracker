"""Tests for FIT file upload (requires fitparse library)."""

import json

import pytest

fitparse = pytest.importorskip("fitparse")

from conftest import create_event


def test_upload_fit_success(http_client, server, sample_data_dir):
    """Upload FIT file should succeed with point count."""
    eid = create_event(http_client, name="FIT Upload", tracker_password="fitpwd")

    fit_path = sample_data_dir / "2026-01-24_AndrewPW.fit"
    assert fit_path.exists(), f"Sample FIT not found: {fit_path}"

    fit_content = fit_path.read_bytes()
    status, body = http_client.post_multipart(
        f"/api/event/{eid}/upload-track",
        fields={"name": "FITSailor", "password": "fitpwd"},
        files={"file": ("track.fit", fit_content)},
    )
    assert status == 200, f"Upload failed: {body}"
    assert body["success"] is True
    assert body["points"] > 0


def test_upload_fit_creates_jsonl(http_client, server, sample_data_dir):
    """FIT upload should create JSONL entries with FIT-specific fields."""
    eid = create_event(http_client, name="FIT JSONL Test", tracker_password="fitpwd2")

    fit_content = (sample_data_dir / "2026-01-24_AndrewPW.fit").read_bytes()
    http_client.post_multipart(
        f"/api/event/{eid}/upload-track",
        fields={"name": "FITLog", "password": "fitpwd2"},
        files={"file": ("track.fit", fit_content)},
    )

    event_log_dir = server.data_dir / "html" / str(eid) / "logs"
    entries = []
    for jf in event_log_dir.glob("*.jsonl"):
        for line in jf.read_text().strip().split("\n"):
            if line:
                entries.append(json.loads(line))

    assert len(entries) > 0
    entry = entries[0]
    assert entry["id"] == "FITLog(FIT)"
    assert entry["src"] == "fit"
    assert entry["displayid"] == "FITLog"


def test_upload_fit_heart_rate(http_client, server, sample_data_dir):
    """FIT upload should include heart rate data when present."""
    eid = create_event(http_client, name="FIT HR Test", tracker_password="fithr")

    fit_content = (sample_data_dir / "2026-01-24_AndrewPW.fit").read_bytes()
    http_client.post_multipart(
        f"/api/event/{eid}/upload-track",
        fields={"name": "FITHR", "password": "fithr"},
        files={"file": ("track.fit", fit_content)},
    )

    event_log_dir = server.data_dir / "html" / str(eid) / "logs"
    entries = []
    for jf in event_log_dir.glob("*.jsonl"):
        for line in jf.read_text().strip().split("\n"):
            if line:
                entries.append(json.loads(line))

    # At least some entries should have heart rate
    hr_entries = [e for e in entries if "hr" in e]
    assert len(hr_entries) > 0, "No entries with heart rate found"
    # Heart rate should be reasonable
    assert all(30 < e["hr"] < 250 for e in hr_entries)


def test_upload_fit_wrong_password(http_client, sample_data_dir):
    """FIT upload with wrong tracker password should fail."""
    eid = create_event(http_client, name="FIT Auth Fail", tracker_password="fitcorrect")

    fit_content = (sample_data_dir / "2026-01-24_AndrewPW.fit").read_bytes()
    status, body = http_client.post_multipart(
        f"/api/event/{eid}/upload-track",
        fields={"name": "BadAuth", "password": "wrongpwd"},
        files={"file": ("track.fit", fit_content)},
        headers={"X-Forwarded-For": "10.33.1.1"},
    )
    assert status == 401
