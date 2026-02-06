"""Tests for 1Hz batch position mode."""

import json
import time

from conftest import create_event, make_packet


def _make_pos_array(base_ts, count=10):
    """Generate a pos array of [ts, lat, lon, spd] entries."""
    return [
        [base_ts + i, -36.85 + i * 0.0001, 174.76 + i * 0.0001, 5.0 + i * 0.1]
        for i in range(count)
    ]


def test_1hz_batch_ack(udp_client, http_client):
    """Send UDP packet with pos array, should get ACK."""
    eid = create_event(http_client, name="1Hz Batch ACK")
    base_ts = int(time.time())
    pos_array = _make_pos_array(base_ts)
    pkt = make_packet(eid=eid, id="HZ01", pos=pos_array, ts=base_ts + 9)
    # Remove lat/lon since pos array provides them
    pkt.pop("lat", None)
    pkt.pop("lon", None)
    ack = udp_client.send_position(pkt)
    assert ack["ack"] == pkt["sq"]


def test_1hz_batch_logged(udp_client, http_client, server):
    """JSONL entry should contain the pos array."""
    eid = create_event(http_client, name="1Hz Batch Log")
    base_ts = int(time.time())
    pos_array = _make_pos_array(base_ts)
    pkt = make_packet(eid=eid, id="HZ02", pos=pos_array, ts=base_ts + 9)
    pkt.pop("lat", None)
    pkt.pop("lon", None)
    udp_client.send_position(pkt)

    time.sleep(0.3)

    event_dir = server.data_dir / "html" / str(eid) / "logs"
    entries = []
    for jf in event_dir.glob("*.jsonl"):
        for line in jf.read_text().strip().split("\n"):
            if line:
                entries.append(json.loads(line))

    matching = [e for e in entries if e.get("id") == "HZ02"]
    assert len(matching) >= 1
    entry = matching[0]
    assert "pos" in entry
    assert len(entry["pos"]) == 10


def test_1hz_positions_use_last(udp_client, http_client, server):
    """current_positions.json should use the last position from the array."""
    eid = create_event(http_client, name="1Hz Last Pos")
    base_ts = int(time.time())
    pos_array = _make_pos_array(base_ts)
    last_lat = pos_array[-1][1]
    last_lon = pos_array[-1][2]
    pkt = make_packet(eid=eid, id="HZ03", pos=pos_array, ts=base_ts + 9)
    pkt.pop("lat", None)
    pkt.pop("lon", None)
    udp_client.send_position(pkt)

    time.sleep(0.3)

    pos_file = server.data_dir / "html" / str(eid) / "current_positions.json"
    data = json.loads(pos_file.read_text())
    assert "HZ03" in data["sailors"]
    sailor = data["sailors"]["HZ03"]
    assert abs(sailor["lat"] - last_lat) < 0.001
    assert abs(sailor["lon"] - last_lon) < 0.001
