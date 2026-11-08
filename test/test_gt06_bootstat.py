"""The server asks our firmware for CXCS#BOOTSTAT once per connection, so reboots are logged."""

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from protocol_GT06 import GT06Connection, GT06Listener, gt06_crc_itu  # noqa: E402


class FakeSock:
    def __init__(self):
        self.sent = []

    def fileno(self):
        return 1

    def sendall(self, data):
        self.sent.append(data)


def reply_frame(text, serial=5):
    payload = bytes([4 + len(text)]) + b"\0\0\0\0" + text.encode()
    body = bytes([len(payload) + 5, 0x15]) + payload + struct.pack(">H", serial)
    return b"\x78\x78" + body + struct.pack(">H", gt06_crc_itu(body)) + b"\r\n"


def make(firmware_text):
    listener = GT06Listener(
        port=None, interval=1, id_prefix="G", get_tracker_func=lambda eid: None,
        gt06_config={"default_eid": 1, "devices": {}}, gt06_config_path=None,
        log_func=lambda *a, **k: None,
    )
    sock = FakeSock()
    conn = GT06Connection(sock, ("127.0.0.1", 1))
    conn.imei = "888800000000178"
    conn.authenticated = True
    listener.connections[1] = conn
    cxzt = (f"{firmware_text}-GT06 MCU:R5*ID:***000178*1.2.3.4:7711*A:simbase*G:A*4G:14*M:1|2|0"
            "*F:60|1800*H:15*SP:1*SF:0*V:0*BT:4000")
    return listener, conn, sock, reply_frame(cxzt)


def queued(conn, sock):
    return [c for c, _ in conn.cmd_queue] + [s.decode("latin1") for s in sock.sent]


def test_wstr_firmware_is_asked_once():
    listener, conn, sock, frame = make("WSTR-STD1_abc1234")
    listener._process_frame(1, frame)
    listener._process_frame(1, frame)
    assert sum("CXCS#BOOTSTAT" in c for c in queued(conn, sock)) == 1


def test_vendor_firmware_is_not_asked():
    listener, conn, sock, frame = make("NT19D_MG133_10F8G_B53_V667")
    listener._process_frame(1, frame)
    assert not any("CXCS#BOOTSTAT" in c for c in queued(conn, sock))
