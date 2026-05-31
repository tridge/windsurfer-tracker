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
        self._scheduled_disconnect_pending = False
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
        self._scheduled_disconnect_pending = False
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
        try:
            if self.entity is None:
                lat, lon, speed_kmh, heading, sats = -41.2865, 174.7762, 0, 180, 8
            else:
                lat = float(self.entity.lat)
                lon = float(self.entity.lon)
                speed_kmh = max(0, min(255, int(self.entity.spd * 1.852)))
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
        except Exception as e:
            log.error("%s send_loc failed: %s (entity=%r)", self.sailor_id, e, self.entity)

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

        # PARAM# — bulk parameter dump (the subset our reconciler reads back).
        if cmd == "PARAM#":
            self._reply(
                f"IMEI:{self.imei};TIMER:{self.freq},{self.freq};"
                f"SENDS:{self.sends_mode};HBT:{self.hbt_interval}Sec;Defense:0;",
                server_flag)
            return

        # CXCS#KEY — read back a SZCS# setting (READOK: KEY=VAL).
        if cmd.startswith("CXCS#"):
            key = cmd[len("CXCS#"):].rstrip("#")
            vals = {"SLPDISCONNECT": self.slp_disconnect,
                    "GPS_RST_TIME": self.gps_rst_time,
                    "VIBCHK": self.vibchk, "ACCLINE": self.accline}
            self._reply(f"READOK: {key}={vals.get(key, 0)}", server_flag)
            return

        # Bare query forms (no comma): SENALM# / MOVING# / SENDS#.
        if cmd == "SENALM#":
            self._reply(f"SENALM:{self.senalm}", server_flag)
            return
        if cmd == "MOVING#":
            self._reply(f"MOVING:{self.moving}", server_flag)
            return
        if cmd == "SENDS#":
            self._reply(f"SENDS:{self.sends_mode}", server_flag)
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

        # MODE4,M# / MODE5,M# → scheduled-wake. After ACK, schedule a
        # disconnect so the next wake fires at the right offset.
        # Arg unit differs by mode (vendor doc):
        #   MODE5: minutes (real devices wake every ~13-15 min @ N=15)
        #   MODE4: seconds (default 60, vibration-responsive)
        # Both ACK in the same format: "MODE{n} OK Freq:{arg}-DW:2".
        for n in (4, 5):
            prefix = f"MODE{n},"
            if cmd.startswith(prefix):
                try:
                    self.mode = n
                    requested = int(cmd.rstrip("#").split(",")[1])
                    clamp = self.quirks.get("mode4_freq_clamp")
                    if n == 4 and clamp is not None:
                        # W07 firmware ignores the MODE4 Freq arg and stays
                        # clamped (the real storm bug). Mirror that here.
                        self.freq = clamp
                    else:
                        self.freq = requested
                except (ValueError, IndexError):
                    pass
                self._reply(f"MODE{n} OK Freq:{self.freq}-DW:2", server_flag)
                # Schedule TCP disconnect after a short dwell — applies to
                # both MODE4 and MODE5 since both tear down the connection
                # between scheduled wakes.
                self._scheduled_disconnect_pending = True
                return

        # TIMER,N,N# — real device's F register bleeds: TIMER overwrites
        # Freq even when device is in MODE5 (the bug we hit on Wed).
        # Reset _next_loc_at so an interval change takes effect immediately
        # rather than waiting out the previously-scheduled (possibly long)
        # interval.
        if cmd.startswith("TIMER,"):
            try:
                n = int(cmd.rstrip("#").split(",")[1])
                self.freq = n
                self._next_loc_at = self.clock.now() + min(n, 1.0)
            except (ValueError, IndexError):
                n = 0
            self._reply(f"TIMER ACC ON:{n}s,ACC OFF:{n}s", server_flag)
            return

        # HBT,N,N# — set heartbeat interval. Triggers hbt_silent quirk.
        # Same reasoning as TIMER: reset _next_hbt_at on every change.
        if cmd.startswith("HBT,"):
            try:
                self.hbt_interval = int(cmd.rstrip("#").split(",")[1])
                self._next_hbt_at = self.clock.now() + min(self.hbt_interval, 1.0)
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
        # Real W07C devices terminate command-response strings with a \x00\x01
        # trailer (seen in raw gt06.log). Mirror it so the server's response
        # parsing (e.g. the settings reconciler's value extraction) is exercised
        # against the real framing, not a forgiving trailing space.
        data = build_command_ack_data(server_flag, text + "\x00\x01")
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

    def _maybe_disconnect_after_scheduled_mode(self):
        """MODE4/MODE5: after a short awake-dwell, drop TCP and schedule
        reconnect at the configured wake interval. MODE5's arg is in
        minutes, MODE4's in seconds (vendor doc)."""
        if not self._scheduled_disconnect_pending:
            return
        now = self.clock.now()
        # Stay awake mode5_dwell_s seconds, then disconnect.
        if now - self._connected_at < self.mode5_dwell_s:
            return
        wake_delta = self.freq * 60 if self.mode == 5 else self.freq
        next_wake = now + wake_delta
        log.info("%s MODE%d sleep, next wake in %ds",
                 self.sailor_id, self.mode, wake_delta)
        self._disconnect()
        self._scheduled_disconnect_pending = False
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
        # First LOC fires almost immediately — real W07C devices upload their
        # last-known position on reconnect, and tests need a position to
        # register the sailor in current_positions.json without waiting.
        self._next_loc_at = now + 0.3
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
            try:
                self._drain_rx()
                self._emit_due()
                self._maybe_disconnect_after_scheduled_mode()
            except Exception as e:
                log.exception("%s loop iteration failed: %s", self.sailor_id, e)

            # V667 tcp-dies-after-10min quirk: stop responding after silence.
            tcp_dies = self.quirks.get("tcp_dies_after_idle_s")
            if tcp_dies and self.sock is not None:
                if self.clock.now() - self._last_rx_at > tcp_dies:
                    log.info("%s modem-sleep quirk: closing TCP", self.sailor_id)
                    self._disconnect()

            if self.sock is None and not self._scheduled_disconnect_pending:
                # Disconnected and no scheduled reconnect — wait for one or stop.
                self.clock.sleep(0.5, self._stop_event)
                continue

            self.clock.sleep(0.1, self._stop_event)

        self._disconnect()


# ---------------------------------------------------------------------------
# Fleet runner — drives N GT06DeviceSim instances using SailingSimulator's
# entity model. Invoked by tracker_server.py's _run_simulator_thread when the
# WebUI selects "GT06 W07C tracker" as the device type.
# ---------------------------------------------------------------------------

def run_gt06_simulation(host, gt06_port, eid, *,
                        num_sailors=5, num_support=1,
                        wind_direction=None, avg_speed=12.0, num_laps=0,
                        max_duration=3600,
                        course_file=None, course_waypoints=None,
                        sailor_names=None,
                        stop_event=None, status_callback=None,
                        speedup=1.0, start_at_start=True, speedup_ref=None,
                        loc_interval_s=10, hbt_interval_s=60):
    """Spin up N GT06DeviceSim instances and drive them with SailingSimulator.

    Mirrors test_client.py:run_simulation's setup (course loading, entity
    generation, wind direction) but each entity's position is shipped over
    TCP as GT06 frames rather than UDP JSON.
    """
    # Lazy imports — test_client pulls in argparse and other heavy modules,
    # and we want gt06_device_sim importable as a small standalone too.
    from test_client import (
        load_course, calculate_wind_from_course, generate_sailor_names,
        create_entities, SailingSimulator,
    )

    if course_waypoints is None and course_file:
        course_data = load_course(course_file)
        if course_data:
            course_waypoints = course_data.waypoints

    if course_waypoints and len(course_waypoints) >= 2:
        start_loc = course_waypoints[0]
        end_loc = course_waypoints[-1]
    else:
        start_loc = (-41.2865, 174.7762)
        end_loc = (-41.2700, 174.8050)

    if wind_direction is None and course_waypoints and len(course_waypoints) >= 2:
        wind_direction = calculate_wind_from_course(course_waypoints)
    elif wind_direction is None:
        wind_direction = 180.0

    if sailor_names is None:
        sailor_names = generate_sailor_names(num_sailors)

    entities = create_entities(
        num_sailors, num_support, 0,
        start_loc, end_loc, course_waypoints, avg_speed=avg_speed,
        sailor_names=sailor_names, start_at_start=start_at_start,
    )
    sailors = [e for e in entities if e.role == "sailor"]
    sailing = SailingSimulator(start_loc, end_loc,
                               wind_direction=wind_direction,
                               num_laps=num_laps)

    # Spawn one sim per entity. IMEIs are synthetic but unique within the
    # event so the server's IMEI→sailor_id mapping produces distinct sailors.
    sims = []
    if eid < 0 or eid > 99:
        raise ValueError(
            f"Sim IMEI scheme encodes eid in 2 digits — eid={eid} out of range")
    for i, ent in enumerate(entities):
        # IMEI format: 999 + 2-digit eid + 10-digit index = 15 digits.
        # The 999 TAC prefix flags this as a sim (real GT06 hardware uses
        # 866-prefixed TACs); the embedded eid lets the listener auto-route
        # to the right event without any gt06.json edits.
        imei = f"999{eid:02d}{i:010d}"
        sim = GT06DeviceSim(
            imei=imei, host=host, port=gt06_port,
            entity=ent,
            freq=loc_interval_s,
            hbt_interval=hbt_interval_s,
        )
        sims.append(sim)
        sim.start()

    start_time = time.time()
    tick = 1.0  # advance entities once per real second
    reason = "stopped"
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            elapsed = time.time() - start_time
            if max_duration > 0 and elapsed >= max_duration:
                reason = "timeout"
                break
            if num_laps > 0 and all(s.current_lap >= num_laps for s in sailors):
                reason = "finished"
                break

            cur_speedup = speedup_ref[0] if speedup_ref else speedup
            steps = max(1, int(tick * cur_speedup))
            for _ in range(steps):
                for ent in entities:
                    if ent.role == "sailor":
                        sailing.update_sailor(ent, 1.0)
                    elif ent.role == "support":
                        sailing.update_support(ent, 1.0, sailors)
                    else:
                        sailing.update_spectator(ent, 1.0)

            if status_callback is not None:
                sailors_finished = sum(
                    1 for s in sailors if num_laps > 0 and s.current_lap >= num_laps)
                status_callback({
                    "updates_sent": int(elapsed),
                    "sailors_finished": sailors_finished,
                    "elapsed_s": elapsed,
                })

            time.sleep(tick)
    finally:
        for sim in sims:
            sim.stop()

    return {"reason": reason,
            "elapsed_s": time.time() - start_time,
            "device_count": len(sims)}


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
