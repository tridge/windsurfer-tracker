#!/usr/bin/env python3
"""Real firmware/SITL carrier restriction, persistence, and offline recovery tests."""

import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import time

from test_gps_retry import gt06_cmd, receive_until
from test_loc_batch import finish_sitl

SITL = str(Path(os.environ.get("SITL_BIN", "./sitl-asan")).resolve())


class Tracker:
    def __init__(self, directory, **env):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.proc = subprocess.Popen(
            [SITL, "--server", f"127.0.0.1:{self.listener.getsockname()[1]}",
             "--teriid", "888800000000178", "--no-reexec"],
            cwd=directory, env={**os.environ, **env}, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.conn = None
        self.serial = 0

    def connect(self, timeout=10):
        if self.conn:
            self.conn.close()
        self.listener.settimeout(timeout)
        self.conn, _ = self.listener.accept()
        receive_until(self.conn, bytearray(), b"\x78\x78", 5)

    def command(self, command, expected):
        self.serial += 1
        self.conn.sendall(gt06_cmd(command, self.serial))
        reply = bytearray()
        receive_until(self.conn, reply, expected.encode(), 5)
        return reply

    def stop(self):
        if self.conn:
            self.conn.close()
        self.listener.close()
        return finish_sitl(self.proc)


def test_setting_and_persistence():
    with tempfile.TemporaryDirectory() as directory:
        t = Tracker(directory)
        try:
            t.connect()
            for invalid in ("505", "5050100", "50501,", ",50501", "50501,,50502",
                            "50501,50501", "50501, 50502", "50501#RESET#",
                            "111111,222222,333333,444444,555555", "50501x"):
                t.command("SZCS#PLMNOK=" + invalid, "SET ERROR: PLMNOK")
                t.command("CXCS#CELLSTAT", "ok50501,50502")
            t.command("SZCS#PLMNOK=310260,50502", "SETOK:PLMNOK=310260,50502")
            t.command("SZCS#PLMNOK=50502", "SETOK:PLMNOK=50502")
        finally:
            t.stop()
        t = Tracker(directory)
        try:
            t.connect()
            t.command("CXCS#CELLSTAT", "ok50502 policy1")
        finally:
            t.stop()
        # Corrupt the newest slot; boot must recover the previous complete setting.
        Path(directory, "sitl_wstrplmn.1").write_bytes(b"partial write")
        t = Tracker(directory)
        try:
            t.connect()
            t.command("CXCS#CELLSTAT", "ok310260,50502 policy1")
            # A failed write must neither alter RAM nor report success.
            path = Path(directory, "sitl_wstrplmn.1")
            path.unlink()
            path.mkdir()
            t.command("SZCS#PLMNOK=50501", "SET ERROR: write")
            t.command("CXCS#CELLSTAT", "ok310260,50502 policy1")
            path.rmdir()
            t.command("SZCS#PLMNOK=", "SETOK:PLMNOK=")
            t.command("CXCS#CELLSTAT", "okany policy1")
        finally:
            t.stop()
        # Empty policy survives reboot and restores auto even if reset preceded live application.
        t = Tracker(directory)
        try:
            t.connect()
            time.sleep(6)
            t.command("CXCS#CELLSTAT", "okany policy1 mode0")
        finally:
            _, err = t.stop()
        assert b"operator request AUTO" in err, err
    print("PLMN validation, persistence, torn-write recovery and explicit auto PASS", flush=True)


def test_disallowed_and_failed_first_carrier():
    with tempfile.TemporaryDirectory() as directory:
        t = Tracker(directory, SITL_PLMN_CURRENT="50503", SITL_PLMN_AVAILABLE="50502",
                    SITL_PLMN_DENIED="1")
        try:
            t.connect(timeout=35)
            time.sleep(2)
            t.command("CXCS#CELLSTAT", "plmn50502 man1 ok50501,50502 policy1 mode1")
            t.command("STATUS#", "GPS:OFF")
            time.sleep(17)  # legacy restore-auto threshold is 15 s in SITL
        finally:
            out, err = t.stop()
        assert b"PLMN: blocked operator 50503" in out, out
        assert b"operator request 50501: denied" in err, err
        assert b"operator request 50502: registered" in err, err
        assert b"operator request 50503" not in err, err
        assert b"operator request AUTO" not in err, err
    print("Disallowed operator recovery, carrier rotation and retained manual mode PASS", flush=True)


def test_denial_after_connection():
    with tempfile.TemporaryDirectory() as directory:
        t = Tracker(directory)
        try:
            t.connect()
            time.sleep(2)
            t.command("TIMER,1,1#", "TIMER,1,1# OK")
            os.kill(t.proc.pid, signal.SIGHUP)
            t.connect(timeout=65)
            time.sleep(2)
            t.command("CXCS#CELLSTAT", "plmn50501 man1")
            t.command("STATUS#", "GPS:ON")
        finally:
            out, err = t.stop()
        assert b"operator request 50501: registered" in err, err
        assert b"operator request AUTO" not in err, err
    print("Offline carrier-denial recovery with tracking retained PASS", flush=True)


def test_exhaustion_backoff():
    with tempfile.TemporaryDirectory() as directory:
        t = Tracker(directory, SITL_PLMN_CURRENT="50503", SITL_PLMN_AVAILABLE="",
                    SITL_PLMN_DENIED="1")
        try:
            time.sleep(47)
            # Service returning during the backoff must clear the busy gate and permit night sleep.
            # SIGHUP clears the injected registration denial on the last selected operator (50502).
            os.kill(t.proc.pid, signal.SIGHUP)
            t.connect()
            t.command("HBT,1,1#", "HBT,1,1# OK")
            t.command("SZCS#SLPDISCONNECT=1", "SETOK:SLPDISCONNECT=1")
            time.sleep(17)   # SLEEP_CMD_QUIET_SEC, then sleep and reconnect
            t.connect()
            t.command("STATUS#", "GPS:OFF")
        finally:
            out, err = t.stop()
        assert err.count(b"operator request ") == 2, err
        assert b"operator request 50503" not in err and b"operator request AUTO" not in err, err
        # a failed round ends with one radio off/on, then the holdoff
        assert (b"round failed, radio off/on" in out or
                b"allowed operators exhausted, backing off" in out), out
        assert b"SLEEP: awake, GPS remains off" in out, out
    print("No allowed coverage: bounded backoff, recovery and night sleep PASS", flush=True)


def test_setting_applies_under_continuous_polling():
    with tempfile.TemporaryDirectory() as directory:
        t = Tracker(directory)
        try:
            t.connect()
            t.command("SZCS#PLMNOK=50501", "SETOK:PLMNOK=50501")
            for _ in range(8):
                t.command("STATUS#", "GPS:OFF")
                time.sleep(1)
            t.command("CXCS#CELLSTAT", "plmn50501 man1 ok50501 policy1 mode1")
        finally:
            _, err = t.stop()
        assert b"operator request 50501: registered" in err, err
    print("Carrier setting applies despite continuous server polling PASS", flush=True)


if __name__ == "__main__":
    test_setting_and_persistence()
    test_disallowed_and_failed_first_carrier()
    test_denial_after_connection()
    test_exhaustion_backoff()
    test_setting_applies_under_continuous_polling()
