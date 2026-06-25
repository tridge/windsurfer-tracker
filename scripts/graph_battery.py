#!/usr/bin/env python3
"""Graph battery voltage versus time from gt06_dump CMDRESP Battery lines."""

import argparse
import json
import os
import re
import sys
from datetime import datetime

import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

# Parametric OCV SoC fit (gt06/battery_data/soc_fit.json) — replaces the old
# single-cell (G226122) discharge table. Approximate for graphing: default 6Ah
# class R, tracking load, no per-unit divider offset.
_SOC_FIT = None
for _p in (os.path.join(os.path.dirname(__file__), "..", "gt06", "battery_data", "soc_fit.json"),
           "gt06/battery_data/soc_fit.json"):
    try:
        _SOC_FIT = json.load(open(_p))
        break
    except OSError:
        continue


def voltage_to_percent(voltage, idle=False):
    """Parametric OCV SoC from a terminal voltage (default 6Ah, tracking load)."""
    if not _SOC_FIT:
        return 0.0
    c = _SOC_FIT["coeffs"]
    r = _SOC_FIT.get("class_r_ohm", {}).get("6Ah", 0.0)
    ocv = voltage + (0.005 if idle else 0.115) * r
    s = c["c1"] * (1 - 1 / (1 + (ocv / c["c2"]) ** c["c4"]) ** c["c3"])
    return max(0.0, min(100.0, s))


def parse_lines(source):
    pattern = re.compile(
        r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.\d+\s+CMDRESP.*Battery:([\d.]+)V'
    )
    times = []
    volts = []
    for line in source:
        m = pattern.match(line)
        if m:
            times.append(datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S'))
            volts.append(float(m.group(2)))
    return times, volts


def median_filter(volts, width=9):
    """Apply a median filter to remove single-sample voltage spikes."""
    if len(volts) < 3:
        return list(volts)
    out = list(volts)
    half = width // 2
    for i in range(half, len(volts) - half):
        window = sorted(volts[i - half:i + half + 1])
        out[i] = window[len(window) // 2]
    return out


def main():
    parser = argparse.ArgumentParser(description='Graph battery voltage from gt06_dump CMDRESP output')
    parser.add_argument('logfile', nargs='?', help='Input file (default: stdin)')
    parser.add_argument('--filter-voltage', action='store_true',
                        help='Apply median(3) filter to remove voltage spikes')
    parser.add_argument('--start-time', metavar='HH:MM:SS',
                        help='Skip data before this time of day')
    args = parser.parse_args()

    if args.logfile:
        source = open(args.logfile)
    else:
        source = sys.stdin

    times, volts = parse_lines(source)
    if not times:
        print("No data found")
        exit(1)

    if args.start_time:
        cutoff = datetime.strptime(args.start_time, '%H:%M:%S').time()
        # Find first sample at/after cutoff, then keep everything from there
        # (handles data crossing midnight)
        start_idx = None
        for i, t in enumerate(times):
            if t.time() >= cutoff:
                start_idx = i
                break
        if start_idx is None:
            print(f"No data after {args.start_time}")
            exit(1)
        times = times[start_idx:]
        volts = volts[start_idx:]

    raw_volts = volts
    if args.filter_voltage:
        volts = median_filter(volts)

    # Trim charging tail: if the filtered minimum voltage is followed by
    # a sustained rise, cut at the minimum
    if args.filter_voltage and len(volts) > 20:
        min_idx = volts.index(min(volts))
        # Only trim if minimum is in the last 30% of data
        if min_idx > len(volts) * 0.7:
            times = times[:min_idx + 1]
            raw_volts = raw_volts[:min_idx + 1]
            volts = volts[:min_idx + 1]

    # Convert to hours from start
    t0 = times[0]
    hours = [(t - t0).total_seconds() / 3600.0 for t in times]

    fig, ax = plt.subplots(figsize=(14, 6))

    ax.set_xlabel('Time from start (h:mm)')
    ax.set_ylabel('Voltage (V)')
    if args.filter_voltage:
        ax.plot(hours, raw_volts, color='tab:red', linewidth=0.5, alpha=0.3, label='Raw V')
        ax.plot(hours, volts, color='tab:red', linewidth=0.8, label='Filtered V')
        # Secondary Y-axis: battery percentage from filtered voltage
        ax2 = ax.twinx()
        pcts = [voltage_to_percent(v) for v in volts]
        ax2.plot(hours, pcts, color='tab:blue', linewidth=0.8, label='Battery %')
        ax2.set_ylabel('Battery (%)')
        ax2.set_ylim(0, 105)
        # Combined legend
        lines1, labels1 = ax.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        ax.legend(lines1 + lines2, labels1 + labels2)
    else:
        ax.plot(hours, volts, color='tab:red', linewidth=0.8)

    def fmt_hours(x, _):
        h = int(x)
        m = int((x - h) * 60)
        return f'{h}:{m:02d}'

    ax.xaxis.set_major_formatter(ticker.FuncFormatter(fmt_hours))

    duration = hours[-1]
    fig.suptitle(f'GT06 Battery Voltage ({times[0].strftime("%Y-%m-%d %H:%M")} start, {duration:.1f}h duration)')
    fig.tight_layout()

    plt.show()

if __name__ == '__main__':
    main()
