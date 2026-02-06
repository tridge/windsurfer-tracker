"""Tests for sublog (race marker) add/delete."""

import json
import time

from conftest import create_event, make_packet


def _setup_log_with_summary(udp_client, http_client, server, eid):
    """Send some positions and wait for the server to have a JSONL file, then
    manually create a summary so we can test sublogs."""
    import sys
    from pathlib import Path

    # Send a few positions to create log data
    for i in range(3):
        pkt = make_packet(eid=eid, id="SUB01")
        udp_client.send_position(pkt)

    time.sleep(0.3)

    # Generate summary using the server function
    server_dir = Path(__file__).resolve().parent.parent / "server"
    sys.path.insert(0, str(server_dir))
    from tracker_server import generate_log_summaries

    log_dir = server.data_dir / "html" / str(eid) / "logs"
    generate_log_summaries(log_dir)

    # Find the JSONL file name
    jsonl_files = list(log_dir.glob("*.jsonl"))
    assert len(jsonl_files) >= 1
    return jsonl_files[0].name


def test_add_sublog(udp_client, http_client, server):
    """POST sublog should appear in summary."""
    eid = create_event(http_client, name="Sublog Add", admin_password="subadmin")
    log_file = _setup_log_with_summary(udp_client, http_client, server, eid)

    status, body = http_client.post(
        f"/api/event/{eid}/log/{log_file}/sublog",
        data={"name": "Race 1", "start_ts": 1000, "end_ts": 2000},
        headers={"X-Admin-Password": "subadmin"},
    )
    assert status == 200

    # Check the returned summary has the sublog
    found = False
    for log_entry in body.get("logs", []):
        if log_entry.get("file") == log_file:
            sublogs = log_entry.get("sublogs", [])
            assert len(sublogs) >= 1
            assert sublogs[0]["name"] == "Race 1"
            found = True
    assert found, "Log entry not found in response"


def test_delete_sublog(udp_client, http_client, server):
    """DELETE sublog should remove it from summary."""
    eid = create_event(http_client, name="Sublog Delete", admin_password="subdel")
    log_file = _setup_log_with_summary(udp_client, http_client, server, eid)

    # Add a sublog first
    http_client.post(
        f"/api/event/{eid}/log/{log_file}/sublog",
        data={"name": "To Delete", "start_ts": 1000, "end_ts": 2000},
        headers={"X-Admin-Password": "subdel"},
    )

    # Delete it (index 0)
    status, body = http_client.delete(
        f"/api/event/{eid}/log/{log_file}/sublog/0",
        headers={"X-Admin-Password": "subdel"},
    )
    assert status == 200

    # Verify it's gone
    for log_entry in body.get("logs", []):
        if log_entry.get("file") == log_file:
            assert len(log_entry.get("sublogs", [])) == 0


def test_sublog_requires_admin_auth(udp_client, http_client, server):
    """Sublog operations should require admin auth."""
    eid = create_event(http_client, name="Sublog Auth", admin_password="subauth")
    log_file = _setup_log_with_summary(udp_client, http_client, server, eid)

    status, _ = http_client.post(
        f"/api/event/{eid}/log/{log_file}/sublog",
        data={"name": "Unauth", "start_ts": 1000, "end_ts": 2000},
        headers={"X-Admin-Password": "wrong", "X-Forwarded-For": "10.8.1.1"},
    )
    assert status == 401


def test_sublog_missing_fields(udp_client, http_client, server):
    """Missing required fields should return 400."""
    eid = create_event(http_client, name="Sublog Fields", admin_password="subfields")
    log_file = _setup_log_with_summary(udp_client, http_client, server, eid)

    # Missing end_ts
    status, _ = http_client.post(
        f"/api/event/{eid}/log/{log_file}/sublog",
        data={"name": "Incomplete", "start_ts": 1000},
        headers={"X-Admin-Password": "subfields"},
    )
    assert status == 400
