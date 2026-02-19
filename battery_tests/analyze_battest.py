#!/usr/bin/env python3
"""Analyze a battery test log file and produce drain statistics.

Usage:
    python3 analyze_battest.py battest.log [--trackers Tracker1,Tracker2,...]
                                           [--tracking-start HH:MM]
                                           [--tracking-end HH:MM]
                                           [--idle-end HH:MM]

If --tracking-start/end are not given, they are auto-detected from
state transitions (Idle → tracking, tracking → Idle).
"""

import argparse
import re
import sys
from datetime import datetime, timedelta
from collections import defaultdict


# States a tracker can be in
IDLE = "Idle"
GPS_WAIT = "GPS-wait"
TRACKING = "Tracking"


def parse_line(line):
    """Parse a log line, return (timestamp, tracker_id, state, battery%) or None."""
    # Match: 2026-02-19 HH:MM:SS [TrackerN] ...
    m = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[([^\]]+)\] (.+)', line)
    if not m:
        return None

    ts_str, tracker_id, rest = m.groups()
    ts = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')

    # Extract battery - handle both "bat=85%" and "bat=100%+"
    bat_m = re.search(r'bat=(\d+)%', rest)
    if not bat_m:
        return None
    battery = int(bat_m.group(1))

    # Determine state
    if rest.startswith('Idle heartbeat'):
        state = IDLE
    elif rest.startswith('GPS-wait heartbeat'):
        state = GPS_WAIT
    elif rest.startswith('pos='):
        state = TRACKING
    else:
        return None

    return ts, tracker_id, state, battery


def analyze_log(logfile, tracker_filter=None, tracking_start=None, tracking_end=None, idle_end=None):
    """Parse log and produce per-tracker analysis."""

    # Per-tracker: list of (timestamp, state, battery)
    tracker_data = defaultdict(list)

    with open(logfile) as f:
        for line in f:
            parsed = parse_line(line.rstrip())
            if parsed is None:
                continue
            ts, tid, state, bat = parsed
            if tracker_filter and tid not in tracker_filter:
                continue
            tracker_data[tid].append((ts, state, bat))

    if not tracker_data:
        print("No tracker data found!")
        return

    # Sort tracker names naturally
    def natural_key(s):
        return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', s)]
    tracker_ids = sorted(tracker_data.keys(), key=natural_key)

    # Detect state transitions per tracker
    print("=" * 80)
    print("STATE TRANSITIONS")
    print("=" * 80)
    transitions = {}  # tid -> list of (timestamp, from_state, to_state, battery)
    for tid in tracker_ids:
        entries = tracker_data[tid]
        trans = []
        prev_state = None
        for ts, state, bat in entries:
            if state != prev_state:
                trans.append((ts, prev_state, state, bat))
                prev_state = state
        transitions[tid] = trans
        print(f"\n{tid}:")
        for ts, from_s, to_s, bat in trans:
            from_str = from_s or "start"
            print(f"  {ts.strftime('%H:%M:%S')}  {from_str:>10} → {to_s:<10}  bat={bat}%")

    # Auto-detect tracking window if not specified
    # Use the date from the first entry
    base_date = tracker_data[tracker_ids[0]][0][0].date()

    if tracking_start:
        h, m = map(int, tracking_start.split(':'))
        t_start = datetime.combine(base_date, datetime.min.time().replace(hour=h, minute=m))
    else:
        # Find earliest Idle→non-Idle transition after midnight
        earliest = None
        for tid in tracker_ids:
            for ts, from_s, to_s, bat in transitions[tid]:
                if from_s == IDLE and to_s in (GPS_WAIT, TRACKING):
                    if earliest is None or ts < earliest:
                        earliest = ts
        t_start = earliest
        if t_start:
            print(f"\nAuto-detected tracking start: {t_start}")

    if tracking_end:
        h, m = map(int, tracking_end.split(':'))
        t_end = datetime.combine(base_date, datetime.min.time().replace(hour=h, minute=m))
    else:
        # Find latest non-Idle→Idle transition
        latest = None
        for tid in tracker_ids:
            for ts, from_s, to_s, bat in transitions[tid]:
                if from_s in (GPS_WAIT, TRACKING) and to_s == IDLE:
                    if latest is None or ts > latest:
                        latest = ts
        t_end = latest
        if t_end:
            print(f"Auto-detected tracking end: {t_end}")

    if idle_end:
        parts = idle_end.split('+')
        h, m = map(int, parts[0].split(':'))
        day_offset = int(parts[1].replace('d', '')) if len(parts) > 1 else 0
        t_idle_end = datetime.combine(base_date + timedelta(days=day_offset),
                                       datetime.min.time().replace(hour=h, minute=m))
    else:
        # Use last entry per tracker
        t_idle_end = None
        for tid in tracker_ids:
            last_ts = tracker_data[tid][-1][0]
            if t_idle_end is None or last_ts > t_idle_end:
                t_idle_end = last_ts
        print(f"Auto-detected idle end: {t_idle_end}")

    if not t_start or not t_end:
        print("Could not detect tracking window!")
        return

    # Find battery at key timestamps for each tracker
    # We find the closest reading at or just after the target time
    def find_battery_at(entries, target_ts, direction='after'):
        """Find battery reading closest to target_ts."""
        if direction == 'after':
            for ts, state, bat in entries:
                if ts >= target_ts:
                    return ts, state, bat
        elif direction == 'before':
            result = None
            for ts, state, bat in entries:
                if ts <= target_ts:
                    result = (ts, state, bat)
                else:
                    break
            return result
        return None

    def find_state_at(entries, target_ts):
        """Find state at a given time (last state at or before target)."""
        result_state = None
        for ts, state, bat in entries:
            if ts <= target_ts:
                result_state = state
            else:
                break
        return result_state

    # Count GPS-wait vs tracking time per tracker during tracking window
    def count_gps_states(entries, t_start, t_end):
        """Count seconds in each state during the tracking window."""
        state_time = {IDLE: 0, GPS_WAIT: 0, TRACKING: 0}
        prev_ts = None
        prev_state = None
        for ts, state, bat in entries:
            if ts < t_start:
                prev_state = state
                prev_ts = ts
                continue
            if ts > t_end:
                if prev_state and prev_ts:
                    dt = (t_end - max(prev_ts, t_start)).total_seconds()
                    if dt > 0:
                        state_time[prev_state] += dt
                break
            if prev_state and prev_ts:
                dt = (ts - max(prev_ts, t_start)).total_seconds()
                if dt > 0:
                    state_time[prev_state] += dt
            prev_ts = ts
            prev_state = state
        else:
            # Reached end of entries before t_end
            if prev_state and prev_ts and prev_ts <= t_end:
                dt = (t_end - max(prev_ts, t_start)).total_seconds()
                if dt > 0:
                    state_time[prev_state] += dt
        return state_time

    tracking_secs = (t_end - t_start).total_seconds()
    tracking_duration = tracking_secs / 3600
    idle_secs = (t_idle_end - t_end).total_seconds()
    idle_duration = idle_secs / 3600

    def fmt_duration(total_secs):
        """Format seconds as Xh Ym."""
        total_mins = int(total_secs) // 60
        h, m = divmod(total_mins, 60)
        return f"{h}h {m:02d}m"

    # --- Tracking Battery Drain ---
    print("\n" + "=" * 80)
    print(f"TRACKING BATTERY DRAIN ({t_start.strftime('%Y-%m-%d')})")
    print(f"Duration: {fmt_duration(tracking_secs)} "
          f"({t_start.strftime('%H:%M')} → {t_end.strftime('%H:%M')})")
    print("=" * 80)
    print(f"{'Tracker':<12} {'Start':>5} {'End':>5} {'Drain':>5} {'Rate':>9} {'Track Life':>10}  Notes")
    print(f"{'-'*12} {'-'*5} {'-'*5} {'-'*5} {'-'*9} {'-'*10}  {'-'*30}")

    tracking_results = []
    for tid in tracker_ids:
        entries = tracker_data[tid]
        start_reading = find_battery_at(entries, t_start, 'after')
        end_reading = find_battery_at(entries, t_end, 'before')

        if not start_reading or not end_reading:
            continue

        start_bat = start_reading[2]
        end_bat = end_reading[2]
        drain = start_bat - end_bat

        # State breakdown during tracking
        state_time = count_gps_states(entries, t_start, t_end)
        total_tracking_secs = state_time[TRACKING] + state_time[GPS_WAIT] + state_time[IDLE]

        notes = []

        # Check GPS-wait time
        gps_wait_pct = state_time[GPS_WAIT] / total_tracking_secs * 100 if total_tracking_secs > 0 else 0
        if gps_wait_pct > 5:
            gps_wait_mins = state_time[GPS_WAIT] / 60
            notes.append(f"GPS-wait {gps_wait_mins:.0f}m ({gps_wait_pct:.0f}%)")

        # Check idle time during tracking window
        idle_pct = state_time[IDLE] / total_tracking_secs * 100 if total_tracking_secs > 0 else 0
        if idle_pct > 5:
            idle_mins = state_time[IDLE] / 60
            notes.append(f"idle {idle_mins:.0f}m ({idle_pct:.0f}%)")

        if drain > 0:
            rate = drain / tracking_duration
            life = 100 / rate
        else:
            rate = 0
            life = float('inf')

        note_str = "; ".join(notes)
        life_str = f"{life:.0f}h" if life < 1000 else ">999h"
        print(f"{tid:<12} {start_bat:>4}% {end_bat:>4}% {drain:>4}% {rate:>7.1f}%/hr {life_str:>10}  {note_str}")
        tracking_results.append((tid, start_bat, end_bat, drain, rate, life, note_str))

    # --- Idle Battery Drain ---
    print(f"\n{'=' * 80}")
    print(f"IDLE BATTERY DRAIN ({t_end.strftime('%Y-%m-%d')})")
    idle_end_str = t_idle_end.strftime('%H:%M')
    if t_idle_end.date() != t_end.date():
        days = (t_idle_end.date() - t_end.date()).days
        idle_end_str += f"+{days}d"
    print(f"Duration: {fmt_duration(idle_secs)} "
          f"({t_end.strftime('%H:%M')} → {idle_end_str})")
    print("=" * 80)
    print(f"{'Tracker':<12} {'Start':>5} {'End':>5} {'Drain':>5} {'Rate':>9} {'Idle Life':>10}")
    print(f"{'-'*12} {'-'*5} {'-'*5} {'-'*5} {'-'*9} {'-'*10}")

    for tid in tracker_ids:
        entries = tracker_data[tid]
        start_reading = find_battery_at(entries, t_end, 'after')
        end_reading = find_battery_at(entries, t_idle_end, 'before')

        if not start_reading or not end_reading:
            continue

        start_bat = start_reading[2]
        end_bat = end_reading[2]
        drain = start_bat - end_bat

        if drain > 0:
            rate = drain / idle_duration
            life = 100 / rate
        else:
            rate = 0
            life = float('inf')

        life_str = f"{life:.0f}h" if life < 1000 else ">999h"
        print(f"{tid:<12} {start_bat:>4}% {end_bat:>4}% {drain:>4}% {rate:>7.1f}%/hr {life_str:>10}")

    # --- GPS state breakdown ---
    print(f"\n{'=' * 80}")
    print(f"GPS STATE BREAKDOWN DURING TRACKING")
    print("=" * 80)
    print(f"{'Tracker':<12} {'Tracking':>10} {'GPS-wait':>10} {'Idle':>10}")
    print(f"{'-'*12} {'-'*10} {'-'*10} {'-'*10}")
    for tid in tracker_ids:
        entries = tracker_data[tid]
        state_time = count_gps_states(entries, t_start, t_end)
        total = sum(state_time.values())
        if total == 0:
            continue
        t_pct = state_time[TRACKING] / total * 100
        g_pct = state_time[GPS_WAIT] / total * 100
        i_pct = state_time[IDLE] / total * 100
        parts = []
        for label, pct in [(TRACKING, t_pct), (GPS_WAIT, g_pct), (IDLE, i_pct)]:
            parts.append(f"{pct:>8.1f}%")
        print(f"{tid:<12} {''.join(parts)}")


def main():
    parser = argparse.ArgumentParser(description='Analyze battery test log')
    parser.add_argument('logfile', help='Path to battery test log file')
    parser.add_argument('--trackers', help='Comma-separated tracker IDs to analyze (default: Tracker1-14)',
                        default=None)
    parser.add_argument('--tracking-start', help='Tracking start time HH:MM (auto-detected if omitted)',
                        default=None)
    parser.add_argument('--tracking-end', help='Tracking end time HH:MM (auto-detected if omitted)',
                        default=None)
    parser.add_argument('--idle-end', help='Idle end time HH:MM or HH:MM+1d (auto-detected if omitted)',
                        default=None)
    args = parser.parse_args()

    if args.trackers:
        tracker_filter = set(args.trackers.split(','))
    else:
        tracker_filter = {f'Tracker{i}' for i in range(1, 15)}

    analyze_log(args.logfile, tracker_filter,
                args.tracking_start, args.tracking_end, args.idle_end)


if __name__ == '__main__':
    main()
