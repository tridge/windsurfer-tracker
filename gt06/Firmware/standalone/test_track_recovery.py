#!/usr/bin/env python3
"""SITL: a tracking unit with no data path cycles the radio early and repeatedly, not once at 20 min."""

import os
import signal
import time

from sitl_util import Server, start, finish, reset_state


def test_tracking_nodata_radio_cycles():
    reset_state()
    srv = Server()
    proc = start(srv, "888800000000701")
    try:
        srv.accept_login()
        srv.send_cmd("TIMER,1,1#")
        srv.pump(5)
        os.kill(proc.pid, signal.SIGUSR1)          # registered, but no data path
        srv.wait_closed(30)
        time.sleep(65)                             # SITL: first cycle at 15 s, then every 30 s
        os.kill(proc.pid, signal.SIGUSR1)
        srv.accept_login(40)
        m = srv.query("CXCS#CELLSTAT", r"ps(\d+) nc(\d+)")
        nc = int(m.group(2))
    finally:
        srv.close()
        out, _ = finish(proc)
    cycles = out.count(b"NODATA: radio off/on")
    assert cycles >= 2, f"only {cycles} radio cycles in ~65 s offline while tracking"
    assert nc == cycles, f"CELLSTAT nc{nc} but {cycles} cycles logged"
    assert b"NODATA: rebooting" not in out, "rebooted while tracking"
    print(f"tracking no-data: {cycles} radio cycles, CELLSTAT nc{nc}, no reboot PASS")


if __name__ == "__main__":
    test_tracking_nodata_radio_cycles()
