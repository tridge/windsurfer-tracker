# GT06 battery datasets

Curated field-measurement datasets for the GT06 Li-ion battery characterisation —
**one per power mode** (tracking / idle / sleep). Decoupled from the raw server logs
(which rotate/expire) so every estimate is reproducible from the repo. These are
expensive one-off experiments (a full fleet discharge, an overnight idle run, an
overnight sleep run); keep them here for re-analysis.

**Full method write-up (raw data → all estimates, with caveats): see `METHOD.md`.**

## The three datasets

| dataset | file | mode / load | source | window |
|---------|------|-------------|--------|--------|
| **tracking** | `tracking.csv.gz` | ~115 mA const-power discharge | position logs (`bat_v`, 60 s bins) | 2026-06-18 17:30 → death |
| **idle** | `idle.csv.gz` | parked race-idle ~6 mA | GT06 binary log (cxzt 1 mV + STATUS 10 mV) | 2026-06-21 15:41 → 02:00 |
| **sleep** | `sleep.csv.gz` | MODE5 overnight + bracketing idle | GT06 binary log (cxzt) | 2026-06-22 ~01:45 → ~08:35 |

- `tracking.csv.gz` — `id,ts,bat_v,chg,spd,nsats,n`. 41 units, ~119 k rows.
- `idle.csv.gz` — `id,ts,source(cxzt|status),v_mv`. Low-noise idle trajectory (~1 mV/h).
- `sleep.csv.gz` — `id,ts,source,v_mv,phase(pre_idle|sleep_wake|post_idle)`.

## Derived / output files

- `meta.json` — dataset manifest (windows, provenance) + the keys the fit scripts read.
- `idle_voltages.csv` — per-unit median idle voltage, **derived from `idle.csv.gz`**; the
  low-load anchor for the two-load resistance estimate.
- `soc_fit.json` — fitted resting-V→SoC curve coefficients (+ class R, per-unit offsets).
- `resistance.json` — per-unit R attempt + the finding it's not identifiable cross-run
  (only class-median is meaningful). See `METHOD.md` §6.
- `sleep_power.json` — two-endpoint sleep-power result (per-class mW).
- `METHOD.md` — full pipeline documentation (raw data → curve, resistance, idle/sleep power).

## Provenance

Unplugged from charge **17:30 2026-06-18** (Australia/Sydney), event 8, tracked ~1 Hz
at constant power until each cell hit low-voltage cutoff (~3.3 V). Built from the event-8
daily position logs `2026_06_18..21.jsonl.gz`.

Regenerate the datasets from the raw logs (run where the logs live, or after copying them):

    # tracking (from the daily position logs)
    python3 scripts/gt06_extract_battery_data.py \
        --logs 2026_06_18.jsonl.gz 2026_06_19.jsonl.gz 2026_06_20.jsonl.gz 2026_06_21.jsonl.gz \
        --unplug '2026-06-18 17:30' --tz 10 --power-w 0.381 --track-ma 115 \
        --out-dir gt06/battery_data

    # idle + sleep (from the GT06 binary log; combine the rotated archive + current so
    # logins are present:  cat old_logs/gt06.log.2026-06-21 <(tail -c +9 gt06.log) > comb.log)
    python3 scripts/gt06_extract_voltage_series.py --log comb.log --mode idle \
        --start 1782020506 --end 1782057600 --out gt06/battery_data/idle.csv.gz
    python3 scripts/gt06_extract_voltage_series.py --log comb.log --mode sleep \
        --start 1782055800 --sleep-start 1782057600 --wake-end 1782079200 \
        --out gt06/battery_data/sleep.csv.gz

## Fit

    python3 scripts/gt06_fit_soc_curve.py --correction offset --soc-lo 10 --soc-hi 90 \
        --ocv global-r --out gt06/battery_data/soc_fit.json

Form (single cell, Roho / ArduPilot BattEstimate): `SoC = c1*(1 - 1/(1+(V/c2)^c4)^c3)`,
V = resting/OCV. OCV is reconstructed from the constant tracking load
(`OCV = bat_v + I_track*R`); SoC is charge-based (integrate `I=P/V`). Fitted to the 33
units that fully discharged. `c1` is fixed at 111.56 to break the c1↔c3 degeneracy.

Three data-quality refinements (verified, see commit / memory):

1. **STATUS rounding** — `bat_v` in these logs (and STATUS#) is `round(cxzt, nearest
   10 mV)` (confirmed: 5050 paired cxzt/STATUS readings, residual 1.65 mV). Each sample
   carries ±5 mV uniform quantization, averaged down by pooling 33 units × ~80 bins.
2. **End trim** — `--soc-lo 10 --soc-hi 90` drops the surface-charge top (just off the
   charger) and the protection-cutoff floor (near death); this alone cut RMS 4.3→2.4%.
3. **Divider correction** — `--correction offset` (per-unit additive) is the production
   choice. `--correction affine` (per-unit gain+offset) is available as a diagnostic: it
   shows a real but second-order gain (sd ~5%, correction @3.7 V sd ~25 mV) that improves
   RMS only 0.07% inside the trimmed window — gain error lives at the extremes the trim
   removes, and over a single unit's ~0.45 V span gain↔offset are degenerate, so per-unit
   gains are not separately trustworthy.

Production fit: RMS **2.36 %SoC**, 50% OCV = 3.768 V, monotonic. `soc_fit.json` holds the
coefficients.
