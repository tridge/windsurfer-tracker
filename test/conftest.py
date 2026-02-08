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
    def __init__(self, host, port, data_dir, manager_password, process, log_file):
        self.host = host
        self.port = port
        self.data_dir = data_dir
        self.manager_password = manager_password
        self.process = process
        self.log_file = log_file


class UDPClient:
    """Simple UDP client for sending tracker packets."""

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(2.0)

    def send_position(self, packet_dict):
        """Send a JSON position packet and return the ACK dict."""
        data = json.dumps(packet_dict).encode("utf-8")
        self.sock.sendto(data, (self.host, self.port))
        resp_data, _ = self.sock.recvfrom(4096)
        return json.loads(resp_data.decode("utf-8"))

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


def _find_free_port():
    """Find a free port by binding to port 0."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
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
