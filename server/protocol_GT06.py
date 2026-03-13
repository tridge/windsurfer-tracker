"""GT06 GPS Tracker Protocol handler.

Extracted from tracker_server.py to keep protocol-specific code separate.
This module must NOT import tracker_server to avoid circular imports.
All server interactions happen through callbacks passed to GT06Listener.
"""

import fcntl
import json
import math
import selectors
import socket
import struct
import time
from calendar import timegm
from datetime import datetime
from pathlib import Path


# Battery level mapping: GT06 reports 0-6, server expects 0-100
_GT06_BATTERY_MAP = {0: 0, 1: 5, 2: 15, 3: 30, 4: 50, 5: 75, 6: 100}


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
        result["charging"] = bool(info & 0x08)
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
            "devices": cfg.get("devices", {}),
        }
        _log(f"[GT06] Loaded config from {config_path}: {len(result['devices'])} device(s), default_eid={result['default_eid']}")
        return result
    except Exception as e:
        _log(f"[GT06] Warning: Could not load {config_path}: {e}")
        return default


class GT06Connection:
    """State for one GT06 TCP connection."""

    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.buf = b""
        self.imei = None
        self.sailor_id = None
        self.eid = None
        self.battery = -1
        self.signal = -1
        self.charging = None
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
                 write_positions_func=None):
        self.port = port
        self.interval = interval
        self.id_prefix = id_prefix
        self.get_tracker = get_tracker_func
        self.gt06_config = gt06_config or {"default_eid": 1, "devices": {}}
        self.connections = {}  # fd -> GT06Connection
        self.sel = selectors.DefaultSelector()
        self.log_file = log_file
        self._log_fd = None
        self.idle_sailors = set()    # sailor_ids currently idle
        self.active_sailors = set()  # sailor_ids explicitly started by admin
        self._sticky_assist: set = set()  # IMEIs with sticky SOS active
        self._log = log_func or _default_log
        self._save_overrides = save_overrides_func
        self._write_positions = write_positions_func

    def _log_packet(self, frame):
        """Log a raw GT06 frame with timestamp+length header."""
        if self._log_fd is None:
            return
        ts = time.time()
        header = struct.pack("<dH", ts, len(frame))
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
        gt_conn = GT06Connection(conn, addr)
        self.connections[fd] = gt_conn
        self.sel.register(conn, selectors.EVENT_READ, data=fd)
        self._log(f"[GT06] Connection from {addr[0]}:{addr[1]}")

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
            self._log_packet(data)
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

    def _reset_rate_monitoring(self, gt_conn, expected_loc_interval):
        """Reset rate monitoring counters after a state transition."""
        gt_conn.rate_check_time = time.monotonic()
        gt_conn.loc_count = 0
        gt_conn.hbt_count = 0
        gt_conn.expected_loc_interval = expected_loc_interval
        gt_conn.rate_retry_count = 0

    def _check_rates(self, fd, gt_conn, now):
        """Check LOC/HBT rates and retry or disconnect if device ignores commands."""
        if gt_conn.rate_check_time == 0:
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
            if gt_conn.rate_retry_count < 2:
                gt_conn.rate_retry_count += 1
                if gt_conn.idle:
                    cmds = ["TIMER,60,60#", "SUP,60#"]
                else:
                    cmds = [f"TIMER,{self.interval},{self.interval}#", "SUP,1#"]
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
                self._log(f"[GT06] No heartbeat from {label} for {hbt_gap:.0f}s — re-queuing HBT")
                self._queue_commands(gt_conn, ["HBT,15,15#"])
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

            # Look up IMEI in gt06_config for event routing
            dev_cfg = self.gt06_config["devices"].get(imei, {})
            gt_conn.eid = dev_cfg.get("eid", self.gt06_config["default_eid"])
            self._log(f"[GT06] Login: IMEI {imei} -> {gt_conn.sailor_id} (eid={gt_conn.eid})")
            self._send(gt_conn, gt06_make_response(protocol, serial))

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

            # Determine idle/active state for this device
            if gt_conn.sailor_id in self.active_sailors:
                # Admin explicitly started this device — resume active tracking
                gt_conn.idle = False
                cmds = [f"TIMER,{self.interval},{self.interval}#", "SUP,1#", "HBT,15,15#"]
                self._reset_rate_monitoring(gt_conn, self.interval)
            else:
                # Default to idle (including first-ever connection)
                gt_conn.idle = True
                self.idle_sailors.add(gt_conn.sailor_id)
                cmds = ["TIMER,60,60#", "SUP,60#", "HBT,15,15#"]
                self._reset_rate_monitoring(gt_conn, 60)
            self._queue_commands(gt_conn, cmds)
            self._log(f"[GT06] Login commands queued ({'active' if not gt_conn.idle else 'idle'})")

            # Restore sticky SOS across TCP reconnects
            if imei in self._sticky_assist:
                gt_conn.assist_active = True
                if gt_conn.idle:
                    self.set_idle(gt_conn.sailor_id, False)
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
                return

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
            )

        elif protocol == 0x13:
            # Heartbeat
            hb = gt06_parse_heartbeat(data)
            if "battery" in hb:
                gt_conn.battery = hb["battery"]
            if "signal" in hb:
                gt_conn.signal = hb["signal"]
            if "charging" in hb:
                gt_conn.charging = hb["charging"]

            gt_conn.hbt_count += 1
            gt_conn.last_hbt_time = time.monotonic()

            bat_str = f"{gt_conn.battery}%" if gt_conn.battery >= 0 else "?"
            if gt_conn.charging:
                bat_str += "+"
            sig_str = f"{gt_conn.signal}/4" if gt_conn.signal >= 0 else "?"
            label = gt_conn.sailor_id or gt_conn.imei or "unknown"
            self._log(f"[GT06] Heartbeat {label}: bat={bat_str} sig={sig_str}{' (idle)' if gt_conn.idle else ''}")
            self._send(gt_conn, gt06_make_response(protocol, serial))

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
                        self.set_idle(gt_conn.sailor_id, False)
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
                    )

        elif protocol == 0x15:
            # Server command response — clear pending, advance queue
            label = gt_conn.sailor_id or gt_conn.imei or "unknown"
            text = ""
            if len(data) >= 5:
                text = " " + data[5:].decode("ascii", errors="replace")
            self._log(f"[GT06] Command ACK from {label}:{text}")
            gt_conn.cmd_pending = None
            gt_conn.cmd_pending_frame = None
            self._send_next_cmd(gt_conn)

    def send_command_to(self, sailor_id, cmd_str):
        """Send a command to a connected GT06 device by sailor_id."""
        for gt_conn in self.connections.values():
            if gt_conn.sailor_id == sailor_id:
                self._queue_commands(gt_conn, [cmd_str])
                return True
        return False

    def cancel_assist(self, sailor_id):
        """Cancel SOS assist for a GT06 device."""
        for gt_conn in self.connections.values():
            if gt_conn.sailor_id == sailor_id and gt_conn.assist_active:
                gt_conn.assist_active = False
                self._queue_commands(gt_conn, ["SENALM,OFF#"])
                if gt_conn.imei:
                    self._sticky_assist.discard(gt_conn.imei)
                self._log(f"[GT06] Cancelled assist for {sailor_id} (sticky cleared)")
                return True
        return False

    def set_idle(self, sailor_id, idle):
        """Set idle state for a GT06 device by sailor_id.

        When idle=True: set SUP,60# (60-min static interval) + TIMER,60,60# (max GPS
        interval) so stationary devices barely report.  Heartbeats still arrive every minute.
        When idle=False: restore normal TIMER + SUP,1# so the device reports frequently.
        """
        if idle:
            self.idle_sailors.add(sailor_id)
            self.active_sailors.discard(sailor_id)
        else:
            self.idle_sailors.discard(sailor_id)
            self.active_sailors.add(sailor_id)

        for gt_conn in self.connections.values():
            if gt_conn.sailor_id == sailor_id:
                gt_conn.idle = idle
                # Clear any pending commands from previous state
                gt_conn.cmd_queue.clear()
                gt_conn.cmd_pending = None
                if idle:
                    cmds = ["TIMER,60,60#", "SUP,60#"]
                    self._reset_rate_monitoring(gt_conn, 60)
                else:
                    cmds = [f"TIMER,{self.interval},{self.interval}#", "SUP,1#"]
                    self._reset_rate_monitoring(gt_conn, self.interval)
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
                            now = time.time()
                            existing["last_seen"] = now
                            existing["last_seen_iso"] = datetime.fromtimestamp(now).isoformat()
                    if pt.positions_file and self._write_positions:
                        overrides = tracker.user_overrides if hasattr(tracker, 'user_overrides') else {}
                        self._write_positions(pt.current_positions, pt.positions_file, overrides, pt.position_tails)
                self._log(f"[GT06] {'Idle' if idle else 'Active'} mode for {sailor_id}")
                return True
        # No active connection, but state is saved for reconnection
        self._log(f"[GT06] {'Idle' if idle else 'Active'} mode queued for {sailor_id} (not connected)")
        return False

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
            self._log_packet(frame)

            try:
                self._process_frame(fd, frame)
            except Exception as e:
                label = gt_conn.sailor_id or gt_conn.imei or "unknown"
                self._log(f"[GT06] Frame error from {label}: {e}")

    def run(self):
        """Main loop — runs in a daemon thread."""
        if self.log_file:
            try:
                self._log_fd = open(self.log_file, "ab")
                self._log(f"[GT06] Packet logging to {self.log_file}")
            except Exception as e:
                self._log(f"[GT06] Warning: Could not open packet log {self.log_file}: {e}")

        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.setblocking(False)
        server_sock.bind(("0.0.0.0", self.port))
        server_sock.listen(16)
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
            for fd in list(self.connections):
                gt_conn = self.connections.get(fd)
                if gt_conn is None or gt_conn.sailor_id is None:
                    continue
                # Check SIOCOUTQ for pending commands
                if gt_conn.cmd_pending:
                    self._check_cmd_delivery(fd, gt_conn, now)
                # Check LOC/HBT rates
                self._check_rates(fd, gt_conn, now)
