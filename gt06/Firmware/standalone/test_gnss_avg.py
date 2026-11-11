#!/usr/bin/env python3
"""SITL: 5 Hz receiver with SZCS#GNSSAVG=1 ships one averaged fix per second (vector-mean velocity);
SZCS#GNSSCMD reaches the modem as a checksummed AT+CGNSSCMD; GPSSTAT r10 reports the NMEA rate."""

import math
import os
import re
import socket
import subprocess

from test_loc_batch import SITL, BATCH_PROTO, frames, parse_batch, drain, finish_sitl, gt06_cmd


def main():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    env = dict(os.environ, SITL_NMEA_HZ="5", SITL_NMEA_JITTER="1")
    proc = subprocess.Popen(
        [SITL, "--server", f"127.0.0.1:{port}", "--teriid", "888800000000801", "--no-reexec"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    conn = None
    try:
        listener.settimeout(8)
        conn, _ = listener.accept()
        drain(conn, 2)
        conn.sendall(gt06_cmd("TIMER,1,1#", 1))
        drain(conn, 3)
        raw = drain(conn, 10)
        raw_fixes = sum(parse_batch(pl)[0]["count"] for p, pl in frames(raw) if p == BATCH_PROTO)

        conn.sendall(gt06_cmd("SZCS#GNSSAVG=1", 2))
        got = drain(conn, 2)
        assert b"SETOK:GNSSAVG=1" in got, got
        got = drain(conn, 12)
        elems = [e for p, pl in frames(got) if p == BATCH_PROTO for e in parse_batch(pl)[1]]

        conn.sendall(gt06_cmd("SZCS#GNSSCMD=CFGNAV,200,200,1000", 3))
        replies = drain(conn, 1)                    # let the modem's OK reach the URC queue
        conn.sendall(gt06_cmd("CXCS#GPSSTAT", 4))
        conn.sendall(gt06_cmd("SZCS#GNSSCMD=BAD*", 5))
        conn.sendall(gt06_cmd("CXCS#GNSSLOG", 6))
        replies = bytes(replies + drain(conn, 3))
    finally:
        if conn:
            conn.close()
        listener.close()
        _, stderr = finish_sitl(proc)

    assert raw_fixes >= 15, f"only {raw_fixes} fixes in 10 s at 5 Hz without averaging"
    # 12 s window; a batch flush boundary can cut one second either side
    assert 9 <= len(elems) <= 14, f"{len(elems)} averaged fixes in 12 s, expected ~12"
    e5 = 5.0 * math.sin(math.radians(35)) + 7.0 * math.sin(math.radians(55))
    n5 = 5.0 * math.cos(math.radians(35)) + 7.0 * math.cos(math.radians(55))
    # a second holds 5 alternating fixes (3:2 either way); its mean must lie between the two extremes
    for e in elems[1:-1]:
        assert 5.4 <= e["spd"] <= 6.6, f"averaged speed {e['spd']} kn"
        assert 41 <= e["hdg"] <= 49, f"averaged heading {e['hdg']}"
    exp = math.hypot(e5, n5) / 2
    mean_spd = sum(e["spd"] for e in elems[1:-1]) / len(elems[1:-1])
    assert abs(mean_spd - exp) < 0.25, f"mean averaged speed {mean_spd:.2f}, expected ~{exp:.2f}"
    m = re.search(rb"r10(\d+) avg1", replies)
    assert m and 40 <= int(m.group(1)) <= 60, replies
    assert b"SETOK:GNSSCMD rc" in replies, replies
    assert b"SET ERROR: GNSSCMD" in replies, "bad characters accepted"
    assert b'AT+CGNSSCMD="$CFGNAV,200,200,1000*' in stderr, stderr[-2000:]
    assert b"READOK:GNSSLOG n" in replies and b"AT:OK" in replies, replies
    print(f"gnss avg: {raw_fixes} raw fixes/10 s -> {len(elems)} averaged/12 s, "
          f"mean {mean_spd:.2f} kn (expect {exp:.2f}), r10 {int(m.group(1))}, GNSSCMD/GNSSLOG PASS")


if __name__ == "__main__":
    main()
