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


def build_set_parameters(phone_bcd: bytes, serial: int,
                         params: dict) -> bytes:
    """Build terminal parameter setting message (0x8103).

    Args:
        params: dict of {param_id: (data_type, value)} where data_type is
                'DWORD', 'WORD', 'BYTE', or 'STRING'.
    """
    body = struct.pack(">B", len(params))
    for param_id, (dtype, value) in params.items():
        if dtype == "DWORD":
            val_bytes = struct.pack(">I", value)
        elif dtype == "WORD":
            val_bytes = struct.pack(">H", value)
        elif dtype == "BYTE":
            val_bytes = struct.pack(">B", value)
        elif dtype == "STRING":
            val_bytes = value.encode("ascii") if isinstance(value, str) else value
        else:
            raise ValueError(f"Unknown dtype {dtype}")
        body += struct.pack(">IB", param_id, len(val_bytes)) + val_bytes
    return jt808_build_frame(0x8103, phone_bcd, serial, body)


def build_query_parameters(phone_bcd: bytes, serial: int) -> bytes:
    """Build query all terminal parameters (0x8104). Empty body."""
    return jt808_build_frame(0x8104, phone_bcd, serial, b"")


def build_query_specific_parameters(phone_bcd: bytes, serial: int,
                                     param_ids: list) -> bytes:
    """Build query specific terminal parameters (0x8106)."""
    body = struct.pack(">B", len(param_ids))
    for pid in param_ids:
        body += struct.pack(">I", pid)
    return jt808_build_frame(0x8106, phone_bcd, serial, body)


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
    tlv_ids = []
    i = 0
    while i + 1 < len(extra):
        tlv_id = extra[i]
        tlv_len = extra[i + 1]
        tlv_ids.append(f"0x{tlv_id:02X}({tlv_len})")
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
        elif tlv_id == 0xE4:
            # Unexpected 0xE4 length — log for debugging
            result["_e4_raw"] = tlv_data[:tlv_len].hex()
        elif tlv_id == 0xEE and tlv_len >= 10:
            # 4G LBS: MCC(2) + MNC(1) + LAC(2) + CellID(4) + signal(1)
            mcc = struct.unpack(">H", tlv_data[0:2])[0]
            mnc = tlv_data[2]
            lac = struct.unpack(">H", tlv_data[3:5])[0]
            cell_id = struct.unpack(">I", tlv_data[5:9])[0]
            cell_sig = tlv_data[9]
            result["mcc"] = mcc
            result["mnc"] = mnc
            result["lac"] = lac
            result["cell_id"] = cell_id
            result["cell_signal"] = cell_sig
        elif tlv_id == 0xEB and tlv_len >= 7:
            # 2G/3G LBS: MCC(2) + MNC(1) + [CellID(2) + LAC(2) + signal(1)] x N
            mcc = struct.unpack(">H", tlv_data[0:2])[0]
            mnc = tlv_data[2]
            if tlv_len >= 8:
                cell_id = struct.unpack(">H", tlv_data[3:5])[0]
                lac = struct.unpack(">H", tlv_data[5:7])[0]
                result["mcc"] = mcc
                result["mnc"] = mnc
                result["lac"] = lac
                result["cell_id"] = cell_id

        i += 2 + tlv_len

    result["_tlv_ids"] = " ".join(tlv_ids)
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
                 write_positions_func=None,
                 get_event_state_func=None,
                 get_event_idle_submode_func=None):
        self.port = port
        self.interval = interval
        self.id_prefix = id_prefix
        self.get_tracker = get_tracker_func
        # Same precedence chain as GT06: in-session sets > event_state >
        # default idle. JT808 doesn't yet use overnight submode (no equiv
        # of MODE5), but we accept the callback for parity in case future
        # JT808 devices grow a deep-sleep mode.
        self.get_event_state = get_event_state_func
        self.get_event_idle_submode = get_event_idle_submode_func
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

    def rotate_log_to(self, archive_path):
        """Move the current packet log to archive_path and open a fresh log."""
        if not self.log_file:
            return
        path = Path(self.log_file) if not isinstance(self.log_file, Path) else self.log_file
        old_fd = self._log_fd
        try:
            if path.exists():
                path.rename(archive_path)
            try:
                self._log_fd = open(path, "ab")
                self._log(f"[JT808] Rotated packet log to {archive_path}, new log at {path}")
            except Exception as e:
                self._log(f"[JT808] Warning: Could not reopen log {path} after rotation: {e}")
                self._log_fd = None
        finally:
            if old_fd is not None:
                try:
                    old_fd.flush()
                    old_fd.close()
                except Exception:
                    pass

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

            # Close any stale connections for the same device
            my_fd = jt_conn.sock.fileno()
            stale_fds = [fd for fd, c in self.connections.items()
                         if c.sailor_id == jt_conn.sailor_id and fd != my_fd]
            for fd in stale_fds:
                self._log(f"[JT808] Closing stale connection for {jt_conn.sailor_id}")
                self._disconnect(fd)

            # Decide idle vs active using the same precedence chain as
            # GT06: in-session sets > event_state > default idle. Without
            # this, a JT808 device reconnecting to a "tracking" event after
            # a server restart would incorrectly fall back to idle.
            jt_key = (jt_conn.eid, jt_conn.sailor_id)
            event_state = None
            if self.get_event_state:
                try:
                    event_state = self.get_event_state(jt_conn.eid)
                except Exception:
                    event_state = None
            if jt_key in self.active_sailors:
                use_active = True
            elif jt_key in self.idle_sailors:
                use_active = False
            elif event_state == "tracking":
                use_active = True
            elif event_state == "idle":
                use_active = False
            else:
                use_active = False  # default: idle

            if use_active:
                jt_conn.idle = False
                report_interval = self.interval
                self._send_tracking_control(jt_conn, report_interval)
                # Send vendor params to match — 0xF104 controls actual rate on P7 devices
                for pid, pval in [(0xF104, report_interval), (0xF110, report_interval), (0x0029, report_interval)]:
                    frame = build_set_parameters(
                        jt_conn.phone_bcd, jt_conn.next_serial(),
                        {pid: ("DWORD", pval)})
                    self._send(jt_conn, frame)
            else:
                jt_conn.idle = True
                self.idle_sailors.add(jt_key)
                # Stop GPS tracking to save power — heartbeat keeps device visible
                self._send_tracking_control(jt_conn, 0)
                for pid in [0xF104, 0xF110, 0x0029]:
                    frame = build_set_parameters(
                        jt_conn.phone_bcd, jt_conn.next_serial(),
                        {pid: ("DWORD", 0)})
                    self._send(jt_conn, frame)
            self._log(f"[JT808] Login commands sent ({'active' if not jt_conn.idle else 'idle'}, interval={report_interval if not jt_conn.idle else 0}s)")

            # Set heartbeat interval to 15s so device stays visible even when idle/stationary
            frame = build_set_parameters(
                jt_conn.phone_bcd, jt_conn.next_serial(),
                {0x0001: ("DWORD", 15)})
            self._send(jt_conn, frame)

            # Query all device parameters to understand its configuration
            frame = build_query_parameters(jt_conn.phone_bcd, jt_conn.next_serial())
            self._send(jt_conn, frame)

            # Query terminal attributes (model, ICCID, GNSS/comm capabilities)
            frame = jt808_build_frame(0x8107, jt_conn.phone_bcd, jt_conn.next_serial(), b"")
            self._send(jt_conn, frame)

            # Restore sticky SOS
            if jt_conn.imei in self._sticky_assist:
                jt_conn.assist_active = True
                if jt_conn.idle:
                    self.set_idle(jt_conn.eid, jt_conn.sailor_id, False)
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
            self._process_location(jt_conn, body)

        elif msg_id == 0x0704:
            # Batch location upload
            if jt_conn.phone_bcd is None:
                jt_conn.phone_bcd = phone_bcd
            self._send_general_response(jt_conn, serial, msg_id, result=0)
            self._process_batch_location(jt_conn, body)

        elif msg_id == 0x0001:
            # Terminal general response (ACK from device to our commands)
            if len(body) >= 5:
                resp_serial = struct.unpack(">H", body[0:2])[0]
                resp_id = struct.unpack(">H", body[2:4])[0]
                result = body[4]
                result_str = {0: "OK", 1: "Fail", 2: "Bad msg", 3: "Unsupported"}.get(result, f"?({result})")
                label = jt_conn.sailor_id or jt_conn.imei or "unknown"
                self._log(f"[JT808] Device ACK from {label}: msg=0x{resp_id:04X} serial={resp_serial} result={result_str}")

        elif msg_id == 0x0003:
            # Terminal logout
            if jt_conn.phone_bcd is None:
                jt_conn.phone_bcd = phone_bcd
            self._send_general_response(jt_conn, serial, msg_id, result=0)
            label = jt_conn.sailor_id or jt_conn.imei or "unknown"
            self._log(f"[JT808] Logout from {label}")

        elif msg_id == 0x0107:
            # Terminal attribute response — contains manufacturer, model, ID, ICCID etc.
            if jt_conn.phone_bcd is None:
                jt_conn.phone_bcd = phone_bcd
            label = jt_conn.sailor_id or jt_conn.imei or "unknown"
            self._log(f"[JT808] Terminal attributes from {label} ({len(body)} bytes)")
            try:
                # Parse 0x0107: type(2) + mfr(5) + model(20) + tid(7)
                # + iccid(10 BCD) + hw_ver_len(1) + hw + fw_ver_len(1) + fw + gnss(1) + comm(1)
                off = 2  # skip terminal type WORD
                mfr = body[off:off+5].decode("ascii", errors="replace").strip('\x00')
                off += 5
                model = body[off:off+20].decode("ascii", errors="replace").strip('\x00')
                off += 20
                tid = body[off:off+7].decode("ascii", errors="replace").strip('\x00')
                off += 7
                iccid_bcd = body[off:off+10]
                iccid = iccid_bcd.hex()
                off += 10
                hw_len = body[off]; off += 1
                off += hw_len
                fw_len = body[off]; off += 1
                fw = body[off:off+fw_len].decode("ascii", errors="replace").strip('\x00')
                off += fw_len
                gnss_attr = body[off] if off < len(body) else 0
                comm_attr = body[off+1] if off+1 < len(body) else 0
                gnss_names = []
                if gnss_attr & 0x01: gnss_names.append("GPS")
                if gnss_attr & 0x02: gnss_names.append("BeiDou")
                if gnss_attr & 0x04: gnss_names.append("GLONASS")
                if gnss_attr & 0x08: gnss_names.append("Galileo")
                self._log(f"[JT808] Attrs {label}: model={model} fw={fw} ICCID={iccid} "
                          f"GNSS={'+'.join(gnss_names) or '?'}(0x{gnss_attr:02X}) comm=0x{comm_attr:02X}")
            except Exception as e:
                self._log(f"[JT808] Attrs parse error: {e}")

        elif msg_id == 0x0104:
            # Query parameter response
            label = jt_conn.sailor_id or jt_conn.imei or "unknown"
            if len(body) >= 3:
                resp_serial = struct.unpack(">H", body[0:2])[0]
                param_count = body[2]
                self._log(f"[JT808] Parameter response from {label}: {param_count} params")
                offset = 3
                for _ in range(param_count):
                    if offset + 5 > len(body):
                        break
                    param_id = struct.unpack(">I", body[offset:offset + 4])[0]
                    param_len = body[offset + 4]
                    offset += 5
                    if offset + param_len > len(body):
                        break
                    param_val = body[offset:offset + param_len]
                    offset += param_len
                    if param_len == 4:
                        val = struct.unpack(">I", param_val)[0]
                        self._log(f"[JT808]   0x{param_id:04X} = {val}")
                    elif param_len == 2:
                        val = struct.unpack(">H", param_val)[0]
                        self._log(f"[JT808]   0x{param_id:04X} = {val}")
                    elif param_len == 1:
                        self._log(f"[JT808]   0x{param_id:04X} = {param_val[0]}")
                    else:
                        self._log(f"[JT808]   0x{param_id:04X} = {param_val.hex()}")

        elif msg_id == 0x0900:
            # Data uplink pass-through
            if jt_conn.phone_bcd is None:
                jt_conn.phone_bcd = phone_bcd
            self._send_general_response(jt_conn, serial, msg_id, result=0)
            if len(body) < 2:
                return
            passthrough_type = body[0]
            passthrough_data = body[1:]
            label = jt_conn.sailor_id or jt_conn.imei or "unknown"
            if passthrough_type == 0x00:
                # GNSS module detailed location data
                self._log(f"[JT808] GNSS pass-through from {label} ({len(passthrough_data)} bytes)")
                self._process_gnss_passthrough(jt_conn, passthrough_data)
            else:
                self._log(f"[JT808] Pass-through type=0x{passthrough_type:02X} from {label} ({len(passthrough_data)} bytes): {passthrough_data.hex()}")

        elif msg_id in (0x0109, 0x0112, 0x1007, 0x1107):
            # Vendor-specific messages — ACK and log
            if jt_conn.phone_bcd is None:
                jt_conn.phone_bcd = phone_bcd
            self._send_general_response(jt_conn, serial, msg_id, result=0)
            label = jt_conn.sailor_id or jt_conn.imei or "unknown"
            if msg_id == 0x1107 and len(body) > 20:
                # Vendor 0x1107 often contains ICCID and model string
                try:
                    printable = body.decode("ascii", errors="replace")
                    self._log(f"[JT808] Vendor 0x1107 from {label}: {body.hex()} text={printable}")
                except Exception:
                    self._log(f"[JT808] Vendor 0x1107 from {label}: {body.hex()}")
            else:
                self._log(f"[JT808] Vendor msg 0x{msg_id:04X} from {label} ({len(body)} bytes)")

        else:
            # Unknown message — send generic ACK
            if jt_conn.phone_bcd is None:
                jt_conn.phone_bcd = phone_bcd
            self._send_general_response(jt_conn, serial, msg_id, result=0)
            label = jt_conn.sailor_id or jt_conn.imei or "unknown"
            self._log(f"[JT808] Unknown msg 0x{msg_id:04X} from {label} ({len(body)} bytes)")

    # --- Location processing ---

    def _process_location(self, jt_conn, loc_body):
        """Process a single location report body (shared by 0x0200 and 0x0704)."""
        loc = parse_location(loc_body)
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
            if jt_conn.battery >= 0 and abs(loc["battery"] - jt_conn.battery) > 20:
                self._log(f"[JT808] Battery jump {jt_conn.sailor_id}: {jt_conn.battery}%→{loc['battery']}% "
                          f"e4_raw={loc.get('_e4_raw', 'n/a')} tlvs={loc.get('_tlv_ids', '')}")
            jt_conn.battery = loc["battery"]
        if "signal" in loc:
            jt_conn.signal = loc["signal"]
        if "charging" in loc:
            jt_conn.charging = loc["charging"]

        # Log cell info when first seen or changed
        if "mcc" in loc:
            cell_key = (loc["mcc"], loc["mnc"], loc.get("lac"), loc.get("cell_id"))
            if not hasattr(jt_conn, '_last_cell') or jt_conn._last_cell != cell_key:
                jt_conn._last_cell = cell_key
                self._log(f"[JT808] Cell info {jt_conn.sailor_id}: MCC={loc['mcc']} MNC={loc['mnc']} "
                          f"LAC={loc.get('lac')} CellID={loc.get('cell_id')} sig={loc.get('cell_signal')}")

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
                    self.set_idle(jt_conn.eid, jt_conn.sailor_id, False)
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

    def _process_batch_location(self, jt_conn, body):
        """Process batch location upload (0x0704).

        Body: count(WORD) + type(BYTE) + items...
        Each item: length(WORD) + location_body(length bytes)
        """
        if len(body) < 3:
            return
        count = struct.unpack(">H", body[0:2])[0]
        loc_type = body[2]  # 0=normal, 1=blind area
        label = jt_conn.sailor_id or jt_conn.imei or "unknown"
        self._log(f"[JT808] Batch location from {label}: {count} items (type={loc_type})")

        offset = 3
        processed = 0
        for _ in range(count):
            if offset + 2 > len(body):
                break
            item_len = struct.unpack(">H", body[offset:offset + 2])[0]
            offset += 2
            if offset + item_len > len(body):
                break
            loc_body = body[offset:offset + item_len]
            offset += item_len
            self._process_location(jt_conn, loc_body)
            processed += 1

        if processed != count:
            self._log(f"[JT808] Batch: parsed {processed}/{count} items from {label}")

    def _process_gnss_passthrough(self, jt_conn, data):
        """Process GNSS module detailed location data from 0x0900 pass-through.

        The payload format is not fully documented — try parsing as 0x0200-style
        location body first. If that fails, log the raw hex for analysis.
        """
        label = jt_conn.sailor_id or jt_conn.imei or "unknown"
        # Try parsing as standard location body (same as 0x0200)
        loc = parse_location(data)
        if loc and loc["gps_valid"] and loc["lat"] != 0 and loc["lon"] != 0:
            self._log(f"[JT808] GNSS data from {label}: lat={loc['lat']:.6f} lon={loc['lon']:.6f} spd={loc['speed_knots']:.1f}kn")
            self._process_location(jt_conn, data)
            return
        # Unknown format — log hex for analysis
        self._log(f"[JT808] GNSS data unknown format from {label}: {data.hex()}")

    # --- Public interface (matches GT06Listener) ---

    def send_command_to(self, eid, sailor_id, cmd_str):
        """Send a command to a connected JT808 device matching (eid, sailor_id).

        Supported commands:
          query-params                       - query all terminal parameters (0x8104)
          query-param 0x0029                 - query specific parameter(s) (0x8106)
          set-param 0x0029=10                - set parameter (0x8103), DWORD assumed
          set-param 0x0090=1:BYTE            - set parameter with type suffix
          set-interval 1                     - set tracking interval via 0x8202
          passthrough 0x00 <hexdata>         - send 0x8900 downlink pass-through (raw hex)
          passthrough-text 0x00 AT+CMD       - send 0x8900 downlink pass-through (ASCII text)
        """
        for jt_conn in self.connections.values():
            if jt_conn.eid == eid and jt_conn.sailor_id == sailor_id:
                return self._exec_command(jt_conn, cmd_str)
        return False

    def _exec_command(self, jt_conn, cmd_str):
        """Execute a command string on a JT808 connection."""
        parts = cmd_str.strip().split()
        if not parts:
            return False
        cmd = parts[0].lower()

        if cmd == "query-params":
            frame = build_query_parameters(jt_conn.phone_bcd, jt_conn.next_serial())
            self._send(jt_conn, frame)
            self._log(f"[JT808] Sent query-all-params to {jt_conn.sailor_id}")
            return True

        elif cmd == "query-attrs":
            frame = jt808_build_frame(0x8107, jt_conn.phone_bcd, jt_conn.next_serial(), b"")
            self._send(jt_conn, frame)
            self._log(f"[JT808] Sent query-attrs to {jt_conn.sailor_id}")
            return True

        elif cmd == "query-param" and len(parts) >= 2:
            param_ids = [int(p, 0) for p in parts[1:]]
            frame = build_query_specific_parameters(
                jt_conn.phone_bcd, jt_conn.next_serial(), param_ids)
            self._send(jt_conn, frame)
            self._log(f"[JT808] Sent query params {[f'0x{p:04X}' for p in param_ids]} to {jt_conn.sailor_id}")
            return True

        elif cmd == "set-param" and len(parts) >= 2:
            params = {}
            for p in parts[1:]:
                if "=" not in p:
                    continue
                key, val = p.split("=", 1)
                param_id = int(key, 0)
                # Support type suffix: 0x0094=1:BYTE, 0x0095=10:DWORD, 0x0010=hologram:STRING
                dtype = "DWORD"
                if ":" in val:
                    val, dtype = val.rsplit(":", 1)
                    dtype = dtype.upper()
                if dtype == "STRING":
                    value = val
                else:
                    value = int(val, 0)
                params[param_id] = (dtype, value)
            if params:
                frame = build_set_parameters(
                    jt_conn.phone_bcd, jt_conn.next_serial(), params)
                self._send(jt_conn, frame)
                self._log(f"[JT808] Sent set-params to {jt_conn.sailor_id}: "
                          f"{', '.join(f'0x{k:04X}={v[1]}' for k, v in params.items())}")
                return True

        elif cmd == "set-interval" and len(parts) >= 2:
            interval = int(parts[1])
            self._send_tracking_control(jt_conn, interval)
            self._log(f"[JT808] Sent set-interval {interval}s to {jt_conn.sailor_id}")
            return True

        elif cmd == "passthrough" and len(parts) >= 2:
            # Send 0x8900 data downlink pass-through
            # Usage: passthrough <type_byte> <hex_data>
            # Example: passthrough 0x00 <hex_bytes>
            # Or: passthrough-text <type_byte> <ascii_text>
            pt_type = int(parts[1], 0)
            if len(parts) >= 3:
                pt_data = bytes.fromhex(parts[2])
            else:
                pt_data = b""
            body = bytes([pt_type]) + pt_data
            frame = jt808_build_frame(0x8900, jt_conn.phone_bcd, jt_conn.next_serial(), body)
            self._send(jt_conn, frame)
            self._log(f"[JT808] Sent passthrough type=0x{pt_type:02X} ({len(pt_data)} bytes) to {jt_conn.sailor_id}")
            return True

        elif cmd == "passthrough-text" and len(parts) >= 3:
            # Send 0x8900 with ASCII text payload (e.g. AT commands)
            pt_type = int(parts[1], 0)
            pt_text = " ".join(parts[2:])
            pt_data = pt_text.encode("ascii")
            body = bytes([pt_type]) + pt_data
            frame = jt808_build_frame(0x8900, jt_conn.phone_bcd, jt_conn.next_serial(), body)
            self._send(jt_conn, frame)
            self._log(f"[JT808] Sent passthrough-text type=0x{pt_type:02X} '{pt_text}' to {jt_conn.sailor_id}")
            return True

        elif cmd == "text" and len(parts) >= 2:
            # Send 0x8300 text information
            # Usage: text <message>
            # Flags: bit0=emergency, bit2=display, bit3=TTS
            flags = 0x0D  # emergency + display + TTS
            text = " ".join(parts[1:])
            text_bytes = text.encode("gbk", errors="replace")
            body = bytes([flags]) + text_bytes
            frame = jt808_build_frame(0x8300, jt_conn.phone_bcd, jt_conn.next_serial(), body)
            self._send(jt_conn, frame)
            self._log(f"[JT808] Sent text (flags=0x{flags:02X}) '{text}' to {jt_conn.sailor_id}")
            return True

        self._log(f"[JT808] Unknown command: {cmd_str}")
        return False

    def cancel_assist(self, eid, sailor_id):
        """Cancel SOS assist for a JT808 device matching (eid, sailor_id)."""
        for jt_conn in self.connections.values():
            if (jt_conn.eid == eid and jt_conn.sailor_id == sailor_id
                    and jt_conn.assist_active):
                jt_conn.assist_active = False
                if jt_conn.imei:
                    self._sticky_assist.discard(jt_conn.imei)
                # Send alarm confirmation response
                self._send_general_response(jt_conn, 0, 0x0200, result=4)
                self._log(f"[JT808] Cancelled assist for {sailor_id} (sticky cleared)")
                return True
        return False

    def set_idle(self, eid, sailor_id, idle):
        """Set idle state for the JT808 device matching (eid, sailor_id)."""
        key = (eid, sailor_id)
        if idle:
            self.idle_sailors.add(key)
            self.active_sailors.discard(key)
        else:
            self.idle_sailors.discard(key)
            self.active_sailors.add(key)

        found = False
        for jt_conn in self.connections.values():
            if jt_conn.eid == eid and jt_conn.sailor_id == sailor_id:
                jt_conn.idle = idle
                if idle:
                    # Stop GPS tracking to save power — heartbeat keeps device visible
                    self._send_tracking_control(jt_conn, 0)
                    for pid in [0xF104, 0xF110, 0x0029]:
                        frame = build_set_parameters(
                            jt_conn.phone_bcd, jt_conn.next_serial(),
                            {pid: ("DWORD", 0)})
                        self._send(jt_conn, frame)
                else:
                    self._send_tracking_control(jt_conn, self.interval)
                    for pid, pval in [(0xF104, self.interval), (0xF110, self.interval), (0x0029, self.interval)]:
                        frame = build_set_parameters(
                            jt_conn.phone_bcd, jt_conn.next_serial(),
                            {pid: ("DWORD", pval)})
                        self._send(jt_conn, frame)
                # Immediately update tracker
                tracker = self.get_tracker(jt_conn.eid)
                if tracker:
                    pt = tracker.position_tracker if hasattr(tracker, 'position_tracker') else tracker
                    with pt._lock:
                        existing = pt.current_positions.get(sailor_id)
                        if existing:
                            existing["stopped"] = idle
                            existing["idle"] = idle
                            # No JT808 overnight equivalent yet — always
                            # clear sleep so UI doesn't show stale SLEEP
                            # state for JT808 trackers.
                            existing["sleep"] = False
                            now = time.time()
                            existing["last_seen"] = now
                            existing["last_seen_iso"] = datetime.fromtimestamp(now).isoformat()
                    if pt.positions_file and self._write_positions:
                        overrides = tracker.user_overrides if hasattr(tracker, 'user_overrides') else {}
                        self._write_positions(pt.current_positions, pt.positions_file, overrides, pt.position_tails)
                self._log(f"[JT808] {'Idle' if idle else 'Active'} mode for {sailor_id}")
                found = True
        if not found:
            if key in self.idle_sailors or key in self.active_sailors:
                self._log(f"[JT808] {'Idle' if idle else 'Active'} mode queued for {sailor_id} (not connected)")
        return found

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

            self._log_packet(b"\x7e" + frame_raw + b"\x7e")  # log the raw frame including delimiters

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
