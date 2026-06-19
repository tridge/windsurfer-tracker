#!/usr/bin/env python3
"""Fit the per-unit 3-parameter battery model and emit gt06_calibration.json.

Model (OCV approximated linear in charge removed over the operating range):

    V_i(t) = B_i - k_i*Q(t) - R_i*I(t)

  * B_i  -> divider offset b_i = B_i - group_mean(B)   (mV; how high it reads)
  * k_i  -> discharge slope (V/Ah); capacity_i = relcap_i * nominal_mah,
            relcap_i = group_median(k)/k_i
  * R_i  -> internal resistance (ohm)

Q(t) is the common cumulative charge removed (identical fleet usage => same Wh),
I(t) the load current by state (plateau~0, idle low, tracking ~track_ma). Fitting
per unit by least squares separates the three effects; capacity no longer leaks
into R. More data (longer windows, more load steps) tightens the estimates.

Run where the position logs live (server, tracker user):
  python3 gt06_fit_calibration.py --logs 2026_06_18.jsonl.gz 2026_06_19.jsonl.gz \
      --plateau '2026-06-18 09:00' '2026-06-18 13:00' \
      --track   '2026-06-18 17:30' '2026-06-19 05:30' \
      --tz 10 --out gt06_calibration.json
"""
import argparse, gzip, json, statistics, time
from collections import defaultdict

# 3000mAh units; everything else is 6000mAh (see reference_gt06_battery_capacities).
THREE = {"G312243","G312268","G312292","G312342","G375349","G375356","G375372",
         "G375539","G375562","G378657","G226122"}
NOMINAL = {"3Ah": 3000, "6Ah": 6000}
# units whose full-charge reading is suspect (read low at full, track normally) --
# offset/charge-state confounded, so cap their correction and flag them.
UNCERTAIN = {"G375430", "G378517"}

def openf(p):
    return gzip.open(p, 'rt', errors='replace') if p.endswith('.gz') else open(p, errors='replace')

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--logs', nargs='+', required=True)
    ap.add_argument('--plateau', nargs=2, required=True, metavar=('START','END'))
    ap.add_argument('--track', nargs=2, required=True, metavar=('START','END'))
    ap.add_argument('--tz', type=float, default=10.0, help='log local tz offset hours')
    ap.add_argument('--track-ma', type=float, default=115.0)
    ap.add_argument('--idle-ma', type=float, default=25.0)
    ap.add_argument('--nominal-v', type=float, default=3.7)
    ap.add_argument('--reference-v', type=float, default=4.17)
    ap.add_argument('--generated', required=True, help='YYYY-MM-DD stamp for the file')
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    try:
        import numpy as np
    except ImportError:
        raise SystemExit("numpy required")

    tzoff = a.tz * 3600
    P0, P1 = _ep(a.plateau[0], tzoff), _ep(a.plateau[1], tzoff)
    T0, T1 = _ep(a.track[0], tzoff), _ep(a.track[1], tzoff)
    I_TRK, I_IDLE = a.track_ma/1000.0, a.idle_ma/1000.0
    BIN = 300

    def I_of(t):
        if P0 <= t < P1: return 0.0
        if T0 <= t < T1: return I_TRK
        return I_IDLE

    binv = defaultdict(lambda: defaultdict(list))
    for fn in a.logs:
        for line in openf(fn):
            try: d = json.loads(line)
            except Exception: continue
            bv, ts, g = d.get('bat_v'), d.get('ts'), d.get('id')
            if bv and ts and g: binv[int(ts//BIN)][g].append(bv)

    used = [b for b in binv if (P0 <= b*BIN < P1) or (T0 <= b*BIN < T1)]
    # cumulative charge Q(Ah) from plateau start
    qcache = {}
    for b in used:
        t, q, s = b*BIN, 0.0, P0
        while s < t:
            q += I_of(s) * (BIN/3600.0); s += BIN
        qcache[b] = q

    data = defaultdict(list)
    for b in used:
        I, q = I_of(b*BIN), qcache[b]
        for g, v in binv[b].items():
            if len(v) >= 3:
                data[g].append((q, I, statistics.median(v)))

    # A unit needs BOTH load levels to separate b (I=0 plateau) from R (I>0
    # tracking) and enough tracking spread to pin k; one-sided data is dropped so
    # it can't corrupt the group offset/capacity stats (it falls back to defaults
    # in the UI). k must be physically positive and not ill-conditioned, else the
    # capacity = kmed/k blows up.
    K_FLOOR = 0.02   # V/Ah; below this the linear fit is too flat to trust
    fit = {}
    for g, pts in data.items():
        n_plat = sum(1 for q, I, v in pts if I == 0.0)
        n_trk = sum(1 for q, I, v in pts if I > 0.0)
        # need the I=0 (plateau) anchor AND tracking spread to separate b/R/k.
        # One plateau bin is a ~5-min median over hundreds of samples -> enough.
        if n_plat < 1 or n_trk < 8:
            continue
        A = np.array([[1.0, -p[0], -p[1]] for p in pts]); y = np.array([p[2] for p in pts])
        (B, k, R), *_ = np.linalg.lstsq(A, y, rcond=None)
        if k <= K_FLOOR:
            print(f"  skip {g}: ill-conditioned k={k:.4f} V/Ah")
            continue
        R = max(0.0, float(R))   # clamp tiny-negative noise; never emit negative R
        rms = float(np.sqrt(np.mean((A @ [B, k, R] - y)**2)))
        fit[g] = dict(B=float(B), k=float(k), R=R, rms=rms)

    cap = lambda g: '3Ah' if g in THREE else '6Ah'
    units, offsets = {}, {}
    for c in ('3Ah', '6Ah'):
        gs = [g for g in fit if cap(g) == c]
        if not gs: continue
        Bbar = statistics.mean(fit[g]['B'] for g in gs)
        kmed = statistics.median(fit[g]['k'] for g in gs)
        for g in gs:
            f = fit[g]
            b_mv = round((f['B'] - Bbar) * 1000, 1)
            # Capacity is NOMINAL-ANCHORED RELATIVE: relcap = group_median(k)/k_i,
            # capacity = relcap * nominal. The fleet only shallow-discharged (stayed
            # in the flat 3.9-4.2V top of the curve), so ABSOLUTE capacity is not
            # well determined by this data -- charge-balance through the real curve
            # blows up there. relcap is the stable relative ordering; the absolute
            # mAh/Wh/runtime are estimates pending a deep-discharge run. Flag the
            # tail (relcap outside ~+-30%) as low-confidence.
            relcap = kmed / f['k'] if f['k'] else 1.0
            cap_mah = int(round(relcap * NOMINAL[c]))
            units[g] = {
                "offset_mv": b_mv,
                "resistance_ohm": round(f['R'], 3),
                "capacity_mah": cap_mah,
                "cap_class": c,
                "fit_rms_mv": round(f['rms']*1000, 1),
                "uncertain": (g in UNCERTAIN) or not (0.7 <= relcap <= 1.3),
            }
            # display correction = volts to ADD = -b; cap the uncertain units at 50mV.
            corr_mv = -b_mv
            if g in UNCERTAIN:
                corr_mv = max(-50.0, min(50.0, corr_mv))
            if abs(corr_mv) >= 5:
                offsets[g] = round(corr_mv/1000.0, 3)

    # defaults for an unseen unit: the 6Ah medians (median offset 0, plus group R/cap).
    sixu = [u for u in units.values() if u['cap_class'] == '6Ah']
    defaults = {
        "offset_mv": 0.0,
        "resistance_ohm": round(statistics.median(u['resistance_ohm'] for u in sixu), 3) if sixu else 0.55,
        "capacity_mah": NOMINAL['6Ah'],
        "cap_class": "6Ah",
    }
    doc = {
        "version": 2,
        "generated": a.generated,
        "reference_v": a.reference_v,
        "model": "V = OCV(Q) + b - I*R; per-unit offset_mv (b), resistance_ohm (R), capacity_mah",
        "track_current_ma": a.track_ma,
        "nominal_voltage": a.nominal_v,
        "note": ("offsets = volts to ADD to raw bat_v (= -offset_mv, uncertain capped at "
                 "+-50mV). units = full 3-param fit. Unseen units use defaults (6Ah medians)."),
        "defaults": defaults,
        "offsets": dict(sorted(offsets.items())),
        "units": dict(sorted(units.items())),
    }
    with open(a.out, 'w') as f:
        json.dump(doc, f, indent=1)
    print(f"wrote {a.out}: {len(units)} units, {len(offsets)} offsets")
    for c in ('6Ah', '3Ah'):
        us = [u for u in units.values() if u['cap_class'] == c]
        if not us:
            continue
        rs = sorted(u['resistance_ohm'] for u in us)
        cs = sorted(u['capacity_mah'] for u in us if u['capacity_mah'] is not None)
        capr = f"{cs[0]}..{cs[-1]} mAh" if cs else "n/a"
        print(f"  {c}: R {rs[0]:.2f}..{rs[-1]:.2f}  cap {capr}  (n={len(us)})")

def _ep(s, tzoff):
    import calendar
    return calendar.timegm(time.strptime(s, '%Y-%m-%d %H:%M')) - tzoff

if __name__ == '__main__':
    main()
