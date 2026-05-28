#!/usr/bin/env python3
"""Analyse overnight GT06 tracker behaviour from a tracker.log file.

Reports per-tracker contact patterns over a time window and flags
deviations from the expected cadence:

  - silent-too-long: max gap > expected_max_gap (mode-specific)
  - thrashing: server pushed the same setup-burst command (TIMER,540
    etc.) more than 6 times in any hour — runaway battery burn
  - mode-drift: device's reported M: changed during the window
    without an operator /admin/start /admin/stop event

Expected cadences:
  MODE1 race-day idle: HBT every ~15s, expect a connection at least
                       every few minutes
  MODE4 overnight   : login every overnight_interval_min minutes
                       (default 15), cxzt# only per wake
  MODE5 overnight   : same as MODE4 cadence-wise

Usage:
  python scripts/overnight_analysis.py \\
    --log /path/to/tracker.log \\
    --positions tracker/html/8/current_positions.json \\
    --from "2026-05-28 16:00" --to "2026-05-29 09:00" \\
    [--tracker G378848]
"""

import argparse
import json
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Log patterns
# ---------------------------------------------------------------------------

TS = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})'

# Per-line event patterns. Each maps to a `kind` we record in the timeline.
PATTERNS = [
    ("login",      re.compile(rf'^{TS} \[GT06\] Login: IMEI (\d+) -> (G\d+) \(eid=(\d+)\)')),
    ("disconnect", re.compile(rf'^{TS} \[GT06\] Disconnected: (G\d+) \(')),
    ("hbt",        re.compile(rf'^{TS} \[GT06\] Heartbeat (G\d+):')),
    ("idle_hb",    re.compile(rf'^{TS} \[(G\d+)\] Idle heartbeat ')),
    ("gpswait_hb", re.compile(rf'^{TS} \[(G\d+)\] GPS-wait heartbeat ')),
    ("pos",        re.compile(rf'^{TS} \[(G\d+)\] pos=')),
    ("cmd_sent",   re.compile(rf'^{TS} \[GT06\] Sent to (G\d+): (\S+)')),
    ("cmd_ack",    re.compile(rf'^{TS} \[GT06\] Command ACK from (G\d+): (.*)')),
    ("tcp_timeout",re.compile(rf'^{TS} \[GT06\] TCP delivery timeout for (G\d+):')),
    ("rate_mm",    re.compile(rf'^{TS} \[GT06\] Rate mismatch for (G\d+):')),
    ("mode_push",  re.compile(rf'^{TS} \[GT06\] (G\d+) reports MODE=(\d+), desired MODE=(\d+)')),
    ("op_start",   re.compile(rf'^{TS} \[GT06\] Active mode (?:for|queued for) (G\d+)')),
    ("op_stop",    re.compile(rf'^{TS} \[GT06\] (?:Idle|Overnight idle) mode (?:for|queued for) (G\d+)')),
    ("admin_evt",  re.compile(rf'^{TS} \[EVENT \d+\] Remote (sleep|stop|start)(?:-all)?.*?(G\d+)?')),
]

# cxzt# rich response — `M:n*F:m` parsable via subpatterns of cmd_ack text.
CXZT_M = re.compile(r'\*M:(\d+)')
CXZT_F = re.compile(r'\*F:(\d+)')

# Setup-burst commands the server cycles during race-day idle. More than
# a handful per hour means the rate-mismatch loop is running unbounded.
RACE_DAY_CMDS = {"TIMER,540,540#", "TIMER,15,15#", "SZCS#SLPDISCONNECT=0",
                 "SENDS,1#", "SENALM,OFF#", "MOVING,OFF#",
                 "SZCS#GPS_RST_TIME=0", "SZCS#VIBCHK=0:16",
                 "HBT,540,540#", "HBT,15,15#"}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

def parse_ts(s):
    return datetime.fromisoformat(s.replace(" ", "T"))


class TrackerLog:
    """Per-tracker event timeline + computed state."""

    def __init__(self, sailor_id):
        self.sailor_id = sailor_id
        self.events = []  # list of (ts, kind, *args)
        self.imei = None
        self.cxzt_history = []  # list of (ts, mode, freq)
        self.cmd_send_count = defaultdict(int)  # cmd -> total sends in window
        self.operator_actions = []  # (ts, action) — start/stop/sleep
        self.last_login_eid = None

    def add(self, ts_dt, kind, *args):
        self.events.append((ts_dt, kind, *args))

    @property
    def first_ts(self):
        return self.events[0][0] if self.events else None

    @property
    def last_ts(self):
        return self.events[-1][0] if self.events else None

    def gap_distribution(self):
        """Return seconds-between-adjacent-events stats."""
        if len(self.events) < 2:
            return None
        gaps = []
        for i in range(1, len(self.events)):
            dt = (self.events[i][0] - self.events[i-1][0]).total_seconds()
            if dt >= 0:
                gaps.append(dt)
        if not gaps:
            return None
        return {
            "count": len(gaps),
            "min": min(gaps),
            "p50": statistics.median(gaps),
            "p95": sorted(gaps)[int(len(gaps) * 0.95)] if len(gaps) > 1 else gaps[0],
            "max": max(gaps),
            "mean": statistics.mean(gaps),
        }


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_log(path, t_from=None, t_to=None, tracker_filter=None):
    """Read the tracker.log, return {sailor_id: TrackerLog}."""
    trackers = {}
    with open(path, "rb") as f:
        for raw in f:
            try:
                line = raw.decode("utf-8", errors="replace")
            except Exception:
                continue
            for kind, pat in PATTERNS:
                m = pat.match(line)
                if not m:
                    continue
                ts = parse_ts(m.group(1))
                if t_from and ts < t_from:
                    break
                if t_to and ts > t_to:
                    return trackers  # log is chronological, we're done
                if kind == "login":
                    imei, sid, eid = m.group(2), m.group(3), int(m.group(4))
                    if tracker_filter and sid != tracker_filter:
                        break
                    t = trackers.setdefault(sid, TrackerLog(sid))
                    t.imei = imei
                    t.last_login_eid = eid
                    t.add(ts, "login", imei, eid)
                elif kind == "cmd_ack":
                    sid, text = m.group(2), m.group(3)
                    if tracker_filter and sid != tracker_filter:
                        break
                    t = trackers.setdefault(sid, TrackerLog(sid))
                    t.add(ts, "cmd_ack", text)
                    # Detect cxzt# response by the presence of M: and F:
                    mm = CXZT_M.search(text)
                    fm = CXZT_F.search(text)
                    if mm and fm:
                        t.cxzt_history.append((ts, int(mm.group(1)), int(fm.group(1))))
                elif kind == "cmd_sent":
                    sid, cmd = m.group(2), m.group(3)
                    if tracker_filter and sid != tracker_filter:
                        break
                    t = trackers.setdefault(sid, TrackerLog(sid))
                    t.add(ts, "cmd_sent", cmd)
                    t.cmd_send_count[cmd] += 1
                elif kind == "mode_push":
                    sid, m_reported, m_desired = m.group(2), int(m.group(3)), int(m.group(4))
                    if tracker_filter and sid != tracker_filter:
                        break
                    t = trackers.setdefault(sid, TrackerLog(sid))
                    t.add(ts, "mode_push", m_reported, m_desired)
                elif kind in ("op_start", "op_stop"):
                    sid = m.group(2)
                    if tracker_filter and sid != tracker_filter:
                        break
                    t = trackers.setdefault(sid, TrackerLog(sid))
                    t.add(ts, kind)
                    t.operator_actions.append((ts, kind))
                elif kind == "admin_evt":
                    action = m.group(2)
                    sid = m.group(3)
                    if not sid:
                        continue
                    if tracker_filter and sid != tracker_filter:
                        break
                    t = trackers.setdefault(sid, TrackerLog(sid))
                    t.add(ts, "admin_evt", action)
                    t.operator_actions.append((ts, f"admin/{action}"))
                else:
                    # disconnect / hbt / idle_hb / pos / tcp_timeout / rate_mm
                    sid = m.group(2)
                    if tracker_filter and sid != tracker_filter:
                        break
                    t = trackers.setdefault(sid, TrackerLog(sid))
                    t.add(ts, kind)
                break
    return trackers


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def expected_max_gap_seconds(mode):
    """Liberal upper bound on time-between-events at the given mode.
    Used to flag silent-too-long. 3× the nominal cadence is the wiggle
    room — anything worse is real."""
    if mode in (4, 5):
        # Overnight: 15-min wake cycle. Allow up to 45 min before flagging.
        return 45 * 60
    if mode == 1:
        # Race-day idle: TIMER 540 + HBT 540 = expect contact every ~9 min.
        # Allow 30 min before flagging.
        return 30 * 60
    return 30 * 60  # unknown — same conservative bound


def analyse(tracker, positions_state=None):
    """Return a verdict dict for one tracker."""
    findings = []
    final_cxzt = tracker.cxzt_history[-1] if tracker.cxzt_history else None
    initial_cxzt = tracker.cxzt_history[0] if tracker.cxzt_history else None

    # --- mode-drift detection ---
    if initial_cxzt and final_cxzt and initial_cxzt[1] != final_cxzt[1]:
        # cxzt# M: changed during the window. Cross-reference operator actions.
        op_set = {a[1] for a in tracker.operator_actions}
        explained = bool(op_set)  # any operator action is enough to explain
        if not explained:
            findings.append(
                f"mode-drift: {initial_cxzt[1]} → {final_cxzt[1]} at "
                f"{final_cxzt[0].isoformat()} with no operator action")

    # --- silent-too-long detection ---
    mode_for_threshold = final_cxzt[1] if final_cxzt else None
    threshold_s = expected_max_gap_seconds(mode_for_threshold)
    gaps_over = []
    if len(tracker.events) >= 2:
        for i in range(1, len(tracker.events)):
            dt = (tracker.events[i][0] - tracker.events[i-1][0]).total_seconds()
            if dt > threshold_s:
                gaps_over.append((tracker.events[i-1][0].isoformat(),
                                  tracker.events[i][0].isoformat(),
                                  int(dt)))
    if gaps_over:
        worst = max(gaps_over, key=lambda g: g[2])
        findings.append(
            f"silent: {len(gaps_over)} gap(s) > {threshold_s}s "
            f"(worst {worst[2]}s between {worst[0]} and {worst[1]})")

    # --- thrashing detection ---
    # Count setup-burst commands sent per hour over the window. Race-day
    # idle legitimately triggers ~6-7 commands/hour via the rate-mismatch
    # retry path; truly broken trackers hit 30+/hour. Threshold at 20 to
    # only flag real runaway loops.
    THRASH_PER_HOUR = 20
    if tracker.events:
        window_s = (tracker.last_ts - tracker.first_ts).total_seconds()
        window_h = max(0.01, window_s / 3600.0)
        for cmd, n in tracker.cmd_send_count.items():
            if cmd in RACE_DAY_CMDS and n / window_h > THRASH_PER_HOUR:
                findings.append(
                    f"thrashing: {cmd} sent {n} times in {window_h:.1f}h "
                    f"(~{n/window_h:.1f}/hour)")
                break  # one is enough; the others will be similar

    # --- expected vs actual flag mismatch ---
    if positions_state is not None:
        intended_sleep = bool(positions_state.get("sleep"))
        observed_mode = final_cxzt[1] if final_cxzt else None
        if intended_sleep and observed_mode not in (4, 5):
            findings.append(
                f"intent mismatch: positions.json says sleep=True but device "
                f"is in M:{observed_mode}")
        elif not intended_sleep and observed_mode in (4, 5):
            findings.append(
                f"intent mismatch: device in M:{observed_mode} but "
                f"positions.json has no sleep flag")

    verdict = "OK" if not findings else \
        ("thrashing" if any("thrashing" in f for f in findings)
         else "mode-drift" if any("mode-drift" in f for f in findings)
         else "silent" if any("silent" in f for f in findings)
         else "intent-mismatch")

    return {
        "sailor_id": tracker.sailor_id,
        "imei": tracker.imei,
        "events": len(tracker.events),
        "first_ts": tracker.first_ts.isoformat() if tracker.first_ts else None,
        "last_ts": tracker.last_ts.isoformat() if tracker.last_ts else None,
        "initial_mode": initial_cxzt[1] if initial_cxzt else None,
        "initial_freq": initial_cxzt[2] if initial_cxzt else None,
        "final_mode": final_cxzt[1] if final_cxzt else None,
        "final_freq": final_cxzt[2] if final_cxzt else None,
        "operator_actions": len(tracker.operator_actions),
        "gaps": tracker.gap_distribution(),
        "race_cmd_sends": {k: v for k, v in tracker.cmd_send_count.items()
                           if k in RACE_DAY_CMDS and v >= 3},
        "verdict": verdict,
        "findings": findings,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _fmt_summary(report):
    """Plain-text summary table."""
    lines = []
    lines.append(f"{'Sailor':10} {'Events':>7} {'M:i→f':12} {'F:i→f':12} {'Ops':>4} "
                 f"{'p50/max gap':>20} {'Verdict':14} Findings")
    lines.append("-" * 110)
    for r in sorted(report, key=lambda x: (x["verdict"] != "OK", x["sailor_id"])):
        mi = r["initial_mode"]
        mf = r["final_mode"]
        fi = r["initial_freq"]
        ff = r["final_freq"]
        mode_str = f"{mi}→{mf}" if mi != mf else f"{mi or '?'}"
        freq_str = f"{fi}→{ff}" if fi != ff else f"{fi or '?'}"
        gaps = r.get("gaps") or {}
        gap_str = f"{gaps.get('p50',0):.0f}/{gaps.get('max',0):.0f}s" if gaps else "?"
        findings_str = "; ".join(r["findings"])[:60]
        lines.append(f"{r['sailor_id']:10} {r['events']:>7} {mode_str:12} "
                     f"{freq_str:12} {r['operator_actions']:>4} {gap_str:>20} "
                     f"{r['verdict']:14} {findings_str}")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--log", required=True, help="path to tracker.log")
    p.add_argument("--positions", help="path to current_positions.json for sleep/idle intent")
    p.add_argument("--from", dest="t_from", help='"YYYY-MM-DD HH:MM" lower bound')
    p.add_argument("--to", dest="t_to", help='"YYYY-MM-DD HH:MM" upper bound')
    p.add_argument("--tracker", help="restrict to one sailor_id (e.g. G378848)")
    p.add_argument("--json", action="store_true", help="emit JSON to stdout instead of text")
    args = p.parse_args()

    t_from = parse_ts(args.t_from + ":00") if args.t_from else None
    t_to = parse_ts(args.t_to + ":00") if args.t_to else None

    trackers = parse_log(args.log, t_from=t_from, t_to=t_to,
                         tracker_filter=args.tracker)

    positions = {}
    if args.positions:
        try:
            data = json.loads(Path(args.positions).read_text())
            positions = data.get("sailors", data)
        except Exception as e:
            print(f"warning: failed to read {args.positions}: {e}", file=sys.stderr)

    report = []
    for sid, tracker in sorted(trackers.items()):
        report.append(analyse(tracker, positions_state=positions.get(sid)))

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(_fmt_summary(report))
        # Per-tracker detail for anything non-OK.
        for r in report:
            if r["verdict"] != "OK":
                print(f"\n--- {r['sailor_id']} ({r['verdict']}) ---")
                for f in r["findings"]:
                    print(f"  • {f}")
                if r.get("race_cmd_sends"):
                    print(f"  race-cmd sends in window: {r['race_cmd_sends']}")


if __name__ == "__main__":
    main()
