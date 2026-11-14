#!/usr/bin/env python3
"""SITL: by default the firmware puts the receiver at 5 Hz and ships one averaged fix per second
(vector-mean velocity). The receiver forgets the rate at power-off (SITL_NMEA_CFGNAV models that), so
the rate must be reasserted after every power-on. SZCS#GNSSAVG=0 ships the raw fixes, GNSSRATE=1000
goes back to 1 Hz, SZCS#GNSSCMD reaches the modem as a checksummed AT+CGNSSCMD, GPSSTAT reports
r10 (NMEA rate), avg and gr (target interval). Also: no fix from before an idle period is shipped
after it, r10 drops to 0 (and the rate is resent) when only RMC stops, and a receiver that ignores
CFGNAV gets 3 tries 30 s apart, then backs off."""

import math
import os
import re
import socket
import subprocess
import tempfile
import time
from datetime import datetime, timezone

from test_loc_batch import SITL, BATCH_PROTO, frames, parse_batch, drain, finish_sitl, gt06_cmd


def r10(buf):
    m = re.findall(rb"r10(\d+) avg(\d) gr(\d+)", buf)
    assert m, buf
    return tuple(int(x) for x in m[-1])


SER = [100]


def wait_rate(conn, lo, hi, limit):
    """Poll GPSSTAT every 3 s until lo <= r10 <= hi; returns (seconds, last reply, everything received)."""
    allb = bytearray()
    for k in range(limit // 3):
        SER[0] += 1
        conn.sendall(gt06_cmd("CXCS#GPSSTAT", SER[0]))
        st = drain(conn, 3)
        allb += st
        if re.search(rb"r10\d+", st) and lo <= r10(st)[0] <= hi:
            return 3 * (k + 1), st, allb
    return None, st, allb


def fix_times(buf):
    """UTC epoch of every fix in the 0xF1 batches in buf."""
    out = []
    for p, pl in frames(buf):
        if p != BATCH_PROTO:
            continue
        h, el = parse_batch(pl)
        yy, mo, dd, hh, mi, ss = h["base_dt"]
        base = datetime(2000 + yy, mo, dd, hh, mi, ss, tzinfo=timezone.utc).timestamp()
        out += [base + e["dt_ds"] / 10 for e in el]
    return out


def cfgnav_sends(stderr, ms=200):
    return len(re.findall(rf'AT\+CGNSSCMD="\$CFGNAV,{ms},{ms},3\*'.encode(), stderr))


def backoff():
    """Receiver ignores CFGNAV: 3 tries 30 s apart, then nothing for 10 min."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    proc = subprocess.Popen(
        [SITL, "--server", f"127.0.0.1:{port}", "--teriid", "888800000000802", "--no-reexec"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(os.environ))
    conn = None
    try:
        listener.settimeout(8)
        conn, _ = listener.accept()
        drain(conn, 2)
        conn.sendall(gt06_cmd("TIMER,1,1#", 1))
        drain(conn, 50)
        _, mid, _ = wait_rate(conn, 999, 999, 3)
        drain(conn, 60)
    finally:
        if conn:
            conn.close()
        listener.close()
        _, stderr = finish_sitl(proc)
    n = cfgnav_sends(stderr)
    assert n == 3, f"{n} CFGNAV sends in ~115 s to a receiver stuck at 1 Hz, expected 3 then back-off"
    assert 8 <= r10(mid)[0] <= 12, mid
    print(f"gnss backoff: {n} CFGNAV tries then quiet, r10 {r10(mid)[0]} PASS")


def main():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    normc = os.path.join(tempfile.mkdtemp(prefix="sitl-gnss-"), "normc")
    env = dict(os.environ, SITL_NMEA_CFGNAV="1", SITL_NMEA_JITTER="1",   # receiver starts at 1 Hz
               SITL_NMEA_NORMC_FLAG=normc)
    proc = subprocess.Popen(
        [SITL, "--server", f"127.0.0.1:{port}", "--teriid", "888800000000801", "--no-reexec"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    conn = None
    try:
        listener.settimeout(8)
        conn, _ = listener.accept()
        drain(conn, 2)
        conn.sendall(gt06_cmd("TIMER,1,1#", 1))
        t1, st1, _ = wait_rate(conn, 40, 60, 36)  # rate set once NMEA flows, r10 window settles
        got = drain(conn, 12)
        batches = [parse_batch(pl)[1] for p, pl in frames(got) if p == BATCH_PROTO]
        elems = [e for b in batches for e in b]
        gaps = [b[i]["dt_ds"] - b[i - 1]["dt_ds"] for b in batches for i in range(1, len(b))]

        conn.sendall(gt06_cmd("SZCS#GNSSAVG=0", 3))
        assert b"SETOK:GNSSAVG=0" in drain(conn, 2)
        raw = drain(conn, 10)
        raw_fixes = sum(parse_batch(pl)[0]["count"] for p, pl in frames(raw) if p == BATCH_PROTO)
        conn.sendall(gt06_cmd("SZCS#GNSSAVG=1", 4))
        drain(conn, 1)

        # idle powers the receiver off (back to 1 Hz); tracking again must restore 5 Hz
        conn.sendall(gt06_cmd("TIMER,600,1800#", 5))
        drain(conn, 10)
        restart = time.time()
        conn.sendall(gt06_cmd("TIMER,1,1#", 6))
        t2, st2, after = wait_rate(conn, 40, 60, 36)

        # RMC stops while GGA/GSA continue: r10 must fall to 0 and the rate be resent
        open(normc, "w").close()
        drain(conn, 40)
        _, st_normc, _ = wait_rate(conn, 0, 0, 3)
        os.unlink(normc)
        drain(conn, 3)

        conn.sendall(gt06_cmd("SZCS#GNSSRATE=1000", 8))
        st3 = drain(conn, 2)
        t3, st, _ = wait_rate(conn, 8, 12, 36)
        st3 += st
        conn.sendall(gt06_cmd("SZCS#GNSSRATE=50", 10))
        conn.sendall(gt06_cmd("SZCS#GNSSCMD=BAD*", 11))
        conn.sendall(gt06_cmd("CXCS#GNSSLOG", 12))
        replies = drain(conn, 3)
    finally:
        if conn:
            conn.close()
        listener.close()
        _, stderr = finish_sitl(proc)

    rate, avg, gr = r10(st1)
    assert t1 and avg == 1 and gr == 200, f"default not 5 Hz averaged: {st1}"
    # one averaged fix per second: on average 1.0 s apart (0.1 s units). Each fix carries the mean
    # sub-second of its window, and SITL's 5 Hz sub-seconds cycle 0..9, so single gaps wander 0.5-1.5 s.
    assert len(elems) >= 9 and gaps and 9 <= sum(gaps) / len(gaps) <= 11 and all(4 <= g <= 16 for g in gaps), \
        f"fix spacing {gaps}"
    e5 = 5.0 * math.sin(math.radians(35)) + 7.0 * math.sin(math.radians(55))
    n5 = 5.0 * math.cos(math.radians(35)) + 7.0 * math.cos(math.radians(55))
    for e in elems[1:-1]:                           # 5 alternating fixes (3:2 either way) per second
        assert 5.4 <= e["spd"] <= 6.6, f"averaged speed {e['spd']} kn"
        assert 41 <= e["hdg"] <= 49, f"averaged heading {e['hdg']}"
    exp = math.hypot(e5, n5) / 2
    mean_spd = sum(e["spd"] for e in elems[1:-1]) / len(elems[1:-1])
    assert abs(mean_spd - exp) < 0.25, f"mean averaged speed {mean_spd:.2f}, expected ~{exp:.2f}"
    assert raw_fixes >= 30, f"only {raw_fixes} raw fixes in 10 s at 5 Hz with GNSSAVG=0"

    rate2, _, _ = r10(st2)
    assert t2, f"5 Hz not restored after a GPS power cycle: {st2}"
    ft = fix_times(after)
    assert ft and min(ft) >= restart - 3, \
        f"fix from {restart - min(ft):.1f} s before tracking restarted was shipped after it"
    assert r10(st_normc)[0] == 0, f"r10 stale while RMC stopped: {st_normc}"
    log = stderr.decode(errors="replace")
    sets = [m.start() for m in re.finditer(r'AT\+CGNSSCMD="\$CFGNAV,200,200,3\*', log)]
    off = log.find("[SITL] GNSS power off", sets[0] if sets else 0)
    assert len(sets) >= 2 and off != -1 and sets[-1] > off, "rate not reasserted after power-on"
    # one per power-on (+1 retry at most), plus 1-2 while RMC was stopped
    assert 3 <= len(sets) <= 6, f"{len(sets)} CFGNAV sends, expected a resend while RMC was stopped"

    rate3, _, gr3 = r10(st3)
    assert t3 and gr3 == 1000 and b"SETOK:GNSSRATE=1000" in st3, st3
    assert 'AT+CGNSSCMD="$CFGNAV,1000,1000,3*' in log
    assert b"SET ERROR: GNSSRATE" in replies, replies
    assert b"SET ERROR: GNSSCMD" in replies, "bad characters accepted"
    assert b"READOK:GNSSLOG n" in replies and b"AT:OK" in replies, replies
    print(f"gnss: default r10 {rate} avg{avg} gr{gr}, {len(elems)} averaged/12 s mean {mean_spd:.2f} kn "
          f"(expect {exp:.2f}) after {t1} s; raw {raw_fixes}/10 s; after power cycle r10 {rate2} in {t2} s "
          f"({len(sets)} CFGNAV); GNSSRATE=1000 r10 {rate3} in {t3} s; PASS")


if __name__ == "__main__":
    main()
    backoff()
