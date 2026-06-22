#!/usr/bin/env python3
"""Extract a per-unit battery voltage time series from the GT06 binary packet log
into gt06/battery_data/ — the idle and sleep companion datasets to tracking.csv.gz.

Unlike tracking.csv.gz (from the 1 Hz position logs), idle/sleep voltages come from
the GT06 binary log: cxzt# replies carry *BT in 1 mV, STATUS# replies carry Battery
in 10 mV. We keep every reading with its timestamp and source so the low-noise idle
endpoints and the noisy sleep wake reads are both preserved for later analysis.

Conn-id-reuse-safe: attributes each frame to whoever MOST-RECENTLY logged in on its
conn_id (conn ids reset across server restarts). Prepend the pre-midnight archive to
the current gt06.log so logins are present (connections persist across the 00:00
rotation).

  # idle dataset (parked race-idle, restart -> sleep)
  python3 gt06_extract_voltage_series.py --log comb.log --start 1782020506 \
      --end 1782057600 --out gt06/battery_data/idle.csv.gz --mode idle

  # sleep dataset (MODE5 overnight bracketed by idle endpoints)
  python3 gt06_extract_voltage_series.py --log comb.log --start 1782055800 \
      --sleep-start 1782057600 --wake-end 1782079200 --out gt06/battery_data/sleep.csv.gz \
      --mode sleep
"""
import argparse, gzip, json, os, re, struct
from collections import defaultdict

DIR_OUT = 0x80000000
_BT = re.compile(rb'\*BT:(\d+)')
_BATT = re.compile(rb'Battery:(\d+\.\d+)V')


def walk(path):
    blob = open(path, 'rb').read()
    i = 8 if blob[:8] == b'GT06LOG2' else 0
    n = len(blob)
    while i + 14 <= n:
        ts, conn, ln = struct.unpack_from('<dIH', blob, i)
        i += 14
        if ln == 0 or i + ln > n:
            break
        fr = blob[i:i + ln]
        i += ln
        if len(fr) < 6 or fr[0] != 0x78:
            continue
        L = fr[2]
        data = fr[4:3 + L - 4] if 3 + L - 4 > 4 else b''
        yield ts, conn & ~DIR_OUT, bool(conn & DIR_OUT), fr[3], data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--log', required=True, help='GT06 binary log (combined)')
    ap.add_argument('--start', type=float, required=True)
    ap.add_argument('--end', type=float, default=None)
    ap.add_argument('--sleep-start', type=float, default=None,
                    help='for --mode sleep: epoch the units entered MODE5 (phase boundary)')
    ap.add_argument('--wake-end', type=float, default=None,
                    help='for --mode sleep: epoch the units returned to idle (phase boundary)')
    ap.add_argument('--mode', choices=('idle', 'sleep'), required=True)
    ap.add_argument('--prefix', default='G')
    ap.add_argument('--out', required=True)
    a = ap.parse_args()

    conn_sid, series = {}, defaultdict(list)
    for ts, cid, is_out, proto, data in walk(a.log):
        if is_out:
            continue
        if proto == 0x01:
            h = data.hex().lstrip('0')
            if len(h) == 16:
                h = h[1:]
            if len(h) >= 6:
                conn_sid[cid] = a.prefix + h[-6:]
            continue
        if proto != 0x15:
            continue
        sid = conn_sid.get(cid)
        if not sid or ts < a.start or (a.end and ts > a.end):
            continue
        m = _BT.search(data)
        if m:
            series[sid].append((ts, 'cxzt', int(m.group(1))))
            continue
        m = _BATT.search(data)
        if m:
            series[sid].append((ts, 'status', int(round(float(m.group(1)) * 1000))))

    def phase(ts):
        if a.mode != 'sleep':
            return 'idle'
        if a.sleep_start and ts < a.sleep_start:
            return 'pre_idle'
        if a.wake_end and ts >= a.wake_end:
            return 'post_idle'
        return 'sleep_wake'

    rows = sorted((g, ts, src, mv) for g in series for ts, src, mv in series[g])
    hdr = 'id,ts,source,v_mv' + (',phase' if a.mode == 'sleep' else '')
    with gzip.open(a.out, 'wt') as f:
        f.write(hdr + '\n')
        for g, ts, src, mv in rows:
            line = f'{g},{int(ts)},{src},{mv}'
            if a.mode == 'sleep':
                line += ',' + phase(ts)
            f.write(line + '\n')
    ncx = sum(1 for r in rows if r[2] == 'cxzt')
    print(f"wrote {a.out}: {len(rows)} readings ({ncx} cxzt, {len(rows)-ncx} status), "
          f"{len(series)} units")


if __name__ == '__main__':
    main()
