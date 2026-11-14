"""Shared helpers for the SITL reliability tests: a minimal GT06 server side."""

import os
import re
import socket
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SITL = os.path.abspath(os.environ.get("SITL_BIN") or os.path.join(HERE, "sitl"))
RUN_DIR = os.environ.get("SITL_RUN_DIR") or HERE   # where SITL keeps its state files


# ---- simulated clock -------------------------------------------------------------------------------
# SITL_TIME_SCALE=N makes the SITL run N x faster than real time (sitl_hal.c divides every HAL wait by N).
# The tests are written in SIM seconds, so this module rescales the harness's own clock to match:
# time.monotonic/time.time/time.gmtime advance N x, and time.sleep / socket timeouts take sim seconds.
# SITL_TIME_ANCHOR is exported so every SITL child (and its re-execs) maps real->sim UTC identically.
# subprocess/threading bind the original clock at import, so process-exit timeouts stay real.
TIME_SCALE = float(os.environ.get("SITL_TIME_SCALE") or 1)


def _install_time_scale(scale):
    real_mono, real_time, real_sleep, real_gmtime = time.monotonic, time.time, time.sleep, time.gmtime
    real_localtime = time.localtime
    anchor = float(os.environ.get("SITL_TIME_ANCHOR") or real_time())
    os.environ["SITL_TIME_ANCHOR"] = repr(anchor)
    m0 = real_mono()
    time.monotonic = lambda: m0 + (real_mono() - m0) * scale
    time.time = lambda: anchor + (real_time() - anchor) * scale
    time.sleep = lambda s: real_sleep(max(0.0, s) / scale)
    time.gmtime = lambda secs=None: real_gmtime(time.time() if secs is None else secs)
    time.localtime = lambda secs=None: real_localtime(time.time() if secs is None else secs)
    real_settimeout = socket.socket.settimeout

    def settimeout(self, value):
        real_settimeout(self, None if value is None else value / scale)
    socket.socket.settimeout = settimeout


if TIME_SCALE != 1:
    _install_time_scale(TIME_SCALE)


def crc_itu(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def frame(proto, payload, serial):
    body = bytes([len(payload) + 5, proto]) + payload + serial.to_bytes(2, "big")
    return b"\x78\x78" + body + crc_itu(body).to_bytes(2, "big") + b"\r\n"


def gt06_cmd(command, serial, crc_ok=True):
    """0x80 server command, with a valid CRC unless crc_ok is False."""
    text = command.encode()
    f = bytearray(frame(0x80, bytes([4 + len(text)]) + b"\0\0\0\0" + text, serial))
    if not crc_ok:
        f[-4] ^= 0xFF
    return bytes(f)


def ack(proto, serial):
    """Server ACK for login/heartbeat/alarm: 78 78 05 proto serial crc 0d 0a."""
    return frame(proto, b"", serial)


def frames(buf):
    """Split a byte stream into (proto, payload, serial) tuples; returns (list, remainder)."""
    out, i = [], 0
    while i + 5 <= len(buf):
        if buf[i:i + 2] != b"\x78\x78":
            i += 1
            continue
        total = buf[i + 2] + 5
        if i + total > len(buf):
            break
        f = buf[i:i + total]
        out.append((f[3], bytes(f[4:-6]), int.from_bytes(f[-6:-4], "big")))
        i += total
    return out, buf[i:]


class Server:
    """Listening socket plus helpers to accept a SITL connection and talk GT06 to it."""

    def __init__(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.port = self.listener.getsockname()[1]
        self.conn = None
        self.buf = b""
        self.seen = []          # every (proto, payload, serial) received on the current conn
        self.auto_ack = True    # ACK login/heartbeat/alarm like the real server
        self.ack_delay = 0.0    # seconds to hold each ACK (a slow link)
        self._delayed = []
        self.serial = 100

    def stop_listening(self):
        """Refuse connections (server down) until reopen()."""
        if self.conn:
            self.conn.close()
            self.conn = None
        self.listener.close()

    def reopen(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", self.port))
        self.listener.listen(4)

    def accept(self, timeout=15):
        self.listener.settimeout(timeout)
        if self.conn:
            self.conn.close()
        self.conn, _ = self.listener.accept()
        self.buf = b""
        self.seen = []
        self._delayed = []
        return self.conn

    def accept_login(self, timeout=30):
        """Accept until a connection actually logs in (skips ones that die or stay silent)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.accept(max(1, deadline - time.monotonic()))
            end = time.monotonic() + 5
            while time.monotonic() < end:
                alive = self.pump(0.2)
                if any(s[0] == 0x01 for s in self.seen):
                    return self.conn
                if not alive:
                    break
        raise AssertionError("no logged-in connection")

    def wait_closed(self, timeout):
        """Pump until the device closes the connection (e.g. it rebooted)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.pump(0.5):
                return
        raise AssertionError("device did not close the connection")

    def pump(self, seconds):
        """Read for `seconds`, ACKing as configured. Returns False if the peer closed."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            now = time.monotonic()
            while self._delayed and self._delayed[0][0] <= now:
                try:
                    self.conn.sendall(self._delayed.pop(0)[1])
                except OSError:
                    return False
            self.conn.settimeout(max(0.01, min(0.2, deadline - time.monotonic())))
            try:
                chunk = self.conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return False
            if not chunk:
                return False
            self.buf += chunk
            got, self.buf = frames(self.buf)
            for proto, payload, serial in got:
                self.seen.append((proto, payload, serial, time.monotonic()))
                if self.auto_ack and proto in (0x01, 0x13, 0x16):
                    if self.ack_delay and proto != 0x01:
                        self._delayed.append((time.monotonic() + self.ack_delay, ack(proto, serial)))
                    else:
                        self.conn.sendall(ack(proto, serial))
        return True

    def send_cmd(self, command, crc_ok=True):
        self.serial += 1
        self.conn.sendall(gt06_cmd(command, self.serial, crc_ok))

    def replies(self):
        return [p[5:].decode(errors="replace") for proto, p, _, _ in self.seen if proto == 0x15]

    def wait_reply(self, pattern, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for r in self.replies():
                m = re.search(pattern, r)
                if m:
                    return m
            if not self.pump(0.3):
                raise AssertionError(f"connection closed waiting for {pattern!r}")
        raise AssertionError(f"timed out waiting for {pattern!r}; replies={self.replies()}")

    def query(self, command, pattern, timeout=10):
        self.seen = [s for s in self.seen if s[0] != 0x15]
        self.send_cmd(command)
        return self.wait_reply(pattern, timeout)

    def close(self):
        if self.conn:
            self.conn.close()
        self.listener.close()


def start(server, teriid, env_extra=None, reexec=False, cwd=None):
    env = dict(os.environ)
    env.update(env_extra or {})
    args = [SITL, "--server", f"127.0.0.1:{server.port}", "--teriid", teriid]
    if not reexec:
        args.append("--no-reexec")
    return subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=cwd or RUN_DIR)


def finish(proc):
    """Stop SITL; fail on a sanitizer report. Returns (stdout, stderr)."""
    proc.terminate()
    try:
        stdout, stderr = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate(timeout=5)
    if b"AddressSanitizer" in stderr or b"runtime error" in stderr:
        raise AssertionError("sanitizer reported a fault:\n" + stderr.decode(errors="replace")[-3000:])
    return stdout, stderr


def reset_state():
    for name in ("sitl_wdstat.bin", "sitl_wdtrace.bin"):
        try:
            os.unlink(os.path.join(RUN_DIR, name))
        except FileNotFoundError:
            pass


_ack_bufs = {}


def server_acks(conn, chunk):
    """ACK login/heartbeat/alarm frames in `chunk` like the real server (stateful per connection)."""
    key = id(conn)
    buf = _ack_bufs.get(key, b"") + chunk
    got, buf = frames(buf)
    _ack_bufs[key] = buf[-512:]
    for proto, _, serial in got:
        if proto in (0x01, 0x13, 0x16):
            try:
                conn.sendall(ack(proto, serial))
            except OSError:
                pass
