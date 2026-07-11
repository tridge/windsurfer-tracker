"""Rotated gt06.log files must be self-describing (conn_id -> IMEI).

rotate_log_to injects a synthetic IMEI-ID frame (protocol 0xFA) for every
identified open connection into the fresh log, and the analysis tools also
learn identity from cxzt *ID:* replies — so a single day's file attributes
streams whose LOGIN happened before the rotation.
"""
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from protocol_GT06 import (GT06Listener, GT06_PROTO_LOG_IMEI,  # noqa: E402
                           gt06_parse_login)
from gt06_frames import build_frame, build_login_data  # noqa: E402
from gt06_dump import (detect_format, read_packets, validate_frame,  # noqa: E402
                       ConnIdentityTracker)

IMEI = "866557081304307"
HB = b"\x78\x78\x05\x13\x01\x02\x03\x04\x0d\x0a"


def _listener(tmp_path):
    lst = GT06Listener(0, 1, "G", lambda *a, **k: None,
                       gt06_config={"default_eid": 1, "devices": {}},
                       log_func=lambda *a, **k: None)
    lst.log_file = tmp_path / "gt06.log"
    return lst


def _records(p):
    with open(p, "rb") as f:
        return list(read_packets(f, fmt=detect_format(f)))


def test_rotation_injects_imei_id_frame(tmp_path):
    lst = _listener(tmp_path)
    lst._open_log_v2(lst.log_file)
    conn = SimpleNamespace(conn_id=7, imei=IMEI)
    lst.connections[1] = conn
    lst._log_packet(conn, HB)
    lst.rotate_log_to(tmp_path / "gt06.log.2026-07-10")
    lst._log_packet(conn, HB)

    recs = _records(lst.log_file)
    assert len(recs) == 2
    ts, conn_id, outgoing, frame = recs[0]
    assert conn_id == 7 and not outgoing
    proto, data, _serial, crc_ok = validate_frame(frame)
    assert proto == GT06_PROTO_LOG_IMEI and crc_ok
    assert gt06_parse_login(data) == IMEI


def test_unidentified_conns_not_injected(tmp_path):
    lst = _listener(tmp_path)
    lst._open_log_v2(lst.log_file)
    lst.connections[1] = SimpleNamespace(conn_id=8, imei=None)  # pre-login conn
    lst.rotate_log_to(tmp_path / "arch")
    assert _records(lst.log_file) == []


def test_tracker_identity_sources():
    tracker = ConnIdentityTracker()
    # 0xFA marker
    marker = build_frame(GT06_PROTO_LOG_IMEI, build_login_data(IMEI), 0)
    assert tracker.feed(7, False, validate_frame(marker)) == "marker"
    assert tracker.get(7) == IMEI
    # cxzt reply *ID:* (identity only; different conn)
    text = ("NT19D_MG133_10F8G_B53_V667-GT06 MCU:B2.5-0*ID:788276404187547*"
            "1.2.3.4:7711*A:simbase*BT:4124*R:1|2|3").encode()
    cxzt = build_frame(0x15, bytes([len(text)]) + b"\x00" * 4 + text, 1)
    assert tracker.feed(9, False, validate_frame(cxzt)) == "cxzt"
    assert tracker.get(9) == "788276404187547"
    # outgoing frames never identify
    assert tracker.feed(10, True, validate_frame(marker)) is None
    assert tracker.get(10) is None


def test_dump_imei_filter_uses_marker(tmp_path):
    """gt06_dump --imei over a rotated file alone must include the stream,
    including frames logged BEFORE the identity arrived (buffered)."""
    lst = _listener(tmp_path)
    lst._open_log_v2(lst.log_file)
    conn = SimpleNamespace(conn_id=7, imei=IMEI)
    other = SimpleNamespace(conn_id=8, imei="866557081399999")
    # Simulate the rotated-file shape: frames first, identity (cxzt) later
    lst._log_packet(conn, HB)
    lst._log_packet(other, HB)
    text = ("W07-GT06 MCU:*ID:%s*1.2.3.4:7711*A:simbase*BT:4000*R:1|2|3" % IMEI).encode()
    lst._log_packet(conn, build_frame(0x15, bytes([len(text)]) + b"\x00" * 4 + text, 1))
    lst._log_packet(conn, HB)

    dump = Path(__file__).resolve().parent.parent / "scripts" / "gt06_dump.py"
    out = subprocess.run([sys.executable, str(dump), str(lst.log_file),
                          "--imei", IMEI], capture_output=True, text=True)
    assert out.returncode == 0
    lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    # both pre-identity HBs on conn 7 (buffered then flushed), the cxzt, the last HB
    assert sum("HB" in ln for ln in lines) == 2
    assert sum("CMDRESP" in ln for ln in lines) == 1
    # the other unit's stream is excluded
    assert not any("c8" in ln.split()[2] for ln in lines if len(ln.split()) > 2)
