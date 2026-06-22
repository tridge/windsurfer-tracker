#!/usr/bin/env python3
"""Joint self-consistent fit of the OCV(SoC) curve AND per-unit internal resistance.

The curve and R are coupled: the curve fit needs R to turn loaded discharge voltage
into OCV, and the two-load R estimate needs the curve to match SoC between the idle
and tracking loads. This solves them together by iterating to a fixed point.

Inputs (all in gt06/battery_data/, see README.md):
  tracking.csv.gz   per-unit terminal V vs time, constant-power tracking (~115 mA)
  idle_voltages.csv  per-unit median idle voltage (~6 mA), cxzt 1 mV
  meta.json          unplug epoch, power_w (0.381), track_current basis

Identities (per cell, per-unit divider offset b, resistance R):
  discharge:  V_track(SoC) = OCV(SoC) - I_track*R + b ,  I_track = P/V_track   (const power)
  idle:       V_idle       = OCV(SoC_idle) - I_idle*R + b
  SoC in discharge is COULOMB-counted (charge integral of I=P/V), independent of voltage.

Iteration:
  1. curve   : OCV_ij = V_track_ij + I_track_ij*R_i - b_i ; fit SoC=f(OCV) (Roho, c1 fixed)
  2. offset  : b_i = median_j[ V_track_ij + I_track_ij*R_i - OCV_curve(SoC_ij) ] ; gauge median(b)=0
  3. resist  : SoC_idle = f(V_idle_i - b_i + I_idle*R_i) ; V_track@ = discharge interp at SoC_idle
               R_i = (V_idle_i - V_track@) / (I_track@ - I_idle)   (b cancels in the difference)
               damped: R_i <- (1-d)*R_i + d*R_new
  repeat until R and curve coeffs stop moving.

Caveats (for review): SoC_idle still rides on the curve, so R carries the curve's modeling
assumptions (constant-power, Roho form, divider gauge). The idle IR term (I_idle*R ~ 6 mV) is
the only R-dependence in SoC matching, so the residual circularity is weak and the damped loop
converges; absolute R accuracy is bounded by those assumptions, relative ordering is firm.

  python3 gt06_fit_joint.py --idle-ma 6 --soc-lo 10 --soc-hi 90 \
      --out-curve gt06/battery_data/soc_fit.json --out-r gt06/battery_data/resistance.json
"""
import argparse, csv, gzip, json, os, statistics
import numpy as np
from scipy.optimize import curve_fit

THREE = {"G312243", "G312268", "G312292", "G312342", "G375349", "G375356",
         "G375372", "G375539", "G375562", "G378657", "G226122"}


def soc_model(v, c1, c2, c3, c4):
    return c1 * (1.0 - 1.0 / (1.0 + (v / c2) ** c4) ** c3)


def soc_clamp(v, c):
    return max(0.0, min(100.0, soc_model(v, *c)))


def soc_inv(soc, c):
    lo, hi = 3.0, 4.25
    for _ in range(50):
        m = 0.5 * (lo + hi)
        if soc_model(m, *c) < soc:
            lo = m
        else:
            hi = m
    return 0.5 * (lo + hi)


def fit_curve(V, S, c1):
    f3 = lambda v, c2, c3, c4: soc_model(v, c1, c2, c3, c4)
    p3, _ = curve_fit(f3, V, S, p0=[3.65, 0.3, 36.0],
                      bounds=([3.0, 0.01, 5.0], [4.2, 1.0, 300.0]), maxfev=200000)
    return np.array([c1, *p3])


def interp_v(pts, soc):
    if soc <= pts[0][0]:
        return pts[0][1]
    if soc >= pts[-1][0]:
        return pts[-1][1]
    for i in range(1, len(pts)):
        if pts[i][0] >= soc:
            s0, v0 = pts[i - 1]; s1, v1 = pts[i]
            f = (soc - s0) / (s1 - s0) if s1 > s0 else 0
            return v0 + f * (v1 - v0)
    return pts[-1][1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', default='gt06/battery_data')
    ap.add_argument('--idle-ma', type=float, default=6.0)
    ap.add_argument('--soc-lo', type=float, default=10.0)
    ap.add_argument('--soc-hi', type=float, default=90.0)
    ap.add_argument('--cutoff-v', type=float, default=3.29)
    ap.add_argument('--firm-v', type=float, default=3.36)
    ap.add_argument('--max-gap', type=float, default=300.0)
    ap.add_argument('--fix-c1', type=float, default=111.56)
    ap.add_argument('--iters', type=int, default=40)
    ap.add_argument('--damp', type=float, default=0.5)
    ap.add_argument('--out-curve', default=None)
    ap.add_argument('--out-r', default=None)
    a = ap.parse_args()

    meta = json.load(open(os.path.join(a.data, 'meta.json')))
    P, unplug, I_idle = meta['power_w'], meta['unplug_epoch'], a.idle_ma / 1000.0

    # discharge: per-unit coulomb-counted (SoC, V_track), full range
    rows = {}
    with gzip.open(os.path.join(a.data, 'tracking.csv.gz'), 'rt') as f:
        for d in csv.DictReader(f):
            rows.setdefault(d['id'], []).append((int(d['ts']), float(d['bat_v']), int(d['chg'])))
    dis, firm = {}, []
    for g, recs in rows.items():
        recs.sort()
        off = [(t, v) for t, v, c in recs if not c and t >= unplug and v >= a.cutoff_v]
        if len(off) < 50 or min(v for _, v in off) > a.firm_v:
            continue
        Q, qs = 0.0, [0.0]
        for i in range(1, len(off)):
            dt = min(off[i][0] - off[i - 1][0], a.max_gap)
            Q += (P / (0.5 * (off[i][1] + off[i - 1][1]))) * (dt / 3600.0); qs.append(Q)
        Qtot = qs[-1]
        if Qtot <= 0:
            continue
        firm.append(g)
        dis[g] = sorted((100.0 * (Qtot - q) / Qtot, v) for (t, v), q in zip(off, qs))

    # idle voltages (prefer cxzt 1 mV)
    vidle = {}
    with open(os.path.join(a.data, 'idle_voltages.csv')) as f:
        for d in csv.DictReader(f):
            v = d['v_idle_cxzt'] or d['v_idle_status']
            if v:
                vidle[d['id']] = float(v)

    cls_of = lambda g: '3Ah' if g in THREE else '6Ah'
    R = {g: 0.55 for g in firm}            # init
    b = {g: 0.0 for g in firm}
    c = np.array([a.fix_c1, 3.6, 0.3, 36.0])
    print(f"joint fit: {len(firm)} firm units, {sum(g in vidle for g in firm)} with idle V, "
          f"idle={a.idle_ma:.0f}mA  SoC[{a.soc_lo:.0f},{a.soc_hi:.0f}]")
    print(f"{'it':>2} {'c2':>7} {'c3':>7} {'c4':>7} {'50%V':>6} {'Rmed3':>6} {'Rmed6':>6} {'maxdR':>7}")
    last50, curve_conv, r_conv = 0.0, False, False
    for it in range(a.iters):
        # 1. curve from discharge, OCV = Vtrack + Itrack*R - b
        V, S = [], []
        for g in firm:
            for soc, vt in dis[g]:
                if a.soc_lo <= soc <= a.soc_hi:
                    V.append(vt + (P / vt) * R[g] - b[g]); S.append(soc)
        c = fit_curve(np.array(V), np.array(S), a.fix_c1)
        # 2. per-unit offset b, gauge median 0
        nb = {}
        for g in firm:
            res = [vt + (P / vt) * R[g] - soc_inv(soc, c)
                   for soc, vt in dis[g] if a.soc_lo <= soc <= a.soc_hi]
            nb[g] = statistics.median(res)
        bm = statistics.median(nb.values())
        b = {g: nb[g] - bm for g in firm}
        # 3. two-load R update (damped)
        maxd = 0.0
        for g in firm:
            if g not in vidle:
                continue
            vi = vidle[g]
            ocv_idle = vi - b[g] + I_idle * R[g]
            soc_idle = soc_clamp(ocv_idle, c)
            vt = interp_v(dis[g], soc_idle)
            I_trk = P / vt
            rnew = (vi - vt) / (I_trk - I_idle)
            rd = (1 - a.damp) * R[g] + a.damp * rnew
            maxd = max(maxd, abs(rd - R[g])); R[g] = rd
        r3 = statistics.median(R[g] for g in firm if cls_of(g) == '3Ah' and g in vidle)
        r6 = [R[g] for g in firm if cls_of(g) == '6Ah' and g in vidle]
        r6m = statistics.median(r6) if r6 else float('nan')
        print(f"{it:>2} {c[1]:>7.4f} {c[2]:>7.4f} {c[3]:>7.3f} {soc_inv(50,c):>6.3f} "
              f"{r3:>6.3f} {r6m:>6.3f} {maxd*1000:>6.1f}m")
        curve_conv = abs(soc_inv(50, c) - last50) < 0.001 if it else False
        r_conv = maxd < 0.0005
        last50 = soc_inv(50, c)
        if r_conv and it > 3:
            break

    # final RMS + per-unit table
    V, S = [], []
    for g in firm:
        for soc, vt in dis[g]:
            if a.soc_lo <= soc <= a.soc_hi:
                V.append(vt + (P / vt) * R[g] - b[g]); S.append(soc)
    V, S = np.array(V), np.array(S)
    rms = float(np.sqrt(np.mean((S - soc_model(V, *c)) ** 2)))

    print(f"\nCURVE {'converged' if curve_conv else 'STABLE-ish'}  |  per-unit R "
          f"{'converged' if r_conv else 'DID NOT CONVERGE (non-identifiable, see METHOD.md s6)'}")
    print(f"  c1={c[0]:.2f} c2={c[1]:.5f} c3={c[2]:.5f} c4={c[3]:.4f}  RMS={rms:.2f}%SoC")
    print(f"  anchors: 20%={soc_inv(20,c):.3f}V 50%={soc_inv(50,c):.3f}V 80%={soc_inv(80,c):.3f}V")
    for cl in ('3Ah', '6Ah'):
        rs = [R[g] for g in firm if cls_of(g) == cl and g in vidle]
        if rs:
            print(f"  {cl} R: n={len(rs)} median={statistics.median(rs):.3f} "
                  f"mean={statistics.mean(rs):.3f} sd={statistics.pstdev(rs):.3f} "
                  f"range {min(rs):.3f}-{max(rs):.3f} ohm")

    print(f"\n{'unit':8} {'cls':4} {'R_ohm':>6} {'off_mV':>7} {'idleSoC':>8}")
    for g in sorted(firm):
        if g not in vidle:
            continue
        soc_i = soc_clamp(vidle[g] - b[g] + I_idle * R[g], c)
        print(f"{g:8} {cls_of(g):4} {R[g]:>6.3f} {b[g]*1000:>+6.1f}m {soc_i:>7.1f}%")

    if a.out_curve:
        json.dump({'form': 'SoC=c1*(1-1/(1+(V/c2)^c4)^c3), V=OCV/cell, clamp[0,100]',
                   'source': 'gt06/battery_data joint curve+R fit',
                   'coeffs': {'c1': round(float(c[0]), 4), 'c2': round(float(c[1]), 5),
                              'c3': round(float(c[2]), 5), 'c4': round(float(c[3]), 4)},
                   'fit_rms_pct_soc': round(rms, 3),
                   'anchors_v': {'20': round(soc_inv(20, c), 4), '50': round(soc_inv(50, c), 4),
                                 '80': round(soc_inv(80, c), 4)}},
                  open(a.out_curve, 'w'), indent=1)
        print(f"\nwrote {a.out_curve}")
    if a.out_r:
        # NOTE: per-unit R below is the RAW iteration output and is NOT identifiable
        # (oscillates, can go negative). The curated finding lives in
        # gt06/battery_data/resistance.json — do not overwrite it with this. Refuse if
        # the target is that curated file.
        if os.path.abspath(a.out_r) == os.path.abspath('gt06/battery_data/resistance.json'):
            raise SystemExit("refusing to overwrite the curated resistance.json with raw "
                             "non-identifiable per-unit R; pick a different --out-r path")
        json.dump({'method': 'RAW joint-iteration per-unit R — NOT IDENTIFIABLE, illustrative only; '
                             'see gt06/battery_data/resistance.json + METHOD.md s6',
                   'per_unit_identifiable': False, 'r_converged': bool(r_conv), 'idle_ma': a.idle_ma,
                   'class_median_ohm': {cl: round(statistics.median(
                       [R[g] for g in firm if cls_of(g) == cl and g in vidle]), 3)
                       for cl in ('3Ah', '6Ah')},
                   'units': {g: {'R_ohm': round(R[g], 4), 'offset_mv': round(b[g] * 1000, 1),
                                 'cap_class': cls_of(g), 'has_idle': g in vidle} for g in sorted(firm)}},
                  open(a.out_r, 'w'), indent=1)
        print(f"wrote {a.out_r}")


if __name__ == '__main__':
    main()
