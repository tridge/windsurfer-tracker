# GT06 battery characterisation — method, raw data → estimates

End-to-end documentation of how we go from raw logs to the battery estimates
(resting-voltage→SoC curve, per-unit internal resistance, idle power draw), written
for review. Supersedes the curve-build sections of `../battery_calibration.md` (which
documents the older single-unit 10 mV lookup table this work replaces).

Everything here is reproducible from the checked-in dataset in this directory plus the
scripts in `scripts/`. Each estimate is stated with its identifiability caveats.

---

## 1. Raw data

Two independent experiments on the same 41-unit fleet (event 8, Australia/Sydney):

| run | what | load | logs |
|-----|------|------|------|
| **discharge** | unplugged 17:30 2026-06-18, tracked ~1 Hz at constant power until each cell hit cutoff (~3.3 V), over ~3 days | tracking ~115 mA (const power 0.381 W) | event-8 daily `2026_06_18..21.jsonl.gz` |
| **idle** | overnight parked-idle test 2026-06-21, GPS off, 30-min uploads | idle ~6 mA | GT06 binary packet log (cxzt 1 mV + STATUS 10 mV) |

The discharge run is the OCV-curve + capacity source; the idle run is the low-load
anchor for resistance and the target for the idle-power estimate.

### 1a. Constant-power assumption
The tracker draws a fixed device power (GPS+modem), not fixed current. Measured: the
median 3 Ah unit ran 29.0 h on an assumed-exactly-3.0 Ah cell → **P = 3.0 Ah × 3.680 V /
29.0 h = 0.381 W** (`../battery_calibration.md` Step 1). So load current `I = P/V` rises
from ~95 mA at 4.0 V to ~115 mA at 3.3 V across the discharge.

---

## 2. Checked-in datasets (this directory)

| file | producer | contents |
|------|----------|----------|
| `discharge.csv.gz` | `gt06_extract_battery_data.py` | `id,ts,bat_v,chg,spd,nsats,n` — 60 s-binned terminal voltage over the discharge run, 41 units, ~119 k rows |
| `idle_voltages.csv` | inline extractor (see README) | per-unit median idle voltage (`v_idle_cxzt` 1 mV, `v_idle_status` 10 mV) |
| `meta.json` | extractor | unplug epoch, `power_w`=0.381, `track_current_ma`=115, bin size |
| `soc_fit.json` | `gt06_fit_soc_curve.py` | **production OCV→SoC curve coefficients** |
| `resistance.json` | `gt06_fit_joint.py` + manual | R result + the non-identifiability finding |

`bat_v` resolution: **`round(cxzt, nearest 10 mV)`** — proven, see §3.

---

## 3. Measurement quantisation (STATUS rounding)

STATUS# reports `Battery:X.XXV` (2 dp); cxzt reports `*BT:<mV>` (1 mV). Pairing **5050
near-simultaneous readings** across 54 units from the idle run: `status_mV − cxzt_mV` is
**uniform over −5…+5 mV**, and "round to nearest 10 mV" fits to **1.65 mV** mean error
(residual = the ≤90 s pairing-time drift). Floor/truncate-to-10 fits to 5.3 mV (rejected).

⇒ the log `bat_v` carries **±5 mV uniform quantisation** (sd ≈ 2.9 mV) per sample,
averaged down by pooling (33 units × ~80 SoC bins) and by preferring cxzt where available.

---

## 4. SoC definition (charge-based)

In the **discharge** run, SoC is coulomb-counted, independent of voltage:
`Q(t) = ∫ I dt = ∫ (P/V) dt`, `SoC%(t) = 100·(Qtot − Q(t))/Qtot` (100 at unplug, 0 at
cutoff). This is true Ah-SoC, not the equal-time/Wh approximation the old lookup used.
100% is defined as full-at-unplug (operational, matches the old curve).

---

## 5. OCV→SoC curve

### 5a. Form
Single-cell Roho / ArduPilot BattEstimate model (`AP_Scripting/applets/BattEstimate.lua`):

    SoC(V) = c1·(1 − 1/(1 + (V/c2)^c4)^c3),  clamped [0,100],  V = resting/OCV

Chosen over alternatives: monotonic, smooth, V→SoC **directly** (Plett's Combined model is
SoC→V, needs inversion; polynomials misbehave at the ends). **c1 is fixed at 111.56** —
free c1 rails at 200 (c1↔c3 degeneracy; large c1 × tiny c3).

### 5b. Why OCV not terminal
The idle units sit at ~6 mA (near rest), so the curve must be in **resting/OCV** space.
The old lookup was 115 mA-load terminal voltage, biasing every idle reading by ~I·R. We
reconstruct OCV from the discharge: `OCV = V_track + I_track·R` (`I_track = P/V_track`).
In OCV space the 3 Ah/6 Ah IR-sag difference collapses, so **one curve serves both
classes** and the old `class_curve_offset_mv=50` hack is unnecessary.

### 5c. Corrections (measured divider voltage → cell OCV)
- **IR-sag** (`--ocv global-r`): add `I_track·R_class` (class-median R). Per-unit R is NOT
  used here — see §6 (it is noisy and would inject scatter); the curve depends on R only
  through this small add-back and is insensitive to it.
- **Divider offset** (`--correction offset`): per-unit additive, gauge-fixed median 0.
- **Divider gain** (`--correction affine`, diagnostic only): a resistor divider's tolerance
  is multiplicative (ratio R2/(R1+R2)) → a GAIN error that diverges at the voltage extremes.
  Confirmed real and per-unit-consistent (same units rank highest in every SoC window) but
  it improves RMS only 0.07% inside the operating window (gain's effect is at the extremes,
  which §5d trims away), and over one unit's ~0.45 V span gain↔offset are degenerate
  (intercept blows to ±600 mV; at a 3.7 V pivot the correction is a sane ±25 mV). ⇒ production
  uses offset-only.

### 5d. End trim
`--soc-lo 10 --soc-hi 90` drops the surface-charge top (just off the charger) and the
protection-cutoff floor (near death). **This alone cut RMS 4.3 → 2.4 %SoC** — the single
biggest quality lever.

### 5e. Production curve (`gt06_fit_soc_curve.py --correction offset --ocv global-r --soc-lo 10 --soc-hi 90`)

    c1 = 111.56 (fixed)   c2 = 3.59030   c3 = 0.31882   c4 = 36.2470
    50% OCV = 3.762 V   20% = 3.575 V   80% = 4.003 V   monotonic ✓
    fit RMS = 2.36 %SoC   (33 firm units = 11×3Ah + 22×6Ah)

The IR add-back uses the self-consistent class-median R from `resistance.json`
(3 Ah 0.40 Ω, 6 Ah 0.52 Ω), NOT the old soft cal R — `soc_fit.json` records `class_r_ohm`
and the per-unit `offsets_mv` (the gauge the curve was fitted in). Consumers
(`gt06_idle_current.py`) use those exported offsets so the SoC lookup matches the fit.

Mid-band fit error 9–17 mV; the smooth sensitivity replaces the old table's erratic
10 mV-quantized gain (which made two identically-behaving idle units look 3× apart — see
`project_idle_power_measurement` and `project_soc_curve_parametric_fit` in memory).

---

## 6. Per-unit internal resistance R — ATTEMPTED, NOT IDENTIFIABLE

### 6a. Method (two-load, joint with the curve)
At matched SoC, with two loads:

    V_idle  = OCV(SoC) − I_idle ·R + b        (idle  ~6 mA)
    V_track = OCV(SoC) − I_track·R + b        (track ~115 mA)
    ⇒ R = (V_idle − V_track) / (I_track − I_idle)        (divider offset b cancels)

SoC is matched via the curve: `SoC_idle = soc_model(V_idle − b + I_idle·R)`, then `V_track`
is the discharge curve interpolated at `SoC_idle`. Because curve and R are coupled (curve
needs R for OCV; R needs the curve for SoC-matching), `gt06_fit_joint.py` iterates them to a
fixed point (damped).

### 6b. Result: curve converges, R does not
The **curve converges and is stable** (50% = 3.752 V, RMS 2.50%, identical for damping
0.5/0.2/0.15). The **per-unit R does NOT**: it oscillates and lands on unphysical values
**including negative R** (3 Ah range −0.3…+1.9 Ω; 6 Ah −1.3…+1.0 Ω). Negative R means a
unit's idle voltage came out *below* its tracking voltage at the matched SoC — impossible —
i.e. the SoC match is off by more than the IR signal.

### 6c. Why (identifiability)
The idle and discharge runs are **separate** (recharge + ~3 days + possible temperature
delta). The IR signal is ~`(115−6)mA × R ≈ 55 mV` at R=0.5 Ω. The SoC-matching error is
~curve RMS (2.5%) + per-unit offset uncertainty, and `2.5% × dV/dSoC(≈6.7 mV/%) ≈ 17 mV`
maps to **±0.15–0.3 Ω** of R — comparable to the signal. So per-unit R is swamped. Only the
**class-median** R survives (3 Ah ≈ 0.40 Ω, 6 Ah ≈ 0.52 Ω; soft, ~±0.15 Ω — same order as
the old shallow-window R in `gt06_calibration.json`).

### 6d. To actually get per-unit R
Need a **same-run two-load** measurement: on a parked unit, toggle GPS/tracking on→off
within minutes and read cxzt at both loads (same SoC, same temperature). That removes the
cross-run matching error. Until then, use the class-median.

---

## 7. Idle power draw (using the §5 curve)

`gt06_idle_current.py --soc-fit soc_fit.json --cxzt-only` over the 10 h idle window:
SoC = `soc_model(bat_v)` (idle ≈ rest), slope of SoC vs time × capacity → mA → W.

**3 Ah group: mean 6.0 mA / 22 mW, median 6.5 mA / 24 mW, sd 1.8 mA** (using the curve's own
exported offset gauge). Excluding the two units that began the idle test near-empty (~8% SoC,
on the curve's insensitive bottom knee): **mean ~6.8 mA / 25 mW**. Far below the old degraded-window 0.28 W figure; idle is
modem-dominated, the practical limit is the 30-min upload cadence not standby. Per-unit
values still ride the curve position (start-SoC column in the tool shows where each sat);
at 13 mV over 10 h we are near the cxzt resolution floor, so trust the group, not the tail.
(The quoted `sd` is unit-to-unit scatter — quantisation + curve position + cell variation —
NOT a confidence interval on the fleet-mean idle power.)

## 7b. Sleep power (two low-noise idle endpoints)

`scripts/gt06_sleep_current.py` on the `sleep` dataset. The cxzt reads taken DURING MODE5
sleep are noisy (each hourly wake polls cxzt# under a load transient — non-monotonic, ~20 mV
scatter; a slope through them is unusable, confirmed by the built-in `wake-slope` validation
which spans −9…+38 mA). Instead bracket the sleep with two **parked-idle** endpoints (idle
cxzt is stable to ~1 mV): the last idle read before 02:00 and the first settled idle read
after the units return to race-idle (~08:15, verified stable: 08:03 reads = 08:17 reads).
`ΔSoC = soc(V_pre) − soc(V_post)` via the §5 curve, `× capacity → mAh`, minus the idle-tail
for the post-settle window, `/ sleep_hours`.

**Result (6 h MODE5 sleep, 60-min GPS-reporting wakes, 41 units, 2026-06-22):**
fleet-median **≈ 15 mA / ~54 mW** — and this is **robust across SoC band** (low/mid/high band
medians 12.7 / 16.0 / 13.6 mA), so the aggregate is trustworthy even though per-unit is not.

**KEY FINDING:** sleep (~54 mW) **EXCEEDS clean parked idle (~22 mW)**. MODE5 with hourly
GPS-reporting wakes does **not** save power vs idle — the per-wake GPS-lock-seek (the wakes
re-enable GPS to report a location; some seek a lock for minutes, [[project_sleep_wake_turns_gps_on]])
dominates and outweighs the modem-off saving between wakes. The lever for real overnight saving
is a **longer wake interval** (fewer wakes) or **sleep-without-location-report** (GPS stays
off at wakes), NOT MODE4-vs-5.

**Caveats:** per-unit sleep is **unreliable** — within a single SoC band units range −7…+71 mA.
Over 6 h the true sleep ΔV is only a few mV (near the cxzt floor), and per-unit it is swamped by
(a) per-wake GPS-seek-time variation, (b) overnight **temperature** drift on OCV (pre 02:00 vs
post 08:15), (c) cell-to-cell self-discharge. Negative per-unit values are floor noise. Only the
fleet median is meaningful, and even that is preliminary/order-of-magnitude — a **longer sleep
run** (bigger ΔV vs the floor) or device-side measurement is needed for a firm number. The
3 Ah per-class median (~22 mA) reads higher than 6 Ah (~14 mA) but is inflated by low-SoC 3 Ah
units on the curve knee — don't split by class here.

---

## 8. Open questions / caveats for review

1. **Constant-power P = 0.381 W** rests on one assumed-exactly-3.0 Ah cell. All capacities
   and the SoC time-base scale with it. Reasonable? Better anchor?
2. **OCV absolute level** depends on the class-median R add-back (~0.5 Ω). If true R were
   1 Ω the 50% anchor shifts ~+50 mV. The §6 attempt to pin R failed, so the absolute OCV
   has ~±25 mV uncertainty; the *shape* (and thus relative SoC) is firm.
3. **Cross-run temperature** between discharge (06-18..21) and idle (06-21 night) is not
   controlled — a prime suspect for the R non-identifiability and a possible OCV bias.
4. **Divider gain** is real but un-modelled in production (offset-only). Is leaving it to
   the trim acceptable, or should we carry a per-unit gain for accuracy at the extremes
   (charge/near-empty)?
5. **Charge-based SoC** assumes continuous 1 Hz tracking at constant power the whole run;
   gaps are capped at `--max-gap`. Material?
6. **`firm-v`/`cutoff-v` thresholds** (3.36 / 3.29) decide which units anchor the fit (33
   firm). Sensitivity not swept.

---

## 9. Review trail

Codex reviewed this pipeline (2026-06-22). Core math validated (charge-SoC integration,
c1/c3 gauge, trim, STATUS rounding). Four consistency issues raised and fixed:
- curve IR add-back now uses `resistance.json` class-median R (0.40/0.52), not the old soft
  cal R → 50% anchor 3.768 → 3.762 V;
- the fit now exports per-unit `offsets_mv`, and `gt06_idle_current.py` uses them, so the idle
  estimate shares the curve's voltage gauge (was mixing the old 10-entry display offsets);
- the standalone `gt06_fit_resistance.py` now removes the divider offset before the SoC match
  (it was using raw V), and is marked superseded by the joint fit;
- `gt06_fit_joint.py` no longer prints "CONVERGED" unconditionally and refuses to overwrite
  the curated `resistance.json` with raw non-identifiable per-unit R.

## 10. Reproduce

    # 1. dataset from raw logs (run where the event-8 logs live)
    python3 scripts/gt06_extract_battery_data.py \
        --logs 2026_06_18.jsonl.gz 2026_06_19.jsonl.gz 2026_06_20.jsonl.gz 2026_06_21.jsonl.gz \
        --unplug '2026-06-18 17:30' --tz 10 --power-w 0.381 --track-ma 115 \
        --out-dir gt06/battery_data
    # idle_voltages.csv: extracted from the idle binary log (see README.md)

    # 2. production OCV->SoC curve
    python3 scripts/gt06_fit_soc_curve.py --correction offset --ocv global-r \
        --soc-lo 10 --soc-hi 90 --out gt06/battery_data/soc_fit.json
    #   diagnostics: --correction affine (gain), --anchor50 (intrinsic shape RMS)

    # 3. joint curve+R (shows R non-identifiable; curve stable). It REFUSES to write
    #    the curated resistance.json; raw per-unit R (illustrative) goes elsewhere:
    python3 scripts/gt06_fit_joint.py --out-r /tmp/resistance_raw.json

    # 4. resistance (two-load standalone, needs the idle binary log)
    python3 scripts/gt06_fit_resistance.py --idle-log <gt06.log> \
        --idle-start 1782020506 --idle-end 1782057600

    # 5. idle power on the new curve
    python3 scripts/gt06_idle_current.py --log <gt06.log> --cal WebUI/gt06_calibration.json \
        --soc-fit gt06/battery_data/soc_fit.json --cxzt-only \
        --start 1782020506 --end 1782057600
