#!/usr/bin/env python3
"""Analyze GT06 battery drain from server log — idle and active periods.

Parses heartbeat and position lines for a given tracker, identifies idle and
active sessions, smooths quantized battery readings, and produces graphs +
summary tables with drain rate estimates.

Usage:
    python3 gt06_battery_graph.py G226122.log [--tracker G226122]
"""

import argparse
import re
import sys
import statistics
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def parse_log(logfile, tracker_id):
    """Parse log, return list of (datetime, state, battery%) for tracker_id.

    state: 'idle' for [GT06] Heartbeat lines, 'active' for [tracker_id] pos= lines.
    Deduplicates entries within the same second.
    """
    entries = []
    seen_seconds = set()

    hb_re = re.compile(
        r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[GT06\] Heartbeat '
        + re.escape(tracker_id) + r': bat=(\d+)%'
    )
    pos_re = re.compile(
        r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \['
        + re.escape(tracker_id) + r'\] pos=.+bat=(\d+)%'
    )

    with open(logfile, 'rb') as f:
        for raw_line in f:
            try:
                line = raw_line.decode('utf-8', errors='replace').rstrip()
            except Exception:
                continue

            m = hb_re.match(line)
            if m:
                ts = datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')
                bat = int(m.group(2))
                key = ('idle', ts)
                if key not in seen_seconds:
                    seen_seconds.add(key)
                    entries.append((ts, 'idle', bat))
                continue

            m = pos_re.match(line)
            if m:
                ts = datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')
                bat = int(m.group(2))
                key = ('active', ts)
                if key not in seen_seconds:
                    seen_seconds.add(key)
                    entries.append((ts, 'active', bat))

    entries.sort(key=lambda x: x[0])
    return entries


def find_idle_periods(entries, min_hours=1.0):
    """Split entries into continuous idle periods (heartbeat-only, >min_hours)."""
    periods = []
    current = []

    for ts, state, bat in entries:
        if state == 'idle':
            current.append((ts, bat))
        else:
            if current:
                duration_h = (current[-1][0] - current[0][0]).total_seconds() / 3600
                if duration_h >= min_hours:
                    periods.append(current)
                current = []

    if current:
        duration_h = (current[-1][0] - current[0][0]).total_seconds() / 3600
        if duration_h >= min_hours:
            periods.append(current)

    return periods


def find_active_sessions(entries, gap_minutes=10, min_hours=2.0):
    """Find continuous active tracking sessions.

    Splits on gaps > gap_minutes between position reports.
    Returns sessions >= min_hours.
    """
    pos_entries = [(ts, bat) for ts, state, bat in entries if state == 'active']
    if not pos_entries:
        return []

    sessions = []
    current = [pos_entries[0]]
    gap_thresh = timedelta(minutes=gap_minutes)

    for i in range(1, len(pos_entries)):
        if pos_entries[i][0] - pos_entries[i - 1][0] > gap_thresh:
            sessions.append(current)
            current = [pos_entries[i]]
        else:
            current.append(pos_entries[i])
    if current:
        sessions.append(current)

    return [s for s in sessions
            if (s[-1][0] - s[0][0]).total_seconds() / 3600 >= min_hours]


def smooth_battery(readings, window_minutes=10):
    """Smooth battery readings using median within time windows."""
    if not readings:
        return []

    start_ts = readings[0][0]
    end_ts = readings[-1][0]
    window = timedelta(minutes=window_minutes)

    smoothed = []
    window_start = start_ts
    while window_start <= end_ts:
        window_end = window_start + window
        vals = [bat for ts, bat in readings if window_start <= ts < window_end]
        if vals:
            mid_ts = window_start + window / 2
            smoothed.append((mid_ts, statistics.median(vals)))
        window_start = window_end

    return smoothed


def is_charging(readings):
    """Detect if a period is charging (battery rises significantly)."""
    if len(readings) < 2:
        return False
    smoothed = smooth_battery(readings)
    if len(smoothed) < 2:
        return False
    return smoothed[-1][1] - smoothed[0][1] > 20


def analyze_and_plot(logfile, tracker_id):
    print(f"Parsing {logfile} for tracker {tracker_id}...")
    entries = parse_log(logfile, tracker_id)
    if not entries:
        print("No entries found!")
        sys.exit(1)

    print(f"  {len(entries)} entries from {entries[0][0]} to {entries[-1][0]}")
    idle_count = sum(1 for _, s, _ in entries if s == 'idle')
    active_count = len(entries) - idle_count
    print(f"  {idle_count} idle heartbeats, {active_count} active positions")

    # --- IDLE ANALYSIS ---
    idle_periods = find_idle_periods(entries)
    idle_drain = []
    idle_charging = []
    for p in idle_periods:
        if is_charging(p):
            idle_charging.append(p)
        else:
            idle_drain.append(p)

    print(f"  {len(idle_drain)} idle drain periods, {len(idle_charging)} idle charging periods")

    MIN_RELIABLE_IDLE_H = 8.0
    idle_total_drain = 0
    idle_total_hours = 0
    idle_plot_data = []

    print(f"\n{'='*95}")
    print(f"IDLE DRAIN PERIODS (>1h, non-charging)")
    print(f"{'='*95}")
    print(f"{'#':>2}  {'Start':>19}  {'End':>19}  {'Duration':>8}  {'Start':>5}  {'End':>5}  {'Drain':>5}  {'Rate':>8}")
    print(f"{'--':>2}  {'-------------------':>19}  {'-------------------':>19}  {'--------':>8}  {'-----':>5}  {'-----':>5}  {'-----':>5}  {'--------':>8}")

    for i, p in enumerate(idle_drain):
        smoothed = smooth_battery(p)
        if len(smoothed) < 2:
            continue
        start_bat = smoothed[0][1]
        end_bat = smoothed[-1][1]
        duration_h = (smoothed[-1][0] - smoothed[0][0]).total_seconds() / 3600
        drain = start_bat - end_bat
        if drain <= 0 or duration_h < 1:
            continue

        rate = drain / duration_h
        reliable = duration_h >= MIN_RELIABLE_IDLE_H
        if reliable:
            idle_total_drain += drain
            idle_total_hours += duration_h

        label = f"{p[0][0].strftime('%b %d %H:%M')} ({duration_h:.1f}h, {drain:.0f}%)"
        offsets = [((ts - smoothed[0][0]).total_seconds() / 3600, bat) for ts, bat in smoothed]
        idle_plot_data.append((label, offsets))

        flag = "" if reliable else "  *short"
        print(f"{i+1:>2}  {p[0][0].strftime('%Y-%m-%d %H:%M:%S'):>19}  "
              f"{p[-1][0].strftime('%Y-%m-%d %H:%M:%S'):>19}  "
              f"{duration_h:>7.1f}h  {start_bat:>4.0f}%  {end_bat:>4.0f}%  "
              f"{drain:>4.0f}%  {rate:>6.2f}%/h{flag}")

    print(f"\n  * Periods < {MIN_RELIABLE_IDLE_H}h excluded from average "
          f"(quantization noise dominates)")

    if idle_charging:
        print(f"\n  Charging periods excluded:")
        for p in idle_charging:
            smoothed = smooth_battery(p)
            if len(smoothed) >= 2:
                print(f"    {p[0][0].strftime('%Y-%m-%d %H:%M:%S')} -> "
                      f"{p[-1][0].strftime('%Y-%m-%d %H:%M:%S')}  "
                      f"bat {smoothed[0][1]:.0f}% -> {smoothed[-1][1]:.0f}%")

    idle_rate = idle_total_drain / idle_total_hours if idle_total_hours > 0 else 0
    idle_life = 100 / idle_rate if idle_rate > 0 else float('inf')

    # --- ACTIVE ANALYSIS ---
    active_sessions = find_active_sessions(entries)
    print(f"\n{'='*95}")
    print(f"ACTIVE TRACKING SESSIONS (>2h)")
    print(f"{'='*95}")
    print(f"{'#':>2}  {'Start':>19}  {'End':>19}  {'Duration':>8}  {'Start':>5}  {'End':>5}  {'Drain':>5}  {'Rate':>8}  {'Notes'}")
    print(f"{'--':>2}  {'-------------------':>19}  {'-------------------':>19}  {'--------':>8}  {'-----':>5}  {'-----':>5}  {'-----':>5}  {'--------':>8}  {'-----'}")

    MIN_RELIABLE_ACTIVE_H = 3.0
    active_total_drain = 0
    active_total_hours = 0
    active_plot_data = []

    for i, s in enumerate(active_sessions):
        smoothed = smooth_battery(s)
        if len(smoothed) < 2:
            continue
        start_bat = smoothed[0][1]
        end_bat = smoothed[-1][1]
        duration_h = (s[-1][0] - s[0][0]).total_seconds() / 3600
        drain = start_bat - end_bat
        rate = drain / duration_h if duration_h > 0 else 0

        notes = ''
        if drain < 0:
            notes = 'CHARGING'
        elif drain == 0:
            notes = 'no boundary crossed'

        reliable = drain > 0 and duration_h >= MIN_RELIABLE_ACTIVE_H
        if reliable:
            active_total_drain += drain
            active_total_hours += duration_h

            label = f"{s[0][0].strftime('%b %d %H:%M')} ({duration_h:.1f}h, {drain:.0f}%)"
            offsets = [((ts - smoothed[0][0]).total_seconds() / 3600, bat) for ts, bat in smoothed]
            active_plot_data.append((label, offsets))

        flag = ""
        if drain > 0 and not reliable:
            flag = "  *short"

        print(f"{i+1:>2}  {s[0][0].strftime('%Y-%m-%d %H:%M:%S'):>19}  "
              f"{s[-1][0].strftime('%Y-%m-%d %H:%M:%S'):>19}  "
              f"{duration_h:>7.1f}h  {start_bat:>4.0f}%  {end_bat:>4.0f}%  "
              f"{drain:>4.0f}%  {rate:>6.2f}%/h  {notes}{flag}")

    if active_total_hours > 0:
        print(f"\n  * Sessions < {MIN_RELIABLE_ACTIVE_H}h or with charging excluded from average")

    active_rate = active_total_drain / active_total_hours if active_total_hours > 0 else 0
    active_life = 100 / active_rate if active_rate > 0 else float('inf')

    # --- COMBINED SUMMARY ---
    print(f"\n{'='*95}")
    print(f"SUMMARY")
    print(f"{'='*95}")
    print(f"  Idle drain rate:     {idle_rate:.2f}%/hr  "
          f"({idle_total_hours:.0f}h of data, est. life {idle_life:.0f}h)")
    print(f"  Active drain rate:   {active_rate:.2f}%/hr  "
          f"({active_total_hours:.0f}h of data, est. life {active_life:.0f}h)")
    if idle_rate > 0:
        print(f"  Active/idle ratio:   {active_rate/idle_rate:.1f}x")

    print(f"\n  5-DAY EVENT ESTIMATE (4h active + 20h idle per day):")
    daily_drain = active_rate * 4 + idle_rate * 20
    print(f"    Active: {active_rate:.2f}%/h x 4h = {active_rate*4:.1f}%")
    print(f"    Idle:   {idle_rate:.2f}%/h x 20h = {idle_rate*20:.1f}%")
    print(f"    Daily total: {daily_drain:.1f}%")
    if daily_drain > 0:
        print(f"    Days per charge: {100/daily_drain:.1f}")
        print(f"    5-day total: {daily_drain*5:.0f}%")

    print(f"\n  BATTERY SIZE COMPARISON (5-day event):")
    print(f"  {'Battery':>10}  {'Daily':>8}  {'Days/chg':>8}  {'5-day':>7}  {'Verdict'}")
    print(f"  {'--------':>10}  {'------':>8}  {'--------':>8}  {'-----':>7}  {'-------'}")
    for mah in [3000, 5000, 8000, 10000, 12000]:
        scale = mah / 3000
        d = daily_drain / scale
        total = d * 5
        days = 100 / d if d > 0 else float('inf')
        if total <= 80:
            verdict = "SAFE"
        elif total <= 100:
            verdict = "TIGHT"
        else:
            verdict = f"need {(total-100)/100:.1f} recharges"
        print(f"  {mah:>8}mAh  {d:>6.1f}%  {days:>7.1f}d  {total:>5.0f}%  {verdict}")

    # --- GRAPHS ---
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 7))

    colors = plt.cm.tab10.colors

    # Idle drain graph
    for i, (label, offsets) in enumerate(idle_plot_data):
        hours = [h for h, _ in offsets]
        bats = [b for _, b in offsets]
        ax1.plot(hours, bats, '-o', markersize=3, color=colors[i % len(colors)],
                 label=label, linewidth=1.5)
    ax1.set_xlabel('Hours since start of idle period')
    ax1.set_ylabel('Battery %')
    ax1.set_title(f'{tracker_id} Idle Battery Drain\n'
                  f'Avg rate: {idle_rate:.2f}%/hr, Est. life: {idle_life:.0f}h')
    ax1.set_ylim(0, 105)
    ax1.grid(True, alpha=0.3)
    ax1.legend(loc='best', fontsize=7)

    # Active drain graph
    for i, (label, offsets) in enumerate(active_plot_data):
        hours = [h for h, _ in offsets]
        bats = [b for _, b in offsets]
        ax2.plot(hours, bats, '-o', markersize=3, color=colors[i % len(colors)],
                 label=label, linewidth=1.5)
    ax2.set_xlabel('Hours since start of active session')
    ax2.set_ylabel('Battery %')
    ax2.set_title(f'{tracker_id} Active Tracking Drain\n'
                  f'Avg rate: {active_rate:.2f}%/hr, Est. life: {active_life:.0f}h')
    ax2.set_ylim(0, 105)
    ax2.grid(True, alpha=0.3)
    ax2.legend(loc='best', fontsize=7)

    plt.tight_layout()

    out_path = Path(__file__).parent / f'{tracker_id}_battery_drain.png'
    fig.savefig(out_path, dpi=150)
    print(f"\nGraph saved to: {out_path}")
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description='Analyze GT06 battery drain')
    parser.add_argument('logfile', help='Path to GT06 server log file')
    parser.add_argument('--tracker', default='G226122',
                        help='Tracker ID (default: G226122)')
    args = parser.parse_args()
    analyze_and_plot(args.logfile, args.tracker)


if __name__ == '__main__':
    main()
