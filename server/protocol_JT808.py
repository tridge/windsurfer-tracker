"""JT808 GPS Tracker Protocol handler (JT/T 808-2013).

Handles Chinese-standard JT808 GPS trackers over TCP.
This module must NOT import tracker_server to avoid circular imports.
All server interactions happen through callbacks passed to JT808Listener.

Frame format: 0x7e [header+body escaped] [checksum] 0x7e
Escaping: 0x7e -> 0x7d 0x02, 0x7d -> 0x7d 0x01
Checksum: XOR of all unescaped header+body bytes
Header: msg_id(2) + attributes(2) + phone_bcd(6) + serial(2) = 12 bytes
"""

import json
import selectors
import socket
import struct
import time
from calendar import timegm
from datetime import datetime, timezone, timedelta
from pathlib import Path


def _default_log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} {msg}")


# --- Escaping / framing ---

def jt808_escape(data: bytes) -> bytes:
    """Escape 0x7e and 0x7d in data for transmission."""
    out = bytearray()
    for b in data:
        if b == 0x7d:
            out.extend(b"\x7d\x01")
        elif b == 0x7e:
            out.extend(b"\x7d\x02")
        else:
            out.append(b)
    return bytes(out)


def jt808_unescape(data: bytes) -> bytes:
    """Reverse JT808 escaping."""
    out = bytearray()
    i = 0
    while i < len(data):
        if data[i] == 0x7d and i + 1 < len(data):
            if data[i + 1] == 0x01:
                out.append(0x7d)
                i += 2
                continue
            elif data[i + 1] == 0x02:
                out.append(0x7e)
                i += 2
                continue
        out.append(data[i])
        i += 1
    return bytes(out)


def jt808_checksum(data: bytes) -> int:
    """XOR checksum over all bytes."""
    cs = 0
    for b in data:
        cs ^= b
    return cs


def jt808_build_frame(msg_id: int, phone_bcd: bytes, serial: int, body: bytes = b"") -> bytes:
    """Build a complete JT808 frame ready for transmission.

    Args:
        msg_id: 2-byte message ID
        phone_bcd: 6-byte BCD phone/IMEI
        serial: 2-byte message serial number
        body: message body bytes
    """
    body_len = len(body)
    # attributes: bits 0-9 = body length, no encryption, no sub-packaging
    attributes = body_len & 0x03FF
    header = struct.pack(">HH", msg_id, attributes) + phone_bcd + struct.pack(">H", serial)
    payload = header + body
    cs = jt808_checksum(payload)
    escaped = jt808_escape(payload + bytes([cs]))
    return b"\x7e" + escaped + b"\x7e"


def jt808_parse_header(data: bytes):
    """Parse a JT808 header from unescaped data.

    Returns (msg_id, attributes, phone_bcd, serial, body_offset) or None.
    Minimum header is 12 bytes.
    """
    if len(data) < 12:
        return None
    msg_id = struct.unpack(">H", data[0:2])[0]
    attributes = struct.unpack(">H", data[2:4])[0]
    phone_bcd = data[4:10]
    serial = struct.unpack(">H", data[10:12])[0]
    body_offset = 12
    # Check for sub-packaging (bit 13)
    if attributes & (1 << 13):
        body_offset = 16  # extra 4 bytes for package info
    return msg_id, attributes, phone_bcd, serial, body_offset


def phone_bcd_to_imei(phone_bcd: bytes) -> str:
    """Convert 6-byte BCD phone field to IMEI string.

    These devices put the IMEI (up to 15 digits) into the 6-byte BCD field,
    left-padded with zeros to fill 12 BCD digits.
    """
    hex_str = phone_bcd.hex()
    # Strip leading zeros but keep at least 6 chars
    return hex_str.lstrip("0") or "0"


def imei_to_phone_bcd(imei: str) -> bytes:
    """Convert IMEI string to 6-byte BCD for header."""
    # Pad to 12 hex chars (6 bytes)
    padded = imei.zfill(12)
    if len(padded) > 12:
        padded = padded[-12:]
    return bytes.fromhex(padded)


def _make_auth_code(phone_bcd: bytes) -> str:
    """Generate a deterministic auth code from phone BCD.

    Deterministic so reconnects work without storing state.
    """
    # Simple: hex of phone_bcd (always the same for same device)
    return phone_bcd.hex()


# --- Message builders ---

def build_general_response(phone_bcd: bytes, serial: int,
                           resp_serial: int, resp_id: int, result: int) -> bytes:
    """Build platform general response (0x8001).

    Args:
        phone_bcd: device phone BCD
        serial: our serial number for this response
        resp_serial: serial number of the message being responded to
        resp_id: message ID being responded to
        result: 0=success, 1=failure, 2=bad msg, 3=unsupported, 4=alarm confirm
    """
    body = struct.pack(">HHB", resp_serial, resp_id, result)
    return jt808_build_frame(0x8001, phone_bcd, serial, body)


def build_registration_response(phone_bcd: bytes, serial: int,
                                 resp_serial: int, result: int,
                                 auth_code: str = "") -> bytes:
    """Build terminal registration response (0x8100).

    Args:
        resp_serial: serial of the 0x0100 registration message
        result: 0=success, 1=vehicle registered, 2=no vehicle, 3=terminal registered, 4=no terminal
        auth_code: authentication code string (only sent on success)
    """
    body = struct.pack(">HB", resp_serial, result)
    if result == 0 and auth_code:
        body += auth_code.encode("ascii")
    return jt808_build_frame(0x8100, phone_bcd, serial, body)


def build_tracking_control(phone_bcd: bytes, serial: int,
                            interval: int, validity: int = 0) -> bytes:
    """Build temporary location tracking control (0x8202).

    Args:
        interval: reporting interval in seconds (0 = stop tracking)
        validity: how long this control is valid in seconds (0 = permanent until next command)
    """
    body = struct.pack(">HI", interval, validity)
    return jt808_build_frame(0x8202, phone_bcd, serial, body)


# --- Location parsing ---

def parse_location(body: bytes):
    """Parse location report body (0x0200).

    Returns dict with alarm_flags, status, lat, lon, speed_knots, heading, ts,
    gps_valid, battery, signal, satellites, or None on error.
    """
    if len(body) < 28:
        return None

    alarm_flags = struct.unpack(">I", body[0:4])[0]
    status = struct.unpack(">I", body[4:8])[0]
    lat_raw = struct.unpack(">I", body[8:12])[0]
    lon_raw = struct.unpack(">I", body[12:16])[0]
    altitude = struct.unpack(">H", body[16:18])[0]
    speed_raw = struct.unpack(">H", body[18:20])[0]  # 1/10 km/h
    heading = struct.unpack(">H", body[20:22])[0]

    # Time: BCD[6] YY-MM-DD-HH-MM-SS in GMT+8
    time_bcd = body[22:28]
    yy = ((time_bcd[0] >> 4) * 10) + (time_bcd[0] & 0x0F)
    mo = ((time_bcd[1] >> 4) * 10) + (time_bcd[1] & 0x0F)
    dd = ((time_bcd[2] >> 4) * 10) + (time_bcd[2] & 0x0F)
    hh = ((time_bcd[3] >> 4) * 10) + (time_bcd[3] & 0x0F)
    mi = ((time_bcd[4] >> 4) * 10) + (time_bcd[4] & 0x0F)
    ss = ((time_bcd[5] >> 4) * 10) + (time_bcd[5] & 0x0F)

    # Convert GMT+8 to UTC unix timestamp
    try:
        gmt8_ts = timegm((2000 + yy, mo, dd, hh, mi, ss))
        ts = gmt8_ts - 8 * 3600  # subtract 8 hours for UTC
    except Exception:
        ts = int(time.time())

    # GPS valid: status bit 1
    gps_valid = bool(status & (1 << 1))

    # Direction: bit 2 = south, bit 3 = west
    lat = lat_raw / 1_000_000.0
    lon = lon_raw / 1_000_000.0
    if status & (1 << 2):
        lat = -lat
    if status & (1 << 3):
        lon = -lon

    # Speed: 1/10 km/h -> knots
    speed_kmh = speed_raw / 10.0
    speed_knots = speed_kmh / 1.852

    result = {
        "alarm_flags": alarm_flags,
        "status": status,
        "lat": lat,
        "lon": lon,
        "altitude": altitude,
        "speed_knots": round(speed_knots, 1),
        "heading": heading,
        "ts": ts,
        "gps_valid": gps_valid,
        "is_sos": bool(alarm_flags & 1),
    }

    # Parse additional info TLVs after the 28-byte base
    extra = body[28:]
    i = 0
    while i + 1 < len(extra):
        tlv_id = extra[i]
        tlv_len = extra[i + 1]
        if i + 2 + tlv_len > len(extra):
            break
        tlv_data = extra[i + 2:i + 2 + tlv_len]

        if tlv_id == 0x30 and tlv_len == 1:
            # Wireless signal strength
            result["signal"] = min(tlv_data[0], 4)  # clamp to 0-4
        elif tlv_id == 0x31 and tlv_len == 1:
            # Satellite count
            result["satellites"] = tlv_data[0]
        elif tlv_id == 0xE4 and tlv_len == 2:
            # Battery: status(1) + level(1)
            # status: 0=charging, 1=not charging
            result["charging"] = (tlv_data[0] == 0)
            result["battery"] = min(tlv_data[1], 100)

        i += 2 + tlv_len

    return result


# --- Config ---

def load_jt808_config(config_path: Path, log_func=None) -> dict:
    """Load JT808 device config from JSON file.

    Returns {"default_eid": int, "devices": {imei: {...}}}.
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
        _log(f"[JT808] Loaded config from {config_path}: {len(result['devices'])} device(s), default_eid={result['default_eid']}")
        return result
    except Exception as e:
        _log(f"[JT808] Warning: Could not load {config_path}: {e}")
        return default


# --- Connection / Listener ---

class JT808Connection:
    """State for one JT808 TCP connection."""

    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.buf = b""
        self.phone_bcd = None
        self.imei = None
        self.sailor_id = None
        self.eid = None
        self.battery = -1
        self.signal = -1
        self.charging = None
        self.authenticated = False
        self.assist_active = False
        self.idle = False
        self.last_lat = None
        self.last_lon = None
        self.last_ts = None
        self.serial = 0  # our outgoing serial counter

    def next_serial(self):
        self.serial += 1
        return self.serial


class JT808Listener:
    """Non-blocking TCP listener for JT808 GPS tracker devices.

    Runs in a single daemon thread using selectors. Mirrors GT06Listener architecture.

    Dependency injection parameters:
      - get_tracker_func: callable(eid) -> tracker object
      - log_func: callable(msg) for logging
      - save_overrides_func: callable(users_file, overrides) to persist user overrides
      - write_positions_func: callable(positions, path, overrides, tails) to write positions JSON
    """

    def __init__(self, port, interval, id_prefix, get_tracker_func, config=None,
                 log_file=None, log_func=None, save_overrides_func=None,
                 write_positions_func=None):
        self.port = port
        self.interval = interval
        self.id_prefix = id_prefix
        self.get_tracker = get_tracker_func
        self.config = config or {"default_eid": 1, "devices": {}}
        self.connections = {}  # fd -> JT808Connection
        self.sel = selectors.DefaultSelector()
        self.log_file = log_file
        self._log_fd = None
        self.idle_sailors = set()
        self.active_sailors = set()
        self._sticky_assist: set = set()  # IMEIs with sticky SOS
        self._log = log_func or _default_log
        self._save_overrides = save_overrides_func
        self._write_positions = write_positions_func

    def _log_packet(self, frame):
        """Log a raw frame with timestamp+length header."""
        if self._log_fd is None:
            return
        ts = time.time()
        header = struct.pack("<dH", ts, len(frame))
        try:
            self._log_fd.write(header + frame)
            self._log_fd.flush()
        except Exception as e:
            self._log(f"[JT808] Packet log write error: {e}")

    def _imei_to_sailor_id(self, imei):
        return self.id_prefix + imei[-6:]

    def _accept(self, server_sock):
        conn, addr = server_sock.accept()
        conn.setblocking(False)
        fd = conn.fileno()
        jt_conn = JT808Connection(conn, addr)
        self.connections[fd] = jt_conn
        self.sel.register(conn, selectors.EVENT_READ, data=fd)
        self._log(f"[JT808] Connection from {addr[0]}:{addr[1]}")

    def _disconnect(self, fd):
        jt_conn = self.connections.pop(fd, None)
        if jt_conn is None:
            return
        try:
            self.sel.unregister(jt_conn.sock)
        except Exception:
            pass
        try:
            jt_conn.sock.close()
        except Exception:
            pass
        label = jt_conn.sailor_id or jt_conn.imei or "unknown"
        self._log(f"[JT808] Disconnected: {label} ({jt_conn.addr[0]}:{jt_conn.addr[1]})")

    def _send(self, jt_conn, data):
        try:
            jt_conn.sock.sendall(data)
            self._log_packet(data)
        except Exception as e:
            self._log(f"[JT808] Send error to {jt_conn.addr}: {e}")
            self._disconnect(jt_conn.sock.fileno())

    def _send_general_response(self, jt_conn, resp_serial, resp_id, result=0):
        """Send platform general response (0x8001)."""
        frame = build_general_response(
            jt_conn.phone_bcd, jt_conn.next_serial(),
            resp_serial, resp_id, result)
        self._send(jt_conn, frame)

    def _send_tracking_control(self, jt_conn, interval, validity=0):
        """Send temporary tracking control (0x8202)."""
        frame = build_tracking_control(
            jt_conn.phone_bcd, jt_conn.next_serial(),
            interval, validity)
        self._send(jt_conn, frame)

    def _process_message(self, fd, msg_id, phone_bcd, serial, body):
        """Process a decoded JT808 message."""
        jt_conn = self.connections.get(fd)
        if jt_conn is None:
            return

        if msg_id == 0x0100:
            # Terminal Registration
            jt_conn.phone_bcd = phone_bcd
            imei = phone_bcd_to_imei(phone_bcd)
            jt_conn.imei = imei
            jt_conn.sailor_id = self._imei_to_sailor_id(imei)

            dev_cfg = self.config["devices"].get(imei, {})
            jt_conn.eid = dev_cfg.get("eid", self.config["default_eid"])

            auth_code = _make_auth_code(phone_bcd)
            self._log(f"[JT808] Registration: IMEI {imei} -> {jt_conn.sailor_id} (eid={jt_conn.eid})")

            frame = build_registration_response(
                phone_bcd, jt_conn.next_serial(), serial, 0, auth_code)
            self._send(jt_conn, frame)

            # Apply device name from config if configured
            dev_name = dev_cfg.get("name")
            if dev_name:
                tracker = self.get_tracker(jt_conn.eid)
                if tracker and hasattr(tracker, 'user_overrides') and hasattr(tracker, 'users_file'):
                    overrides = tracker.user_overrides
                    did_key = f"did:{imei}"
                    existing = overrides.get(did_key)
                    if not existing or existing.get("name") != dev_name or existing.get("_last_id") != jt_conn.sailor_id:
                        overrides[did_key] = {"name": dev_name, "_last_id": jt_conn.sailor_id}
                        if jt_conn.sailor_id in overrides:
                            del overrides[jt_conn.sailor_id]
                        if self._save_overrides:
                            self._save_overrides(tracker.users_file, overrides)
                        self._log(f"[JT808] Set display name for {jt_conn.sailor_id} (did:{imei}): {dev_name}")

        elif msg_id == 0x0102:
            # Terminal Authentication
            if jt_conn.phone_bcd is None:
                jt_conn.phone_bcd = phone_bcd
                jt_conn.imei = phone_bcd_to_imei(phone_bcd)
                jt_conn.sailor_id = self._imei_to_sailor_id(jt_conn.imei)
                dev_cfg = self.config["devices"].get(jt_conn.imei, {})
                jt_conn.eid = dev_cfg.get("eid", self.config["default_eid"])

            jt_conn.authenticated = True
            self._log(f"[JT808] Authenticated: {jt_conn.sailor_id}")
            self._send_general_response(jt_conn, serial, msg_id, result=0)

            # Set initial idle/active state and send tracking interval
            if jt_conn.sailor_id in self.active_sailors:
                jt_conn.idle = False
                self._send_tracking_control(jt_conn, self.interval)
            else:
                jt_conn.idle = True
                self.idle_sailors.add(jt_conn.sailor_id)
                self._send_tracking_control(jt_conn, 60)
            self._log(f"[JT808] Login commands sent ({'active' if not jt_conn.idle else 'idle'})")

            # Restore sticky SOS
            if jt_conn.imei in self._sticky_assist:
                jt_conn.assist_active = True
                if jt_conn.idle:
                    self.set_idle(jt_conn.sailor_id, False)
                self._log(f"[JT808] Restored sticky SOS after reconnect for {jt_conn.sailor_id}")

            # Restore last known position
            tracker = self.get_tracker(jt_conn.eid)
            if tracker:
                pt = tracker.position_tracker if hasattr(tracker, 'position_tracker') else tracker
                with pt._lock:
                    existing = pt.current_positions.get(jt_conn.sailor_id)
                if existing and existing.get("lat") and (time.time() - existing.get("last_seen", 0)) < 300:
                    jt_conn.last_lat = existing["lat"]
                    jt_conn.last_lon = existing["lon"]
                    tracker.process_position(
                        sailor_id=jt_conn.sailor_id,
                        lat=jt_conn.last_lat, lon=jt_conn.last_lon,
                        speed=0, heading=0, ts=existing.get("ts", int(time.time())),
                        assist=existing.get("ast", False),
                        battery=existing.get("bat", -1),
                        signal=existing.get("sig", -1),
                        role="sailor", version="jt808",
                        flags={}, src_ip=jt_conn.addr[0], source="JT808",
                        charging=existing.get("chg", False),
                        stopped=jt_conn.idle, idle=jt_conn.idle,
                        did=jt_conn.imei,
                        skip_log=True,
                    )

        elif msg_id == 0x0002:
            # Heartbeat
            if jt_conn.phone_bcd is None:
                jt_conn.phone_bcd = phone_bcd
            label = jt_conn.sailor_id or jt_conn.imei or "unknown"
            bat_str = f"{jt_conn.battery}%" if jt_conn.battery >= 0 else "?"
            if jt_conn.charging:
                bat_str += "+"
            sig_str = f"{jt_conn.signal}/4" if jt_conn.signal >= 0 else "?"
            self._log(f"[JT808] Heartbeat {label}: bat={bat_str} sig={sig_str}{' (idle)' if jt_conn.idle else ''}")
            self._send_general_response(jt_conn, serial, msg_id, result=0)

            # Update tracker on heartbeat when GPS is stale
            gps_stale = jt_conn.last_ts is None or (time.time() - jt_conn.last_ts) >= 15
            if jt_conn.sailor_id and gps_stale:
                tracker = self.get_tracker(jt_conn.eid)
                if tracker:
                    lat = jt_conn.last_lat if jt_conn.last_lat is not None else 0.0
                    lon = jt_conn.last_lon if jt_conn.last_lon is not None else 0.0
                    tracker.process_position(
                        sailor_id=jt_conn.sailor_id,
                        lat=lat, lon=lon,
                        speed=0, heading=0,
                        ts=int(time.time()),
                        assist=jt_conn.assist_active,
                        battery=jt_conn.battery,
                        signal=jt_conn.signal,
                        role="sailor", version="jt808",
                        flags={}, src_ip=jt_conn.addr[0], source="JT808",
                        nsats=0,
                        charging=jt_conn.charging,
                        stopped=jt_conn.idle, idle=jt_conn.idle,
                        did=jt_conn.imei,
                    )

        elif msg_id == 0x0200:
            # Location Report
            if jt_conn.phone_bcd is None:
                jt_conn.phone_bcd = phone_bcd
            self._send_general_response(jt_conn, serial, msg_id, result=0)

            loc = parse_location(body)
            if not loc:
                self._log(f"[JT808] Location too short from {jt_conn.sailor_id}")
                return
            if not jt_conn.sailor_id:
                self._log(f"[JT808] Location before login from {jt_conn.addr}")
                return

            if not loc["gps_valid"]:
                return

            # Update battery/signal from TLVs if present
            if "battery" in loc:
                jt_conn.battery = loc["battery"]
            if "signal" in loc:
                jt_conn.signal = loc["signal"]
            if "charging" in loc:
                jt_conn.charging = loc["charging"]

            jt_conn.last_lat = loc["lat"]
            jt_conn.last_lon = loc["lon"]
            jt_conn.last_ts = loc["ts"]

            # SOS handling
            if loc["is_sos"]:
                imei = jt_conn.imei
                if imei and imei not in self._sticky_assist:
                    jt_conn.assist_active = True
                    self._sticky_assist.add(imei)
                    self._log(f"[JT808] SOS activated (sticky) from {jt_conn.sailor_id}")
                    if jt_conn.idle:
                        self.set_idle(jt_conn.sailor_id, False)
                        self._log(f"[JT808] Exited idle due to SOS from {jt_conn.sailor_id}")

            tracker = self.get_tracker(jt_conn.eid)
            if tracker is None:
                return

            tracker.process_position(
                sailor_id=jt_conn.sailor_id,
                lat=loc["lat"],
                lon=loc["lon"],
                speed=loc["speed_knots"],
                heading=loc["heading"],
                ts=loc["ts"],
                assist=jt_conn.assist_active,
                battery=jt_conn.battery,
                signal=jt_conn.signal,
                role="sailor",
                version="jt808",
                flags={},
                src_ip=jt_conn.addr[0],
                source="JT808",
                nsats=loc.get("satellites", 0),
                charging=jt_conn.charging,
                stopped=jt_conn.idle,
                idle=jt_conn.idle,
                did=jt_conn.imei,
            )

        elif msg_id == 0x0001:
            # Terminal general response (ACK from device to our commands)
            if len(body) >= 5:
                resp_serial = struct.unpack(">H", body[0:2])[0]
                resp_id = struct.unpack(">H", body[2:4])[0]
                result = body[4]
                label = jt_conn.sailor_id or jt_conn.imei or "unknown"
                self._log(f"[JT808] Device ACK from {label}: msg=0x{resp_id:04X} result={result}")

        elif msg_id == 0x0003:
            # Terminal logout
            if jt_conn.phone_bcd is None:
                jt_conn.phone_bcd = phone_bcd
            self._send_general_response(jt_conn, serial, msg_id, result=0)
            label = jt_conn.sailor_id or jt_conn.imei or "unknown"
            self._log(f"[JT808] Logout from {label}")

        else:
            # Unknown message — send generic ACK
            if jt_conn.phone_bcd is None:
                jt_conn.phone_bcd = phone_bcd
            self._send_general_response(jt_conn, serial, msg_id, result=0)
            label = jt_conn.sailor_id or jt_conn.imei or "unknown"
            self._log(f"[JT808] Unknown msg 0x{msg_id:04X} from {label} ({len(body)} bytes)")

    # --- Public interface (matches GT06Listener) ---

    def send_command_to(self, sailor_id, cmd_str):
        """Send a command to a connected JT808 device by sailor_id.

        For JT808, raw command strings aren't meaningful like GT06.
        This is a placeholder for protocol-specific commands.
        """
        return False

    def cancel_assist(self, sailor_id):
        """Cancel SOS assist for a JT808 device."""
        for jt_conn in self.connections.values():
            if jt_conn.sailor_id == sailor_id and jt_conn.assist_active:
                jt_conn.assist_active = False
                if jt_conn.imei:
                    self._sticky_assist.discard(jt_conn.imei)
                # Send alarm confirmation response
                self._send_general_response(jt_conn, 0, 0x0200, result=4)
                self._log(f"[JT808] Cancelled assist for {sailor_id} (sticky cleared)")
                return True
        return False

    def set_idle(self, sailor_id, idle):
        """Set idle state for a JT808 device."""
        if idle:
            self.idle_sailors.add(sailor_id)
            self.active_sailors.discard(sailor_id)
        else:
            self.idle_sailors.discard(sailor_id)
            self.active_sailors.add(sailor_id)

        for jt_conn in self.connections.values():
            if jt_conn.sailor_id == sailor_id:
                jt_conn.idle = idle
                if idle:
                    self._send_tracking_control(jt_conn, 60)
                else:
                    self._send_tracking_control(jt_conn, self.interval)
                # Immediately update tracker
                tracker = self.get_tracker(jt_conn.eid)
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
                self._log(f"[JT808] {'Idle' if idle else 'Active'} mode for {sailor_id}")
                return True
        self._log(f"[JT808] {'Idle' if idle else 'Active'} mode queued for {sailor_id} (not connected)")
        return False

    # --- TCP / selector plumbing ---

    def _on_readable(self, fd):
        jt_conn = self.connections.get(fd)
        if jt_conn is None:
            return

        try:
            chunk = jt_conn.sock.recv(4096)
        except Exception:
            self._disconnect(fd)
            return

        if not chunk:
            self._disconnect(fd)
            return

        jt_conn.buf += chunk

        # Extract complete frames delimited by 0x7e
        while True:
            # Find start delimiter
            start = jt_conn.buf.find(b"\x7e")
            if start < 0:
                jt_conn.buf = b""
                break
            if start > 0:
                jt_conn.buf = jt_conn.buf[start:]

            # Find end delimiter (second 0x7e)
            end = jt_conn.buf.find(b"\x7e", 1)
            if end < 0:
                break  # incomplete frame

            # Handle consecutive delimiters (empty frame or delimiter between frames)
            if end == 1:
                # Two consecutive 0x7e — skip the first one
                jt_conn.buf = jt_conn.buf[1:]
                continue

            frame_raw = jt_conn.buf[1:end]  # between delimiters
            jt_conn.buf = jt_conn.buf[end:]  # keep from end delimiter

            self._log_packet(jt_conn.buf[:end + 1])  # log the raw frame including delimiters

            # Unescape
            data = jt808_unescape(frame_raw)
            if len(data) < 13:  # 12-byte header + 1-byte checksum minimum
                continue

            # Verify checksum
            cs_received = data[-1]
            cs_calc = jt808_checksum(data[:-1])
            if cs_received != cs_calc:
                label = jt_conn.sailor_id or jt_conn.imei or "unknown"
                self._log(f"[JT808] Checksum mismatch from {label}: "
                          f"received 0x{cs_received:02X}, calculated 0x{cs_calc:02X}")
                continue

            # Parse header
            parsed = jt808_parse_header(data)
            if parsed is None:
                continue
            msg_id, attributes, phone_bcd, serial, body_offset = parsed
            body_len = attributes & 0x03FF
            body = data[body_offset:body_offset + body_len]

            try:
                self._process_message(fd, msg_id, phone_bcd, serial, body)
            except Exception as e:
                label = jt_conn.sailor_id or jt_conn.imei or "unknown"
                self._log(f"[JT808] Message error from {label}: {e}")

    def run(self):
        """Main loop — runs in a daemon thread."""
        if self.log_file:
            try:
                self._log_fd = open(self.log_file, "ab")
                self._log(f"[JT808] Packet logging to {self.log_file}")
            except Exception as e:
                self._log(f"[JT808] Warning: Could not open packet log {self.log_file}: {e}")

        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.setblocking(False)
        server_sock.bind(("0.0.0.0", self.port))
        server_sock.listen(16)
        self.sel.register(server_sock, selectors.EVENT_READ, data="server")
        self._log(f"[JT808] Listening on TCP port {self.port} (interval={self.interval}s, prefix={self.id_prefix})")

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
                        self._log(f"[JT808] Accept error: {e}")
                else:
                    self._on_readable(key.data)
