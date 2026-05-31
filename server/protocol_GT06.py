"""GT06 GPS Tracker Protocol handler.

Extracted from tracker_server.py to keep protocol-specific code separate.
This module must NOT import tracker_server to avoid circular imports.
All server interactions happen through callbacks passed to GT06Listener.
"""

import fcntl
import json
import math
import os
import re
import selectors
import socket
import struct
import time
from calendar import timegm
from datetime import datetime
from pathlib import Path


# Battery level mapping: GT06 reports 0-6, server expects 0-100
_GT06_BATTERY_MAP = {0: 0, 1: 5, 2: 15, 3: 30, 4: 50, 5: 75, 6: 100}

# gt06.log binary format v2: 8-byte file magic, then 14-byte per-record header
# (8B float64 ts LE + 4B uint32 conn_id LE + 2B uint16 length LE) + frame bytes.
# Bit 31 of conn_id is the direction flag: 1 = server→device, 0 = device→server.
# Format v1 (legacy): no magic, 10-byte per-record header (8B ts + 2B length).
GT06_LOG_MAGIC_V2 = b"GT06LOG2"
GT06_LOG_DIR_OUT = 0x80000000  # OR'd into conn_id for server→device packets

# Empirical discharge curve for W07C (3000mAh), derived from 24h turntable test.
# Pairs of (voltage, percentage), descending voltage, evenly spaced in time.
_W07C_DISCHARGE = [
    (4.14, 100), (4.03, 95), (3.99, 90), (3.97, 85), (3.93, 80),
    (3.89, 75),  (3.86, 70), (3.82, 65), (3.77, 60), (3.72, 55),
    (3.67, 50),  (3.65, 45), (3.62, 40), (3.60, 35), (3.58, 30),
    (3.55, 25),  (3.52, 20), (3.47, 15), (3.44, 10), (3.37, 5),
]

# Storm guard: how many times we re-push the overnight Freq when a device
# reports the wrong value before giving up. Firmware that simply refuses the
# Freq (e.g. W07 clamping MODE4 to 120) would otherwise re-push forever and
# burn the battery. After this many tries we log once and leave it alone.
OVERNIGHT_FREQ_MAX_RETRIES = 3


# Commands to send when entering idle mode.
#
# TIMER ACC ON/OFF intervals are deliberately equal — vehicle-tracker "ACC"
# detection on the W07C is vibration-driven, so we don't want behavior to
# depend on whether the device thinks it's "stopped" or "running". With both
# intervals equal, every idle device uploads at the same cadence regardless.
# The interval is matched to idle_hbt_interval so the rate monitor doesn't
# get confused.
#
# SZCS#SLPDISCONNECT=0 = "long connection", i.e. don't drop TCP when the
# device's modem enters sleep. Without this, V667 idle devices lose TCP
# within ~10 min of silence and we can't reach them until their next ACC-OFF
# reconnect cycle. With it, TCP stays open across sleep. Safe to send on
# every login (no reconnect side-effect, no-op if already set).
#
# MODE1 is deliberately NOT sent here. Sending MODE1 every login causes a
# reconnect storm: each MODE1 tears down the TCP, the device immediately
# reconnects, login handler sends MODE1 again, repeat. MODE1 persists on
# the device across reboots, so it should be set once per device via a
# separate first-time-setup path.
def _idle_cmds(interval):
    return ["SZCS#SLPDISCONNECT=0",
            f"TIMER,{interval},{interval}#",
            "SENDS,1#", "SENALM,OFF#", "MOVING,OFF#",
            "SZCS#GPS_RST_TIME=0", "SZCS#VIBCHK=0:16"]


def _active_cmds(interval):
    """Commands to send when entering active tracking mode.

    SLPDISCONNECT=0 added for V667 firmware to prevent TCP drops on
    transient modem sleeps. MODE1 NOT sent here — see _idle_cmds comment;
    MODE1 must be a one-shot per device, not a per-login command.
    """
    return ["SZCS#SLPDISCONNECT=0",
            f"TIMER,{interval},{interval}#", "SENDS,0#",
            "SZCS#GPS_RST_TIME=300", "SZCS#VIBCHK=0:16"]


def _overnight_arg(interval_min, mode_number):
    """Convert the human-facing interval_min into the units the chosen
    MODE command expects on the wire.

    MODE5 takes minutes directly (vendor doc: min 5, max 65535 minutes).
    MODE4 takes seconds (vendor doc: default 60 seconds, vibration-wake).

    Keeping a single human-facing "interval_min" knob means operators
    can flip overnight_mode_number between 4 and 5 in gt06.json without
    rethinking the wake cadence.
    """
    return interval_min * 60 if mode_number == 4 else interval_min


def _overnight_cmds(interval_min, mode_number=4):
    """Commands to send when entering OVERNIGHT idle (deep sleep).

    The device wakes every `interval_min` minutes, opens a TCP connection,
    locates, reports, then powers off until the next scheduled wake.

    Two supported overnight modes (mode_number config):
      4 — MODE4: scheduled wake, vibration-responsive (default; vendor-
          recommended replacement for MODE5 as of 2026-05). Arg in seconds.
      5 — MODE5: scheduled wake, strictly time-based (no vibration wake),
          MCU+modem off between wakes. Arg in minutes.

    SZCS#ACCLINE=1 stops the device treating vibration as ACC-ON, so
    boats rocking with wind/wave overnight don't trigger spurious wakes.
    For MODE5 this is belt-and-braces (MODE5 already ignores vibration);
    for MODE4 it suppresses the vibration-wake behaviour for the night.

    SZCS#SLPDISCONNECT=0 ("long connection") is harmless in either mode
    since the device tears down its TCP every cycle anyway.
    """
    arg = _overnight_arg(interval_min, mode_number)
    return ["SZCS#SLPDISCONNECT=0",
            "SZCS#ACCLINE=1",
            f"MODE{mode_number},{arg}#"]


# ---------------------------------------------------------------------------
# Table-driven settings reconciler
#
# Per non-MODE setting: how to SET it to a value, and how to QUERY its current
# value. MODE itself is NOT in this table — it keeps its existing one-shot /
# storm-guarded handling in the cxzt# response path. On (re)connect (and on a
# commanded state change) we query every setting, log the observed values for
# certainty during testing, then send ONLY the ones whose value is wrong. The
# ACTIVE desired values below reproduce the legacy _active_cmds output exactly,
# so live race tracking is byte-for-byte unchanged.
# ---------------------------------------------------------------------------

# key -> (set-command builder, query command). TIMER/HBT are read back from the
# bulk cxzt# response (F / H), so they have no separate query command.
_SETTINGS = {
    "SLPDISCONNECT": (lambda v: f"SZCS#SLPDISCONNECT={v}", "CXCS#SLPDISCONNECT"),
    "GPS_RST_TIME":  (lambda v: f"SZCS#GPS_RST_TIME={v}",  "CXCS#GPS_RST_TIME"),
    "VIBCHK":        (lambda v: f"SZCS#VIBCHK={v}",        "CXCS#VIBCHK"),
    "ACCLINE":       (lambda v: f"SZCS#ACCLINE={v}",       "CXCS#ACCLINE"),
    "SENDS":         (lambda v: f"SENDS,{v}#",             "SENDS#"),
    "SENALM":        (lambda v: f"SENALM,{v}#",            "SENALM#"),
    "MOVING":        (lambda v: f"MOVING,{v}#",            "MOVING#"),
    "TIMER":         (lambda v: f"TIMER,{v},{v}#",         None),
    "HBT":           (lambda v: f"HBT,{v},{v}#",           None),
}

# Order in which corrective sets are applied (deterministic, mirrors the legacy
# command order so any ordering dependence is preserved).
_APPLY_ORDER = ["SLPDISCONNECT", "TIMER", "SENDS", "SENALM", "MOVING",
                "GPS_RST_TIME", "VIBCHK", "ACCLINE", "HBT"]


def _norm(v):
    """Normalise a setting value for comparison."""
    if v is None:
        return None
    return str(v).strip().lower()


def _atomic_write_json(path, obj):
    """Write JSON to `path` atomically via a same-dir temp + os.replace, with a
    plain-write fallback if the filesystem rejects the rename (e.g. EXDEV on an
    overlay/bind mount, as in some test sandboxes)."""
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    try:
        os.replace(tmp, path)
    except OSError:
        # Cross-device or other rename failure — fall back to a direct write.
        with open(path, "w") as f:
            json.dump(obj, f, indent=2)
        try:
            os.remove(tmp)
        except OSError:
            pass


def voltage_to_percent(voltage):
    """Convert voltage to battery percentage using linear interpolation."""
    table = _W07C_DISCHARGE
    if voltage >= table[0][0]:
        return 100
    if voltage < table[-1][0]:
        return 0
    for i in range(len(table) - 1):
        v_hi, p_hi = table[i]
        v_lo, p_lo = table[i + 1]
        if voltage >= v_lo:
            frac = (voltage - v_lo) / (v_hi - v_lo)
            return round(p_lo + frac * (p_hi - p_lo))
    return 0


def _default_log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} {msg}")


def gt06_crc_itu(data):
    """CRC-ITU (CRC-16/X.25): polynomial 0x8408 reflected, init 0xFFFF."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0x8408
            else:
                crc >>= 1
    return crc ^ 0xFFFF


def gt06_make_response(protocol, serial):
    """Build a GT06 response frame: 78 78 05 [protocol] [serial] [crc] 0d 0a."""
    payload = struct.pack(">BBH", 0x05, protocol, serial)
    crc = gt06_crc_itu(payload)
    return b"\x78\x78" + payload + struct.pack(">H", crc) + b"\x0d\x0a"


def gt06_make_command(cmd_str, cmd_serial):
    """Build a GT06 server command frame (protocol 0x80).

    Format: 78 78 [len] 80 [content_len] [server_flag 4B] [cmd ASCII] [serial] [crc] 0d 0a
    """
    cmd_bytes = cmd_str.encode("ascii")
    content_len = 4 + len(cmd_bytes)
    # length = protocol(1) + content_len_field(1) + server_flag(4) + cmd + serial(2) + crc(2)
    length = 1 + 1 + content_len + 2 + 2
    payload = struct.pack(">BB", length, 0x80)
    payload += struct.pack(">B", content_len)
    payload += b"\x00\x00\x00\x00"  # server flag
    payload += cmd_bytes
    payload += struct.pack(">H", cmd_serial)
    crc = gt06_crc_itu(payload)
    return b"\x78\x78" + payload + struct.pack(">H", crc) + b"\x0d\x0a"


def gt06_parse_login(data):
    """Parse login packet (protocol 0x01). Data is IMEI in BCD encoding."""
    imei = data.hex()
    imei = imei.lstrip("0")
    if len(imei) == 16:
        imei = imei[1:]  # Remove leading nibble padding for 15-digit IMEI
    return imei


def gt06_parse_location(data):
    """Parse location packet (protocol 0x12 or 0x22).

    Returns dict with lat, lon, speed_kmh, heading, satellites, gps_valid, ts.
    """
    if len(data) < 18:
        return None

    yy, mo, dd, hh, mi, ss = data[0:6]
    gps_info = data[6]
    sat_count = gps_info & 0x0F

    lat_raw = struct.unpack(">I", data[7:11])[0]
    lon_raw = struct.unpack(">I", data[11:15])[0]

    speed_kmh = data[15]

    course_status = struct.unpack(">H", data[16:18])[0]
    heading = course_status & 0x03FF
    is_west = bool(course_status & (1 << 11))
    is_south = not bool(course_status & (1 << 10))
    gps_valid = bool(course_status & (1 << 12))

    lat = lat_raw / 1_800_000.0
    lon = lon_raw / 1_800_000.0

    if is_south:
        lat = -lat
    if is_west:
        lon = -lon

    # Convert GT06 datetime (UTC) to unix timestamp
    try:
        ts = timegm((2000 + yy, mo, dd, hh, mi, ss))
    except Exception:
        ts = int(time.time())

    return {
        "lat": lat,
        "lon": lon,
        "speed_kmh": speed_kmh,
        "heading": heading,
        "satellites": sat_count,
        "gps_valid": gps_valid,
        "ts": ts,
    }


def gt06_parse_alarm_status(data):
    """Parse alarm packet extended fields after the 18-byte location block.

    Alarm packets (protocol 0x16/0x23) contain GPS data (18 bytes) followed by:
    LBS length (1) + LBS data + terminal_info (1) + voltage (1) + signal (1) + alarm/lang (2).

    Returns dict with is_sos, alarm_type, battery, signal, charging, or None on parse error.
    """
    if len(data) <= 18:
        return None
    extra = data[18:]
    if len(extra) < 1:
        return None
    lbs_len = extra[0]
    after_lbs = extra[1 + lbs_len:] if len(extra) > 1 + lbs_len else b""
    if len(after_lbs) < 1:
        return None
    ti = after_lbs[0]
    alarm_bits = (ti >> 3) & 0x07
    _ALARM_NAMES = {0: "Normal", 1: "Shock", 2: "Power Cut", 3: "Low Battery", 4: "SOS"}
    result = {
        "is_sos": alarm_bits == 4,
        "alarm_type": _ALARM_NAMES.get(alarm_bits, f"Unknown({alarm_bits})"),
        "charging": bool(ti & 0x04),
    }
    if len(after_lbs) >= 2:
        result["battery"] = _GT06_BATTERY_MAP.get(after_lbs[1], 0)
    if len(after_lbs) >= 3:
        result["signal"] = min(after_lbs[2], 4)
    return result


def gt06_parse_heartbeat(data):
    """Parse heartbeat packet (protocol 0x13).

    Returns dict with battery (0-100), signal (0-4), charging (bool).
    """
    result = {}
    if len(data) >= 1:
        info = data[0]
        result["charging"] = bool(info & 0x04)
    if len(data) >= 2:
        vlevel = data[1]
        result["battery"] = _GT06_BATTERY_MAP.get(vlevel, 0)
    if len(data) >= 3:
        result["signal"] = min(data[2], 4)
    return result


def load_gt06_config(config_path: Path, log_func=None) -> dict:
    """Load GT06 device config from JSON file.

    Returns {"default_eid": int, "devices": {imei: {...}}}.
    If file doesn't exist, returns defaults.
    """
    _log = log_func or _default_log
    default = {"default_eid": 1, "devices": {}}
    if not config_path.exists():
        return default
    try:
        with open(config_path) as f:
            cfg = json.load(f)
        result = {
            "default_eid": cfg.get("default_eid", 1),
            "idle_hbt_interval": cfg.get("idle_hbt_interval", 15),
            "idle_poll_interval": cfg.get("idle_poll_interval", 60),
            "idle_loc_interval": cfg.get("idle_loc_interval",
                                         cfg.get("idle_hbt_interval", 15)),
            "idle_keepalive_interval": cfg.get("idle_keepalive_interval",
                                               cfg.get("idle_poll_interval", 60)),
            "firmware_overrides": cfg.get("firmware_overrides", {}),
            "overnight_interval_min": cfg.get("overnight_interval_min", 15),
            # 4 = MODE4 (vendor-recommended 2026-05, vibration-responsive
            # but ACCLINE=1 in the chain suppresses spurious wakes);
            # 5 = MODE5 (strictly scheduled, no vibration wake).
            "overnight_mode_number": cfg.get("overnight_mode_number", 4),
            "slow_speed_knots": cfg.get("slow_speed_knots", 2),
            "slow_speed_seconds": cfg.get("slow_speed_seconds", 20),
            "slow_loc_interval": cfg.get("slow_loc_interval", 3),
            "devices": cfg.get("devices", {}),
        }
        _log(f"[GT06] Loaded config from {config_path}: {len(result['devices'])} device(s), default_eid={result['default_eid']}")
        return result
    except Exception as e:
        _log(f"[GT06] Warning: Could not load {config_path}: {e}")
        return default


class GT06Connection:
    """State for one GT06 TCP connection."""

    def __init__(self, sock, addr, conn_id=0):
        self.sock = sock
        self.addr = addr
        self.conn_id = conn_id   # unique per-connection ID for gt06.log v2 format
        self.buf = b""
        self.imei = None
        self.sailor_id = None
        self.eid = None
        self.battery = -1
        self.signal = -1
        self.charging = None
        self.battery_voltage = None             # actual voltage from STATUS (float)
        self.last_status_time = time.monotonic()  # monotonic time of last STATUS# send
        self.status_miss_count = 0
        self.cmd_serial = 0
        self.assist_active = False
        self.idle = False
        self.last_lat = None
        self.last_lon = None
        self.last_ts = None
        self.pos_history = []  # last 3 (lat, lon, ts) for speed smoothing

        # Command queue — sequential delivery with SIOCOUTQ verification
        self.cmd_queue = []           # list of command strings waiting to send
        self.cmd_pending = None       # command string currently awaiting TCP ACK
        self.cmd_pending_frame = None # raw frame bytes (for retry)
        self.cmd_sent_time = 0        # time.monotonic() when cmd was sent
        self.cmd_tcp_acked = False    # True once SIOCOUTQ shows 0
        self.cmd_tcp_ack_time = 0    # time.monotonic() when TCP ACK confirmed

        # Rate monitoring — detect when device ignores commands
        self.rate_check_time = 0      # monotonic time when current rate window started
        self.loc_count = 0            # LOC packets received in current window
        self.hbt_count = 0            # HBT packets received in current window
        self.last_hbt_time = 0        # monotonic time of last heartbeat
        self.expected_loc_interval = 60  # expected LOC interval (updated on state change)
        self.expected_hbt_interval = 15  # expected HBT interval
        self.rate_retry_count = 0     # how many times we've re-sent commands for wrong rate

        # Slow-mode LOC rate — reduce TX when speed is below threshold
        self.slow_mode = False        # currently in slow LOC mode
        self.slow_since = 0           # monotonic time speed first dropped below threshold

        # Monotonic time of the accept(); used to expire connections that
        # accept a TCP socket but never send a LOGIN frame, holding a
        # file descriptor indefinitely.
        self.connected_at = time.monotonic()

        # Some W07C firmware revisions send LOC with course_status=0x0000 even
        # when they have a real fix. Track whether we've logged the workaround.
        self.stale_fix_warned = False

        # Mode the server intends this device to be in (1 = MODE1 race-day /
        # active, 5 = MODE5 overnight scheduled-wake). Set by the login
        # handler and set_idle(); enforced in the cxzt# response handler so
        # that a stale device-side mode (e.g. MODE5 left over from sleep
        # before a /admin/start-all) gets corrected via a MODE1/MODE5 push.
        self.desired_mode = 1
        # overnight: True when this device is in OVERNIGHT idle (deep sleep)
        # intent. The effective MODE is firmware-dependent (V667 -> MODE4,
        # W07/V6.6x -> MODE1 long-TIMER) and resolved in the cxzt# handler.
        self.overnight = False
        # Storm guard counter for overnight Freq re-pushes (see
        # OVERNIGHT_FREQ_MAX_RETRIES).
        self.overnight_freq_retries = 0
        # When set (active login), the cxzt# handler applies the active config
        # (_active_cmds) only if the device isn't already running it — avoids
        # re-bootstrapping (GPS_RST_TIME/SENDS toggles) a tracker that is
        # already reporting on a mid-race reconnect. Cleared once handled.
        self.want_active_interval = None
        # Table-driven settings reconciler (query -> diff -> apply).
        self.target_state = None      # "active" | "idle" we're reconciling to
        self.observed = {}            # settings read back this connection
        self.reconcile_phase = None   # None | "query" | "apply"

        # Last time we received ANY frame from the device. Used as the
        # liveness signal for the no-HBT-disconnect check, so a device that
        # responds to commands or sends LOCs counts as alive even if its HB
        # scheduler is broken/asleep.
        self.last_alive_time = 0

        # Last time the periodic loop sent an idle-mode keepalive probe
        # (STATUS#) to this device. Some W07C firmware revisions ACK the
        # HBT command but never actually emit heartbeats — without an
        # active probe the connection would die at the no-traffic timeout.
        self.last_idle_poll_time = 0

        # Firmware version string reported by the device in response to
        # VERSION# (e.g. "NT19D_MG133_10F8G_B53_V667 2026-04-13"). Captured
        # at login and exposed for the device-management UI.
        self.firmware = None

    def next_cmd_serial(self):
        self.cmd_serial += 1
        return self.cmd_serial


class GT06Listener:
    """Non-blocking TCP listener for GT06 GPS tracker devices.

    Runs in a single daemon thread using selectors. Calls process_position()
    on the appropriate EventTracker/PositionTracker when location data arrives.

    Dependency injection parameters:
      - get_tracker_func: callable(eid) -> tracker object
      - log_func: callable(msg) for logging
      - save_overrides_func: callable(users_file, overrides) to persist user overrides
      - write_positions_func: callable(positions, path, overrides, tails) to write positions JSON
    """

    def __init__(self, port, interval, id_prefix, get_tracker_func, gt06_config=None,
                 log_file=None, log_func=None, save_overrides_func=None,
                 write_positions_func=None, get_event_state_func=None,
                 get_event_idle_submode_func=None, gt06_config_path=None):
        self.port = port
        self.interval = interval
        self.id_prefix = id_prefix
        self.get_tracker = get_tracker_func
        self.get_event_state = get_event_state_func  # callable(eid) -> "tracking" | "idle"
        # callable(eid) -> "race" | "overnight" — when an idle tracker
        # reconnects, picks the command set. Default "race" if unset.
        self.get_event_idle_submode = get_event_idle_submode_func
        self.gt06_config = gt06_config or {"default_eid": 1, "idle_hbt_interval": 15, "devices": {}}
        self.idle_hbt_interval = self.gt06_config.get("idle_hbt_interval", 15)
        self.idle_poll_interval = self.gt06_config.get("idle_poll_interval", 60)
        # Daytime-idle LOC cadence, decoupled from heartbeat (Phase B). Falls
        # back to idle_hbt_interval so behaviour is unchanged until set.
        self.idle_loc_interval = self.gt06_config.get(
            "idle_loc_interval", self.idle_hbt_interval)
        # STATUS# keepalive cadence; 0 disables. Defaults to idle_poll_interval.
        self.idle_keepalive_interval = self.gt06_config.get(
            "idle_keepalive_interval", self.idle_poll_interval)
        # Per-firmware-prefix override table (see _resolve_setting).
        self.firmware_overrides = self.gt06_config.get("firmware_overrides", {})
        # Overnight (deep-sleep) wake interval in MINUTES. Same human-facing
        # value regardless of which overnight mode is in use; _overnight_cmds
        # converts to the on-wire unit (seconds for MODE4, minutes for MODE5).
        self.overnight_interval_min = self.gt06_config.get("overnight_interval_min", 15)
        # Overnight mode number — 4 (MODE4, vendor-recommended 2026-05) or
        # 5 (MODE5). Live-flippable: edit gt06.json and restart, trackers
        # currently in the other mode will migrate on their next cxzt# poll
        # via the mode-mismatch handler in the 0x15 path.
        self.overnight_mode_number = self.gt06_config.get("overnight_mode_number", 4)
        self.slow_speed_knots = self.gt06_config.get("slow_speed_knots", 2)
        self.slow_speed_seconds = self.gt06_config.get("slow_speed_seconds", 20)
        self.slow_loc_interval = self.gt06_config.get("slow_loc_interval", 3)
        self.connections = {}  # fd -> GT06Connection
        self.sel = selectors.DefaultSelector()
        self.log_file = log_file
        self._log_fd = None
        self._next_conn_id = 1   # monotonic per-connection ID for gt06.log v2 format
        self.idle_sailors = set()
        self.active_sailors = set()
        self._sticky_assist: set = set()
        self._log = log_func or _default_log
        self._save_overrides = save_overrides_func
        self._write_positions = write_positions_func
        # Path to gt06.json so the management page can persist per-device config
        # (event assignment, overnight mode). None disables config writes.
        self.gt06_config_path = Path(gt06_config_path) if gt06_config_path else None
        # Persisted per-device state (firmware, last cxzt# snapshot, battery,
        # last-seen) so the management page shows offline devices too. Sidecar
        # gt06_state.json next to the config; survives restart.
        self.device_state = self._load_device_state()
    def _log_packet(self, gt_conn, frame, outgoing=False):
        """Log a raw GT06 frame with v2 header (ts + conn_id + length).

        conn_id high bit indicates direction (1 = server→device, 0 = device→server).
        """
        if self._log_fd is None:
            return
        ts = time.time()
        conn_id = (gt_conn.conn_id if gt_conn else 0)
        if outgoing:
            conn_id |= GT06_LOG_DIR_OUT
        header = struct.pack("<dIH", ts, conn_id & 0xFFFFFFFF, len(frame))
        try:
            self._log_fd.write(header + frame)
            self._log_fd.flush()
        except Exception as e:
            self._log(f"[GT06] Packet log write error: {e}")

    def _imei_to_sailor_id(self, imei):
        """Map IMEI to sailor_id: prefix + last 6 digits."""
        return self.id_prefix + imei[-6:]

    def _accept(self, server_sock):
        """Accept a new GT06 connection."""
        conn, addr = server_sock.accept()
        conn.setblocking(False)
        fd = conn.fileno()
        conn_id = self._next_conn_id & 0x7FFFFFFF  # keep below 2^31 (bit 31 = direction)
        self._next_conn_id = (self._next_conn_id + 1) & 0x7FFFFFFF
        if self._next_conn_id == 0:
            self._next_conn_id = 1  # never reuse 0 (reserved for "no connection")
        gt_conn = GT06Connection(conn, addr, conn_id=conn_id)
        self.connections[fd] = gt_conn
        self.sel.register(conn, selectors.EVENT_READ, data=fd)
        self._log(f"[GT06] Connection from {addr[0]}:{addr[1]} (conn_id={conn_id})")

    def _disconnect(self, fd):
        """Clean up a disconnected GT06 connection."""
        gt_conn = self.connections.pop(fd, None)
        if gt_conn is None:
            return
        try:
            self.sel.unregister(gt_conn.sock)
        except Exception:
            pass
        try:
            gt_conn.sock.close()
        except Exception:
            pass
        label = gt_conn.sailor_id or gt_conn.imei or "unknown"
        self._log(f"[GT06] Disconnected: {label} ({gt_conn.addr[0]}:{gt_conn.addr[1]})")

    def _send(self, gt_conn, data):
        """Best-effort send to a GT06 connection."""
        try:
            gt_conn.sock.sendall(data)
            self._log_packet(gt_conn, data, outgoing=True)
        except Exception as e:
            self._log(f"[GT06] Send error to {gt_conn.addr}: {e}")
            self._disconnect(gt_conn.sock.fileno())

    # -- Command queue: sequential delivery with SIOCOUTQ verification --

    def _queue_commands(self, gt_conn, cmds):
        """Queue command strings for sequential delivery to a GT06 device."""
        gt_conn.cmd_queue.extend(cmds)
        self._send_next_cmd(gt_conn)

    def _send_next_cmd(self, gt_conn):
        """Send the next queued command if none is pending."""
        if gt_conn.cmd_pending is not None:
            return  # waiting for current command
        if not gt_conn.cmd_queue:
            # Queue drained — let the reconciler advance its phase (this may
            # queue the corrective sets), then fall through to send them.
            self._reconcile_advance(gt_conn)
            if not gt_conn.cmd_queue:
                return
        cmd_str = gt_conn.cmd_queue.pop(0)
        frame = gt06_make_command(cmd_str, gt_conn.next_cmd_serial())
        self._send(gt_conn, frame)
        gt_conn.cmd_pending = cmd_str
        gt_conn.cmd_pending_frame = frame
        gt_conn.cmd_sent_time = time.monotonic()
        gt_conn.cmd_tcp_acked = False
        label = gt_conn.sailor_id or gt_conn.imei or "unknown"
        self._log(f"[GT06] Sent to {label}: {cmd_str} (queue: {len(gt_conn.cmd_queue)} remaining)")

    # ---- Settings reconciler (table/state driven) ----------------------

    def _desired_settings(self, gt_conn, state):
        """{key: value} the device should hold in `state`. Reproduces the legacy
        _active_cmds / _idle_cmds output exactly (MODE handled separately in the
        cxzt# path)."""
        if state == "active":
            interval = self.slow_loc_interval if gt_conn.slow_mode else self.interval
            return {"SLPDISCONNECT": 0, "TIMER": interval, "SENDS": 0,
                    "GPS_RST_TIME": 300, "VIBCHK": "0:16", "HBT": 15}
        if state == "idle":
            return {"SLPDISCONNECT": 0, "TIMER": self.idle_loc_interval, "SENDS": 1,
                    "SENALM": "OFF", "MOVING": "OFF", "GPS_RST_TIME": 0,
                    "VIBCHK": "0:16", "HBT": self.idle_hbt_interval}
        return {}

    def _reconcile_begin(self, gt_conn, state):
        """Start a query->diff->apply reconcile for `state`: queue a full read of
        every setting; the diff/apply runs when the query queue drains."""
        gt_conn.target_state = state
        gt_conn.observed = {}
        gt_conn.reconcile_phase = "query"
        queries, seen = ["cxzt#"], set()
        for key in _APPLY_ORDER:
            q = _SETTINGS[key][1]
            if q and q not in seen:
                queries.append(q)
                seen.add(q)
        self._queue_commands(gt_conn, queries)

    def _reconcile_advance(self, gt_conn):
        """Called when the command queue drains; drives the reconcile phases."""
        phase = getattr(gt_conn, "reconcile_phase", None)
        if phase == "query":
            gt_conn.reconcile_phase = "apply"
            self._reconcile_apply(gt_conn)
        elif phase == "apply":
            gt_conn.reconcile_phase = None
            label = gt_conn.sailor_id or gt_conn.imei or "unknown"
            self._log(f"[GT06] {label} reconcile complete "
                      f"({getattr(gt_conn, 'target_state', '?')})")

    def _reconcile_apply(self, gt_conn):
        """Diff observed vs desired; queue corrective sets for mismatches only."""
        desired = self._desired_settings(gt_conn, getattr(gt_conn, "target_state", None))
        observed = getattr(gt_conn, "observed", {})
        label = gt_conn.sailor_id or gt_conn.imei or "unknown"
        obs = " ".join(f"{k}={observed.get(k, '?')}"
                       for k in _APPLY_ORDER if k in desired)
        self._log(f"[GT06] {label} observed [{getattr(gt_conn,'target_state','?')}]: {obs}")
        cmds = [_SETTINGS[k][0](desired[k]) for k in _APPLY_ORDER
                if k in desired and _norm(observed.get(k)) != _norm(desired[k])]
        if cmds:
            self._log(f"[GT06] {label} applying: {' '.join(cmds)}")
            self._queue_commands(gt_conn, cmds)
        else:
            self._log(f"[GT06] {label} all settings already correct")

    def _reconcile_parse(self, gt_conn, text):
        """Parse a command response into gt_conn.observed while reconciling."""
        if getattr(gt_conn, "reconcile_phase", None) is None:
            return
        obs = gt_conn.observed
        if 'MCU:' in text:  # cxzt#: F=TIMER loc freq, H=HBT
            m = re.search(r'\*F:(\d+)', text)
            if m: obs["TIMER"] = int(m.group(1))
            m = re.search(r'\*H:(\d+)', text)
            if m: obs["HBT"] = int(m.group(1))
        if 'TIMER:' in text and ';' in text:  # PARAM#
            for k, pat in (("TIMER", r'TIMER:(\d+)'), ("SENDS", r'SENDS:(\d+)'),
                           ("HBT", r'HBT:(\d+)')):
                m = re.search(pat, text)
                if m: obs[k] = int(m.group(1))
        m = re.search(r'(?:READOK|SETOK):\s*([A-Z_]+)=(\S+)', text)  # CXCS read / set ack
        if m: obs[m.group(1)] = m.group(2)
        m = re.search(r'\bSENALM:(\w+)', text)
        if m: obs["SENALM"] = m.group(1)
        m = re.search(r'\bMOVING:(\w+)', text)
        if m: obs["MOVING"] = m.group(1)
        m = re.search(r'\bSENDS:(\d+)', text)
        if m: obs["SENDS"] = int(m.group(1))

    def _check_siocoutq(self, gt_conn):
        """Check if all outbound TCP data has been ACKed by the remote TCP stack.
        Returns True if the kernel send buffer is empty (SIOCOUTQ == 0)."""
        SIOCOUTQ = 0x5411
        try:
            buf = fcntl.ioctl(gt_conn.sock.fileno(), SIOCOUTQ, b'\x00\x00\x00\x00')
            pending = struct.unpack("I", buf)[0]
            return pending == 0
        except Exception:
            return False

    def _check_cmd_delivery(self, fd, gt_conn, now):
        """Check SIOCOUTQ and timeouts for a pending command."""
        if not gt_conn.cmd_tcp_acked:
            if self._check_siocoutq(gt_conn):
                gt_conn.cmd_tcp_acked = True
                gt_conn.cmd_tcp_ack_time = now
                label = gt_conn.sailor_id or gt_conn.imei or "unknown"
                self._log(f"[GT06] TCP ACK confirmed for {label}: {gt_conn.cmd_pending}")
            elif now - gt_conn.cmd_sent_time > 30:
                # TCP can't deliver the data — connection dead/very laggy
                label = gt_conn.sailor_id or gt_conn.imei or "unknown"
                self._log(f"[GT06] TCP delivery timeout for {label}: {gt_conn.cmd_pending} — disconnecting")
                self._disconnect(fd)
                return
        else:
            # TCP delivered but no 0x15 app ACK — not all commands produce one
            # (e.g. SUP doesn't ACK). Advance queue after 10s.
            if now - gt_conn.cmd_tcp_ack_time > 10:
                label = gt_conn.sailor_id or gt_conn.imei or "unknown"
                self._log(f"[GT06] No app ACK for {label}: {gt_conn.cmd_pending} — advancing queue")
                gt_conn.cmd_pending = None
                gt_conn.cmd_pending_frame = None
                self._send_next_cmd(gt_conn)

    def _resolve_setting(self, gt_conn, key, default=None):
        """Resolve a tuning setting with precedence:

            per-device (devices.{imei})  >  per-firmware-prefix  >  global

        The firmware-prefix layer is skipped until the device's firmware is
        known (first cxzt#/VERSION#), which is when overnight mode is chosen.
        """
        devices = self.gt06_config.get("devices", {})
        if gt_conn is not None and gt_conn.imei:
            dev = devices.get(gt_conn.imei)
            if isinstance(dev, dict) and key in dev:
                return dev[key]
        fw = getattr(gt_conn, "firmware", None) if gt_conn is not None else None
        if fw:
            for prefix, overrides in self.firmware_overrides.items():
                if (fw.startswith(prefix) and isinstance(overrides, dict)
                        and key in overrides):
                    return overrides[key]
        return self.gt06_config.get(key, default)

    def _reset_rate_monitoring(self, gt_conn, expected_loc_interval):
        """Reset rate monitoring counters after a state transition.

        Also resets last_hbt_time so the periodic HBT-gap check uses a fresh
        grace period after the transition. Without this, switching from idle
        (expected_hbt_interval=300) to active (expected_hbt_interval=15) would
        instantly trip the "no heartbeat for N seconds — disconnecting" check
        because the last HB arrived correctly per the old idle interval but is
        now compared against the much shorter active threshold.
        """
        now = time.monotonic()
        gt_conn.rate_check_time = now
        gt_conn.loc_count = 0
        gt_conn.hbt_count = 0
        gt_conn.expected_loc_interval = expected_loc_interval
        gt_conn.last_hbt_time = now
        gt_conn.last_alive_time = now
        gt_conn.rate_retry_count = 0

    def _check_rates(self, fd, gt_conn, now):
        """Check LOC/HBT rates and retry or disconnect if device ignores commands."""
        if gt_conn.rate_check_time == 0:
            return
        # Don't check rates while commands are still being delivered
        if gt_conn.cmd_queue or gt_conn.cmd_pending is not None:
            gt_conn.rate_check_time = now  # reset grace period
            gt_conn.loc_count = 0
            return

        elapsed = now - gt_conn.rate_check_time
        if elapsed < 30:
            return  # grace period

        # Check LOC rate
        expected_loc_rate = 1.0 / gt_conn.expected_loc_interval
        actual_loc_rate = gt_conn.loc_count / elapsed if elapsed > 0 else 0
        label = gt_conn.sailor_id or gt_conn.imei or "unknown"

        loc_rate_wrong = False
        if gt_conn.expected_loc_interval >= 60:
            # Idle: flag if rate is more than 3x expected (getting too many packets)
            if actual_loc_rate > expected_loc_rate * 3:
                loc_rate_wrong = True
        else:
            # Tracking: flag if rate is less than 0.3x expected (not enough packets)
            if actual_loc_rate < expected_loc_rate * 0.3 and gt_conn.loc_count > 0:
                loc_rate_wrong = True

        if loc_rate_wrong:
            # Overnight idle (MODE5): the device wakes briefly every ~15
            # min and pushes a couple of heartbeats in a ~30s window, which
            # always trips the rate-mismatch check. Pushing _idle_cmds()
            # here sends TIMER,540,540# which overwrites MODE5's Freq:15
            # to Freq:540, breaking the 15-min wake cadence. Suppress the
            # whole rate-mismatch path for MODE5 — the wake cycles are by
            # design infrequent and a couple of LOC per wake is expected.
            if gt_conn.desired_mode == self.overnight_mode_number:
                gt_conn.rate_check_time = now
                gt_conn.loc_count = 0
                gt_conn.hbt_count = 0
                gt_conn.rate_retry_count = 0
                return
            if gt_conn.rate_retry_count < 2:
                gt_conn.rate_retry_count += 1
                if gt_conn.idle:
                    cmds = _idle_cmds(self.idle_loc_interval)
                else:
                    cmds = _active_cmds(self.slow_loc_interval if gt_conn.slow_mode else self.interval)
                self._log(f"[GT06] Rate mismatch for {label}: "
                    f"expected {expected_loc_rate:.3f}/s, actual {actual_loc_rate:.3f}/s "
                    f"({gt_conn.loc_count} LOC in {elapsed:.0f}s) — "
                    f"retry {gt_conn.rate_retry_count}/2")
                gt_conn.cmd_queue.clear()
                gt_conn.cmd_pending = None
                self._queue_commands(gt_conn, cmds)
                gt_conn.rate_check_time = now
                gt_conn.loc_count = 0
                gt_conn.hbt_count = 0
            else:
                if not gt_conn.idle:
                    self._log(f"[GT06] Rate mismatch for {label} after 2 retries — disconnecting")
                    self._disconnect(fd)
                    return
                else:
                    self._log(f"[GT06] Rate mismatch for {label} after 2 retries (idle, not disconnecting)")
                    gt_conn.rate_retry_count = 0
                    gt_conn.rate_check_time = now
                    gt_conn.loc_count = 0

        # Check HBT rate — if no heartbeat for > 3x expected interval and we're getting LOC
        if gt_conn.last_hbt_time > 0 and gt_conn.loc_count > 0:
            hbt_gap = now - gt_conn.last_hbt_time
            if hbt_gap > gt_conn.expected_hbt_interval * 3:
                hbt_int = self.idle_hbt_interval if gt_conn.idle else 15
                self._log(f"[GT06] No heartbeat from {label} for {hbt_gap:.0f}s — re-queuing HBT")
                self._queue_commands(gt_conn, [f"HBT,{hbt_int},{hbt_int}#"])
                gt_conn.last_hbt_time = now  # prevent repeated re-queuing

    def _process_frame(self, fd, frame):
        """Process a complete GT06 frame."""
        gt_conn = self.connections.get(fd)
        if gt_conn is None:
            return

        protocol = frame[3]
        length = frame[2]
        crc_offset = 3 + length - 2
        serial_offset = 3 + length - 4
        crc_received = struct.unpack(">H", frame[crc_offset:crc_offset + 2])[0]
        serial = struct.unpack(">H", frame[serial_offset:serial_offset + 2])[0]
        crc_calc = gt06_crc_itu(frame[2:crc_offset])

        if crc_received != crc_calc:
            self._log(f"[GT06] CRC mismatch from {gt_conn.addr}: "
                f"received 0x{crc_received:04X}, calculated 0x{crc_calc:04X}")
            return

        data = frame[4:serial_offset]

        if protocol == 0x01:
            # Login
            imei = gt06_parse_login(data)
            gt_conn.imei = imei
            gt_conn.sailor_id = self._imei_to_sailor_id(imei)

            # Look up IMEI in gt06_config for event routing.
            # Sim convention: IMEIs starting with 999 carry the eid in
            # positions 3..5 (two decimal digits), so WebUI-launched sim
            # fleets route to their owning event without any config edits.
            # Real GT06 hardware never uses 999 as a TAC prefix.
            dev_cfg = self.gt06_config["devices"].get(imei, {})
            sim_eid = None
            if "eid" not in dev_cfg and imei.startswith("999") and len(imei) >= 5:
                try:
                    sim_eid = int(imei[3:5])
                except ValueError:
                    sim_eid = None
            gt_conn.eid = dev_cfg.get(
                "eid",
                sim_eid if sim_eid is not None else self.gt06_config["default_eid"])
            self._log(f"[GT06] Login: IMEI {imei} -> {gt_conn.sailor_id} (eid={gt_conn.eid})")
            self._send(gt_conn, gt06_make_response(protocol, serial))

            # Close any stale connections for the same device
            my_fd = gt_conn.sock.fileno()
            stale_fds = [fd for fd, c in self.connections.items()
                         if c.sailor_id == gt_conn.sailor_id and fd != my_fd]
            for fd in stale_fds:
                self._log(f"[GT06] Closing stale connection for {gt_conn.sailor_id}")
                self._disconnect(fd)

            # Apply device name from gt06.json if configured (keyed by did:IMEI)
            dev_name = dev_cfg.get("name")
            if dev_name:
                tracker = self.get_tracker(gt_conn.eid)
                if tracker and hasattr(tracker, 'user_overrides') and hasattr(tracker, 'users_file'):
                    overrides = tracker.user_overrides
                    did_key = f"did:{imei}"
                    existing = overrides.get(did_key)
                    if not existing or existing.get("name") != dev_name or existing.get("_last_id") != gt_conn.sailor_id:
                        overrides[did_key] = {"name": dev_name, "_last_id": gt_conn.sailor_id}
                        # Remove old sailor_id entry if present
                        if gt_conn.sailor_id in overrides:
                            del overrides[gt_conn.sailor_id]
                        if self._save_overrides:
                            self._save_overrides(tracker.users_file, overrides)
                        self._log(f"[GT06] Set display name for {gt_conn.sailor_id} (did:{imei}): {dev_name}")

            # Determine idle/active state for this device. Precedence:
            #   1. In-session per-sailor explicit override (active_sailors /
            #      idle_sailors) — only populated by set_idle() calls, i.e.
            #      explicit operator actions during this server run.
            #   2. Explicit event-scope state (set via /admin/start-all,
            #      /admin/stop-all, or /admin/state). This represents the
            #      current operator intent for the whole event and overrides
            #      any per-sailor state that wasn't explicitly chosen.
            #   3. Persisted per-sailor idle from current_positions (a fallback
            #      for trackers seen before any event-level intent was set).
            #   4. Default: idle.
            #
            # Note: we intentionally do NOT auto-add this sailor to
            # idle_sailors/active_sailors. Those sets represent explicit
            # operator choices; auto-population would block event_state from
            # taking effect on later reconnects of the same tracker.
            event_state = None
            if self.get_event_state:
                try:
                    event_state = self.get_event_state(gt_conn.eid)
                except Exception:
                    event_state = None

            saved_idle = None
            saved_sleep = False  # persisted per-sailor SLEEP (overnight) flag
            tracker_for_lookup = self.get_tracker(gt_conn.eid)
            if tracker_for_lookup is not None:
                pt = (tracker_for_lookup.position_tracker
                      if hasattr(tracker_for_lookup, "position_tracker")
                      else tracker_for_lookup)
                with pt._lock:
                    existing_pos = pt.current_positions.get(gt_conn.sailor_id)
                if existing_pos is not None:
                    if "idle" in existing_pos:
                        saved_idle = bool(existing_pos["idle"])
                    saved_sleep = bool(existing_pos.get("sleep", False))

            sailor_key = (gt_conn.eid, gt_conn.sailor_id)
            if sailor_key in self.active_sailors:
                use_active = True
            elif sailor_key in self.idle_sailors:
                use_active = False
            elif event_state == "tracking":
                use_active = True
            elif event_state == "idle":
                use_active = False
            elif saved_idle is not None:
                use_active = not saved_idle
            else:
                use_active = False  # default: idle

            if use_active:
                gt_conn.idle = False
                gt_conn.overnight = False
                gt_conn.desired_mode = 1
                # Table/state-driven reconcile: query every setting, then apply
                # only the ones that are wrong. The reconciler queues cxzt#
                # first, which also drives the one-shot MODE1 switch in the cxzt#
                # handler. A mid-race reconnect of an already-configured tracker
                # finds everything correct and sends nothing (no GPS_RST/SENDS
                # churn) — the result the old want_active_interval gating gave,
                # now general across all settings.
                gt_conn.expected_hbt_interval = 15
                self._reset_rate_monitoring(gt_conn, self.interval)
                self._reconcile_begin(gt_conn, "active")
                cmds = None
            else:
                gt_conn.idle = True
                # Pick race-day idle vs overnight idle. Per-sailor saved_sleep
                # (set by a previous /admin/sleep, persisted in
                # current_positions.json) wins — that's the explicit per-
                # tracker choice and survives server restart. Otherwise fall
                # back to event-scope idle_submode (default "race").
                if saved_sleep:
                    idle_submode = "overnight"
                else:
                    idle_submode = "race"
                    if self.get_event_idle_submode:
                        try:
                            idle_submode = self.get_event_idle_submode(gt_conn.eid) or "race"
                        except Exception:
                            idle_submode = "race"
                if idle_submode == "overnight":
                    gt_conn.overnight = True
                    gt_conn.overnight_freq_retries = 0
                    gt_conn.desired_mode = self.overnight_mode_number
                    # Overnight: queue ONLY cxzt# probe. If the device is
                    # already in the right MODE (it usually is — wake-cycle
                    # reconnects land here), the cxzt# handler will see
                    # M:overnight_mode == desired and do nothing; device
                    # just sleeps again on its own cadence. Otherwise the
                    # handler pushes the full _overnight_cmds chain
                    # (SLPDISCONNECT, ACCLINE, MODE{4|5}). This avoids the
                    # re-push storm where every wake reset the timer back
                    # to the start of its period.
                    cmds = ["cxzt#"]
                    gt_conn.expected_hbt_interval = self.overnight_interval_min * 60
                    self._reset_rate_monitoring(gt_conn, self.overnight_interval_min * 60)
                else:
                    gt_conn.overnight = False
                    gt_conn.desired_mode = 1
                    # Race-day idle: table/state-driven reconcile (see active).
                    gt_conn.expected_hbt_interval = self.idle_hbt_interval
                    self._reset_rate_monitoring(gt_conn, self.idle_loc_interval)
                    self._reconcile_begin(gt_conn, "idle")
                    cmds = None
            if cmds:
                self._queue_commands(gt_conn, cmds)
            self._log(f"[GT06] Login commands queued ({'active' if not gt_conn.idle else 'idle'})")
            # Record liveness/eid so the manager device page lists this unit
            # even before its first cxzt# response lands.
            self._record_device_state(gt_conn)

            # Restore sticky SOS across TCP reconnects
            if imei in self._sticky_assist:
                gt_conn.assist_active = True
                if gt_conn.idle:
                    self.set_idle(gt_conn.eid, gt_conn.sailor_id, False)
                self._log(f"[GT06] Restored sticky SOS after reconnect for {gt_conn.sailor_id}")

            # Restore last known position from tracker and immediately update
            # idle/active state in current_positions.json so the UI reflects
            # the correct state without waiting for the next packet.
            tracker = self.get_tracker(gt_conn.eid)
            if tracker:
                pt = tracker.position_tracker if hasattr(tracker, 'position_tracker') else tracker
                with pt._lock:
                    existing = pt.current_positions.get(gt_conn.sailor_id)
                if existing and existing.get("lat") and (time.time() - existing.get("last_seen", 0)) < 300:
                    gt_conn.last_lat = existing["lat"]
                    gt_conn.last_lon = existing["lon"]
                    tracker.process_position(
                        sailor_id=gt_conn.sailor_id,
                        lat=gt_conn.last_lat, lon=gt_conn.last_lon,
                        speed=0, heading=0, ts=existing.get("ts", int(time.time())),
                        assist=existing.get("ast", False),
                        battery=existing.get("bat", -1),
                        signal=existing.get("sig", -1),
                        role="sailor", version="gt06",
                        flags={}, src_ip=gt_conn.addr[0], source="GT06",
                        charging=existing.get("chg", False),
                        stopped=gt_conn.idle, idle=gt_conn.idle,
                        did=gt_conn.imei,
                        skip_log=True,
                    )

        elif protocol in (0x12, 0x22):
            # Location
            loc = gt06_parse_location(data)
            if not loc:
                self._log(f"[GT06] Location packet too short from {gt_conn.sailor_id}")
                return
            if not gt_conn.sailor_id:
                self._log(f"[GT06] Location before login from {gt_conn.addr}")
                return

            if not loc["gps_valid"]:
                # Some W07C firmware revisions send LOC with course_status=0x0000
                # (all status bits cleared) even when they have a real fix.
                # Accept the packet if the embedded data looks internally
                # consistent: non-zero lat/lon, enough satellites, and gps_time
                # near server time.
                if not (loc["lat"] != 0 and loc["lon"] != 0
                        and loc["satellites"] >= 4
                        and abs(loc["ts"] - time.time()) <= 300):
                    return
                if not gt_conn.stale_fix_warned:
                    label = gt_conn.sailor_id or gt_conn.imei or "unknown"
                    self._log(f"[GT06] {label}: accepting LOC with cleared status bits "
                              f"(sats={loc['satellites']}) — firmware quirk")
                    gt_conn.stale_fix_warned = True

            gt_conn.loc_count += 1

            # GT06 reports speed as a single byte (integer km/h) which drops
            # to 0 at low speeds.  When reported speed is 0, estimate from
            # position delta over 3 samples (~3s) to smooth out GPS jitter.
            speed_knots = loc["speed_kmh"] / 1.852
            if speed_knots == 0 and len(gt_conn.pos_history) >= 3:
                old_lat, old_lon, old_ts = gt_conn.pos_history[0]
                dt = loc["ts"] - old_ts
                if 0 < dt < 5:
                    dlat = loc["lat"] - old_lat
                    dlon = loc["lon"] - old_lon
                    lat_nm = dlat * 60.0
                    lon_nm = dlon * 60.0 * math.cos(math.radians(loc["lat"]))
                    dist_nm = math.sqrt(lat_nm * lat_nm + lon_nm * lon_nm)
                    speed_knots = dist_nm * 3600.0 / dt

            # Maintain rolling 3-sample history for speed smoothing
            gt_conn.pos_history.append((loc["lat"], loc["lon"], loc["ts"]))
            if len(gt_conn.pos_history) > 3:
                gt_conn.pos_history.pop(0)

            # Adaptive LOC rate: reduce interval when moving slowly
            if not gt_conn.idle:
                now_mono = time.monotonic()
                if speed_knots < self.slow_speed_knots:
                    if gt_conn.slow_since == 0:
                        gt_conn.slow_since = now_mono
                    elif not gt_conn.slow_mode and (now_mono - gt_conn.slow_since >= self.slow_speed_seconds):
                        gt_conn.slow_mode = True
                        self._queue_commands(gt_conn, [f"TIMER,{self.slow_loc_interval},{self.slow_loc_interval}#"])
                        self._reset_rate_monitoring(gt_conn, self.slow_loc_interval)
                        label = gt_conn.sailor_id or gt_conn.imei or "unknown"
                        self._log(f"[GT06] {label} slow ({speed_knots:.1f}kn) — LOC interval {self.slow_loc_interval}s")
                else:
                    gt_conn.slow_since = 0
                    if gt_conn.slow_mode:
                        gt_conn.slow_mode = False
                        self._queue_commands(gt_conn, [f"TIMER,{self.interval},{self.interval}#"])
                        self._reset_rate_monitoring(gt_conn, self.interval)
                        label = gt_conn.sailor_id or gt_conn.imei or "unknown"
                        self._log(f"[GT06] {label} fast ({speed_knots:.1f}kn) — LOC interval {self.interval}s")

            # Save last known position for idle heartbeat updates
            gt_conn.last_lat = loc["lat"]
            gt_conn.last_lon = loc["lon"]
            gt_conn.last_ts = loc["ts"]

            tracker = self.get_tracker(gt_conn.eid)
            if tracker is None:
                return

            tracker.process_position(
                sailor_id=gt_conn.sailor_id,
                lat=loc["lat"],
                lon=loc["lon"],
                speed=round(speed_knots, 1),
                heading=loc["heading"],
                ts=loc["ts"],
                assist=gt_conn.assist_active,
                battery=gt_conn.battery,
                signal=gt_conn.signal,
                role="sailor",
                version="gt06",
                flags={},
                src_ip=gt_conn.addr[0],
                source="GT06",
                nsats=loc["satellites"],
                charging=gt_conn.charging,
                stopped=gt_conn.idle,
                idle=gt_conn.idle,
                did=gt_conn.imei,
                battery_voltage=gt_conn.battery_voltage,
            )

        elif protocol == 0x13:
            # Heartbeat
            hb = gt06_parse_heartbeat(data)
            if "battery" in hb and gt_conn.battery_voltage is None:
                # Only use coarse heartbeat level if we don't have a
                # voltage-based percentage from STATUS
                gt_conn.battery = hb["battery"]
            if "signal" in hb:
                gt_conn.signal = hb["signal"]
            if "charging" in hb:
                gt_conn.charging = hb["charging"]

            gt_conn.hbt_count += 1
            gt_conn.last_hbt_time = time.monotonic()

            bat_str = f"{gt_conn.battery}%" if gt_conn.battery >= 0 else "?"
            if gt_conn.battery_voltage is not None:
                bat_str += f"/{gt_conn.battery_voltage}V"
            if gt_conn.charging:
                bat_str += "+"
            sig_str = f"{gt_conn.signal}/4" if gt_conn.signal >= 0 else "?"
            label = gt_conn.sailor_id or gt_conn.imei or "unknown"
            self._log(f"[GT06] Heartbeat {label}: bat={bat_str} sig={sig_str}{' (idle)' if gt_conn.idle else ''}")
            self._send(gt_conn, gt06_make_response(protocol, serial))

            # Queue STATUS# on heartbeat, but no more than once per 60s
            now_mono = time.monotonic()
            if now_mono - gt_conn.last_status_time >= 60:
                gt_conn.status_miss_count += 1
                if gt_conn.status_miss_count >= 3:
                    label = gt_conn.sailor_id or gt_conn.imei or "unknown"
                    self._log(f"[GT06] No STATUS reply from {label} after 3 heartbeats — disconnecting")
                    self._disconnect(fd)
                    return
                self._queue_commands(gt_conn, ["STATUS#"])
                gt_conn.last_status_time = now_mono

            # Update tracker on heartbeat only when GPS is stale (no LOC for 15s+)
            # to avoid overwriting satellite/position data from recent LOC packets
            gps_stale = gt_conn.last_ts is None or (time.time() - gt_conn.last_ts) >= 15
            if gt_conn.sailor_id and gps_stale:
                tracker = self.get_tracker(gt_conn.eid)
                if tracker:
                    lat = gt_conn.last_lat if gt_conn.last_lat is not None else 0.0
                    lon = gt_conn.last_lon if gt_conn.last_lon is not None else 0.0
                    tracker.process_position(
                        sailor_id=gt_conn.sailor_id,
                        lat=lat,
                        lon=lon,
                        speed=0, heading=0,
                        ts=int(time.time()),
                        assist=gt_conn.assist_active,
                        battery=gt_conn.battery,
                        signal=gt_conn.signal,
                        role="sailor",
                        version="gt06",
                        flags={},
                        src_ip=gt_conn.addr[0],
                        source="GT06",
                        nsats=0,
                        charging=gt_conn.charging,
                        stopped=gt_conn.idle,
                        idle=gt_conn.idle,
                        did=gt_conn.imei,
                        battery_voltage=gt_conn.battery_voltage,
                    )

        elif protocol in (0x16, 0x23):
            # Alarm — parse location + alarm status, detect SOS
            loc = gt06_parse_location(data)
            self._send(gt_conn, gt06_make_response(protocol, serial))

            alarm = gt06_parse_alarm_status(data)
            is_sos = alarm["is_sos"] if alarm else False
            alarm_type = alarm["alarm_type"] if alarm else "Unknown"

            # Update battery/signal from alarm packet
            if alarm:
                if "battery" in alarm:
                    gt_conn.battery = alarm["battery"]
                if "signal" in alarm:
                    gt_conn.signal = alarm["signal"]
                if "charging" in alarm:
                    gt_conn.charging = alarm["charging"]

            label = gt_conn.sailor_id or gt_conn.imei or "unknown"
            self._log(f"[GT06] Alarm from {label}: {alarm_type}")

            if is_sos:
                imei = gt_conn.imei
                if imei not in self._sticky_assist:
                    gt_conn.assist_active = True
                    self._sticky_assist.add(imei)
                    self._log(f"[GT06] SOS activated (sticky) from {label}")
                    # Come out of idle so we get full GPS tracking
                    if gt_conn.idle:
                        self.set_idle(gt_conn.eid, gt_conn.sailor_id, False)
                        self._log(f"[GT06] Exited idle due to SOS from {label}")
                else:
                    self._log(f"[GT06] SOS already active, ignoring repeat press from {label}")

            if loc and gt_conn.sailor_id and loc["gps_valid"]:
                speed_knots = loc["speed_kmh"] / 1.852
                tracker = self.get_tracker(gt_conn.eid)
                if tracker:
                    tracker.process_position(
                        sailor_id=gt_conn.sailor_id,
                        lat=loc["lat"],
                        lon=loc["lon"],
                        speed=round(speed_knots, 1),
                        heading=loc["heading"],
                        ts=loc["ts"],
                        assist=gt_conn.assist_active,
                        battery=gt_conn.battery,
                        signal=gt_conn.signal,
                        role="sailor",
                        version="gt06",
                        flags={},
                        src_ip=gt_conn.addr[0],
                        source="GT06",
                        nsats=loc["satellites"],
                        charging=gt_conn.charging,
                        did=gt_conn.imei,
                        battery_voltage=gt_conn.battery_voltage,
                    )

        elif protocol == 0x15:
            # Server command response — clear pending, advance queue
            label = gt_conn.sailor_id or gt_conn.imei or "unknown"
            text = ""
            if len(data) >= 5:
                text = " " + data[5:].decode("ascii", errors="replace")
            self._log(f"[GT06] Command ACK from {label}:{text}")
            # Feed any query/ack response into the reconciler's observed-state.
            self._reconcile_parse(gt_conn, text)
            # Capture firmware version from VERSION# response, e.g.
            #   "NT19D_MG133_10F8G_B53_V667 2026-04-13"
            fwmatch = re.search(r'([A-Z0-9]+(?:_[A-Z0-9]+)*_V\d+)\s+(\d{4}-\d{2}-\d{2})', text)
            if fwmatch and gt_conn.firmware != fwmatch.group(0):
                gt_conn.firmware = fwmatch.group(0)
                self._log(f"[GT06] {label} firmware: {gt_conn.firmware}")
            # Parse cxzt# response (rich device-info, *-delimited fields).
            # Detect by presence of "MCU:" and "ID:" within the response.
            # If the device is not in MODE1, queue MODE1 once — this is the
            # one-shot mechanism (next cxzt# after the resulting reconnect
            # will see M=1 and not re-send, so no storm).
            if 'MCU:' in text and 'ID:' in text and '*' in text:
                fw_cxzt = re.match(r'\s*(\S+)-GT06', text)
                if fw_cxzt and gt_conn.firmware != fw_cxzt.group(1):
                    gt_conn.firmware = fw_cxzt.group(1)
                    self._log(f"[GT06] {label} firmware: {gt_conn.firmware}")
                mode_match = re.search(r'\*M:(\d+)', text)
                if mode_match:
                    mode = int(mode_match.group(1))
                    fmatch = re.search(r'\*F:(\d+)', text)
                    f_val = int(fmatch.group(1)) if fmatch else None
                    push_cmds = None
                    if gt_conn.overnight:
                        # Overnight intent — resolve the effective MODE for this
                        # device's firmware (per-IMEI > firmware-prefix > global).
                        # V667 honours MODE4's Freq arg; W07/V6.6x firmware does
                        # not (it clamps MODE4 to 120 and would storm), so those
                        # are configured to overnight_mode_number=1 and kept on
                        # MODE1 with a long TIMER instead.
                        eff_mode = self._resolve_setting(
                            gt_conn, "overnight_mode_number",
                            self.overnight_mode_number)
                        eff_interval = self._resolve_setting(
                            gt_conn, "overnight_interval_min",
                            self.overnight_interval_min)
                        if eff_mode == 1:
                            # MODE1 long-TIMER. desired_mode=1 means the overnight
                            # Freq re-push branch never runs for this firmware, so
                            # there is no storm path. loc_int is the check-in
                            # cadence in seconds.
                            gt_conn.desired_mode = 1
                            loc_int = eff_interval * 60
                            if mode != 1:
                                # One-shot switch back to MODE1 (persists across
                                # reboot). After the resulting reconnect we land
                                # here with mode==1 and apply the long TIMER.
                                push_cmds = ["SZCS#SLPDISCONNECT=0",
                                             f"MODE1,{loc_int},{loc_int}#"]
                                self._log(f"[GT06] {label} overnight on "
                                          f"{gt_conn.firmware or 'unknown fw'}: "
                                          f"switching MODE{mode}->1 long-TIMER "
                                          f"({loc_int}s)")
                            elif f_val is not None and f_val != loc_int:
                                if (gt_conn.overnight_freq_retries
                                        < OVERNIGHT_FREQ_MAX_RETRIES):
                                    gt_conn.overnight_freq_retries += 1
                                    push_cmds = (_idle_cmds(loc_int)
                                        + [f"HBT,{loc_int},{loc_int}#"])
                                    self._log(f"[GT06] {label} overnight MODE1: "
                                        f"F={f_val}, want {loc_int} — applying "
                                        f"long TIMER ("
                                        f"{gt_conn.overnight_freq_retries}/"
                                        f"{OVERNIGHT_FREQ_MAX_RETRIES})")
                                elif (gt_conn.overnight_freq_retries
                                        == OVERNIGHT_FREQ_MAX_RETRIES):
                                    gt_conn.overnight_freq_retries += 1
                                    self._log(f"[GT06] {label} won't accept "
                                        f"overnight TIMER (stuck at {f_val}, "
                                        f"wanted {loc_int}) — giving up after "
                                        f"{OVERNIGHT_FREQ_MAX_RETRIES} tries")
                            else:
                                gt_conn.overnight_freq_retries = 0
                        else:
                            # Deep-sleep MODE4/5 (V667). Enforce mode then Freq.
                            gt_conn.desired_mode = eff_mode
                            expected_f = _overnight_arg(eff_interval, eff_mode)
                            if mode != eff_mode:
                                # Push full overnight setup (SLPDISCONNECT,
                                # ACCLINE, MODE{4|5}) — bare MODE without
                                # ACCLINE=1 leaves vibration-wake enabled and
                                # the device wakes uselessly on wave motion.
                                gt_conn.overnight_freq_retries = 0
                                push_cmds = _overnight_cmds(eff_interval, eff_mode)
                                self._log(f"[GT06] {label} reports MODE={mode}, "
                                    f"desired overnight MODE={eff_mode} — "
                                    f"pushing {' '.join(push_cmds)}")
                            elif f_val is not None and f_val != expected_f:
                                # Right MODE, wrong Freq — race-day TIMER may
                                # have clobbered it (seen on G334189/G378848).
                                # Re-push, but cap attempts so firmware that
                                # refuses the Freq can't storm us.
                                if (gt_conn.overnight_freq_retries
                                        < OVERNIGHT_FREQ_MAX_RETRIES):
                                    gt_conn.overnight_freq_retries += 1
                                    push_cmds = _overnight_cmds(
                                        eff_interval, eff_mode)
                                    self._log(f"[GT06] {label} in MODE{mode} but "
                                        f"F={f_val}, expected {expected_f} — "
                                        f"re-pushing overnight setup ("
                                        f"{gt_conn.overnight_freq_retries}/"
                                        f"{OVERNIGHT_FREQ_MAX_RETRIES})")
                                elif (gt_conn.overnight_freq_retries
                                        == OVERNIGHT_FREQ_MAX_RETRIES):
                                    gt_conn.overnight_freq_retries += 1
                                    self._log(f"[GT06] {label} won't accept "
                                        f"overnight Freq (stuck at {f_val}, "
                                        f"wanted {expected_f}) on "
                                        f"{gt_conn.firmware or 'unknown fw'} — "
                                        f"giving up after "
                                        f"{OVERNIGHT_FREQ_MAX_RETRIES} tries")
                            else:
                                gt_conn.overnight_freq_retries = 0
                    else:
                        # Not overnight (active or race-day idle). Enforce
                        # desired_mode (always 1 here); recover a device stuck
                        # in MODE4/5 from a previous overnight period.
                        # Mode is enforced here (one-shot MODE1, storm-guarded).
                        # All NON-mode settings are handled by the reconciler.
                        desired = gt_conn.desired_mode
                        if mode != desired:
                            push_cmds = ["MODE1,30,300#"]
                            self._log(f"[GT06] {label} reports MODE={mode}, "
                                f"desired MODE={desired} — pushing "
                                f"{' '.join(push_cmds)}")
                    if push_cmds:
                        # A MODE change tears down TCP and reconnects; abandon any
                        # in-flight reconcile (it restarts fresh on reconnect) so
                        # we don't diff against a half-read observed-state.
                        gt_conn.reconcile_phase = None
                        gt_conn.cmd_queue.clear()
                        self._queue_commands(gt_conn, push_cmds)
                # Capture the full cxzt# settings snapshot for the manager
                # device page (firmware, mode, freq, server, APN, battery_mV…).
                self._record_device_state(gt_conn, text)
            # Parse battery voltage from STATUS response
            vmatch = re.search(r'Battery:(\d+\.\d+)V', text)
            if vmatch:
                gt_conn.battery_voltage = float(vmatch.group(1))
                gt_conn.battery = voltage_to_percent(gt_conn.battery_voltage)
                gt_conn.status_miss_count = 0
                self._log(f"[GT06] {label} battery voltage: {gt_conn.battery_voltage}V ({gt_conn.battery}%)")
            # Detect GPS still active during idle — re-send power-off commands
            if gt_conn.idle and 'GPS:Fail positioning' in text:
                self._log(f"[GT06] {label} idle but GPS active — re-sending GPS power-off commands")
                self._queue_commands(gt_conn, ["SZCS#GPS_RST_TIME=0", "SZCS#VIBCHK=0:16"])
            gt_conn.cmd_pending = None
            gt_conn.cmd_pending_frame = None
            self._send_next_cmd(gt_conn)

    def send_command_to(self, eid, sailor_id, cmd_str):
        """Send a command to a connected GT06 device matching (eid, sailor_id)."""
        for gt_conn in self.connections.values():
            if gt_conn.eid == eid and gt_conn.sailor_id == sailor_id:
                self._queue_commands(gt_conn, [cmd_str])
                return True
        return False

    # ------------------------------------------------------------------
    # Device management (manager-level GT06 admin page)
    # ------------------------------------------------------------------

    def _state_path(self):
        """Sidecar path for persisted per-device state, or None."""
        if not self.gt06_config_path:
            return None
        return self.gt06_config_path.parent / "gt06_state.json"

    def _load_device_state(self):
        p = self._state_path()
        if not p or not p.exists():
            return {}
        try:
            with open(p) as f:
                data = json.load(f)
            return data.get("devices", {}) if isinstance(data, dict) else {}
        except Exception as e:
            self._log(f"[GT06] Warning: could not load device state {p}: {e}")
            return {}

    def _save_device_state(self):
        p = self._state_path()
        if not p:
            return
        try:
            _atomic_write_json(p, {"updated": time.time(),
                                   "devices": self.device_state})
        except Exception as e:
            self._log(f"[GT06] Warning: could not save device state {p}: {e}")

    @staticmethod
    def _parse_cxzt(text):
        """Parse a cxzt# response into a flat settings dict.

        Format: "{fw}-GT06 MCU:{mcu}*ID:{imei}*{server}*A:{apn}*G:..*M:m|2|0
        *F:f|.. *H:h *SP:.. *BT:mv *..". Fields are '*'-delimited KEY:VALUE;
        the server-address chunk has no key. Returns {} if it doesn't look
        like a cxzt# response.
        """
        if 'MCU:' not in text or 'ID:' not in text or '*' not in text:
            return {}
        out = {}
        chunks = text.strip().split('*')
        for i, chunk in enumerate(chunks):
            chunk = chunk.strip()
            if not chunk:
                continue
            if i == 0:
                # "{fw}-GT06 MCU:{mcu}"
                m = re.match(r'(\S+)-GT06\s+MCU:(\S+)', chunk)
                if m:
                    out['firmware'] = m.group(1)
                    out['MCU'] = m.group(2)
                continue
            if ':' in chunk:
                k, v = chunk.split(':', 1)
                out[k.strip()] = v.strip()
            else:
                # Unkeyed chunk = server address (host:port already consumed by
                # split on ':' would break it, so only reach here if no colon).
                out.setdefault('server', chunk)
        # Server addr does contain a colon, so it landed as key=host val=port —
        # repair: detect a numeric-dotted key.
        for k in list(out.keys()):
            if re.match(r'^\d+\.\d+\.\d+\.\d+$', k):
                out['server'] = f"{k}:{out.pop(k)}"
        return out

    def _record_device_state(self, gt_conn, cxzt_text=None):
        """Update the persisted snapshot for a device. Called from the cxzt#
        handler (rich) and at login/disconnect (liveness)."""
        if not gt_conn or not gt_conn.imei:
            return
        now = time.time()
        st = self.device_state.get(gt_conn.imei, {})
        st['imei'] = gt_conn.imei
        st['sailor_id'] = gt_conn.sailor_id
        st['eid'] = gt_conn.eid
        if gt_conn.firmware:
            st['firmware'] = gt_conn.firmware
        if gt_conn.battery is not None and gt_conn.battery >= 0:
            st['battery'] = gt_conn.battery
        if gt_conn.battery_voltage is not None:
            st['battery_voltage'] = gt_conn.battery_voltage
        if gt_conn.signal is not None and gt_conn.signal >= 0:
            st['signal'] = gt_conn.signal
        st['idle'] = gt_conn.idle
        st['overnight'] = gt_conn.overnight
        st['last_seen'] = now
        st['last_seen_iso'] = datetime.fromtimestamp(now).isoformat()
        if cxzt_text:
            settings = self._parse_cxzt(cxzt_text)
            if settings:
                st['settings'] = settings
                st['raw_cxzt'] = cxzt_text.strip()
                if 'M' in settings:
                    st['mode'] = settings['M'].split('|')[0]
                if 'F' in settings:
                    st['freq'] = settings['F'].split('|')[0]
        self.device_state[gt_conn.imei] = st
        self._save_device_state()

    def _conn_for_imei(self, imei):
        for gt_conn in self.connections.values():
            if gt_conn.imei == imei:
                return gt_conn
        return None

    def get_device_inventory(self):
        """Merge config + persisted state + live connections into a list of
        device dicts for the management page."""
        cfg_devices = self.gt06_config.get("devices", {})
        imeis = set(self.device_state) | set(cfg_devices)
        for gt_conn in self.connections.values():
            if gt_conn.imei:
                imeis.add(gt_conn.imei)
        now = time.time()
        out = []
        for imei in sorted(imeis):
            st = dict(self.device_state.get(imei, {}))
            cfg = cfg_devices.get(imei, {}) if isinstance(cfg_devices, dict) else {}
            conn = self._conn_for_imei(imei)
            assigned_eid = (cfg.get("eid") if isinstance(cfg, dict) and "eid" in cfg
                            else st.get("eid", self.gt06_config.get("default_eid", 1)))
            entry = {
                "imei": imei,
                "sailor_id": (conn.sailor_id if conn else st.get("sailor_id")),
                "eid": assigned_eid,
                "firmware": (conn.firmware if conn and conn.firmware
                             else st.get("firmware")),
                "online": conn is not None,
                "last_seen_iso": st.get("last_seen_iso"),
                "battery": (conn.battery if conn and conn.battery is not None
                            and conn.battery >= 0 else st.get("battery")),
                "signal": (conn.signal if conn and conn.signal is not None
                           and conn.signal >= 0 else st.get("signal")),
                "idle": (conn.idle if conn else st.get("idle")),
                "overnight": (conn.overnight if conn else st.get("overnight")),
                "mode": st.get("mode"),
                "freq": st.get("freq"),
                "overnight_mode_number": (cfg.get("overnight_mode_number")
                                          if isinstance(cfg, dict) else None),
                "settings": st.get("settings", {}),
                "raw_cxzt": st.get("raw_cxzt"),
            }
            # Effective overnight mode resolution (what the device would get).
            if conn is not None:
                entry["effective_overnight_mode"] = self._resolve_setting(
                    conn, "overnight_mode_number", self.overnight_mode_number)
            out.append(entry)
        return out

    def queue_command_by_imei(self, imei, cmd_str):
        """Queue a command to a connected device by IMEI. Returns True if the
        device is connected and the command was queued."""
        conn = self._conn_for_imei(imei)
        if conn is None:
            return False
        self._queue_commands(conn, [cmd_str])
        self._log(f"[GT06] Manager command to {conn.sailor_id or imei}: {cmd_str}")
        return True

    def refresh_device(self, imei):
        """Queue a cxzt# probe so the device re-reports its full settings."""
        return self.queue_command_by_imei(imei, "cxzt#")

    def reboot_device(self, imei):
        """Ask a device to reboot (GT06 RESET#)."""
        return self.queue_command_by_imei(imei, "RESET#")

    def set_device_config(self, imei, updates):
        """Persist per-device config (e.g. {"eid": 3, "overnight_mode_number": 1}
        or {"name": "..."}) to gt06.json and apply in memory. Returns
        (ok: bool, error: str|None). eid changes take effect on the device's
        next reconnect; overnight_mode_number on its next cxzt# probe."""
        if not self.gt06_config_path:
            return False, "no gt06 config path configured"
        allowed = {"eid", "overnight_mode_number", "overnight_interval_min", "name"}
        clean = {k: v for k, v in (updates or {}).items() if k in allowed}
        if not clean:
            return False, "no recognised fields to update"
        # Update on-disk file (preserve everything else).
        try:
            data = {}
            if self.gt06_config_path.exists():
                with open(self.gt06_config_path) as f:
                    data = json.load(f)
            devices = data.setdefault("devices", {})
            dev = devices.setdefault(imei, {})
            for k, v in clean.items():
                if v is None:
                    dev.pop(k, None)
                else:
                    dev[k] = v
            _atomic_write_json(self.gt06_config_path, data)
        except Exception as e:
            return False, f"could not write config: {e}"
        # Apply in memory so it takes effect without a restart.
        mem_devices = self.gt06_config.setdefault("devices", {})
        mem_dev = mem_devices.setdefault(imei, {})
        for k, v in clean.items():
            if v is None:
                mem_dev.pop(k, None)
            else:
                mem_dev[k] = v
        self._log(f"[GT06] Manager set config for {imei}: {clean}")
        return True, None

    def cancel_assist(self, eid, sailor_id):
        """Cancel SOS assist for a GT06 device matching (eid, sailor_id)."""
        for gt_conn in self.connections.values():
            if (gt_conn.eid == eid and gt_conn.sailor_id == sailor_id
                    and gt_conn.assist_active):
                gt_conn.assist_active = False
                self._queue_commands(gt_conn, ["SENALM,OFF#"])
                if gt_conn.imei:
                    self._sticky_assist.discard(gt_conn.imei)
                self._log(f"[GT06] Cancelled assist for {sailor_id} (sticky cleared)")
                return True
        return False

    def set_idle(self, eid, sailor_id, idle, submode="race"):
        """Set idle state for the GT06 device matching (eid, sailor_id).

        When idle=True:
          submode="race"      → race-day idle (long TIMER + HBT + SENDS,1)
          submode="overnight" → scheduled-wake deep sleep using the mode
                                number from overnight_mode_number config
                                (4 = MODE4, 5 = MODE5).
        When idle=False: restore active command set.
        """
        key = (eid, sailor_id)
        if idle:
            self.idle_sailors.add(key)
            self.active_sailors.discard(key)
        else:
            self.idle_sailors.discard(key)
            self.active_sailors.add(key)

        found = False
        for gt_conn in self.connections.values():
            if gt_conn.eid == eid and gt_conn.sailor_id == sailor_id:
                gt_conn.idle = idle
                gt_conn.slow_mode = False
                gt_conn.slow_since = 0
                # Clear any pending commands from previous state
                gt_conn.cmd_queue.clear()
                gt_conn.cmd_pending = None
                if idle:
                    if submode == "overnight":
                        gt_conn.overnight = True
                        gt_conn.overnight_freq_retries = 0
                        gt_conn.desired_mode = self.overnight_mode_number
                        # Defer to the cxzt# handler — it resolves the firmware-
                        # appropriate overnight MODE (MODE4 for V667, MODE1
                        # long-TIMER for W07) and pushes it only if needed.
                        cmds = ["cxzt#"]
                        gt_conn.expected_hbt_interval = self.overnight_interval_min * 60
                        self._reset_rate_monitoring(gt_conn, self.overnight_interval_min * 60)
                    else:
                        gt_conn.overnight = False
                        gt_conn.desired_mode = 1
                        gt_conn.expected_hbt_interval = self.idle_hbt_interval
                        self._reset_rate_monitoring(gt_conn, self.idle_loc_interval)
                        self._reconcile_begin(gt_conn, "idle")
                        cmds = None
                else:
                    gt_conn.overnight = False
                    gt_conn.desired_mode = 1
                    gt_conn.expected_hbt_interval = 15
                    self._reset_rate_monitoring(gt_conn, self.interval)
                    self._reconcile_begin(gt_conn, "active")
                    cmds = None
                if cmds:
                    self._queue_commands(gt_conn, cmds)
                # Immediately update tracker so UI reflects idle/active state
                # (direct metadata update to avoid advancing dedup timestamp)
                tracker = self.get_tracker(gt_conn.eid)
                if tracker:
                    pt = tracker.position_tracker if hasattr(tracker, 'position_tracker') else tracker
                    with pt._lock:
                        existing = pt.current_positions.get(sailor_id)
                        if existing:
                            existing["stopped"] = idle
                            existing["idle"] = idle
                            # Per-sailor sleep flag — persisted to
                            # current_positions.json so the state survives
                            # server restart. WebUI reads it to show "SLEEP"
                            # vs "Idle"; login handler reads it to pick
                            # MODE5 vs MODE1 commands on reconnect.
                            existing["sleep"] = bool(idle and submode == "overnight")
                            now = time.time()
                            existing["last_seen"] = now
                            existing["last_seen_iso"] = datetime.fromtimestamp(now).isoformat()
                    if pt.positions_file and self._write_positions:
                        overrides = tracker.user_overrides if hasattr(tracker, 'user_overrides') else {}
                        self._write_positions(pt.current_positions, pt.positions_file, overrides, pt.position_tails)
                mode_label = ("Overnight idle" if (idle and submode == "overnight")
                              else "Idle" if idle else "Active")
                self._log(f"[GT06] {mode_label} mode for {sailor_id}")
                found = True
        if not found:
            # Only log if this (eid, sailor_id) has been touched by set_idle
            # at least once (which the .add above always does, so this always
            # holds — but keep the gate for symmetry with prior behaviour).
            if key in self.idle_sailors or key in self.active_sailors:
                self._log(f"[GT06] {'Idle' if idle else 'Active'} mode queued for {sailor_id} (not connected)")
        return found

    def _on_readable(self, fd):
        """Handle readable event on a GT06 connection."""
        gt_conn = self.connections.get(fd)
        if gt_conn is None:
            return

        try:
            chunk = gt_conn.sock.recv(1024)
        except Exception:
            self._disconnect(fd)
            return

        if not chunk:
            self._disconnect(fd)
            return

        gt_conn.buf += chunk

        # Process all complete frames in the buffer
        while True:
            idx = gt_conn.buf.find(b"\x78\x78")
            if idx < 0:
                gt_conn.buf = b""
                break
            if idx > 0:
                gt_conn.buf = gt_conn.buf[idx:]

            if len(gt_conn.buf) < 8:
                break

            length = gt_conn.buf[2]
            frame_size = 2 + 1 + length + 2
            if len(gt_conn.buf) < frame_size:
                break

            frame = gt_conn.buf[:frame_size]
            gt_conn.buf = gt_conn.buf[frame_size:]
            self._log_packet(gt_conn, frame, outgoing=False)
            gt_conn.last_alive_time = time.monotonic()

            try:
                self._process_frame(fd, frame)
            except Exception as e:
                label = gt_conn.sailor_id or gt_conn.imei or "unknown"
                self._log(f"[GT06] Frame error from {label}: {e}")

    def _archive_legacy_log(self, path):
        """Rename a legacy v1 gt06.log to gt06.log.preformat2.<n> so we don't overwrite."""
        for n in range(1, 1000):
            candidate = path.with_name(f"{path.name}.preformat2.{n}")
            if not candidate.exists():
                path.rename(candidate)
                return candidate
        # Fallback: timestamp-based name
        candidate = path.with_name(f"{path.name}.preformat2.{int(time.time())}")
        path.rename(candidate)
        return candidate

    def _open_log_v2(self, log_path):
        """Open the packet log in v2 format.

        - New (empty) file: write magic header, then start appending.
        - Existing v2 file: append without rewriting magic.
        - Existing v1 file: rename to .preformat2.<N> and start fresh.
        """
        path = Path(log_path) if not isinstance(log_path, Path) else log_path
        if path.exists() and path.stat().st_size > 0:
            with open(path, "rb") as f:
                head = f.read(len(GT06_LOG_MAGIC_V2))
            if head != GT06_LOG_MAGIC_V2:
                archived = self._archive_legacy_log(path)
                self._log(f"[GT06] Archived legacy v1 log {path} -> {archived}")
        self._log_fd = open(path, "ab")
        if path.stat().st_size == 0:
            self._log_fd.write(GT06_LOG_MAGIC_V2)
            self._log_fd.flush()
        self._log(f"[GT06] Packet logging to {path} (v2 format)")

    def rotate_log_to(self, archive_path):
        """Move the current packet log to archive_path and open a fresh log.

        Safe to call from any thread: rename of an open file keeps the old fd
        pointing to the archived inode; the new fd is opened atomically and
        swapped in, so concurrent _log_packet calls may write to either file
        across the swap but never lose or corrupt frames.
        """
        if not self.log_file:
            return
        path = Path(self.log_file) if not isinstance(self.log_file, Path) else self.log_file
        old_fd = self._log_fd
        try:
            if path.exists():
                path.rename(archive_path)
            self._log_fd = None
            self._open_log_v2(path)
        finally:
            if old_fd is not None:
                try:
                    old_fd.flush()
                    old_fd.close()
                except Exception:
                    pass

    def run(self):
        """Main loop — runs in a daemon thread."""
        if self.log_file:
            try:
                self._open_log_v2(self.log_file)
            except Exception as e:
                self._log(f"[GT06] Warning: Could not open packet log {self.log_file}: {e}")

        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.setblocking(False)
        server_sock.bind(("0.0.0.0", self.port))
        # Backlog sized well above the expected fleet — at the 250-tracker
        # target a server restart or mobile-network flap can trigger that
        # many simultaneous connect attempts and listen(16) would drop most.
        server_sock.listen(512)
        self.sel.register(server_sock, selectors.EVENT_READ, data="server")
        self._log(f"[GT06] Listening on TCP port {self.port} (interval={self.interval}s, prefix={self.id_prefix})")

        while True:
            try:
                events = self.sel.select(timeout=5)
            except Exception:
                continue
            for key, mask in events:
                if key.data == "server":
                    try:
                        self._accept(server_sock)
                    except Exception as e:
                        self._log(f"[GT06] Accept error: {e}")
                else:
                    self._on_readable(key.data)

            # Periodic checks on all connections
            now = time.monotonic()
            PRELOGIN_DEADLINE_S = 30  # accept→LOGIN must complete within this
            for fd in list(self.connections):
                gt_conn = self.connections.get(fd)
                if gt_conn is None:
                    continue
                # Expire pre-login connections that never sent a LOGIN frame.
                # Without this a client can accept a socket and sit there
                # forever, holding an fd and a slot in self.connections.
                if gt_conn.sailor_id is None:
                    if now - gt_conn.connected_at > PRELOGIN_DEADLINE_S:
                        self._log(f"[GT06] Pre-login timeout ({PRELOGIN_DEADLINE_S}s) "
                                  f"for {gt_conn.addr[0]}:{gt_conn.addr[1]} (conn_id={gt_conn.conn_id})")
                        self._disconnect(fd)
                    continue
                # Check SIOCOUTQ for pending commands
                if gt_conn.cmd_pending:
                    self._check_cmd_delivery(fd, gt_conn, now)
                # Check LOC/HBT rates
                self._check_rates(fd, gt_conn, now)
                # (STATUS# is also sent from heartbeat handler when HB arrives;
                # this block additionally polls idle connections that have
                # gone silent, since some new-firmware devices ACK HBT but
                # never actually emit heartbeats.)

                # Idle-mode keepalive probe. V667 firmware's cellular modem
                # appears to enter a deep sleep when nothing is happening,
                # after which the TCP connection becomes unreachable. To keep
                # race-day idle reachable for snappy operator control, poll
                # STATUS# aggressively (default every 60s). The device's
                # response keeps the modem awake (and the carrier NAT
                # mapping alive) and updates last_alive_time so the
                # disconnect check below stays satisfied.
                if (gt_conn.idle
                        and self.idle_keepalive_interval
                        and gt_conn.cmd_pending is None
                        and not gt_conn.cmd_queue
                        and gt_conn.last_alive_time > 0):
                    alive_gap = now - gt_conn.last_alive_time
                    poll_gap = now - gt_conn.last_idle_poll_time
                    if alive_gap >= self.idle_keepalive_interval and poll_gap >= self.idle_keepalive_interval:
                        gt_conn.last_idle_poll_time = now
                        self._queue_commands(gt_conn, ["STATUS#"])

                # Disconnect if no frame received from the device for too long.
                # We use last_alive_time (any received frame) rather than
                # last_hbt_time so a device responding to STATUS#/commands or
                # sending LOC counts as alive even if its HB scheduler is
                # broken or asleep. Observed on W07C new firmware: after a
                # natural ACC-OFF reconnect, the device ACKs HBT,15,15# but
                # never actually emits HB packets.
                if gt_conn.last_alive_time > 0:
                    alive_gap = now - gt_conn.last_alive_time
                    if alive_gap > gt_conn.expected_hbt_interval * 3 + 30:
                        label = gt_conn.sailor_id or gt_conn.imei or "unknown"
                        self._log(f"[GT06] No traffic from {label} for {alive_gap:.0f}s — disconnecting")
                        self._disconnect(fd)
                        continue
