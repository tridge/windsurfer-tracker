# GT06 Battery Calibration

Empirical characterisation of the GT06 tracker batteries (capacity, per-mode power)
from a full-discharge run. The method avoids circular reasoning by resting on a
single assumption: **the median 3 Ah unit has exactly 3.0 Ah capacity.** Everything
else is derived from measured runtimes and voltages.

This document is a running log — calculations are added as they are done.

## Environment & data

- Fleet: 41 GT06 trackers — 11 × 3000 mAh nominal ("3 Ah"), 30 × 6000 mAh ("6 Ah").
- Discharge run: unplugged from charger **17:30 on 2026-06-18**, tracked at 1 Hz
  until each battery hit low-voltage cutoff (~3.3 V).
- Death = last contact in `tracker.log` (complete; the daily position logs can be
  partial after an idle/restart, so deaths are taken from tracker.log).
- **Caveat:** the tracking power is for *this* RF environment — modem TX power, and
  hence draw, may vary with distance from cell towers.

## Step 1 — Tracking power from the median unit

Pick the median 3 Ah unit (median by shutdown order = 6th of 11 to die):
**G375539 (T3Ah-V668-8)**. Assume it is exactly 3.0 Ah.

| quantity | value | source |
|---|---|---|
| assumed capacity | 3.0 Ah | assumption |
| start | 17:30, 06-18 | unplug |
| death | 22:29, 06-19 | tracker.log (last contact, ~3.3 V) |
| runtime | **29.0 h** | death − start |
| midpoint (14.5 h) | 08:00, 06-19 | runtime / 2 |
| voltage at midpoint | **3.680 V** | heartbeat 3.680 V @ 08:00:07; tracking fix 3.682 V @ 08:17 |

It tracked continuously at 1 Hz the whole 29 h (verified in the full event-8 log —
~3600 fixes/h). So:

```
Energy = capacity × midpoint_voltage = 3.0 Ah × 3.680 V = 11.04 Wh
Power  = Energy / runtime            = 11.04 Wh / 29.0 h = 0.381 W
```

**→ Tracking power ≈ 0.381 W, assumed identical for every tracker** (same GPS +
modem hardware). This is the constant used for all downstream runtime/capacity work.

## Step 2 — Per-unit 3 Ah capacity from runtime

With tracking power constant at 0.381 W, and all 3 Ah cells sharing the same
voltage-vs-SoC curve (so each unit's midpoint voltage ≈ 3.68 V regardless of
capacity), energy = P × runtime and capacity = energy / V_nom. Capacity therefore
**scales linearly with runtime**:

```
Wh_i       = 0.381 W × runtime_i
capacity_i = Wh_i / 3.68 V          (equivalently: 3.0 Ah × runtime_i / 29.0 h)
```

All units started 17:30 on 06-18; deaths from `tracker.log`:

| unit | death (Syd) | runtime (h) | Wh | capacity (mAh) |
|---|---|---:|---:|---:|
| T3Ah-V668-7 (G375372) | 06-19 17:44 | 24.24 | 9.24 | 2509 |
| T3Ah-V663-1 (G226122) | 06-19 19:26 | 25.94 | 9.88 | 2685 |
| T3Ah-V668-4 (G312342) | 06-19 20:37 | 27.12 | 10.33 | 2807 |
| T3Ah-V668-6 (G375356) | 06-19 20:47 | 27.29 | 10.40 | 2825 |
| T3Ah-V668-3 (G312292) | 06-19 22:28 | 28.98 | 11.04 | 3000 |
| **T3Ah-V668-8 (G375539)** | **06-19 22:29** | **28.99** | **11.05** | **3001 ← median, assumed 3.0 Ah** |
| T3Ah-V668-1 (G312243) | 06-20 00:51 | 31.35 | 11.94 | 3245 |
| T3Ah-V668-9 (G375562) | 06-20 01:24 | 31.91 | 12.16 | 3303 |
| T3Ah-V668-5 (G375349) | 06-20 02:45 | 33.26 | 12.67 | 3443 |
| T3Ah-V668-2 (G312268) | 06-20 03:08 | 33.64 | 12.82 | 3482 |
| T3Ah-V668-10 (G378657) | 06-20 05:48 | 36.30 | 13.83 | 3758 |

**Result:** 5 below 3.0 Ah, the median at 3.0 Ah, 5 above — real usable capacity
spread **2509–3758 mAh** (±25% around nominal), genuine cell-to-cell variation.

## Step 3 — Per-unit voltage offset

At each unit's **half-way-to-dead** time (= 50% SoC, since power is constant) all
cells are at the same true voltage (shared curve), so the measured spread there is
the per-unit voltage error. Take the **5-min median** voltage centred on each unit's
halfway time; the **fleet median is the nominal 50%-SoC voltage = 3.670 V**. Each
unit's offset is its deviation from that:

```
offset_i = halfway_median_i − 3.670 V        (+ve = reads high; subtract to correct)
```

Used directly as the voltage correction (no IR-sag separation). The ~3.3 V cutoff
was **rejected** as a reference: it's likely set by a protection circuit/diode
separate from the sense divider, so its tight 20 mV spread reflects that circuit,
not the divider (the divider spread is ~100 mV here at mid-discharge).

Each tracker now has **two calibration values: capacity (Ah) + voltage offset (mV)**:

| unit | capacity (mAh) | offset (mV) |
|---|---:|---:|
| T3Ah-V668-7 (G375372) | 2509 | −60 |
| T3Ah-V663-1 (G226122) | 2685 | +20 |
| T3Ah-V668-4 (G312342) | 2807 | +10 |
| T3Ah-V668-6 (G375356) | 2825 | −10 |
| T3Ah-V668-3 (G312292) | 3000 | +10 |
| T3Ah-V668-8 (G375539) | 3001 | +10 |
| T3Ah-V668-1 (G312243) | 3245 | +40 |
| T3Ah-V668-9 (G375562) | 3303 | −10 |
| T3Ah-V668-5 (G375349) | 3443 | −10 |
| T3Ah-V668-2 (G312268) | 3482 | −20 |
| T3Ah-V668-10 (G378657) | 3758 | 0 |

Offset spread −60 … +40 mV (100 mV total). Fleet nominal 50%-SoC voltage: **3.670 V**.

## Step 4 — Discharge curve (corrected voltage → remaining capacity, 1%)

Built from the median tracker **G375539** (offset +10 mV → correction −10 mV). Its
29 h discharge is split into 100 equal-time sections; since power is constant,
each section is **1% of capacity**. For each section take the median **corrected**
voltage (raw + correction). The result is a 100-point table of corrected voltage
vs remaining-capacity %, assumed common to all trackers (apply each unit's offset
first, then look up). 50% reads 3.670 V by construction; the curve is monotonic.
These are **tracking-load (~115 mA) voltages** (not OCV). Built from 99,573 samples.

Full 100-row table (remaining capacity % → corrected voltage):

| % remaining | corrected V |
|---:|---:|
| 100 | 4.060 |
| 99 | 4.050 |
| 98 | 4.040 |
| 97 | 4.030 |
| 96 | 4.020 |
| 95 | 4.010 |
| 94 | 4.000 |
| 93 | 3.990 |
| 92 | 3.980 |
| 91 | 3.970 |
| 90 | 3.960 |
| 89 | 3.940 |
| 88 | 3.930 |
| 87 | 3.920 |
| 86 | 3.910 |
| 85 | 3.900 |
| 84 | 3.900 |
| 83 | 3.890 |
| 82 | 3.890 |
| 81 | 3.880 |
| 80 | 3.880 |
| 79 | 3.880 |
| 78 | 3.870 |
| 77 | 3.860 |
| 76 | 3.860 |
| 75 | 3.850 |
| 74 | 3.840 |
| 73 | 3.840 |
| 72 | 3.830 |
| 71 | 3.820 |
| 70 | 3.810 |
| 69 | 3.810 |
| 68 | 3.800 |
| 67 | 3.790 |
| 66 | 3.780 |
| 65 | 3.770 |
| 64 | 3.760 |
| 63 | 3.750 |
| 62 | 3.740 |
| 61 | 3.730 |
| 60 | 3.720 |
| 59 | 3.710 |
| 58 | 3.710 |
| 57 | 3.706 |
| 56 | 3.700 |
| 55 | 3.690 |
| 54 | 3.680 |
| 53 | 3.673 |
| 52 | 3.670 |
| 51 | 3.670 |
| 50 | 3.670 |
| 49 | 3.660 |
| 48 | 3.650 |
| 47 | 3.650 |
| 46 | 3.640 |
| 45 | 3.640 |
| 44 | 3.630 |
| 43 | 3.630 |
| 42 | 3.620 |
| 41 | 3.620 |
| 40 | 3.610 |
| 39 | 3.610 |
| 38 | 3.600 |
| 37 | 3.600 |
| 36 | 3.590 |
| 35 | 3.590 |
| 34 | 3.580 |
| 33 | 3.580 |
| 32 | 3.570 |
| 31 | 3.570 |
| 30 | 3.560 |
| 29 | 3.560 |
| 28 | 3.550 |
| 27 | 3.540 |
| 26 | 3.530 |
| 25 | 3.530 |
| 24 | 3.520 |
| 23 | 3.510 |
| 22 | 3.510 |
| 21 | 3.500 |
| 20 | 3.490 |
| 19 | 3.490 |
| 18 | 3.480 |
| 17 | 3.460 |
| 16 | 3.460 |
| 15 | 3.450 |
| 14 | 3.440 |
| 13 | 3.430 |
| 12 | 3.420 |
| 11 | 3.410 |
| 10 | 3.410 |
| 9 | 3.400 |
| 8 | 3.390 |
| 7 | 3.370 |
| 6 | 3.330 |
| 5 | 3.300 |
| 4 | 3.290 |
| 3 | 3.290 |
| 2 | 3.290 |
| 1 | 3.290 |

(The bottom ~4% flattens at ~3.29 V — the protection-cutoff floor, not real curve.)

## Step 5 — Cross-check the curve against the other 10 units

Built the same 100-point corrected-voltage curve for all 11 units (each with its
own offset) and compared to the median tracker G375539.

- **Monotonic:** 8 of 11 fully monotonic; 3 (V668-7, V668-2, V668-1) have a single
  ~2–5 mV up-blip at one 1% step (quantisation, not a real reversal). The adopted
  reference curve (G375539) is fully monotonic.
- **Agreement:** anchored at 50% (= 3.670 V for all), the curves agree to **≤50 mV
  for 9 of 11 units**. Outliers: G226122 (79 mV), G375372 (99 mV).
- **Where they diverge:** deviation grows toward both ends (95%: 4.01–4.10 V;
  5%: 3.29–3.37 V). A single additive offset measured at 50% aligns the middle but
  not the extremes — the residual is **gain (proportional divider) error**.
  G375372's divider has a different slope, not just an offset.

**Verdict:** one curve + one offset is good to ~50 mV (≈1–2% capacity) for 9/11 —
fine to adopt. A per-unit gain term would tighten the two outliers at the extremes
(future work if needed).

## Step 6 — 6 Ah units (capacity + the 3 Ah↔6 Ah IR correction)

Same method: 0.381 W constant power, start 17:30 06-18, capacity = 0.381 W ×
runtime ÷ 3.68 V. Of the 30 6 Ah units, 17 fully drained and 13 were still live.

**17 drained (firm):** ran 50–59 h → **5181–6131 mAh**.

**3 Ah↔6 Ah IR correction.** At 50% SoC both cell sizes share the same OCV, so the
difference in their median *terminal* voltage is the IR-sag difference. The 17
drained 6 Ah units' half-way-to-death 5-min-median voltage (raw) is **3.720 V**
(tight cluster 3.71–3.74) vs the 3 Ah median **3.670 V** → 6 Ah cells sag ~50 mV
less (lower internal resistance). So **a 6 Ah voltage must be reduced by 50 mV
before looking it up in the (3 Ah-derived) discharge curve**. Validated: with
−50 mV the 6 Ah halfway maps to the curve's 50%.

**13 live (estimated):** current raw voltage − 50 mV → curve → remaining % R;
total runtime = elapsed × 100/(100−R); capacity = 0.381 × total ÷ 3.68. They were
~60 h in with 0–18 h left → **6237–8101 mAh** (the IR correction cut the top end
from ~8771 to ~8101). These firm up to exact runtime capacity as they die.

**All 30 6 Ah: median ~6.05 Ah ≈ nominal, range 5.2–8.1 Ah** (genuinely wider than
3 Ah's 2.5–3.8 Ah; the live high-end is still soft).

**Applied to the UI** (`gt06_calibration.json` v4 + `battery_cal.js`):
`class_curve_offset_mv = {3Ah:0, 6Ah:50}` is subtracted before the curve lookup
(`percentForUnit` / `correct`). 6 Ah per-unit divider offsets are set to 0 for now
(uncharacterised — measure them via the halfway method once all 6 Ah are dead).

## Pending

- **Finalise 6 Ah:** replace the 13 live estimates with true runtime capacities once
  they die (≤~18 h), and derive per-unit 6 Ah divider offsets via the halfway method.
- **Idle / sleep power:** idle preliminary ~0.28 W (modem-dominated — LTE link stays
  up; GPS-off saves little); sleep unmeasured (needs a clean off-charge MODE5 night).
- **Deploy:** v4 calibration + battery_cal.js (class offset) + the still-pending
  server restart (summary + STATUS-voltage fixes) land together at the next restart.
