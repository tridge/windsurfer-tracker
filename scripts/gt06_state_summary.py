#!/usr/bin/env python3
"""Summarise the states each GT06 tracker went through over a window.

Combines two ground-truth sources, bucketed over time:
  * active TRACKING  -- from the event position logs (logs/YYYY_MM_DD.jsonl[.gz]);
    idle units never log positions, so any position = actively tracking. NOTE a
    unit replaying its on-device blind buffer after a reconnect backfills past
    buckets, so absolute tracking time can read high -- but it inflates equally
    for every unit, so cross-unit *uniformity* still holds. For an artifact-free
    active-time/energy comparison, count fixes in a known-clean window instead.
  * CONNECTED (idle) -- from tracker.log "Idle heartbeat" / "GPS-wait heartbeat"
    lines: alive but not sending fixes. GPS-wait can also fire on a stuck-GPS
    idle unit, so it is NOT counted as tracking -- positions are the only truth.
  * state TRANSITIONS-- tracker.log "Active mode for", "Idle mode for",
    "Overnight idle mode for" (counted per unit).
A unit/bucket with neither positions nor heartbeats is OFFLINE (dark/gap).

Run it where the logs live (the server, as the tracker user):
  python3 gt06_state_summary.py \
      --tracker-log /home/tracker/tracker/tracker.log \
      --logs-dir   /home/tracker/tracker/html/8/logs \
      --start '2026-06-18 18:00' --bucket-min 30
"""
import argparse, gzip, json, os, re, time
from collections import defaultdict

HB = re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) .*\b(G\d{6})\b.*\b(Idle heartbeat|GPS-wait heartbeat)\b')
TR = re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) .*\b(Active mode for|Idle mode for|Overnight idle mode for) (G\d{6})')

def to_epoch(s):
    return time.mktime(time.strptime(s, '%Y-%m-%d %H:%M:%S'))

def open_maybe_gz(p):
    return gzip.open(p, 'rt', errors='replace') if p.endswith('.gz') else open(p, errors='replace')

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tracker-log', required=True)
    ap.add_argument('--logs-dir', required=True)
    ap.add_argument('--start', required=True, help="local 'YYYY-MM-DD HH:MM'")
    ap.add_argument('--bucket-min', type=int, default=30)
    a = ap.parse_args()

    start = time.mktime(time.strptime(a.start, '%Y-%m-%d %H:%M'))
    now = time.time()
    bw = a.bucket_min * 60
    nb = int((now - start) // bw) + 1
    def bk(ep): return int((ep - start) // bw)

    track = defaultdict(set)   # unit -> set(bucket) with positions
    idle  = defaultdict(set)   # unit -> set(bucket) with heartbeats
    trans = defaultdict(lambda: defaultdict(int))  # unit -> {transition: count}
    units = set()

    # 1) tracking from position logs (epoch ts)
    for fn in sorted(os.listdir(a.logs_dir)):
        if not re.match(r'2026_\d\d_\d\d\.jsonl(\.gz)?$', fn):
            continue
        for line in open_maybe_gz(os.path.join(a.logs_dir, fn)):
            try:
                d = json.loads(line)
            except Exception:
                continue
            ts, sid = d.get('ts'), d.get('id')
            if ts is None or not sid or ts < start:
                continue
            track[sid].add(bk(ts)); units.add(sid)

    # 2) idle heartbeats + transitions from tracker.log (local-time strings)
    for line in open_maybe_gz(a.tracker_log):
        if 'heartbeat' in line:
            m = HB.match(line)
            if m:
                ep = to_epoch(m.group(1))
                if ep >= start:
                    sid = m.group(2)
                    # Both heartbeat kinds mean "connected but not sending fixes".
                    # (GPS-wait can also fire on a stuck-GPS idle unit, so it is
                    # NOT evidence of tracking -- positions are the only truth.)
                    idle[sid].add(bk(ep))
                    units.add(sid)
        elif 'mode for' in line:
            m = TR.match(line)
            if m and to_epoch(m.group(1)) >= start:
                trans[m.group(3)][m.group(2)] += 1
                units.add(m.group(3))

    span_h = (now - start) / 3600.0
    print(f"window: {a.start} -> now  ({span_h:.1f}h, {nb} x {a.bucket_min}min buckets), {len(units)} units\n")
    bmin = a.bucket_min
    hdr = f"{'unit':9}{'track_h':>8}{'idle_h':>8}{'offline_h':>10}{'->act':>6}{'->idle':>7}{'->sleep':>8}"
    print(hdr)
    rows = []
    for sid in sorted(units):
        tb, ib = track[sid], idle[sid]
        t_h = len(tb) * bmin / 60.0
        i_h = len(set(ib) - tb) * bmin / 60.0          # idle buckets that weren't tracking
        off = (nb - len(tb | ib)) * bmin / 60.0
        ta = trans[sid].get('Active mode for', 0)
        ti = trans[sid].get('Idle mode for', 0)
        ts_ = trans[sid].get('Overnight idle mode for', 0)
        rows.append((sid, t_h, i_h, off, ta, ti, ts_))
        print(f"{sid:9}{t_h:>8.1f}{i_h:>8.1f}{off:>10.1f}{ta:>6}{ti:>7}{ts_:>8}")

    def stats(i):
        v = sorted(r[i] for r in rows)
        return v[0], v[len(v)//2], v[-1]
    print("\nfleet spread (min / median / max):")
    for name, idx in (('track_h', 1), ('idle_h', 2), ('offline_h', 3)):
        lo, md, hi = stats(idx)
        print(f"  {name:10} {lo:6.1f} / {md:6.1f} / {hi:6.1f}   (range {hi-lo:.1f}h)")

if __name__ == '__main__':
    main()
