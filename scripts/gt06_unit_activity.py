#!/usr/bin/env python3
"""Per-unit activity / state / battery timeline for one GT06 tracker.

Diagnostic for "why is this idle unit draining / behaving oddly". Walks one or
more v2 gt06 logs, filters to a single unit, and reports:

  * SESSIONS   -- each connection (login .. disconnect gap): duration, #LOC/#HB/
                  #STATUS, blind-buffer replay burst (LOC with lag > --lag at the
                  start of the session), and the first cxzt battery mV.
  * PER-HOUR   -- connected minutes, HB & STATUS poll rate, LOC count, GPS-on
                  fraction (from STATUS replies), and battery SoC.
  * BATTERY    -- resting SoC trajectory (STATUS readings with GPS:OFF & ACC:OFF,
                  i.e. low-load, so no IR sag) via the parametric OCV fit, plus the
                  implied average current; readings under load (GPS/ACC on) are
                  tagged and kept out of the drain fit so load-sag isn't mistaken
                  for drain.
  * SUMMARY    -- connected vs offline time, disconnect count, LOC/replay totals,
                  STATUS poll rate, net resting-SoC drop -> drain rate & mA.

Needs v2 logs (per-packet conn IDs). Combine contiguous daily rotations first so a
connection whose login is in an earlier file stays attributed, e.g.:
  cat old_logs/gt06.log.DAY1 <(tail -c +9 old_logs/gt06.log.DAY2) <(tail -c +9 gt06.log) > comb.log

Usage:
  scripts/gt06_unit_activity.py comb.log --imei G347082
  scripts/gt06_unit_activity.py comb.log --imei 347082 --start '2026-06-29 00:00' --bucket-min 60
"""
import sys
import re
import time
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gt06_dump import (read_packets, detect_format, validate_frame, fmt_time,  # noqa: E402
                       ConnIdentityTracker)
from protocol_GT06 import gt06_parse_location  # noqa: E402

# Voltage / state fields inside the 0x15 string replies (cxzt# and STATUS#).
_BT = re.compile(rb'\*BT:(\d+)')                 # cxzt, 1 mV
_STAT_V = re.compile(rb'Battery:(\d+\.\d+)V')    # STATUS#, 10 mV
_ACC = re.compile(rb'ACC:(ON|OFF)')
_GPS = re.compile(rb'GPS:(ON|OFF)')


def soc_model(v, c1, c2, c3, c4):
    s = c1 * (1.0 - 1.0 / (1.0 + (v / c2) ** c4) ** c3)
    return max(0.0, min(100.0, s))


def parse_when(s):
    if s is None:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            return time.mktime(time.strptime(s, fmt))
        except ValueError:
            continue
    raise SystemExit(f"can't parse time {s!r} (use epoch or 'YYYY-MM-DD HH:MM')")


def imei_matches(target, imei):
    digits = re.sub(r'\D', '', target)
    return bool(digits) and (imei.endswith(digits) or imei == digits)


def _hm(ts):
    return fmt_time(ts)[5:19]


def _dur(sec):
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}h{m:02d}m" if h else (f"{m}m{s:02d}s" if m else f"{s}s")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('logfiles', nargs='+', help='one or more v2 gt06 logs (chronological)')
    ap.add_argument('--imei', required=True, help='unit: full IMEI, trailing digits, or G-prefixed id')
    ap.add_argument('--start', default=None, help="epoch or 'YYYY-MM-DD HH:MM'")
    ap.add_argument('--end', default=None, help="epoch or 'YYYY-MM-DD HH:MM'")
    ap.add_argument('--bucket-min', type=float, default=60.0, help='per-bucket size (default 60)')
    ap.add_argument('--lag', type=float, default=30.0, help='LOC lag > this = blind-buffer replay (default 30)')
    ap.add_argument('--disc-gap', type=float, default=120.0, help='HB gap > this implies a disconnect (default 120)')
    ap.add_argument('--cap-mah', type=float, default=6000.0, help='cell capacity for mA estimate (default 6000)')
    ap.add_argument('--soc-fit', default='gt06/battery_data/soc_fit.json')
    ap.add_argument('--prefix', default='G')
    args = ap.parse_args()

    start = parse_when(args.start)
    end = parse_when(args.end)

    # SoC curve (parametric OCV fit) + the unit's own divider offset.
    sid = args.prefix + re.sub(r'\D', '', args.imei)[-6:]
    try:
        import json
        fit = json.load(open(args.soc_fit))
        coeffs = fit['coeffs']
        soc_off = fit.get('offsets_mv', {}).get(sid, 0) / 1000.0
        def soc_of(v):
            c = coeffs
            return soc_model(v + soc_off, c['c1'], c['c2'], c['c3'], c['c4'])
    except Exception as e:
        print(f"(no SoC fit: {e}; battery shown as volts only)", file=sys.stderr)
        soc_of = None

    # ---- walk logs, collect this unit's frames --------------------------------
    # conn_id -> imei from login / 0xFA rotation marker / cxzt *ID:* replies, so
    # a rotated file self-attributes without the previous day's LOGIN. Frames on
    # a not-yet-identified conn are buffered and flushed once it identifies.
    tracker = ConnIdentityTracker()
    ev = []                       # (ts, kind, dict) for the target unit
    pending = {}                  # conn_id -> [(ts, kind, dict)] until identity known

    def frame_event(ts, proto, data, outgoing):
        """Decode one frame into a (ts, kind, dict) event, or None."""
        if outgoing:
            if proto == 0x80:                # server->device command
                return (ts, 'cmd', {'txt': bytes(data).decode('latin1', 'replace')})
            return None
        if proto == 0x01:
            return (ts, 'login', {})
        if proto in (0x12, 0x22):
            loc = gt06_parse_location(data)
            if loc and loc.get('ts'):
                return (ts, 'loc', {'lag': ts - loc['ts'], 'spd': loc['speed_kmh'] / 1.852,
                                    'sats': loc['satellites'], 'valid': loc['gps_valid']})
            return None
        if proto == 0x13:
            return (ts, 'hb', {})
        if proto == 0x15:                    # cxzt# / STATUS# string reply
            d = {}
            m = _BT.search(data)
            if m:
                d['bt'] = int(m.group(1)) / 1000.0
            m = _STAT_V.search(data)
            if m:
                d['statv'] = float(m.group(1))
            m = _ACC.search(data)
            if m:
                d['acc'] = m.group(1) == b'ON'
            m = _GPS.search(data)
            if m:
                d['gps'] = m.group(1) == b'ON'
            if d:
                return (ts, 'stat', d)
        return None

    for path in args.logfiles:
        with open(path, 'rb') as f:
            fmt = detect_format(f)
            if fmt == 'v1':
                raise SystemExit(f"{path}: v1 log has no conn IDs; can't attribute a unit")
            for ts, conn_id, outgoing, frame in read_packets(f, fmt=fmt):
                res = validate_frame(frame)
                if not res:
                    continue
                proto, data = res[0], res[1]
                tracker.feed(conn_id, outgoing, res)
                cur = tracker.get(conn_id)
                # NOTE: the 0xFA rotation marker and cxzt IDs give identity only —
                # they never produce events ('login' stays a session boundary).
                e = frame_event(ts, proto, data, outgoing)
                if cur is None:
                    if e is not None:
                        pending.setdefault(conn_id, []).append(e)
                    continue
                buf = pending.pop(conn_id, None)
                if buf and imei_matches(args.imei, cur):
                    ev.extend(buf)
                if not imei_matches(args.imei, cur):
                    continue
                if e is not None:
                    ev.append(e)

    ev = [e for e in ev if (start is None or e[0] >= start) and (end is None or e[0] <= end)]
    if not ev:
        print(f"No frames for {args.imei} in the given logs/window.")
        return
    ev.sort(key=lambda e: e[0])
    t0, t1 = ev[0][0], ev[-1][0]

    # ---- sessions: split on login and on HB gaps > disc-gap -------------------
    sessions = []                 # dict(start,end,loc,hb,stat,replay,bt0)
    cur = None
    last_hb = None
    for ts, kind, d in ev:
        newsess = cur is None or kind == 'login' or (kind == 'hb' and last_hb and ts - last_hb > args.disc_gap)
        if newsess:
            cur = {'start': ts, 'end': ts, 'loc': 0, 'hb': 0, 'stat': 0, 'replay': 0, 'bt0': None,
                   'gap_before': (ts - last_hb) if (last_hb and cur) else 0}
            sessions.append(cur)
        cur['end'] = ts
        if kind == 'hb':
            cur['hb'] += 1
            last_hb = ts
        elif kind == 'loc':
            cur['loc'] += 1
            if d['lag'] > args.lag:
                cur['replay'] += 1
        elif kind == 'stat':
            cur['stat'] += 1
            if cur['bt0'] is None and 'bt' in d:
                cur['bt0'] = d['bt']

    # ---- per-hour buckets -----------------------------------------------------
    bsec = args.bucket_min * 60
    buckets = {}                  # bucket_start -> counts
    def bk(ts):
        b = t0 + int((ts - t0) // bsec) * bsec
        return buckets.setdefault(b, {'hb': 0, 'loc': 0, 'stat': 0, 'login': 0,
                                      'gps_on': 0, 'gps_n': 0, 'replay': 0, 'v': []})
    for ts, kind, d in ev:
        b = bk(ts)
        if kind == 'hb':
            b['hb'] += 1
        elif kind == 'loc':
            b['loc'] += 1
            if d['lag'] > args.lag:
                b['replay'] += 1
        elif kind == 'login':
            b['login'] += 1
        elif kind == 'stat':
            b['stat'] += 1
            if 'gps' in d:
                b['gps_n'] += 1
                b['gps_on'] += 1 if d['gps'] else 0
            v = d.get('bt') or d.get('statv')
            if v:
                b['v'].append((v, d.get('gps'), d.get('acc')))

    # ---- resting SoC trajectory (STATUS GPS:OFF & ACC:OFF = low load) ----------
    rest = [(ts, d.get('bt') or d.get('statv')) for ts, k, d in ev
            if k == 'stat' and d.get('gps') is False and d.get('acc') is False and (d.get('bt') or d.get('statv'))]

    # ================= output =================
    disc = [s for s in sessions if s['gap_before'] > args.disc_gap]
    off_tot = sum(s['gap_before'] for s in disc)
    loc_tot = sum(s['loc'] for s in sessions)
    replay_tot = sum(s['replay'] for s in sessions)
    stat_tot = sum(s['stat'] for s in sessions)
    conn_tot = sum(s['end'] - s['start'] for s in sessions)
    span = t1 - t0

    print(f"Unit {sid}  ({args.imei})   {_hm(t0)} .. {_hm(t1)}   span {_dur(span)}")
    print(f"connected {_dur(conn_tot)} ({100*conn_tot/span:.0f}%),  "
          f"offline {_dur(off_tot)} in {len(disc)} disconnect(s)")
    print(f"LOC {loc_tot} ({replay_tot} blind-replay, lag>{args.lag:.0f}s),  "
          f"STATUS polls {stat_tot} (~1/{span/stat_tot:.0f}s)" if stat_tot else "")

    print(f"\n=== SESSIONS ({len(sessions)}) ===")
    print(f"{'start':14s} {'dur':>7s} {'off_before':>10s} {'HB':>5s} {'STAT':>5s} {'LOC':>4s} {'rply':>4s} {'BT0':>6s}")
    for s in sessions:
        off = _dur(s['gap_before']) if s['gap_before'] > args.disc_gap else '-'
        bt0 = f"{s['bt0']:.3f}" if s['bt0'] else '-'
        print(f"{_hm(s['start']):14s} {_dur(s['end']-s['start']):>7s} {off:>10s} "
              f"{s['hb']:5d} {s['stat']:5d} {s['loc']:4d} {s['replay']:4d} {bt0:>6s}")

    print(f"\n=== PER-{int(args.bucket_min)}min ===")
    print(f"{'bucket':14s} {'conn%':>6s} {'HB/min':>7s} {'STAT':>5s} {'LOC':>4s} {'rply':>4s} {'GPSon':>6s} {'SoC%':>6s} {'V':>7s}")
    for b0 in sorted(buckets):
        b = buckets[b0]
        conn_pct = 100.0 * min(1.0, b['hb'] * 15.0 / bsec)      # HB ~ every 15 s when connected
        gps = f"{100*b['gps_on']/b['gps_n']:.0f}%" if b['gps_n'] else '-'
        vlist = [v for v, g, a in b['v']]
        vshow = f"{sum(vlist)/len(vlist):.3f}" if vlist else '-'
        # SoC from the lowest-load reading in the bucket (prefer GPS:OFF)
        restv = [v for v, g, a in b['v'] if g is False and a is False] or vlist
        soc = f"{soc_of(sum(restv)/len(restv)):.0f}" if (restv and soc_of) else '-'
        print(f"{_hm(b0):14s} {conn_pct:5.0f}% {b['hb']*60.0/bsec:7.1f} {b['stat']:5d} "
              f"{b['loc']:4d} {b['replay']:4d} {gps:>6s} {soc:>6s} {vshow:>7s}")

    if rest and soc_of:
        (ta, va), (tb, vb) = rest[0], rest[-1]
        sa, sb = soc_of(va), soc_of(vb)
        hrs = (tb - ta) / 3600.0
        if hrs > 0:
            dsoc = sa - sb
            ma = args.cap_mah * (dsoc / hrs) / 100.0
            print(f"\n=== RESTING SoC DRAIN (GPS:OFF & ACC:OFF readings, load-sag excluded) ===")
            print(f"{_hm(ta)}  {va:.3f}V = {sa:.1f}%   ->   {_hm(tb)}  {vb:.3f}V = {sb:.1f}%")
            print(f"drop {dsoc:.1f}% over {_dur(tb-ta)}  =>  {dsoc/hrs:.2f}%/h,  "
                  f"~{ma:.0f} mA  ({len(rest)} resting reads; cap {args.cap_mah:.0f} mAh)")
        else:
            print("\n(resting SoC: too few points for a rate)")
    elif soc_of:
        print("\n(no GPS:OFF/ACC:OFF resting readings for a clean drain fit)")


if __name__ == '__main__':
    main()
