#!/usr/bin/env python3
"""Sleep power draw from two low-noise idle endpoints.

The cxzt reads taken DURING MODE5 sleep are noisy: each hourly wake polls cxzt# while
the unit just powered on under a changing load (modem/GPS spin-up) — non-monotonic,
~20 mV scatter. A slope through them is unusable.

Instead bracket the sleep with two PARKED-IDLE cxzt endpoints (idle cxzt is stable to
~1 mV): the last idle reading before sleep, and the first settled idle reading after the
units return to race-idle. The voltage drop between them over the sleep duration gives the
average overnight sleep power (including the per-wake overhead — the real battery cost).

Operates on gt06/battery_data/sleep.csv.gz (id,ts,source,v_mv,phase), the new SoC curve
(soc_fit.json) and capacities (WebUI/gt06_calibration.json).

  python3 gt06_sleep_current.py --sleep-start 1782057600 --wake-end 1782079200
"""
import argparse, csv, gzip, json, os, statistics


def soc_model(v, c):
    s = c['c1'] * (1.0 - 1.0 / (1.0 + (v / c['c2']) ** c['c4']) ** c['c3'])
    return max(0.0, min(100.0, s))


THREE = {"G312243", "G312268", "G312292", "G312342", "G375349", "G375356",
         "G375372", "G375539", "G375562", "G378657", "G226122"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', default='gt06/battery_data')
    ap.add_argument('--cal', default='WebUI/gt06_calibration.json')
    ap.add_argument('--soc-fit', default='gt06/battery_data/soc_fit.json')
    ap.add_argument('--sleep-start', type=float, required=True, help='epoch units slept (~02:00)')
    ap.add_argument('--wake-end', type=float, required=True, help='epoch units returned to idle (~08:00)')
    ap.add_argument('--pre-window-min', type=float, default=30, help='median pre-idle cxzt over the N min before sleep')
    ap.add_argument('--post-settle-min', type=float, default=12, help='ignore post-idle reads until N min after wake (settle)')
    ap.add_argument('--post-window-min', type=float, default=40, help='median post-idle cxzt over a N min window after settle')
    ap.add_argument('--idle-ma', type=float, default=6.0, help='idle current for the post-settle tail correction')
    ap.add_argument('--vnom', type=float, default=None, help='nominal V for power (default: per-unit mean endpoint V)')
    ap.add_argument('--out', default=None)
    a = ap.parse_args()

    coeffs = json.load(open(a.soc_fit))['coeffs']
    fit_off = {g: mv / 1000.0 for g, mv in json.load(open(a.soc_fit)).get('offsets_mv', {}).items()}
    cap = {g: u.get('capacity_mah') for g, u in json.load(open(a.cal))['units'].items()}

    # gather cxzt readings by phase
    pre, post, wake = {}, {}, {}
    with gzip.open(os.path.join(a.data, 'sleep.csv.gz'), 'rt') as f:
        for d in csv.DictReader(f):
            if d['source'] != 'cxzt':
                continue
            g, ts, v = d['id'], int(d['ts']), int(d['v_mv']) / 1000.0
            ph = d['phase']
            if ph == 'pre_idle' and ts >= a.sleep_start - a.pre_window_min * 60:
                pre.setdefault(g, []).append((ts, v))
            elif ph == 'post_idle' and a.wake_end + a.post_settle_min * 60 <= ts <= a.wake_end + (a.post_settle_min + a.post_window_min) * 60:
                post.setdefault(g, []).append((ts, v))
            elif ph == 'sleep_wake':
                wake.setdefault(g, []).append((ts, v))

    cls_of = lambda g: '3Ah' if g in THREE else '6Ah'
    rows = []
    for g in sorted(set(pre) & set(post)):
        v_pre = statistics.median(v for _, v in pre[g])
        v_post = statistics.median(v for _, v in post[g])
        t_post = statistics.median(t for t, _ in post[g])
        soc_pre = soc_model(v_pre + fit_off.get(g, 0.0), coeffs)
        soc_post = soc_model(v_post + fit_off.get(g, 0.0), coeffs)
        c = cap.get(g)
        if not c:
            continue
        q_total = (soc_pre - soc_post) / 100.0 * c                       # mAh drained
        q_idle = a.idle_ma * (t_post - a.wake_end) / 3600.0              # idle-tail mAh
        sleep_h = (a.wake_end - a.sleep_start) / 3600.0
        sleep_ma = (q_total - q_idle) / sleep_h
        vn = a.vnom if a.vnom else 0.5 * (v_pre + v_post)
        sleep_w = sleep_ma / 1000.0 * vn
        rows.append((g, cls_of(g), v_pre, v_post, (v_pre - v_post) * 1000, soc_pre - soc_post,
                     c, sleep_ma, sleep_w * 1000, len(wake.get(g, []))))

    print(f"sleep window {(a.wake_end-a.sleep_start)/3600:.2f}h  idle-tail {a.idle_ma:.0f}mA  "
          f"units={len(rows)}")
    print(f"{'unit':8} {'cls':4} {'Vpre':>6} {'Vpost':>6} {'dmV':>5} {'dSoC':>5} {'cap':>5} "
          f"{'mA':>6} {'mW':>6} {'wk':>3}")
    for g, cl, vp, vq, dmv, dsoc, c, ma, mw, nw in rows:
        print(f"{g:8} {cl:4} {vp:>6.3f} {vq:>6.3f} {dmv:>5.0f} {dsoc:>5.1f} {c:>5} "
              f"{ma:>6.1f} {mw:>6.1f} {nw:>3}")

    print("\nPer-class sleep power (median / mean / sd across units):")
    for cl in ('3Ah', '6Ah'):
        ws = [r[8] for r in rows if r[1] == cl]
        ms = [r[7] for r in rows if r[1] == cl]
        if ws:
            print(f"  {cl}: n={len(ws)}  current med={statistics.median(ms):.1f} "
                  f"mean={statistics.mean(ms):.1f} mA  |  power med={statistics.median(ws):.0f} "
                  f"mean={statistics.mean(ws):.0f} sd={statistics.pstdev(ws):.0f} mW")

    # validation: naive slope through the noisy sleep-wake cxzt
    print("\nValidation — naive wake-slope estimate (should be noisy/divergent):")
    for cl in ('3Ah', '6Ah'):
        mas = []
        for g in sorted(wake):
            if cls_of(g) != cl or g not in cap or not cap[g] or len(wake[g]) < 3:
                continue
            pts = sorted(wake[g])
            xs = [(t - pts[0][0]) / 3600 for t, _ in pts]
            ys = [soc_model(v + fit_off.get(g, 0.0), coeffs) for _, v in pts]
            n = len(xs); sx = sum(xs); sy = sum(ys)
            sxx = sum(x * x for x in xs); sxy = sum(x * y for x, y in zip(xs, ys))
            d = n * sxx - sx * sx
            if d:
                slope = (n * sxy - sx * sy) / d
                mas.append(-slope / 100 * cap[g])
        if mas:
            print(f"  {cl}: n={len(mas)} wake-slope mA median={statistics.median(mas):.1f} "
                  f"range {min(mas):.1f}..{max(mas):.1f}  (vs clean two-endpoint above)")

    if a.out:
        json.dump({'method': 'two low-noise idle endpoints bracketing MODE5 sleep',
                   'sleep_hours': round((a.wake_end - a.sleep_start) / 3600, 2),
                   'idle_tail_ma': a.idle_ma,
                   'per_class_mw': {cl: round(statistics.median([r[8] for r in rows if r[1] == cl]), 1)
                                    for cl in ('3Ah', '6Ah') if any(r[1] == cl for r in rows)},
                   'units': {r[0]: {'sleep_mw': round(r[8], 1), 'sleep_ma': round(r[7], 2),
                                    'dmv': round(r[4], 1), 'cap_class': r[1]} for r in rows}},
                  open(a.out, 'w'), indent=1)
        print(f"\nwrote {a.out}")


if __name__ == '__main__':
    main()
