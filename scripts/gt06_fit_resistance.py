#!/usr/bin/env python3
"""Per-unit internal resistance R from a TWO-LOAD comparison.

Old R (gt06_calibration.json) came from a shallow plateau(I=0)+tracking window and was
flagged soft/ill-conditioned. We now have two clean, well-separated load levels at
overlapping SoC:

  * tracking discharge ~115 mA  (gt06/battery_data/tracking.csv.gz)
  * idle               ~6 mA    (idle test, cxzt 1 mV voltages)

At a matched SoC s (matched via the fitted OCV curve soc_fit.json):

  V_idle  = OCV(s) - I_idle *R
  V_track = OCV(s) - I_track*R
  =>  R = (V_idle - V_track) / (I_track - I_idle)

The per-unit divider OFFSET cancels (same hardware reads both loads); a divider GAIN
error survives only as a ~few-% scale on R. I_track = P/V_track (constant power), so it
is taken at the matched SoC. The idle voltage (~6 mA) is ~OCV (IR drop ~3 mV).

  python3 gt06_fit_resistance.py --idle-log /tmp/gt06_comb.log \
      --idle-start 1782020506 --idle-end 1782057600 \
      --data gt06/battery_data --cal WebUI/gt06_calibration.json \
      --soc-fit gt06/battery_data/soc_fit.json
"""
import argparse, csv, gzip, json, os, re, statistics, struct
from collections import defaultdict

DIR_OUT = 0x80000000
_BT = re.compile(rb'\*BT:(\d+)')
THREE = {"G312243", "G312268", "G312292", "G312342", "G375349", "G375356",
         "G375372", "G375539", "G375562", "G378657", "G226122"}


def soc_model(v, c):
    s = c['c1'] * (1.0 - 1.0 / (1.0 + (v / c['c2']) ** c['c4']) ** c['c3'])
    return max(0.0, min(100.0, s))


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


def idle_voltage(path, start, end, prefix='G'):
    """Per-unit median idle cxzt voltage (V) over [start,end]."""
    conn_sid, series = {}, defaultdict(list)
    for ts, cid, out, proto, data in walk(path):
        if out:
            continue
        if proto == 0x01:
            h = data.hex().lstrip('0')
            if len(h) == 16:
                h = h[1:]
            if len(h) >= 6:
                conn_sid[cid] = prefix + h[-6:]
            continue
        if proto != 0x15:
            continue
        sid = conn_sid.get(cid)
        if not sid or ts < start or (end and ts > end):
            continue
        m = _BT.search(data)
        if m:
            series[sid].append(int(m.group(1)) / 1000.0)
    return {g: (statistics.median(v), len(v)) for g, v in series.items() if len(v) >= 5}


def discharge_curve(data_dir, unplug, P, cutoff_v=3.29, max_gap=300.0):
    """Per-unit [(SoC, V_track)] via constant-power charge integration."""
    rows = defaultdict(list)
    with gzip.open(os.path.join(data_dir, 'tracking.csv.gz'), 'rt') as f:
        for d in csv.DictReader(f):
            rows[d['id']].append((int(d['ts']), float(d['bat_v']), int(d['chg'])))
    out = {}
    for g, recs in rows.items():
        recs.sort()
        off = [(t, v) for t, v, c in recs if not c and t >= unplug and v >= cutoff_v]
        if len(off) < 50:
            continue
        Q, qs = 0.0, [0.0]
        for i in range(1, len(off)):
            dt = min(off[i][0] - off[i - 1][0], max_gap)
            vmid = 0.5 * (off[i][1] + off[i - 1][1])
            Q += (P / vmid) * (dt / 3600.0)
            qs.append(Q)
        Qtot = qs[-1]
        if Qtot <= 0:
            continue
        pts = sorted((100.0 * (Qtot - q) / Qtot, v) for (t, v), q in zip(off, qs))
        out[g] = pts
    return out


def interp_v(pts, soc):
    """V_track at a given SoC from sorted (SoC, V) ascending."""
    if soc <= pts[0][0]:
        return pts[0][1]
    if soc >= pts[-1][0]:
        return pts[-1][1]
    for i in range(1, len(pts)):
        if pts[i][0] >= soc:
            s0, v0 = pts[i - 1]
            s1, v1 = pts[i]
            f = (soc - s0) / (s1 - s0) if s1 > s0 else 0
            return v0 + f * (v1 - v0)
    return pts[-1][1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--idle-log', required=True)
    ap.add_argument('--idle-start', type=float, required=True)
    ap.add_argument('--idle-end', type=float, default=None)
    ap.add_argument('--idle-ma', type=float, default=6.0)
    ap.add_argument('--data', default='gt06/battery_data')
    ap.add_argument('--cal', default='WebUI/gt06_calibration.json')
    ap.add_argument('--soc-fit', default='gt06/battery_data/soc_fit.json')
    ap.add_argument('--out', default=None)
    a = ap.parse_args()

    meta = json.load(open(os.path.join(a.data, 'meta.json')))
    coeffs = json.load(open(a.soc_fit))['coeffs']
    cal = json.load(open(a.cal))
    P = meta['power_w']

    vidle = idle_voltage(a.idle_log, a.idle_start, a.idle_end)
    dis = discharge_curve(a.data, meta['unplug_epoch'], P)
    I_idle = a.idle_ma / 1000.0

    # SUPERSEDED by scripts/gt06_fit_joint.py, which solves R jointly with the curve and
    # is offset-aware. This standalone is a first-pass illustration; it applies the cal
    # divider offset to the SoC match but still ignores per-unit gain. Per-unit R is NOT
    # identifiable cross-run (see gt06/battery_data/METHOD.md s6) — read class medians only.
    cal_off = cal.get('offsets', {})
    rows = []
    for g in sorted(set(vidle) & set(dis)):
        vi, n = vidle[g]
        # remove divider offset before the SoC lookup so offset cancellation in ΔV is valid
        soc = soc_model(vi + cal_off.get(g, 0.0), coeffs)   # idle V ~= OCV -> SoC
        vt = interp_v(dis[g], soc)             # tracking terminal V at same SoC
        I_trk = P / vt
        R = (vi - vt) / (I_trk - I_idle)
        cls = '3Ah' if g in THREE else '6Ah'
        Rold = cal.get('units', {}).get(g, {}).get('resistance_ohm')
        rows.append((g, cls, vi, soc, vt, I_trk * 1000, R, Rold))

    print(f"two-load R: idle {a.idle_ma:.0f}mA (cxzt) vs tracking ~{statistics.median(r[5] for r in rows):.0f}mA")
    print(f"{'unit':8} {'cls':4} {'V_idle':>7} {'SoC':>6} {'V_trk':>7} {'I_trk':>6} "
          f"{'R_new':>6} {'R_old':>6} {'dmV@115':>8}")
    for g, cls, vi, soc, vt, itrk, R, Rold in rows:
        print(f"{g:8} {cls:4} {vi:>7.3f} {soc:>5.1f}% {vt:>7.3f} {itrk:>5.0f}m "
              f"{R:>6.3f} {Rold if Rold else 0:>6.3f} {(vi-vt)*1000:>7.1f}m")

    for cls in ('3Ah', '6Ah'):
        rs = [r[6] for r in rows if r[1] == cls and 8 <= r[3] <= 92]   # well-resolved SoC
        if rs:
            print(f"\n  {cls} R (SoC 8-92%): n={len(rs)} median={statistics.median(rs):.3f} "
                  f"mean={statistics.mean(rs):.3f} sd={statistics.pstdev(rs):.3f} ohm  "
                  f"range {min(rs):.3f}-{max(rs):.3f}")

    if a.out:
        doc = {'method': 'two-load idle(~6mA) vs tracking(~115mA), SoC-matched via OCV curve; '
                         'divider offset cancels',
               'idle_ma': a.idle_ma,
               'units': {r[0]: {'R_ohm': round(r[6], 4), 'soc_pct': round(r[3], 1),
                                'cap_class': r[1]} for r in rows}}
        json.dump(doc, open(a.out, 'w'), indent=1)
        print(f"\nwrote {a.out}")


if __name__ == '__main__':
    main()
