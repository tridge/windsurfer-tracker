#!/usr/bin/env python3
"""End-to-end SITL regression test for GNSS power state and retry handling."""

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


def main():
    if not os.path.exists(SITL):
        raise SystemExit("build first: make sitl")

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    env = dict(os.environ)
    env["SITL_GNSS_POWER_FAILS"] = "2"
    proc = subprocess.Popen(
        [
            SITL,
            "--server",
            f"127.0.0.1:{port}",
            "--teriid",
            "888800000000125",
            "--no-reexec",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

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

    print("GPS STATUS state + 10-second power retry PASS")


if __name__ == "__main__":
    main()
