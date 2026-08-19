#!/usr/bin/env python3
"""End-to-end SITL regression tests for GNSS power state and retry handling.

Covers both directions: retrying power-ON while tracking, and retrying power-OFF
until the resident confirms it while idle (a failed power-off would otherwise leave
GNSS drawing current for the whole idle period).
"""

import os
import socket
import subprocess
import time


HERE = os.path.dirname(os.path.abspath(__file__))
SITL = os.environ.get("SITL_BIN") or os.path.join(HERE, "sitl")


def gt06_cmd(command, serial):
    """Build a GT06 0x80 server-to-device command frame."""
    text = command.encode()
    length = 10 + len(text)
    body = (
        bytes([0x80, 4 + len(text)])
        + b"\x00\x00\x00\x00"
        + text
        + serial.to_bytes(2, "big")
        + b"\x00\x00"
    )
    return b"\x78\x78" + bytes([length]) + body + b"\x0d\x0a"


def receive_until(conn, received, marker, timeout):
    deadline = time.monotonic() + timeout
    while marker not in received and time.monotonic() < deadline:
        conn.settimeout(min(0.5, max(0.01, deadline - time.monotonic())))
        try:
            chunk = conn.recv(4096)
        except socket.timeout:
            continue
        if not chunk:
            raise AssertionError("SITL closed its GT06 connection")
        received.extend(chunk)
    if marker not in received:
        raise AssertionError(f"timed out waiting for {marker!r}")


def start_sitl(listener_port, env_extra):
    env = dict(os.environ)
    env.update(env_extra)
    return subprocess.Popen(
        [SITL, "--server", f"127.0.0.1:{listener_port}", "--teriid",
         "888800000000125", "--no-reexec"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )


def test_power_on_retry():
    if not os.path.exists(SITL):
        raise SystemExit("build first: make sitl")

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    proc = start_sitl(port, {"SITL_GNSS_POWER_FAILS": "2"})

    conn = None
    try:
        listener.settimeout(8)
        conn, _ = listener.accept()
        received = bytearray()
        receive_until(conn, received, b"\x78\x78", 5)

        conn.sendall(gt06_cmd("TIMER,1,1#", 1))
        receive_until(conn, received, b"TIMER,1,1# OK", 5)

        # The first injected power-on failure must not make STATUS claim that GPS is on.
        conn.sendall(gt06_cmd("STATUS#", 2))
        receive_until(conn, received, b"GPS:OFF", 5)

        # Immediate start and the first 10-second retry fail; the second retry succeeds at ~20 seconds.
        time.sleep(21)
        conn.sendall(gt06_cmd("STATUS#", 3))
        receive_until(conn, received, b"GPS:ON", 5)
    finally:
        if conn is not None:
            conn.close()
        listener.close()
        proc.terminate()
        try:
            stdout, stderr = proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate(timeout=3)

    if stdout.count(b"ST: GPS power on FAILED") != 2:
        raise AssertionError(stdout.decode(errors="replace"))
    if b"ST: GPS on (tracking)" not in stdout:
        raise AssertionError(stdout.decode(errors="replace"))
    if stderr.count(b"GNSS power-on failure injected") != 2:
        raise AssertionError(stderr.decode(errors="replace"))

    print("GPS power-ON retry + truthful STATUS PASS")


def test_power_off_retry():
    """Idle boot with a failing power-off must keep retrying until the resident confirms OFF,
    and must then stop (no pointless calls for the rest of the idle period)."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    # Boot is idle, so apply_gps(0) runs immediately; fail it twice.
    proc = start_sitl(port, {"SITL_GNSS_POWEROFF_FAILS": "2"})

    conn = None
    try:
        listener.settimeout(8)
        conn, _ = listener.accept()
        received = bytearray()
        receive_until(conn, received, b"\x78\x78", 5)

        # A failed power-off must NOT be reported as OFF.
        conn.sendall(gt06_cmd("STATUS#", 1))
        receive_until(conn, received, b"GPS:UNKNOWN", 5)

        # Boot attempt and the first 10 s retry fail; the second retry succeeds at ~20 s.
        time.sleep(21)
        conn.sendall(gt06_cmd("STATUS#", 2))
        receive_until(conn, received, b"GPS:OFF", 5)
        settled = time.monotonic()

        # Once confirmed OFF it must stop retrying.
        time.sleep(12)
    finally:
        if conn is not None:
            conn.close()
        listener.close()
        proc.terminate()
        try:
            stdout, stderr = proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate(timeout=3)

    if stdout.count(b"ST: GPS power off FAILED") != 2:
        raise AssertionError(stdout.decode(errors="replace"))
    if stderr.count(b"GNSS power-off failure injected") != 2:
        raise AssertionError(stderr.decode(errors="replace"))
    # exactly one success, and no further calls in the ~12 s after it settled
    if stdout.count(b"ST: GPS off (idle)") != 1:
        raise AssertionError(
            "expected exactly one confirmed power-off, got "
            f"{stdout.count(b'ST: GPS off (idle)')}\n"
            + stdout.decode(errors="replace"))

    print("GPS power-OFF retry-until-confirmed PASS")


def main():
    test_power_on_retry()
    test_power_off_retry()


if __name__ == "__main__":
    main()
