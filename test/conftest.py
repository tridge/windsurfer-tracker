"""
Server fixture, UDP/HTTP client helpers, and shared test utilities.

Starts a real tracker_server.py subprocess in multi-event mode and provides
helpers for UDP and HTTP communication.
"""

import gzip
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import pytest

SERVER_SCRIPT = Path(__file__).resolve().parent.parent / "server" / "tracker_server.py"
SAMPLE_DATA_DIR = Path(__file__).resolve().parent / "sample_data"

MANAGER_PASSWORD = "testmanager"
DEFAULT_ADMIN_PASSWORD = "admin123"
DEFAULT_TRACKER_PASSWORD = ""  # no tracker password by default

# Auto-incrementing counters for unique packet generation
_seq_counter = 1000
_ts_counter = int(time.time())


class ServerInfo:
    """Holds connection details for the running server."""
    def __init__(self, host, port, data_dir, manager_password, process, log_file,
                 gt06_port=None):
        self.host = host
        self.port = port
        self.data_dir = data_dir
        self.manager_password = manager_password
        self.process = process
        self.log_file = log_file
        self.gt06_port = gt06_port


class UDPClient:
    """Simple UDP client for sending tracker packets."""

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(2.0)

    def send_position(self, packet_dict):
        """Send a JSON position packet and return the ACK dict.

        Skips any proactive command packets in the socket buffer,
        keeping only the real ACK response.
        """
        data = json.dumps(packet_dict).encode("utf-8")
        self.sock.sendto(data, (self.host, self.port))
        while True:
            resp_data, _ = self.sock.recvfrom(4096)
            resp = json.loads(resp_data.decode("utf-8"))
            if resp.get("proactive"):
                continue  # Skip proactive command packets
            return resp

    def receive_proactive(self, timeout=2.0):
        """Try to receive a proactive command packet. Returns dict or None on timeout."""
        old_timeout = self.sock.gettimeout()
        self.sock.settimeout(timeout)
        try:
            resp_data, _ = self.sock.recvfrom(4096)
            return json.loads(resp_data.decode("utf-8"))
        except socket.timeout:
            return None
        finally:
            self.sock.settimeout(old_timeout)

    def send_raw(self, raw_bytes):
        """Send raw bytes and try to receive a response. Returns bytes or None on timeout."""
        self.sock.sendto(raw_bytes, (self.host, self.port))
        try:
            resp_data, _ = self.sock.recvfrom(4096)
            return resp_data
        except socket.timeout:
            return None

    def close(self):
        self.sock.close()


class HTTPClient:
    """Simple HTTP client using urllib (stdlib only)."""

    def __init__(self, base_url):
        self.base_url = base_url

    def _request(self, method, path, data=None, headers=None):
        """Make an HTTP request. Returns (status_code, response_body_dict_or_str)."""
        url = self.base_url + path
        req_headers = headers or {}
        # Use a safe default IP so rate limiting from intentional auth failures
        # doesn't block normal requests (server reads X-Forwarded-For for IP)
        req_headers.setdefault("X-Forwarded-For", "192.168.100.1")

        body = None
        if data is not None:
            if isinstance(data, (dict, list)):
                body = json.dumps(data).encode("utf-8")
                req_headers.setdefault("Content-Type", "application/json")
            elif isinstance(data, bytes):
                body = data
            else:
                body = str(data).encode("utf-8")

        req = Request(url, data=body, headers=req_headers, method=method)
        try:
            resp = urlopen(req, timeout=5)
            resp_body = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(resp_body)
            except json.JSONDecodeError:
                return resp.status, resp_body
        except HTTPError as e:
            resp_body = e.read().decode("utf-8")
            try:
                return e.code, json.loads(resp_body)
            except json.JSONDecodeError:
                return e.code, resp_body

    def get(self, path, headers=None):
        return self._request("GET", path, headers=headers)

    def post(self, path, data=None, headers=None):
        return self._request("POST", path, data=data, headers=headers)

    def patch(self, path, data=None, headers=None):
        return self._request("PATCH", path, data=data, headers=headers)

    def delete(self, path, headers=None):
        return self._request("DELETE", path, headers=headers)

    def post_multipart(self, path, fields=None, files=None, headers=None):
        """Send a multipart/form-data POST request.

        fields: dict of {name: value} for text fields
        files: dict of {name: (filename, content_bytes)} for file fields
        """
        import uuid
        boundary = uuid.uuid4().hex
        body_parts = []

        if fields:
            for name, value in fields.items():
                body_parts.append(
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                    f"{value}\r\n".encode("utf-8")
                )
        if files:
            for name, (filename, content) in files.items():
                body_parts.append(
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                    f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8")
                )
                body_parts.append(content)
                body_parts.append(b"\r\n")

        body_parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(body_parts)

        req_headers = headers or {}
        req_headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"

        return self._request("POST", path, data=body, headers=req_headers)


def _gt06_crc_itu(data):
    """CRC-ITU (CRC-16/X.25) — matches server implementation."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0x8408
            else:
                crc >>= 1
    return crc ^ 0xFFFF


class GT06Client:
    """TCP client emulator for GT06 GPS tracker protocol.

    Builds and sends binary GT06 frames (login, location, heartbeat, alarm)
    and receives/parses server responses and commands.
    """

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.sock = None
        self._serial = 0

    def connect(self):
        """Open TCP connection to the GT06 listener."""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(3.0)
        self.sock.connect((self.host, self.port))

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def _next_serial(self):
        self._serial += 1
        return self._serial

    def _build_frame(self, protocol, data):
        """Build a complete GT06 frame with CRC."""
        serial = self._next_serial()
        # length = protocol(1) + data + serial(2) + crc(2)
        length = 1 + len(data) + 2 + 2
        payload = struct.pack(">B", length) + struct.pack(">B", protocol) + data
        payload += struct.pack(">H", serial)
        crc = _gt06_crc_itu(payload)
        return b"\x78\x78" + payload + struct.pack(">H", crc) + b"\x0d\x0a"

    def _recv_frames(self, timeout=0.5):
        """Receive all available frames from the server within timeout."""
        frames = []
        buf = b""
        self.sock.settimeout(timeout)
        try:
            while True:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                # Extract complete frames
                while len(buf) >= 5:
                    if buf[0:2] != b"\x78\x78":
                        # Skip junk
                        idx = buf.find(b"\x78\x78", 1)
                        if idx < 0:
                            buf = b""
                            break
                        buf = buf[idx:]
                        continue
                    length = buf[2]
                    frame_size = 2 + 1 + length + 2  # start(2) + len(1) + payload + end(2)
                    if len(buf) < frame_size:
                        break
                    frames.append(buf[:frame_size])
                    buf = buf[frame_size:]
        except socket.timeout:
            pass
        return frames

    def _parse_frame(self, frame):
        """Parse a GT06 frame into (protocol, data_bytes, serial)."""
        protocol = frame[3]
        length = frame[2]
        serial_offset = 3 + length - 4
        serial = struct.unpack(">H", frame[serial_offset:serial_offset + 2])[0]
        data = frame[4:serial_offset]
        return protocol, data, serial

    def _extract_command_text(self, frame):
        """Extract ASCII command string from a server command frame (protocol 0x80)."""
        protocol, data, serial = self._parse_frame(frame)
        if protocol != 0x80:
            return None
        # data: content_len(1) + server_flag(4) + cmd_ascii
        if len(data) < 5:
            return None
        return data[5:].decode("ascii", errors="replace")

    def send_login(self, imei="863874081226122"):
        """Send login packet and return list of received frames."""
        # BCD-encode the IMEI (pad to 16 digits = 8 bytes)
        imei_padded = imei.rjust(16, "0")
        imei_bcd = bytes.fromhex(imei_padded)
        frame = self._build_frame(0x01, imei_bcd)
        self.sock.sendall(frame)
        return self._recv_frames()

    def build_location_data(self, lat=-35.2999, lon=149.1003, speed_kmh=0,
                            heading=180, satellites=8, gps_valid=True,
                            year=26, month=2, day=21, hour=12, minute=0, second=0):
        """Build 18-byte location data block."""
        data = struct.pack(">BBBBBB", year, month, day, hour, minute, second)
        gps_info = (satellites & 0x0F) | 0xF0  # high nibble = GPS data length
        data += struct.pack(">B", gps_info)

        lat_raw = int(abs(lat) * 1_800_000)
        lon_raw = int(abs(lon) * 1_800_000)
        data += struct.pack(">II", lat_raw, lon_raw)

        data += struct.pack(">B", speed_kmh)

        course_status = heading & 0x03FF
        if gps_valid:
            course_status |= (1 << 12)
        if lat >= 0:
            course_status |= (1 << 10)  # North
        if lon < 0:
            course_status |= (1 << 11)  # West
        data += struct.pack(">H", course_status)

        return data

    def send_location(self, protocol=0x12, **kwargs):
        """Send a location packet. Returns list of received frames."""
        data = self.build_location_data(**kwargs)
        frame = self._build_frame(protocol, data)
        self.sock.sendall(frame)
        return self._recv_frames(timeout=0.1)

    def send_heartbeat(self, battery_level=6, signal=4, charging=False):
        """Send heartbeat packet. Returns list of received frames.

        battery_level: 0-6 (GT06 raw level, mapped to 0-100% by server)
        signal: 0-4
        charging: bool
        """
        info = 0x08 if charging else 0x00
        data = struct.pack(">BBB", info, battery_level, signal)
        frame = self._build_frame(0x13, data)
        self.sock.sendall(frame)
        return self._recv_frames()

    def send_alarm(self, alarm_type="SOS", protocol=0x16, battery_level=6,
                   signal=4, charging=False, **loc_kwargs):
        """Send alarm/SOS packet. Returns list of received frames.

        alarm_type: "Normal", "Shock", "Power Cut", "Low Battery", "SOS"
        """
        loc_data = self.build_location_data(**loc_kwargs)

        alarm_bits_map = {"Normal": 0, "Shock": 1, "Power Cut": 2,
                          "Low Battery": 3, "SOS": 4}
        alarm_bits = alarm_bits_map.get(alarm_type, 0)

        # LBS data (minimal: 0 bytes)
        lbs_len = 0
        # terminal_info: alarm_bits in bits 3-5, charging in bit 2
        ti = (alarm_bits << 3)
        if charging:
            ti |= 0x04

        extra = struct.pack(">BBBB", lbs_len, ti, battery_level, signal)
        frame = self._build_frame(protocol, loc_data + extra)
        self.sock.sendall(frame)
        return self._recv_frames()

    def get_commands(self, frames):
        """Extract command text strings from a list of frames."""
        cmds = []
        for f in frames:
            cmd = self._extract_command_text(f)
            if cmd is not None:
                cmds.append(cmd)
        return cmds

    def drain(self, timeout=0.1):
        """Read and discard any pending data from the socket."""
        self.sock.settimeout(timeout)
        try:
            while self.sock.recv(4096):
                pass
        except socket.timeout:
            pass


@pytest.fixture
def gt06_client(server):
    """Function-scoped GT06 TCP client."""
    client = GT06Client(server.host, server.gt06_port)
    client.connect()
    yield client
    client.close()


def _find_free_port(tcp=False):
    """Find a free port by binding to port 0."""
    sock_type = socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM
    with socket.socket(socket.AF_INET, sock_type) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(host, port, timeout=10.0):
    """Wait for the server to be ready by polling UDP."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.3)
    deadline = time.time() + timeout

    dummy_packet = json.dumps({"id": "probe", "sq": 0, "ts": 0, "lat": 0, "lon": 0,
                                "spd": 0, "hdg": 0, "ast": False, "bat": 50,
                                "sig": 3, "role": "sailor", "ver": "test", "eid": 1}).encode("utf-8")

    while time.time() < deadline:
        try:
            sock.sendto(dummy_packet, (host, port))
            sock.recvfrom(4096)
            sock.close()
            return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            time.sleep(0.2)

    sock.close()
    return False


@pytest.fixture(scope="session")
def server(tmp_path_factory):
    """Start a real tracker_server.py subprocess. Session-scoped (one for all tests)."""
    data_dir = tmp_path_factory.mktemp("server_data")
    html_dir = data_dir / "html"
    html_dir.mkdir()
    # Write a minimal index.html so static serving doesn't error
    (html_dir / "index.html").write_text("<html><body>test</body></html>")

    port = _find_free_port()
    gt06_port = _find_free_port(tcp=True)

    # Pre-write events.json with manager password and a default event
    events_data = {
        "next_eid": 2,
        "manager_password": MANAGER_PASSWORD,
        "events": {
            "1": {
                "name": "Default Test Event",
                "description": "Auto-created for tests",
                "admin_password": DEFAULT_ADMIN_PASSWORD,
                "tracker_password": DEFAULT_TRACKER_PASSWORD,
                "timezone": "Pacific/Auckland",
                "archived": False,
                "assist_enabled": True,
            }
        }
    }
    events_file = data_dir / "events.json"
    events_file.write_text(json.dumps(events_data))

    log_path = data_dir / "server.log"
    log_fh = open(log_path, "w")

    proc = subprocess.Popen(
        [
            sys.executable, str(SERVER_SCRIPT),
            "--port", str(port),
            "--static-dir", str(html_dir),
            "--events-file", str(events_file),
            "--manager-password", MANAGER_PASSWORD,
            "--gt06-port", str(gt06_port),
            "--gt06-interval", "10",
            "--gt06-id-prefix", "G",
        ],
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        cwd=str(data_dir),
    )

    if not _wait_for_server("127.0.0.1", port):
        # Server didn't start - read log and fail
        log_fh.flush()
        log_content = log_path.read_text()
        proc.kill()
        log_fh.close()
        pytest.fail(f"Server failed to start within timeout.\nServer log:\n{log_content}")

    info = ServerInfo(
        host="127.0.0.1",
        port=port,
        data_dir=data_dir,
        manager_password=MANAGER_PASSWORD,
        process=proc,
        log_file=log_path,
        gt06_port=gt06_port,
    )

    yield info

    # Teardown
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)
    log_fh.close()


@pytest.fixture
def udp_client(server):
    """Function-scoped UDP client."""
    client = UDPClient(server.host, server.port)
    yield client
    client.close()


@pytest.fixture
def http_client(server):
    """Function-scoped HTTP client."""
    return HTTPClient(f"http://{server.host}:{server.port}")


def make_packet(eid=1, **overrides):
    """Generate a valid position packet with auto-incrementing sq and ts."""
    global _seq_counter, _ts_counter
    _seq_counter += 1
    _ts_counter += 1

    packet = {
        "id": "S01",
        "sq": _seq_counter,
        "ts": _ts_counter,
        "lat": -36.8485,
        "lon": 174.7633,
        "spd": 12.5,
        "hdg": 275,
        "ast": False,
        "bat": 85,
        "sig": 3,
        "role": "sailor",
        "ver": "test",
        "eid": eid,
        "did": "test_device_001",
    }
    packet.update(overrides)
    return packet


def make_idle_packet(eid=1, **overrides):
    """Generate a valid idle heartbeat packet with auto-incrementing sq and ts."""
    global _seq_counter, _ts_counter
    _seq_counter += 1
    _ts_counter += 1

    packet = {
        "id": "S01",
        "sq": _seq_counter,
        "ts": _ts_counter,
        "idle": True,
        "bat": 85,
        "sig": 3,
        "role": "sailor",
        "ver": "test",
        "eid": eid,
    }
    packet.update(overrides)
    return packet


def create_event(http_client, name="Test Event", admin_password="admin123",
                 tracker_password="", **kwargs):
    """Create an event via HTTP API. Returns the event ID."""
    data = {
        "name": name,
        "admin_password": admin_password,
        "tracker_password": tracker_password,
        **kwargs,
    }
    status, body = http_client.post("/api/manage/event", data=data,
                                     headers={"X-Manager-Password": MANAGER_PASSWORD})
    assert status == 200, f"Failed to create event: {body}"
    return body["eid"]


@pytest.fixture(scope="session")
def sample_data_dir(tmp_path_factory):
    """Decompress sample data files into a temp directory."""
    out_dir = tmp_path_factory.mktemp("sample_data")

    for gz_file in SAMPLE_DATA_DIR.glob("*.gz"):
        out_name = gz_file.stem  # removes .gz
        out_path = out_dir / out_name
        with gzip.open(gz_file, "rb") as f_in:
            out_path.write_bytes(f_in.read())

    return out_dir
