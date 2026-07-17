#!/usr/bin/env python3
"""Fleet surface-charge / weak-cell screen over v2 gt06 logs.

A weak cell can rest at a healthy-looking voltage while holding far less real
charge than the voltage->SoC curve implies (G375430/G347082, Yeppoon 2026:
normal 4.05 V at rest, collapse the moment race load came on). This script
walks v2 gt06 logs and measures, per unit:

  * LOAD-ON SAG    -- step from the last resting voltage (STATUS GPS:OFF &
                      ACC:OFF) to the first loaded reading at each idle->
                      tracking transition, and the apparent resistance dV/dI.
                      ONLY meaningful at low report cadence (~115 mA): under
                      1 Hz reporting the radio TX duty depresses every loaded
                      reading (half the healthy fleet "sagged" 300-500 mV at
                      the 07-10 marathon start), so events whose LOC rate
                      exceeds --fast-loc are excluded from the sag verdict.
  * HOLLOW RATIO   -- rest-to-rest energy accounting per load window:
                      indicated charge lost (SoC(V_rest_before) -
                      SoC(V_rest_recovered_after) x capacity) vs the maximum
                      the electronics could have drawn (window hours x
                      --imax-ma). A healthy cell stays near 1; a surface-
                      charge cell indicated ~5.7 Ah lost in a 56-minute race
                      (~0.2 Ah drawn) = ratio >20. Cadence-immune because it
                      only uses resting reads (recovered: max rest V in the
                      hours after load-off, so polarisation rebound is
                      counted, capped at the next charge/load).
                      The verdict is PEER-RELATIVE per day: on the Yeppoon
                      07-10 long course the whole fleet ran hot (offshore
                      max-power TX / out-of-coverage hunts pushed real drain
                      far past any fixed budget) and most aged cells showed
                      hollow 4-9x, yet the two true weak cells sat at 21-24x.
                      A window flags only when its ratio is both >= --hollow
                      and >= --rel times the same-day fleet median (absolute
                      rule alone when fewer than 4 peer windows that day).
  * CHARGE PEAK    -- highest settled resting voltage after each charging
                      interval. A cell that won't take a full charge plateaus
                      low (G375430: 4.05 V vs fleet-full ~4.15+).

Combine daily rotations first so logins stay attributed:
  cat old_logs/gt06.log.DAY1 <(tail -c +9 old_logs/gt06.log.DAY2) ... > comb.log

Usage:
  scripts/gt06_sag_screen.py comb.log
  scripts/gt06_sag_screen.py comb.log --events --imei 375430
  scripts/gt06_sag_screen.py comb.log --json screen.json
"""
import sys
import re
import json
import time
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gt06_dump import (read_packets, detect_format, validate_frame, fmt_time,  # noqa: E402
                       ConnIdentityTracker)
from protocol_GT06 import gt06_parse_heartbeat  # noqa: E402
from gt06_soc import SocModel, gid_of  # noqa: E402

_STAT_V = re.compile(rb'Battery:(\d+\.\d+)V')
_ACC = re.compile(rb'ACC:(ON|OFF)')
_GPS = re.compile(rb'GPS:([^;]*)')


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
    raise SystemExit(f"can't parse time {s!r}")


class Unit:
    def __init__(self, gid, imei):
        self.gid = gid
        self.imei = imei
        self.last_rest = None        # (ts, volts) latest resting read
        self.win = None              # open load window
        self.windows = []            # closed load windows
        self.charge_iv = []          # merged [start, end] charging intervals
        self.rest_hist = []          # (ts, volts) resting reads, chronological
        self.loc_count = 0           # LOCs since window open

    def feed_charging(self, ts):
        if self.charge_iv and ts - self.charge_iv[-1][1] < 300:
            self.charge_iv[-1][1] = ts
        else:
            self.charge_iv.append([ts, ts])

    def near_charging(self, ts, pad=300):
        for iv in reversed(self.charge_iv):
            if iv[0] - pad <= ts <= iv[1] + pad:
                return True
            if iv[1] + pad < ts:
                break
        return False


class Screen:
    def __init__(self, cfg):
        self.cfg = cfg
        self.units = {}

    def unit(self, imei):
        # key by G<last6>: the TERIID login id and hardware IMEI of one
        # physical unit share their trailing digits
        gid = gid_of(imei)
        u = self.units.get(gid)
        if u is None:
            u = self.units[gid] = Unit(gid, imei)
        return u

    def close_window(self, u):
        w = u.win
        u.win = None
        if not w or not w['loads']:
            return
        w['t_off'] = w['loads'][-1][0]
        w['dur_h'] = (w['t_off'] - w['t_on']) / 3600.0
        first = w['loads'][0][1]
        w['sag_mv'] = (w['rest_v'] - first) * 1000.0 if w['rest_v'] else None
        w['loc_rate'] = w['locs'] / max(1.0, w['t_off'] - w['t_on'])
        w['loads_n'] = len(w['loads'])
        del w['loads']
        u.windows.append(w)

    def feed_status(self, u, ts, volts, gps_on, acc_on):
        cfg = self.cfg
        if not (gps_on or acc_on):
            # a resting read always ends any open load window
            if u.win:
                self.close_window(u)
            if not u.near_charging(ts):
                u.last_rest = (ts, volts)
                u.rest_hist.append((ts, volts))
            return
        if u.win:
            if ts - u.win['loads'][-1][0] > cfg.win_gap:
                self.close_window(u)      # long silence: treat as separate window
            else:
                u.win['loads'].append((ts, volts))
                u.win['locs'] = u.loc_count
                return
        if not u.near_charging(ts):
            # a stale resting read (device offline / long tracking gap before
            # this window) can't anchor sag or energy accounting
            rts, rv = u.last_rest if (
                u.last_rest and ts - u.last_rest[0] <= cfg.rest_window) else (None, None)
            u.loc_count = 0
            u.win = {'t_on': ts, 'rest_ts': rts, 'rest_v': rv,
                     'rest_age': (ts - rts) if rts else None,
                     'loads': [(ts, volts)], 'locs': 0}

    def feed(self, ts, imei, proto, data, outgoing):
        if outgoing:
            return
        u = self.unit(imei)
        if proto in (0x12, 0x22):
            u.loc_count += 1
        elif proto == 0x13:
            hb = gt06_parse_heartbeat(data)
            if hb.get('charging'):
                u.feed_charging(ts)
        elif proto == 0x15:
            m = _STAT_V.search(data)
            if not m:
                return
            mg = _GPS.search(data)
            ma = _ACC.search(data)
            if mg is None or ma is None:
                return
            self.feed_status(u, ts, float(m.group(1)),
                             not mg.group(1).startswith(b'OFF'),
                             ma.group(1) == b'ON')

    def finish(self, soc):
        cfg = self.cfg
        for u in self.units.values():
            self.close_window(u)
            cap = soc.capacity_mah(u.gid)
            # rest-to-rest energy accounting per window
            for i, w in enumerate(u.windows):
                # recovery rest V: max resting read after load-off, before the
                # next load window / charge / --recover-h horizon
                lim = w['t_off'] + cfg.recover_h * 3600
                if i + 1 < len(u.windows):
                    lim = min(lim, u.windows[i + 1]['t_on'])
                for iv in u.charge_iv:
                    if iv[0] > w['t_off']:
                        lim = min(lim, iv[0])
                        break
                after = [v for t, v in u.rest_hist
                         if w['t_off'] < t <= lim]
                if not after or w['rest_v'] is None \
                        or w['dur_h'] * 60 < cfg.min_window_min:
                    w['hollow'] = None
                    continue
                v_rec = max(after)
                # conservative indicated loss: +-vmargin absorbs the 10 mV
                # STATUS quantisation and residual polarisation, which in the
                # flat OCV plateau (3.6-3.8 V) otherwise read as huge SoC steps
                vm = cfg.vmargin_mv / 1000.0
                ind_pct = (soc.soc_of(u.gid, w['rest_v'] - vm)
                           - soc.soc_of(u.gid, v_rec + vm))
                ind_mah = ind_pct / 100.0 * cap
                drawn_mah = w['dur_h'] * cfg.imax_ma
                w['v_rec'] = v_rec
                w['ind_mah'] = ind_mah
                w['hollow'] = (ind_mah / drawn_mah) if ind_mah > 0 else 0.0
                w['hollow_ok'] = ind_mah >= cfg.min_ind_mah
            # per-charge settled peak
            u.charges = []
            for iv in u.charge_iv:
                after = [(t, v) for t, v in u.rest_hist
                         if iv[1] + 600 <= t <= iv[1] + 6 * 3600]
                if after:
                    u.charges.append({'start': iv[0], 'end': iv[1],
                                      'peak_v': max(v for _, v in after)})


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('logfiles', nargs='+')
    ap.add_argument('--imei', default=None, help='only this unit (trailing digits ok)')
    ap.add_argument('--start', default=None)
    ap.add_argument('--end', default=None)
    ap.add_argument('--sag-mv', type=float, default=150.0,
                    help='flag low-cadence load-on sag above this (default 150)')
    ap.add_argument('--fast-loc', type=float, default=0.5,
                    help='LOC/s above this = high-TX window, sag not judged (default 0.5)')
    ap.add_argument('--hollow', type=float, default=3.0,
                    help='flag indicated/drawn charge ratio above this (default 3)')
    ap.add_argument('--rel', type=float, default=3.0,
                    help='also require ratio >= this x same-day fleet median '
                         'when >=4 peer windows exist (default 3)')
    ap.add_argument('--imax-ma', type=float, default=250.0,
                    help='max plausible average draw for the energy budget (default 250)')
    ap.add_argument('--min-ind-mah', type=float, default=400.0,
                    help='ignore windows that indicate less than this lost (default 400)')
    ap.add_argument('--vmargin-mv', type=float, default=20.0,
                    help='voltage uncertainty margin on the indicated loss (default 20)')
    ap.add_argument('--min-window-min', type=float, default=15.0,
                    help='ignore load windows shorter than this (default 15)')
    ap.add_argument('--min-rest-v', type=float, default=3.50,
                    help='only judge events starting at/above this resting V (default 3.50)')
    ap.add_argument('--recover-h', type=float, default=16.0,
                    help='rest-recovery search horizon after load-off (default 16 h)')
    ap.add_argument('--rest-window', type=float, default=600.0,
                    help='max age of the resting read anchoring a window (default 600 s)')
    ap.add_argument('--win-gap', type=float, default=7200.0,
                    help='loaded-read silence that splits a window (default 7200 s; '
                         'resting reads always split)')
    ap.add_argument('--events', action='store_true', help='print every load window')
    ap.add_argument('--json', default=None, help='write full results to this file')
    args = ap.parse_args()

    start, end = parse_when(args.start), parse_when(args.end)
    want = re.sub(r'\D', '', args.imei) if args.imei else None
    soc = SocModel()

    scr = Screen(args)
    tracker = ConnIdentityTracker()
    pending = {}                  # conn_id -> [(ts, proto, data, outgoing)]
    for path in args.logfiles:
        with open(path, 'rb') as f:
            fmt = detect_format(f)
            if fmt == 'v1':
                raise SystemExit(f"{path}: v1 log has no conn IDs")
            for ts, conn_id, outgoing, frame in read_packets(f, fmt=fmt):
                res = validate_frame(frame)
                if not res:
                    continue
                proto, data = res[0], res[1]
                src = tracker.feed(conn_id, outgoing, res)
                imei = tracker.get(conn_id)
                if imei is None:
                    pending.setdefault(conn_id, []).append((ts, proto, data, outgoing))
                    if len(pending[conn_id]) > 4096:
                        del pending[conn_id]
                    continue
                buf = pending.pop(conn_id, None)
                # a LOGIN starts a new connection: frames buffered before it on
                # this (reused) conn_id belonged to someone else — drop them
                if src == 'login':
                    buf = None
                items = (buf or []) + [(ts, proto, data, outgoing)]
                if want and not imei.endswith(want):
                    continue
                for its, iproto, idata, iout in items:
                    if (start and its < start) or (end and its > end):
                        continue
                    scr.feed(its, imei, iproto, idata, iout)
    scr.finish(soc)

    # same-day fleet median hollow, to normalise out day-wide conditions
    # (offshore TX power, out-of-coverage hunts, temperature)
    byday = {}
    for u in scr.units.values():
        for w in u.windows:
            if w.get('hollow') is not None and w.get('hollow_ok'):
                day = fmt_time(w['t_on'])[:10]
                w['day'] = day
                byday.setdefault(day, []).append(w['hollow'])
    daymed = {d: sorted(v)[len(v) // 2] for d, v in byday.items()}

    # ---- verdicts ----
    def judge(u):
        sag_bad, hollow_bad = [], []
        for w in u.windows:
            if not w['rest_v'] or w['rest_v'] < args.min_rest_v:
                continue
            if w['loc_rate'] <= args.fast_loc and w['sag_mv'] is not None \
                    and w['sag_mv'] >= args.sag_mv:
                sag_bad.append(w)
            if w.get('hollow') is not None and w.get('hollow_ok') \
                    and w['hollow'] >= args.hollow:
                peers = byday.get(w.get('day'), [])
                w['day_med'] = daymed.get(w.get('day'))
                if len(peers) < 4 or w['hollow'] >= args.rel * daymed[w['day']]:
                    hollow_bad.append(w)
        return sag_bad, hollow_bad

    print(f"{'unit':8s} {'cls':>3s} {'win':>4s} {'worst sag*':>10s} {'@ time':11s} "
          f"{'hollow':>7s} {'@ time':11s} {'ind/drawn mAh':>14s} {'chg peak':>8s} verdict")
    rows = []
    for u in sorted(scr.units.values(), key=lambda u: u.gid):
        sag_bad, hollow_bad = judge(u)
        slow = [w for w in u.windows
                if w['loc_rate'] <= args.fast_loc and w['sag_mv'] is not None
                and w['rest_v'] >= args.min_rest_v]
        wsag = max(slow, key=lambda w: w['sag_mv']) if slow else None
        whol = max((w for w in u.windows if w.get('hollow') is not None),
                   key=lambda w: w['hollow'], default=None)
        verdict = '-'
        if u.windows:
            verdict = 'SUSPECT' if (sag_bad or hollow_bad) else 'PASS'
        why = []
        if sag_bad:
            why.append(f"sag x{len(sag_bad)}")
        if hollow_bad:
            why.append(f"hollow x{len(hollow_bad)}")
        peak = max((c['peak_v'] for c in getattr(u, 'charges', [])), default=None)
        s_sag = f"{wsag['sag_mv']:.0f}mV" if wsag else '-'
        s_sagt = fmt_time(wsag['t_on'])[5:16] if wsag else '-'
        s_hol = f"{whol['hollow']:.1f}x" if whol else '-'
        s_holt = fmt_time(whol['t_on'])[5:16] if whol else '-'
        s_mah = (f"{whol['ind_mah']:.0f}/{whol['dur_h']*args.imax_ma:.0f}"
                 if whol else '-')
        s_pk = f"{peak:.3f}" if peak else '-'
        print(f"{u.gid:8s} {soc.cap_class(u.gid):>3s} {len(u.windows):4d} "
              f"{s_sag:>10s} {s_sagt:11s} {s_hol:>7s} {s_holt:11s} "
              f"{s_mah:>14s} {s_pk:>8s} {verdict}"
              + (f"  ({', '.join(why)})" if why else ''))
        rows.append((u, sag_bad, hollow_bad))

    print("\n(*worst sag over low-cadence windows only; high-TX 1 Hz windows excluded)")

    if args.events:
        print("\n=== LOAD WINDOWS ===")
        for u, sag_bad, hollow_bad in rows:
            for w in u.windows:
                mode = 'FAST' if w['loc_rate'] > args.fast_loc else 'slow'
                flags = []
                if w in sag_bad:
                    flags.append('SAG')
                if w in hollow_bad:
                    flags.append('HOLLOW')
                h = (f"hollow {w['hollow']:.1f}x (ind {w['ind_mah']:.0f} mAh, rec "
                     f"{w['v_rec']:.2f}V)" if w.get('hollow') is not None else 'hollow -')
                rest = f"{w['rest_v']:.2f}V" if w['rest_v'] else 'stale'
                sag = f"{w['sag_mv']:+.0f}mV" if w['sag_mv'] is not None else '-'
                print(f"{u.gid} {fmt_time(w['t_on'])[:19]} {w['dur_h']*60:6.0f}min "
                      f"{mode} loc/s {w['loc_rate']:.2f}  rest {rest} "
                      f"sag {sag}  {h}"
                      + (f"  <-- {'+'.join(flags)}" if flags else ''))

    if args.json:
        out = {u.gid: {'imei': u.imei,
                       'verdict': ('SUSPECT' if (sb or hb) else ('PASS' if u.windows else '-')),
                       'windows': u.windows, 'charges': getattr(u, 'charges', [])}
               for u, sb, hb in rows}
        with open(args.json, 'w') as f:
            json.dump(out, f, indent=1, default=float)
        print(f"\nwrote {args.json}")


if __name__ == '__main__':
    main()
