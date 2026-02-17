#!/usr/bin/env python3
"""Analyze tracker.log to estimate battery life per tracker.

Usage: battery_analysis.py <logfile> [pattern]
  logfile   - path to tracker.log
  pattern   - fnmatch wildcard for tracker IDs (default: "*")

Parses log lines with bat=NN% (discharging) or bat=NN%+ (charging).
Charging periods are excluded from drain calculations.
"""

import argparse
import re
import sys
from datetime import datetime
from fnmatch import fnmatch

# Match lines with [TrackerID] and bat=NN%  or bat=NN%+
LINE_RE = re.compile(
    r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) '
    r'\[([^\]]+)\] '
    r'(.+?)'
    r'bat=(\d+)%(\+?)'
)


def natural_sort_key(s):
    """Sort key that handles embedded numbers naturally."""
    parts = re.split(r'(\d+)', s)
    return [int(p) if p.isdigit() else p.lower() for p in parts]


def classify_state(text):
    """Determine state from the log line text between tracker ID and bat=."""
    if 'Idle heartbeat' in text:
        return 'idle'
    if 'GPS-wait heartbeat' in text or 'pos=' in text:
        return 'tracking'
    return None


def parse_log(logfile, pattern):
    """Parse log file into per-tracker data point lists."""
    trackers = {}  # tracker_id -> [(timestamp, battery%, charging, state), ...]
    with open(logfile) as f:
        for line in f:
            m = LINE_RE.match(line)
            if not m:
                continue
            ts_str, tracker_id, mid_text, bat_str, chg_str = m.groups()
            if not fnmatch(tracker_id, pattern):
                continue
            state = classify_state(mid_text)
            if state is None:
                continue
            ts = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
            bat = int(bat_str)
            charging = chg_str == '+'
            trackers.setdefault(tracker_id, []).append((ts, bat, charging, state))
    return trackers


def build_segments(data_points):
    """Build contiguous drain segments from chronological data points.

    A segment breaks on:
    - State change (idle -> tracking or vice versa)
    - Charging detected (bat=NN%+ in log)
    - Battery increase without charging flag (legacy logs without +)

    Returns dict: {'idle': [segments], 'tracking': [segments]}
    where each segment is (first_ts, first_bat, last_ts, last_bat).
    """
    segments = {'idle': [], 'tracking': []}
    if not data_points:
        return segments

    seg_start = None  # (ts, bat)
    seg_state = None
    prev_ts, prev_bat = None, None

    for ts, bat, charging, state in data_points:
        # Detect charging: explicit flag or battery increase (fallback for old logs)
        is_charging = charging or (prev_bat is not None and bat > prev_bat)

        if is_charging or (seg_state is not None and state != seg_state):
            # Save previous segment if we have one
            if seg_start is not None and prev_ts is not None:
                segments[seg_state].append((*seg_start, prev_ts, prev_bat))
            seg_start = None
            seg_state = None
            if is_charging:
                prev_ts, prev_bat = ts, bat
                continue

        # Not charging
        if seg_start is None:
            seg_start = (ts, bat)
            seg_state = state
        prev_ts, prev_bat = ts, bat

    # Final segment
    if seg_start is not None and prev_ts is not None:
        segments[seg_state].append((*seg_start, prev_ts, prev_bat))

    return segments


def compute_estimate(segments, min_drain=1, min_minutes=10):
    """Compute weighted average drain rate from segments.

    Only uses segments with >= min_drain% drop and >= min_minutes duration.
    Returns (estimated_hours_full_battery, rate_pct_per_hr, observation_hours) or None.
    """
    total_weight = 0.0
    weighted_rate = 0.0

    for start_ts, start_bat, end_ts, end_bat in segments:
        drain = start_bat - end_bat
        hours = (end_ts - start_ts).total_seconds() / 3600.0
        if drain < min_drain or hours < min_minutes / 60.0:
            continue
        rate = drain / hours
        total_weight += hours
        weighted_rate += rate * hours

    if total_weight == 0:
        return None

    avg_rate = weighted_rate / total_weight
    if avg_rate <= 0:
        return None
    est_hours = 100.0 / avg_rate
    return (est_hours, avg_rate, total_weight)


def format_cell(estimate):
    """Format estimate as (life, rate, obs) strings or N/A."""
    if estimate is None:
        return 'N/A', 'N/A', 'N/A'
    est_hours, rate, obs_hours = estimate
    return f'{est_hours:.0f}h', f'{rate:.1f}%/hr', f'{obs_hours:.1f}h'


def main():
    parser = argparse.ArgumentParser(description='Analyze tracker battery life from log files')
    parser.add_argument('logfile', help='Path to tracker.log')
    parser.add_argument('pattern', nargs='?', default='*', help='fnmatch pattern for tracker IDs (default: *)')
    args = parser.parse_args()

    trackers = parse_log(args.logfile, args.pattern)
    if not trackers:
        print(f'No matching trackers found for pattern "{args.pattern}"')
        sys.exit(1)

    # Compute estimates per tracker
    results = []
    for tid in sorted(trackers.keys(), key=natural_sort_key):
        segments = build_segments(trackers[tid])
        idle_est = compute_estimate(segments['idle'])
        track_est = compute_estimate(segments['tracking'])
        last_bat = trackers[tid][-1][1]
        last_chg = trackers[tid][-1][2]
        results.append((tid, last_bat, last_chg, idle_est, track_est))

    # Find max tracker name width
    name_w = max(len(r[0]) for r in results)
    name_w = max(name_w, len('Tracker'))

    print(f'{"Tracker":<{name_w}}  Bat%  {"Idle Life":>9} {"Rate":>8} {"Obs":>5}  {"Track Life":>10} {"Rate":>8} {"Obs":>5}')
    print(f'{"":-<{name_w}}  ----  {"":-<9} {"":-<8} {"":-<5}  {"":-<10} {"":-<8} {"":-<5}')

    for tid, last_bat, last_chg, idle_est, track_est in results:
        idle_life, idle_rate, idle_obs = format_cell(idle_est)
        track_life, track_rate, track_obs = format_cell(track_est)
        bat_str = f'{last_bat}%'
        if last_chg:
            bat_str += '+'
        print(f'{tid:<{name_w}}  {bat_str:>5}  {idle_life:>9} {idle_rate:>8} {idle_obs:>5}  {track_life:>10} {track_rate:>8} {track_obs:>5}')

    print()
    print('Life = estimated hours from 100% to 0% (extrapolated from observed drain)')
    print('Obs = hours of observation used for the estimate')
    print('+ after Bat% = currently charging')


if __name__ == '__main__':
    main()
