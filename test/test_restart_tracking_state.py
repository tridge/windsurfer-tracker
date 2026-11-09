"""A server restart must keep each GT06 unit's tracking/idle state, both for
event-wide start-all/stop-all and for per-unit start/stop overrides."""

import json
import signal
import subprocess
import sys
import time

import pytest

from conftest import (GT06Client, HTTPClient, SERVER_SCRIPT, MANAGER_PASSWORD,
                      DEFAULT_ADMIN_PASSWORD, _find_free_port, _wait_for_server)

ADMIN = {"X-Admin-Password": DEFAULT_ADMIN_PASSWORD}
ACTIVE_TIMER = "TIMER,10,10#"
IDLE_TIMER = "TIMER,60,1800#"


class RestartableServer:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.html = data_dir / "html"
        self.html.mkdir()
        (self.html / "index.html").write_text("<html></html>")
        self.events = data_dir / "events.json"
        self.events.write_text(json.dumps({
            "next_eid": 2, "manager_password": MANAGER_PASSWORD,
            "events": {"1": {"name": "Restart", "admin_password": DEFAULT_ADMIN_PASSWORD,
                             "tracker_password": "", "timezone": "Pacific/Auckland",
                             "archived": False}}}))
        self.gt06_cfg = data_dir / "gt06.json"
        self.gt06_cfg.write_text(json.dumps({"default_eid": 1}))
        self.port = _find_free_port()
        self.gt06_port = _find_free_port(tcp=True)
        self.proc = None
        self.log = open(data_dir / "server.log", "a")

    def start(self):
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER_SCRIPT), "--port", str(self.port),
             "--static-dir", str(self.html), "--events-file", str(self.events),
             "--manager-password", MANAGER_PASSWORD, "--gt06-port", str(self.gt06_port),
             "--gt06-interval", "10", "--gt06-id-prefix", "G",
             "--gt06-config", str(self.gt06_cfg),
             "--jt808-port", str(_find_free_port(tcp=True))],
            stdout=self.log, stderr=subprocess.STDOUT, cwd=str(self.data_dir))
        assert _wait_for_server("127.0.0.1", self.port), (self.data_dir / "server.log").read_text()
        self.http = HTTPClient(f"http://127.0.0.1:{self.port}")

    def stop(self):
        self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()

    def restart(self):
        self.stop()
        self.start()


@pytest.fixture
def srv(tmp_path):
    s = RestartableServer(tmp_path)
    s.start()
    yield s
    s.stop()
    s.log.close()


def connect(srv, imei):
    c = GT06Client("127.0.0.1", srv.gt06_port)
    c.connect()
    cmds = c.recv_all_queued_commands(initial_frames=c.send_login(imei))
    c.send_location(minute=0, second=1)
    c.drain()
    return c, cmds


def login_mode(srv, imei):
    """Fresh connection after a restart: which TIMER does the server send?"""
    c, cmds = connect(srv, imei)
    c.close()
    if ACTIVE_TIMER in cmds:
        return "tracking"
    if IDLE_TIMER in cmds:
        return "idle"
    return f"unknown {cmds}"


A, B = "863874081700001", "863874081700002"   # G700001, G700002


def setup_units(srv):
    for imei in (A, B):
        c, _ = connect(srv, imei)
        c.close()
    time.sleep(0.3)


@pytest.mark.parametrize("event_cmd,want", [("start-all", "tracking"), ("stop-all", "idle")])
def test_event_wide_state_survives_restart(srv, event_cmd, want):
    setup_units(srv)
    st, _ = srv.http.post(f"/api/event/1/admin/{event_cmd}", headers=ADMIN)
    assert st == 200
    time.sleep(0.5)
    srv.restart()
    assert login_mode(srv, A) == want
    assert login_mode(srv, B) == want


@pytest.mark.parametrize("event_cmd,unit_cmd,other", [
    ("stop-all", "start", ("tracking", "idle")),   # event idle, A started alone
    ("start-all", "stop", ("idle", "tracking")),   # event tracking, A stopped alone
])
def test_per_unit_override_survives_restart(srv, event_cmd, unit_cmd, other):
    setup_units(srv)
    assert srv.http.post(f"/api/event/1/admin/{event_cmd}", headers=ADMIN)[0] == 200
    time.sleep(0.3)
    assert srv.http.post(f"/api/event/1/admin/{unit_cmd}/G700001", headers=ADMIN)[0] == 200
    time.sleep(0.5)
    assert (login_mode(srv, A), login_mode(srv, B)) == other   # reconnect, no restart
    srv.restart()
    assert (login_mode(srv, A), login_mode(srv, B)) == other
