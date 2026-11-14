#!/usr/bin/env python3
"""SITL: status LEDs. Idle: blue flashes briefly every 2 s, yellow off. Tracking: blue solid.
Back to idle: the flashes resume. Low battery (<= ~25%): the idle flash is yellow instead."""

import re
import time

from sitl_util import Server, start, finish, TIME_SCALE

BLUE, YELLOW = 7, 0x78


def real_ms():
    """Real CLOCK_MONOTONIC ms, the clock the SITL stamps LED changes with (not scaled)."""
    return time.clock_gettime(time.CLOCK_MONOTONIC) * 1000


def main():
    srv = Server()
    proc = start(srv, "888800000000901")
    try:
        srv.accept_login()
        srv.pump(3)                                # settle after login
        t0 = real_ms()
        srv.pump(12)
        t1 = real_ms()
        srv.query("TIMER,1,1#", r"TIMER,1,1# OK")
        srv.pump(6)
        t2 = real_ms()
        srv.query("TIMER,600,1800#", r"TIMER,600,1800# OK")
        srv.pump(8)
        t3 = real_ms()
    finally:
        srv.close()
        _, stderr = finish(proc)

    ev = [(int(i), int(lv), int(t)) for i, lv, t in
          re.findall(rb"\[SITL\] LED id=(\d+) level=(\d) t=(\d+)", stderr)]
    sim = lambda ms: ms * TIME_SCALE              # real ms -> sim ms

    def flashes(a, b):
        """(on time, sim duration) of each blue on->off in [a, b)"""
        out, on = [], None
        for i, lv, t in ev:
            if i != BLUE or not (a <= t < b):
                continue
            if lv == 1:
                on = t
            elif on is not None:
                out.append((on, sim(t - on)))
                on = None
        return out

    idle = flashes(t0, t1)
    assert 4 <= len(idle) <= 7, f"{len(idle)} blue flashes in 12 s idle, expected ~6"
    # 60 ms; host scheduling counts TIME_SCALE x in sim time, so allow more when accelerated
    hi = 120 if TIME_SCALE == 1 else 400
    assert all(40 <= d <= hi for _, d in idle), f"flash lengths (ms): {[round(d) for _, d in idle]}"
    gaps = [sim(idle[k][0] - idle[k - 1][0]) / 1000 for k in range(1, len(idle))]
    assert all(1.5 <= g <= 3.5 for g in gaps), f"flash spacing (s): {gaps}"
    assert not any(i == YELLOW and lv == 1 and t0 <= t < t1 for i, lv, t in ev), "yellow on while idle"

    # tracking: blue goes solid and stays on
    trk = [(lv, t) for i, lv, t in ev if i == BLUE and t1 <= t < t2]
    assert trk and trk[-1][0] == 1, f"blue not on while tracking: {trk}"
    settle = t1 + 3000 / TIME_SCALE
    assert not any(lv == 0 for lv, t in trk if t > settle), f"blue blinked while tracking: {trk}"

    back = flashes(t2 + 2000 / TIME_SCALE, t3)
    assert len(back) >= 2, f"flashes didn't resume after going idle: {back}"
    print(f"LEDs: idle {len(idle)} blue flashes/12 s, {round(min(d for _, d in idle))}-"
          f"{round(max(d for _, d in idle))} ms, every {min(gaps):.1f}-{max(gaps):.1f} s; solid while "
          f"tracking; {len(back)} flashes after going idle again PASS")


def low_battery():
    srv = Server()
    proc = start(srv, "888800000000902", env_extra={"SITL_BATT_MV": "3550"})
    try:
        srv.accept_login()
        srv.pump(3)
        t0 = real_ms()
        srv.pump(9)
        t1 = real_ms()
    finally:
        srv.close()
        _, stderr = finish(proc)
    ev = [(int(i), int(lv), int(t)) for i, lv, t in
          re.findall(rb"\[SITL\] LED id=(\d+) level=(\d) t=(\d+)", stderr)]
    yel = [t for i, lv, t in ev if i == YELLOW and lv == 1 and t0 <= t < t1]
    blue = [t for i, lv, t in ev if i == BLUE and lv == 1 and t0 <= t < t1]
    assert 3 <= len(yel) <= 6 and not blue, f"3.55 V idle: {len(yel)} yellow, {len(blue)} blue flashes in 9 s"
    print(f"LEDs: low battery (3.55 V) idle -> {len(yel)} yellow flashes/9 s, no blue PASS")


if __name__ == "__main__":
    main()
    low_battery()
