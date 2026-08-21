#!/usr/bin/env python3
"""SITL test for CXCS#GPSSTAT — the GNSS health reply.

Motivation (G226122, 2026-08-21): the unit sat in GPS-wait for 2h22m, through a 25-minute flight with
perfect sky view, and finally locked on the ground. STATUS said GPS:ON the whole time — but that only
means our last power-on call returned 0, since the resident API is a setter with no getter. The batch
message carries satellites/fix/HDOP, yet it is only sent when there ARE fixes, so during a GPS-wait
there was no way to tell a dead receiver from one still acquiring.

GPSSTAT has to separate exactly those two cases:
  nmea == 0            -> nothing at all from the receiver (wedged / wiring / never powered)
  nmea > 0, vrmc == 0  -> receiver alive and talking, but no fix (antenna, signal, or acquiring)

and when the power call itself is failing, api/prc/pf say whether the resident slot is missing
(api0, nothing to call) or present and rejecting us (api1 with a non-zero prc).
"""

import os
import re
import socket
import subprocess
import time

from test_loc_batch import finish_sitl

HERE = os.path.dirname(os.path.abspath(__file__))
SITL = os.path.abspath(os.environ.get("SITL_BIN") or os.path.join(HERE, "sitl"))


def gt06_cmd(command, serial):
    text = command.encode()
    length = 10 + len(text)
    body = (bytes([0x80, 4 + len(text)]) + b"\x00\x00\x00\x00" + text
            + serial.to_bytes(2, "big") + b"\x00\x00")
    return b"\x78\x78" + bytes([length]) + body + b"\x0d\x0a"


def query_gpsstat(env_extra, tracking, settle, timeout=30):
    """Boot SITL, optionally start tracking, then return the parsed GPSSTAT fields."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    env = dict(os.environ)
    env.update(env_extra)
    proc = subprocess.Popen(
        [SITL, "--server", f"127.0.0.1:{port}", "--teriid", "888800000000160", "--no-reexec"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)

    conn = None
    try:
        listener.settimeout(8)
        conn, _ = listener.accept()
        time.sleep(2)
        if tracking:
            conn.sendall(gt06_cmd("TIMER,1,1#", 1))
        time.sleep(settle)

        buf = bytearray()
        deadline = time.monotonic() + timeout
        conn.sendall(gt06_cmd("CXCS#GPSSTAT", 2))
        while time.monotonic() < deadline:
            conn.settimeout(1.0)
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                conn.sendall(gt06_cmd("CXCS#GPSSTAT", 3))
                continue
            if not chunk:
                break
            buf += chunk
            if b"READOK:GPSSTAT" in buf:
                text = buf[buf.index(b"READOK:GPSSTAT"):].decode(errors="replace")
                fields = dict(re.findall(r"([a-z]+)(\d+)", text))
                if "fage" in fields:                      # last field -> reply is complete
                    return {k: int(v) for k, v in fields.items()}, text
        raise AssertionError(f"no GPSSTAT reply\n{buf[-400:]!r}")
    finally:
        if conn is not None:
            conn.close()
        listener.close()
        finish_sitl(proc)


def main():
    if not os.path.exists(SITL):
        raise SystemExit("build first: make sitl")

    # ---- healthy: tracking, GNSS running, fixes flowing ----
    f, text = query_gpsstat({}, tracking=True, settle=8)
    assert f["pwr"] == 2, f"expected power ON, got pwr={f['pwr']}: {text}"
    assert f["nmea"] > 0, f"no NMEA counted while the receiver is running: {text}"
    assert f["vrmc"] > 0, f"no valid RMC while fixes are flowing: {text}"
    assert f["sats"] == 17, f"sats={f['sats']} (injector reports 17): {text}"
    assert f["fix"] == 3, f"fix={f['fix']} (expected 3 = 3D): {text}"
    assert f["hdop"] == 12, f"hdop={f['hdop']} (expected 12 = 1.2): {text}"
    assert f["nage"] < 30 and f["fage"] < 30, f"stale ages on a healthy unit: {text}"
    assert f["api"] == 1, f"resident GPS slot reported missing on a healthy unit: {text}"
    assert f["pf"] == 0, f"power-call failures on a healthy unit: {text}"
    print(f"healthy: nmea={f['nmea']} vrmc={f['vrmc']} sats={f['sats']} fix={f['fix']} "
          f"hdop={f['hdop']} fage={f['fage']} api={f['api']} pf={f['pf']} PASS")

    # ---- receiver never comes up: the "is it dead?" case ----
    f, text = query_gpsstat({"SITL_GNSS_POWER_FAILS": "9999"}, tracking=True, settle=8)
    assert f["nmea"] == 0, f"NMEA counted though GNSS never powered on: {text}"
    assert f["vrmc"] == 0, f"valid RMC without a receiver: {text}"
    assert f["nage"] == 999, f"nage should be 'never' (999), got {f['nage']}: {text}"
    assert f["fage"] == 999, f"fage should be 'never' (999), got {f['fage']}: {text}"
    assert f["pwr"] != 2, f"power reported ON though every power call failed: {text}"
    # the whole point of the extra fields: say WHY it failed, not just that it did
    assert f["api"] == 1, f"slot was present, so api should be 1: {text}"
    assert f["pf"] > 0, f"failed power calls not counted: {text}"
    assert "prc-" in text, f"the resident's error return is not reported: {text}"
    print(f"dead receiver: nmea=0 nage=999 pwr={f['pwr']} api={f['api']} pf={f['pf']} "
          f"(rc reported) PASS")

    # ---- idle: GPS deliberately off, and that must be distinguishable from a fault ----
    f, text = query_gpsstat({}, tracking=False, settle=4)
    assert f["pwr"] == 1, f"idle should report power OFF (1), got pwr={f['pwr']}: {text}"
    print(f"idle: pwr={f['pwr']} (off, not a fault) PASS")

    print("GPSSTAT separates a dead receiver from one that is merely not locked")


if __name__ == "__main__":
    main()
