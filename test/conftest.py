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
                 gt06_port=None, jt808_port=None):
        self.host = host
        self.port = port
        self.data_dir = data_dir
        self.manager_password = manager_password
        self.process = process
        self.log_file = log_file
        self.gt06_port = gt06_port
        self.jt808_port = jt808_port


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

    def send_command_ack(self, cmd_text=""):
        """Send a 0x15 command response frame to ACK a server command."""
        # data: content_len(1) + server_flag(4) + cmd_text
        cmd_bytes = cmd_text.encode("ascii")
        data = struct.pack(">B4s", len(cmd_bytes) + 4, b"\x00\x00\x00\x00") + cmd_bytes
        frame = self._build_frame(0x15, data)
        self.sock.sendall(frame)

    def recv_all_queued_commands(self, initial_frames=None, timeout=0.5):
        """Receive all queued commands, ACKing each one to advance the queue.

        Args:
            initial_frames: frames already received (e.g. from send_login())
                that may contain command frames needing ACKs.
            timeout: socket receive timeout per round.

        Returns list of command text strings. Sends a 0x15 ACK for each
        0x80 command frame to trigger the server to send the next queued command.
        """
        all_cmds = []
        if initial_frames:
            cmds = self.get_commands(initial_frames)
            all_cmds.extend(cmds)
            if cmds:
                self.send_command_ack(cmds[-1])
        while True:
            frames = self._recv_frames(timeout=timeout)
            cmds = self.get_commands(frames)
            if not cmds:
                break
            all_cmds.extend(cmds)
            self.send_command_ack(cmds[-1])
        return all_cmds

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


# Add path so we can import protocol modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from protocol_JT808 import (jt808_escape, jt808_unescape, jt808_checksum,
                              jt808_build_frame, jt808_parse_header,
                              phone_bcd_to_imei, imei_to_phone_bcd,
                              parse_location as jt808_parse_location)


class JT808Client:
    """TCP client emulator for JT808 GPS tracker protocol.

    Builds and sends binary JT808 frames (registration, auth, location, heartbeat)
    and receives/parses server responses.
    """

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.sock = None
        self._serial = 0
        self.phone_bcd = None
        self.auth_code = None

    def connect(self):
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

    def _recv_frames(self, timeout=0.5):
        """Receive all available JT808 frames from the server within timeout."""
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
                while True:
                    start = buf.find(b"\x7e")
                    if start < 0:
                        buf = b""
                        break
                    if start > 0:
                        buf = buf[start:]
                    end = buf.find(b"\x7e", 1)
                    if end < 0:
                        break
                    if end == 1:
                        buf = buf[1:]
                        continue
                    frame_raw = buf[1:end]
                    buf = buf[end:]
                    # Decode the frame
                    data = jt808_unescape(frame_raw)
                    if len(data) >= 13:
                        cs = data[-1]
                        if jt808_checksum(data[:-1]) == cs:
                            frames.append(data[:-1])  # header + body without checksum
                    # Continue to see if there's another frame after the delimiter
        except socket.timeout:
            pass
        return frames

    def _parse_frame(self, data):
        """Parse a decoded frame into (msg_id, phone_bcd, serial, body)."""
        parsed = jt808_parse_header(data)
        if parsed is None:
            return None
        msg_id, attributes, phone_bcd, serial, body_offset = parsed
        body_len = attributes & 0x03FF
        body = data[body_offset:body_offset + body_len]
        return msg_id, phone_bcd, serial, body

    def send_registration(self, imei="862831041694915"):
        """Send terminal registration (0x0100) and return received frames.

        Sends a minimal registration body.
        """
        self.phone_bcd = imei_to_phone_bcd(imei)
        # Minimal registration body:
        # province(2) + city(2) + manufacturer(5) + terminal_type(20) + terminal_id(7) + plate_color(1)
        body = b"\x00\x00"  # province
        body += b"\x00\x00"  # city
        body += b"TRACK"    # manufacturer (5 bytes)
        body += b"\x00" * 20  # terminal type (20 bytes)
        body += b"TRACKER"   # terminal ID (7 bytes)
        body += b"\x00"      # plate color
        frame = jt808_build_frame(0x0100, self.phone_bcd, self._next_serial(), body)
        self.sock.sendall(frame)
        frames = self._recv_frames()
        # Extract auth_code from registration response
        for f in frames:
            parsed = self._parse_frame(f)
            if parsed and parsed[0] == 0x8100:
                resp_body = parsed[3]
                if len(resp_body) >= 3 and resp_body[2] == 0:  # result == success
                    self.auth_code = resp_body[3:].decode("ascii", errors="replace")
        return frames

    def send_auth(self, auth_code=None):
        """Send terminal authentication (0x0102) and return received frames."""
        code = auth_code or self.auth_code or ""
        body = code.encode("ascii")
        frame = jt808_build_frame(0x0102, self.phone_bcd, self._next_serial(), body)
        self.sock.sendall(frame)
        return self._recv_frames()

    def send_login(self, imei="862831041694915"):
        """Full login: registration + authentication. Returns all frames."""
        reg_frames = self.send_registration(imei)
        auth_frames = self.send_auth()
        return reg_frames + auth_frames

    def build_location_body(self, lat=-35.2999, lon=149.1003, speed_kmh_10=0,
                             heading=180, alarm_flags=0,
                             gps_valid=True, satellites=8,
                             year=26, month=2, day=21, hour=4, minute=0, second=0,
                             battery=None, signal=None, charging=None):
        """Build a 0x0200 location report body.

        speed_kmh_10: speed in 1/10 km/h units
        """
        # Status bits
        status = 0
        if gps_valid:
            status |= (1 << 1)   # bit 1: positioning
        if lat < 0:
            status |= (1 << 2)   # bit 2: south
        if lon < 0:
            status |= (1 << 3)   # bit 3: west

        lat_raw = int(abs(lat) * 1_000_000)
        lon_raw = int(abs(lon) * 1_000_000)

        # BCD time (GMT+8 — we pass the time in GMT+8 from test)
        def to_bcd(val):
            return ((val // 10) << 4) | (val % 10)

        body = struct.pack(">I", alarm_flags)
        body += struct.pack(">I", status)
        body += struct.pack(">I", lat_raw)
        body += struct.pack(">I", lon_raw)
        body += struct.pack(">H", 0)  # altitude
        body += struct.pack(">H", speed_kmh_10)
        body += struct.pack(">H", heading)
        body += bytes([to_bcd(year), to_bcd(month), to_bcd(day),
                        to_bcd(hour), to_bcd(minute), to_bcd(second)])

        # Optional TLVs
        if signal is not None:
            body += bytes([0x30, 0x01, signal])
        if satellites is not None:
            body += bytes([0x31, 0x01, satellites])
        if battery is not None:
            chg_byte = 0 if charging else 1
            body += bytes([0xE4, 0x02, chg_byte, battery])

        return body

    def send_location(self, **kwargs):
        """Send a location packet. Returns list of received frames."""
        body = self.build_location_body(**kwargs)
        frame = jt808_build_frame(0x0200, self.phone_bcd, self._next_serial(), body)
        self.sock.sendall(frame)
        return self._recv_frames(timeout=0.1)

    def send_heartbeat(self):
        """Send heartbeat (0x0002). Returns list of received frames."""
        frame = jt808_build_frame(0x0002, self.phone_bcd, self._next_serial())
        self.sock.sendall(frame)
        return self._recv_frames()

    def drain(self, timeout=0.1):
        """Read and discard any pending data from the socket."""
        self.sock.settimeout(timeout)
        try:
            while self.sock.recv(4096):
                pass
        except socket.timeout:
            pass


@pytest.fixture
def jt808_client(server):
    """Function-scoped JT808 TCP client."""
    client = JT808Client(server.host, server.jt808_port)
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
    jt808_port = _find_free_port(tcp=True)

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
            "--jt808-port", str(jt808_port),
            "--jt808-interval", "10",
            "--jt808-id-prefix", "J",
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
        jt808_port=jt808_port,
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
