"""GT06 GPS Tracker Protocol handler.

Extracted from tracker_server.py to keep protocol-specific code separate.
This module must NOT import tracker_server to avoid circular imports.
All server interactions happen through callbacks passed to GT06Listener.
"""

import fcntl
import hashlib
import hmac
import json
import math
import os
import re
import selectors
import socket
import struct
import threading
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

# Battery % is computed from the parametric OCV fit in gt06_calibration.json
# (load-aware), see GT06Listener._battery_percent. The old single-cell (G226122)
# _W07C_DISCHARGE table was retired 2026-06-25.

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
def _idle_cmds(interval, gps_rst=60):
    # TIMER T1=60 (ACC-ON, the documented 5-60s max), T2=interval as the
    # ACC-OFF/parked cadence (clamped to the 5-1800s range). A symmetric
    # TIMER,{interval},{interval} put T1 out of range for any interval >60,
    # which made the device ignore it and fall back to ~30s GPS-on uploads.
    # ACCLINE=1 = derive ACC from the voltage/ACC line, NOT vibration, so an
    # off-charge idle unit being knocked/rocked doesn't flip to ACC-ON (T1 + GPS
    # on). Vendor default is already 1, but pin it so a unit that ever got 0
    # (or shipped 0) is corrected. The ~5V charge doesn't cross the ACC-line
    # threshold, so this leaves charging-as-ACC off too; charge detection
    # (heartbeat bit2) is unaffected. Probed live 2026-06-21.
    return ["SZCS#SLPDISCONNECT=0",
            f"TIMER,60,{min(1800, max(5, interval))}#",
            "SENDS,1#", "SENALM,OFF#", "MOVING,OFF#",
            # GPS_RST_TIME>0 so a wedged/no-fix receiver auto-resets instead of
            # hanging GPS-on (sat=0, 5s no-fix replay). 0 = never reset = the
            # hang we saw. Only bites the no-lock path; outdoors it locks well
            # inside the timeout. Defaults to idle_gps_rst_time.
            f"SZCS#GPS_RST_TIME={gps_rst}", "SZCS#GPSCODEWAIT=10",
            # BLIND_EN=0: don't store/replay fixes while idle — an idle unit that
            # loses coverage shouldn't run the GPS blind-spot capture (_active_cmds
            # re-enables it for racing). Idle positions aren't recorded anyway.
            "SZCS#VIBCHK=0:16", "SZCS#ACCLINE=1", "SZCS#BLIND_EN=0"]


def _active_cmds(interval):
    """Commands to send when entering active tracking mode.

    SLPDISCONNECT=0 added for V667 firmware to prevent TCP drops on
    transient modem sleeps. MODE1 NOT sent here — see _idle_cmds comment;
    MODE1 must be a one-shot per device, not a per-login command.
    """
    return ["SZCS#SLPDISCONNECT=0",
            f"TIMER,{interval},{interval}#", "SENDS,0#",
            "SZCS#GPS_RST_TIME=300", "SZCS#GPSCODEWAIT=10", "SZCS#VIBCHK=0:16",
            # BLIND_EN=1: re-enable offline store-and-replay for racing so a
            # coverage hole doesn't leave a gap in a sailor's track (idle sets 0).
            "SZCS#BLIND_EN=1"]


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
    # GPSCODEWAIT=2: fail-fast GPS lock-wait for overnight — the device still
    # powers GPS on each wake, so cap how long it burns trying to lock when it
    # can't (race-idle/active reconcile restores the factory default of 10).
    return ["SZCS#SLPDISCONNECT=0",
            "SZCS#ACCLINE=1",
            "SZCS#GPSCODEWAIT=2",
            # Non-tracking, like idle: no offline blind-spot capture on a wake in a
            # coverage hole. Set here too because overnight bypasses the idle
            # reconciler, so an active->overnight unit would otherwise keep it on.
            "SZCS#BLIND_EN=0",
            f"MODE{mode_number},{arg}#"]


def _firmware_allows_mode4(fw):
    """MODE4's per-wake Freq arg only works on V667/NT19D firmware. W07/V6.6x
    hard-brick on MODE4 — the modem goes fully dark and only a physical
    power-cycle recovers it. Allow MODE4 only when we positively know V667;
    the W07 line and unknown fw are always refused. The version token is
    anchored so V6670 etc. don't slip through."""
    if not fw or fw.startswith("W07"):
        return False
    return re.search(r'V667(?!\d)', fw) is not None


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
    "GPSCODEWAIT":   (lambda v: f"SZCS#GPSCODEWAIT={v}",   "CXCS#GPSCODEWAIT"),
    "VIBCHK":        (lambda v: f"SZCS#VIBCHK={v}",        "CXCS#VIBCHK"),
    "ACCLINE":       (lambda v: f"SZCS#ACCLINE={v}",       "CXCS#ACCLINE"),
    # Blind buffer (offline store-and-replay). 1 while tracking (preserve a
    # racer's track through a coverage hole); 0 in every non-tracking state so an
    # idle/overnight unit that loses coverage doesn't run the GPS blind-spot
    # capture. If the CXCS#BLIND_EN query isn't answered it's just re-set each
    # reconnect (harmless — reconcile is per-login, not periodic).
    "BLIND_EN":      (lambda v: f"SZCS#BLIND_EN={v}",      "CXCS#BLIND_EN"),
    "SENDS":         (lambda v: f"SENDS,{v}#",             "SENDS#"),
    "SENALM":        (lambda v: f"SENALM,{v}#",            "SENALM#"),
    "MOVING":        (lambda v: f"MOVING,{v}#",            "MOVING#"),
    # value is "T1,T2" (ACC-ON,ACC-OFF); cxzt reports it back as *F:T1|T2.
    "TIMER":         (lambda v: f"TIMER,{v}#",              None),
    "HBT":           (lambda v: f"HBT,{v},{v}#",           None),
}

# Order in which corrective sets are applied (deterministic, mirrors the legacy
# command order so any ordering dependence is preserved).
_APPLY_ORDER = ["SLPDISCONNECT", "TIMER", "SENDS", "SENALM", "MOVING",
                "GPS_RST_TIME", "GPSCODEWAIT", "VIBCHK", "ACCLINE", "BLIND_EN", "HBT"]


def _norm(v):
    """Normalise a setting value for comparison. Strips surrounding whitespace
    AND control chars — real devices terminate command replies with a \\x00\\x01
    trailer, so e.g. "300\\x00\\x01" must compare equal to 300 (else the diff
    would re-apply correct settings on every reconnect = churn)."""
    if v is None:
        return None
    return re.sub(r'[\x00-\x1f]', '', str(v)).strip().lower()


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


def _default_log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} {msg}")


# Mask the 9-digit credential prefix of a 15-digit terminal id wherever it appears:
# `TERIID=<15>` (set/read commands + ACKs) and `ID:<15>` (cxzt# *ID field). Keeps the
# last 6 (public IMEI suffix = the label). For a non-provisioned unit ID: is the real
# IMEI (public); masking its prefix is harmless and keeps logs consistent.
_REDACT_RE = re.compile(r"(TERIID=|ID:)(\d{9})(\d{6})")


def _redact_teriid(msg):
    if not msg:
        return msg
    return _REDACT_RE.sub(lambda m: m.group(1) + "***" + m.group(3), msg)


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


# Global night-sleep schedule default. Bench-proven overnight recipe
# (2026-06-17): MODE5 + 1-hour wake interval keeps the fleet's battery drain
# negligible even when GPS can't lock. Disabled by default (opt-in); times are
# "HH:MM" in each event's own timezone; a window with start > end wraps midnight.
DEFAULT_SLEEP_SCHEDULE = {
    "enabled": False,
    "start": "22:00",
    "end": "06:00",
    "overnight_mode_number": 5,
    "overnight_interval_min": 60,
}

# Night-idle: instead of MODE5 deep-sleep, keep idle units in MODE1 idle but with
# long intervals overnight (heartbeat/keepalive/cxzt). Proven ~18mW (≈ parked-idle
# floor, ~1/3 of MODE5 sleep) while staying connected & GPS-reportable (2026-06-23
# overnight test). When enabled it REPLACES the MODE5 sleep-schedule path: the same
# sleep_schedule window now switches idle units between day (short) and night (long)
# idle intervals. MODE5 deep-sleep remains available via a manual /admin/sleep.
DEFAULT_NIGHT_IDLE = {
    "enabled": False,
    "hbt_interval": 900,       # device heartbeat (HBT), seconds
    "keepalive_interval": 1800,  # server STATUS# poll, seconds
    "cxzt_poll_min": 30,       # cxzt# battery poll, minutes (0 = off)
    "acc_off_interval": 1800,  # TIMER T2 (ACC-off parked upload), seconds (5-1800)
    "gps_rst_time": 60,        # idle GPS no-fix reset, seconds (0 = never)
}


def load_gt06_config(config_path: Path, log_func=None) -> dict:
    """Load GT06 device config from JSON file.

    Returns {"default_eid": int, "devices": {imei: {...}}}.
    If file doesn't exist, returns defaults.
    """
    _log = log_func or _default_log
    default = {"default_eid": 1, "devices": {},
               "sleep_schedule": dict(DEFAULT_SLEEP_SCHEDULE)}
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
            # Periodic cxzt# poll interval in MINUTES (idle AND tracking), 0 = off.
            # cxzt# carries mV-resolution battery (*BT) vs STATUS#'s 10mV, so this
            # gives fine-grained battery sampling for drain/calibration analysis.
            "cxzt_poll_min": cfg.get("cxzt_poll_min", 0),
            # After login, hold off keepalive/cxzt polling and stuck-detection for
            # this many seconds so the device's state machine settles once before
            # we probe it (per codex). 0 = no grace.
            "reconnect_grace_sec": cfg.get("reconnect_grace_sec", 60),
            # Detect-and-remediate a unit stuck GPS-on in idle (continuous LOC that
            # never drops to parked after a reconnect — a firmware runtime-state
            # latch only a MODE re-entry clears). 1 = on. When an idle unit uploads
            # >= idle_stuck_loc_per_min over a window past the grace period, bounce
            # it once with MODE1; give up + flag after idle_stuck_max_bounces.
            "idle_stuck_bounce": cfg.get("idle_stuck_bounce", 1),
            "idle_stuck_loc_per_min": cfg.get("idle_stuck_loc_per_min", 5),
            "idle_stuck_max_bounces": cfg.get("idle_stuck_max_bounces", 3),
            "idle_stuck_window_sec": cfg.get("idle_stuck_window_sec", 60),
            # Idle parked-upload interval = TIMER T2 (ACC-OFF), the GPS-off lever.
            # Vendor range 5-1800s; at the 1800s max the parked device uploads a
            # position only every 30 min, so GPS stays powered down between (the
            # original behaviour that commit 4d21120 lost by collapsing TIMER to
            # one value — T1's max is 60, so a symmetric 540 was out of range and
            # the device fell back to ~30s uploads with GPS continuously on).
            # TIMER T1 (ACC-ON, moving-upload interval). Vendor range 5-60 but the
            # device accepts higher (tested to 120); doesn't affect responsiveness
            # (that's the open TCP), only how often a *moving* idle unit uploads.
            "idle_acc_on_interval": min(1800, max(5,
                cfg.get("idle_acc_on_interval", 60))),
            "idle_acc_off_interval": min(1800, max(5,
                cfg.get("idle_acc_off_interval", 1800))),
            # GPS no-fix timeout for idle (seconds). 0 = the firmware never
            # resets GPS, so a wedged/no-lock receiver hangs GPS-on forever
            # (sat=0, 5s no-fix replay — observed 2026-06-21); 60 auto-resets it.
            "idle_gps_rst_time": cfg.get("idle_gps_rst_time", 60),
            "idle_gps_off_resend_sec": cfg.get("idle_gps_off_resend_sec", 300),
            "firmware_overrides": cfg.get("firmware_overrides", {}),
            "overnight_interval_min": cfg.get("overnight_interval_min", 15),
            # 4 = MODE4 (vendor-recommended 2026-05, vibration-responsive
            # but ACCLINE=1 in the chain suppresses spurious wakes);
            # 5 = MODE5 (strictly scheduled, no vibration wake).
            "overnight_mode_number": cfg.get("overnight_mode_number", 4),
            "slow_speed_knots": cfg.get("slow_speed_knots", 2),
            "slow_speed_seconds": cfg.get("slow_speed_seconds", 20),
            "slow_loc_interval": cfg.get("slow_loc_interval", 3),
            # Lag remediation (blind-buffer drain). lag_remediation_sec=0 disables
            # it; these must be copied here or _resolve_setting() only sees the
            # per-device layer and the global gt06.json values are silently lost.
            "lag_remediation_sec": cfg.get("lag_remediation_sec", 0),
            "lag_drain_interval": cfg.get("lag_drain_interval", 2),
            "lag_restore_sec": cfg.get("lag_restore_sec", 8),
            "lag_remediation_cooldown_sec": cfg.get("lag_remediation_cooldown_sec", 60),
            "lag_remediation_max_retries": cfg.get("lag_remediation_max_retries", 3),
            "lag_drain_max_sec": cfg.get("lag_drain_max_sec", 180),
            "sleep_schedule": {**DEFAULT_SLEEP_SCHEDULE,
                               **(cfg.get("sleep_schedule") or {})},
            "night_idle": {**DEFAULT_NIGHT_IDLE,
                           **(cfg.get("night_idle") or {})},
            "login_strict": bool(cfg.get("login_strict", False)),
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
        self.login_id = None        # raw terminal id sent in the 0x01 login (= TERIID)
        self.authenticated = False  # passed TERIID/HMAC check → may map to an event
        self.auth_status = None     # "auth"|"legacy_raw"|"sim"|"spoof_alert"|"onboard"|"recovery"
        self.resolved_imei = None   # real IMEI inferred for an UNAUTH conn (display only)
        self.battery = -1
        self.signal = -1
        self.charging = None
        self.battery_voltage = None             # actual voltage from STATUS/cxzt (float)
        self.last_status_time = time.monotonic()  # monotonic time of last STATUS# send
        self.last_overnight_probe = 0.0         # monotonic time of last overnight cxzt# re-probe
        self.last_gps_off_resend = 0.0          # monotonic time of last idle GPS-off re-push
        self.status_miss_count = 0
        self.cmd_serial = 0
        self.assist_active = False
        self.idle = False
        self.last_lat = None
        self.last_lon = None
        self.last_ts = None
        self.last_loc_mono = 0.0  # monotonic wall-time the last LOC arrived
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
        # When overnight was driven by a sleep SCHEDULE, the schedule's
        # (mode, interval_min) — these override the config-resolved overnight
        # mode/interval in the cxzt# handler. None = use config resolution
        # (manual /admin/sleep or event-submode overnight).
        self.sched_overnight_mode = None
        self.sched_overnight_interval = None
        # Manual-sleep morning-wake deadline (epoch): the cxzt# handler clamps
        # the resolved overnight interval to this so the unit wakes on time.
        # None = no manual-sleep clamp (schedule pre-clamps its own interval).
        self.overnight_until = None
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

        # Lag remediation (blind-buffer drain). When an ACTIVE device's replay
        # tip (last_ts) falls behind wall-clock, its on-device offline buffer is
        # replaying at the fixed ~1Hz rate but never catching up. Bench-proven
        # 2026-05-31: temporarily lowering TIMER (capture rate) below the 1Hz
        # replay drains the backlog with no track loss. State for that drain:
        self.lag_draining = False     # currently in a TIMER-lowered drain
        self.lag_drain_started = 0.0  # monotonic when the current drain began
        self.lag_drain_last = 0.0     # monotonic of last drain start (cooldown)
        self.lag_drain_attempts = 0   # drains started since last full recovery

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
        # Last time the periodic cxzt# battery-sampling poll fired for this device.
        self.last_cxzt_poll_time = 0
        # Monotonic time this connection finished login (reconnect grace window).
        self.login_mono = 0
        # Windowed idle-LOC counter for the stuck-GPS-on detector.
        self.idle_loc_count = 0
        self.idle_loc_window_start = 0

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
                 get_event_idle_submode_func=None, gt06_config_path=None,
                 get_event_sleep_active_func=None,
                 get_event_night_active_func=None, battery_cal_path=None):
        self.port = port
        self.interval = interval
        self.id_prefix = id_prefix
        self.get_tracker = get_tracker_func
        # Logger first (so _apply_config below can log). Redact the TERIID secret
        # from all log output, keeping the last 6 (public IMEI suffix = label).
        _base_log = log_func or _default_log
        self._log = lambda msg: _base_log(_redact_teriid(msg)
                                          if msg and ("TERIID" in msg or "ID:" in msg) else msg)
        self.get_event_state = get_event_state_func  # callable(eid) -> "tracking" | "idle"
        # callable(eid) -> "race" | "overnight" — when an idle tracker
        # reconnects, picks the command set. Default "race" if unset.
        self.get_event_idle_submode = get_event_idle_submode_func
        # callable(eid) -> bool: is the event currently inside its scheduled
        # night-sleep window? Used so an idle tracker that connects during the
        # window goes straight to overnight deep-sleep (sticky on reconnect).
        self.get_event_sleep_active = get_event_sleep_active_func
        # callable(eid) -> bool: is the event currently inside its night-idle
        # window (night_idle enabled AND in the sleep_schedule window)? When true,
        # idle units use the long night intervals instead of the day ones.
        self.get_event_night_active = get_event_night_active_func
        self._apply_config(gt06_config or {"default_eid": 1, "idle_hbt_interval": 15,
                                           "devices": {}})
        self.connections = {}  # fd -> GT06Connection
        self.sel = selectors.DefaultSelector()
        self.log_file = log_file
        self._log_fd = None
        # Serialises packet-log writes (run() select thread) against rotation
        # (LogRotator midnight thread) so a rotation never drops/corrupts a frame.
        self._log_lock = threading.Lock()
        self._next_conn_id = 1   # monotonic per-connection ID for gt06.log v2 format
        self.idle_sailors = set()
        self.active_sailors = set()
        self._sticky_assist: set = set()
        self._save_overrides = save_overrides_func
        self._write_positions = write_positions_func
        # Path to gt06.json so the management page can persist per-device config
        # (event assignment, overnight mode). None disables config writes.
        self.gt06_config_path = Path(gt06_config_path) if gt06_config_path else None
        # Now that the config path is known, (re)load the server-only login master
        # key (the init _apply_config above ran before the path was set) and
        # re-evaluate strict (the no-key guard would have forced it off pre-load).
        self._login_master_key = self._load_master_key()
        self.login_strict = bool(self.gt06_config.get("login_strict")) and bool(self._login_master_key)
        # Unauthenticated connections (no valid TERIID) keyed by raw login_id, so
        # they show in the management UI for recovery/onboarding without clobbering
        # the real units' device_state (which is keyed by IMEI).
        self.pending_devices = {}
        # Persisted per-device state (firmware, last cxzt# snapshot, battery,
        # last-seen) so the management page shows offline devices too. Sidecar
        # gt06_state.json next to the config; survives restart.
        self.device_state = self._load_device_state()
        # Battery calibration (parametric OCV fit) for voltage -> % conversion.
        self.battery_cal_path = Path(battery_cal_path) if battery_cal_path else None
        self.battery_cal = self._load_battery_cal()

    def _load_battery_cal(self):
        """Load gt06_calibration.json (parametric OCV fit) for the battery %.
        Returns {} if unavailable — % then reads -1 (unknown)."""
        if not self.battery_cal_path:
            return {}
        try:
            cal = json.loads(self.battery_cal_path.read_text())
            self._log(f"[GT06] Battery calibration loaded (v{cal.get('version')}, "
                      f"{len(cal.get('units', {}))} units, "
                      f"soc_fit={'yes' if cal.get('soc_fit') else 'no'})")
            return cal
        except Exception as e:
            self._log(f"[GT06] Battery calibration not loaded ({self.battery_cal_path}): {e}")
            return {}

    def _battery_percent(self, gt_conn, voltage):
        """Remaining % from a terminal voltage via the parametric OCV fit, load-aware:
        OCV = V + per-unit divider offset + I_load*R_class (I_load = idle vs tracking,
        from gt_conn.idle), then SoC = c1*(1 - 1/(1+(OCV/c2)^c4)^c3). -1 if no fit."""
        cal = self.battery_cal
        sf = cal.get("soc_fit") if cal else None
        if voltage is None or not sf:
            return -1
        c = sf["coeffs"]
        sid = gt_conn.sailor_id
        defaults = cal.get("defaults", {})
        unit = cal.get("units", {}).get(sid) or {}
        cap_class = unit.get("cap_class") or defaults.get("cap_class", "6Ah")
        offset = sf.get("offsets_mv", {}).get(sid, 0) / 1000.0
        r = sf.get("class_r_ohm", {}).get(cap_class, 0.0)
        nom_v = cal.get("nominal_voltage", 3.7)
        if gt_conn.idle:
            i_load = (cal.get("mode_power_w", {}).get("idle", 0) / nom_v) if nom_v else 0.0
        else:
            i_load = cal.get("track_current_ma", 0) / 1000.0
        ocv = voltage + offset + i_load * r
        if ocv <= 0:   # (ocv/c2)**c4 with non-integer c4 needs a positive base
            return -1
        try:
            s = c["c1"] * (1 - 1 / (1 + (ocv / c["c2"]) ** c["c4"]) ** c["c3"])
        except (ZeroDivisionError, OverflowError, ValueError):
            return -1
        return int(round(max(0.0, min(100.0, s))))

    def _apply_config(self, cfg):
        """(Re)derive all config-cached attributes from a gt06_config dict.
        Called at init and by reload_config() so a live config edit takes
        effect without a restart (new values apply on each device's next
        wake/reconnect, since the reconciler reads these attributes then)."""
        self.gt06_config = cfg
        self.idle_hbt_interval = cfg.get("idle_hbt_interval", 15)
        self.idle_poll_interval = cfg.get("idle_poll_interval", 60)
        self.idle_loc_interval = cfg.get("idle_loc_interval", self.idle_hbt_interval)
        self.idle_keepalive_interval = cfg.get("idle_keepalive_interval",
                                               self.idle_poll_interval)
        # Periodic cxzt# poll (minutes; 0 = off) for mV battery sampling. Read
        # live by the run loop, so a config edit applies without restart.
        self.cxzt_poll_min = cfg.get("cxzt_poll_min", 0)
        # Reconnect grace + stuck-GPS-on idle remediation (read live by run loop).
        self.reconnect_grace_sec = cfg.get("reconnect_grace_sec", 60)
        self.idle_stuck_bounce = cfg.get("idle_stuck_bounce", 1)
        self.idle_stuck_loc_per_min = cfg.get("idle_stuck_loc_per_min", 5)
        self.idle_stuck_max_bounces = cfg.get("idle_stuck_max_bounces", 3)
        self.idle_stuck_window_sec = cfg.get("idle_stuck_window_sec", 60)
        self.idle_acc_on_interval = min(1800, max(5,
            cfg.get("idle_acc_on_interval", 60)))
        self.idle_acc_off_interval = min(1800, max(5,
            cfg.get("idle_acc_off_interval", 1800)))
        self.idle_gps_rst_time = cfg.get("idle_gps_rst_time", 60)
        self.idle_gps_off_resend_sec = cfg.get("idle_gps_off_resend_sec", 300)
        self.firmware_overrides = cfg.get("firmware_overrides", {})
        self.overnight_interval_min = cfg.get("overnight_interval_min", 15)
        self.overnight_mode_number = cfg.get("overnight_mode_number", 4)
        self.slow_speed_knots = cfg.get("slow_speed_knots", 2)
        self.slow_speed_seconds = cfg.get("slow_speed_seconds", 20)
        self.slow_loc_interval = cfg.get("slow_loc_interval", 3)
        self.sleep_schedule = {**DEFAULT_SLEEP_SCHEDULE,
                               **(cfg.get("sleep_schedule") or {})}
        # Night-idle: long idle intervals applied during the sleep_schedule window
        # (replaces MODE5 sleep when enabled). acc_off clamped to firmware 5-1800.
        ni = {**DEFAULT_NIGHT_IDLE, **(cfg.get("night_idle") or {})}
        ni["acc_off_interval"] = min(1800, max(5, int(ni.get("acc_off_interval", 1800))))
        self.night_idle = ni
        # TERIID anti-spoofing. login_strict gates whether un-provisioned raw-IMEI
        # logins still route to an event (False = legacy, during rollout). The
        # master key is loaded separately (server-only, never in this cfg dict).
        self.login_strict = bool(cfg.get("login_strict", False))
        self._login_master_key = self._load_master_key()
        # Strict without a master key would lock out the whole fleet — refuse it.
        if self.login_strict and not self._login_master_key:
            self._log("[GT06] login_strict requested but no master key loaded — "
                      "forcing NON-strict to avoid a fleet lockout")
            self.login_strict = False
        # Warn on last6 collisions among provisioned units (would mis-resolve a TERIID).
        seen = {}
        for im, c in (cfg.get("devices") or {}).items():
            if isinstance(c, dict) and c.get("provisioned") and isinstance(im, str) and len(im) >= 6:
                seen.setdefault(im[-6:], []).append(im)
        for s6, ims in seen.items():
            if len(ims) > 1:
                self._log(f"[GT06] WARNING: last6 collision among provisioned units {ims} "
                          f"— TERIID resolution for suffix {s6} is ambiguous")

    # ---- TERIID auth (anti-spoofing) ----------------------------------------

    def _load_master_key(self):
        """Server-only login master key, from `gt06_master_key` next to gt06.json
        (NEVER part of gt06_config, so it can't leak via the config API/UI).
        Returns bytes, or None when absent → feature off (legacy behaviour)."""
        path = getattr(self, "gt06_config_path", None)
        if not path:
            return None
        try:
            keyfile = Path(path).parent / "gt06_master_key"
            if keyfile.exists():
                data = keyfile.read_text().strip()
                return data.encode() if data else None
        except Exception as e:
            self._log(f"[GT06] master key load failed: {e}")
        return None

    def _hmac_prefix(self, imei):
        """9-digit decimal HMAC(master_key, imei) in [100000000, 998999999] — no
        leading zero (clean 15-digit parse), never 999… (sim range). None when no
        master key (feature off)."""
        if not self._login_master_key or not imei:
            return None
        d = hmac.new(self._login_master_key, imei.encode(), hashlib.sha256).digest()
        return 100000000 + (int.from_bytes(d, "big") % 899000000)

    def _teriid_for(self, imei):
        """Provisioned terminal id for `imei`: prefix9 + imei[-6:] (15 digits)."""
        p = self._hmac_prefix(imei)
        if p is None or not imei or len(imei) < 6:
            return None
        return f"{p}{imei[-6:]}"

    def _is_provisioned(self, imei):
        dev = self.gt06_config.get("devices", {})
        d = dev.get(imei) if isinstance(dev, dict) else None
        return bool(isinstance(d, dict) and d.get("provisioned"))

    def _provisioned_imeis(self):
        """Real IMEIs we've ONBOARDED (gt06.json devices with provisioned=true).
        This is the auth TRUST ANCHOR — operator-controlled, NOT device_state (which
        a non-strict-window spoofer could pollute with fake 'known' IMEIs)."""
        dev = self.gt06_config.get("devices", {})
        if not isinstance(dev, dict):
            return set()
        return {im for im, c in dev.items()
                if isinstance(im, str) and im.isdigit() and len(im) >= 6
                and not im.startswith("999") and isinstance(c, dict) and c.get("provisioned")}

    def _assigned_imeis(self):
        """IMEIs the operator has added to gt06.json devices (provisioned or not) —
        the onboarding candidates under strict."""
        dev = self.gt06_config.get("devices", {})
        if not isinstance(dev, dict):
            return set()
        return {im for im in dev if isinstance(im, str) and im.isdigit()
                and len(im) >= 6 and not im.startswith("999")}

    def _provisioned_index(self):
        """last6 → provisioned IMEI (collisions warned at config load)."""
        return {im[-6:]: im for im in self._provisioned_imeis()}

    def _resolve_login(self, login_id):
        """Map a raw login terminal id → (authenticated, imei, status). `imei` is
        the real device IMEI when resolvable (display/routing), else None.
        status ∈ auth | legacy_raw | sim | spoof_alert | onboard | recovery.

        Trust anchor = PROVISIONED units (gt06.json devices, provisioned=true), never
        device_state. NON-STRICT (rollout default) preserves legacy routing: any clean
        15-digit id routes, plus provisioned TERIIDs resolve. STRICT accepts only a
        valid TERIID (and sim in dev); everything else is unauthenticated and listed
        for onboarding/recovery (never event-mapped)."""
        # sim convention: 999-prefixed ids carry the eid (dev/test only)
        if login_id.startswith("999") and len(login_id) >= 5:
            return (True, login_id, "sim")
        is15 = len(login_id) == 15 and login_id.isdigit()
        prov = self._provisioned_index()
        # provisioned TERIID: 15-digit, suffix→provisioned unit, prefix == HMAC
        if is15:
            imei = prov.get(login_id[-6:])
            if imei and self._teriid_for(imei) == login_id:
                return (True, imei, "auth")
        if not self.login_strict:
            # legacy: any clean 15-digit id routes (= current behaviour). Garbled /
            # non-decimal can't form a sailor → recovery.
            if is15:
                return (True, login_id, "legacy_raw")
            return (False, None, "recovery")
        # STRICT:
        if is15:
            pim = prov.get(login_id[-6:])
            if pim:                                   # provisioned suffix, not its TERIID → forged
                return (False, pim, "spoof_alert")    # (covers the unit's raw IMEI too)
            if login_id in self._assigned_imeis():    # operator-added, awaiting onboarding
                return (False, login_id, "onboard")
        return (False, None, "recovery")

    def _publishes(self, gt_conn):
        """Whether this connection should be mapped to a public event (event.html).
        When the TERIID feature is OFF (no master key) everything publishes — full
        legacy behaviour (dev/tests + pre-onboarding fleet). When it's ON, only
        TERIID-registered ('auth') and simulator ('sim') units publish; an
        un-onboarded 'legacy_raw' unit stays connected + manageable but off the map
        until it is Registered. (Unauthenticated connections never reach here.)"""
        if not self._login_master_key:
            return True
        return gt_conn.auth_status in ("auth", "sim")

    def _unpublish_sailor(self, gt_conn):
        """Remove a non-publishing unit's sailor from its event current_positions so
        it disappears from event.html (it stays in the admin trackers page). Called
        at login for legacy/un-onboarded units."""
        if not gt_conn.sailor_id:
            return
        tracker = self.get_tracker(gt_conn.eid)
        if not tracker:
            return
        pt = (tracker.position_tracker if hasattr(tracker, "position_tracker") else tracker)
        removed = False
        with pt._lock:
            if pt.current_positions.pop(gt_conn.sailor_id, None) is not None:
                removed = True
        if removed and pt.positions_file and self._write_positions:
            self._write_positions(pt.current_positions, pt.positions_file,
                                  getattr(tracker, "user_overrides", {}), pt.position_tails)

    def _idle_intervals(self, gt_conn):
        """Effective idle intervals for `gt_conn`: the long NIGHT set when the
        unit's event is inside its night-idle window, else the day (race) set.
        The night switch is per-event (timezone-aware), so it must be resolved
        per connection — different events can be in night vs day at once."""
        night = False
        if self.get_event_night_active and self.night_idle.get("enabled"):
            try:
                night = bool(self.get_event_night_active(gt_conn.eid))
            except Exception:
                night = False
        if night:
            ni = self.night_idle
            return {"hbt": int(ni["hbt_interval"]),
                    "keepalive": int(ni["keepalive_interval"]),
                    "cxzt_min": int(ni["cxzt_poll_min"]),
                    "acc_off": int(ni["acc_off_interval"]),
                    "acc_on": self.idle_acc_on_interval,
                    "gps_rst": int(ni["gps_rst_time"]),
                    "night": True}
        return {"hbt": self.idle_hbt_interval,
                "keepalive": self.idle_keepalive_interval,
                "cxzt_min": self.cxzt_poll_min,
                "acc_off": self.idle_acc_off_interval,
                "acc_on": self.idle_acc_on_interval,
                "gps_rst": self.idle_gps_rst_time,
                "night": False}

    def reload_config(self, new_config):
        """Swap in a freshly-loaded gt06_config and re-derive cached attrs.
        Lets the management UI edit gt06.json and apply it with no restart."""
        self._apply_config(new_config)
        self._log("[GT06] Config reloaded (live, no restart): "
                  f"{len(new_config.get('devices', {}))} device(s), "
                  f"overnight MODE{self.overnight_mode_number}/"
                  f"{self.overnight_interval_min}min")
        # A plain config edit only re-derives the cached value — it would not
        # reach units already connected (HBT is otherwise pushed only at login,
        # an idle transition, or a reconcile). Re-push the effective idle heartbeat
        # to live race-idle units that still hold an old value (idempotent).
        self._repush_idle_hbt()

    def _repush_idle_hbt(self):
        """Push each connected race-idle unit's EFFECTIVE idle HBT (day or, in its
        night-idle window, the long night value) when it differs from what the
        device last got. Idempotent — units already holding the right value are
        skipped. Used after a live config edit and at a night-idle window edge.

        Runs on the HTTP/reload/scheduler thread while the listener's select loop
        may add/remove connections, so iterate a snapshot (like set_idle)."""
        n = 0
        for gt_conn in list(self.connections.values()):
            # Race-idle only: active units use the tracking HBT; overnight units
            # run their own scheduled-wake cadence (expected_hbt_interval = mins).
            if not gt_conn.idle or gt_conn.overnight:
                continue
            hbt = self._idle_intervals(gt_conn)["hbt"]
            if gt_conn.expected_hbt_interval == hbt:
                continue  # device already holds this value
            self._queue_commands(gt_conn, [f"HBT,{hbt},{hbt}#"])
            gt_conn.expected_hbt_interval = hbt
            n += 1
            self._log(f"[GT06] Re-pushed HBT,{hbt},{hbt}# to "
                      f"{gt_conn.sailor_id or gt_conn.imei} (idle HBT changed)")
        if n:
            self._log(f"[GT06] Idle HBT applied live to {n} idle unit(s)")

    def _log_packet(self, gt_conn, frame, outgoing=False):
        """Log a raw GT06 frame with v2 header (ts + conn_id + length).

        conn_id high bit indicates direction (1 = server→device, 0 = device→server).
        """
        ts = time.time()
        conn_id = (gt_conn.conn_id if gt_conn else 0)
        if outgoing:
            conn_id |= GT06_LOG_DIR_OUT
        header = struct.pack("<dIH", ts, conn_id & 0xFFFFFFFF, len(frame))
        # Hold the lock across the fd read + write so a concurrent rotation can't
        # swap/close the fd mid-write (no dropped or corrupted frame).
        with self._log_lock:
            if self._log_fd is None:
                return
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
        # Persist the in-memory device snapshot (incl. the last STATUS voltage and
        # last-contact) so a now-offline unit keeps its freshest reading on disk.
        self._save_device_state()

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
        # Hard safety net: MODE4 hard-bricks W07/V6.6x firmware (modem goes
        # dark, physical power-cycle to recover). Drop any MODE4 to such a
        # device whatever the source — schedule, reconcile, or a raw operator
        # command. The cxzt# overnight branch already substitutes a safe mode;
        # this catches every other path (incl. unknown firmware). V667 only.
        safe = []
        for c in cmds:
            if c.startswith("MODE4") and not _firmware_allows_mode4(gt_conn.firmware):
                self._log(f"[GT06] {gt_conn.sailor_id or gt_conn.imei}: refusing "
                          f"{c} — MODE4 unsafe on "
                          f"{gt_conn.firmware or 'unknown fw'}")
                continue
            safe.append(c)
        gt_conn.cmd_queue.extend(safe)
        self._send_next_cmd(gt_conn)

    def _send_next_cmd(self, gt_conn):
        """Send the next queued command if none is pending."""
        if gt_conn.cmd_pending is not None:
            return  # waiting for current command
        if not gt_conn.cmd_queue:
            # Queue drained — let the reconciler advance its phase. NB
            # _reconcile_advance -> _reconcile_apply -> _queue_commands ALREADY
            # sends the first corrective command (setting cmd_pending). Re-check
            # cmd_pending here: without it we fall through and send a SECOND
            # command without waiting for the first's ACK, and a later stray ACK
            # then marks the reconcile complete before that command is acked.
            self._reconcile_advance(gt_conn)
            if gt_conn.cmd_pending is not None or not gt_conn.cmd_queue:
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
            return {"SLPDISCONNECT": 0, "TIMER": f"{interval},{interval}", "SENDS": 0,
                    "GPS_RST_TIME": 300, "GPSCODEWAIT": 10, "VIBCHK": "0:16", "HBT": 15,
                    "BLIND_EN": 1}
        if state == "idle":
            # T1=acc_on (ACC-ON, moving) + T2=acc_off (ACC-OFF, parked). Parked
            # units use T2 → upload rarely → GPS stays off. ACCLINE=1: ACC follows
            # the voltage/ACC line, not vibration, so movement on an off-charge idle
            # unit doesn't assert ACC-ON (→ GPS). Intervals are the day set, or the
            # long night set when this unit's event is in its night-idle window.
            eff = self._idle_intervals(gt_conn)
            return {"SLPDISCONNECT": 0,
                    "TIMER": f"{eff['acc_on']},{eff['acc_off']}",
                    "SENDS": 1,
                    "SENALM": "OFF", "MOVING": "OFF",
                    "GPS_RST_TIME": eff["gps_rst"],
                    "GPSCODEWAIT": 10, "VIBCHK": "0:16", "ACCLINE": 1,
                    "HBT": eff["hbt"], "BLIND_EN": 0}
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
        if 'MCU:' in text:  # cxzt#: F=TIMER T1|T2 (ACC-ON|ACC-OFF), H=HBT
            m = re.search(r'\*F:(\d+)\|(\d+)', text)
            if m: obs["TIMER"] = f"{m.group(1)},{m.group(2)}"
            m = re.search(r'\*H:(\d+)', text)
            if m: obs["HBT"] = int(m.group(1))
        if 'TIMER:' in text and ';' in text:  # PARAM#
            for k, pat in (("TIMER", r'TIMER:(\d+)'), ("SENDS", r'SENDS:(\d+)'),
                           ("HBT", r'HBT:(\d+)')):
                m = re.search(pat, text)
                if m: obs[k] = int(m.group(1))
        # CXCS read / set ack. Value stops at whitespace OR a control char so the
        # device's trailing \x00\x01 (and any framing) isn't captured.
        m = re.search(r'(?:READOK|SETOK):\s*([A-Z_]+)=([^\s\x00-\x1f]+)', text)
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

    def _check_lag(self, fd, gt_conn, now):
        """Blind-buffer lag remediation (active tracking only).

        If the device's replay tip (`last_ts`, the gps_time of its most recent
        LOC) is behind wall-clock by more than `lag_remediation_sec`, its offline
        buffer is replaying at the fixed ~1Hz device rate but never catching up.
        Temporarily lower TIMER to `lag_drain_interval` so capture drops below the
        replay rate and the backlog drains (no track loss — every buffered fix
        still replays); restore the active interval once the lag falls back under
        `lag_restore_sec`. Throttled like the overnight storm guard.

        `now` is monotonic (cooldown/timeout); lag is wall-clock because
        `last_ts` is the embedded gps_time.
        """
        threshold = self._resolve_setting(gt_conn, "lag_remediation_sec", 0)
        if not threshold or threshold <= 0:
            return  # feature disabled (default)

        # Active tracking only — never touch idle / overnight / mid-reconcile.
        active = (not gt_conn.idle and not gt_conn.overnight
                  and gt_conn.target_state != "idle"
                  and gt_conn.reconcile_phase is None)
        if not active or gt_conn.last_ts is None:
            if gt_conn.lag_draining:
                self._lag_restore(gt_conn, "state change")
            return

        lag = time.time() - gt_conn.last_ts
        restore_sec = self._resolve_setting(gt_conn, "lag_restore_sec", 8)

        if gt_conn.lag_draining:
            if lag <= restore_sec:
                self._lag_restore(gt_conn, f"recovered (lag {lag:.0f}s)")
                gt_conn.lag_drain_attempts = 0   # full recovery re-arms budget
            else:
                max_drain = self._resolve_setting(gt_conn, "lag_drain_max_sec", 180)
                if now - gt_conn.lag_drain_started > max_drain:
                    self._lag_restore(gt_conn, f"timeout (lag still {lag:.0f}s)")
            return

        # Not draining.
        if lag <= threshold:
            if gt_conn.lag_drain_attempts and lag <= restore_sec:
                gt_conn.lag_drain_attempts = 0   # caught up on its own — re-arm
            return

        # Lag over threshold — consider starting a drain. Only act on a device
        # that's actively sending LOC (genuinely replaying a backlog); if LOC
        # have stopped (e.g. GPS lost), the lag is stale and a TIMER change won't
        # help — wait for it to resume.
        if now - gt_conn.last_loc_mono > 10:
            return
        # Quiet pipeline only, so we don't stack onto an in-flight reconcile /
        # slow-mode push / rate retry.
        if gt_conn.cmd_queue or gt_conn.cmd_pending is not None:
            return
        max_retries = self._resolve_setting(gt_conn, "lag_remediation_max_retries", 3)
        if gt_conn.lag_drain_attempts >= max_retries:
            return  # gave up; re-arms when lag returns to normal
        cooldown = self._resolve_setting(gt_conn, "lag_remediation_cooldown_sec", 60)
        if gt_conn.lag_drain_last and now - gt_conn.lag_drain_last < cooldown:
            return
        self._lag_start_drain(gt_conn, now, lag, threshold, max_retries)

    def _lag_start_drain(self, gt_conn, now, lag, threshold, max_retries):
        """Lower TIMER to the drain interval so the buffer replay outpaces capture."""
        drain_interval = self._resolve_setting(gt_conn, "lag_drain_interval", 2)
        gt_conn.lag_draining = True
        gt_conn.lag_drain_started = now
        gt_conn.lag_drain_last = now
        gt_conn.lag_drain_attempts += 1
        self._queue_commands(gt_conn,
                             [_SETTINGS["TIMER"][0](f"{drain_interval},{drain_interval}")])
        # Tell the rate monitor to expect the slower drain rate (don't fight it).
        self._reset_rate_monitoring(gt_conn, drain_interval)
        label = gt_conn.sailor_id or gt_conn.imei or "unknown"
        self._log(f"[GT06] {label} lag {lag:.0f}s > {threshold}s — draining backlog "
                  f"at TIMER,{drain_interval} (attempt {gt_conn.lag_drain_attempts}/{max_retries})")

    def _lag_restore(self, gt_conn, reason):
        """End the drain: restore the active LOC interval."""
        gt_conn.lag_draining = False
        interval = self.slow_loc_interval if gt_conn.slow_mode else self.interval
        self._queue_commands(gt_conn, [_SETTINGS["TIMER"][0](f"{interval},{interval}")])
        self._reset_rate_monitoring(gt_conn, interval)
        label = gt_conn.sailor_id or gt_conn.imei or "unknown"
        self._log(f"[GT06] {label} lag drain {reason} — restoring TIMER,{interval}")

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
            # Idle / overnight: the firmware ignores TIMER and uploads on its own
            # fixed cadence (~30s on every W07/V667 unit, confirmed across the
            # fleet), so a too-fast idle rate can NEVER be corrected by re-pushing
            # TIMER — _idle_cmds() just churns the radio (8 cmds + 8 ACKs) every
            # cycle, wasting battery on the very units we're trying to keep quiet.
            # Overnight is the same story (brief wake bursts trip the check).
            # Only ACTIVE tracking benefits from a re-push: there a too-slow rate
            # means the device fell behind and needs re-arming, else disconnect.
            if gt_conn.overnight or gt_conn.idle:
                gt_conn.rate_check_time = now
                gt_conn.loc_count = 0
                gt_conn.hbt_count = 0
                gt_conn.rate_retry_count = 0
                return
            if gt_conn.rate_retry_count < 2:
                gt_conn.rate_retry_count += 1
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
                self._log(f"[GT06] Rate mismatch for {label} after 2 retries — disconnecting")
                self._disconnect(fd)
                return

        # Check HBT rate — if no heartbeat for > 3x expected interval and we're getting LOC
        if gt_conn.last_hbt_time > 0 and gt_conn.loc_count > 0:
            hbt_gap = now - gt_conn.last_hbt_time
            if hbt_gap > gt_conn.expected_hbt_interval * 3:
                # Effective idle HBT (day or night-window long value), not the bare
                # day default — else a night-idle unit gets bounced back to HBT,15.
                hbt_int = self._idle_intervals(gt_conn)["hbt"] if gt_conn.idle else 15
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
            # Resolve the raw terminal id (= TERIID) to a real device + auth status.
            # Unauthenticated logins are recorded for the management UI (recovery /
            # onboarding) but get NO sailor_id, event mapping, reconcile,
            # incumbent-kick, or position publishing.
            login_id = gt06_parse_login(data)
            gt_conn.login_id = login_id
            gt_conn.login_mono = time.monotonic()   # start the reconnect grace window
            authd, imei, status = self._resolve_login(login_id)
            gt_conn.auth_status = status
            gt_conn.authenticated = authd
            self._send(gt_conn, gt06_make_response(protocol, serial))  # ACK → stays connected
            if not authd:
                # Stays fully inert in the imei/sailor-keyed paths (imei=None) so it
                # can't clobber real units or publish; only recorded for the UI.
                gt_conn.resolved_imei = imei   # display only; imei stays None
                gt_conn.eid = None
                self._record_pending(gt_conn)
                self._log(f"[GT06] Login UNAUTH ({status}): id=***{login_id[-6:]} "
                          f"imei={imei} ip={gt_conn.addr[0] if gt_conn.addr else '?'}")
                return
            # Authenticated → real IMEI (or sim/legacy login_id), sailor_id, event.
            gt_conn.imei = imei
            gt_conn.sailor_id = self._imei_to_sailor_id(imei)
            # Event routing. Sim convention: 999-prefixed ids carry the eid in
            # positions 3..5; real GT06 hardware never uses 999 as a TAC prefix.
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
            # Mask the credential prefix; keep last6 (label) + sailor for ID.
            self._log(f"[GT06] Login: id ***{login_id[-6:]} -> {gt_conn.sailor_id} "
                      f"(eid={gt_conn.eid}, {status})")

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
            manual_sleep = False  # manual /admin/sleep override still in force
            sleep_until = None    # its expiry (epoch) — clamps the sleep interval
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
                    # A manual /admin/sleep stores sleep_until = the next morning
                    # wake (or a far-future sentinel when there's no schedule).
                    # It forces overnight until that time, then expires so the
                    # unit wakes — so a manual sleep rejoins the morning wake
                    # instead of sticking forever. Schedule-driven sleeps carry
                    # NO sleep_until; the window itself governs them (out of
                    # window they fall to race-idle).
                    sleep_until = existing_pos.get("sleep_until")
                    manual_sleep = sleep_until is not None and time.time() < sleep_until

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
                gt_conn.overnight_until = None
                gt_conn.sched_overnight_mode = None
                gt_conn.sched_overnight_interval = None
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
                # Overnight vs race-day idle. Overnight when the schedule window
                # is active OR a manual /admin/sleep override is still in force;
                # otherwise race-day idle. BOTH overnight sources wake in the
                # morning: the window by going out-of-window, the manual override
                # by expiring at the next window-end. The schedule's
                # (mode, interval) is adopted when the window drove it. Only the
                # idle path reaches here, so an active/racing tracker is never
                # auto-slept.
                sched_params = None
                if self.get_event_sleep_active:
                    try:
                        sched_params = self.get_event_sleep_active(gt_conn.eid)
                    except Exception:
                        sched_params = None
                # Resolve overnight, or fall to race-idle. The schedule pins both
                # the mode and the window-clamped interval. A manual sleep pins
                # only the interval — clamped to the time left until its morning
                # wake, recomputed each wake — and leaves the MODE to per-device/
                # firmware resolution in the cxzt# handler. Within 5 min of the
                # wake a manual sleep doesn't sleep at all.
                overnight = False
                ov_int = None
                if sched_params:
                    overnight = True
                    ov_mode, ov_int = sched_params
                    gt_conn.sched_overnight_mode = ov_mode
                    gt_conn.sched_overnight_interval = ov_int
                    gt_conn.overnight_until = None
                    gt_conn.desired_mode = ov_mode
                elif manual_sleep:
                    rem_min = int((sleep_until - time.time()) // 60)
                    if rem_min > 5:
                        overnight = True
                        # Leave mode+interval to per-device/firmware resolution in
                        # the cxzt# handler; it clamps the interval to the morning
                        # wake recorded here. ov_int is just an HBT estimate.
                        gt_conn.sched_overnight_mode = None
                        gt_conn.sched_overnight_interval = None
                        gt_conn.overnight_until = sleep_until
                        gt_conn.desired_mode = self.overnight_mode_number
                        ov_int = min(self.overnight_interval_min, rem_min)
                if overnight:
                    idle_submode = "overnight"
                    gt_conn.overnight = True
                    gt_conn.overnight_freq_retries = 0
                    # Overnight: queue ONLY cxzt# probe. If the device is
                    # already in the right MODE (it usually is — wake-cycle
                    # reconnects land here), the cxzt# handler will see
                    # M:overnight_mode == desired and do nothing; device
                    # just sleeps again on its own cadence. Otherwise the
                    # handler pushes the full _overnight_cmds chain.
                    cmds = ["cxzt#"]
                    gt_conn.expected_hbt_interval = ov_int * 60
                    self._reset_rate_monitoring(gt_conn, ov_int * 60)
                else:
                    gt_conn.overnight = False
                    gt_conn.desired_mode = 1
                    gt_conn.sched_overnight_mode = None
                    gt_conn.sched_overnight_interval = None
                    gt_conn.overnight_until = None
                    # Race-day idle: table/state-driven reconcile (see active). The
                    # effective intervals are day, or the long set when this unit's
                    # event is inside its night-idle window.
                    eff = self._idle_intervals(gt_conn)
                    gt_conn.expected_hbt_interval = eff["hbt"]
                    self._reset_rate_monitoring(gt_conn, eff["acc_off"])
                    self._reconcile_begin(gt_conn, "idle")
                    cmds = None
            if cmds:
                self._queue_commands(gt_conn, cmds)
            self._log(f"[GT06] Login commands queued ({'active' if not gt_conn.idle else 'idle'})")
            # Record liveness/eid so the manager device page lists this unit
            # even before its first cxzt# response lands.
            self._record_device_state(gt_conn)

            # An un-onboarded (legacy_raw) unit is manageable but stays OFF the public
            # event map until Registered — drop any stale current_positions entry.
            if not self._publishes(gt_conn):
                self._unpublish_sailor(gt_conn)

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
                # Reflect overnight as SLEEP in the UI — process_position carries
                # idle/stopped but not the sleep flag, so a unit that goes
                # overnight on (re)connect during a sleep window would otherwise
                # show "idle" instead of "SLEEP". Set it explicitly + persist.
                with pt._lock:
                    ex = pt.current_positions.get(gt_conn.sailor_id)
                    if ex:
                        ex["idle"] = gt_conn.idle
                        ex["stopped"] = gt_conn.idle
                        ex["sleep"] = bool(gt_conn.overnight)
                        # Clear a spent manual-sleep override once the unit is no
                        # longer overnight (it expired at the morning wake, or the
                        # window/operator woke it) so it doesn't linger.
                        if not gt_conn.overnight:
                            ex.pop("sleep_until", None)
                if pt.positions_file and self._write_positions:
                    self._write_positions(
                        pt.current_positions, pt.positions_file,
                        getattr(tracker, 'user_overrides', {}), pt.position_tails)

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

            # Adaptive LOC rate: reduce interval when moving slowly. Suppressed
            # during a lag drain so slow/fast TIMER pushes don't fight the drain.
            if not gt_conn.idle and not gt_conn.lag_draining:
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
            gt_conn.last_loc_mono = time.monotonic()

            # Surface idle LOC in tracker.log. A parked idle unit should only
            # upload at the T2 (~30 min) interval with GPS off between, so a
            # stream of these = a unit stuck GPS-on in idle — the issue is then
            # obvious in the log instead of only in a packet capture. tracker.log
            # is private (same as gt06.log), so logging lat/lon here is fine.
            if gt_conn.idle:
                gt_conn.idle_loc_count += 1   # feeds the stuck-GPS-on detector
                label = gt_conn.sailor_id or gt_conn.imei or "unknown"
                fix = (f"sats={loc['satellites']}" if loc["gps_valid"]
                       else f"NO-FIX sats={loc['satellites']}")
                self._log(f"[GT06] {label} IDLE LOC {loc['lat']:.5f},"
                          f"{loc['lon']:.5f} {fix}")

            tracker = self.get_tracker(gt_conn.eid)
            if tracker is None or not self._publishes(gt_conn):
                return   # un-onboarded (legacy_raw) units don't go on the public map

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

            # Re-probe a CONNECTED overnight unit stuck in the wrong mode: if it
            # didn't switch into deep-sleep at the window-start (e.g. it was on
            # external power), re-send cxzt# every ~2 min to re-check/re-push the
            # mode (and refresh voltage). A correctly-sleeping unit is offline
            # between wakes, so this never fires for it.
            if gt_conn.overnight and now_mono - gt_conn.last_overnight_probe >= 120:
                self._queue_commands(gt_conn, ["cxzt#"])
                gt_conn.last_overnight_probe = now_mono

            # Update tracker on heartbeat only when GPS is stale (no LOC for 15s+)
            # to avoid overwriting satellite/position data from recent LOC packets
            gps_stale = gt_conn.last_ts is None or (time.time() - gt_conn.last_ts) >= 15
            if gt_conn.sailor_id and gps_stale and self._publishes(gt_conn):
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

            if is_sos and self._publishes(gt_conn) and gt_conn.sailor_id:
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

            if loc and gt_conn.sailor_id and loc["gps_valid"] and self._publishes(gt_conn):
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
            # A "MODE<n> OK" ack confirms the device adopted mode n. Update the
            # cached mode/freq now so the management UI reflects the switch
            # immediately — otherwise it shows the stale mode from this wake's
            # earlier cxzt# (which reports the PRE-switch mode) until the next
            # wake. (e.g. a unit migrating MODE4->MODE5 showed MODE4 for a cycle.)
            mode_ok = re.search(r'MODE(\d)\s+OK', text)
            if mode_ok and gt_conn.imei:
                st = self.device_state.get(gt_conn.imei)
                if st is not None:
                    st['mode'] = mode_ok.group(1)
                    fok = re.search(r'Freq:(\d+)', text)
                    if fok:
                        st['freq'] = fok.group(1)
                    self._save_device_state()
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
                # cxzt# carries *BT:<mV> and lands on EVERY overnight wake (unlike
                # STATUS#, whose round-trip often doesn't finish in the short
                # MODE5 dwell), so use it as the per-wake battery-voltage source.
                btmatch = re.search(r'\*BT:(\d+)', text)
                if btmatch:
                    gt_conn.battery_voltage = int(btmatch.group(1)) / 1000.0
                    gt_conn.battery = self._battery_percent(gt_conn, gt_conn.battery_voltage)
                mode_match = re.search(r'\*M:(\d+)', text)
                if mode_match:
                    mode = int(mode_match.group(1))
                    fmatch = re.search(r'\*F:(\d+)', text)
                    f_val = int(fmatch.group(1)) if fmatch else None
                    push_cmds = None
                    # A manual sleep whose morning wake is now within 5 min (e.g.
                    # a delayed cxzt# round-trip) must NOT (re)sleep — clear
                    # overnight so the race branch below wakes it instead.
                    # set_idle/login apply the same >5 guard up front; this is the
                    # authoritative backstop after firmware/timing are known.
                    if (gt_conn.overnight and gt_conn.overnight_until is not None
                            and int((gt_conn.overnight_until - time.time()) // 60)
                            <= 5):
                        gt_conn.overnight = False
                        gt_conn.overnight_until = None
                        gt_conn.sched_overnight_mode = None
                        gt_conn.sched_overnight_interval = None
                        gt_conn.desired_mode = 1
                    if gt_conn.overnight:
                        # Overnight intent — resolve the effective MODE for this
                        # device's firmware (per-IMEI > firmware-prefix > global).
                        # V667 honours MODE4's Freq arg; W07/V6.6x firmware does
                        # not (it clamps MODE4 to 120 and would storm), so those
                        # are configured to overnight_mode_number=1 and kept on
                        # MODE1 with a long TIMER instead.
                        # Mode and interval resolve INDEPENDENTLY: the schedule
                        # pins both; a manual sleep pins NEITHER and leaves both
                        # to per-IMEI > firmware-prefix > global resolution (here,
                        # where the firmware IS known), then clamps the interval
                        # to its morning-wake deadline just below.
                        if gt_conn.sched_overnight_mode is not None:
                            eff_mode = gt_conn.sched_overnight_mode
                        else:
                            eff_mode = self._resolve_setting(
                                gt_conn, "overnight_mode_number",
                                self.overnight_mode_number)
                        if gt_conn.sched_overnight_interval is not None:
                            eff_interval = gt_conn.sched_overnight_interval
                        else:
                            eff_interval = self._resolve_setting(
                                gt_conn, "overnight_interval_min",
                                self.overnight_interval_min)
                        # Manual-sleep clamp: cap the resolved interval at the
                        # minutes left until the morning wake so it wakes on time.
                        if gt_conn.overnight_until is not None:
                            rem_min = int(
                                (gt_conn.overnight_until - time.time()) // 60)
                            eff_interval = max(1, min(eff_interval, rem_min))
                        # Hard firmware floor: never emit MODE4 to firmware
                        # that bricks on it, whatever the source (a schedule
                        # mode bypasses _resolve_setting, so the override alone
                        # isn't enough). Fall back to the firmware's configured
                        # overnight mode, or MODE5 if that's also 4.
                        if eff_mode == 4 and not _firmware_allows_mode4(
                                gt_conn.firmware):
                            eff_mode = self._resolve_setting(
                                gt_conn, "overnight_mode_number", 5)
                            if eff_mode == 4:
                                eff_mode = 5
                            self._log(f"[GT06] {label} firmware "
                                f"{gt_conn.firmware or 'unknown'} can't do "
                                f"MODE4 safely — using MODE{eff_mode}")
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
                                    push_cmds = (_idle_cmds(loc_int, self.idle_gps_rst_time)
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
                # A cxzt# response proves the unit is alive, so reset the
                # STATUS-miss disconnect counter (a stuck overnight unit may
                # answer cxzt# but not STATUS#), and reset the overnight re-probe
                # throttle (the heartbeat re-probe counts from here).
                gt_conn.status_miss_count = 0
                gt_conn.last_overnight_probe = time.monotonic()
                # Push the fresh cxzt# voltage straight into current_positions so
                # event.html shows it on this wake even if no heartbeat follows —
                # only when THIS response actually carried *BT (btmatch).
                if btmatch and gt_conn.sailor_id:
                    tracker = self.get_tracker(gt_conn.eid)
                    if tracker:
                        pt = (tracker.position_tracker
                              if hasattr(tracker, 'position_tracker') else tracker)
                        with pt._lock:
                            ex = pt.current_positions.get(gt_conn.sailor_id)
                            if ex:
                                ex["bat_v"] = gt_conn.battery_voltage
                                ex["bat"] = gt_conn.battery
                        if pt.positions_file and self._write_positions:
                            self._write_positions(
                                pt.current_positions, pt.positions_file,
                                getattr(tracker, 'user_overrides', {}),
                                pt.position_tails)
            # Parse battery voltage from STATUS response
            vmatch = re.search(r'Battery:(\d+\.\d+)V', text)
            if vmatch:
                gt_conn.battery_voltage = float(vmatch.group(1))
                gt_conn.battery = self._battery_percent(gt_conn, gt_conn.battery_voltage)
                gt_conn.status_miss_count = 0
                # STATUS# carries fresh battery voltage ~once/min during tracking,
                # far fresher than the occasional cxzt# that fills settings.BT.
                # Reflect it (+ last-contact) in the device snapshot the management
                # UI reads — in memory only here; persisted on disconnect to avoid a
                # full state write every ~60s per unit.
                st = self.device_state.get(gt_conn.imei)
                if st is not None:
                    now_w = time.time()
                    st['battery_voltage'] = gt_conn.battery_voltage
                    st['battery'] = gt_conn.battery
                    st['last_seen'] = now_w
                    st['last_seen_iso'] = datetime.fromtimestamp(now_w).isoformat()
                self._log(f"[GT06] {label} battery voltage: {gt_conn.battery_voltage}V ({gt_conn.battery}%)")
            # Detect GPS still active during idle — re-assert the GPS-off setting,
            # but rate-limited: firing on every STATUS# (~60s) was a second churn
            # source, and on a unit that can't lock it re-sends every poll forever.
            if gt_conn.idle and 'GPS:Fail positioning' in text:
                now_mono = time.monotonic()
                if (now_mono - gt_conn.last_gps_off_resend
                        >= self.idle_gps_off_resend_sec):
                    gt_conn.last_gps_off_resend = now_mono
                    # Effective idle GPS-reset (day or night-window value), so this
                    # remediation can't overwrite the night no-fix policy with the day one.
                    gps_rst = self._idle_intervals(gt_conn)["gps_rst"]
                    self._log(f"[GT06] {label} idle but GPS active — re-asserting "
                              f"GPS_RST_TIME={gps_rst}")
                    self._queue_commands(gt_conn, [
                        f"SZCS#GPS_RST_TIME={gps_rst}",
                        "SZCS#VIBCHK=0:16"])
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
                # Redact the terminal-id (TERIID) credential before persisting /
                # returning it via the inventory API — keep last6 (label).
                if isinstance(settings.get('ID'), str) and len(settings['ID']) == 15 and settings['ID'].isdigit():
                    settings['ID'] = '***' + settings['ID'][-6:]
                st['settings'] = settings
                st['raw_cxzt'] = _redact_teriid(cxzt_text.strip())
                if 'M' in settings:
                    st['mode'] = settings['M'].split('|')[0]
                if 'F' in settings:
                    st['freq'] = settings['F'].split('|')[0]
        self.device_state[gt_conn.imei] = st
        self._save_device_state()

    def _record_pending(self, gt_conn):
        """Record an UNAUTHENTICATED connection (no valid TERIID) keyed by raw
        login_id, so it shows in the management UI for recovery/onboarding without
        clobbering the real units' IMEI-keyed device_state. Bounded (flood guard)."""
        lid = gt_conn.login_id
        if not lid:
            return
        now = time.time()
        p = self.pending_devices.get(lid, {})
        p.update({
            "login_id": lid,
            "imei": gt_conn.resolved_imei,    # resolved real imei, or None
            "status": gt_conn.auth_status,
            "src_ip": gt_conn.addr[0] if gt_conn.addr else None,
            "firmware": gt_conn.firmware or p.get("firmware"),
            "last_seen": now,
            "last_seen_iso": datetime.fromtimestamp(now).isoformat(),
        })
        p.setdefault("first_seen", now)
        self.pending_devices[lid] = p
        if len(self.pending_devices) > 200:   # cap (per-IP rate-limit deferred)
            for k, _ in sorted(self.pending_devices.items(),
                               key=lambda kv: kv[1].get("last_seen", 0))[:-200]:
                self.pending_devices.pop(k, None)

    def _conn_for_login_id(self, login_id):
        for gt_conn in self.connections.values():
            if gt_conn.login_id == login_id:
                return gt_conn
        return None

    def queue_command_by_login_id(self, login_id, cmd_str):
        """Queue a command to a connected device by its raw login_id (for
        recovery/onboarding of unauthenticated units). True if queued."""
        conns = [c for c in self.connections.values() if c.login_id == login_id]
        if not conns:
            return False
        if len(conns) > 1:
            self._log(f"[GT06] WARNING: {len(conns)} connections share login_id "
                      f"***{login_id[-6:]}; commanding the first")
        self._queue_commands(conns[0], [cmd_str])
        self._log(f"[GT06] Manager command to login_id ***{login_id[-6:]}: {cmd_str}")
        return True

    def queue_command_any(self, target, cmd_str):
        """Command a unit by IMEI (authenticated) OR raw login_id (pending recovery)."""
        return (self.queue_command_by_imei(target, cmd_str)
                or self.queue_command_by_login_id(target, cmd_str))

    def _real_imei_of(self, conn):
        """Best-effort real hardware IMEI for a connection: the authenticated imei,
        else the resolved imei, else the raw login_id when it's a 15-digit number."""
        for cand in (conn.imei, conn.resolved_imei, conn.login_id):
            if cand and isinstance(cand, str) and cand.isdigit() and len(cand) == 15:
                return cand
        return None

    def onboard_unit(self, target, eid):
        """Provision a CONNECTED unit over its live TCP link: compute its TERIID,
        push SZCS#TERIID + RESET#. `target` = the unit's imei (authenticated) or raw
        login_id (pending). Single-connection guarded (refuse if 0 or >1 live
        connections claim this identity — possible spoof). Returns
        {ok, imei, teriid} | {ok:False, error}. Caller persists provisioned+eid."""
        conn = self._conn_for_imei(target) or self._conn_for_login_id(target)
        if conn is None:
            return {"ok": False, "error": "device not connected"}
        real_imei = self._real_imei_of(conn)
        if not real_imei:
            return {"ok": False, "error": f"no real IMEI resolvable for {target}"}
        teriid = self._teriid_for(real_imei)
        if not teriid:
            return {"ok": False, "error": "no master key loaded — cannot compute TERIID"}
        # Single-connection guard: exactly one live connection must claim this unit.
        n = sum(1 for c in self.connections.values() if self._real_imei_of(c) == real_imei)
        if n != 1:
            return {"ok": False,
                    "error": f"refusing onboard: {n} live connections claim {real_imei} "
                             f"(expected exactly 1 — possible spoof)"}
        self._queue_commands(conn, [f"SZCS#TERIID={teriid}", "RESET#"])
        self._log(f"[GT06] Onboarding {real_imei} -> eid {eid}: pushed TERIID + RESET#")
        return {"ok": True, "imei": real_imei, "teriid": teriid}

    def _conn_for_imei(self, imei):
        for gt_conn in self.connections.values():
            if gt_conn.imei == imei and gt_conn.authenticated:
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
            # device_state.last_seen only advances on login/cxzt; a tracking unit
            # sends mostly LOC, so prefer the live connection's per-frame
            # last_alive_time (any frame) when connected, so "last seen" reflects
            # actual contact rather than time-since-last-cxzt.
            last_seen = st.get("last_seen")
            last_seen_iso = st.get("last_seen_iso")
            if conn is not None and getattr(conn, "last_alive_time", 0) > 0:
                wall = now - (time.monotonic() - conn.last_alive_time)
                if wall > (last_seen or 0):
                    last_seen = wall
                    last_seen_iso = datetime.fromtimestamp(wall).isoformat()
            entry = {
                "imei": imei,
                "sailor_id": (conn.sailor_id if conn else st.get("sailor_id")),
                "eid": assigned_eid,
                "firmware": (conn.firmware if conn and conn.firmware
                             else st.get("firmware")),
                "online": conn is not None,
                "last_seen": last_seen,
                "last_seen_iso": last_seen_iso,
                "battery": (conn.battery if conn and conn.battery is not None
                            and conn.battery >= 0 else st.get("battery")),
                # Freshest voltage: live connection (updated by cxzt# AND STATUS#)
                # for online units, else the persisted snapshot. settings.BT below
                # is cxzt#-only and can be stale; prefer this.
                "battery_voltage": (conn.battery_voltage if conn and conn.battery_voltage is not None
                                    else st.get("battery_voltage")),
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
                "authenticated": (conn.authenticated if conn else None),
                "provisioned": self._is_provisioned(imei),
                "auth_status": (conn.auth_status if conn else None),
            }
            # Effective overnight mode resolution (what the device would get).
            if conn is not None:
                entry["effective_overnight_mode"] = self._resolve_setting(
                    conn, "overnight_mode_number", self.overnight_mode_number)
            out.append(entry)
        # Unauthenticated connections — separate rows keyed by login_id, NEVER mapped
        # to an event; shown so the operator can recover/onboard them.
        for lid, p in self.pending_devices.items():
            live = self._conn_for_login_id(lid) is not None
            out.append({
                "login_id": lid,
                "imei": None,                    # pending rows key by login_id, NEVER imei
                "resolved_imei": p.get("imei"),  # the real imei this login claims (display)
                "sailor_id": None,
                "eid": None,
                "online": live,
                "authenticated": False,
                "auth_status": p.get("status"),   # spoof_alert | onboard | recovery
                "provisioned": False,
                "src_ip": p.get("src_ip"),
                "firmware": p.get("firmware"),
                "last_seen": p.get("last_seen"),
                "last_seen_iso": p.get("last_seen_iso"),
            })
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
        return self.queue_command_any(imei, "cxzt#")

    def reboot_device(self, imei):
        """Ask a device to reboot (GT06 RESET#)."""
        return self.queue_command_any(imei, "RESET#")

    def disconnect_device(self, imei):
        """Force-close a device's TCP socket so it reconnects (no reboot).
        Shutdown (not close) from the HTTP thread → the select loop sees EOF and
        runs the normal _disconnect on its own thread, so we never touch the
        selector cross-thread. Used to reproduce the post-reconnect blind-buffer
        replay, and to recover a wedged socket."""
        gt_conn = self._conn_for_imei(imei)
        if gt_conn is None:
            return False
        try:
            gt_conn.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        self._log(f"[GT06] {imei[-6:]} force-disconnect (manage)")
        return True

    def set_device_config(self, imei, updates):
        """Persist per-device config (e.g. {"eid": 3, "overnight_mode_number": 1}
        or {"name": "..."}) to gt06.json and apply in memory. Returns
        (ok: bool, error: str|None). eid changes take effect on the device's
        next reconnect; overnight_mode_number on its next cxzt# probe."""
        if not self.gt06_config_path:
            return False, "no gt06 config path configured"
        allowed = {"eid", "overnight_mode_number", "overnight_interval_min", "name", "provisioned"}
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
        # Apply an event (eid) change to the LIVE connection so the tracker moves
        # immediately (next report ~15s) without a reboot or server restart: update
        # gt_conn.eid and drop the stale entry from the OLD event's map.
        if "eid" in clean:
            conn = self._conn_for_imei(imei)
            if conn is not None and conn.eid != clean["eid"]:
                old_eid = conn.eid
                ot = self.get_tracker(old_eid)
                if ot and conn.sailor_id:
                    pt = (ot.position_tracker if hasattr(ot, "position_tracker") else ot)
                    with pt._lock:
                        gone = pt.current_positions.pop(conn.sailor_id, None) is not None
                    if gone and pt.positions_file and self._write_positions:
                        self._write_positions(pt.current_positions, pt.positions_file,
                                              getattr(ot, "user_overrides", {}), pt.position_tails)
                conn.eid = clean["eid"]   # live route to the new event
                self._log(f"[GT06] Live event change for {conn.sailor_id}: "
                          f"{old_eid} -> {clean['eid']} (moves on its next report)")
        self._log(f"[GT06] Manager set config for {imei}: {clean}")
        return True, None

    def forget_device(self, target):
        """Remove ALL record of a device — its event current_positions row,
        device_state, pending entry, and gt06.json devices entry. For stale/ghost
        rows (e.g. test artifacts). `target` = imei (device row) or login_id (pending
        row). Returns True if anything was removed. A still-connected unit reappears
        on its next login."""
        removed = False
        # current_positions (use the device_state eid+sailor before we drop it)
        st = self.device_state.get(target)
        if st and st.get("sailor_id") and st.get("eid") is not None:
            tracker = self.get_tracker(st["eid"])
            if tracker:
                pt = (tracker.position_tracker if hasattr(tracker, "position_tracker") else tracker)
                with pt._lock:
                    gone = pt.current_positions.pop(st["sailor_id"], None) is not None
                if gone and pt.positions_file and self._write_positions:
                    self._write_positions(pt.current_positions, pt.positions_file,
                                          getattr(tracker, "user_overrides", {}), pt.position_tails)
        if target in self.device_state:
            del self.device_state[target]
            self._save_device_state()
            removed = True
        if target in self.pending_devices:
            del self.pending_devices[target]
            removed = True
        dev = self.gt06_config.get("devices", {})
        if isinstance(dev, dict) and target in dev:
            del dev[target]
            if self.gt06_config_path:
                try:
                    disk = json.load(open(self.gt06_config_path)) if self.gt06_config_path.exists() else {}
                    if isinstance(disk.get("devices"), dict) and target in disk["devices"]:
                        del disk["devices"][target]
                        _atomic_write_json(self.gt06_config_path, disk)
                except Exception as e:
                    self._log(f"[GT06] forget_device config persist failed: {e}")
            removed = True
        if removed:
            self._log(f"[GT06] Forgot device record: {target}")
        return removed

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

    def scheduled_sleep_apply(self, eid, sleep_active,
                              overnight_mode=None, overnight_interval_min=None):
        """Transition connected units in `eid` at a night-sleep window edge.

        sleep_active=True  → put currently-IDLE units into overnight deep-sleep.
        sleep_active=False → return overnight units to race-day idle.

        ONLY idle (non-racing, non-assist) units are touched — an actively
        tracked tracker (a sailor still out) is never auto-slept. Returns the
        number of units transitioned. Reconnecting/late units are handled
        separately by the login handler via get_event_sleep_active."""
        targets = []
        for gt_conn in list(self.connections.values()):
            if gt_conn.eid != eid or not gt_conn.sailor_id:
                continue
            if sleep_active:
                if gt_conn.idle and not gt_conn.overnight and not gt_conn.assist_active:
                    targets.append(gt_conn.sailor_id)
            elif gt_conn.overnight:
                targets.append(gt_conn.sailor_id)
        for sid in targets:
            self.set_idle(eid, sid, True,
                          submode="overnight" if sleep_active else "race",
                          overnight_mode=overnight_mode,
                          overnight_interval_min=overnight_interval_min)
        return len(targets)

    def scheduled_night_idle_apply(self, eid):
        """At a night-idle window edge (day↔night), re-reconcile connected IDLE
        units in `eid` so the new effective intervals are pushed to the device.

        The keepalive/cxzt cadence switches on its own (read live by the run loop);
        this re-pushes the device-side settings (HBT, TIMER, GPS_RST). Re-running
        the idle reconcile diffs every setting and sends only what changed. ONLY
        idle (non-overnight, non-assist) units are touched — an actively-tracked
        tracker is never auto-changed. Returns the number re-reconciled."""
        targets = [c.sailor_id for c in list(self.connections.values())
                   if c.eid == eid and c.sailor_id and c.idle
                   and not c.overnight and not c.assist_active]
        for sid in targets:
            self.set_idle(eid, sid, True, submode="race")
        return len(targets)

    def set_idle(self, eid, sailor_id, idle, submode="race",
                 overnight_mode=None, overnight_interval_min=None,
                 overnight_until=None):
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
        # Snapshot — this runs on the scheduler/HTTP threads while the listener's
        # select loop may add/remove connections, so iterating the live dict
        # could raise "dict changed size during iteration".
        for gt_conn in list(self.connections.values()):
            if gt_conn.eid == eid and gt_conn.sailor_id == sailor_id:
                gt_conn.idle = idle
                gt_conn.slow_mode = False
                gt_conn.slow_since = 0
                # Clear any pending commands from previous state. Abort any
                # in-flight reconcile too, otherwise a stale phase would, once
                # the new state's probe drains, apply the OLD target's settings
                # (e.g. active/idle config onto a device we just put to sleep).
                # The active/idle branches below restart reconcile cleanly; the
                # overnight branch deliberately leaves it off.
                gt_conn.cmd_queue.clear()
                gt_conn.cmd_pending = None
                gt_conn.reconcile_phase = None
                if idle:
                    # Within ~5 min of the morning wake a manual sleep isn't
                    # worth it — stay race-idle (matches the login/schedule rule).
                    if (submode == "overnight" and overnight_until is not None
                            and int((overnight_until - time.time()) // 60) <= 5):
                        submode = "race"
                    if submode == "overnight":
                        gt_conn.overnight = True
                        gt_conn.overnight_freq_retries = 0
                        # Pin state for the cxzt# handler, which does the
                        # firmware-aware resolution. The schedule pins both mode
                        # and its (already window-clamped) interval; a manual
                        # sleep pins NEITHER and instead records its morning-wake
                        # deadline (overnight_until) so the cxzt# handler resolves
                        # per-device/firmware THEN clamps the interval to it.
                        if overnight_mode is not None:
                            gt_conn.sched_overnight_mode = overnight_mode
                            gt_conn.desired_mode = overnight_mode
                        else:
                            gt_conn.sched_overnight_mode = None
                            gt_conn.desired_mode = self.overnight_mode_number
                        gt_conn.sched_overnight_interval = overnight_interval_min
                        gt_conn.overnight_until = overnight_until
                        ov_int = overnight_interval_min or self.overnight_interval_min
                        # Defer to the cxzt# handler — it resolves the firmware-
                        # appropriate overnight MODE and pushes it only if needed.
                        cmds = ["cxzt#"]
                        gt_conn.expected_hbt_interval = ov_int * 60
                        self._reset_rate_monitoring(gt_conn, ov_int * 60)
                    else:
                        gt_conn.overnight = False
                        gt_conn.overnight_until = None
                        gt_conn.sched_overnight_mode = None
                        gt_conn.sched_overnight_interval = None
                        gt_conn.desired_mode = 1
                        eff = self._idle_intervals(gt_conn)
                        gt_conn.expected_hbt_interval = eff["hbt"]
                        self._reset_rate_monitoring(gt_conn, eff["acc_off"])
                        self._reconcile_begin(gt_conn, "idle")
                        cmds = None
                else:
                    gt_conn.overnight = False
                    gt_conn.overnight_until = None
                    gt_conn.sched_overnight_mode = None
                    gt_conn.sched_overnight_interval = None
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
                            # Waking (race/active) clears any manual-sleep
                            # override; sleep_until itself is set by the
                            # /admin/sleep handler, not here.
                            if not (idle and submode == "overnight"):
                                existing.pop("sleep_until", None)
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
        # Write the magic into the new file BEFORE publishing the fd, so a
        # concurrent _log_packet never writes a frame ahead of the header.
        fd = open(path, "ab")
        if path.stat().st_size == 0:
            fd.write(GT06_LOG_MAGIC_V2)
            fd.flush()
        self._log_fd = fd
        self._log(f"[GT06] Packet logging to {path} (v2 format)")

    def rotate_log_to(self, archive_path):
        """Move the current packet log to archive_path and open a fresh log.

        Safe to call from any thread WITHOUT losing frames: the old fd keeps
        writing to the renamed (archived) inode and is only closed AFTER the fresh
        fd is swapped into self._log_fd by a single atomic assignment (in
        _open_log_v2). _log_fd is never set to None here, so a concurrent
        _log_packet (run() thread) always sees a valid fd — frames at the instant
        of rotation land in either the archived or the new file, never dropped.
        """
        if not self.log_file:
            return
        path = Path(self.log_file) if not isinstance(self.log_file, Path) else self.log_file
        # Hold the log lock for the whole swap so no _log_packet runs against a
        # half-rotated state (None fd, magic-less new file, or a just-closed fd).
        with self._log_lock:
            old_fd = self._log_fd
            try:
                if path.exists():
                    path.rename(archive_path)
                # old_fd still serves _log_packet (archived inode) until this swaps
                # in the new fd. Do NOT clear _log_fd first — that window drops frames.
                self._open_log_v2(path)
            finally:
                # Close the old fd only once the swap actually happened; if
                # _open_log_v2 raised, _log_fd still points at old_fd (writes keep
                # landing in the archived file — mis-filed but not lost) so leave it.
                if old_fd is not None and old_fd is not self._log_fd:
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
            PRELOGIN_DEADLINE_S = 30   # accept→LOGIN must complete within this
            PENDING_DEADLINE_S = 600   # logged-in but UNAUTHENTICATED (pending
                                       # recovery/onboarding): keep it commandable a
                                       # while, but bounded so spoofers don't camp
            for fd in list(self.connections):
                gt_conn = self.connections.get(fd)
                if gt_conn is None:
                    continue
                # Expire connections without an event mapping. Never-logged-in
                # (login_id None) get the short pre-login deadline. Logged-in-but-
                # UNAUTHENTICATED (pending recovery/onboarding) get a longer one so
                # they stay listed + commandable, but bounded so spoofers can't camp.
                if gt_conn.sailor_id is None:
                    deadline = (PRELOGIN_DEADLINE_S if gt_conn.login_id is None
                                else PENDING_DEADLINE_S)
                    if now - gt_conn.connected_at > deadline:
                        kind = "pre-login" if gt_conn.login_id is None else "pending"
                        self._log(f"[GT06] {kind} timeout ({deadline}s) for "
                                  f"{gt_conn.addr[0]}:{gt_conn.addr[1]} (conn_id={gt_conn.conn_id})")
                        self._disconnect(fd)
                    continue
                # Check SIOCOUTQ for pending commands
                if gt_conn.cmd_pending:
                    self._check_cmd_delivery(fd, gt_conn, now)
                # Check LOC/HBT rates
                self._check_rates(fd, gt_conn, now)
                # Blind-buffer lag remediation (active devices only)
                self._check_lag(fd, gt_conn, now)
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
                # Effective idle intervals: day, or the long night set when this
                # unit's event is inside its night-idle window.
                eff_idle = self._idle_intervals(gt_conn) if gt_conn.idle else None
                if (gt_conn.idle
                        and eff_idle["keepalive"]
                        and gt_conn.cmd_pending is None
                        and not gt_conn.cmd_queue
                        and gt_conn.last_alive_time > 0):
                    alive_gap = now - gt_conn.last_alive_time
                    poll_gap = now - gt_conn.last_idle_poll_time
                    if alive_gap >= eff_idle["keepalive"] and poll_gap >= eff_idle["keepalive"]:
                        gt_conn.last_idle_poll_time = now
                        self._queue_commands(gt_conn, ["STATUS#"])

                # Periodic cxzt# battery-sampling poll (idle AND tracking). cxzt#
                # returns mV-resolution battery (*BT) — far finer than STATUS#'s
                # 10mV — so a poll every cxzt_poll_min minutes gives clean drain
                # samples for calibration. Off by default (0). Skip overnight
                # (deep-sleep units have their own cxzt-on-wake flow) and while a
                # command is in flight so it never disturbs a reconcile.
                cxzt_min = eff_idle["cxzt_min"] if gt_conn.idle else self.cxzt_poll_min
                if (cxzt_min
                        and not gt_conn.overnight
                        and gt_conn.cmd_pending is None
                        and not gt_conn.cmd_queue
                        and now - gt_conn.login_mono >= self.reconnect_grace_sec
                        and now - gt_conn.last_cxzt_poll_time >= cxzt_min * 60):
                    gt_conn.last_cxzt_poll_time = now
                    self._queue_commands(gt_conn, ["cxzt#"])

                # Detect-and-remediate a unit stuck GPS-on in idle. A parked idle
                # unit uploads ~once per T2 (30 min); a stream of LOC means it
                # latched into continuous-tracking on reconnect — a firmware
                # runtime-state latch that config re-pushes do NOT clear, only a
                # MODE re-entry does (confirmed on hardware 2026-06-21). Past the
                # grace period, if idle LOC rate is high, bounce once with MODE1;
                # cap at idle_stuck_max_bounces then flag idle-degraded and leave
                # it alone (never churn forever — the MODE4-storm lesson). The
                # bounce drops TCP -> reconnect, so the bounce count + degraded
                # flag live in device_state (survive the reconnect; the per-conn
                # window resets naturally on the new connection).
                if (self.idle_stuck_bounce and gt_conn.idle and not gt_conn.overnight
                        and gt_conn.cmd_pending is None and not gt_conn.cmd_queue
                        and now - gt_conn.login_mono >= self.reconnect_grace_sec):
                    if gt_conn.idle_loc_window_start == 0:
                        gt_conn.idle_loc_window_start = now
                    elif now - gt_conn.idle_loc_window_start >= self.idle_stuck_window_sec:
                        span = now - gt_conn.idle_loc_window_start
                        per_min = gt_conn.idle_loc_count * 60.0 / span if span > 0 else 0
                        st = self.device_state.get(gt_conn.imei)
                        label = gt_conn.sailor_id or gt_conn.imei or "unknown"
                        if st is not None and per_min >= self.idle_stuck_loc_per_min:
                            n = st.get("idle_stuck_bounces", 0)
                            if st.get("idle_degraded"):
                                pass
                            elif n < self.idle_stuck_max_bounces:
                                st["idle_stuck_bounces"] = n + 1
                                self._log(f"[GT06] {label} stuck GPS-on in idle "
                                          f"({per_min:.0f} LOC/min) — MODE1 bounce "
                                          f"{n + 1}/{self.idle_stuck_max_bounces}")
                                self._queue_commands(gt_conn, ["MODE1,60,600#"])
                            else:
                                st["idle_degraded"] = True
                                self._log(f"[GT06] {label} still stuck GPS-on after "
                                          f"{self.idle_stuck_max_bounces} MODE1 bounces "
                                          f"— idle-degraded, leaving alone")
                        elif st is not None and (st.get("idle_stuck_bounces")
                                                 or st.get("idle_degraded")):
                            st["idle_stuck_bounces"] = 0   # recovered to parked
                            st["idle_degraded"] = False
                        gt_conn.idle_loc_window_start = now
                        gt_conn.idle_loc_count = 0

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
