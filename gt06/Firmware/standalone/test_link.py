#!/usr/bin/env python3
"""SITL: link liveness, fixes in flight on a dead link, SOS until ACKed, socket errors."""

import datetime
import os
import signal
import socket
import struct
import time

from sitl_util import HERE, Server, start, finish

STUCK = os.path.join(HERE, "sitl_tx_stuck.flag")


def fix_times(seen):
    """Absolute fix times (deciseconds since 2000) from every 0xF1 / 0x12 frame in `seen`."""
    out = []
    for proto, p, _, _ in seen:
        if proto == 0xF1 and len(p) >= 12:
            yy, mo, dd, hh, mi, ss = p[5:11]
            base = datetime.datetime(2000 + yy, mo, dd, hh, mi, ss)
            b = int((base - datetime.datetime(2000, 1, 1)).total_seconds()) * 10 + p[11]
            for i in range(p[1]):
                out.append(b + struct.unpack(">H", p[12 + 14 * i:14 + 14 * i])[0])
        elif proto == 0x12 and len(p) >= 6 and p[1]:
            yy, mo, dd, hh, mi, ss = p[0:6]
            t = datetime.datetime(2000 + yy, mo, dd, hh, mi, ss)
            out.append(int((t - datetime.datetime(2000, 1, 1)).total_seconds()) * 10)
    return out


def track(srv, batch=5):
    srv.send_cmd(f"SZCS#LOCBATCH={batch}")
    srv.send_cmd("HBT,5,5#")
    srv.send_cmd("SZCS#BLIND_EN=1")
    srv.send_cmd("TIMER,1,1800#")
    srv.pump(8)


def test_silent_server_requeues_inflight_fixes():
    srv = Server()
    proc = start(srv, "888800000000401")
    try:
        srv.accept_login()
        track(srv)
        # The server goes silent: TCP stays up, nothing is ACKed any more.
        srv.auto_ack = False
        srv.seen = []
        t0 = time.monotonic()
        srv.pump(45)                               # pump returns early on close
        dead = fix_times(srv.seen)
        took = time.monotonic() - t0
        assert dead, "no fixes were sent on the silent link"
        srv.auto_ack = True
        srv.accept_login(20)
        srv.pump(15)
        replayed = set(fix_times(srv.seen))
        missing = [t for t in dead if t not in replayed]
        assert not missing, f"{len(missing)}/{len(dead)} fixes sent on the dead link never came back"
    finally:
        srv.close()
        out, _ = finish(proc)
    assert b"LINK: no server response" in out, out.decode(errors="replace")[-1500:]
    print(f"silent server: link dropped after ~{took:.0f}s, all {len(dead)} in-flight fixes replayed PASS")


def test_stuck_send_buffer():
    srv = Server()
    proc = start(srv, "888800000000402", {"SITL_TX_STUCK_FLAG": STUCK})
    try:
        srv.accept_login()
        track(srv)
        open(STUCK, "w").close()
        t_stuck = time.time()
        srv.pump(35)
        os.unlink(STUCK)
        srv.accept_login(30)
        srv.pump(15)
        times = fix_times(srv.seen)
        sec = [t // 10 for t in times]
        epoch = int((datetime.datetime.fromtimestamp(t_stuck, datetime.UTC).replace(tzinfo=None) - datetime.datetime(2000, 1, 1)).total_seconds())
        during = [s for s in sec if epoch + 2 <= s <= epoch + 30]
        assert len(set(during)) >= 20, f"only {len(set(during))} s of the stuck period came back"
    finally:
        if os.path.exists(STUCK):
            os.unlink(STUCK)
        srv.close()
        out, _ = finish(proc)
    assert b"LINK: send buffer stuck" in out, out.decode(errors="replace")[-1500:]
    print("stuck send buffer: no blocking, link dropped, stuck-period fixes replayed PASS")


def test_sos_until_acked():
    srv = Server()
    proc = start(srv, "888800000000403", {"SITL_SOS": "1"})
    try:
        srv.auto_ack = False
        srv.accept(15)
        srv.pump(28)
        alarms = [s for s in srv.seen if s[0] == 0x16]
        assert len(alarms) >= 11, f"only {len(alarms)} alarms without an ACK — gave up early"
        srv.auto_ack = True
        srv.pump(3)
        n = len([s for s in srv.seen if s[0] == 0x16])
        srv.pump(15)
        after = len([s for s in srv.seen if s[0] == 0x16])
        assert after == n, f"kept sending after the ACK ({n} -> {after})"
    finally:
        srv.close()
        finish(proc)
    print(f"SOS: {len(alarms)} alarms while un-ACKed, stopped once ACKed PASS")


def test_reset_connection_reconnects():
    srv = Server()
    proc = start(srv, "888800000000404")
    try:
        srv.accept_login()
        srv.pump(2)
        srv.conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        srv.send_cmd("STATUS#")
        srv.conn.close()                          # RST
        srv.conn = None
        t0 = time.monotonic()
        srv.accept_login(20)
        took = time.monotonic() - t0
        assert took < 12, f"reconnect took {took:.0f}s"
    finally:
        srv.close()
        finish(proc)
    print(f"connection reset -> reconnect in {took:.0f}s PASS")


def test_bad_commands_are_not_acted_on():
    srv = Server()
    proc = start(srv, "888800000000405")
    try:
        srv.accept_login()
        srv.send_cmd("RESET#", crc_ok=False)       # corrupt frame: ignored outright
        srv.send_cmd("RESETjunk#")
        srv.wait_reply(r"RESETjunk# ERROR")
        srv.send_cmd("TIMER,1x#")
        srv.wait_reply(r"TIMER,1x# ERROR")
        srv.send_cmd("HBT,0,0#")
        srv.wait_reply(r"HBT,0,0# ERROR")
        assert srv.pump(4), "a malformed command reset the tracker"
        m = srv.query("STATUS#", r"GPS:(\w+)")
        assert m.group(1) == "OFF", "malformed TIMER started tracking"
        assert not any("RESET# " in r for r in srv.replies()), "bad-CRC RESET# was answered"
        srv.send_cmd("TIMER,1,1800#")
        srv.wait_reply(r"TIMER,1,1800# OK")
    finally:
        srv.close()
        out, _ = finish(proc)
    assert b"RESET: reboot requested" not in out
    print("bad CRC / RESETjunk / malformed TIMER+HBT rejected, valid TIMER accepted PASS")


JUMP = os.path.join(HERE, "sitl_nmea_jump.flag")


def test_blind_replay_across_a_day_gap():
    """Fixes a day apart must not be packed into one frame as if they were seconds apart."""
    srv = Server()
    proc = start(srv, "888800000000406", {"SITL_NMEA_JUMP_FLAG": JUMP})
    try:
        srv.accept_login()
        track(srv)
        os.kill(proc.pid, signal.SIGUSR1)          # coverage lost: fixes go to the blind buffer
        time.sleep(6)
        open(JUMP, "w").close()                    # GPS time now a day (+1 s) later
        time.sleep(6)
        os.kill(proc.pid, signal.SIGUSR1)
        srv.accept_login(20)
        srv.pump(12)
        times = sorted(set(fix_times(srv.seen)))
        gaps = [b - a for a, b in zip(times, times[1:])]
        assert max(gaps) >= 86000 * 10, f"day gap lost: largest step {max(gaps) / 10:.0f}s"
        assert all(g < 30 * 10 or g >= 86000 * 10 for g in gaps), f"odd steps {sorted(gaps)[-5:]}"
    finally:
        if os.path.exists(JUMP):
            os.unlink(JUMP)
        srv.close()
        finish(proc)
    print("blind replay across a day gap keeps true times PASS")


def test_second_sos_on_the_same_connection():
    srv = Server()
    proc = start(srv, "888800000000407")
    try:
        srv.accept_login()
        os.kill(proc.pid, signal.SIGTSTP)          # press
        srv.pump(12)
        first = len([x for x in srv.seen if x[0] == 0x16])
        assert first >= 1, "first SOS not sent"
        os.kill(proc.pid, signal.SIGTSTP)          # release
        srv.query("CANCELSOS#", r"CANCELSOS# OK")
        srv.pump(7)
        os.kill(proc.pid, signal.SIGTSTP)          # press again, same connection
        srv.pump(12)
        second = len([x for x in srv.seen if x[0] == 0x16]) - first
        assert second >= 1, "second SOS on the same connection was never sent"
    finally:
        srv.close()
        finish(proc)
    print("second SOS after ACK + CANCELSOS on one connection is sent PASS")


def test_slow_acks_still_release_inflight_fixes():
    srv = Server()
    proc = start(srv, "888800000000408")
    try:
        srv.accept_login()
        srv.ack_delay = 12.0                       # ACK latency longer than HBT
        track(srv)
        srv.pump(45)
        m = srv.query("CXCS#LINKSTAT", r" ua(\d+)")
        ua = int(m.group(1))
        assert ua < 40, f"{ua} fixes still unproven after 45 s of slow but working ACKs"
    finally:
        srv.close()
        finish(proc)
    print(f"ACKs slower than HBT still release in-flight fixes (ua={ua}) PASS")


def test_rejected_hbt_changes_nothing():
    srv = Server()
    proc = start(srv, "888800000000409")
    try:
        srv.accept_login()
        srv.query("HBT,1,0#", r"HBT,1,0# ERROR")
        m = srv.query("CXCS#LINKSTAT", r" hb(\d+)/(\d+)")
        assert (m.group(1), m.group(2)) == ("15", "15"), f"rejected HBT applied: {m.group(0)}"
    finally:
        srv.close()
        finish(proc)
    print("rejected HBT leaves the heartbeat unchanged PASS")


def test_backward_gps_time_kept_in_replay():
    srv = Server()
    proc = start(srv, "888800000000410", {"SITL_NMEA_JUMP_FLAG": JUMP, "SITL_NMEA_JUMP_S": "-3"})
    try:
        srv.accept_login()
        track(srv)
        os.kill(proc.pid, signal.SIGUSR1)
        time.sleep(5)
        open(JUMP, "w").close()                    # receiver clock steps back 3 s
        time.sleep(5)
        os.kill(proc.pid, signal.SIGUSR1)
        srv.accept_login(20)
        srv.pump(10)
        times = fix_times(srv.seen)
        steps = [b - a for a, b in zip(times, times[1:])]
        assert any(-45 <= d <= -15 for d in steps), f"backward step not preserved: {steps}"
    finally:
        if os.path.exists(JUMP):
            os.unlink(JUMP)
        srv.close()
        finish(proc)
    print("backward GPS time step keeps its true timestamp in replay PASS")


if __name__ == "__main__":
    test_second_sos_on_the_same_connection()
    test_slow_acks_still_release_inflight_fixes()
    test_rejected_hbt_changes_nothing()
    test_backward_gps_time_kept_in_replay()
    test_blind_replay_across_a_day_gap()
    test_bad_commands_are_not_acted_on()
    test_reset_connection_reconnects()
    test_sos_until_acked()
    test_silent_server_requeues_inflight_fixes()
    test_stuck_send_buffer()
    print("dead links are detected, in-flight fixes and SOS are not lost")
