#!/usr/bin/env python3
"""SITL: the real reliability.c watchdog, its boot deadline and the reset-reason record."""

import os
import time

from sitl_util import HERE, Server, start, finish, reset_state

FLAG = os.path.join(HERE, "sitl_hang_send.flag")


def bootstat(srv):
    m = srv.query("CXCS#BOOTSTAT", r"BOOTSTAT rr(\d+) bc(\d+) up(\d+)")
    return int(m.group(1)), int(m.group(2))


def test_reset_reasons_and_send_hang():
    reset_state()
    srv = Server()
    proc = start(srv, "888800000000301", {"SITL_HANG_SEND_FLAG": FLAG}, reexec=True)
    try:
        srv.accept_login()
        rr, bc = bootstat(srv)
        assert rr == 3, f"first boot should read FIRSTBOOT, got rr{rr}"
        srv.send_cmd("RESET#")
        srv.wait_reply(r"RESET# OK")
        srv.accept_login(20)
        rr2, bc2 = bootstat(srv)
        assert rr2 == 2 and bc2 == bc + 1, f"RESET# should read COMMAND, got rr{rr2} bc{bc2}"
        m = srv.query("CXCS#BOOTSTAT", r" wt(\S+)")
        assert m.group(1).startswith("2,"), f"RESET# trace missing: wt{m.group(1)}"

        # A send() that never returns: the watchdog (timer thread, no I/O) must still reset us.
        open(FLAG, "w").close()
        t0 = time.monotonic()
        srv.accept_login(45)
        took = time.monotonic() - t0
        rr3, bc3 = bootstat(srv)
        assert rr3 == 0 and bc3 == bc2 + 1, f"watchdog reset should read UNEXPECTED, got rr{rr3} bc{bc3}"
        assert took < 35, f"watchdog took {took:.0f}s"
        m = srv.query("CXCS#BOOTSTAT", r" wt(\S+)")
        assert m.group(1).startswith("1,S,"), f"watchdog trace should name the send phase: wt{m.group(1)}"
    finally:
        srv.close()
        _, err = finish(proc)
    assert b"send() hang injected" in err, err.decode(errors="replace")[-2000:]
    print("reset reasons (first boot / RESET# / watchdog on a hung send) PASS")


def test_boot_hang_deadline():
    srv = Server()
    proc = start(srv, "888800000000302", {"SITL_HANG_BOOT": "1"}, reexec=True)
    try:
        t0 = time.monotonic()
        srv.accept_login(45)
        took = time.monotonic() - t0
        rr, _ = bootstat(srv)
        assert rr == 0, f"boot-deadline reset should read UNEXPECTED, got rr{rr}"
        assert took > 15, "connected before the boot deadline could have fired"
    finally:
        srv.close()
        _, err = finish(proc)
    assert b"boot hang injected" in err, err.decode(errors="replace")[-2000:]
    print(f"boot hang caught by the boot deadline ({took:.0f}s) PASS")


def test_corrupt_primary_config_uses_backup():
    srv = Server()
    proc = start(srv, "888800000000303", {"SITL_CWPRM_CORRUPT": "1"})
    try:
        srv.accept_login()
        m = srv.query("cxzt#", r"\*ID:(\d+)\*")
        assert m.group(1) == "888800000000303", f"logged in as {m.group(1)}, not the backup TERIID"
    finally:
        srv.close()
        out, _ = finish(proc)
    assert b"S4:cfg_ok=0x00000002" in out, "backup config not used"
    print("corrupt primary config: the backup copy is used PASS")


if __name__ == "__main__":
    test_corrupt_primary_config_uses_backup()
    test_reset_reasons_and_send_hang()
    test_boot_hang_deadline()
    print("watchdog covers boot and blocked sends; reset reasons are recorded")
