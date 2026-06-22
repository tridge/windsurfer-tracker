#!/usr/bin/env python3
"""Fit a parameterised resting-voltage -> SoC curve for the GT06 Li-ion cells.

Operates on the checked-in dataset in gt06/battery_data/ (produced by
gt06_extract_battery_data.py), NOT the raw server logs.

Why: the old 100-point lookup (battery_calibration.md Step 4) is (a) built from a
SINGLE unit, (b) tracking-load terminal voltage not resting/OCV, and (c) rounded to
10 mV (STATUS#/log bat_v = round-to-nearest-10mV, confirmed against cxzt 1 mV) — so
in the flat 3.60-3.66 V band two SoC% land in one 10 mV step, doubling the
voltage->SoC gain and wrecking low-signal idle estimates.

This fits ONE smooth monotonic function to ALL firm (fully-discharged) units in
resting-voltage (OCV) space, using the Roho / ArduPilot BattEstimate form:

    SoC(V) = c1 * (1 - 1/(1 + (V/c2)^c4)^c3)        clamped [0,100]

(see AP_Scripting/applets/BattEstimate.lua).

Corrections (each unit's measured divider voltage -> true cell OCV):
  * IR-sag:   OCV = V_term + I_track*R_class   (load -> rest; collapses the 3/6 Ah gap)
  * divider:  per-unit affine  V_true = gain*V_meas + offset.  A resistor divider's
              tolerance is multiplicative (ratio R2/(R1+R2)), so the dominant error is
              GAIN (diverges at the voltage extremes); ADC offset adds a small additive
              term. Fitted jointly with the curve by iterative re-anchoring.

SoC is true charge-based: at constant power P, I=P/V, charge = integral(P/V dt);
SoC% = 100*(Qtot-Q)/Qtot (100 at unplug, 0 at cutoff). The unreliable ends (surface
charge just off the charger; protection-cutoff floor near death) are trimmed via
--soc-lo/--soc-hi.

  python3 gt06_fit_soc_curve.py --correction affine --soc-lo 10 --soc-hi 90
"""
import argparse, csv, gzip, json, os, statistics
import numpy as np
from scipy.optimize import curve_fit

THREE = {"G312243", "G312268", "G312292", "G312342", "G375349", "G375356",
         "G375372", "G375539", "G375562", "G378657", "G226122"}


def soc_model(v, c1, c2, c3, c4):
    return c1 * (1.0 - 1.0 / (1.0 + (v / c2) ** c4) ** c3)


def load_data(path):
    rows = {}
    with gzip.open(os.path.join(path, 'tracking.csv.gz'), 'rt') as f:
        for d in csv.DictReader(f):
            rows.setdefault(d['id'], []).append(
                (int(d['ts']), float(d['bat_v']), int(d['chg'])))
    meta = json.load(open(os.path.join(path, 'meta.json')))
    return rows, meta


def fit_curve(V, S, c1):
    """Fit soc_model to (V,S). c1>0 fixes c1 (breaks c1<->c3 degeneracy)."""
    if c1 and c1 > 0:
        f3 = lambda v, c2, c3, c4: soc_model(v, c1, c2, c3, c4)
        p3, _ = curve_fit(f3, V, S, p0=[3.65, 0.205, 80.0],
                          bounds=([3.0, 0.01, 5.0], [4.2, 1.0, 300.0]), maxfev=200000)
        return np.array([c1, *p3])
    p0 = [111.56, 3.65, 0.205, 80.0]
    popt, _ = curve_fit(soc_model, V, S, p0=p0,
                        bounds=([90, 3.0, 0.01, 5.0], [300, 4.2, 1.0, 300.0]), maxfev=200000)
    return popt


def soc_inv(soc, c):
    """Invert soc_model: OCV at a given SoC (bisection)."""
    lo, hi = 3.0, 4.25
    for _ in range(50):
        m = 0.5 * (lo + hi)
        if soc_model(m, *c) < soc:
            lo = m
        else:
            hi = m
    return 0.5 * (lo + hi)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', default='gt06/battery_data')
    ap.add_argument('--cal', default='WebUI/gt06_calibration.json')
    ap.add_argument('--ocv', choices=('per-unit-r', 'global-r', 'terminal'),
                    default='global-r', help='IR-sag reconstruction mode')
    ap.add_argument('--correction', choices=('none', 'offset', 'affine'),
                    default='affine', help='per-unit divider correction')
    ap.add_argument('--soc-lo', type=float, default=10.0, help='trim below this SoC%%')
    ap.add_argument('--soc-hi', type=float, default=90.0, help='trim above this SoC%%')
    ap.add_argument('--cutoff-v', type=float, default=3.30,
                    help='drop samples below this terminal V (protection floor)')
    ap.add_argument('--firm-v', type=float, default=3.36,
                    help='a unit is "firm" if it reached <= this V')
    ap.add_argument('--max-gap', type=float, default=300.0,
                    help='cap dt across log gaps (s) when integrating charge')
    ap.add_argument('--fix-c1', type=float, default=111.56,
                    help='fix c1; pass 0 to let it float')
    ap.add_argument('--iters', type=int, default=8)
    ap.add_argument('--r-json', default='gt06/battery_data/resistance.json',
                    help='class-median R for the global-r IR add-back (self-consistent '
                         'with the joint fit). Falls back to the old cal R if absent.')
    ap.add_argument('--out', default=None)
    a = ap.parse_args()

    rows, meta = load_data(a.data)
    cal = json.load(open(a.cal))
    units_cal = cal.get('units', {})
    I_trk = meta['track_current_ma'] / 1000.0
    P = meta['power_w']
    unplug = meta['unplug_epoch']
    # class-median R for the IR add-back: prefer the self-consistent joint-fit value
    # (resistance.json) over the old soft shallow-window cal R.
    rmed = None
    if a.r_json and os.path.exists(a.r_json):
        rj = json.load(open(a.r_json)).get('class_median_ohm')
        if rj:
            rmed = {c: float(rj[c]) for c in ('3Ah', '6Ah')}
    if rmed is None:
        rmed = {c: statistics.median(
            [units_cal[g]['resistance_ohm'] for g in units_cal
             if units_cal[g].get('cap_class') == c]) for c in ('3Ah', '6Ah')}

    def IR_of(g):
        if a.ocv == 'terminal':
            return 0.0
        if a.ocv == 'per-unit-r':
            r = units_cal.get(g, {}).get('resistance_ohm', rmed['3Ah' if g in THREE else '6Ah'])
        else:
            r = rmed['3Ah' if g in THREE else '6Ah']
        return r * I_trk

    # per-unit: (g, IR, [(soc, V_meas)]) trimmed to [soc_lo, soc_hi]
    per_unit, firm, live = [], [], []
    for g, recs in rows.items():
        recs.sort()
        off = [(t, v) for t, v, chg in recs if not chg and t >= unplug and v >= a.cutoff_v]
        if len(off) < 50:
            live.append(g); continue
        if min(v for _, v in off) > a.firm_v:
            live.append(g); continue
        firm.append(g)
        Q, qs = 0.0, [0.0]
        for i in range(1, len(off)):
            dt = min(off[i][0] - off[i - 1][0], a.max_gap)
            vmid = 0.5 * (off[i][1] + off[i - 1][1])
            Q += (P / vmid) * (dt / 3600.0); qs.append(Q)
        Qtot = qs[-1]
        if Qtot <= 0:
            continue
        binned = {}
        for (t, v), q in zip(off, qs):
            soc = 100.0 * (Qtot - q) / Qtot
            if a.soc_lo <= soc <= a.soc_hi:
                binned.setdefault(int(soc), []).append((soc, v))
        pts = [(statistics.median(s for s, _ in lst), statistics.median(v for _, v in lst))
               for lst in binned.values()]
        per_unit.append((g, IR_of(g), sorted(pts)))

    # iterative joint fit: per-unit affine (gain,offset) + shared curve
    gain = {g: 1.0 for g, _, _ in per_unit}
    off_mv = {g: 0.0 for g, _, _ in per_unit}
    c1 = a.fix_c1
    popt = None
    for it in range(a.iters):
        V, S = [], []
        for g, IR, pts in per_unit:
            for s, v in pts:
                V.append(gain[g] * v + off_mv[g] + IR); S.append(s)
        V = np.array(V); S = np.array(S)
        popt = fit_curve(V, S, c1)
        if a.correction == 'none':
            break
        # re-anchor each unit: target OCV* = soc_inv(soc); fit V_meas -> (OCV*-IR)
        ng, nb = {}, {}
        for g, IR, pts in per_unit:
            x = np.array([v for _, v in pts])
            y = np.array([soc_inv(s, popt) - IR for s, _ in pts])
            if a.correction == 'affine' and len(x) >= 5 and np.ptp(x) > 0.05:
                A = np.vstack([x, np.ones_like(x)]).T
                (gg, bb), *_ = np.linalg.lstsq(A, y, rcond=None)
            else:                                  # offset-only
                gg, bb = 1.0, float(np.mean(y - x))
            ng[g], nb[g] = gg, bb
        # gauge-fix: median gain -> 1, median offset -> 0 (common part folds into curve)
        gm = statistics.median(ng.values())
        for g in ng:
            ng[g] /= gm
        bmid = statistics.median(nb.values())
        for g in nb:
            nb[g] -= bmid
        gain, off_mv = ng, nb

    # final residuals
    V, S = [], []
    for g, IR, pts in per_unit:
        for s, v in pts:
            V.append(gain[g] * v + off_mv[g] + IR); S.append(s)
    V = np.array(V); S = np.array(S)
    resid = S - soc_model(V, *popt)
    rms = float(np.sqrt(np.mean(resid ** 2)))

    print(f"firm units (fit): {len(firm)}  |  live/partial: {len(live)}   "
          f"SoC window [{a.soc_lo:.0f},{a.soc_hi:.0f}]  ocv={a.ocv}  correction={a.correction}")
    print(f"SoC(V)=c1*(1-1/(1+(V/c2)^c4)^c3)   pooled pts={len(V)}")
    print(f"  c1={popt[0]:.4f}  c2={popt[1]:.5f}  c3={popt[2]:.5f}  c4={popt[3]:.4f}")
    print(f"  fit RMS = {rms:.2f} %SoC")

    if a.correction == 'affine':
        # report the physical correction at a 3.7V pivot, not the V=0 intercept
        V0 = 3.7
        corr37 = {g: (gain[g] - 1) * V0 + off_mv[g] for g in gain}  # volts to add at 3.7V
        gs = sorted(gain.values()); cs = sorted(corr37[g] * 1000 for g in corr37)
        print(f"\n  per-unit GAIN     : median 1.000  spread {gs[0]:.4f}..{gs[-1]:.4f}  "
              f"sd={statistics.pstdev(gs)*100:.2f}%")
        print(f"  correction @3.7V  : median {statistics.median(cs):+.1f}mV  "
              f"spread {cs[0]:.1f}..{cs[-1]:.1f} mV  sd={statistics.pstdev(cs):.1f}mV")
        worst = sorted(gain, key=lambda g: abs(gain[g] - 1), reverse=True)[:4]
        print("  largest-gain units: " +
              ", ".join(f"{g} g={gain[g]:.3f} @3.7V={corr37[g]*1000:+.0f}mV" for g in worst))

    print(f"\n  SoC band   n   resid(%SoC)  ~mV err")
    eps = 1e-3
    for lo in range(0, 100, 10):
        msk = (S >= lo) & (S < lo + 10)
        if not msk.any():
            continue
        vmid = float(np.median(V[msk]))
        slope = (soc_model(vmid + eps, *popt) - soc_model(vmid - eps, *popt)) / (2 * eps)
        rb = float(np.sqrt(np.mean(resid[msk] ** 2)))
        print(f"  {lo:3d}-{lo+10:<3d} {int(msk.sum()):4d}  {rb:8.2f}   "
              f"{rb/abs(slope)*1000 if slope else float('nan'):6.1f}")

    print(f"\n  key OCV anchors: 20%={soc_inv(20,popt):.3f}V  50%={soc_inv(50,popt):.3f}V  "
          f"80%={soc_inv(80,popt):.3f}V")
    print(f"  monotonic 3.30-4.06V: "
          f"{all(np.diff([soc_model(v,*popt) for v in np.arange(3.30,4.061,0.005)])>=0)}")

    if a.out:
        doc = {
            'form': 'SoC = c1*(1 - 1/(1+(V/c2)^c4)^c3), V=resting/OCV per cell, clamp[0,100]',
            'source': 'gt06/battery_data (full discharge 2026-06-18..21)',
            'ocv_mode': a.ocv, 'correction': a.correction,
            'soc_window': [a.soc_lo, a.soc_hi],
            'n_firm_units': len(firm), 'firm_units': sorted(firm),
            'coeffs': {'c1': round(float(popt[0]), 4), 'c2': round(float(popt[1]), 5),
                       'c3': round(float(popt[2]), 5), 'c4': round(float(popt[3]), 4)},
            'fit_rms_pct_soc': round(rms, 3),
            'anchors_v': {'20': round(soc_inv(20, popt), 4),
                          '50': round(soc_inv(50, popt), 4),
                          '80': round(soc_inv(80, popt), 4)},
            'class_r_ohm': rmed,
            # per-unit divider offset (volts to ADD to measured V before the curve),
            # gauge-fixed median 0. Same gauge the curve was fitted in — consumers
            # (gt06_idle_current.py) should use THESE, not the old display offsets.
            'offsets_mv': {g: round(off_mv[g] * 1000, 1) for g in sorted(off_mv)
                           if abs(off_mv[g] * 1000) >= 1},
        }
        json.dump(doc, open(a.out, 'w'), indent=1)
        print(f"\nwrote {a.out}")


if __name__ == '__main__':
    main()
