#!/usr/bin/env python3
"""Regatta test controller — cycles event 10 between active tracking and normal
race-day idle, with an overnight idle hold.

- Tracking phase: a random 20-80 min, then
- Idle phase: a random 20-80 min, then repeat.
- Overnight (21:00-05:00 in the event timezone): hold NORMAL idle (stop-all),
  NOT night-idle/sleep mode.
- Logs every transition + a settle-time verification (how many units actually
  reached the commanded state) to regatta_test.log.

Runs on the server (localhost admin API). Stop cleanly: `touch <DIR>/STOP`.
"""
import os
import sys
import json
import time
import random
import datetime
import urllib.request
from zoneinfo import ZoneInfo

EID = 10
BASE = "http://localhost:41234"
EVENTS_JSON = "/home/tracker/tracker/events.json"
STATE_JSON = "/home/tracker/tracker/gt06_state.json"
DIR = "/home/tracker/regatta_test"
LOGFILE = os.path.join(DIR, "regatta_test.log")
STOPFILE = os.path.join(DIR, "STOP")

NIGHT_START, NIGHT_END = 21, 5          # 21:00 .. 05:00, idle hold
MIN_MIN, MAX_MIN = 20, 80               # phase length bounds
SETTLE_SEC = 120                        # wait before verifying a transition

try:
    TZ = ZoneInfo(json.load(open(EVENTS_JSON))["events"][str(EID)].get("timezone", "Australia/Brisbane"))
except Exception:
    TZ = ZoneInfo("Australia/Brisbane")


def now():
    return datetime.datetime.now(TZ)


def log(msg):
    line = f"{now().strftime('%Y-%m-%d %H:%M:%S %Z')}  {msg}"
    print(line, flush=True)
    with open(LOGFILE, "a") as f:
        f.write(line + "\n")


def _pw():
    return json.load(open(EVENTS_JSON))["events"][str(EID)]["admin_password"]


def post(path):
    req = urllib.request.Request(BASE + path, method="POST",
                                 headers={"X-Admin-Password": _pw()})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except Exception as e:
        return None, {"error": str(e)}


def fleet_state():
    """(active, idle, offline) counts for event 10 from device_state."""
    try:
        devs = json.load(open(STATE_JSON))["devices"]
    except Exception:
        return None
    t = time.time()
    active = idle = offline = 0
    for s in devs.values():
        if s.get("eid") != EID:
            continue
        if t - (s.get("last_seen") or 0) > 180:
            offline += 1
        elif s.get("idle"):
            idle += 1
        else:
            active += 1
    return active, idle, offline


def set_state(active):
    sub = "start-all" if active else "stop-all"
    st, resp = post(f"/api/event/{EID}/admin/{sub}")
    cnt = resp.get("started_count", resp.get("stopped_count", resp.get("error", "?")))
    log(f"CMD {'TRACK' if active else 'IDLE '} -> HTTP {st} count={cnt}")
    return st == 200


def interruptible_sleep(target):
    while now() < target:
        if os.path.exists(STOPFILE):
            return True
        time.sleep(min(30, max(1, (target - now()).total_seconds())))
    return os.path.exists(STOPFILE)


def in_night(t):
    return t.hour >= NIGHT_START or t.hour < NIGHT_END


def verify(active):
    time.sleep(SETTLE_SEC)
    fs = fleet_state()
    if fs is None:
        log("  verify: (state unavailable)")
        return
    a, i, off = fs
    want = "active" if active else "idle"
    got = a if active else i
    log(f"  verify {SETTLE_SEC}s after {want.upper()}: active={a} idle={i} offline={off} "
        f"-> {got}/41 reached {want}")


def main():
    os.makedirs(DIR, exist_ok=True)
    if os.path.exists(STOPFILE):
        os.remove(STOPFILE)
    log(f"=== regatta test START (event {EID}, tz {TZ}, night-idle hold "
        f"{NIGHT_START:02d}:00-{NIGHT_END:02d}:00, phases {MIN_MIN}-{MAX_MIN} min) ===")
    next_active = True   # begin with a tracking phase
    while not os.path.exists(STOPFILE):
        t = now()
        if in_night(t):
            set_state(False)
            log("NIGHT idle hold")
            verify(False)
            end = t.replace(hour=NIGHT_END, minute=0, second=0, microsecond=0)
            if t.hour >= NIGHT_START:
                end += datetime.timedelta(days=1)
            if interruptible_sleep(end):
                break
            next_active = True      # resume the day with tracking
            continue
        # daytime phase
        set_state(next_active)
        verify(next_active)
        dur = datetime.timedelta(minutes=random.randint(MIN_MIN, MAX_MIN))
        night = t.replace(hour=NIGHT_START, minute=0, second=0, microsecond=0)
        end = min(now() + dur, night)   # cap the phase at the night boundary
        log(f"  phase {'TRACK' if next_active else 'IDLE '} until {end.strftime('%H:%M')} "
            f"({int((end-now()).total_seconds()/60)} min)")
        if interruptible_sleep(end):
            break
        next_active = not next_active
    set_state(False)   # leave the fleet idle on exit
    log("=== regatta test STOP (left fleet idle) ===")


if __name__ == "__main__":
    main()
