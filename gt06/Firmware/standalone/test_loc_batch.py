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
from sitl_util import server_acks, gt06_cmd as _crc_cmd  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SITL = os.environ.get("SITL_BIN") or os.path.join(HERE, "sitl")

BATCH_PROTO = 0xF1
LOC_PROTO = 0x12


def gt06_cmd(command, serial):
    """Build a GT06 0x80 server-to-device command frame (valid CRC)."""
    return _crc_cmd(command, serial)


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
            server_acks(conn, chunk)
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
        ack = drain(conn, 4)
        assert b"SETOK:LOCBATCH=3" in ack, ack[-200:]
        # The rate change spills the partial batch to the blind buffer, and replay packs up to
        # LOC_BATCH_MAX per frame regardless of LOCBATCH (that is what makes catch-up fast), so the
        # first frames after the change are replays. Let them drain before checking the LIVE window.
        got = drain(conn, 10)
        batches = [parse_batch(pl) for p, pl in frames(got) if p == BATCH_PROTO]
        assert batches, "no batches after LOCBATCH=3"
        assert all(h["count"] <= 3 for h, _ in batches), \
            f"live counts {[h['count'] for h, _ in batches]} exceed the configured 3"

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
        # Replay is batched now, so the backlog comes back inside 0xF1 frames (legacy 0x12 only when
        # LOCBATCH=0). Reconstruct each element's absolute seconds-of-day from its base + delta.
        stamps = []
        for p, pl in frames(got):
            if p != BATCH_PROTO:
                continue
            h, elems = parse_batch(pl)
            base = h["base_dt"][3] * 3600 + h["base_dt"][4] * 60 + h["base_dt"][5]
            stamps += [base + e["dt_ds"] // 10 for e in elems]
        assert stamps, "nothing replayed after reconnect"
        # Fixes captured AFTER the drop reach the blind buffer on their own, so their presence proves
        # nothing. The batch spill is what preserves fixes stamped BEFORE coverage went away.
        assert min(stamps) < drop_sod, (
            f"no pre-drop fix replayed (earliest {min(stamps)} vs drop {drop_sod}) — "
            "the in-memory batch was discarded rather than spilled")
    finally:
        if conn is not None:
            conn.close()
        listener.close()
        finish_sitl(proc)

    print("batch survives a link drop PASS")


def test_blind_replay_is_batched_and_fast():
    """A backlog must come back as 0xF1 batches, not one 0x12 per fix.

    The legacy path replays BLIND_REPLAY_HZ (5) fixes per RMC, i.e. ~5 fixes/s, so a long outage takes
    as long to drain as it did to accumulate. Batched replay carries up to 10 fixes per frame at the
    same 5 frames/s. This asserts both the format and the resulting throughput, and that speed/heading
    survive the round trip through the blind buffer.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    env = dict(os.environ)
    env["SITL_NMEA_HZ"] = "5"                     # build a backlog quickly
    proc = subprocess.Popen(
        [SITL, "--server", f"127.0.0.1:{port}", "--teriid", "888800000000130", "--no-reexec"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)

    conn = None
    try:
        listener.settimeout(8)
        conn, _ = listener.accept()
        drain(conn, 2)
        conn.sendall(gt06_cmd("TIMER,1,1#", 1))
        drain(conn, 2)

        proc.send_signal(signal.SIGUSR1)          # coverage DOWN — accumulate a backlog
        drain(conn, 2, allow_close=True)
        conn.close()
        conn = None
        time.sleep(12)                            # ~60 fixes at 5 Hz
        proc.send_signal(signal.SIGUSR1)          # coverage UP

        listener.settimeout(30)
        conn, _ = listener.accept()
        t0 = time.monotonic()
        got = drain(conn, 12)
        elapsed = time.monotonic() - t0

        batches = [parse_batch(pl) for p, pl in frames(got) if p == BATCH_PROTO]
        replayed = sum(h["count"] for h, _ in batches)
        legacy = len([p for p, _ in frames(got) if p == LOC_PROTO])
        assert batches, "backlog was not replayed as 0xF1 batches"
        assert replayed >= 40, f"only {replayed} fixes replayed in {elapsed:.1f}s"
        # the legacy path could not have moved this many: 5 fixes/s ceiling
        assert replayed > 5 * elapsed, (
            f"replayed {replayed} fixes in {elapsed:.1f}s — no faster than the legacy 5/s path")
        assert legacy == 0, f"{legacy} legacy 0x12 frames sent while batching is on"
        # speed/heading must survive the blind buffer, not come back as zeros
        assert any(e["spd"] > 0 for _, es in batches for e in es), \
            "replayed fixes lost their speed"
        assert any(e["hdg"] == 45 for _, es in batches for e in es), \
            "replayed fixes lost their heading"
    finally:
        if conn is not None:
            conn.close()
        listener.close()
        finish_sitl(proc)

    print(f"blind replay is batched and fast PASS ({replayed} fixes in {elapsed:.1f}s)")


def test_batch_survives_midnight():
    """A batch straddling 23:59:59 -> 00:00:00 must keep the post-midnight fixes.

    Seconds-of-day goes backwards at midnight, so a raw `<` monotonic check rejected every fix after
    00:00:00 until the batch flushed — up to 9 lost fixes a night at LOCBATCH=10.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    # place the stamped clock ~8 s before midnight UTC
    nowu = time.time()
    sod = nowu % 86400
    env = dict(os.environ)
    env["SITL_NMEA_TIME_OFFSET_S"] = str(int(86400 - sod - 8))
    proc = subprocess.Popen(
        [SITL, "--server", f"127.0.0.1:{port}", "--teriid", "888800000000131", "--no-reexec"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)

    conn = None
    try:
        listener.settimeout(8)
        conn, _ = listener.accept()
        drain(conn, 2)
        conn.sendall(gt06_cmd("TIMER,1,1#", 1))
        got = drain(conn, 22)                      # spans the rollover
        batches = [parse_batch(pl) for p, pl in frames(got) if p == BATCH_PROTO]
        assert batches, "no batches around midnight"

        # A batch based BEFORE midnight must carry at least one element that lands AFTER it. With a raw
        # seconds-of-day comparison those elements are rejected, so no such element exists — which is
        # exactly the bug, and a weaker check (batch merely non-empty) cannot see it.
        crossed = []
        for h, elems in batches:
            base_ds10 = ((h["base_dt"][3] * 3600 + h["base_dt"][4] * 60 + h["base_dt"][5]) * 10
                         + h["base_ds"])
            for e in elems:
                if base_ds10 + e["dt_ds"] >= 864000:      # past 24 h => the next day
                    crossed.append((h, e))
        assert crossed, (
            "no fix delivered past midnight inside its batch — post-midnight fixes were dropped; "
            f"bases={[h['base_dt'][3:6] for h, _ in batches]}")
        # and the rollover must not produce an absurd delta
        for h, elems in batches:
            assert all(e["dt_ds"] <= 120 for e in elems), \
                f"midnight produced an absurd delta: {[e['dt_ds'] for e in elems]}"
    finally:
        if conn is not None:
            conn.close()
        listener.close()
        finish_sitl(proc)

    print("batch survives midnight PASS")


def test_overflow_fixes_are_kept_and_ordered():
    """More fixes than a batch can hold must all be delivered, in order.

    At 5 Hz with LOCBATCH=5 the window overflows constantly, so the batch-full path runs on nearly
    every window. Nothing may be dropped and nothing may arrive out of order.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    env = dict(os.environ)
    env["SITL_NMEA_HZ"] = "5"                      # 25 fixes per 5 s window vs a batch of 5
    proc = subprocess.Popen(
        [SITL, "--server", f"127.0.0.1:{port}", "--teriid", "888800000000132", "--no-reexec"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)

    conn = None
    try:
        listener.settimeout(8)
        conn, _ = listener.accept()
        drain(conn, 2)
        conn.sendall(gt06_cmd("TIMER,1,1#", 1))
        got = drain(conn, 18)
        batches = [parse_batch(pl) for p, pl in frames(got) if p == BATCH_PROTO]
        assert batches, "no batches under overflow"
        total = sum(h["count"] for h, _ in batches)
        # ~5 Hz for ~16 s of tracking; allow slack for start-up but require the overflow path to have
        # preserved far more than the 5-per-window a naive drop would leave
        assert total >= 40, f"only {total} fixes delivered under overflow — fixes were dropped"
        for h, elems in batches:
            assert [e["dt_ds"] for e in elems] == sorted(e["dt_ds"] for e in elems), \
                f"batch out of order: {[e['dt_ds'] for e in elems]}"
    finally:
        if conn is not None:
            conn.close()
        listener.close()
        finish_sitl(proc)

    print(f"overflow fixes kept and ordered PASS ({total} delivered)")


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
    test_blind_replay_is_batched_and_fast()
    test_batch_survives_midnight()
    test_overflow_fixes_are_kept_and_ordered()
