#!/usr/bin/env python3
"""End-to-end SITL test for the batched LOC message (GT06 protocol 0xF1).

Checks the wire contract the server depends on:
  - one 0xF1 frame every LOCBATCH seconds while tracking, carrying up to LOCBATCH fixes;
  - a short window still sends what it has (fewer elements), it does not wait for a full batch;
  - NO frame at all when the window produced no fixes (the no-GPS case);
  - the 8-bit satellite count survives >15 (the old 0x12 nibble saturated at 15);
  - fix type and HDOP are carried;
  - timestamps have 0.1 s resolution and elements are ordered;
  - SZCS#LOCBATCH reconfigures the batch size, and 0 restores the legacy per-fix 0x12 path.
"""

import os
import socket
import struct
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SITL = os.environ.get("SITL_BIN") or os.path.join(HERE, "sitl")

BATCH_PROTO = 0xF1
LOC_PROTO = 0x12


def gt06_cmd(command, serial):
    """Build a GT06 0x80 server-to-device command frame."""
    text = command.encode()
    length = 10 + len(text)
    body = (bytes([0x80, 4 + len(text)]) + b"\x00\x00\x00\x00" + text
            + serial.to_bytes(2, "big") + b"\x00\x00")
    return b"\x78\x78" + bytes([length]) + body + b"\x0d\x0a"


def frames(buf):
    """Yield (protocol, payload) for every complete 0x7878 frame in buf."""
    i = 0
    while i + 5 <= len(buf):
        if buf[i] != 0x78 or buf[i + 1] != 0x78:
            i += 1
            continue
        ln = buf[i + 2]
        end = i + 3 + ln + 2                      # len byte counts proto..crc; +2 for the 0d0a
        if end > len(buf):
            break
        proto = buf[i + 3]
        # len byte = proto(1) + payload + serial(2) + crc(2), so payload is ln-5 bytes
        yield proto, bytes(buf[i + 4:i + 4 + ln - 5])
        i = end


def parse_batch(payload):
    """Decode a 0xF1 payload into (header dict, [element dicts])."""
    assert len(payload) >= 12, f"batch payload too short: {len(payload)}"
    fmt, count, sats, fix, hdop = payload[0], payload[1], payload[2], payload[3], payload[4]
    base_dt = list(payload[5:11])
    base_ds = payload[11]
    assert len(payload) == 12 + 14 * count, (
        f"payload {len(payload)} != 12 + 14*{count}")
    elems = []
    for i in range(count):
        o = 12 + 14 * i
        dt_ds, lat, lon, spd, hdg = struct.unpack(">HiiHH", payload[o:o + 14])
        elems.append({"dt_ds": dt_ds, "lat": lat / 1e7, "lon": lon / 1e7,
                      "spd": spd / 10.0, "hdg": hdg})
    return ({"fmt": fmt, "count": count, "sats": sats, "fix": fix, "hdop": hdop / 10.0,
             "base_dt": base_dt, "base_ds": base_ds}, elems)


def drain(conn, seconds):
    """Collect everything the device sends for a fixed period."""
    buf = bytearray()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        conn.settimeout(max(0.05, deadline - time.monotonic()))
        try:
            chunk = conn.recv(4096)
        except socket.timeout:
            continue
        if not chunk:
            raise AssertionError("SITL closed its GT06 connection")
        buf += chunk
    return buf


def main():
    if not os.path.exists(SITL):
        raise SystemExit("build first: make sitl")

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    proc = subprocess.Popen(
        [SITL, "--server", f"127.0.0.1:{port}", "--teriid", "888800000000126", "--no-reexec"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(os.environ))

    conn = None
    try:
        listener.settimeout(8)
        conn, _ = listener.accept()
        drain(conn, 2)                              # login etc.

        # ---- idle: GPS is off, so no batch frames may appear at all ----
        idle = drain(conn, 7)
        assert not [p for p, _ in frames(idle) if p == BATCH_PROTO], \
            "batch frame sent while idle (no fixes) — must send nothing"

        # ---- tracking at the default batch of 5 ----
        conn.sendall(gt06_cmd("TIMER,1,1#", 1))
        drain(conn, 2)                              # let GPS come up and the first partial window pass
        got = drain(conn, 12)
        batches = [parse_batch(pl) for p, pl in frames(got) if p == BATCH_PROTO]
        assert len(batches) >= 2, f"expected >=2 batch frames in 12 s, got {len(batches)}"

        for hdr, elems in batches:
            assert hdr["fmt"] == 1, hdr
            assert 1 <= hdr["count"] <= 5, f"count {hdr['count']} outside 1..5"
            assert hdr["count"] == len(elems)
            # 8-bit satellite count: the injector reports 17, which the old 4-bit nibble could not
            assert hdr["sats"] == 17, f"sats {hdr['sats']} — 8-bit count not carried"
            assert hdr["fix"] == 3, f"fix type {hdr['fix']} — expected 3 (3D) from GSA"
            assert abs(hdr["hdop"] - 1.2) < 0.01, f"hdop {hdr['hdop']}"
            assert hdr["base_ds"] <= 9
            for e in elems:
                assert -90 <= e["lat"] <= 90 and -180 <= e["lon"] <= 180, e
                assert e["lat"] < 0, "expected a southern-hemisphere fix (signed lat)"
                assert abs(e["spd"] - 6.0) < 0.01, f"speed {e['spd']} — expected 6.0 kn"
                assert e["hdg"] == 45, f"heading {e['hdg']} — expected 45"
            # ordered, and spanning less than the batch window
            assert [e["dt_ds"] for e in elems] == sorted(e["dt_ds"] for e in elems)
            assert elems[-1]["dt_ds"] <= 60, f"span {elems[-1]['dt_ds']} ds > one 5 s window"

        # a full window at 1 Hz should reach the configured 5
        assert any(h["count"] == 5 for h, _ in batches), \
            f"no full batch of 5 seen: {[h['count'] for h, _ in batches]}"
        # 0.1 s resolution really is carried: the base decisecond is non-zero somewhere AND at least
        # one inter-fix delta is not a whole second (the injector rotates the sub-second stamp).
        assert any(h["base_ds"] for h, _ in batches), \
            f"base deciseconds always 0: {[h['base_ds'] for h, _ in batches]}"
        assert any(e["dt_ds"] % 10 for _, es in batches for e in es), \
            "no sub-second deltas — 0.1 s resolution not exercised"

        # ---- reconfigure the batch size ----
        conn.sendall(gt06_cmd("SZCS#LOCBATCH=3", 2))
        got = drain(conn, 10)
        assert b"SETOK:LOCBATCH=3" in got, got[-200:]
        batches = [parse_batch(pl) for p, pl in frames(got) if p == BATCH_PROTO]
        assert batches, "no batches after LOCBATCH=3"
        assert all(h["count"] <= 3 for h, _ in batches), \
            f"counts {[h['count'] for h, _ in batches]} exceed the configured 3"

        # ---- 0 disables batching and restores the legacy per-fix 0x12 ----
        conn.sendall(gt06_cmd("SZCS#LOCBATCH=0", 3))
        got = drain(conn, 8)
        assert b"SETOK:LOCBATCH=0" in got, got[-200:]
        protos = [p for p, _ in frames(got)]
        assert BATCH_PROTO not in protos, "still batching after LOCBATCH=0"
        assert LOC_PROTO in protos, "legacy 0x12 LOC not restored after LOCBATCH=0"
    finally:
        if conn is not None:
            conn.close()
        listener.close()
        proc.terminate()
        try:
            proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=3)

    print("batched LOC (0xF1) PASS")


if __name__ == "__main__":
    main()
