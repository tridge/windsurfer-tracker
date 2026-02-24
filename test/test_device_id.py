"""Tests for device ID (did) field passthrough in tracker packets."""

import json
import time

from conftest import create_event, make_packet


def _read_log_entries(server, eid):
    """Read all JSONL log entries for an event."""
    event_dir = server.data_dir / "html" / str(eid) / "logs"
    entries = []
    for jf in event_dir.glob("*.jsonl"):
        for line in jf.read_text().strip().split("\n"):
            if line:
                entries.append(json.loads(line))
    return entries


def _read_positions(server, eid):
    """Read current_positions.json for an event."""
    pos_file = server.data_dir / "html" / str(eid) / "current_positions.json"
    if not pos_file.exists():
        return {}
    data = json.loads(pos_file.read_text())
    return data.get("sailors", data)


def test_did_in_jsonl_log(udp_client, http_client, server):
    """Device ID should appear in the JSONL daily log entry."""
    eid = create_event(http_client, name="DID Log Test")
    device_id = "abc123def456"
    pkt = make_packet(eid=eid, id="DID01", did=device_id)
    udp_client.send_position(pkt)

    time.sleep(0.3)

    entries = _read_log_entries(server, eid)
    matching = [e for e in entries if e.get("id") == "DID01"]
    assert len(matching) >= 1, f"Expected log entry for DID01, got {len(matching)}"
    assert matching[0].get("did") == device_id, f"Expected did={device_id}, got {matching[0]}"


def test_did_in_current_positions(udp_client, http_client, server):
    """Device ID should appear in current_positions.json."""
    eid = create_event(http_client, name="DID Positions Test")
    device_id = "pos_device_789"
    pkt = make_packet(eid=eid, id="DID02", did=device_id)
    udp_client.send_position(pkt)

    time.sleep(0.3)

    positions = _read_positions(server, eid)
    assert "DID02" in positions, f"Expected DID02 in positions, got {list(positions.keys())}"
    assert positions["DID02"].get("did") == device_id


def test_did_in_batch_log(udp_client, http_client, server):
    """Device ID should appear in JSONL log for 1Hz batch packets."""
    eid = create_event(http_client, name="DID Batch Test")
    device_id = "batch_device_abc"
    base_ts = int(time.time())
    pos_array = [
        [base_ts + i, -36.85 + i * 0.0001, 174.76 + i * 0.0001, 5.0]
        for i in range(5)
    ]
    pkt = make_packet(eid=eid, id="DID03", did=device_id, pos=pos_array, ts=base_ts + 4)
    pkt.pop("lat", None)
    pkt.pop("lon", None)
    udp_client.send_position(pkt)

    time.sleep(0.3)

    entries = _read_log_entries(server, eid)
    matching = [e for e in entries if e.get("id") == "DID03"]
    assert len(matching) >= 1, f"Expected log entry for DID03"
    assert matching[0].get("did") == device_id


def test_no_did_when_absent(udp_client, http_client, server):
    """When did is not sent, it should not appear in the log."""
    eid = create_event(http_client, name="DID Absent Test")
    pkt = make_packet(eid=eid, id="DID04")
    del pkt["did"]
    udp_client.send_position(pkt)

    time.sleep(0.3)

    entries = _read_log_entries(server, eid)
    matching = [e for e in entries if e.get("id") == "DID04"]
    assert len(matching) >= 1
    assert "did" not in matching[0], f"did should not be present when not sent"
