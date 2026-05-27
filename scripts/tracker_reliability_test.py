#!/usr/bin/env python3
"""Long-term reliability test for GT06 trackers.

Drives event-scope state transitions on a schedule (active ↔ idle) and
verifies every tracker behaves as expected at each phase boundary. Per-
tracker state is rebuilt from tracker.log at startup so the test is fully
resumable — you can Ctrl-C and re-run hours later and pick up exactly
where you left off.

Usage:
    scripts/tracker_reliability_test.py --duration 24h
    scripts/tracker_reliability_test.py --plan custom_plan.json --state /tmp/state.json

Inputs:
    --state <file>      Persistent state JSON (default tracker_test_state.json
                        in CWD). On startup, rebuilt from logs; on shutdown,
                        re-saved.
    --plan  <file>      Phase plan JSON (see DEFAULT_PHASES below).
    --duration <T>      Stop after T total runtime (e.g. 24h, 90m, 3600s).
                        Default: run forever until interrupted.
    --event  <eid>      Event ID to drive (default 8).
    --server <host>     ssh hostname for the tracker server (default wstracker).
    --no-act            Read-only mode — don't issue any admin commands, just
                        track state and report. Useful when running alongside
                        a real race.

How state is derived
--------------------
Each tracker has an observed_state ∈ {unknown, disconnected, idle, active}.
We parse tracker.log events:
    "Login: IMEI ... -> Gxxxxxx (eid=8)"        → resets login info
    "Login commands queued (active|idle)"        → sets state
    "Disconnected: Gxxxxxx (..."                 → disconnected
    "Heartbeat Gxxxxxx: ..."                     → updates last_seen
    "Gxxxxxx firmware: ..."                      → updates last_seen + records fw
    "Gxxxxxx reports MODE=N, switching to MODE1" → records in_mode1=False
    "Idle heartbeat ..." / "GPS-wait heartbeat ..." → idle/active confirmation

A phase boundary action (e.g. switch to "tracking"):
  1. POST /api/event/{eid}/admin/state to set event_state
  2. Wait `transition_window` seconds
  3. For each tracker, check observed_state matches expected. If not, record
     a failure (timestamp, expected, observed, tracker).
  4. Stay in this phase for the configured duration, periodically rechecking.

Failures are accumulated per-tracker and printed at every phase boundary.
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path


# ----------------------------------------------------------------------------
# Configuration & default plan
# ----------------------------------------------------------------------------

DEFAULT_STATE_FILE = "tracker_test_state.json"
DEFAULT_EVENT_ID = 8
DEFAULT_SERVER = "wstracker"
DEFAULT_HTTP_URL = "http://wstracker.org:41234"
ADMIN_PW_FILE = "/tmp/.ev8pw"
REMOTE_LOG_PATH = "tracker/tracker.log"

# Default phase cycle (loops forever). Each phase: set event_state, run for
# `duration_s` seconds, then advance to next phase. The list is cyclic.
DEFAULT_PHASES = [
    # Race-day cycle: 30 min racing, 10 min between heats, repeat 4 times
    {"name": "heat1",   "state": "tracking", "duration_s": 1800},
    {"name": "break1",  "state": "idle",     "duration_s":  600},
    {"name": "heat2",   "state": "tracking", "duration_s": 1800},
    {"name": "break2",  "state": "idle",     "duration_s":  600},
    {"name": "heat3",   "state": "tracking", "duration_s": 1800},
    {"name": "break3",  "state": "idle",     "duration_s":  600},
    {"name": "heat4",   "state": "tracking", "duration_s": 1800},
    {"name": "evening", "state": "idle",     "duration_s": 3600},
    # Overnight is just a long idle for now (overnight-specific commands TBD)
    {"name": "night1",  "state": "idle",     "duration_s": 7200},
    {"name": "night2",  "state": "idle",     "duration_s": 7200},
    {"name": "night3",  "state": "idle",     "duration_s": 7200},
]

# How long to give every tracker to transition after a phase change.
TRANSITION_WINDOW_S = 180

# A tracker is considered "alive" if last_seen is within this many seconds.
# Tuned per active vs idle expectations.
ALIVE_THRESHOLDS = {
    "active": 75,    # active mode HBT=15s, max no-traffic = 75s
    "idle":   930,   # idle HBT=540 (race-day idle), max 540*3+30 = 1650s; pick a tighter check
}


# ----------------------------------------------------------------------------
# Tracker state
# ----------------------------------------------------------------------------

class TrackerState:
    """Per-tracker observed state, rebuilt from log."""

    def __init__(self, sailor_id):
        self.sailor_id = sailor_id
        self.imei = None
        self.firmware = None
        self.observed_state = "unknown"  # unknown | disconnected | idle | active
        self.last_seen = None            # iso str — any traffic
        self.last_login = None
        self.last_disconnect = None
        self.in_mode1 = None             # True | False | None=unknown
        self.connect_count = 0
        self.disconnect_count = 0
        self.failures = []               # [(iso_ts, reason)]

    def to_dict(self):
        return {
            "sailor_id": self.sailor_id,
            "imei": self.imei,
            "firmware": self.firmware,
            "observed_state": self.observed_state,
            "last_seen": self.last_seen,
            "last_login": self.last_login,
            "last_disconnect": self.last_disconnect,
            "in_mode1": self.in_mode1,
            "connect_count": self.connect_count,
            "disconnect_count": self.disconnect_count,
            "failures": self.failures[-100:],
        }

    @classmethod
    def from_dict(cls, d):
        t = cls(d["sailor_id"])
        for k in ("imei", "firmware", "observed_state", "last_seen",
                  "last_login", "last_disconnect", "in_mode1",
                  "connect_count", "disconnect_count"):
            if k in d:
                setattr(t, k, d[k])
        t.failures = list(d.get("failures", []))
        return t

    def record_failure(self, ts, reason):
        self.failures.append([ts, reason])


# ----------------------------------------------------------------------------
# Log parsing
# ----------------------------------------------------------------------------

# Patterns we care about (compiled once)
_RE_LOGIN = re.compile(
    r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[GT06\] Login: IMEI (\d+) -> (G\d+) \(eid=(\d+)\)')
_RE_LOGIN_QUEUED = re.compile(
    r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[GT06\] Login commands queued \((active|idle)\)')
_RE_DISCONNECTED = re.compile(
    r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[GT06\] Disconnected: (G\d+) \(')
_RE_HEARTBEAT = re.compile(
    r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[GT06\] Heartbeat (G\d+):')
_RE_IDLE_HB = re.compile(
    r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(G\d+)\] Idle heartbeat ')
_RE_GPSWAIT_HB = re.compile(
    r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(G\d+)\] GPS-wait heartbeat ')
_RE_POS = re.compile(
    r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(G\d+)\] pos=')
_RE_FIRMWARE = re.compile(
    r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[GT06\] (G\d+) firmware: (\S+)')
_RE_MODE_SWITCH = re.compile(
    r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[GT06\] (G\d+) reports MODE=(\d+), switching to MODE1')
_RE_NO_TRAFFIC = re.compile(
    r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[GT06\] No (?:heartbeat|traffic) from (G\d+) for (\d+)s')


def process_log_line(line, trackers, last_login_id=[None]):
    """Update trackers dict in place from one log line. Returns sailor_id if relevant."""
    line = line.rstrip("\n")
    if not line:
        return None

    m = _RE_LOGIN.match(line)
    if m:
        ts, imei, sid, eid = m.group(1), m.group(2), m.group(3), m.group(4)
        t = trackers.setdefault(sid, TrackerState(sid))
        t.imei = imei
        t.last_login = ts
        t.last_seen = ts
        t.connect_count += 1
        last_login_id[0] = sid
        return sid

    m = _RE_LOGIN_QUEUED.match(line)
    if m:
        ts, kind = m.group(1), m.group(2)
        sid = last_login_id[0]
        if sid:
            t = trackers.setdefault(sid, TrackerState(sid))
            t.observed_state = kind
            t.last_seen = ts
        return sid

    m = _RE_DISCONNECTED.match(line)
    if m:
        ts, sid = m.group(1), m.group(2)
        t = trackers.setdefault(sid, TrackerState(sid))
        t.observed_state = "disconnected"
        t.last_disconnect = ts
        return sid

    m = _RE_HEARTBEAT.match(line) or _RE_IDLE_HB.match(line) \
        or _RE_GPSWAIT_HB.match(line) or _RE_POS.match(line)
    if m:
        ts, sid = m.group(1), m.group(2)
        t = trackers.setdefault(sid, TrackerState(sid))
        t.last_seen = ts
        return sid

    m = _RE_FIRMWARE.match(line)
    if m:
        ts, sid, fw = m.group(1), m.group(2), m.group(3)
        t = trackers.setdefault(sid, TrackerState(sid))
        t.firmware = fw
        t.last_seen = ts
        # Firmware string comes from cxzt# response — if we got here without a
        # "switching to MODE1" log line, the device was already in M=1.
        if t.in_mode1 is None:
            t.in_mode1 = True
        return sid

    m = _RE_MODE_SWITCH.match(line)
    if m:
        ts, sid, mode = m.group(1), m.group(2), m.group(3)
        t = trackers.setdefault(sid, TrackerState(sid))
        t.in_mode1 = False
        t.record_failure(ts, f"non-MODE1 (M={mode}) detected, server auto-switching")
        return sid

    m = _RE_NO_TRAFFIC.match(line)
    if m:
        ts, sid, gap_s = m.group(1), m.group(2), m.group(3)
        t = trackers.setdefault(sid, TrackerState(sid))
        t.record_failure(ts, f"server-side disconnect (no traffic {gap_s}s)")
        return sid

    return None


# ----------------------------------------------------------------------------
# Remote log access
# ----------------------------------------------------------------------------

def ssh_run(host, cmd, capture=True):
    """Run a command via ssh, return stdout as str."""
    p = subprocess.run(
        ["ssh", host, cmd],
        capture_output=capture, text=True, errors="replace")
    return p.stdout if capture else ""


def replay_remote_log(host, trackers, since_iso=None, log_path=REMOTE_LOG_PATH):
    """Read tracker.log from the server and feed every line through the parser."""
    # `cat` is fine for <50MB logs; if it gets bigger we'd switch to a tail.
    out = ssh_run(host, f"cat {log_path} 2>/dev/null || true")
    last_login_id = [None]
    n_lines = n_relevant = 0
    for line in out.splitlines():
        n_lines += 1
        if since_iso and line[:19] < since_iso:
            continue
        sid = process_log_line(line, trackers, last_login_id)
        if sid:
            n_relevant += 1
    return n_lines, n_relevant


def tail_remote_log(host, on_line, stop_event, log_path=REMOTE_LOG_PATH):
    """Stream new tracker.log lines via `tail -F`, call on_line for each."""
    proc = subprocess.Popen(
        ["ssh", host, f"tail -n 0 -F {log_path} 2>/dev/null"],
        stdout=subprocess.PIPE, text=True, errors="replace")
    try:
        for line in proc.stdout:
            if stop_event.is_set():
                break
            on_line(line)
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


# ----------------------------------------------------------------------------
# Admin API client
# ----------------------------------------------------------------------------

def read_admin_pw():
    p = Path(ADMIN_PW_FILE)
    if not p.exists():
        raise SystemExit(f"Admin password file {ADMIN_PW_FILE} not found. "
                         f"Create it with the event admin password.")
    return p.read_text().strip()


def set_event_state(http_url, eid, state, admin_pw):
    body = json.dumps({"state": state}).encode("utf-8")
    req = urllib.request.Request(
        f"{http_url}/api/event/{eid}/admin/state",
        data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Admin-Password": admin_pw})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}", "body": e.read().decode(errors="replace")}


def set_all_state(http_url, eid, state, admin_pw):
    """Hit /admin/start-all or /admin/stop-all so currently-connected trackers
    flip immediately, AND event_state propagates for later joiners."""
    endpoint = "start-all" if state == "tracking" else "stop-all"
    req = urllib.request.Request(
        f"{http_url}/api/event/{eid}/admin/{endpoint}",
        data=b"", method="POST",
        headers={"X-Admin-Password": admin_pw})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}", "body": e.read().decode(errors="replace")}


# ----------------------------------------------------------------------------
# State persistence
# ----------------------------------------------------------------------------

def load_state(path):
    p = Path(path)
    if not p.exists():
        return {"trackers": {}, "current_phase_idx": 0, "phase_start_ts": None,
                "test_start_ts": None}
    with open(p) as f:
        return json.load(f)


def save_state(path, state_dict):
    p = Path(path)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state_dict, f, indent=2, default=str)
    tmp.replace(p)


# ----------------------------------------------------------------------------
# Main test loop
# ----------------------------------------------------------------------------

def iso_now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_duration(s):
    """'90m' / '24h' / '3600s' / '3600' → seconds."""
    s = s.strip().lower()
    if s.endswith("h"):
        return int(float(s[:-1]) * 3600)
    if s.endswith("m"):
        return int(float(s[:-1]) * 60)
    if s.endswith("s"):
        return int(s[:-1])
    return int(s)


def check_tracker_alive(t, target_state, now_iso):
    """Return failure reason str, or None if OK."""
    if t.observed_state == "disconnected":
        return "disconnected"
    if t.observed_state == "unknown":
        return "unknown state"
    if t.observed_state != target_state:
        return f"in {t.observed_state} not {target_state}"
    if t.last_seen is None:
        return "no traffic ever"
    # Liveness check
    threshold = ALIVE_THRESHOLDS.get(target_state, 600)
    last = datetime.strptime(t.last_seen, "%Y-%m-%d %H:%M:%S")
    now = datetime.strptime(now_iso, "%Y-%m-%d %H:%M:%S")
    gap = (now - last).total_seconds()
    if gap > threshold:
        return f"stale (last seen {int(gap)}s ago, threshold {threshold}s)"
    return None


def phase_summary(trackers, target_state, now_iso):
    """Return (n_ok, n_fail, [(sid, reason)])."""
    ok, fails = 0, []
    for sid in sorted(trackers):
        reason = check_tracker_alive(trackers[sid], target_state, now_iso)
        if reason:
            fails.append((sid, reason))
        else:
            ok += 1
    return ok, len(fails), fails


def print_phase_summary(phase, trackers, now_iso):
    target = phase["state"] if phase["state"] in ("active", "idle") else (
        "active" if phase["state"] == "tracking" else "idle")
    ok, n_fail, fails = phase_summary(trackers, target, now_iso)
    total = ok + n_fail
    print(f"\n[{iso_now()}] === Phase '{phase['name']}' check === "
          f"target={target}  ok={ok}/{total}")
    if fails:
        print("  FAILURES:")
        for sid, reason in fails:
            print(f"    {sid:10} {reason}")
    else:
        print("  All trackers OK.")


def run_test(args, trackers, state_dict, stop_event):
    plan = state_dict.get("plan", DEFAULT_PHASES)
    if args.plan:
        with open(args.plan) as f:
            plan = json.load(f)
    state_dict["plan"] = plan

    admin_pw = None if args.no_act else read_admin_pw()
    test_start = state_dict.get("test_start_ts") or iso_now()
    state_dict["test_start_ts"] = test_start
    deadline = None
    if args.duration:
        secs = parse_duration(args.duration)
        deadline = datetime.strptime(test_start, "%Y-%m-%d %H:%M:%S") + timedelta(seconds=secs)

    phase_idx = state_dict.get("current_phase_idx", 0)

    while not stop_event.is_set():
        if deadline and datetime.now() >= deadline:
            print(f"[{iso_now()}] Total test duration reached. Stopping.")
            break

        phase = plan[phase_idx % len(plan)]
        phase_start = state_dict.get("phase_start_ts")

        if not phase_start or state_dict.get("current_phase_idx") != phase_idx:
            # Entering a new phase
            phase_start = iso_now()
            state_dict["phase_start_ts"] = phase_start
            state_dict["current_phase_idx"] = phase_idx
            target_label = phase["state"]
            print(f"\n[{iso_now()}] ===== Enter phase '{phase['name']}' "
                  f"(state={target_label}, duration={phase['duration_s']}s) =====")
            if not args.no_act:
                r = set_all_state(args.http_url, args.event, target_label, admin_pw)
                print(f"  {target_label} all → {r}")
            save_state(args.state, _build_state_dict(state_dict, trackers))

        # Compute remaining time in phase
        elapsed = (datetime.now() - datetime.strptime(phase_start, "%Y-%m-%d %H:%M:%S")).total_seconds()
        remaining = phase["duration_s"] - elapsed

        if remaining > TRANSITION_WINDOW_S:
            # Mid-phase: sleep until next checkpoint (every minute), then re-check
            sleep_for = min(60, remaining - TRANSITION_WINDOW_S)
            stop_event.wait(sleep_for)
            continue

        if remaining > 0:
            # Approaching phase end — do one final summary then sleep till boundary
            print_phase_summary(phase, trackers, iso_now())
            save_state(args.state, _build_state_dict(state_dict, trackers))
            stop_event.wait(max(1, remaining))
            continue

        # Phase complete — final summary, advance
        print_phase_summary(phase, trackers, iso_now())
        phase_idx += 1
        state_dict["current_phase_idx"] = phase_idx
        state_dict["phase_start_ts"] = None
        save_state(args.state, _build_state_dict(state_dict, trackers))


def _build_state_dict(state_dict, trackers):
    return {
        **state_dict,
        "trackers": {sid: t.to_dict() for sid, t in trackers.items()},
        "saved_at": iso_now(),
    }


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--state", default=DEFAULT_STATE_FILE)
    ap.add_argument("--plan", default=None,
                    help="JSON file with a list of phases; defaults to "
                         "DEFAULT_PHASES baked into the script.")
    ap.add_argument("--duration", default=None,
                    help="Total runtime, e.g. 24h, 90m, 3600s. Default: forever.")
    ap.add_argument("--event", type=int, default=DEFAULT_EVENT_ID)
    ap.add_argument("--server", default=DEFAULT_SERVER)
    ap.add_argument("--http-url", default=DEFAULT_HTTP_URL)
    ap.add_argument("--no-act", action="store_true",
                    help="Read-only — don't send admin commands.")
    ap.add_argument("--replay-since", default=None,
                    help="Only replay log lines after this ISO timestamp "
                         "(YYYY-MM-DD HH:MM:SS). Default: full log.")
    args = ap.parse_args()

    print(f"[{iso_now()}] Loading persistent state from {args.state}")
    state_dict = load_state(args.state)
    trackers = {sid: TrackerState.from_dict(d)
                for sid, d in state_dict.get("trackers", {}).items()}
    print(f"  loaded {len(trackers)} trackers from previous run")

    # Always re-derive truth from logs — log is the source of truth, state file
    # is just a cached convenience. The replay overwrites observed_state/etc.
    print(f"[{iso_now()}] Re-deriving state from {args.server}:{REMOTE_LOG_PATH}")
    n_lines, n_relevant = replay_remote_log(args.server, trackers, args.replay_since)
    print(f"  replayed {n_lines} log lines ({n_relevant} relevant), "
          f"now tracking {len(trackers)} trackers")

    state_dict["trackers"] = {sid: t.to_dict() for sid, t in trackers.items()}
    save_state(args.state, _build_state_dict(state_dict, trackers))

    # Start live log tail in a background thread
    stop_event = threading.Event()
    last_login_id = [None]
    save_lock = threading.Lock()

    def on_live_line(line):
        sid = process_log_line(line, trackers, last_login_id)
        if sid:
            # Occasionally flush state to disk
            now = time.monotonic()
            if not hasattr(on_live_line, "_last_save"):
                on_live_line._last_save = now
            if now - on_live_line._last_save > 30:
                with save_lock:
                    save_state(args.state, _build_state_dict(state_dict, trackers))
                    on_live_line._last_save = now

    tail_thread = threading.Thread(
        target=tail_remote_log,
        args=(args.server, on_live_line, stop_event),
        daemon=True, name="log-tail")
    tail_thread.start()

    def handle_signal(signum, frame):
        print(f"\n[{iso_now()}] Got signal {signum}, shutting down...")
        stop_event.set()
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        run_test(args, trackers, state_dict, stop_event)
    finally:
        stop_event.set()
        save_state(args.state, _build_state_dict(state_dict, trackers))
        print(f"[{iso_now()}] State saved to {args.state}")
        print(f"\n=== FINAL TRACKER STATUS ===")
        for sid in sorted(trackers):
            t = trackers[sid]
            fw = t.firmware or "?"
            m1 = "M1" if t.in_mode1 else ("M!=1" if t.in_mode1 is False else "?")
            print(f"  {sid:10} state={t.observed_state:12} last_seen={t.last_seen} "
                  f"connects={t.connect_count} disc={t.disconnect_count} "
                  f"failures={len(t.failures)} {m1} {fw}")


if __name__ == "__main__":
    main()
