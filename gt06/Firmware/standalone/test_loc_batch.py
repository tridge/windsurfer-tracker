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
import signal
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


def loc_sod(payload):
    """Seconds-of-day from a legacy 0x12 LOC payload (yy mo dd hh mi ss ...)."""
    if len(payload) < 6:
        return None
    _, _, _, hh, mi, ss = payload[0:6]
    if hh > 23 or mi > 59 or ss > 60:
        return None
    return hh * 3600 + mi * 60 + ss


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


def drain(conn, seconds, allow_close=False):
    """Collect everything the device sends for a fixed period.

    allow_close: the device is expected to hang up (e.g. simulated coverage loss), so a closed
    connection ends the collection instead of failing the test.
    """
    buf = bytearray()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        conn.settimeout(max(0.05, deadline - time.monotonic()))
        try:
            chunk = conn.recv(4096)
        except socket.timeout:
            continue
        except OSError:
            if allow_close:
                break
            raise
        if not chunk:
            if allow_close:
                break
            raise AssertionError("SITL closed its GT06 connection")
        buf += chunk
    return buf


def finish_sitl(proc):
    """Stop the SITL process and FAIL if it crashed or a sanitizer complained.

    Without this a firmware crash goes unnoticed: the assertions above run against frames that
    already arrived, and the process is killed in `finally` regardless. That is exactly how a stack
    buffer overflow in the batch path passed a green test run.
    """
    crashed_early = proc.poll() is not None
    rc = proc.returncode
    proc.terminate()
    try:
        stdout, stderr = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate(timeout=5)
    tail = stderr.decode(errors="replace")
    if b"AddressSanitizer" in stderr or b"runtime error" in stderr:
        raise AssertionError("sanitizer reported a fault:\n" + tail[-3000:])
    if crashed_early:
        raise AssertionError(f"SITL exited early (rc={rc}):\n{tail[-3000:]}")
    return stdout, stderr


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

        # ---- the maximum batch: biggest frame we can emit, 12 + 14*10 = 152 B payload ----
        conn.sendall(gt06_cmd("SZCS#LOCBATCH=10", 4))
        got = drain(conn, 24)
        assert b"SETOK:LOCBATCH=10" in got, got[-200:]
        batches = [parse_batch(pl) for p, pl in frames(got) if p == BATCH_PROTO]
        assert batches, "no batches at LOCBATCH=10"
        counts = [h["count"] for h, _ in batches]
        # A 10 s window at 1 Hz yields 9 or 10 depending on where the first fix falls relative to the
        # flush, so accept either — what matters is that a near-maximum frame is built and framed
        # correctly (parse_batch above checks payload == 12 + 14*count) and that the cap holds.
        assert max(counts) >= 9, f"never reached a near-full batch: {counts}"
        assert max(counts) <= 10, f"batch exceeded the configured maximum: {counts}"

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
        finish_sitl(proc)

    print("batched LOC (0xF1) PASS")


def test_full_batch_serialises_the_maximum_frame():
    """Actually build a 10-element frame — the largest payload the format allows (152 B).

    At 1 Hz phase alignment leaves a 10 s window one fix short, so the maximum element is never
    serialised and a sanitizer cannot see an overrun confined to it. Run GNSS faster so the window
    genuinely fills, which also exercises the batch-full path.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    env = dict(os.environ)
    env["SITL_NMEA_HZ"] = "5"                    # 5 fixes/s: a 5 s window overfills a 10-fix batch
    proc = subprocess.Popen(
        [SITL, "--server", f"127.0.0.1:{port}", "--teriid", "888800000000128", "--no-reexec"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)

    conn = None
    try:
        listener.settimeout(8)
        conn, _ = listener.accept()
        drain(conn, 2)
        conn.sendall(gt06_cmd("TIMER,1,1#", 1))
        drain(conn, 2)
        conn.sendall(gt06_cmd("SZCS#LOCBATCH=10", 2))
        got = drain(conn, 26)
        assert b"SETOK:LOCBATCH=10" in got, got[-200:]
        batches = [parse_batch(pl) for p, pl in frames(got) if p == BATCH_PROTO]
        counts = [h["count"] for h, _ in batches]
        assert 10 in counts, f"never serialised a full 10-element frame: {counts}"
        assert max(counts) <= 10, f"batch exceeded the maximum: {counts}"
        # every element of the largest frame must still decode sanely
        full = [(h, e) for h, e in batches if h["count"] == 10][0]
        assert [x["dt_ds"] for x in full[1]] == sorted(x["dt_ds"] for x in full[1])
    finally:
        if conn is not None:
            conn.close()
        listener.close()
        finish_sitl(proc)

    print("maximum 10-element frame PASS")


def test_batch_survives_a_link_drop():
    """A pending batch must not be lost when the link goes down.

    SIGUSR1 makes the simulated modem lose coverage; fixes captured before that are still in the
    in-memory batch and would be discarded without the blind-buffer spill. After coverage returns the
    device reconnects and replays them as legacy 0x12 frames.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    proc = subprocess.Popen(
        [SITL, "--server", f"127.0.0.1:{port}", "--teriid", "888800000000129", "--no-reexec"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(os.environ))

    conn = None
    try:
        listener.settimeout(8)
        conn, _ = listener.accept()
        drain(conn, 2)
        conn.sendall(gt06_cmd("TIMER,1,1#", 1))
        drain(conn, 3)                            # accumulate a partial batch

        drop_sod = time.gmtime().tm_hour * 3600 + time.gmtime().tm_min * 60 + time.gmtime().tm_sec
        proc.send_signal(signal.SIGUSR1)          # coverage DOWN mid-window
        drain(conn, 3, allow_close=True)          # the device hangs up when coverage goes
        conn.close()
        conn = None

        proc.send_signal(signal.SIGUSR1)          # coverage back UP
        listener.settimeout(30)
        conn, _ = listener.accept()               # device reconnects
        got = drain(conn, 20)
        replays = [loc_sod(pl) for p, pl in frames(got) if p == LOC_PROTO]
        replays = [r for r in replays if r is not None]
        assert replays, "nothing replayed after reconnect"
        # Fixes captured AFTER the drop reach the blind buffer on their own, so their presence proves
        # nothing. The batch spill is what preserves fixes stamped BEFORE coverage went away.
        assert min(replays) < drop_sod, (
            f"no pre-drop fix replayed (earliest {min(replays)} vs drop {drop_sod}) — "
            "the in-memory batch was discarded rather than spilled")
    finally:
        if conn is not None:
            conn.close()
        listener.close()
        finish_sitl(proc)

    print("batch survives a link drop PASS")


def test_tracking_without_fixes_sends_nothing():
    """A tracking window that produced NO fixes must send no batch frame at all.

    The main test can only check this while idle, where the flush is disabled outright; here GNSS
    power-on is failed forever, so the device is genuinely tracking and simply has nothing to report.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    env = dict(os.environ)
    env["SITL_GNSS_POWER_FAILS"] = "9999"        # GNSS never comes up -> no NMEA -> no fixes
    proc = subprocess.Popen(
        [SITL, "--server", f"127.0.0.1:{port}", "--teriid", "888800000000127", "--no-reexec"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)

    conn = None
    try:
        listener.settimeout(8)
        conn, _ = listener.accept()
        drain(conn, 2)
        conn.sendall(gt06_cmd("TIMER,1,1#", 1))
        got = drain(conn, 14)                     # several 5 s windows
        assert b"TIMER,1,1# OK" in got, "device did not enter tracking"
        assert not [p for p, _ in frames(got) if p == BATCH_PROTO], \
            "batch frame sent for a window with no fixes"
        assert not [p for p, _ in frames(got) if p == LOC_PROTO], \
            "legacy LOC sent for a window with no fixes"
    finally:
        if conn is not None:
            conn.close()
        listener.close()
        finish_sitl(proc)

    print("tracking with no fixes sends nothing PASS")


if __name__ == "__main__":
    main()
    test_tracking_without_fixes_sends_nothing()
    test_full_batch_serialises_the_maximum_frame()
    test_batch_survives_a_link_drop()
