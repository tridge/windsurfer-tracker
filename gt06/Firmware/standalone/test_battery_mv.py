#!/usr/bin/env python3
"""SITL regression for the supervisor battery reading, including a unit on charge.

Live failure this guards against (G378517, 2026-08-21): the tracker was left charging overnight,
reported BT:4486 happily on vendor firmware, and reported BT:0 the moment it was updated to ours.
The M0 measures the charge rail rather than a resting cell, so on charge it climbs past 4.2 V; our
plausibility window stopped at 4500 mV and silently rejected every sample, leaving g_batt_mv at 0.

A full battery reporting as flat is the worst possible direction for this error to fail in -- it is
indistinguishable from a unit about to die, so it invites exactly the wrong response.
"""

import os
import socket
import subprocess
import time

from test_loc_batch import finish_sitl
from sitl_util import server_acks, gt06_cmd as _crc_cmd  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SITL = os.path.abspath(os.environ.get("SITL_BIN") or os.path.join(HERE, "sitl"))


def gt06_cmd(command, serial):
    """Build a GT06 0x80 server-to-device command frame (valid CRC)."""
    return _crc_cmd(command, serial)


def read_bt(mv, timeout=25):
    """Boot SITL with the M0 reporting `mv`, then ask for cxzt# and return its *BT: value."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    env = dict(os.environ)
    env["SITL_BATT_MV"] = str(mv)
    proc = subprocess.Popen(
        [SITL, "--server", f"127.0.0.1:{port}", "--teriid", "888800000000150", "--no-reexec"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)

    conn = None
    try:
        listener.settimeout(8)
        conn, _ = listener.accept()
        buf = bytearray()
        deadline = time.monotonic() + timeout
        conn.sendall(gt06_cmd("cxzt#", 1))
        while time.monotonic() < deadline:
            conn.settimeout(1.0)
            try:
                chunk = conn.recv(4096)
                server_acks(conn, chunk)
            except socket.timeout:
                conn.sendall(gt06_cmd("cxzt#", 2))
                continue
            if not chunk:
                break
            buf += chunk
            if b"*BT:" in buf:
                seg = buf.split(b"*BT:")[1]
                digits = b""
                for c in seg:
                    ch = bytes([c])
                    if ch.isdigit():
                        digits += ch
                    else:
                        break
                if digits:
                    return int(digits)
        return None
    finally:
        if conn is not None:
            conn.close()
        listener.close()
        finish_sitl(proc)


def main():
    if not os.path.exists(SITL):
        raise SystemExit("build first: make sitl")

    # a resting cell
    bt = read_bt(4100)
    assert bt == 4100, f"resting battery reported as {bt}, expected 4100"
    print(f"resting 4100 mV -> BT:{bt} PASS")

    # ON CHARGE: above a resting cell's 4.2 V, and above the old 4500 ceiling that broke G378517
    bt = read_bt(4486)
    assert bt == 4486, f"charging battery reported as {bt}, expected 4486"
    print(f"charging 4486 mV -> BT:{bt} PASS")

    bt = read_bt(4700)
    assert bt == 4700, (
        f"charging battery reported as {bt} — a real reading above the old 4500 limit was rejected, "
        "which is what made a fully charged unit report 0%")
    print(f"charging 4700 mV -> BT:{bt} PASS")

    # G378517 on charge (2026-11-08) read above the 5500 ceiling and showed BT:0
    bt = read_bt(5800)
    assert bt == 5800, f"charging battery at 5800 mV reported as {bt}"
    print(f"charging 5800 mV -> BT:{bt} PASS")

    # framing garbage must still be rejected rather than published
    bt = read_bt(9000)
    assert bt in (0, None), f"implausible {9000} mV was published as BT:{bt}"
    print("implausible 9000 mV rejected PASS")

    print("battery mV window covers charging without accepting garbage")


if __name__ == "__main__":
    main()
