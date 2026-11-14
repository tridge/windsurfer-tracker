#!/usr/bin/env python3
"""SITL: a receiver that is powered but silent gets power-cycled, then the tracker reboots."""

import os
import time

from sitl_util import RUN_DIR, Server, start, finish, reset_state

SILENT = os.path.join(RUN_DIR, "sitl_gnss_silent.flag")


def test_silent_receiver_is_power_cycled_then_rebooted():
    reset_state()
    srv = Server()
    open(SILENT, "w").close()
    proc = start(srv, "888800000000501", {"SITL_GNSS_SILENT_FLAG": SILENT}, reexec=True)
    try:
        srv.accept_login()
        srv.send_cmd("TIMER,1,1800#")
        srv.wait_reply(r"TIMER,1,1800# OK")
        m = None
        t0 = time.monotonic()
        while time.monotonic() - t0 < 30:
            m = srv.query("CXCS#GPSSTAT", r" cy(\d+)")
            if int(m.group(1)) >= 2:
                break
            srv.pump(2)
        assert m and int(m.group(1)) >= 2, "silent receiver was not power-cycled"
        # Still silent: after GPS_CYCLE_MAX cycles the tracker reboots with reason RR_GPS_DEAD.
        srv.wait_closed(60)
        srv.accept_login(30)
        m = srv.query("CXCS#BOOTSTAT", r"rr(\d+)")
        assert int(m.group(1)) == 8, f"expected GPS_DEAD reboot, got rr{m.group(1)}"
        # Same fault again must not reboot-loop: it keeps cycling instead.
        srv.send_cmd("TIMER,1,1800#")
        srv.wait_reply(r"TIMER,1,1800# OK")
        assert srv.pump(50), "rebooted again for the same fault"
        os.unlink(SILENT)
        srv.pump(12)
        assert any(s[0] == 0xF1 for s in srv.seen), "fixes did not resume once the receiver talked"
    finally:
        if os.path.exists(SILENT):
            os.unlink(SILENT)
        srv.close()
        _, err = finish(proc)
    assert err.count(b"GNSS power off") >= 3, err.decode(errors="replace")[-2000:]
    print("silent GNSS: power-cycled, one reboot, no reboot loop, recovers PASS")


if __name__ == "__main__":
    test_silent_receiver_is_power_cycled_then_rebooted()
