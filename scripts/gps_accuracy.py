#!/usr/bin/env python3
"""
GPS accuracy analyser for co-located trackers.

When several GPS devices are carried together (e.g. a bag of trackers + a phone
+ a watch all on the one windsurfer), their true position is identical at any
instant. We can therefore use the MEDIAN position across devices as ground
truth and measure each device's deviation from it. This is a classic
co-located static/dynamic GPS accuracy test.

For each device this reports the standard-deviation position error in metres
(the headline metric), plus mean / CEP50 / CEP95 / max / RMS(DRMS) and the
systematic bias vector. It does this twice: over ALL valid samples, and over a
STATIONARY-only subset (median device speed below --max-speed). The stationary
subset isolates pure GPS noise from the apparent error that sub-second
timestamp misalignment injects during fast movement (~4 m per 0.5 s at 15 kn).

Examples:

  # local cached log, Saturday Sail window
  python3 scripts/gps_accuracy.py --logfile tmp/ev8/main.jsonl \\
      --start-ts 1780111982 --end-ts 1780118202

  # fetch fresh from wstracker (event 8, the "Saturday Sail" sublog)
  python3 scripts/gps_accuracy.py

  # machine-readable
  python3 scripts/gps_accuracy.py --logfile tmp/ev8/main.jsonl --json
"""

import argparse
import json
import math
import statistics
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

# The user's 13 co-located devices on 2026-05-30 (event 8, "Saturday Sail").
# 11 GT06 trackers + Pixel 4 watch (AndrewT2) + Pixel 9a phone (AndrewT).
DEFAULT_USERS = [
    "G378848", "G370589", "G388862", "G375356", "G312334", "G375547",
    "G334189", "G334023", "G306088", "G347082", "G226122",
    "AndrewT2", "AndrewT",
]

# Fallback window if the summary sublog can't be read.
DEFAULT_START_TS = 1780111982
DEFAULT_END_TS = 1780118202

# Per-device firmware, from `cxzt#` firmware strings in tracker.log on
# 2026-05-30 (verified against the live server, matches the documented split).
# Phone/watch are not GT06 firmware lines but are grouped here so they appear
# in the firmware comparison as their own classes.
DEFAULT_FIRMWARE = {
    "G378848": "W07_V6.68", "G375356": "W07_V6.68",
    "G312334": "W07_V6.68", "G375547": "W07_V6.68",
    "G226122": "W07_V6.63",
    "G370589": "NT19D_V667", "G388862": "NT19D_V667", "G334189": "NT19D_V667",
    "G334023": "NT19D_V667", "G306088": "NT19D_V667", "G347082": "NT19D_V667",
    "AndrewT": "Pixel9a_phone", "AndrewT2": "Pixel4_watch",
}

# Display order for firmware groups (any extras appended after).
FIRMWARE_ORDER = ["W07_V6.63", "W07_V6.68", "NT19D_V667",
                  "Pixel9a_phone", "Pixel4_watch"]

# Upper edges (knots) for the speed bands. The final band is "edge+".
DEFAULT_SPEED_BANDS = [2, 5, 10, 15, 20]

M_PER_DEG_LAT = 111320.0


def ssh_run(host, cmd):
    """Run a command via ssh, return stdout bytes (binary-safe for .gz)."""
    p = subprocess.run(["ssh", host, cmd], capture_output=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr.decode("utf-8", "replace"))
    return p.stdout


def fetch_log(host, remote_path):
    """Fetch a remote log over ssh, transparently handling a .gz variant.

    Returns the decoded text of the log file, or None if nothing was found.
    """
    # Prefer plain file; fall back to gzip. `cat`-ing a missing file yields b"".
    data = ssh_run(host, f"cat {remote_path} 2>/dev/null || true")
    if data.strip():
        return data.decode("utf-8", "replace")
    gz = ssh_run(host, f"cat {remote_path}.gz 2>/dev/null || true")
    if gz.strip():
        import gzip
        return gzip.decompress(gz).decode("utf-8", "replace")
    return None


def fetch_summary(host, remote_dir, date):
    """Fetch {remote_dir}/{date}_summary.json over ssh; return parsed dict or None."""
    path = f"{remote_dir}/{date}_summary.json"
    data = ssh_run(host, f"cat {path} 2>/dev/null || true")
    if data.strip():
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return None
    return None


def sublog_window(summary, name):
    """Find a named sublog's (start_ts, end_ts) in a summary dict, or None."""
    if not summary:
        return None
    for log in summary.get("logs", []):
        for sub in log.get("sublogs", []):
            if sub.get("name") == name:
                return sub.get("start_ts"), sub.get("end_ts")
    return None


def iter_points(text, users, start_ts, end_ts):
    """Yield (device_id, ts, lat, lon, spd) from raw jsonl text.

    Handles both line formats:
      - top-level lat/lon/ts (cached logs)
      - compact pos:[[ts,lat,lon,spd?],...] arrays (live server logs)
    Skips nogps lines, devices not in `users`, and out-of-window points.
    """
    userset = set(users)
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        dev = e.get("id")
        if dev not in userset:
            continue
        if e.get("nogps"):
            continue

        pos = e.get("pos")
        if isinstance(pos, list) and pos:
            for pt in pos:
                if not pt or len(pt) < 3:
                    continue
                ts = pt[0]
                if ts is None or ts < start_ts or ts > end_ts:
                    continue
                lat, lon = pt[1], pt[2]
                spd = pt[3] if len(pt) > 3 else e.get("spd")
                if lat is None or lon is None:
                    continue
                yield dev, int(ts), float(lat), float(lon), spd
        else:
            ts = e.get("ts")
            lat, lon = e.get("lat"), e.get("lon")
            if ts is None or lat is None or lon is None:
                continue
            if ts < start_ts or ts > end_ts:
                continue
            yield dev, int(ts), float(lat), float(lon), e.get("spd")


def build_bins(points):
    """Group points into integer-second bins.

    Returns {ts: {device: (lat, lon, spd)}}. If a device reports more than once
    in a second, the last one wins (1 Hz data normally gives exactly one).
    """
    bins = defaultdict(dict)
    for dev, ts, lat, lon, spd in points:
        bins[ts][dev] = (lat, lon, spd)
    return bins


def analyse(bins, min_devices):
    """Compute per-device error samples against the per-instant median truth.

    Returns (per_dev, bins_used) where per_dev maps device -> list of
    (err_m, east_m, north_m, truth_speed_kn). truth_speed is the median of the
    devices' reported speeds at that instant (None if none reported), and is
    used later for speed-band and stationary splits.
    """
    per_dev = defaultdict(list)
    bins_used = 0

    for ts in sorted(bins):
        devs = bins[ts]
        if len(devs) < min_devices:
            continue
        lats = [v[0] for v in devs.values()]
        lons = [v[1] for v in devs.values()]
        tlat = statistics.median(lats)
        tlon = statistics.median(lons)

        # shared "truth" speed = median of reported device speeds this instant
        spds = [v[2] for v in devs.values() if v[2] is not None]
        tspd = statistics.median(spds) if spds else None

        m_per_deg_lon = M_PER_DEG_LAT * math.cos(math.radians(tlat))
        bins_used += 1

        for dev, (lat, lon, _spd) in devs.items():
            east = (lon - tlon) * m_per_deg_lon
            north = (lat - tlat) * M_PER_DEG_LAT
            err = math.hypot(east, north)
            per_dev[dev].append((err, east, north, tspd))

    return per_dev, bins_used


def band_labels(edges):
    """Return human labels for the speed bands defined by upper `edges`."""
    labels = []
    lo = 0
    for hi in edges:
        labels.append(f"{lo:g}-{hi:g}")
        lo = hi
    labels.append(f"{lo:g}+")
    return labels


def band_index(tspd, edges):
    """Index of the speed band a truth-speed falls in, or None if unknown."""
    if tspd is None:
        return None
    for i, hi in enumerate(edges):
        if tspd < hi:
            return i
    return len(edges)


def summarise(samples):
    """Reduce a list of (err, east, north) to a metrics dict (or None if empty)."""
    if not samples:
        return None
    errs = [s[0] for s in samples]
    easts = [s[1] for s in samples]
    norths = [s[2] for s in samples]
    n = len(errs)
    errs_sorted = sorted(errs)

    def pct(p):
        if n == 1:
            return errs_sorted[0]
        k = (n - 1) * p
        lo = int(math.floor(k))
        hi = int(math.ceil(k))
        if lo == hi:
            return errs_sorted[lo]
        return errs_sorted[lo] + (errs_sorted[hi] - errs_sorted[lo]) * (k - lo)

    bias_e = statistics.mean(easts)
    bias_n = statistics.mean(norths)
    return {
        "n": n,
        "std": statistics.pstdev(errs),
        "mean": statistics.mean(errs),
        "cep50": statistics.median(errs),
        "cep95": pct(0.95),
        "max": max(errs),
        "rms": math.sqrt(sum(e * e for e in errs) / n),
        "bias_e": bias_e,
        "bias_n": bias_n,
        "bias_mag": math.hypot(bias_e, bias_n),
    }


def print_table(title, rows):
    """rows: list of (device, metrics_dict). Prints an aligned text table."""
    print(f"\n{title}")
    hdr = (f"{'device':<14} {'n':>6} {'std':>7} {'mean':>7} {'CEP50':>7} "
           f"{'CEP95':>7} {'max':>7} {'DRMS':>7} {'bias':>7}")
    print(hdr)
    print("-" * len(hdr))
    for dev, m in rows:
        if m is None:
            print(f"{dev:<14} {'(no samples)':>6}")
            continue
        print(f"{dev:<14} {m['n']:>6} {m['std']:>7.2f} {m['mean']:>7.2f} "
              f"{m['cep50']:>7.2f} {m['cep95']:>7.2f} {m['max']:>7.2f} "
              f"{m['rms']:>7.2f} {m['bias_mag']:>7.2f}")
    print("(all values in metres; std = std-dev of position error vs median truth)")


def std_of(samples):
    """Population std-dev of the error column, or None if too few samples."""
    if len(samples) < 2:
        return None
    return statistics.pstdev([s[0] for s in samples])


def print_band_matrix(title, rows, edges):
    """Std-dev-error matrix: one row per (label, samples), one column per band.

    Each cell is the std-dev of error for samples in that speed band; the
    bracketed number under the header is the per-band sample count (summed over
    all rows). A trailing column gives the row's total sample count.
    """
    labels = band_labels(edges)
    print(f"\n{title}")
    cw = 9
    hdr = f"{'':<14}" + "".join(f"{lab:>{cw}}" for lab in labels) + f"{'n':>9}"
    print(hdr)
    print("-" * len(hdr))
    band_counts = [0] * len(labels)
    for name, samples in rows:
        by_band = [[] for _ in labels]
        for s in samples:
            bi = band_index(s[3], edges)
            if bi is not None:
                by_band[bi].append(s)
        cells = ""
        for bi, bs in enumerate(by_band):
            band_counts[bi] += len(bs)
            sd = std_of(bs)
            cells += (f"{sd:>{cw}.2f}" if sd is not None else f"{'-':>{cw}}")
        print(f"{name:<14}{cells}{len(samples):>9}")
    print("-" * len(hdr))
    foot = f"{'band n':<14}" + "".join(f"{c:>{cw}}" for c in band_counts)
    print(foot)
    print("(cells = std-dev of position error in m, by truth-speed band in knots)")


def firmware_rows(seen, per_dev, fwmap):
    """Group device samples by firmware. Returns list of (group, pooled_samples)
    ordered by FIRMWARE_ORDER, plus a {group: [devices]} map."""
    groups = defaultdict(list)
    members = defaultdict(list)
    for d in seen:
        g = fwmap.get(d, "unknown")
        groups[g].extend(per_dev[d])
        members[g].append(d)
    order = [g for g in FIRMWARE_ORDER if g in groups]
    order += [g for g in groups if g not in order]
    return [(g, groups[g]) for g in order], members


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--logfile", help="local jsonl log to analyse (skips ssh fetch)")
    ap.add_argument("--host", default="wstracker", help="ssh host for --fetch (default: wstracker)")
    ap.add_argument("--eid", default="8", help="event id (default: 8)")
    ap.add_argument("--date", default="2026-05-30", help="log date YYYY-MM-DD (default: 2026-05-30)")
    ap.add_argument("--remote-path", help="override remote log path")
    ap.add_argument("--remote-dir", help="override remote log directory (for summary)")
    ap.add_argument("--sublog", default="Saturday Sail",
                    help="named sublog window to analyse (default: 'Saturday Sail')")
    ap.add_argument("--start-ts", type=int, help="override window start (unix ts)")
    ap.add_argument("--end-ts", type=int, help="override window end (unix ts)")
    ap.add_argument("--users", help="comma-separated device ids (default: the 13 co-located devices)")
    ap.add_argument("--min-devices", type=int, default=6,
                    help="min devices in a second-bin for a valid truth (default: 6)")
    ap.add_argument("--max-speed", type=float, default=2.0,
                    help="median speed (kn) below which a bin is 'stationary' (default: 2.0)")
    ap.add_argument("--speed-bands", default=",".join(str(b) for b in DEFAULT_SPEED_BANDS),
                    help="comma-separated upper edges (kn) for speed bands "
                         "(default: 2,5,10,15,20)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = ap.parse_args()

    users = [u.strip() for u in args.users.split(",")] if args.users else DEFAULT_USERS
    edges = [float(x) for x in args.speed_bands.split(",") if x.strip() != ""]
    date_us = args.date.replace("-", "_")

    remote_dir = args.remote_dir or f"tracker/html/{args.eid}/logs"
    remote_path = args.remote_path or f"{remote_dir}/{date_us}.jsonl"

    # ---- load log text + resolve window ----------------------------------
    summary = None
    if args.logfile:
        p = Path(args.logfile)
        text = p.read_text(errors="replace")
        # try a sibling summary for sublog lookup
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
            sys.stderr.write(
                f"error: could not read {remote_path}[.gz] on {args.host}; "
                f"check --remote-path / --date\n")
            return 1
        summary = fetch_summary(args.host, remote_dir, date_us)

    if args.start_ts is not None and args.end_ts is not None:
        start_ts, end_ts = args.start_ts, args.end_ts
    else:
        win = sublog_window(summary, args.sublog)
        if win and win[0] is not None and win[1] is not None:
            start_ts, end_ts = win
            sys.stderr.write(f"using sublog '{args.sublog}': {start_ts}..{end_ts}\n")
        else:
            start_ts, end_ts = DEFAULT_START_TS, DEFAULT_END_TS
            sys.stderr.write(
                f"sublog '{args.sublog}' not found; using default window "
                f"{start_ts}..{end_ts}\n")

    # ---- analyse ----------------------------------------------------------
    points = iter_points(text, users, start_ts, end_ts)
    bins = build_bins(points)
    if not bins:
        sys.stderr.write("error: no in-window points for the selected devices\n")
        return 1
    per_dev, bins_used = analyse(bins, args.min_devices)

    # device order: keep the configured user order, then any extras seen
    seen = [u for u in users if u in per_dev]
    seen += [d for d in per_dev if d not in seen]

    fwmap = DEFAULT_FIRMWARE

    def stationary(samples):
        return [s for s in samples if s[3] is not None and s[3] < args.max_speed]

    bins_stat = sum(1 for ts in bins
                    if (lambda v: v and statistics.median(v) < args.max_speed)(
                        [x[2] for x in bins[ts].values() if x[2] is not None])
                    and len(bins[ts]) >= args.min_devices)

    # per-device tables (sorted by std-dev, None last)
    all_rows = sorted([(d, summarise(per_dev[d])) for d in seen],
                      key=lambda r: (r[1] is None, r[1]["std"] if r[1] else 0))
    stat_rows = sorted([(d, summarise(stationary(per_dev[d]))) for d in seen],
                       key=lambda r: (r[1] is None, r[1]["std"] if r[1] else 0))

    fw_rows, fw_members = firmware_rows(seen, per_dev, fwmap)
    fw_metric_rows = [(g, summarise(samples)) for g, samples in fw_rows]

    if args.json:
        out = {
            "window": {"start_ts": start_ts, "end_ts": end_ts},
            "bins_used": bins_used,
            "bins_stationary": bins_stat,
            "min_devices": args.min_devices,
            "max_speed_kn": args.max_speed,
            "speed_bands_kn": edges,
            "speed_band_labels": band_labels(edges),
            "devices": {
                d: {
                    "firmware": fwmap.get(d, "unknown"),
                    "all": summarise(per_dev[d]),
                    "stationary": summarise(stationary(per_dev[d])),
                    "by_band": [summarise([s for s in per_dev[d]
                                           if band_index(s[3], edges) == b])
                                for b in range(len(edges) + 1)],
                }
                for d in seen
            },
            "firmware_groups": {
                g: {"devices": fw_members[g], "all": summarise(samples),
                    "by_band": [summarise([s for s in samples
                                           if band_index(s[3], edges) == b])
                                for b in range(len(edges) + 1)]}
                for g, samples in fw_rows
            },
        }
        print(json.dumps(out, indent=2))
        return 0

    span_min = (end_ts - start_ts) / 60.0
    print(f"window {start_ts}..{end_ts} ({span_min:.1f} min)  "
          f"devices={len(seen)}  bins={bins_used} (stationary={bins_stat})  "
          f"min_devices={args.min_devices}  max_speed={args.max_speed} kn")
    print_table("ALL SAMPLES (sorted by std-dev error)", all_rows)
    print_table(f"STATIONARY ONLY (median speed < {args.max_speed} kn)", stat_rows)

    print_band_matrix("STD-DEV ERROR BY SPEED BAND (per device)",
                      [(d, per_dev[d]) for d in seen], edges)

    print_table("FIRMWARE GROUP COMPARISON (all samples)", fw_metric_rows)
    for g, _ in fw_rows:
        print(f"  {g}: {', '.join(fw_members[g])}")
    print_band_matrix("STD-DEV ERROR BY SPEED BAND (per firmware group)",
                      fw_rows, edges)
    return 0


if __name__ == "__main__":
    sys.exit(main())
