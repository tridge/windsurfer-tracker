#!/usr/bin/env python3
"""End-to-end SITL test for direct CPSI telemetry and GPS-safe timed idle sleep."""

import os
import socket
import subprocess
import time

from test_gps_retry import gt06_cmd, receive_until
from test_loc_batch import finish_sitl
from sitl_util import server_acks, gt06_cmd as _crc_cmd  # noqa: E402


HERE = os.path.dirname(os.path.abspath(__file__))
SITL = os.environ.get("SITL_BIN") or os.path.join(HERE, "sitl")


def wait_for_close(conn, message):
    deadline = time.monotonic() + 10
    conn.settimeout(1)
    while time.monotonic() < deadline:
        try:
            chunk = conn.recv(4096)
            server_acks(conn, chunk)
            if chunk == b"":
                return
        except socket.timeout:
            continue
    raise AssertionError(message)


def test_fault_readback_and_recovery(injection, closes_before_failure):
    """Each failed sleep API reads disabled, and an explicit set clears the latch."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)
    port = listener.getsockname()[1]
    env = os.environ.copy()
    env[injection] = "1"
    proc = subprocess.Popen(
        [SITL, "--server", f"127.0.0.1:{port}", "--teriid",
         "888800000000177", "--no-reexec"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )

    conn = fault_reconnect = reconnect = None
    try:
        listener.settimeout(8)
        conn, _ = listener.accept()
        received = bytearray()
        receive_until(conn, received, b"\x78\x78", 5)
        conn.sendall(gt06_cmd("SZCS#SLPDISCONNECT=1", 10))
        receive_until(conn, received, b"SETOK:SLPDISCONNECT=1", 5)
        conn.sendall(gt06_cmd("HBT,1,1#", 11))
        receive_until(conn, received, b"HBT,1,1# OK", 5)

        # A wake-alarm failure leaves TCP open. A sleep-enable failure happens after the successfully
        # armed alarm has caused TCP to close, so first prove the immediate reconnect in that branch.
        if closes_before_failure:
            wait_for_close(conn, "sleep-enable failure did not close the GT06 connection")
            listener.settimeout(8)
            fault_reconnect, _ = listener.accept()
            received = bytearray()
            receive_until(fault_reconnect, received, b"\x78\x78", 5)
            active = fault_reconnect
        else:
            time.sleep(6)
            active = conn

        # The effective setting must read disabled so server reconciliation can see the failure instead
        # of believing that night sleep is active, regardless of which API rejected the attempt.
        active.sendall(gt06_cmd("CXCS#SLPDISCONNECT", 12))
        receive_until(active, received, b"READOK:SLPDISCONNECT=0", 5)

        # Re-applying the desired setting clears the fault latch. Prove both the immediate readback and
        # the eventual successful close/sleep/reconnect rather than merely checking a SETOK response.
        active.sendall(gt06_cmd("SZCS#SLPDISCONNECT=1", 13))
        receive_until(active, received, b"SETOK:SLPDISCONNECT=1", 5)
        active.sendall(gt06_cmd("CXCS#SLPDISCONNECT", 14))
        receive_until(active, received, b"READOK:SLPDISCONNECT=1", 5)

        wait_for_close(active, "recovered sleep path did not close the GT06 connection")

        listener.settimeout(8)
        reconnect, _ = listener.accept()
        after = bytearray()
        receive_until(reconnect, after, b"\x78\x78", 5)
        reconnect.sendall(gt06_cmd("STATUS#", 15))
        receive_until(reconnect, after, b"GPS:OFF", 5)
    finally:
        if reconnect is not None:
            reconnect.close()
        if fault_reconnect is not None:
            fault_reconnect.close()
        if conn is not None:
            conn.close()
        listener.close()
        stdout, stderr = finish_sitl(proc)

    assert b"SLEEP: disabled after API failure" in stdout, stdout.decode(errors="replace")
    marker = (b"[SITL] injected system sleep failure" if closes_before_failure
              else b"[SITL] injected wake alarm failure")
    assert marker in stderr, stderr.decode(errors="replace")
    assert b"[SITL] wake alarm 2000 ms" in stderr, stderr.decode(errors="replace")
    assert b"SLEEP: awake, GPS remains off" in stdout, stdout.decode(errors="replace")


def main():
    if not os.path.exists(SITL):
        raise SystemExit("build first: make sitl")

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)
    port = listener.getsockname()[1]
    proc = subprocess.Popen(
        [SITL, "--server", f"127.0.0.1:{port}", "--teriid",
         "888800000000176", "--no-reexec"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )

    conn = reconnect = None
    try:
        listener.settimeout(8)
        conn, _ = listener.accept()
        received = bytearray()
        receive_until(conn, received, b"\x78\x78", 5)

        conn.sendall(gt06_cmd("STATUS#", 1))
        receive_until(
            conn, received,
            b"LTE:505-02|EUTRAN-BAND3|52200|36519691|101|-102|-12|-75|8;", 5)

        conn.sendall(gt06_cmd("CXCS#SLPDISCONNECT", 2))
        receive_until(conn, received, b"READOK:SLPDISCONNECT=0", 5)
        conn.sendall(gt06_cmd("SZCS#SLPDISCONNECT=1", 3))
        receive_until(conn, received, b"SETOK:SLPDISCONNECT=1", 5)
        conn.sendall(gt06_cmd("HBT,1,1#", 4))
        receive_until(conn, received, b"HBT,1,1# OK", 5)

        # Five quiet seconds, then the firmware closes TCP before sleeping. EOF proves it did not leave
        # a half-open connection for the server to mistake as reachable.
        deadline = time.monotonic() + 10
        conn.settimeout(1)
        while time.monotonic() < deadline:
            try:
                chunk = conn.recv(4096)
                server_acks(conn, chunk)
                if chunk == b"":
                    break
            except socket.timeout:
                continue
        else:
            raise AssertionError("timed sleep did not close the GT06 connection")

        listener.settimeout(8)
        reconnect, _ = listener.accept()
        after = bytearray()
        receive_until(reconnect, after, b"\x78\x78", 5)
        reconnect.sendall(gt06_cmd("STATUS#", 5))
        receive_until(reconnect, after, b"GPS:OFF", 5)
    finally:
        if reconnect is not None:
            reconnect.close()
        if conn is not None:
            conn.close()
        listener.close()
        stdout, stderr = finish_sitl(proc)

    assert b"[SITL] wake alarm 2000 ms" in stderr, stderr.decode(errors="replace")
    assert b"[SITL] system sleep enabled" in stderr, stderr.decode(errors="replace")
    assert b"[SITL] system sleep disabled" in stderr, stderr.decode(errors="replace")
    assert b"SLEEP: awake, GPS remains off" in stdout, stdout.decode(errors="replace")
    test_fault_readback_and_recovery("SITL_SLEEP_ALARM_FAILS", False)
    test_fault_readback_and_recovery("SITL_SLEEP_SET_FAILS", True)
    print("direct CPSI STATUS + GPS-safe disconnected sleep + fault recovery PASS")


if __name__ == "__main__":
    main()
