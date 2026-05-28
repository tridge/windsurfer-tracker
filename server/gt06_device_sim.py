"""GT06 device simulator — a single Python class that opens a real TCP
connection to the GT06 listener and behaves like a W07C tracker.

Designed to mirror what we've learned about real-device behaviour: cxzt#
state introspection, MODE1/MODE5 wake cycles, TIMER/HBT/SZCS# state
mutation, and the V667 firmware quirks catalogued in
project_v667_firmware_quirks.md.

Two clock modes:
  - Wall clock (default): used by the WebUI simulator. Real time, real sleeps.
  - Virtual clock (tests): advance time programmatically so MODE5,15# can fire
    its next wake in milliseconds. Lets the behaviour tests run in <1s each.

Single-device class — drive N of them concurrently for a fleet simulation.
"""

import argparse
import heapq
import logging
import random
import socket
import threading
import time
from datetime import datetime, timezone

from gt06_frames import (
    build_frame,
    build_login_data,
    build_location_data,
    build_heartbeat_data,
    build_alarm_data,
    build_command_ack_data,
    iter_frames,
    parse_frame,
    extract_command,
)


log = logging.getLogger("gt06sim")


# ---------------------------------------------------------------------------
# Virtual clock
# ---------------------------------------------------------------------------

class VirtualClock:
    """A controllable time source. Wall mode delegates to time.time/sleep;
    virtual mode returns an explicit `now` that tests advance via `set`/`advance`.

    Sleep semantics:
      - wall: real time.sleep(seconds), interruptible via stop_event.
      - virtual: spin-wait until virtual now >= deadline, with a short real
        sleep so the test thread can call advance().

    Schedule semantics: callbacks fire when now() >= their target time.
    Callbacks are checked on each sleep() call (cheap, single-thread runner).
    """

    def __init__(self, virtual=False, start_time=None):
        self.virtual = virtual
        self._now = start_time if start_time is not None else (0.0 if virtual else time.time())
        self._lock = threading.Lock()
        self._scheduled = []  # min-heap of (at_time, seq, callback)
        self._seq = 0

    def now(self):
        if self.virtual:
            with self._lock:
                return self._now
        return time.time()

    def set(self, t):
        """Virtual mode: jump time to absolute t and fire any due callbacks."""
        if not self.virtual:
            raise RuntimeError("set() requires virtual=True")
        with self._lock:
            self._now = max(self._now, t)
        self._fire_due()

    def advance(self, seconds):
        """Virtual mode: advance time by N seconds and fire any due callbacks."""
        if not self.virtual:
            raise RuntimeError("advance() requires virtual=True")
        with self._lock:
            self._now += seconds
        self._fire_due()

    def sleep(self, seconds, stop_event=None):
        """Block for `seconds`. Returns early if stop_event fires."""
        if seconds <= 0:
            self._fire_due()
            return
        if self.virtual:
            deadline = self.now() + seconds
            while self.now() < deadline:
                if stop_event is not None and stop_event.is_set():
                    return
                time.sleep(0.01)
            self._fire_due()
            return
        if stop_event is not None:
            stop_event.wait(seconds)
        else:
            time.sleep(seconds)

    def schedule(self, at, callback):
        """Fire callback() once when now() reaches at (absolute time)."""
        with self._lock:
            self._seq += 1
            heapq.heappush(self._scheduled, (at, self._seq, callback))

    def _fire_due(self):
        now = self.now()
        due = []
        with self._lock:
            while self._scheduled and self._scheduled[0][0] <= now:
                due.append(heapq.heappop(self._scheduled))
        for _at, _seq, cb in due:
            try:
                cb()
            except Exception:
                log.exception("scheduled callback failed")


# ---------------------------------------------------------------------------
# GT06 device simulator
# ---------------------------------------------------------------------------

_DEFAULT_QUIRKS = {
    # Emit LOC with course_status = 0x0000 even on valid fix (V667 firmware).
    "course_status_zero": False,
    # Reply OK to HBT,N,N# but never actually send 0x13 heartbeat frames.
    "hbt_silent": False,
    # Stop responding to anything after N seconds of idle (V667 modem-dies-after-10min).
    "tcp_dies_after_idle_s": 0,
    # Disconnect immediately after the login command burst completes (off-charge quirk).
    "off_charge_disconnect": False,
    # Vibration drives LOC every ~60s regardless of TIMER ACC OFF interval.
    "vibration_loc_override": False,
}


class GT06DeviceSim:
    """One simulated GT06 device. Runs its main loop in a worker thread.

    Server commands (0x80) arrive on the socket and are dispatched to
    `_handle_server_cmd`, which mutates internal state and sends a 0x15 ACK.

    Position updates: caller mutates `lat/lon/speed/heading/battery_mv` (e.g.
    via the SailingSimulator entity), the loop emits LOC packets at the
    configured `freq` and HBT at `hbt_interval`.
    """

    def __init__(self, imei, host, port, *, clock=None,
                 firmware="NT19D_MG133_10F8G_B53_V667",
                 mcu="B2.5-0",
                 server_addr="103.230.158.49:7711",
                 apn="simbase",
                 quirks=None, entity=None,
                 mode=1, freq=30, hbt_interval=300,
                 battery_mv=4000, signal=4):
        self.imei = imei
        self.host = host
        self.port = port
        self.clock = clock or VirtualClock(virtual=False)
        self.firmware = firmware
        self.mcu = mcu
        self.server_addr = server_addr
        self.apn = apn
        self.quirks = dict(_DEFAULT_QUIRKS)
        if quirks:
            self.quirks.update(quirks)
        self.entity = entity

        # Device-side state — mirrors what real W07C exposes via cxzt#.
        self.mode = mode
        self.freq = freq
        self.hbt_interval = hbt_interval
        self.battery_mv = battery_mv
        self.signal = signal
        self.charging = False
        # SZCS# settings (string values where applicable).
        self.slp_disconnect = 1
        self.accline = 0
        self.gps_rst_time = 300
        self.vibchk = "0:16"
        # Other tunables touched by server commands.
        self.sends_mode = 0
        self.senalm = "ON"
        self.moving = "ON"
        # MODE5 dwell-while-awake (seconds the device stays online per wake).
        self.mode5_dwell_s = 60

        # Internal plumbing.
        self.sock = None
        self._serial = 0
        self._last_server_flag = b"\x00\x00\x00\x00"
        self._stop_event = threading.Event()
        self._thread = None
        self._rx_buf = b""
        self._last_rx_at = 0.0
        self._hbt_acked_silent = False  # tracks the hbt_silent quirk
        self._mode5_disconnect_pending = False
        self._connected_at = 0.0
        # Initialised so step()/_emit_due() are safe to call before connect().
        self._next_hbt_at = float("inf")
        self._next_loc_at = float("inf")

        # Per-device sailor_id (the server derives this from the IMEI).
        self.sailor_id = self._derive_sailor_id(imei)

    # ------ public lifecycle ------

    def start(self):
        """Spawn the worker thread. Returns immediately."""
        if self._thread is not None:
            raise RuntimeError("already started")
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"gt06sim-{self.sailor_id}")
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        self._disconnect()

    def join(self, timeout=None):
        if self._thread:
            self._thread.join(timeout=timeout)

    # ------ optional manual driving (tests) ------

    def step(self):
        """Process any pending server frames + emit any due LOC/HBT.
        Useful for virtual-clock tests that don't want to wait for the thread."""
        self._drain_rx()
        self._emit_due()

    # ------ internals ------

    @staticmethod
    def _derive_sailor_id(imei):
        # Server's convention from the logs: last 6 digits of IMEI prefixed with G.
        # (Actual server mapping is in tracker_server; this is for log readability.)
        if len(imei) >= 6:
            return "G" + imei[-6:]
        return "G" + imei

    def _next_serial(self):
        self._serial += 1
        return self._serial

    def connect(self):
        """Open TCP and send login. Synchronous — returns after socket is open."""
        if self.sock is not None:
            return
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(2.0)
        self.sock.connect((self.host, self.port))
        self._rx_buf = b""
        self._last_rx_at = self.clock.now()
        self._connected_at = self.clock.now()
        self._mode5_disconnect_pending = False
        # Login frame: protocol 0x01, payload = 8-byte BCD IMEI.
        self._send_frame(0x01, build_login_data(self.imei))

    def _disconnect(self):
        if self.sock is None:
            return
        try:
            self.sock.close()
        except Exception:
            pass
        self.sock = None

    def _send_frame(self, protocol, data):
        if self.sock is None:
            return
        frame = build_frame(protocol, data, self._next_serial())
        try:
            self.sock.sendall(frame)
        except Exception as e:
            log.debug("%s send failed: %s", self.sailor_id, e)
            self._disconnect()

    # ------ packet emitters (callable from tests) ------

    def send_loc(self):
        """Build and send a LOC frame from current entity position."""
        if self.entity is None:
            lat, lon, speed_kmh, heading, sats = -41.2865, 174.7762, 0, 180, 8
        else:
            lat = self.entity.lat
            lon = self.entity.lon
            speed_kmh = int(self.entity.spd * 1.852)  # knots → km/h
            heading = int(self.entity.hdg) % 360
            sats = 8
        ts = datetime.fromtimestamp(self.clock.now(), tz=timezone.utc)
        data = build_location_data(
            lat=lat, lon=lon, speed_kmh=speed_kmh, heading=heading,
            satellites=sats, gps_valid=True,
            year=ts.year - 2000, month=ts.month, day=ts.day,
            hour=ts.hour, minute=ts.minute, second=ts.second,
            course_status_zero=bool(self.quirks.get("course_status_zero")),
        )
        self._send_frame(0x12, data)

    def send_hbt(self):
        """Build and send a 0x13 heartbeat frame.
        Respects the hbt_silent quirk: after the device ACKs a HBT,N,N#
        command it stops actually sending 0x13 (V667 firmware bug)."""
        if self.quirks.get("hbt_silent") and self._hbt_acked_silent:
            return
        bat_level = self._battery_mv_to_level(self.battery_mv)
        data = build_heartbeat_data(bat_level, self.signal, self.charging)
        self._send_frame(0x13, data)

    def send_alarm(self, alarm_type="SOS"):
        if self.entity is None:
            lat, lon, heading = -41.2865, 174.7762, 0
        else:
            lat, lon, heading = self.entity.lat, self.entity.lon, int(self.entity.hdg) % 360
        loc = build_location_data(lat=lat, lon=lon, heading=heading)
        bat_level = self._battery_mv_to_level(self.battery_mv)
        data = build_alarm_data(loc, alarm_type=alarm_type,
                                battery_level=bat_level, signal=self.signal,
                                charging=self.charging)
        self._send_frame(0x16, data)

    @staticmethod
    def _battery_mv_to_level(mv):
        """Inverse of the server's voltage→percent mapping, coarse."""
        if mv >= 4100:
            return 6
        if mv >= 3950:
            return 5
        if mv >= 3800:
            return 4
        if mv >= 3700:
            return 3
        if mv >= 3600:
            return 2
        if mv >= 3500:
            return 1
        return 0

    # ------ command handling (server → device) ------

    def _drain_rx(self):
        """Read any pending bytes off the socket, dispatch complete frames."""
        if self.sock is None:
            return
        try:
            self.sock.settimeout(0.0)
            try:
                chunk = self.sock.recv(4096)
            except (BlockingIOError, socket.error):
                chunk = b""
            if chunk:
                self._rx_buf += chunk
                self._last_rx_at = self.clock.now()
        except Exception:
            return
        finally:
            try:
                self.sock.settimeout(2.0)
            except Exception:
                pass

        frames, self._rx_buf = iter_frames(self._rx_buf)
        for frame in frames:
            self._dispatch_frame(frame)

    def _dispatch_frame(self, frame):
        parsed = parse_frame(frame)
        if parsed is None:
            return
        protocol, _data, _serial = parsed
        if protocol == 0x80:
            extracted = extract_command(frame)
            if extracted:
                server_flag, cmd_text = extracted
                self._last_server_flag = server_flag
                self._handle_server_cmd(cmd_text.strip(), server_flag)
        # Other server→device frames are ACKs to our login/LOC/HBT — ignore.

    def _handle_server_cmd(self, cmd, server_flag):
        """Mutate state per command and emit the 0x15 ACK the server expects.

        Real W07C devices have idiosyncratic response strings; we mirror the
        ones our parser looks for (see protocol_GT06.py:1083-1140).
        """
        # cxzt# — return the rich device-info line our cxzt# handler parses.
        if cmd == "cxzt#":
            resp = (
                f"{self.firmware}-GT06 MCU:{self.mcu}*ID:{self.imei}*"
                f"{self.server_addr}*A:{self.apn}*G:A*4G:14*"
                f"M:{self.mode}|2|0*F:{self.freq}|540*H:{self.hbt_interval}*"
                f"SP:1*SF:0*V:0*BT:{self.battery_mv}*LBS:1*R:14|1|1"
            )
            self._reply(resp, server_flag)
            return

        # STATUS# — battery + GPRS state.
        if cmd == "STATUS#":
            volts = self.battery_mv / 1000.0
            resp = (f"Battery:{volts:.2f}V;GPRS:Online;GSM Signal Level:{self.signal*5};"
                    f"ACC:{'ON' if not self.charging else 'OFF'};GPS:OFF;Defense:OFF;")
            self._reply(resp, server_flag)
            return

        # MODE1,F,H#  → switch to MODE1, set freq + heartbeat.
        if cmd.startswith("MODE1,"):
            parts = cmd.rstrip("#").split(",")
            try:
                self.mode = 1
                self.freq = int(parts[1])
                self.hbt_interval = int(parts[2]) if len(parts) > 2 else self.hbt_interval
            except (ValueError, IndexError):
                pass
            self._reply(f"MODE1 OK Freq:{self.freq}-HBT:{self.hbt_interval}", server_flag)
            return

        # MODE5,M# → MODE5 scheduled-wake. After ACK, schedule a disconnect
        # so the next wake is freq minutes away. NB: vendor doc says minutes;
        # real devices behave like minutes (~13-15 min observed wakes).
        if cmd.startswith("MODE5,"):
            try:
                self.mode = 5
                self.freq = int(cmd.rstrip("#").split(",")[1])
            except (ValueError, IndexError):
                pass
            self._reply(f"MODE5 OK Freq:{self.freq}-DW:2", server_flag)
            # Schedule TCP disconnect after a short dwell.
            self._mode5_disconnect_pending = True
            return

        # TIMER,N,N# — real device's F register bleeds: TIMER overwrites
        # Freq even when device is in MODE5 (the bug we hit on Wed).
        if cmd.startswith("TIMER,"):
            try:
                n = int(cmd.rstrip("#").split(",")[1])
                self.freq = n
            except (ValueError, IndexError):
                n = 0
            self._reply(f"TIMER ACC ON:{n}s,ACC OFF:{n}s", server_flag)
            return

        # HBT,N,N# — set heartbeat interval. Triggers hbt_silent quirk.
        if cmd.startswith("HBT,"):
            try:
                self.hbt_interval = int(cmd.rstrip("#").split(",")[1])
            except (ValueError, IndexError):
                pass
            self._reply(f"HBT ACC ON:{self.hbt_interval}s,ACC OFF:{self.hbt_interval}s", server_flag)
            self._hbt_acked_silent = True
            return

        # SZCS#KEY=VAL — generic key/value setter. Real device returns SETOK.
        if cmd.startswith("SZCS#"):
            kv = cmd[len("SZCS#"):].rstrip("#")
            key, _, val = kv.partition("=")
            if key == "SLPDISCONNECT":
                try:
                    self.slp_disconnect = int(val)
                except ValueError:
                    pass
            elif key == "ACCLINE":
                try:
                    self.accline = int(val)
                except ValueError:
                    pass
            elif key == "GPS_RST_TIME":
                try:
                    self.gps_rst_time = int(val)
                except ValueError:
                    pass
            elif key == "VIBCHK":
                self.vibchk = val
            self._reply(f"SETOK: {kv}", server_flag)
            return

        # SENDS,N# — ACK only.
        if cmd.startswith("SENDS,"):
            try:
                self.sends_mode = int(cmd.rstrip("#").split(",")[1])
            except (ValueError, IndexError):
                pass
            self._reply(f"SENDS:{self.sends_mode}", server_flag)
            return

        # SENALM, MOVING — ACK only.
        if cmd.startswith("SENALM,"):
            self.senalm = cmd.rstrip("#").split(",")[1]
            self._reply(f"SENALM:{self.senalm}", server_flag)
            return
        if cmd.startswith("MOVING,"):
            self.moving = cmd.rstrip("#").split(",")[1]
            self._reply(f"MOVING:{self.moving}", server_flag)
            return

        # Anything else — generic ACK with command echoed.
        self._reply(f"ACK:{cmd}", server_flag)

    def _reply(self, text, server_flag):
        data = build_command_ack_data(server_flag, text + " ")
        self._send_frame(0x15, data)

    # ------ main loop ------

    def _emit_due(self):
        """Send HBT / LOC if their next-due time has been reached."""
        now = self.clock.now()
        if self._next_hbt_at <= now:
            self.send_hbt()
            self._next_hbt_at = now + self.hbt_interval
        if self._next_loc_at <= now:
            self.send_loc()
            interval = self.freq
            if self.quirks.get("vibration_loc_override") and interval > 60:
                interval = 60
            self._next_loc_at = now + interval

    def _maybe_disconnect_after_mode5(self):
        """MODE5: after a short awake-dwell, drop TCP and schedule reconnect."""
        if not self._mode5_disconnect_pending:
            return
        now = self.clock.now()
        # Stay awake mode5_dwell_s seconds, then disconnect.
        if now - self._connected_at < self.mode5_dwell_s:
            return
        next_wake = now + self.freq * 60  # MODE5,N — N is minutes
        log.info("%s MODE5 sleep, next wake in %ds", self.sailor_id, self.freq * 60)
        self._disconnect()
        self._mode5_disconnect_pending = False
        # Schedule reconnect via the clock.
        self.clock.schedule(next_wake, self._reconnect)

    def _reconnect(self):
        if self._stop_event.is_set():
            return
        try:
            self.connect()
            self._reset_timers()
            log.info("%s wake-cycle reconnect at t=%.1f", self.sailor_id, self.clock.now())
        except Exception as e:
            log.warning("%s reconnect failed: %s", self.sailor_id, e)

    def _reset_timers(self):
        now = self.clock.now()
        self._next_hbt_at = now + self.hbt_interval
        # First LOC ~5s after connect.
        self._next_loc_at = now + 5
        self._last_rx_at = now
        self._hbt_acked_silent = False

    def _run(self):
        try:
            self.connect()
        except Exception as e:
            log.error("%s initial connect failed: %s", self.sailor_id, e)
            return
        self._reset_timers()

        while not self._stop_event.is_set():
            self._drain_rx()
            self._emit_due()
            self._maybe_disconnect_after_mode5()

            # V667 tcp-dies-after-10min quirk: stop responding after silence.
            tcp_dies = self.quirks.get("tcp_dies_after_idle_s")
            if tcp_dies and self.sock is not None:
                if self.clock.now() - self._last_rx_at > tcp_dies:
                    log.info("%s modem-sleep quirk: closing TCP", self.sailor_id)
                    self._disconnect()

            if self.sock is None and not self._mode5_disconnect_pending:
                # Disconnected and no scheduled reconnect — wait for one or stop.
                self.clock.sleep(0.5, self._stop_event)
                continue

            self.clock.sleep(0.1, self._stop_event)

        self._disconnect()


# ---------------------------------------------------------------------------
# CLI for manual dogfooding
# ---------------------------------------------------------------------------

def _main():
    p = argparse.ArgumentParser(description="Single-device GT06 simulator")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7711)
    p.add_argument("--imei", default="999000000000001")
    p.add_argument("--firmware", default="NT19D_MG133_10F8G_B53_V667")
    p.add_argument("--mode", default="default",
                   choices=["default", "v667"],
                   help="quirk preset to apply")
    p.add_argument("--freq", type=int, default=30, help="MODE1 LOC interval (s)")
    p.add_argument("--hbt", type=int, default=300, help="heartbeat interval (s)")
    p.add_argument("--duration", type=int, default=120, help="stop after N seconds")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    quirks = None
    if args.mode == "v667":
        quirks = {
            "course_status_zero": True,
            "hbt_silent": True,
            "tcp_dies_after_idle_s": 600,
            "vibration_loc_override": True,
        }

    sim = GT06DeviceSim(
        imei=args.imei, host=args.host, port=args.port,
        firmware=args.firmware, quirks=quirks,
        freq=args.freq, hbt_interval=args.hbt,
    )
    sim.start()
    try:
        time.sleep(args.duration)
    except KeyboardInterrupt:
        pass
    finally:
        sim.stop()


if __name__ == "__main__":
    _main()
