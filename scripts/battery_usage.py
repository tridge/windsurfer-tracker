#!/usr/bin/env python3
"""
Battery-usage analyser for the GT06 trackers over a tracking window.

Reuses the log-loading / window-resolution / firmware-mapping helpers from
gps_accuracy.py and reports, per GT06 unit, how fast the battery drained during
the window: the reported battery % drop and (finer-grained) the cell voltage
slope. Battery % is coarse (whole-percent steps), so the headline drain rate is
taken from a least-squares fit of voltage vs time, with the %/h slope shown
alongside and a projected continuous-tracking runtime.

Results are also pooled by firmware line, to see whether firmware affects power
draw (the W07 V6.68 / V6.63 / NT19D V667 split).

Examples:
  python3 scripts/battery_usage.py --logfile tmp/ev8/main.jsonl \\
      --start-ts 1780111982 --end-ts 1780118202
  python3 scripts/battery_usage.py            # fetch fresh from wstracker
  python3 scripts/battery_usage.py --json
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from gps_accuracy import (
    DEFAULT_FIRMWARE, FIRMWARE_ORDER, DEFAULT_START_TS, DEFAULT_END_TS,
    fetch_log, fetch_summary, sublog_window,
)

# Battery capacity per unit (mAh). Two units carry a 3000 mAh cell; the rest are
# 6000 mAh. Capacity matters because %/h drain depends on it — the
# capacity-independent power measure is current draw (mA), computed from %/h.
DEFAULT_CAPACITY = {dev: 3000 for dev in (
    # 10× V6.68 + 1× V6.63 (G226122) carry a 3000 mAh cell; rest are 6000 mAh.
    "G312243", "G312268", "G312292", "G312342", "G375349", "G375356",
    "G375372", "G375539", "G375562", "G378657", "G226122",
)}
DEFAULT_CAPACITY_MAH = 6000


def capacity_of(dev):
    return DEFAULT_CAPACITY.get(dev, DEFAULT_CAPACITY_MAH)


def lin_slope(xs, ys):
    """Least-squares slope of ys vs xs (per unit x), or None if degenerate."""
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom


def collect(text, start_ts, end_ts):
    """Return {device: [(ts, bat, bat_v, chg), ...]} for GT06 units in window.

    Skips charging samples and invalid bat/bat_v so the slope reflects real
    discharge only.
    """
    series = defaultdict(list)
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        dev = e.get("id", "")
        if not dev.startswith("G"):
            continue
        ts = e.get("ts")
        if ts is None or ts < start_ts or ts > end_ts:
            continue
        if e.get("chg"):
            continue
        bat = e.get("bat")
        batv = e.get("bat_v")
        bat = bat if (isinstance(bat, (int, float)) and bat >= 0) else None
        batv = batv if (isinstance(batv, (int, float)) and batv > 0) else None
        if bat is None and batv is None:
            continue
        series[dev].append((ts, bat, batv, e.get("chg")))
    return series


def summarise_device(samples, capacity_mah):
    """Compute battery metrics for one device's in-window samples."""
    samples = sorted(samples, key=lambda s: s[0])
    t0 = samples[0][0]
    hours = [(s[0] - t0) / 3600.0 for s in samples]
    span_h = hours[-1] if hours[-1] > 0 else None

    bat_pts = [(h, s[1]) for h, s in zip(hours, samples) if s[1] is not None]
    v_pts = [(h, s[2]) for h, s in zip(hours, samples) if s[2] is not None]

    bat0 = bat_pts[0][1] if bat_pts else None
    bat1 = bat_pts[-1][1] if bat_pts else None
    v0 = v_pts[0][1] if v_pts else None
    v1 = v_pts[-1][1] if v_pts else None

    # %/h from least-squares fit (robust to coarse integer steps), negated so a
    # positive number means "percent drained per hour".
    pct_slope = lin_slope([h for h, _ in bat_pts], [b for _, b in bat_pts])
    pct_per_h = -pct_slope if pct_slope is not None else None
    mv_slope = lin_slope([h for h, _ in v_pts], [v * 1000 for _, v in v_pts])
    mv_per_h = -mv_slope if mv_slope is not None else None

    # projected continuous-tracking runtime from current %, at this drain rate
    runtime_h = None
    if pct_per_h and pct_per_h > 0 and bat1 is not None:
        runtime_h = bat1 / pct_per_h

    # capacity-independent power draw: mA = (%/h / 100) * capacity_mAh
    draw_ma = (pct_per_h / 100.0) * capacity_mah if pct_per_h is not None else None

    return {
        "n": len(samples),
        "span_h": span_h,
        "capacity_mah": capacity_mah,
        "bat_start": bat0, "bat_end": bat1,
        "bat_drop": (bat0 - bat1) if (bat0 is not None and bat1 is not None) else None,
        "pct_per_h": pct_per_h,
        "draw_ma": draw_ma,
        "v_start": v0, "v_end": v1,
        "mv_per_h": mv_per_h,
        "runtime_h": runtime_h,
    }


def fmt(x, spec):
    return ("{:" + spec + "}").format(x) if x is not None else "-"


def print_device_table(rows):
    print("\nBATTERY DRAIN PER GT06 UNIT (window, off-charge)")
    hdr = (f"{'device':<10}{'fw':>12}{'mAh':>6}{'bat%':>11}{'%/h':>7}"
           f"{'draw mA':>9}{'volts':>13}{'~runtime':>10}")
    print(hdr)
    print("-" * len(hdr))
    for dev, fw, m in rows:
        batrng = (f"{fmt(m['bat_start'],'.0f')}->{fmt(m['bat_end'],'.0f')}")
        vrng = (f"{fmt(m['v_start'],'.3f')}->{fmt(m['v_end'],'.3f')}")
        rt = (f"{m['runtime_h']:.0f}h" if m['runtime_h'] is not None else "-")
        print(f"{dev:<10}{fw:>12}{m['capacity_mah']:>6}{batrng:>11}"
              f"{fmt(m['pct_per_h'],'>7.2f')}{fmt(m['draw_ma'],'>9.1f')}"
              f"{vrng:>13}{rt:>10}")
    print("(draw mA = (%/h/100)*mAh, the capacity-independent power; "
          "~runtime = bat_end / %-per-h)")


def print_firmware_table(groups):
    print("\nBATTERY DRAIN BY FIRMWARE GROUP (mean of member units)")
    hdr = (f"{'firmware':<14}{'units':>6}{'mean %/h':>10}{'mean draw mA':>14}"
           f"{'mean ~runtime':>15}")
    print(hdr)
    print("-" * len(hdr))
    for g, devs, mp, mma, rt in groups:
        print(f"{g:<14}{len(devs):>6}{fmt(mp,'>10.2f')}{fmt(mma,'>14.1f')}"
              f"{(f'{rt:.0f}h' if rt is not None else '-'):>15}")
    print("(draw mA is the real power-consumption measure; %/h & runtime depend "
          "on each unit's mAh)")


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--logfile", help="local jsonl log (skips ssh fetch)")
    ap.add_argument("--host", default="wstracker", help="ssh host (default: wstracker)")
    ap.add_argument("--eid", default="8", help="event id (default: 8)")
    ap.add_argument("--date", default="2026-05-30", help="log date YYYY-MM-DD")
    ap.add_argument("--remote-path", help="override remote log path")
    ap.add_argument("--remote-dir", help="override remote log dir (for summary)")
    ap.add_argument("--sublog", default="Saturday Sail", help="named sublog window")
    ap.add_argument("--start-ts", type=int, help="override window start (unix ts)")
    ap.add_argument("--end-ts", type=int, help="override window end (unix ts)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of tables")
    args = ap.parse_args()

    date_us = args.date.replace("-", "_")
    remote_dir = args.remote_dir or f"tracker/html/{args.eid}/logs"
    remote_path = args.remote_path or f"{remote_dir}/{date_us}.jsonl"

    summary = None
    if args.logfile:
        p = Path(args.logfile)
        text = p.read_text(errors="replace")
        sp = p.parent / f"{date_us}_summary.json"
        if sp.exists():
            try:
                summary = json.loads(sp.read_text())
            except json.JSONDecodeError:
                summary = None
    else:
        sys.stderr.write(f"fetching {args.host}:{remote_path} ...\n")
        text = fetch_log(args.host, remote_path)
        if text is None:
            sys.stderr.write(f"error: could not read {remote_path}[.gz] on {args.host}\n")
            return 1
        summary = fetch_summary(args.host, remote_dir, date_us)

    if args.start_ts is not None and args.end_ts is not None:
        start_ts, end_ts = args.start_ts, args.end_ts
    else:
        win = sublog_window(summary, args.sublog)
        if win and None not in win:
            start_ts, end_ts = win
            sys.stderr.write(f"using sublog '{args.sublog}': {start_ts}..{end_ts}\n")
        else:
            start_ts, end_ts = DEFAULT_START_TS, DEFAULT_END_TS
            sys.stderr.write(f"sublog not found; using {start_ts}..{end_ts}\n")

    series = collect(text, start_ts, end_ts)
    if not series:
        sys.stderr.write("error: no in-window GT06 battery samples\n")
        return 1

    devs = sorted(series)
    metrics = {d: summarise_device(series[d], capacity_of(d)) for d in devs}

    # firmware grouping
    by_fw = defaultdict(list)
    for d in devs:
        by_fw[DEFAULT_FIRMWARE.get(d, "unknown")].append(d)
    fw_order = [g for g in FIRMWARE_ORDER if g in by_fw]
    fw_order += [g for g in by_fw if g not in fw_order]

    if args.json:
        out = {
            "window": {"start_ts": start_ts, "end_ts": end_ts,
                       "span_h": (end_ts - start_ts) / 3600.0},
            "devices": {d: {"firmware": DEFAULT_FIRMWARE.get(d, "unknown"),
                            **metrics[d]} for d in devs},
            "firmware_groups": {
                g: {"devices": by_fw[g],
                    "mean_pct_per_h": mean([metrics[d]["pct_per_h"] for d in by_fw[g]]),
                    "mean_draw_ma": mean([metrics[d]["draw_ma"] for d in by_fw[g]]),
                    "mean_runtime_h": mean([metrics[d]["runtime_h"] for d in by_fw[g]])}
                for g in fw_order},
        }
        print(json.dumps(out, indent=2))
        return 0

    span_h = (end_ts - start_ts) / 3600.0
    print(f"window {start_ts}..{end_ts} ({span_h:.2f} h)  GT06 units={len(devs)}")

    rows = sorted(
        [(d, DEFAULT_FIRMWARE.get(d, "?"), metrics[d]) for d in devs],
        key=lambda r: (r[2]["draw_ma"] is None, r[2]["draw_ma"] or 0), reverse=True)
    print_device_table(rows)

    groups = []
    for g in fw_order:
        ds = by_fw[g]
        groups.append((g, ds,
                       mean([metrics[d]["pct_per_h"] for d in ds]),
                       mean([metrics[d]["draw_ma"] for d in ds]),
                       mean([metrics[d]["runtime_h"] for d in ds])))
    print_firmware_table(groups)
    return 0


if __name__ == "__main__":
    sys.exit(main())
