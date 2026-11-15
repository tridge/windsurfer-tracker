#!/usr/bin/env python3
"""SITL: idle-and-offline behaviour — connect backoff, no-data recovery, night sleep while offline."""

import os
import signal
import time

from sitl_util import Server, start, finish, reset_state, run_tests


def attempts(out):
    return out.count(b"S4: connect retry")


def test_idle_offline_backoff():
    srv = Server()
    proc = start(srv, "888800000000601")
    try:
        srv.accept_login()
        os.kill(proc.pid, signal.SIGUSR1)          # coverage lost while idle
        srv.wait_closed(30)
        time.sleep(55)                             # under the SITL no-data reboot (150 s)
        os.kill(proc.pid, signal.SIGUSR1)          # coverage back
        t0 = time.monotonic()
        srv.accept_login(30)
        took = time.monotonic() - t0
    finally:
        srv.close()
        out, _ = finish(proc)
    n = attempts(out)
    # unbounded 5 s retry would be ~11 attempts in 55 s; backoff caps at 20 s in SITL
    assert n <= 8, f"{n} connect attempts in 55 s offline — no backoff"
    assert took < 25, f"reconnect took {took:.0f}s after coverage returned"
    print(f"idle offline: {n} attempts in 55 s (backoff), reconnect {took:.0f}s after service PASS")


def test_nodata_reboot():
    reset_state()
    srv = Server()
    proc = start(srv, "888800000000602", reexec=True)
    try:
        srv.accept_login()
        os.kill(proc.pid, signal.SIGUSR1)          # registered, but no data path
        srv.wait_closed(30)
        srv.accept_login(240)                      # re-exec after the no-data reboot clears the fault
        m = srv.query("CXCS#BOOTSTAT", r"rr(\d+) .* nd(\d+)")
        assert int(m.group(1)) == 7, f"expected NODATA reboot, got rr{m.group(1)}"
        assert int(m.group(2)) == 0, "no-data counter not cleared by the successful login"
    finally:
        srv.close()
        out, _ = finish(proc)
    assert b"NODATA: radio off/on" in out and b"NODATA: rebooting" in out, out.decode(errors="replace")[-1500:]
    print("no data path: radio off/on, then a NODATA reboot, counter cleared on login PASS")


def test_night_sleep_while_offline():
    srv = Server()
    proc = start(srv, "888800000000603")
    try:
        srv.accept_login()
        srv.query("HBT,10,10#", r"HBT,10,10# OK")
        srv.query("SZCS#SLPDISCONNECT=1", r"SETOK:SLPDISCONNECT=1")
        srv.pump(8)                                # let PLMN confirm registration: a fixed loss path
        os.kill(proc.pid, signal.SIGUSR1)          # lose coverage before the connected sleep starts
        srv.wait_closed(40)
        time.sleep(170)                            # grace + PLMN round (~110 s in SITL), then sleep cycles
        os.kill(proc.pid, signal.SIGUSR1)
        srv.accept_login(40)
    finally:
        srv.close()
        out, _ = finish(proc)
    sleeps = out.count(b"SLEEP: awake, GPS remains off")
    assert sleeps >= 2, f"only {sleeps} sleeps while offline\n" + out.decode(errors="replace")[-4000:]
    print(f"night sleep while offline: {sleeps} sleep cycles, {attempts(out)} attempts PASS")


def test_offline_sleep_with_server_down_still_tries_on_wake():
    """Registered throughout (no coverage edge to reset the backoff): every wake must make a real
    connection attempt, so the unit comes back once the server does."""
    srv = Server()
    proc = start(srv, "888800000000604")
    try:
        srv.accept_login()
        srv.query("HBT,10,10#", r"HBT,10,10# OK")
        srv.query("SZCS#SLPDISCONNECT=1", r"SETOK:SLPDISCONNECT=1")
        srv.stop_listening()                       # server down, radio fine
        time.sleep(70)
        srv.reopen()
        t0 = time.monotonic()
        srv.accept_login(40)
        took = time.monotonic() - t0
    finally:
        srv.close()
        out, _ = finish(proc)
    sleeps = out.count(b"SLEEP: awake, GPS remains off")
    assert sleeps >= 2, f"only {sleeps} sleeps\n" + out.decode(errors="replace")[-4000:]
    assert took < 30, f"took {took:.0f}s to come back after the server returned"
    print(f"server down while registered: {sleeps} sleeps, back {took:.0f}s after the server PASS")


if __name__ == "__main__":
    run_tests([
        test_offline_sleep_with_server_down_still_tries_on_wake,
        test_idle_offline_backoff,
        test_night_sleep_while_offline,
        test_nodata_reboot,
    ], "idle units out of coverage back off, sleep, and recover a dead data path")
