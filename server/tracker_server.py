#!/usr/bin/env python3
"""
Windsurfer Tracker - Multi-Event UDP Server with HTTP Admin API
Receives position reports from sailor apps, sends ACKs, logs data.
Provides HTTP endpoints for admin functions, course management, and event management.
Supports multiple concurrent events, each with its own data directory and passwords.
"""

import fcntl
import math
import selectors
import socket
import json
import struct
import time
import argparse
import os
import re
import sys
import threading
import traceback
import email.utils
from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Force line-buffered output for real-time logging with tail -f
sys.stdout.reconfigure(line_buffering=True)


def format_timestamp(ts: int) -> str:
    """Convert unix timestamp to readable format."""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def format_position(lat: float, lon: float) -> str:
    """Format lat/lon for display."""
    lat_dir = "S" if lat < 0 else "N"
    lon_dir = "W" if lon < 0 else "E"
    return f"{abs(lat):.5f}°{lat_dir} {abs(lon):.5f}°{lon_dir}"


def log(msg: str) -> None:
    """Print a message with local timestamp prefix."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} {msg}")


def rotate_file(filepath: Path) -> Path | None:
    """Rotate a file to FILENAME.1, FILENAME.2, etc. Returns new path or None if file doesn't exist."""
    if not filepath.exists():
        return None

    # Find the next available number
    n = 1
    while True:
        new_path = filepath.parent / f"{filepath.name}.{n}"
        if not new_path.exists():
            break
        n += 1

    filepath.rename(new_path)
    log(f"Rotated {filepath} -> {new_path}")
    return new_path


def sanitize_tracker_packet(packet: dict) -> dict:
    """Sanitize tracker packet inputs to prevent HTML injection and ensure type safety.

    - String fields: Strip HTML tags, limit length
    - Numeric fields: Ensure they are numbers, use defaults if invalid
    - Boolean fields: Ensure they are booleans
    """
    # HTML tag pattern for stripping
    html_tag_pattern = re.compile(r'<[^>]+>')

    def sanitize_string(value, max_length: int = 64, default: str = "?") -> str:
        """Sanitize a string value: strip HTML, limit length."""
        if not isinstance(value, str):
            value = str(value) if value is not None else default
        # Strip HTML tags
        value = html_tag_pattern.sub('', value)
        # Strip dangerous characters
        value = value.replace('<', '').replace('>', '').replace('&', '').replace('"', '').replace("'", '')
        # Limit length
        return value[:max_length].strip() or default

    def sanitize_int(value, default: int = 0, min_val: int = None, max_val: int = None) -> int:
        """Sanitize an integer value."""
        try:
            result = int(value) if value is not None else default
            if min_val is not None:
                result = max(min_val, result)
            if max_val is not None:
                result = min(max_val, result)
            return result
        except (ValueError, TypeError):
            return default

    def sanitize_float(value, default: float = 0.0, min_val: float = None, max_val: float = None) -> float:
        """Sanitize a float value."""
        try:
            result = float(value) if value is not None else default
            if min_val is not None:
                result = max(min_val, result)
            if max_val is not None:
                result = min(max_val, result)
            return result
        except (ValueError, TypeError):
            return default

    def sanitize_bool(value, default: bool = False) -> bool:
        """Sanitize a boolean value."""
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ('true', '1', 'yes')
        try:
            return bool(value)
        except (ValueError, TypeError):
            return default

    # Sanitize the packet in place
    sanitized = {}

    # String fields
    sanitized['id'] = sanitize_string(packet.get('id'), max_length=32, default='???')
    sanitized['role'] = sanitize_string(packet.get('role'), max_length=16, default='sailor')
    sanitized['ver'] = sanitize_string(packet.get('ver'), max_length=64, default='?')
    if 'os' in packet:
        sanitized['os'] = sanitize_string(packet.get('os'), max_length=64, default='')
    if 'pwd' in packet:
        sanitized['pwd'] = sanitize_string(packet.get('pwd'), max_length=64, default='')

    # Integer fields
    sanitized['sq'] = sanitize_int(packet.get('sq'), default=0, min_val=0)
    sanitized['ts'] = sanitize_int(packet.get('ts'), default=0, min_val=0)
    sanitized['hdg'] = sanitize_int(packet.get('hdg'), default=0, min_val=0, max_val=360)
    sanitized['bat'] = sanitize_int(packet.get('bat'), default=-1, min_val=-1, max_val=100)
    sanitized['sig'] = sanitize_int(packet.get('sig'), default=-1, min_val=-1, max_val=4)
    sanitized['eid'] = sanitize_int(packet.get('eid'), default=1, min_val=1)
    if 'hr' in packet and packet.get('hr') is not None:
        sanitized['hr'] = sanitize_int(packet.get('hr'), default=0, min_val=0, max_val=300)

    # Float fields
    sanitized['lat'] = sanitize_float(packet.get('lat'), default=0.0, min_val=-90.0, max_val=90.0)
    sanitized['lon'] = sanitize_float(packet.get('lon'), default=0.0, min_val=-180.0, max_val=180.0)
    sanitized['spd'] = sanitize_float(packet.get('spd'), default=0.0, min_val=0.0, max_val=100.0)
    if 'bdr' in packet and packet.get('bdr') is not None:
        sanitized['bdr'] = sanitize_float(packet.get('bdr'), default=0.0, min_val=0.0, max_val=100.0)
    if 'hac' in packet and packet.get('hac') is not None:
        sanitized['hac'] = sanitize_float(packet.get('hac'), default=0.0, min_val=0.0, max_val=10000.0)
    if 'nsats' in packet and packet.get('nsats') is not None:
        sanitized['nsats'] = sanitize_int(packet.get('nsats'), default=0, min_val=0, max_val=200)

    # Device ID (stable identifier, optional)
    if 'did' in packet:
        sanitized['did'] = sanitize_string(packet.get('did'), max_length=64, default='')

    # Boolean fields
    sanitized['ast'] = sanitize_bool(packet.get('ast'), default=False)
    if 'chg' in packet:
        sanitized['chg'] = sanitize_bool(packet.get('chg'), default=False)
    if 'ps' in packet:
        sanitized['ps'] = sanitize_bool(packet.get('ps'), default=False)
    if 'stopped' in packet:
        sanitized['stopped'] = sanitize_bool(packet.get('stopped'), default=False)
    if 'idle' in packet:
        sanitized['idle'] = sanitize_bool(packet.get('idle'), default=False)

    # Pass through pos array (1Hz mode) with sanitized values
    # Format: [[ts, lat, lon], ...] or [[ts, lat, lon, spd], ...]
    if 'pos' in packet and isinstance(packet.get('pos'), list):
        sanitized_pos = []
        for pos in packet['pos'][:100]:  # Limit to 100 positions
            if isinstance(pos, list) and len(pos) >= 3:
                entry = [
                    sanitize_int(pos[0], default=0, min_val=0),  # timestamp
                    sanitize_float(pos[1], default=0.0, min_val=-90.0, max_val=90.0),  # lat
                    sanitize_float(pos[2], default=0.0, min_val=-180.0, max_val=180.0)  # lon
                ]
                # Include speed if present (4th element)
                if len(pos) >= 4:
                    entry.append(sanitize_float(pos[3], default=0.0, min_val=0.0, max_val=100.0))  # spd in knots
                sanitized_pos.append(entry)
        if sanitized_pos:
            sanitized['pos'] = sanitized_pos

    # Pass through flags dict if present
    if 'flg' in packet and isinstance(packet.get('flg'), dict):
        sanitized['flg'] = packet['flg']

    return sanitized


def get_course_timestamp(course_path: Path) -> float | None:
    """Get the 'updated' timestamp from inside a course file.

    Returns the internal 'updated' field if present, otherwise file mtime.
    """
    try:
        with open(course_path, 'r') as f:
            course = json.load(f)
            if 'updated' in course:
                return course['updated']
            # Fallback to file mtime
            return course_path.stat().st_mtime
    except Exception:
        return None


def find_applicable_course(event_dir: Path, log_end_ts: float) -> tuple[str, float] | None:
    """Find the course file that was active at log_end_ts.

    Scans course.json and rotated versions (course.json.1, course.json.2, etc.)
    and returns the one with the latest 'updated' timestamp that is <= log_end_ts.

    Returns (course_filename, updated_ts) or None if no applicable course.
    """
    course_files = []

    # Check main course.json
    base = event_dir / "course.json"
    if base.exists():
        ts = get_course_timestamp(base)
        if ts is not None:
            course_files.append((base.name, ts))

    # Check rotated versions (course.json.1, course.json.2, ...)
    # Use glob to find all rotated files without arbitrary limit
    for rotated in event_dir.glob("course.json.[0-9]*"):
        # Verify the suffix is purely numeric (avoid matching course.json.backup etc)
        suffix = rotated.name[12:]  # len("course.json.") == 12
        if suffix.isdigit():
            ts = get_course_timestamp(rotated)
            if ts is not None:
                course_files.append((rotated.name, ts))

    if not course_files:
        return None

    # Find latest course that was created before log ended
    applicable = [(f, t) for f, t in course_files if t <= log_end_ts]
    if applicable:
        return max(applicable, key=lambda x: x[1])

    # No course was active at the time of this log
    return None


def generate_log_summaries(log_dir: Path) -> int:
    """
    Generate summary JSON files for each day's logs.

    Scans the log directory for YYYY_MM_DD.jsonl files (and rotations),
    and generates YYYY_MM_DD_summary.json with metadata about each log segment.

    Uses file modification times to skip regeneration if logs haven't changed.

    Returns the number of summaries generated/updated.
    """
    import re
    from collections import defaultdict

    if not log_dir.exists():
        return 0

    # Find all log files grouped by date
    # Pattern: YYYY_MM_DD.jsonl or YYYY_MM_DD.jsonl.N
    date_pattern = re.compile(r'^(\d{4}_\d{2}_\d{2})\.jsonl(\.(\d+))?$')

    # Group files by date
    date_files: dict[str, list[Path]] = defaultdict(list)
    for f in log_dir.iterdir():
        match = date_pattern.match(f.name)
        if match:
            date_str = match.group(1)
            date_files[date_str].append(f)

    updated_count = 0

    for date_str, log_files in date_files.items():
        summary_file = log_dir / f"{date_str}_summary.json"

        # Check if regeneration is needed (any log file newer than summary)
        summary_mtime = summary_file.stat().st_mtime if summary_file.exists() else 0
        log_mtimes = [f.stat().st_mtime for f in log_files]
        newest_log_mtime = max(log_mtimes) if log_mtimes else 0

        if summary_mtime >= newest_log_mtime and summary_file.exists():
            # Summary is up to date
            continue

        # Generate summary for this date
        logs_data = []

        for log_file in sorted(log_files, key=lambda f: f.name):
            # Parse rotation index from filename
            match = date_pattern.match(log_file.name)
            rotation_idx = int(match.group(3)) if match.group(3) else 0

            # Scan the log file
            start_ts = None
            end_ts = None
            point_count = 0
            sailors: dict[str, dict] = {}  # key -> {points, first_ts, last_ts, id, displayid}
            # Key is displayid if present, otherwise id. This allows the same tracker
            # to appear as multiple entries if its display name changed during the log.

            try:
                with open(log_file, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            ts = entry.get('ts')
                            sailor_id = entry.get('id')
                            displayid = entry.get('displayid')

                            if ts is None or sailor_id is None:
                                continue

                            # Count no-GPS entries separately
                            if entry.get('nogps'):
                                key = displayid if displayid else sailor_id
                                if key not in sailors:
                                    sailors[key] = {
                                        'points': 0,
                                        'first_ts': ts,
                                        'last_ts': ts,
                                        'id': sailor_id
                                    }
                                    if displayid:
                                        sailors[key]['displayid'] = displayid
                                sailors[key]['nogps_points'] = sailors[key].get('nogps_points', 0) + 1
                                if ts < sailors[key]['first_ts']:
                                    sailors[key]['first_ts'] = ts
                                if ts > sailors[key]['last_ts']:
                                    sailors[key]['last_ts'] = ts
                                continue

                            point_count += 1

                            if start_ts is None or ts < start_ts:
                                start_ts = ts
                            if end_ts is None or ts > end_ts:
                                end_ts = ts

                            # Use displayid as the key if present, otherwise sailor_id
                            # This groups entries by their display name at the time of logging
                            key = displayid if displayid else sailor_id

                            if key not in sailors:
                                sailors[key] = {
                                    'points': 0,
                                    'first_ts': ts,
                                    'last_ts': ts,
                                    'id': sailor_id  # Store original tracker ID
                                }
                                # Store displayid if present (for search)
                                if displayid:
                                    sailors[key]['displayid'] = displayid

                            sailors[key]['points'] += 1
                            if ts < sailors[key]['first_ts']:
                                sailors[key]['first_ts'] = ts
                            if ts > sailors[key]['last_ts']:
                                sailors[key]['last_ts'] = ts

                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                log(f"[SUMMARY] Error reading {log_file}: {e}")
                continue

            if point_count > 0:
                log_entry = {
                    'file': log_file.name,
                    'index': rotation_idx,
                    'start_ts': start_ts,
                    'end_ts': end_ts,
                    'point_count': point_count,
                    'sailors': sailors
                }

                # Find applicable course for this log segment
                event_dir = log_dir.parent
                course_info = find_applicable_course(event_dir, end_ts)
                if course_info:
                    log_entry['course'] = course_info[0]
                    log_entry['course_mtime'] = course_info[1]

                logs_data.append(log_entry)

        if not logs_data:
            continue

        # Preserve sublogs from existing summary if present
        if summary_file.exists():
            try:
                with open(summary_file, 'r') as f:
                    old_summary = json.load(f)
                # Build map of file -> sublogs from old summary
                old_sublogs = {}
                for old_log in old_summary.get('logs', []):
                    if old_log.get('sublogs'):
                        old_sublogs[old_log.get('file')] = old_log['sublogs']
                # Merge sublogs into new log entries
                for log_entry in logs_data:
                    file_name = log_entry.get('file')
                    if file_name in old_sublogs:
                        log_entry['sublogs'] = old_sublogs[file_name]
            except Exception:
                pass  # If we can't read old summary, just continue without sublogs

        # Sort by start time (most recent first for display)
        logs_data.sort(key=lambda x: x.get('start_ts', 0), reverse=True)

        # Write summary file
        summary = {
            'date': date_str,
            'generated': time.time(),
            'generated_iso': datetime.now().isoformat(),
            'logs': logs_data
        }

        try:
            tmp_file = summary_file.with_suffix('.tmp')
            with open(tmp_file, 'w') as f:
                json.dump(summary, f, indent=2)
            tmp_file.rename(summary_file)
            updated_count += 1
            total_points = sum(log['point_count'] for log in logs_data)
            log(f"[SUMMARY] Generated {summary_file.name}: {len(logs_data)} logs, {total_points} points")
        except Exception as e:
            log(f"[SUMMARY] Error writing {summary_file}: {e}")

    return updated_count


# --- GT06 GPS Tracker Protocol ---

# Battery level mapping: GT06 reports 0-6, server expects 0-100
_GT06_BATTERY_MAP = {0: 0, 1: 5, 2: 15, 3: 30, 4: 50, 5: 75, 6: 100}


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
        from calendar import timegm
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


def load_gt06_config(config_path: Path) -> dict:
    """Load GT06 device config from JSON file.

    Returns {"default_eid": int, "devices": {imei: {...}}}.
    If file doesn't exist, returns defaults.
    """
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
        log(f"[GT06] Loaded config from {config_path}: {len(result['devices'])} device(s), default_eid={result['default_eid']}")
        return result
    except Exception as e:
        log(f"[GT06] Warning: Could not load {config_path}: {e}")
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
    """

    def __init__(self, port, interval, id_prefix, get_tracker_func, gt06_config=None, log_file=None):
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
            log(f"[GT06] Packet log write error: {e}")

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
        log(f"[GT06] Connection from {addr[0]}:{addr[1]}")

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
        log(f"[GT06] Disconnected: {label} ({gt_conn.addr[0]}:{gt_conn.addr[1]})")

    def _send(self, gt_conn, data):
        """Best-effort send to a GT06 connection."""
        try:
            gt_conn.sock.sendall(data)
            self._log_packet(data)
        except Exception as e:
            log(f"[GT06] Send error to {gt_conn.addr}: {e}")
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
        log(f"[GT06] Sent to {label}: {cmd_str} (queue: {len(gt_conn.cmd_queue)} remaining)")

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
                log(f"[GT06] TCP ACK confirmed for {label}: {gt_conn.cmd_pending}")
            elif now - gt_conn.cmd_sent_time > 30:
                # TCP can't deliver the data — connection dead/very laggy
                label = gt_conn.sailor_id or gt_conn.imei or "unknown"
                log(f"[GT06] TCP delivery timeout for {label}: {gt_conn.cmd_pending} — disconnecting")
                self._disconnect(fd)
                return
        else:
            # TCP delivered but no 0x15 app ACK — not all commands produce one
            # (e.g. SUP doesn't ACK). Advance queue after 10s.
            if now - gt_conn.cmd_tcp_ack_time > 10:
                label = gt_conn.sailor_id or gt_conn.imei or "unknown"
                log(f"[GT06] No app ACK for {label}: {gt_conn.cmd_pending} — advancing queue")
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
                log(f"[GT06] Rate mismatch for {label}: "
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
                log(f"[GT06] Rate mismatch for {label} after 2 retries — disconnecting")
                self._disconnect(fd)
                return

        # Check HBT rate — if no heartbeat for > 3x expected interval and we're getting LOC
        if gt_conn.last_hbt_time > 0 and gt_conn.loc_count > 0:
            hbt_gap = now - gt_conn.last_hbt_time
            if hbt_gap > gt_conn.expected_hbt_interval * 3:
                log(f"[GT06] No heartbeat from {label} for {hbt_gap:.0f}s — re-queuing HBT")
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
            log(f"[GT06] CRC mismatch from {gt_conn.addr}: "
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
            log(f"[GT06] Login: IMEI {imei} -> {gt_conn.sailor_id} (eid={gt_conn.eid})")
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
                        save_user_overrides(tracker.users_file, overrides)
                        log(f"[GT06] Set display name for {gt_conn.sailor_id} (did:{imei}): {dev_name}")

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
            log(f"[GT06] Login commands queued ({'active' if not gt_conn.idle else 'idle'})")

            # Restore sticky SOS across TCP reconnects
            if imei in self._sticky_assist:
                gt_conn.assist_active = True
                if gt_conn.idle:
                    self.set_idle(gt_conn.sailor_id, False)
                log(f"[GT06] Restored sticky SOS after reconnect for {gt_conn.sailor_id}")

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
                log(f"[GT06] Location packet too short from {gt_conn.sailor_id}")
                return
            if not gt_conn.sailor_id:
                log(f"[GT06] Location before login from {gt_conn.addr}")
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
            log(f"[GT06] Heartbeat {label}: bat={bat_str} sig={sig_str}{' (idle)' if gt_conn.idle else ''}")
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
            log(f"[GT06] Alarm from {label}: {alarm_type}")

            if is_sos:
                imei = gt_conn.imei
                if imei not in self._sticky_assist:
                    gt_conn.assist_active = True
                    self._sticky_assist.add(imei)
                    log(f"[GT06] SOS activated (sticky) from {label}")
                    # Come out of idle so we get full GPS tracking
                    if gt_conn.idle:
                        self.set_idle(gt_conn.sailor_id, False)
                        log(f"[GT06] Exited idle due to SOS from {label}")
                else:
                    log(f"[GT06] SOS already active, ignoring repeat press from {label}")

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
            log(f"[GT06] Command ACK from {label}:{text}")
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
                log(f"[GT06] Cancelled assist for {sailor_id} (sticky cleared)")
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
                    if pt.positions_file:
                        overrides = tracker.user_overrides if hasattr(tracker, 'user_overrides') else {}
                        write_current_positions(pt.current_positions, pt.positions_file, overrides, pt.position_tails)
                log(f"[GT06] {'Idle' if idle else 'Active'} mode for {sailor_id}")
                return True
        # No active connection, but state is saved for reconnection
        log(f"[GT06] {'Idle' if idle else 'Active'} mode queued for {sailor_id} (not connected)")
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
                log(f"[GT06] Frame error from {label}: {e}")

    def run(self):
        """Main loop — runs in a daemon thread."""
        if self.log_file:
            try:
                self._log_fd = open(self.log_file, "ab")
                log(f"[GT06] Packet logging to {self.log_file}")
            except Exception as e:
                log(f"[GT06] Warning: Could not open packet log {self.log_file}: {e}")

        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.setblocking(False)
        server_sock.bind(("0.0.0.0", self.port))
        server_sock.listen(16)
        self.sel.register(server_sock, selectors.EVENT_READ, data="server")
        log(f"[GT06] Listening on TCP port {self.port} (interval={self.interval}s, prefix={self.id_prefix})")

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
                        log(f"[GT06] Accept error: {e}")
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


class EventManager:
    """Manages multiple events with their configurations and passwords."""

    def __init__(self, events_file: Path, html_dir: Path):
        self.events_file = events_file
        self.html_dir = html_dir
        self.events: dict[int, dict] = {}
        self.manager_password: str = ""
        self.next_eid: int = 1
        self._lock = threading.Lock()
        self._load_events()

    def _load_events(self):
        """Load events from JSON file."""
        if not self.events_file.exists():
            log(f"[EVENTS] No events file found at {self.events_file}")
            return

        try:
            with open(self.events_file, 'r') as f:
                data = json.load(f)
            self.manager_password = data.get('manager_password', '')
            self.next_eid = data.get('next_eid', 1)
            # Load events, converting string keys to int
            events_data = data.get('events', {})
            for eid_str, event in events_data.items():
                try:
                    eid = int(eid_str)
                    # Normalize tracker_password to list
                    tp = event.get('tracker_password', '')
                    if isinstance(tp, str):
                        event['tracker_password'] = [tp] if tp else []
                    self.events[eid] = event
                except ValueError:
                    log(f"[EVENTS] Skipping invalid event ID: {eid_str}")
            log(f"[EVENTS] Loaded {len(self.events)} events from {self.events_file}")
        except Exception as e:
            log(f"[EVENTS] Error loading events file: {e}")

    def _save_events(self):
        """Save events to JSON file (atomic write)."""
        output = {
            "next_eid": self.next_eid,
            "manager_password": self.manager_password,
            "events": {str(eid): event for eid, event in self.events.items()}
        }
        try:
            tmp_file = self.events_file.with_suffix('.tmp')
            with open(tmp_file, 'w') as f:
                json.dump(output, f, indent=2)
            tmp_file.rename(self.events_file)
            log(f"[EVENTS] Saved {len(self.events)} events to {self.events_file}")
        except Exception as e:
            log(f"[EVENTS] Error saving events file: {e}")

    def get_event(self, eid: int) -> dict | None:
        """Get event by ID."""
        with self._lock:
            return self.events.get(eid)

    def list_events(self) -> list[int]:
        """Get list of all event IDs."""
        with self._lock:
            return list(self.events.keys())

    def get_public_events(self) -> list[dict]:
        """Get list of active (non-archived) events without passwords."""
        with self._lock:
            result = []
            for eid, event in self.events.items():
                if not event.get('archived', False):
                    result.append({
                        "eid": eid,
                        "name": event.get("name", f"Event {eid}"),
                        "description": event.get("description", ""),
                        "timezone": event.get("timezone", "Australia/Sydney"),
                        "home_location": event.get("home_location", ""),
                        "home_lat": event.get("home_lat"),
                        "home_lon": event.get("home_lon")
                    })
            # Sort by name
            result.sort(key=lambda e: e.get("name", ""))
            return result

    def get_all_events(self) -> list[dict]:
        """Get list of all events with full details (for manager)."""
        with self._lock:
            result = []
            for eid, event in self.events.items():
                result.append({
                    "eid": eid,
                    **event
                })
            # Sort by eid
            result.sort(key=lambda e: e.get("eid", 0))
            return result

    def create_event(self, name: str, description: str,
                     admin_password: str, tracker_password="",
                     timezone: str = "Australia/Sydney",
                     home_location: str = "", home_lat: float = None,
                     home_lon: float = None) -> int:
        """Create new event, return event ID."""
        # Normalize tracker_password to list
        if isinstance(tracker_password, str):
            tracker_password = [tracker_password] if tracker_password else []
        with self._lock:
            eid = self.next_eid
            self.next_eid += 1
            self.events[eid] = {
                "name": name,
                "description": description,
                "admin_password": admin_password,
                "tracker_password": tracker_password,
                "timezone": timezone,
                "home_location": home_location,
                "home_lat": home_lat,
                "home_lon": home_lon,
                "archived": False,
                "assist_enabled": True,  # Whether assist button is available to users
                "idle_interval": 0,  # Idle heartbeat interval in seconds (0=disabled)
                "created": time.time(),
                "created_iso": datetime.now().isoformat()
            }
            self._save_events()
            # Create event data directory
            self._ensure_event_dir(eid)
            log(f"[EVENTS] Created event {eid}: {name} (timezone: {timezone}, location: {home_location})")
            return eid

    def update_event(self, eid: int, updates: dict) -> bool:
        """Update event properties (name, description, archived, passwords, timezone, location)."""
        with self._lock:
            if eid not in self.events:
                return False
            event = self.events[eid]
            # Only allow updating certain fields
            allowed_fields = ['name', 'description', 'archived', 'assist_enabled',
                              'idle_interval',
                              'admin_password', 'tracker_password', 'timezone',
                              'home_location', 'home_lat', 'home_lon']
            for field in allowed_fields:
                if field in updates:
                    event[field] = updates[field]
            # Normalize tracker_password to list
            if 'tracker_password' in updates:
                tp = event['tracker_password']
                if isinstance(tp, str):
                    event['tracker_password'] = [tp] if tp else []
            event['updated'] = time.time()
            event['updated_iso'] = datetime.now().isoformat()
            self._save_events()
            log(f"[EVENTS] Updated event {eid}: {updates}")
            return True

    def _ensure_event_dir(self, eid: int):
        """Ensure event data directory exists."""
        event_dir = self.html_dir / str(eid)
        logs_dir = event_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        log(f"[EVENTS] Ensured directory exists: {event_dir}")

    def get_event_data_dir(self, eid: int) -> Path:
        """Get data directory for event, creating if needed."""
        event_dir = self.html_dir / str(eid)
        if not event_dir.exists():
            self._ensure_event_dir(eid)
        return event_dir


def write_current_positions(positions: dict, positions_file: Path, user_overrides: dict | None = None, position_tails: dict | None = None):
    """Write current positions to a JSON file for web UI consumption."""
    # Deduplicate did entries: if two positions share the same did,
    # clear did from the older one (keep position, just remove did link)
    did_map: dict[str, list[tuple[str, float]]] = {}
    for sid, pos in positions.items():
        did = pos.get("did")
        if did:
            did_map.setdefault(did, []).append((sid, pos.get("last_seen", 0)))
    for did, entries in did_map.items():
        if len(entries) > 1:
            entries.sort(key=lambda x: x[1], reverse=True)
            for sid, last_seen in entries[1:]:
                del positions[sid]["did"]
                log(f"[CLEANUP] Cleared duplicate did={did} from {sid} (keeping in {entries[0][0]})")

    # Apply user overrides for display (name, role, hidden, info)
    display_positions = {}
    for sailor_id, pos in positions.items():
        display_pos = pos.copy()
        override = get_user_override(user_overrides, sailor_id, pos.get("did"))
        if override:
            if 'name' in override:
                display_pos['name'] = override['name']
                display_pos['displayid'] = override['name']
            if 'role' in override:
                display_pos['role'] = override['role']
            if override.get('hidden'):
                display_pos['hidden'] = True
            else:
                display_pos.pop('hidden', None)
            if 'info' in override:
                display_pos['info'] = override['info']
        # Add position tail if available (last 20 seconds of positions)
        if position_tails and sailor_id in position_tails:
            display_pos['tail'] = position_tails[sailor_id]
        display_positions[sailor_id] = display_pos

    output = {
        "updated": time.time(),
        "updated_iso": datetime.now().isoformat(),
        "sailors": display_positions
    }
    # Write atomically to avoid partial reads
    # Use absolute paths to avoid issues when working directory differs
    try:
        positions_file = positions_file.resolve()
        tmp_file = positions_file.with_suffix('.tmp')
        with open(tmp_file, 'w') as f:
            json.dump(output, f, indent=2)
        tmp_file.rename(positions_file)
    except OSError as e:
        log(f"[WARNING] Failed to write positions file: {e}")


class DailyLogger:
    """Handles daily log file rotation."""

    def __init__(self, log_dir: Path, tz_name: str = "Australia/Sydney"):
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.current_date = None
        self.log_fh = None
        self._write_lock = threading.Lock()
        # Store timezone for date calculations
        try:
            self.tz = ZoneInfo(tz_name)
        except Exception as e:
            log(f"[WARNING] Invalid timezone '{tz_name}', using Australia/Sydney: {e}")
            self.tz = ZoneInfo("Australia/Sydney")
        self._open_log_for_today()

    def _get_log_filename(self, d: date) -> Path:
        return self.log_dir / f"{d.strftime('%Y_%m_%d')}.jsonl"

    def _get_today_in_tz(self) -> date:
        """Get today's date in the configured timezone."""
        return datetime.now(self.tz).date()

    def _open_log_for_today(self):
        today = self._get_today_in_tz()
        if self.current_date != today:
            if self.log_fh:
                self.log_fh.close()
            self.current_date = today
            log_path = self._get_log_filename(today)
            self.log_fh = open(log_path, 'a')
            log(f"Logging to: {log_path}")

    def write(self, entry: dict):
        """Write a log entry, rolling over at midnight if needed."""
        with self._write_lock:
            self._open_log_for_today()
            self.log_fh.write(json.dumps(entry) + "\n")
            self.log_fh.flush()

    def close(self):
        with self._write_lock:
            if self.log_fh:
                self.log_fh.close()
                self.log_fh = None

    def clear_today(self):
        """Clear today's log file by rotating it to .1, .2, etc."""
        with self._write_lock:
            self._open_log_for_today()
            if self.log_fh:
                self.log_fh.close()
                self.log_fh = None
            log_path = self._get_log_filename(self._get_today_in_tz())
            # Rotate the file instead of truncating
            rotate_file(log_path)
            # Open a fresh log file
            self.log_fh = open(log_path, 'a')
            log(f"Cleared track log: {log_path}")

    def merge_entries(self, date_str: str, new_entries: list[dict]):
        """Merge new entries into a JSONL log file, sorted by timestamp.

        Holds _write_lock for the entire read-modify-write to prevent
        DailyLogger.write() from appending during the merge. Uses
        temp file + atomic rename to prevent corruption.
        """
        import tempfile

        log_file = self.log_dir / f"{date_str}.jsonl"
        today_str = self._get_today_in_tz().strftime('%Y_%m_%d')
        is_today = (date_str == today_str)

        with self._write_lock:
            # If merging into today's file, close the append handle first
            if is_today and self.log_fh:
                self.log_fh.flush()
                self.log_fh.close()
                self.log_fh = None

            try:
                # Read existing entries
                existing = []
                if log_file.exists():
                    with open(log_file, 'r') as f:
                        for line in f:
                            line = line.strip()
                            if line:
                                try:
                                    existing.append(json.loads(line))
                                except json.JSONDecodeError:
                                    pass  # Skip corrupt lines

                # Merge and sort by timestamp
                merged = existing + new_entries
                merged.sort(key=lambda e: e.get('ts', 0))

                # Write to temp file in same directory for atomic rename
                tmp_fd, tmp_path = tempfile.mkstemp(
                    dir=str(self.log_dir), suffix='.jsonl.tmp')
                try:
                    with os.fdopen(tmp_fd, 'w') as f:
                        for entry in merged:
                            f.write(json.dumps(entry) + '\n')
                    # Atomic replace
                    os.rename(tmp_path, str(log_file))
                    # Set mtime to the last entry's timestamp so mtime-based
                    # caching in the summary generator and compressor reflects
                    # the actual data content, not the upload time
                    if merged:
                        last_ts = merged[-1].get('ts', 0)
                        if last_ts > 0:
                            os.utime(str(log_file), (last_ts, last_ts))
                except Exception:
                    # Clean up temp file on failure
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    raise
            finally:
                # If we closed today's file, force reopen on next write()
                if is_today:
                    self.current_date = None


class PositionTracker:
    """Handles position tracking state and processing."""

    # How many seconds of position history to keep for tails
    TAIL_DURATION_SECONDS = 20

    def __init__(self, positions_file: Path | None, daily_logger: DailyLogger | None):
        self.positions_file = positions_file
        self.daily_logger = daily_logger
        self.current_positions: dict[str, dict] = {}
        self.last_timestamp: dict[str, int] = {}
        self.last_sq: dict[str, int] = {}
        # Position tails: sailor_id -> list of [ts, lat, lon] for last 20 seconds
        self.position_tails: dict[str, list] = {}
        self._lock = threading.Lock()
        # Load existing state from positions file if it exists
        self._load_from_file()

    def _load_from_file(self):
        """Load position state from existing positions file on startup."""
        if not self.positions_file:
            return
        try:
            positions_path = self.positions_file.resolve()
            if not positions_path.exists():
                return
            with open(positions_path, 'r') as f:
                data = json.load(f)
            sailors = data.get('sailors', {})
            if not sailors:
                return
            with self._lock:
                for sailor_id, pos in sailors.items():
                    # Restore position data (excluding display overrides)
                    self.current_positions[sailor_id] = {
                        "id": pos.get("id", sailor_id),
                        "lat": pos.get("lat", 0),
                        "lon": pos.get("lon", 0),
                        "spd": pos.get("spd", 0),
                        "hdg": pos.get("hdg", 0),
                        "ast": pos.get("ast", False),
                        "bat": pos.get("bat", -1),
                        "sig": pos.get("sig", -1),
                        "role": pos.get("role", "sailor"),
                        "ver": pos.get("ver", ""),
                        "flg": pos.get("flg", {}),
                        "ts": pos.get("ts", 0),
                        "last_seen": pos.get("last_seen", 0),
                        "last_seen_iso": pos.get("last_seen_iso", ""),
                        "src_ip": pos.get("src_ip", "")
                    }
                    if "did" in pos:
                        self.current_positions[sailor_id]["did"] = pos["did"]
                    if "chg" in pos:
                        self.current_positions[sailor_id]["chg"] = pos["chg"]
                    # Restore timestamp and sq tracking for duplicate detection
                    if pos.get("ts"):
                        self.last_timestamp[sailor_id] = pos["ts"]
                    if pos.get("sq"):
                        self.last_sq[sailor_id] = pos["sq"]
            log(f"[STARTUP] Loaded {len(sailors)} positions from {positions_path}")
        except Exception as e:
            log(f"[STARTUP] Could not load positions file: {e}")

    def clear(self):
        """Clear all position state."""
        with self._lock:
            self.current_positions.clear()
            self.last_timestamp.clear()
            self.last_sq.clear()
            self.position_tails.clear()
        log("[ADMIN] Cleared internal position state")

    def process_position(self, sailor_id: str, lat: float, lon: float, speed: float,
                         heading: int, ts: int, assist: bool, battery: int, signal: int,
                         role: str, version: str, flags: dict, src_ip: str, source: str = "UDP",
                         battery_drain_rate: float | None = None, heart_rate: int | None = None,
                         os_version: str | None = None, horizontal_accuracy: float | None = None,
                         nsats: int | None = None,
                         skip_log: bool = False, stopped: bool = False,
                         pos_array: list | None = None, user_overrides: dict | None = None,
                         idle: bool = False, charging: bool | None = None,
                         sq: int = 0, did: str | None = None) -> bool:
        """
        Process a position update from any source (UDP or HTTP).
        Returns True if this was a new position, False if duplicate.
        If stopped=True, the user deliberately stopped tracking (vs losing signal).
        If pos_array is provided (1Hz mode), all positions are added to the tail.
        If idle=True, this is a heartbeat from an idle app (no GPS data).
        """
        recv_time = time.time()

        # Idle packets: update metadata but preserve existing position
        if idle:
            with self._lock:
                self.last_timestamp[sailor_id] = ts
                if sq > 0:
                    self.last_sq[sailor_id] = sq
                existing = self.current_positions.get(sailor_id, {})
                pos_data = {
                    "id": sailor_id,
                    "bat": battery,
                    "sig": signal,
                    "role": role,
                    "ver": version,
                    "flg": flags,
                    "ts": ts,
                    "last_seen": recv_time,
                    "last_seen_iso": datetime.fromtimestamp(recv_time).isoformat(),
                    "src_ip": src_ip,
                    "stopped": True,
                    "idle": True,
                }
                if did:
                    pos_data["did"] = did
                if charging is not None:
                    pos_data["chg"] = charging
                if os_version:
                    pos_data["os"] = os_version
                # Preserve existing lat/lon if user previously tracked
                if "lat" in existing and "lon" in existing:
                    pos_data["lat"] = existing["lat"]
                    pos_data["lon"] = existing["lon"]
                self.current_positions[sailor_id] = pos_data

            bat_str = f"{battery}%" if battery >= 0 else "?"
            if charging:
                bat_str += "+"
            sig_str = f"{signal}/4" if signal >= 0 else "?"
            flg_parts = []
            if flags.get("ps"):
                flg_parts.append("PS")
            if not flags.get("bo"):
                flg_parts.append("!BO")
            flg_str = f" [{','.join(flg_parts)}]" if flg_parts else ""
            log(f"[{sailor_id}] Idle heartbeat bat={bat_str} sig={sig_str}{flg_str} [{source}] ip={src_ip}")

            # Write current positions file (no log entry, no tail update)
            if self.positions_file:
                write_current_positions(self.current_positions, self.positions_file, user_overrides, self.position_tails)

            return True

        # GPS-wait packets: tracking active but no GPS fix yet (nsats=0 or missing lat/lon)
        no_gps = (nsats == 0) or (lat == 0.0 and lon == 0.0)
        if no_gps:
            with self._lock:
                self.last_timestamp[sailor_id] = ts
                if sq > 0:
                    self.last_sq[sailor_id] = sq
                existing = self.current_positions.get(sailor_id, {})
                pos_data = {
                    "id": sailor_id,
                    "bat": battery,
                    "sig": signal,
                    "role": role,
                    "ver": version,
                    "flg": flags,
                    "ts": ts,
                    "last_seen": recv_time,
                    "last_seen_iso": datetime.fromtimestamp(recv_time).isoformat(),
                    "src_ip": src_ip,
                    "nsats": 0,
                }
                if stopped:
                    pos_data["stopped"] = True
                if did:
                    pos_data["did"] = did
                if charging is not None:
                    pos_data["chg"] = charging
                if os_version:
                    pos_data["os"] = os_version
                # Preserve existing lat/lon if user previously tracked
                if "lat" in existing and "lon" in existing:
                    pos_data["lat"] = existing["lat"]
                    pos_data["lon"] = existing["lon"]
                self.current_positions[sailor_id] = pos_data

            bat_str = f"{battery}%" if battery >= 0 else "?"
            if charging:
                bat_str += "+"
            sig_str = f"{signal}/4" if signal >= 0 else "?"
            flg_parts = []
            if flags.get("ps"):
                flg_parts.append("PS")
            if not flags.get("bo"):
                flg_parts.append("!BO")
            flg_str = f" [{','.join(flg_parts)}]" if flg_parts else ""
            label = "Stopped" if stopped else "GPS-wait heartbeat"
            log(f"[{sailor_id}] {label} bat={bat_str} sig={sig_str}{flg_str} [{source}] ip={src_ip}")

            # Log GPS-wait entry (no lat/lon — WebUI skips entries without coordinates)
            if self.daily_logger:
                track_entry = {
                    "id": sailor_id,
                    "ts": ts,
                    "recv_ts": recv_time,
                    "nogps": True,
                    "bat": battery,
                    "sig": signal,
                    "role": role,
                    "ver": version,
                    "flg": flags
                }
                if did:
                    track_entry["did"] = did
                if charging is not None:
                    track_entry["chg"] = charging
                if battery_drain_rate is not None:
                    track_entry["bdr"] = battery_drain_rate
                if os_version:
                    track_entry["os"] = os_version
                self.daily_logger.write(track_entry)

            # Write current positions file (no tail update for GPS-wait)
            if self.positions_file:
                write_current_positions(self.current_positions, self.positions_file, user_overrides, self.position_tails)

            return True

        with self._lock:
            # Check for duplicate using sequence number (preferred) or timestamp (fallback)
            is_dup = False
            if sailor_id in self.last_sq and sq > 0:
                # Use sequence number: same sq = retransmission, different sq = new packet
                if sq == self.last_sq[sailor_id]:
                    is_dup = True
            elif sailor_id in self.last_timestamp:
                # Fallback for clients that don't send sq (or sq=0)
                if ts <= self.last_timestamp[sailor_id]:
                    is_dup = True

            if not is_dup:
                self.last_timestamp[sailor_id] = ts
                if sq > 0:
                    self.last_sq[sailor_id] = sq

        # If stopped=True, clear any assist request
        if stopped:
            assist = False

        # Format output
        dup_marker = " [DUP]" if is_dup else ""
        assist_marker = " *** ASSIST REQUESTED ***" if assist else ""
        stopped_marker = " [STOPPED]" if stopped else ""
        bat_str = f"{battery}%" if battery >= 0 else "?"
        if charging:
            bat_str += "+"
        sig_str = f"{signal}/4" if signal >= 0 else "?"
        flg_parts = []
        if flags.get("ps"):
            flg_parts.append("PS")
        if not flags.get("bo"):
            flg_parts.append("!BO")
        flg_str = f" [{','.join(flg_parts)}]" if flg_parts else ""
        hac_str = f" hac={horizontal_accuracy:.0f}m" if horizontal_accuracy is not None else ""
        local_time = datetime.fromtimestamp(recv_time).strftime("%Y-%m-%d %H:%M:%S")

        log_line = (
            f"{local_time} [{sailor_id}] "
            f"pos={format_position(lat, lon)}{hac_str} "
            f"spd={speed:.1f}kn hdg={heading:03d}° "
            f"bat={bat_str} sig={sig_str}{flg_str} "
            f"ver={version} "
            f"time={format_timestamp(ts)} "
            f"[{source}] "
            f"ip={src_ip}"
            f"{dup_marker}{assist_marker}{stopped_marker}"
        )
        print(log_line)

        if stopped:
            log(f"[{sailor_id}] Tracking stopped by user")

        if assist:
            log("!" * 60)
            log(f"!!! SAILOR {sailor_id} REQUESTING ASSISTANCE !!!")
            log(f"!!! Position: {format_position(lat, lon)}")
            log("!" * 60)

        # Update current positions (only if not a duplicate, but always update if stopped)
        if not is_dup or stopped:
            with self._lock:
                pos_data = {
                    "id": sailor_id,
                    "lat": lat,
                    "lon": lon,
                    "spd": speed,
                    "hdg": heading,
                    "ast": assist,
                    "bat": battery,
                    "sig": signal,
                    "role": role,
                    "ver": version,
                    "flg": flags,
                    "ts": ts,
                    "last_seen": recv_time,
                    "last_seen_iso": datetime.fromtimestamp(recv_time).isoformat(),
                    "src_ip": src_ip
                }
                if sq > 0:
                    pos_data["sq"] = sq
                if did:
                    pos_data["did"] = did
                if charging is not None:
                    pos_data["chg"] = charging
                if battery_drain_rate is not None:
                    pos_data["bdr"] = battery_drain_rate
                if heart_rate is not None and heart_rate > 0:
                    pos_data["hr"] = heart_rate
                if os_version:
                    pos_data["os"] = os_version
                if horizontal_accuracy is not None:
                    pos_data["hac"] = horizontal_accuracy
                if nsats is not None:
                    pos_data["nsats"] = nsats
                if stopped:
                    pos_data["stopped"] = True
                self.current_positions[sailor_id] = pos_data

                # Update position tail (last 20 seconds of positions)
                if sailor_id not in self.position_tails:
                    self.position_tails[sailor_id] = []
                tail = self.position_tails[sailor_id]
                # In 1Hz mode, add all positions from the array
                if pos_array and isinstance(pos_array, list) and len(pos_array) > 0:
                    for pos_entry in pos_array:
                        if len(pos_entry) >= 3:
                            tail.append([pos_entry[0], pos_entry[1], pos_entry[2]])
                else:
                    # Standard mode - just add current position
                    tail.append([ts, lat, lon])
                # Remove positions older than TAIL_DURATION_SECONDS
                cutoff_ts = ts - self.TAIL_DURATION_SECONDS
                while tail and tail[0][0] < cutoff_ts:
                    tail.pop(0)

            # Write current positions file
            if self.positions_file:
                write_current_positions(self.current_positions, self.positions_file, user_overrides, self.position_tails)

            # Write to daily track log (unless skip_log is True, e.g., for batch entries)
            if self.daily_logger and not skip_log:
                track_entry = {
                    "id": sailor_id,
                    "ts": ts,
                    "recv_ts": recv_time,
                    "lat": lat,
                    "lon": lon,
                    "spd": speed,
                    "hdg": heading,
                    "ast": assist,
                    "bat": battery,
                    "sig": signal,
                    "role": role,
                    "ver": version,
                    "flg": flags
                }
                if did:
                    track_entry["did"] = did
                if charging is not None:
                    track_entry["chg"] = charging
                if battery_drain_rate is not None:
                    track_entry["bdr"] = battery_drain_rate
                if heart_rate is not None and heart_rate > 0:
                    track_entry["hr"] = heart_rate
                if os_version:
                    track_entry["os"] = os_version
                if horizontal_accuracy is not None:
                    track_entry["hac"] = horizontal_accuracy
                if nsats is not None:
                    track_entry["nsats"] = nsats
                # Add displayid if user has a name mapping
                override = get_user_override(user_overrides, sailor_id, did)
                if override and override.get('name'):
                    track_entry["displayid"] = override['name']
                self.daily_logger.write(track_entry)

        return not is_dup


class EventTracker:
    """Per-event tracker wrapping PositionTracker, DailyLogger, and user overrides."""

    def __init__(self, eid: int, data_dir: Path, event_config: dict):
        self.eid = eid
        self.data_dir = data_dir
        self.event_config = event_config
        self.positions_file = data_dir / "current_positions.json"
        self.course_file = data_dir / "course.json"
        self.users_file = data_dir / "users.json"
        self.log_dir = data_dir / "logs"

        # Ensure directories exist
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # Create daily logger with event timezone
        event_tz = event_config.get('timezone', 'Australia/Sydney')
        self.daily_logger = DailyLogger(self.log_dir, event_tz)

        # Load user overrides
        self.user_overrides = load_user_overrides(self.users_file)

        # Create position tracker
        self.position_tracker = PositionTracker(self.positions_file, self.daily_logger)

        # Ensure current_positions.json exists
        if not self.positions_file.exists():
            write_current_positions({}, self.positions_file, self.user_overrides)

        log(f"[EVENT {eid}] Initialized tracker for '{event_config.get('name', 'Unnamed')}'")

    def process_position(self, sailor_id: str, lat: float, lon: float, speed: float,
                         heading: int, ts: int, assist: bool, battery: int, signal: int,
                         role: str, version: str, flags: dict, src_ip: str, source: str = "UDP",
                         battery_drain_rate: float | None = None, heart_rate: int | None = None,
                         os_version: str | None = None, horizontal_accuracy: float | None = None,
                         nsats: int | None = None,
                         skip_log: bool = False, pos_array: list | None = None,
                         stopped: bool = False, idle: bool = False,
                         charging: bool | None = None, sq: int = 0,
                         did: str | None = None) -> bool:
        """Process a position update for this event."""
        recv_time = time.time()

        # Idle packets skip batch logging entirely
        if idle:
            return self.position_tracker.process_position(
                sailor_id=sailor_id, lat=lat, lon=lon, speed=speed,
                heading=heading, ts=ts, assist=assist, battery=battery,
                signal=signal, role=role, version=version, flags=flags,
                src_ip=src_ip, source=f"[E{self.eid}]{source}",
                os_version=os_version, idle=True,
                user_overrides=self.user_overrides,
                charging=charging, sq=sq, did=did
            )

        # If 1Hz array format, log as single entry with pos array (more compact)
        has_batch = pos_array and isinstance(pos_array, list) and len(pos_array) > 1
        if has_batch and self.daily_logger:
            track_entry = {
                "id": sailor_id,
                "ts": ts,
                "recv_ts": recv_time,
                "pos": pos_array,
                "spd": speed,
                "hdg": heading,
                "ast": assist,
                "bat": battery,
                "sig": signal,
                "role": role,
                "ver": version,
                "flg": flags
            }
            if did:
                track_entry["did"] = did
            if charging is not None:
                track_entry["chg"] = charging
            if battery_drain_rate is not None:
                track_entry["bdr"] = battery_drain_rate
            if heart_rate is not None and heart_rate > 0:
                track_entry["hr"] = heart_rate
            if os_version:
                track_entry["os"] = os_version
            if horizontal_accuracy is not None:
                track_entry["hac"] = horizontal_accuracy
            if nsats is not None:
                track_entry["nsats"] = nsats
            # Add displayid if user has a name mapping
            override = get_user_override(self.user_overrides, sailor_id, did)
            if override and override.get('name'):
                track_entry["displayid"] = override['name']
            self.daily_logger.write(track_entry)

        # Process through position tracker
        result = self.position_tracker.process_position(
            sailor_id=sailor_id,
            lat=lat,
            lon=lon,
            speed=speed,
            heading=heading,
            ts=ts,
            assist=assist,
            battery=battery,
            signal=signal,
            role=role,
            version=version,
            flags=flags,
            src_ip=src_ip,
            source=f"[E{self.eid}]{source}",
            battery_drain_rate=battery_drain_rate,
            heart_rate=heart_rate,
            os_version=os_version,
            horizontal_accuracy=horizontal_accuracy,
            nsats=nsats,
            skip_log=has_batch or skip_log,
            stopped=stopped,
            pos_array=pos_array,
            user_overrides=self.user_overrides,
            charging=charging,
            sq=sq,
            did=did
        )

        # Write positions with event-specific user overrides
        if result and self.positions_file:
            write_current_positions(
                self.position_tracker.current_positions,
                self.positions_file,
                self.user_overrides,
                self.position_tracker.position_tails
            )

        return result

    def clear_tracks(self):
        """Clear tracks for this event (rotates log file)."""
        if self.daily_logger:
            self.daily_logger.clear_today()
        if self.positions_file and self.positions_file.exists():
            self.positions_file.unlink()
        self.position_tracker.clear()
        # Recreate empty positions file
        write_current_positions({}, self.positions_file, self.user_overrides)
        log(f"[EVENT {self.eid}] Tracks cleared")

    def clear_positions_only(self):
        """Clear in-memory positions without rotating log file.

        Used by midnight auto-clear since DailyLogger already handles
        switching to a new date-named log file automatically.
        """
        if self.positions_file and self.positions_file.exists():
            self.positions_file.unlink()
        self.position_tracker.clear()
        # Recreate empty positions file
        write_current_positions({}, self.positions_file, self.user_overrides)
        log(f"[EVENT {self.eid}] Positions cleared (midnight auto-clear)")

    def close(self):
        """Clean up resources."""
        if self.daily_logger:
            self.daily_logger.close()


# Global references for HTTP handler to access
# Multi-event mode globals
_event_manager: EventManager | None = None
_event_trackers: dict[int, EventTracker] = {}  # eid -> EventTracker
_event_trackers_lock = threading.Lock()

_gt06_listener: "GT06Listener | None" = None

# Pending commands: {event_id}:{user_id} -> (cmd, queued_time, expiry_seconds)
# Only one pending command per client. New commands overwrite previous.
# Commands: "stop", "cancel_assist", "start", "shutdown"
_pending_commands: dict[str, tuple[str, float, float]] = {}
_CMD_EXPIRY = {
    "stop": 30.0,
    "cancel_assist": 30.0,
    "start": 90.0,
    "shutdown": 90.0,
}

# Last known UDP address for proactive command sending: "{eid}:{sailor_id}" -> (ip, port)
_client_addrs: dict[str, tuple[str, int]] = {}

# UDP socket reference so HTTP handler threads can send proactive commands
_udp_sock: socket.socket | None = None


def queue_pending_command(key: str, cmd: str):
    """Queue a pending command for a client. Overwrites any existing command."""
    _pending_commands[key] = (cmd, time.time(), _CMD_EXPIRY[cmd])


def send_proactive_command(key: str, cmd: str):
    """Best-effort proactive send of a command to a client's last known UDP address.

    Sends {"ack": 0, "ts": <now>, "cmd": "<cmd>", "proactive": true} to the client.
    Uses ack: 0 as a sentinel (no real ACK uses seq 0).
    Does NOT consume the pending command - it stays in _pending_commands for the normal ACK path.
    """
    if _udp_sock is None:
        return
    addr = _client_addrs.get(key)
    if addr is None:
        return
    try:
        proactive_pkt = json.dumps({
            "ack": 0,
            "ts": int(time.time()),
            "cmd": cmd,
            "proactive": True
        }).encode("utf-8")
        _udp_sock.sendto(proactive_pkt, addr)
        log(f"[UDP] Proactive {cmd} sent to {key} at {addr}")
    except Exception as e:
        log(f"[UDP] Proactive send failed for {key}: {e}")

# Rate limiting for password guessing protection
# Maps (IP, sailor_id) -> timestamp of last failed auth attempt
# Using (IP, sailor_id) tuple so one misconfigured tracker doesn't block
# all trackers sharing the same public IP (e.g. behind cellular NAT)
_failed_auth_times: dict[tuple[str, str], float] = {}
_RATE_LIMIT_SECONDS = 5.0


def is_rate_limited(ip: str, sailor_id: str = "__admin__") -> bool:
    """Check if an (IP, sailor_id) pair is rate limited due to recent failed auth."""
    key = (ip, sailor_id)
    if key in _failed_auth_times:
        elapsed = time.time() - _failed_auth_times[key]
        if elapsed < _RATE_LIMIT_SECONDS:
            return True
    return False


def record_failed_auth(ip: str, sailor_id: str = "__admin__"):
    """Record a failed authentication attempt for rate limiting."""
    _failed_auth_times[(ip, sailor_id)] = time.time()


def get_event_tracker(eid: int) -> EventTracker | None:
    """Get or create an EventTracker for the given event ID."""
    global _event_trackers

    event = _event_manager.get_event(eid)
    if not event:
        return None

    with _event_trackers_lock:
        if eid not in _event_trackers:
            data_dir = _event_manager.get_event_data_dir(eid)
            _event_trackers[eid] = EventTracker(eid, data_dir, event)
        return _event_trackers[eid]


def load_user_overrides(users_file: Path) -> dict[str, dict]:
    """Load user overrides from JSON file."""
    if users_file and users_file.exists():
        try:
            with open(users_file, 'r') as f:
                data = json.load(f)
                return data.get('users', {})
        except Exception as e:
            log(f"Warning: Could not load users file: {e}")
    return {}


def save_user_overrides(users_file: Path, overrides: dict[str, dict]):
    """Save user overrides to JSON file."""
    if not users_file:
        return
    output = {
        "updated": time.time(),
        "updated_iso": datetime.now().isoformat(),
        "users": overrides
    }
    tmp_file = users_file.with_suffix('.tmp')
    with open(tmp_file, 'w') as f:
        json.dump(output, f, indent=2)
    tmp_file.rename(users_file)
    log(f"[ADMIN] Saved user overrides: {len(overrides)} users")


def load_races(data_dir: Path) -> tuple[int, list]:
    """Load races from results.jsonl. Returns (next_id, races_list)."""
    races_file = data_dir / "results.jsonl"
    if not races_file.exists():
        return 1, []
    try:
        with open(races_file, 'r') as f:
            lines = f.read().strip().split('\n')
        if not lines or not lines[0]:
            return 1, []
        header = json.loads(lines[0])
        next_id = header.get('next_id', 1)
        races = []
        for line in lines[1:]:
            if line.strip():
                races.append(json.loads(line))
        return next_id, races
    except Exception as e:
        log(f"Error loading races from {races_file}: {e}")
        return 1, []


def save_races(data_dir: Path, next_id: int, races: list):
    """Save races to results.jsonl atomically."""
    races_file = data_dir / "results.jsonl"
    tmp_file = races_file.with_suffix('.tmp')
    with open(tmp_file, 'w') as f:
        f.write(json.dumps({"next_id": next_id}) + '\n')
        for race in races:
            f.write(json.dumps(race) + '\n')
    tmp_file.rename(races_file)


def get_user_override(user_overrides: dict, sailor_id: str, did: str | None = None) -> dict | None:
    """Look up user override: check did:XXX first, then sailor_id."""
    if not user_overrides:
        return None
    if did:
        override = user_overrides.get(f"did:{did}")
        if override:
            return override
    return user_overrides.get(sailor_id)


def _resolve_did_overrides(user_overrides: dict, current_positions: dict) -> dict:
    """Resolve did:XXX keys to current sailor_ids for the API response.

    The WebUI always works with sailor_ids, so we translate did:XXX entries
    back to their current sailor_id using the live positions map.
    """
    if not user_overrides:
        return {}
    # Build reverse map: did -> current sailor_id
    did_to_id = {}
    for sid, pos in current_positions.items():
        did = pos.get("did")
        if did:
            did_to_id[did] = sid

    resolved = {}
    for key, override in user_overrides.items():
        if key.startswith("did:"):
            did = key[4:]
            sailor_id = did_to_id.get(did) or override.get("_last_id")
            if sailor_id:
                # Strip internal _last_id from the response
                clean = {k: v for k, v in override.items() if k != "_last_id"}
                resolved[sailor_id] = clean
            # else: orphaned did entry, skip
        else:
            resolved[key] = override
    return resolved


_static_dir: Path | None = None


def parse_gpx_to_entries(gpx_content: str, name: str) -> list[dict]:
    """Parse GPX XML content into JSONL track entries.

    Returns a list of entry dicts sorted by timestamp.
    """
    import xml.etree.ElementTree as ET

    entries = []
    root = ET.fromstring(gpx_content)

    # Handle GPX namespace (1.0 and 1.1)
    ns = ''
    if root.tag.startswith('{'):
        ns = root.tag.split('}')[0] + '}'

    sailor_id = f"{name}(GPX)"

    for trk in root.iter(f'{ns}trk'):
        for trkseg in trk.iter(f'{ns}trkseg'):
            for trkpt in trkseg.iter(f'{ns}trkpt'):
                lat = float(trkpt.get('lat'))
                lon = float(trkpt.get('lon'))

                # Parse time
                time_el = trkpt.find(f'{ns}time')
                if time_el is None or not time_el.text:
                    continue
                time_str = time_el.text.strip()
                # Handle Z suffix for UTC
                if time_str.endswith('Z'):
                    time_str = time_str[:-1] + '+00:00'
                dt = datetime.fromisoformat(time_str)
                unix_ts = int(dt.timestamp())

                # Parse optional speed (m/s -> knots)
                speed = 0.0
                speed_el = trkpt.find(f'{ns}speed')
                if speed_el is not None and speed_el.text:
                    speed = float(speed_el.text) / 0.514444

                # Parse optional course/heading
                heading = 0
                course_el = trkpt.find(f'{ns}course')
                if course_el is not None and course_el.text:
                    heading = int(float(course_el.text))

                entries.append({
                    "id": sailor_id,
                    "ts": unix_ts,
                    "recv_ts": unix_ts,
                    "lat": round(lat, 6),
                    "lon": round(lon, 6),
                    "spd": round(speed, 1),
                    "hdg": heading,
                    "ast": False,
                    "bat": -1,
                    "sig": -1,
                    "role": "sailor",
                    "ver": "gpx-upload",
                    "displayid": name,
                    "src": "gpx"
                })

    entries.sort(key=lambda e: e['ts'])
    return entries


def parse_fit_to_entries(fit_content: bytes, name: str) -> list[dict]:
    """Parse FIT binary content into JSONL track entries.

    Returns a list of entry dicts sorted by timestamp.
    Requires the fitparse library.
    """
    import fitparse

    entries = []
    fitfile = fitparse.FitFile(fit_content)
    sailor_id = f"{name}(FIT)"

    # Semicircles to degrees: degrees = semicircles * (180 / 2^31)
    SEMI_TO_DEG = 180.0 / (2 ** 31)

    for record in fitfile.get_messages('record'):
        lat_semi = record.get_value('position_lat')
        lon_semi = record.get_value('position_long')
        ts = record.get_value('timestamp')

        if lat_semi is None or lon_semi is None or ts is None:
            continue

        lat = lat_semi * SEMI_TO_DEG
        lon = lon_semi * SEMI_TO_DEG

        # FIT timestamps are naive UTC datetimes
        unix_ts = int(ts.replace(tzinfo=timezone.utc).timestamp())

        # Speed: m/s -> knots
        speed = 0.0
        spd_val = record.get_value('enhanced_speed') or record.get_value('speed')
        if spd_val is not None:
            speed = spd_val / 0.514444

        entry = {
            "id": sailor_id,
            "ts": unix_ts,
            "recv_ts": unix_ts,
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "spd": round(speed, 1),
            "hdg": 0,
            "ast": False,
            "bat": -1,
            "sig": -1,
            "role": "sailor",
            "ver": "fit-upload",
            "displayid": name,
            "src": "fit"
        }

        hr = record.get_value('heart_rate')
        if hr is not None and hr > 0:
            entry["hr"] = int(hr)

        entries.append(entry)

    entries.sort(key=lambda e: e['ts'])
    return entries


class AdminHTTPHandler(BaseHTTPRequestHandler):
    """HTTP handler for admin API endpoints and optional static file serving."""
    
    def log_message(self, format, *args):
        """Override to prefix with [HTTP]"""
        log(f"[HTTP] {args[0]}")
    
    def _send_json(self, data: dict | list, status: int = 200):
        """Send JSON response."""
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'X-Admin-Password, X-Manager-Password, Content-Type')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PATCH, DELETE, OPTIONS')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode('utf-8'))
    
    def _send_file(self, filepath: Path, content_type: str):
        """Send a static file with Last-Modified header and If-Modified-Since support."""
        try:
            stat_info = filepath.stat()
            last_modified = email.utils.formatdate(stat_info.st_mtime, usegmt=True)

            # Check If-Modified-Since header for conditional GET
            ims = self.headers.get('If-Modified-Since')
            if ims:
                try:
                    ims_time = email.utils.parsedate_to_datetime(ims)
                    file_time = datetime.fromtimestamp(stat_info.st_mtime, tz=timezone.utc)
                    if file_time <= ims_time:
                        self.send_response(304)
                        self.end_headers()
                        return
                except (ValueError, TypeError):
                    pass  # Invalid date format, proceed with full response

            with open(filepath, 'rb') as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', len(content))
            self.send_header('Last-Modified', last_modified)
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self._send_json({"error": "Not found"}, 404)
    
    def _get_client_ip(self) -> str:
        """Get client IP address, preferring X-Forwarded-For for proxied requests."""
        return self.headers.get('X-Forwarded-For', self.client_address[0])

    def _check_manager_auth(self) -> bool:
        """Check manager password from header with rate limiting."""
        client_ip = self._get_client_ip()

        if is_rate_limited(client_ip):
            log(f"[HTTP] Manager auth rate-limited for {client_ip}")
            return False

        password = self.headers.get('X-Manager-Password', '')
        if password != _event_manager.manager_password:
            record_failed_auth(client_ip)
            log(f"[HTTP] Manager auth failed from {client_ip}")
            return False
        return True

    def _check_event_admin_auth(self, eid: int) -> bool:
        """Check per-event admin password from header with rate limiting."""
        client_ip = self._get_client_ip()

        if is_rate_limited(client_ip):
            log(f"[HTTP] Event {eid} admin auth rate-limited for {client_ip}")
            return False

        event = _event_manager.get_event(eid)
        if not event:
            log(f"[HTTP] Event {eid} not found")
            return False

        password = self.headers.get('X-Admin-Password', '')
        if password != event.get('admin_password', ''):
            record_failed_auth(client_ip)
            log(f"[HTTP] Event {eid} admin auth failed from {client_ip}")
            return False
        return True

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'X-Admin-Password, X-Manager-Password, Content-Type')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PATCH, DELETE, OPTIONS')
        self.end_headers()
    
    def do_GET(self):
        """Handle GET requests."""
        path = urlparse(self.path).path
        
        if path == '/api/events':
            # Return list of active events (public endpoint)
            self._send_json({"events": _event_manager.get_public_events()})

        elif path == '/api/manage/events':
            # Return full event list with details (manager only)
            if not self._check_manager_auth():
                self._send_json({"error": "Unauthorized"}, 401)
                return
            self._send_json({"events": _event_manager.get_all_events()})

        elif path.startswith('/api/event/'):
            # Per-event API endpoints
            self._handle_event_get(path)
            return

        elif _static_dir:
            # Serve static files
            if path == '/' or path == '':
                path = '/index.html'
            
            # Security: prevent directory traversal
            try:
                filepath = (_static_dir / path.lstrip('/')).resolve()
                if not str(filepath).startswith(str(_static_dir.resolve())):
                    self._send_json({"error": "Forbidden"}, 403)
                    return
            except Exception:
                self._send_json({"error": "Bad request"}, 400)
                return
            
            if filepath.exists() and filepath.is_file():
                # Determine content type
                ext = filepath.suffix.lower()
                content_types = {
                    '.html': 'text/html',
                    '.css': 'text/css',
                    '.js': 'application/javascript',
                    '.json': 'application/json',
                    '.jsonl': 'application/jsonlines',
                    '.gz': 'application/gzip',
                    '.png': 'image/png',
                    '.jpg': 'image/jpeg',
                    '.jpeg': 'image/jpeg',
                    '.svg': 'image/svg+xml',
                    '.ico': 'image/x-icon',
                    '.plist': 'text/xml',
                    '.ipa': 'application/octet-stream',
                }
                content_type = content_types.get(ext, 'application/octet-stream')
                self._send_file(filepath, content_type)
            else:
                self._send_json({"error": "Not found"}, 404)
        else:
            self._send_json({"error": "Not found"}, 404)

    def _parse_event_path(self, path: str) -> tuple[int | None, str]:
        """Parse /api/event/{eid}/... path. Returns (eid, remaining_path) or (None, '') on error."""
        # Pattern: /api/event/{eid}/...
        match = re.match(r'^/api/event/(\d+)(/.*)?$', path)
        if not match:
            return None, ''
        eid = int(match.group(1))
        remaining = match.group(2) or ''
        return eid, remaining

    def _handle_event_get(self, path: str):
        """Handle GET requests for per-event endpoints."""
        eid, subpath = self._parse_event_path(path)
        if eid is None:
            self._send_json({"error": "Invalid event path"}, 400)
            return

        # Check if event exists
        event = _event_manager.get_event(eid)
        if not event:
            self._send_json({"error": f"Event {eid} not found"}, 404)
            return

        if subpath == '/course':
            # Return course for this event (public)
            tracker = get_event_tracker(eid)
            if tracker and tracker.course_file.exists():
                try:
                    with open(tracker.course_file, 'r') as f:
                        course = json.load(f)
                    self._send_json(course)
                except Exception as e:
                    self._send_json({"error": str(e)}, 500)
            else:
                self._send_json({"course": None})

        elif subpath == '/auth/check':
            # Check admin password for this event
            if self._check_event_admin_auth(eid):
                self._send_json({"authenticated": True})
            else:
                self._send_json({"authenticated": False}, 401)

        elif subpath == '/users':
            # Return user overrides for this event (admin only)
            if not self._check_event_admin_auth(eid):
                self._send_json({"error": "Unauthorized"}, 401)
                return
            tracker = get_event_tracker(eid)
            if tracker:
                resolved = _resolve_did_overrides(tracker.user_overrides,
                                                  tracker.position_tracker.current_positions)
                self._send_json({"users": resolved})
            else:
                self._send_json({"users": {}})

        elif subpath == '/search':
            # Search across all summary files for this event (public)
            parsed = urlparse(self.path)
            query_params = parse_qs(parsed.query)
            query = query_params.get('q', [''])[0].strip().lower()

            if not query or len(query) < 2:
                self._send_json({"error": "Query parameter 'q' required (min 2 chars)"}, 400)
                return

            tracker = get_event_tracker(eid)
            if not tracker:
                self._send_json({"error": f"Event {eid} not found"}, 404)
                return

            # Load user overrides for display names
            user_overrides = tracker.user_overrides or {}

            results = []
            # Scan all summary files
            if tracker.log_dir.exists():
                for summary_file in sorted(tracker.log_dir.glob('*_summary.json'), reverse=True):
                    try:
                        with open(summary_file, 'r') as f:
                            summary = json.load(f)
                        date = summary.get('date', summary_file.stem.replace('_summary', ''))

                        for log in summary.get('logs', []):
                            for key, sailor_data in log.get('sailors', {}).items():
                                # Get the original tracker ID and displayid from summary
                                # In new format: sailor_data has 'id' (original tracker ID) and optionally 'displayid'
                                # In old format: key is the sailor_id
                                original_id = sailor_data.get('id', key)
                                displayid = sailor_data.get('displayid', '')

                                # Get display name from user overrides (fallback for old summaries)
                                search_override = get_user_override(user_overrides, original_id)
                                override_name = search_override.get('name', '') if search_override else ''

                                # The display name is: displayid (from log) > override name > key
                                display_name = displayid or override_name or key

                                # Match against key (which may be displayid or id), original id, and display name
                                if (query in key.lower() or
                                    query in original_id.lower() or
                                    query in display_name.lower()):
                                    start_ts = sailor_data.get('first_ts', log.get('start_ts', 0))
                                    end_ts = sailor_data.get('last_ts', log.get('end_ts', 0))
                                    results.append({
                                        'date': date,
                                        'log_file': log.get('file', ''),
                                        'sailor_id': original_id,  # Original tracker ID
                                        'key': key,  # The key used in userData (displayid or original id)
                                        'name': display_name,
                                        'start_ts': start_ts,
                                        'end_ts': end_ts,
                                        'duration_secs': end_ts - start_ts if end_ts > start_ts else 0
                                    })
                    except Exception as e:
                        logging.warning(f"Error reading summary {summary_file}: {e}")
                        continue

            self._send_json({"results": results})

        elif subpath == '/info':
            # Return custom event info (or default from events.json)
            tracker = get_event_tracker(eid)
            info_file = tracker.data_dir / 'info.json' if tracker else None

            # Try to load custom info.json
            if info_file and info_file.exists():
                try:
                    with open(info_file, 'r') as f:
                        data = json.load(f)
                    self._send_json({"info": data.get('info', ''), "source": "custom"})
                    return
                except Exception as e:
                    logging.warning(f"Error reading info.json for event {eid}: {e}")

            # Fall back to event description from events.json
            if _event_manager:
                event = _event_manager.get_event(eid)
                if event and event.get('description'):
                    self._send_json({"info": event['description'], "source": "default"})
                    return

            self._send_json({"info": "", "source": "default"})

        elif subpath.startswith('/admin/gt06-cmd/'):
            # Send an arbitrary command to a GT06 device (for testing)
            # URL: /api/event/{eid}/admin/gt06-cmd/{user_id}?cmd=FIND%23
            if not self._check_event_admin_auth(eid):
                self._send_json({"error": "Unauthorized"}, 401)
                return
            from urllib.parse import unquote
            user_id = unquote(subpath[len('/admin/gt06-cmd/'):])
            if not user_id:
                self._send_json({"error": "User ID required"}, 400)
                return
            params = parse_qs(urlparse(self.path).query)
            cmd_str = params.get("cmd", [None])[0]
            if not cmd_str:
                self._send_json({"error": "cmd parameter required"}, 400)
                return
            if _gt06_listener:
                sent = _gt06_listener.send_command_to(user_id, cmd_str)
                if sent:
                    self._send_json({"success": True, "user_id": user_id, "cmd": cmd_str})
                else:
                    self._send_json({"error": f"GT06 device {user_id} not connected"}, 404)
            else:
                self._send_json({"error": "GT06 listener not running"}, 404)

        elif subpath == '/races':
            # Return all races for this event (public)
            tracker = get_event_tracker(eid)
            if tracker:
                next_id, races = load_races(tracker.data_dir)
                self._send_json({"races": races})
            else:
                self._send_json({"races": []})

        else:
            self._send_json({"error": "Not found"}, 404)

    @staticmethod
    def _compress_log_files(log_dir: Path, filenames: list[str]):
        """Compress JSONL log files to .gz for efficient serving.

        The .gz files keep their natural mtime (time of compression) so
        browser If-Modified-Since caching detects new uploads correctly.
        """
        import gzip
        for fname in filenames:
            log_file = log_dir / fname
            if not log_file.exists():
                continue
            gz_file = log_file.with_suffix('.jsonl.gz')
            tmp_gz = gz_file.parent / f"{gz_file.name}.tmp"
            with open(log_file, 'rb') as f_in:
                with gzip.open(tmp_gz, 'wb') as f_out:
                    f_out.write(f_in.read())
            tmp_gz.rename(gz_file)

    def _handle_track_upload(self, eid: int, event: dict):
        """Handle GPX track file upload with tracker password auth."""
        from collections import defaultdict

        client_ip = self._get_client_ip()

        # Rate limiting
        if is_rate_limited(client_ip):
            self._send_json({"error": "Too many attempts, please wait"}, 429)
            return

        # Parse multipart form data
        content_type = self.headers.get('Content-Type', '')
        if 'multipart/form-data' not in content_type:
            self._send_json({"error": "Expected multipart/form-data"}, 400)
            return

        # Extract boundary
        boundary = None
        for part in content_type.split(';'):
            part = part.strip()
            if part.startswith('boundary='):
                boundary = part[len('boundary='):]
                break
        if not boundary:
            self._send_json({"error": "Missing boundary in Content-Type"}, 400)
            return

        content_length = int(self.headers.get('Content-Length', 0))
        if content_length > 50 * 1024 * 1024:  # 50MB limit
            self._send_json({"error": "File too large (max 50MB)"}, 413)
            return
        body = self.rfile.read(content_length)

        # Parse multipart parts manually
        boundary_bytes = boundary.encode('utf-8')
        parts = body.split(b'--' + boundary_bytes)
        fields = {}  # name -> value (str)
        file_content = None
        file_name = ''

        for part in parts:
            if part in (b'', b'--\r\n', b'--'):
                continue
            # Split headers from body
            if b'\r\n\r\n' not in part:
                continue
            header_section, part_body = part.split(b'\r\n\r\n', 1)
            # Strip trailing \r\n
            if part_body.endswith(b'\r\n'):
                part_body = part_body[:-2]

            headers_str = header_section.decode('utf-8', errors='replace')
            # Extract field name and filename from Content-Disposition
            field_name = None
            part_filename = ''
            for line in headers_str.split('\r\n'):
                if line.lower().startswith('content-disposition:'):
                    for item in line.split(';'):
                        item = item.strip()
                        if item.startswith('name="') and item.endswith('"'):
                            field_name = item[6:-1]
                        if item.startswith('filename="') and item.endswith('"'):
                            part_filename = item[10:-1]

            if field_name == 'file' and part_filename:
                file_content = part_body
                file_name = part_filename
            elif field_name:
                fields[field_name] = part_body.decode('utf-8', errors='replace')

        # Validate required fields
        name = fields.get('name', '').strip()
        password = fields.get('password', '')

        if not name:
            self._send_json({"error": "Display name is required"}, 400)
            return
        if file_content is None:
            self._send_json({"error": "No file uploaded"}, 400)
            return

        # Authenticate with tracker password
        event_tracker_pwds = event.get('tracker_password', []) if event else []
        if event_tracker_pwds:
            if password not in event_tracker_pwds:
                record_failed_auth(client_ip)
                log(f"[EVENT {eid}] Upload auth failed from {client_ip}")
                self._send_json({"error": "Invalid event password"}, 401)
                return

        # Parse track file based on extension
        ext = file_name.rsplit('.', 1)[-1].lower() if '.' in file_name else ''
        try:
            if ext == 'gpx':
                entries = parse_gpx_to_entries(file_content.decode('utf-8'), name)
            elif ext == 'fit':
                entries = parse_fit_to_entries(file_content, name)
            else:
                self._send_json({"error": f"Unsupported file type: .{ext} (use .gpx or .fit)"}, 400)
                return
        except Exception as e:
            log(f"[EVENT {eid}] {ext.upper()} parse error: {e}")
            self._send_json({"error": f"Failed to parse {ext.upper()} file: {e}"}, 400)
            return

        if not entries:
            self._send_json({"error": f"No trackpoints found in {ext.upper()} file"}, 400)
            return

        # Get event tracker
        tracker = get_event_tracker(eid)
        if not tracker:
            self._send_json({"error": "Could not get event tracker"}, 500)
            return

        if not tracker.daily_logger:
            self._send_json({"error": "Track logging not enabled"}, 500)
            return

        # Group entries by local date in the event timezone
        event_tz = ZoneInfo(event.get('timezone', 'Australia/Sydney') if event else 'Australia/Sydney')
        date_groups = defaultdict(list)
        for entry in entries:
            local_dt = datetime.fromtimestamp(entry['ts'], tz=event_tz)
            date_str = local_dt.strftime('%Y_%m_%d')
            date_groups[date_str].append(entry)

        # Merge each day's entries via DailyLogger (holds _write_lock to
        # prevent data loss from concurrent appends during read-modify-write)
        merged_files = []
        errors = []
        for date_str, day_entries in date_groups.items():
            try:
                tracker.daily_logger.merge_entries(date_str, day_entries)
                merged_files.append(f"{date_str}.jsonl")
            except Exception as e:
                log(f"[EVENT {eid}] Merge failed for {date_str}: {e}")
                errors.append(f"{date_str}.jsonl: {e}")

        # Regenerate summaries and .gz files for modified log files.
        # Delete stale summaries first so generate_log_summaries() doesn't
        # skip them due to mtime-based caching.
        if merged_files:
            for fname in merged_files:
                date_str = fname.replace('.jsonl', '')
                for suffix in ('_summary.json',):
                    stale = tracker.log_dir / f"{date_str}{suffix}"
                    if stale.exists():
                        stale.unlink()
            try:
                generate_log_summaries(tracker.log_dir)
            except Exception as e:
                log(f"[EVENT {eid}] Summary regeneration failed: {e}")
            try:
                self._compress_log_files(tracker.log_dir, merged_files)
            except Exception as e:
                log(f"[EVENT {eid}] Log compression failed: {e}")

        if errors:
            log(f"[EVENT {eid}] Upload partial failure: {errors}")
            self._send_json({
                "error": f"Some files failed: {'; '.join(errors)}",
                "success": len(merged_files) > 0,
                "points": len(entries),
                "files": merged_files
            }, 207)  # Multi-Status
        else:
            log(f"[EVENT {eid}] Uploaded {len(entries)} {ext.upper()} points for '{name}' to {merged_files}")
            self._send_json({
                "success": True,
                "points": len(entries),
                "files": sorted(merged_files)
            })

    def _handle_event_post(self, path: str):
        """Handle POST requests for per-event endpoints."""
        eid, subpath = self._parse_event_path(path)
        if eid is None:
            self._send_json({"error": "Invalid event path"}, 400)
            return

        # Check if event exists
        if _event_manager:
            event = _event_manager.get_event(eid)
            if not event:
                self._send_json({"error": f"Event {eid} not found"}, 404)
                return
            if event.get('archived'):
                self._send_json({"error": f"Event {eid} is archived"}, 400)
                return

        # Upload track endpoint - uses tracker password, not admin password
        if subpath == '/upload-track':
            self._handle_track_upload(eid, event)
            return

        # Admin endpoints require per-event admin auth
        if not self._check_event_admin_auth(eid):
            self._send_json({"error": "Unauthorized"}, 401)
            return

        if subpath == '/admin/clear-tracks':
            tracker = get_event_tracker(eid)
            if tracker:
                tracker.clear_tracks()
                self._send_json({"success": True, "message": f"Event {eid} tracks cleared"})
            else:
                self._send_json({"error": "Could not get event tracker"}, 500)

        elif subpath == '/admin/course':
            # Save course for this event
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                course = json.loads(body)
                course['updated'] = time.time()
                course['updated_iso'] = datetime.now().isoformat()

                tracker = get_event_tracker(eid)
                if tracker:
                    # Rotate existing course before saving new one
                    if tracker.course_file.exists():
                        rotate_file(tracker.course_file)
                    tmp_file = tracker.course_file.with_suffix('.tmp')
                    with open(tmp_file, 'w') as f:
                        json.dump(course, f, indent=2)
                    tmp_file.rename(tracker.course_file)
                    log(f"[EVENT {eid}] Course saved: {len(course.get('marks', []))} marks")
                    self._send_json({"success": True})
                else:
                    self._send_json({"error": "Could not get event tracker"}, 500)

            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON"}, 400)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif subpath == '/admin/info':
            # Save custom event info
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                data = json.loads(body)

                tracker = get_event_tracker(eid)
                if not tracker:
                    self._send_json({"error": "Could not get event tracker"}, 500)
                    return

                info_data = {
                    'info': str(data.get('info', '')),
                    'updated': time.time(),
                    'updated_iso': datetime.now().isoformat()
                }

                info_file = tracker.data_dir / 'info.json'
                tmp_file = info_file.with_suffix('.tmp')
                with open(tmp_file, 'w') as f:
                    json.dump(info_data, f, indent=2)
                tmp_file.rename(info_file)

                log(f"[EVENT {eid}] Event info saved")
                self._send_json({"success": True})

            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON"}, 400)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif subpath.startswith('/admin/user/'):
            # Create or update a user override for this event
            from urllib.parse import unquote
            user_id = unquote(subpath[len('/admin/user/'):])
            if not user_id:
                self._send_json({"error": "User ID required"}, 400)
                return

            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                data = json.loads(body)

                tracker = get_event_tracker(eid)
                if not tracker:
                    self._send_json({"error": "Could not get event tracker"}, 500)
                    return

                override = {}
                if 'name' in data:
                    override['name'] = str(data['name'])
                if 'role' in data and data['role'] in ('sailor', 'support', 'spectator'):
                    override['role'] = data['role']
                if 'hidden' in data:
                    override['hidden'] = bool(data['hidden'])
                if 'info' in data:
                    override['info'] = str(data['info'])

                if override:
                    # Prefer did:XXX key if device has a did
                    store_key = user_id
                    pos = tracker.position_tracker.current_positions.get(user_id)
                    if pos and pos.get("did"):
                        store_key = f"did:{pos['did']}"
                        override["_last_id"] = user_id
                        # Remove old sailor_id entry if migrating to did key
                        if user_id in tracker.user_overrides and store_key != user_id:
                            del tracker.user_overrides[user_id]
                    tracker.user_overrides[store_key] = override
                    save_user_overrides(tracker.users_file, tracker.user_overrides)
                    # Refresh positions file
                    write_current_positions(
                        tracker.position_tracker.current_positions,
                        tracker.positions_file,
                        tracker.user_overrides,
                        tracker.position_tracker.position_tails
                    )
                    log(f"[EVENT {eid}] User override set for {user_id} (key={store_key}): {override}")
                    self._send_json({"success": True, "user_id": user_id, "override": override})
                else:
                    self._send_json({"error": "No valid fields (name, role, info)"}, 400)

            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON"}, 400)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif subpath == '/admin/stop-all':
            # Send remote stop command to all active trackers
            tracker = get_event_tracker(eid)
            if not tracker:
                self._send_json({"error": f"Event {eid} not found"}, 404)
                return

            stopped_ids = []
            for user_id, pos in tracker.position_tracker.current_positions.items():
                if not pos.get("stopped", False):
                    queue_pending_command(f"{eid}:{user_id}", "stop")
                    send_proactive_command(f"{eid}:{user_id}", "stop")
                    if _gt06_listener:
                        _gt06_listener.set_idle(user_id, True)
                    stopped_ids.append(user_id)

            log(f"[EVENT {eid}] Remote stop-all queued for {len(stopped_ids)} trackers: {stopped_ids}")
            self._send_json({"success": True, "stopped_count": len(stopped_ids), "user_ids": stopped_ids})

        elif subpath.startswith('/admin/stop/'):
            # Send remote stop command to a user
            from urllib.parse import unquote
            user_id = unquote(subpath[len('/admin/stop/'):])
            if not user_id:
                self._send_json({"error": "User ID required"}, 400)
                return

            queue_pending_command(f"{eid}:{user_id}", "stop")
            send_proactive_command(f"{eid}:{user_id}", "stop")
            if _gt06_listener:
                _gt06_listener.set_idle(user_id, True)
            log(f"[EVENT {eid}] Remote stop queued for {user_id}")
            self._send_json({"success": True, "user_id": user_id, "event_id": eid})

        elif subpath.startswith('/admin/cancel-assist/'):
            # Send remote cancel assist command to a user
            from urllib.parse import unquote
            user_id = unquote(subpath[len('/admin/cancel-assist/'):])
            if not user_id:
                self._send_json({"error": "User ID required"}, 400)
                return

            queue_pending_command(f"{eid}:{user_id}", "cancel_assist")
            send_proactive_command(f"{eid}:{user_id}", "cancel_assist")
            # For GT06 devices, clear assist state and try to silence buzzer
            if _gt06_listener:
                _gt06_listener.cancel_assist(user_id)
            log(f"[EVENT {eid}] Remote cancel assist queued for {user_id}")
            self._send_json({"success": True, "user_id": user_id, "event_id": eid})

        elif subpath == '/admin/start-all':
            # Send remote start command to all idle trackers
            tracker = get_event_tracker(eid)
            if not tracker:
                self._send_json({"error": f"Event {eid} not found"}, 404)
                return

            started_ids = []
            for user_id, pos in tracker.position_tracker.current_positions.items():
                if pos.get("idle", False):
                    queue_pending_command(f"{eid}:{user_id}", "start")
                    send_proactive_command(f"{eid}:{user_id}", "start")
                    if _gt06_listener:
                        _gt06_listener.set_idle(user_id, False)
                    started_ids.append(user_id)

            log(f"[EVENT {eid}] Remote start-all queued for {len(started_ids)} idle trackers: {started_ids}")
            self._send_json({"success": True, "started_count": len(started_ids), "user_ids": started_ids})

        elif subpath == '/admin/shutdown-all':
            # Send remote shutdown command to all idle trackers
            tracker = get_event_tracker(eid)
            if not tracker:
                self._send_json({"error": f"Event {eid} not found"}, 404)
                return

            shutdown_ids = []
            for user_id, pos in tracker.position_tracker.current_positions.items():
                if pos.get("idle", False):
                    queue_pending_command(f"{eid}:{user_id}", "shutdown")
                    send_proactive_command(f"{eid}:{user_id}", "shutdown")
                    shutdown_ids.append(user_id)

            log(f"[EVENT {eid}] Remote shutdown-all queued for {len(shutdown_ids)} idle trackers: {shutdown_ids}")
            self._send_json({"success": True, "shutdown_count": len(shutdown_ids), "user_ids": shutdown_ids})

        elif subpath.startswith('/admin/start/'):
            # Send remote start command to a single idle user
            from urllib.parse import unquote
            user_id = unquote(subpath[len('/admin/start/'):])
            if not user_id:
                self._send_json({"error": "User ID required"}, 400)
                return

            queue_pending_command(f"{eid}:{user_id}", "start")
            send_proactive_command(f"{eid}:{user_id}", "start")
            if _gt06_listener:
                _gt06_listener.set_idle(user_id, False)
            log(f"[EVENT {eid}] Remote start queued for {user_id}")
            self._send_json({"success": True, "user_id": user_id, "event_id": eid})

        elif subpath.startswith('/admin/shutdown/'):
            # Send remote shutdown command to a single user
            from urllib.parse import unquote
            user_id = unquote(subpath[len('/admin/shutdown/'):])
            if not user_id:
                self._send_json({"error": "User ID required"}, 400)
                return

            queue_pending_command(f"{eid}:{user_id}", "shutdown")
            send_proactive_command(f"{eid}:{user_id}", "shutdown")
            log(f"[EVENT {eid}] Remote shutdown queued for {user_id}")
            self._send_json({"success": True, "user_id": user_id, "event_id": eid})

        elif subpath.startswith('/log/') and '/sublog' in subpath:
            # Add a sublog (race marker) to a log file's summary
            # URL format: /log/{log_file}/sublog
            parts = subpath[5:].split('/sublog')
            if len(parts) != 2 or parts[1] != '':
                self._send_json({"error": "Invalid sublog path"}, 400)
                return
            log_file = parts[0]
            if not log_file:
                self._send_json({"error": "Log file required"}, 400)
                return

            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                data = json.loads(body)

                # Validate required fields
                if 'name' not in data or 'start_ts' not in data or 'end_ts' not in data:
                    self._send_json({"error": "name, start_ts, and end_ts required"}, 400)
                    return

                name = str(data['name']).strip()
                start_ts = int(data['start_ts'])
                end_ts = int(data['end_ts'])

                if not name:
                    self._send_json({"error": "name cannot be empty"}, 400)
                    return
                if start_ts >= end_ts:
                    self._send_json({"error": "start_ts must be before end_ts"}, 400)
                    return

                tracker = get_event_tracker(eid)
                if not tracker:
                    self._send_json({"error": "Could not get event tracker"}, 500)
                    return

                # Find the summary file for this log
                # Extract date from log file name (e.g., 2025_01_15.jsonl -> 2025_01_15_summary.json)
                date_match = re.match(r'^(\d{4}_\d{2}_\d{2})\.jsonl', log_file)
                if not date_match:
                    self._send_json({"error": "Invalid log file format"}, 400)
                    return
                date_str = date_match.group(1)
                summary_file = tracker.log_dir / f"{date_str}_summary.json"

                if not summary_file.exists():
                    self._send_json({"error": "Summary file not found"}, 404)
                    return

                # Load summary, add sublog, save
                with open(summary_file, 'r') as f:
                    summary = json.load(f)

                # Find the log entry
                log_entry = None
                for entry in summary.get('logs', []):
                    if entry.get('file') == log_file:
                        log_entry = entry
                        break

                if not log_entry:
                    self._send_json({"error": f"Log {log_file} not found in summary"}, 404)
                    return

                # Add sublog
                if 'sublogs' not in log_entry:
                    log_entry['sublogs'] = []

                log_entry['sublogs'].append({
                    'name': name,
                    'start_ts': start_ts,
                    'end_ts': end_ts
                })

                # Sort sublogs by start time
                log_entry['sublogs'].sort(key=lambda x: x.get('start_ts', 0))

                # Save summary atomically
                tmp_file = summary_file.with_suffix('.tmp')
                with open(tmp_file, 'w') as f:
                    json.dump(summary, f, indent=2)
                tmp_file.rename(summary_file)

                log(f"[EVENT {eid}] Added sublog '{name}' to {log_file}")
                self._send_json(summary)

            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON"}, 400)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif subpath == '/admin/races':
            # Create a new race
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                data = json.loads(body)
                name = str(data.get('name', '')).strip()
                if not name:
                    self._send_json({"error": "Race name required"}, 400)
                    return

                tracker = get_event_tracker(eid)
                if not tracker:
                    self._send_json({"error": "Could not get event tracker"}, 500)
                    return

                next_id, races = load_races(tracker.data_dir)
                race = {"id": next_id, "name": name, "start_ts": None, "end_ts": None, "finishers": []}
                races.append(race)
                save_races(tracker.data_dir, next_id + 1, races)
                log(f"[EVENT {eid}] Race created: {name} (id={next_id})")
                self._send_json(race)

            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON"}, 400)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif re.match(r'^/admin/races/\d+/start$', subpath):
            # Set race start time
            race_id = int(subpath.split('/')[3])
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                data = json.loads(body)
                start_ts = data.get('start_ts')
                if start_ts is not None:
                    start_ts = float(start_ts)

                tracker = get_event_tracker(eid)
                if not tracker:
                    self._send_json({"error": "Could not get event tracker"}, 500)
                    return

                next_id, races = load_races(tracker.data_dir)
                race = next((r for r in races if r['id'] == race_id), None)
                if not race:
                    self._send_json({"error": f"Race {race_id} not found"}, 404)
                    return

                race['start_ts'] = start_ts
                # Clearing start time also clears all results
                if start_ts is None:
                    race['finishers'] = []
                    race['end_ts'] = None
                save_races(tracker.data_dir, next_id, races)
                log(f"[EVENT {eid}] Race {race_id} start set to {start_ts}")
                self._send_json(race)

            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON"}, 400)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif re.match(r'^/admin/races/\d+/end$', subpath):
            # Set race end time
            race_id = int(subpath.split('/')[3])
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                data = json.loads(body)
                end_ts = data.get('end_ts')
                if end_ts is not None:
                    end_ts = float(end_ts)

                tracker = get_event_tracker(eid)
                if not tracker:
                    self._send_json({"error": "Could not get event tracker"}, 500)
                    return

                next_id, races = load_races(tracker.data_dir)
                race = next((r for r in races if r['id'] == race_id), None)
                if not race:
                    self._send_json({"error": f"Race {race_id} not found"}, 404)
                    return

                race['end_ts'] = end_ts
                save_races(tracker.data_dir, next_id, races)
                log(f"[EVENT {eid}] Race {race_id} end set to {end_ts}")
                self._send_json(race)

            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON"}, 400)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif re.match(r'^/admin/races/\d+/finish$', subpath):
            # Record a finish
            race_id = int(subpath.split('/')[3])
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                data = json.loads(body)
                sailor_id = str(data.get('sailor_id', '')).strip()
                finish_ts = float(data.get('finish_ts', 0))
                if not sailor_id:
                    self._send_json({"error": "sailor_id required"}, 400)
                    return

                tracker = get_event_tracker(eid)
                if not tracker:
                    self._send_json({"error": "Could not get event tracker"}, 500)
                    return

                next_id, races = load_races(tracker.data_dir)
                race = next((r for r in races if r['id'] == race_id), None)
                if not race:
                    self._send_json({"error": f"Race {race_id} not found"}, 404)
                    return

                # Check for duplicate
                if any(f['sailor_id'] == sailor_id for f in race['finishers']):
                    self._send_json({"error": f"Sailor {sailor_id} already has a result in this race"}, 400)
                    return

                race['finishers'].append({
                    "sailor_id": sailor_id,
                    "finish_ts": finish_ts,
                    "status": "finished"
                })
                save_races(tracker.data_dir, next_id, races)
                log(f"[EVENT {eid}] Race {race_id}: {sailor_id} finished at {finish_ts}")
                self._send_json(race)

            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON"}, 400)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif re.match(r'^/admin/races/\d+/dnf$', subpath):
            # Mark sailor as DNF
            race_id = int(subpath.split('/')[3])
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                data = json.loads(body)
                sailor_id = str(data.get('sailor_id', '')).strip()
                if not sailor_id:
                    self._send_json({"error": "sailor_id required"}, 400)
                    return

                tracker = get_event_tracker(eid)
                if not tracker:
                    self._send_json({"error": "Could not get event tracker"}, 500)
                    return

                next_id, races = load_races(tracker.data_dir)
                race = next((r for r in races if r['id'] == race_id), None)
                if not race:
                    self._send_json({"error": f"Race {race_id} not found"}, 404)
                    return

                # Check for duplicate
                if any(f['sailor_id'] == sailor_id for f in race['finishers']):
                    self._send_json({"error": f"Sailor {sailor_id} already has a result in this race"}, 400)
                    return

                race['finishers'].append({
                    "sailor_id": sailor_id,
                    "finish_ts": None,
                    "status": "dnf"
                })
                save_races(tracker.data_dir, next_id, races)
                log(f"[EVENT {eid}] Race {race_id}: {sailor_id} DNF")
                self._send_json(race)

            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON"}, 400)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif re.match(r'^/admin/races/\d+/dns$', subpath):
            # Mark sailor as DNS (Did Not Start)
            race_id = int(subpath.split('/')[3])
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                data = json.loads(body)
                sailor_id = str(data.get('sailor_id', '')).strip()
                if not sailor_id:
                    self._send_json({"error": "sailor_id required"}, 400)
                    return

                tracker = get_event_tracker(eid)
                if not tracker:
                    self._send_json({"error": "Could not get event tracker"}, 500)
                    return

                next_id, races = load_races(tracker.data_dir)
                race = next((r for r in races if r['id'] == race_id), None)
                if not race:
                    self._send_json({"error": f"Race {race_id} not found"}, 404)
                    return

                # Check for duplicate
                if any(f['sailor_id'] == sailor_id for f in race['finishers']):
                    self._send_json({"error": f"Sailor {sailor_id} already has a result in this race"}, 400)
                    return

                race['finishers'].append({
                    "sailor_id": sailor_id,
                    "finish_ts": None,
                    "status": "dns"
                })
                save_races(tracker.data_dir, next_id, races)
                log(f"[EVENT {eid}] Race {race_id}: {sailor_id} DNS")
                self._send_json(race)

            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON"}, 400)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        else:
            self._send_json({"error": "Not found"}, 404)

    def _handle_event_delete(self, path: str):
        """Handle DELETE requests for per-event endpoints."""
        eid, subpath = self._parse_event_path(path)
        if eid is None:
            self._send_json({"error": "Invalid event path"}, 400)
            return

        if not self._check_event_admin_auth(eid):
            self._send_json({"error": "Unauthorized"}, 401)
            return

        if subpath == '/admin/course':
            tracker = get_event_tracker(eid)
            if tracker and tracker.course_file.exists():
                rotate_file(tracker.course_file)
                log(f"[EVENT {eid}] Course deleted (rotated)")
            self._send_json({"success": True})

        elif subpath.startswith('/admin/user/'):
            from urllib.parse import unquote
            user_id = unquote(subpath[len('/admin/user/'):])
            if not user_id:
                self._send_json({"error": "User ID required"}, 400)
                return

            tracker = get_event_tracker(eid)
            if tracker:
                removed = False
                # Remove sailor_id entry
                if user_id in tracker.user_overrides:
                    del tracker.user_overrides[user_id]
                    removed = True
                # Remove did:XXX entry if device has a did
                pos = tracker.position_tracker.current_positions.get(user_id)
                if pos and pos.get("did"):
                    did_key = f"did:{pos['did']}"
                    if did_key in tracker.user_overrides:
                        del tracker.user_overrides[did_key]
                        removed = True
                if removed:
                    save_user_overrides(tracker.users_file, tracker.user_overrides)
                    write_current_positions(
                        tracker.position_tracker.current_positions,
                        tracker.positions_file,
                        tracker.user_overrides,
                        tracker.position_tracker.position_tails
                    )
                    log(f"[EVENT {eid}] User override removed for {user_id}")
            self._send_json({"success": True, "user_id": user_id})

        elif subpath.startswith('/log/') and '/sublog/' in subpath:
            # Delete a sublog (race marker) from a log file's summary
            # URL format: /log/{log_file}/sublog/{index}
            parts = subpath[5:].split('/sublog/')
            if len(parts) != 2:
                self._send_json({"error": "Invalid sublog path"}, 400)
                return
            log_file = parts[0]
            try:
                sublog_index = int(parts[1])
            except ValueError:
                self._send_json({"error": "Invalid sublog index"}, 400)
                return

            if not log_file:
                self._send_json({"error": "Log file required"}, 400)
                return

            tracker = get_event_tracker(eid)
            if not tracker:
                self._send_json({"error": "Could not get event tracker"}, 500)
                return

            # Find the summary file for this log
            date_match = re.match(r'^(\d{4}_\d{2}_\d{2})\.jsonl', log_file)
            if not date_match:
                self._send_json({"error": "Invalid log file format"}, 400)
                return
            date_str = date_match.group(1)
            summary_file = tracker.log_dir / f"{date_str}_summary.json"

            if not summary_file.exists():
                self._send_json({"error": "Summary file not found"}, 404)
                return

            try:
                # Load summary
                with open(summary_file, 'r') as f:
                    summary = json.load(f)

                # Find the log entry
                log_entry = None
                for entry in summary.get('logs', []):
                    if entry.get('file') == log_file:
                        log_entry = entry
                        break

                if not log_entry:
                    self._send_json({"error": f"Log {log_file} not found in summary"}, 404)
                    return

                sublogs = log_entry.get('sublogs', [])
                if sublog_index < 0 or sublog_index >= len(sublogs):
                    self._send_json({"error": "Sublog index out of range"}, 400)
                    return

                # Remove the sublog
                removed = sublogs.pop(sublog_index)

                # Save summary atomically
                tmp_file = summary_file.with_suffix('.tmp')
                with open(tmp_file, 'w') as f:
                    json.dump(summary, f, indent=2)
                tmp_file.rename(summary_file)

                log(f"[EVENT {eid}] Removed sublog '{removed.get('name', 'unnamed')}' from {log_file}")
                self._send_json(summary)

            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif re.match(r'^/admin/races/\d+$', subpath):
            # Delete a race
            race_id = int(subpath.split('/')[3])
            tracker = get_event_tracker(eid)
            if not tracker:
                self._send_json({"error": "Could not get event tracker"}, 500)
                return

            next_id, races = load_races(tracker.data_dir)
            original_len = len(races)
            races = [r for r in races if r['id'] != race_id]
            if len(races) == original_len:
                self._send_json({"error": f"Race {race_id} not found"}, 404)
                return

            save_races(tracker.data_dir, next_id, races)
            log(f"[EVENT {eid}] Race {race_id} deleted")
            self._send_json({"success": True})

        elif re.match(r'^/admin/races/\d+/finish/.+$', subpath):
            # Undo a finish - DELETE /admin/races/{id}/finish/{sailor_id}
            parts = subpath.split('/')
            race_id = int(parts[3])
            from urllib.parse import unquote
            sailor_id = unquote(parts[5])

            tracker = get_event_tracker(eid)
            if not tracker:
                self._send_json({"error": "Could not get event tracker"}, 500)
                return

            next_id, races = load_races(tracker.data_dir)
            race = next((r for r in races if r['id'] == race_id), None)
            if not race:
                self._send_json({"error": f"Race {race_id} not found"}, 404)
                return

            original_len = len(race['finishers'])
            race['finishers'] = [f for f in race['finishers'] if f['sailor_id'] != sailor_id]
            if len(race['finishers']) == original_len:
                self._send_json({"error": f"Sailor {sailor_id} not found in race {race_id}"}, 404)
                return

            save_races(tracker.data_dir, next_id, races)
            log(f"[EVENT {eid}] Race {race_id}: undid result for {sailor_id}")
            self._send_json(race)

        else:
            self._send_json({"error": "Not found"}, 404)

    def do_POST(self):
        """Handle POST requests."""
        path = urlparse(self.path).path

        # Tracker endpoint - UDP fallback via HTTP POST
        if path == '/api/tracker':
            self._handle_tracker_post()
            return

        # iOS UDID collection endpoint - no auth required
        if path == '/api/udid':
            self._handle_udid_collection()
            return

        # Per-event endpoints
        if path.startswith('/api/event/'):
            self._handle_event_post(path)
            return

        # Manager endpoint - create event
        if path == '/api/manage/event':
            if not self._check_manager_auth():
                self._send_json({"error": "Unauthorized"}, 401)
                return
            self._handle_create_event()
            return

        if path == '/api/admin/stop-all':
            # Send remote stop command to all active trackers
            parsed = urlparse(self.path)
            query_params = parse_qs(parsed.query)
            event_id = int(query_params.get('event_id', [1])[0])

            stopped_ids = []

            tracker = get_event_tracker(event_id)
            if tracker:
                positions = tracker.position_tracker.current_positions
            else:
                positions = {}

            for user_id, pos in positions.items():
                if not pos.get("stopped", False):
                    queue_pending_command(f"{event_id}:{user_id}", "stop")
                    send_proactive_command(f"{event_id}:{user_id}", "stop")
                    stopped_ids.append(user_id)

            log(f"[ADMIN] Remote stop-all queued for {len(stopped_ids)} trackers (event {event_id}): {stopped_ids}")
            self._send_json({"success": True, "stopped_count": len(stopped_ids), "user_ids": stopped_ids})

        elif path.startswith('/api/admin/stop/'):
            # Send remote stop command to a user
            user_id = path[len('/api/admin/stop/'):]
            if not user_id:
                self._send_json({"error": "User ID required"}, 400)
                return

            # Get event ID from query parameter or use default
            parsed = urlparse(self.path)
            query_params = parse_qs(parsed.query)
            event_id = int(query_params.get('event_id', [1])[0])

            queue_pending_command(f"{event_id}:{user_id}", "stop")
            send_proactive_command(f"{event_id}:{user_id}", "stop")
            log(f"[ADMIN] Remote stop queued for {user_id} (event {event_id})")
            self._send_json({"success": True, "user_id": user_id, "event_id": event_id})

        else:
            self._send_json({"error": "Not found"}, 404)

    def _handle_tracker_post(self):
        """Handle tracker position updates via HTTP POST (UDP fallback).

        Accepts the same JSON format as UDP packets, returns ACK response.
        Supports multi-event mode via 'eid' field (defaults to 1).
        Uses per-event tracker password for authentication if configured.
        """
        client_ip = self._get_client_ip()
        recv_time = time.time()

        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            packet = json.loads(body)

            # Sanitize packet inputs
            packet = sanitize_tracker_packet(packet)

            # Extract fields with defaults (same as UDP handler)
            sailor_id = packet.get("id", "???")
            seq = packet.get("sq", 0)
            ts = packet.get("ts", 0)
            speed = packet.get("spd", 0.0)
            heading = packet.get("hdg", 0)
            assist = packet.get("ast", False)
            battery = packet.get("bat", -1)
            charging = packet.get("chg")  # Charging status (optional boolean)
            signal = packet.get("sig", -1)
            heart_rate = packet.get("hr")  # Heart rate in bpm (optional, from Wear OS)
            role = packet.get("role", "sailor")
            version = packet.get("ver", "?")
            flags = packet.get("flg", {})
            battery_drain_rate = packet.get("bdr")
            os_version = packet.get("os")  # OS version string (optional)
            horizontal_accuracy = packet.get("hac")  # Horizontal accuracy in meters (optional)
            nsats = packet.get("nsats")  # Number of GPS satellites (optional)
            stopped = packet.get("stopped", False)  # User deliberately stopped tracking
            idle = packet.get("idle", False)  # Idle heartbeat (no GPS)
            device_id = packet.get("did")  # Stable device identifier (optional)

            # Extract event ID (default to 1 for backwards compatibility)
            eid = packet.get("eid", 1)

            # Multi-event mode: look up event and check per-event password
            event = _event_manager.get_event(eid)
            if not event:
                log(f"[POST] Event {eid} not found for {sailor_id}")
                self._send_json({"ack": seq, "ts": int(recv_time), "error": "event", "msg": f"Event {eid} not found"}, 404)
                return
            if event.get('archived'):
                log(f"[POST] Event {eid} is archived, rejecting {sailor_id}")
                self._send_json({"ack": seq, "ts": int(recv_time), "error": "event", "msg": f"Event {eid} is archived"}, 400)
                return

            # Check per-event tracker password
            event_tracker_pwds = event.get('tracker_password', [])
            if event_tracker_pwds:
                if is_rate_limited(client_ip, sailor_id):
                    log(f"[AUTH] Rate limited for {sailor_id} from {client_ip} os={os_version} ver={version}")
                    self._send_json({"ack": seq, "ts": int(recv_time), "error": "auth", "msg": "Too many attempts"}, 429)
                    return
                packet_pwd = packet.get("pwd", "")
                if packet_pwd not in event_tracker_pwds:
                    record_failed_auth(client_ip, sailor_id)
                    log(f"[AUTH] Failed for event {eid} user={sailor_id} pwd='{packet_pwd}' os={os_version} ver={version} from {client_ip}")
                    self._send_json({"ack": seq, "ts": int(recv_time), "error": "auth", "msg": "Invalid password"}, 401)
                    return

            # Check for auth-only request (no position update)
            if packet.get("auth_check"):
                log(f"[AUTH] Checkuser OK for event {eid} user={sailor_id} from {client_ip} os={os_version} ver={version}")
                self._send_json({"ack": seq, "ts": int(recv_time)})
                return

            # Get or create the event tracker
            tracker = get_event_tracker(eid)
            if not tracker:
                log(f"[POST] ERROR: Could not get tracker for event {eid}")
                self._send_json({"error": "Could not initialize event tracker"}, 500)
                return
            event_name = event.get('name', f'Event {eid}')
            assist_enabled = event.get('assist_enabled', True)

            # Check for 1Hz array format vs single position
            pos_array = packet.get("pos")
            if pos_array and isinstance(pos_array, list) and len(pos_array) > 0:
                last_pos = pos_array[-1]
                lat = last_pos[1] if len(last_pos) > 1 else 0.0
                lon = last_pos[2] if len(last_pos) > 2 else 0.0
                ts = last_pos[0] if len(last_pos) > 0 else ts
            else:
                lat = packet.get("lat", 0.0)
                lon = packet.get("lon", 0.0)

            # Clear assist flag if assist is disabled for this event
            if not assist_enabled:
                assist = False

            # Process through event tracker
            tracker.process_position(
                sailor_id=sailor_id,
                lat=lat,
                lon=lon,
                speed=speed,
                heading=heading,
                ts=ts,
                assist=assist,
                battery=battery,
                signal=signal,
                role=role,
                version=version,
                flags=flags,
                src_ip=client_ip,
                source="POST",
                battery_drain_rate=battery_drain_rate,
                heart_rate=heart_rate,
                os_version=os_version,
                horizontal_accuracy=horizontal_accuracy,
                nsats=nsats,
                pos_array=pos_array,
                stopped=stopped,
                idle=idle,
                charging=charging,
                sq=seq,
                did=device_id
            )

            # Send ACK response (same format as UDP)
            ack_response = {"ack": seq, "ts": int(recv_time), "event": event_name}
            if not assist_enabled:
                ack_response["assist"] = False

            # Always include idle_interval so idle clients receive idle=0 when disabled
            idle_interval = event.get('idle_interval', 0)
            ack_response["idle"] = idle_interval

            # Check for pending command
            cmd_key = f"{eid}:{sailor_id}"
            if cmd_key in _pending_commands:
                cmd, queued_time, expiry = _pending_commands[cmd_key]
                if recv_time - queued_time < expiry:
                    ack_response["cmd"] = cmd
                    log(f"[POST] Sending {cmd} command to {sailor_id} (event {eid})")
                del _pending_commands[cmd_key]

            self._send_json(ack_response)

        except json.JSONDecodeError as e:
            log(f"[POST] JSON PARSE ERROR from {client_ip}: {e}")
            self._send_json({"error": "Invalid JSON"}, 400)
        except Exception as e:
            log(f"[POST] ERROR from {client_ip}: {e}")
            self._send_json({"error": str(e)}, 500)

    def _handle_udid_collection(self):
        """Handle iOS UDID collection from mobileconfig profile.

        iOS sends device info as signed plist (PKCS#7/CMS envelope) when installing
        a Profile Service profile. We need to extract the plist from the signature.
        """
        import plistlib
        import subprocess
        import tempfile
        import os

        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            content_type = self.headers.get('Content-Type', 'unknown')

            log(f"[UDID] Received {content_length} bytes, Content-Type: {content_type}")
            log(f"[UDID] First 100 bytes: {body[:100]}")

            data = None

            # Try parsing as raw plist first
            try:
                data = plistlib.loads(body)
                log(f"[UDID] Parsed as raw plist")
            except Exception:
                pass

            # If that failed, try extracting from PKCS#7/CMS envelope using openssl
            if data is None:
                try:
                    with tempfile.NamedTemporaryFile(delete=False, suffix='.der') as f:
                        f.write(body)
                        der_file = f.name

                    # Use openssl to extract the signed content
                    result = subprocess.run(
                        ['openssl', 'cms', '-verify', '-noverify', '-inform', 'DER',
                         '-in', der_file, '-out', '-'],
                        capture_output=True
                    )
                    os.unlink(der_file)

                    if result.returncode == 0:
                        data = plistlib.loads(result.stdout)
                        log(f"[UDID] Parsed from CMS envelope")
                    else:
                        log(f"[UDID] openssl failed: {result.stderr.decode()}")
                except Exception as e:
                    log(f"[UDID] CMS extraction failed: {e}")

            if data is None:
                log(f"[UDID] Could not parse plist from body")
                self.send_response(302)
                self.send_header('Location', '/install/flutter-ios.html?error=parse')
                self.end_headers()
                return

            # Extract UDID and device info
            udid = data.get('UDID', '')
            product = data.get('PRODUCT', '')
            version = data.get('VERSION', '')
            serial = data.get('SERIAL', '')

            log(f"[UDID] Received: UDID={udid}, Product={product}, Version={version}")

            # Redirect back to install page with UDID in URL
            redirect_url = f'/install/flutter-ios.html?udid={udid}&device={product}'

            self.send_response(301)
            self.send_header('Location', redirect_url)
            self.end_headers()

        except Exception as e:
            log(f"[UDID] Error handling request: {e}")
            import traceback
            traceback.print_exc()
            self.send_response(302)
            self.send_header('Location', '/install/flutter-ios.html?error=unknown')
            self.end_headers()

    def _handle_create_event(self):
        """Handle event creation (manager endpoint)."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body)

            if not _event_manager:
                self._send_json({"error": "Multi-event mode not enabled"}, 400)
                return

            name = data.get('name', '').strip()
            if not name:
                self._send_json({"error": "Event name is required"}, 400)
                return

            description = data.get('description', '')
            admin_password = data.get('admin_password', '')
            if not admin_password:
                self._send_json({"error": "Admin password is required"}, 400)
                return

            tracker_password = data.get('tracker_password', '')
            timezone = data.get('timezone', 'Australia/Sydney')
            home_location = data.get('home_location', '')
            home_lat = data.get('home_lat')
            home_lon = data.get('home_lon')

            eid = _event_manager.create_event(
                name=name,
                description=description,
                admin_password=admin_password,
                tracker_password=tracker_password,
                timezone=timezone,
                home_location=home_location,
                home_lat=home_lat,
                home_lon=home_lon
            )

            self._send_json({"success": True, "eid": eid})

        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, 400)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def do_PATCH(self):
        """Handle PATCH requests (for updating events)."""
        path = urlparse(self.path).path

        # Manager endpoint - update event
        match = re.match(r'^/api/manage/event/(\d+)$', path)
        if match:
            if not self._check_manager_auth():
                self._send_json({"error": "Unauthorized"}, 401)
                return

            eid = int(match.group(1))
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                updates = json.loads(body)

                if _event_manager.update_event(eid, updates):
                    self._send_json({"success": True, "eid": eid})
                else:
                    self._send_json({"error": f"Event {eid} not found"}, 404)

            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON"}, 400)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        self._send_json({"error": "Not found"}, 404)

    def do_DELETE(self):
        """Handle DELETE requests."""
        path = urlparse(self.path).path

        # Per-event DELETE endpoints
        if path.startswith('/api/event/'):
            self._handle_event_delete(path)
            return

        self._send_json({"error": "Not found"}, 404)


def run_http_server(port: int):
    """Run HTTP server in a thread."""
    server = ThreadingHTTPServer(('0.0.0.0', port), AdminHTTPHandler)
    log(f"Admin HTTP server listening on port {port}")
    server.serve_forever()


def run_summary_generator(log_dir: Path, interval: int = 60):
    """Background thread to periodically generate log summaries."""
    log(f"[SUMMARY] Background generator started (interval: {interval}s)")
    while True:
        try:
            updated = generate_log_summaries(log_dir)
            if updated > 0:
                log(f"[SUMMARY] Updated {updated} summary file(s)")
        except Exception as e:
            log(f"[SUMMARY] Error in background generator: {e}")
        time.sleep(interval)


def run_log_compressor(log_dir: Path, interval: int = 10, live_window_minutes: int = 20):
    """Background thread to compress log files for efficient serving.

    Creates two compressed files every `interval` seconds if source changed:
    1. YYYY_MM_DD_live.jsonl.gz - Rolling window of last `live_window_minutes` (for live tracking)
    2. YYYY_MM_DD.jsonl.gz - Full compressed log (for historical review)

    Uses atomic writes (temp file + rename) for concurrent read safety.
    """
    import gzip

    log(f"[COMPRESS] Background compressor started (interval: {interval}s, live window: {live_window_minutes}min)")
    last_mtime: dict[str, float] = {}

    while True:
        try:
            today = date.today()
            log_file = log_dir / f"{today.strftime('%Y_%m_%d')}.jsonl"
            live_gz_file = log_dir / f"{today.strftime('%Y_%m_%d')}_live.jsonl.gz"
            full_gz_file = log_dir / f"{today.strftime('%Y_%m_%d')}.jsonl.gz"

            if log_file.exists():
                current_mtime = log_file.stat().st_mtime
                cached_mtime = last_mtime.get(log_file.name, 0)

                if current_mtime > cached_mtime:
                    cutoff_ts = int(time.time()) - (live_window_minutes * 60)
                    live_lines = 0
                    total_lines = 0

                    # Generate rolling live file (last N minutes only)
                    tmp_live = live_gz_file.parent / f"{live_gz_file.name}.tmp"
                    with open(log_file, 'r') as f_in:
                        with gzip.open(tmp_live, 'wt') as f_out:
                            for line in f_in:
                                total_lines += 1
                                try:
                                    entry = json.loads(line)
                                    # Check timestamp - use 'ts' field or 'recv_ts'
                                    entry_ts = entry.get('ts', 0)
                                    if entry_ts >= cutoff_ts:
                                        f_out.write(line)
                                        live_lines += 1
                                except json.JSONDecodeError:
                                    pass
                    tmp_live.rename(live_gz_file)

                    # Generate full compressed file (for review page)
                    tmp_full = full_gz_file.parent / f"{full_gz_file.name}.tmp"
                    with open(log_file, 'rb') as f_in:
                        with gzip.open(tmp_full, 'wb') as f_out:
                            f_out.write(f_in.read())
                    tmp_full.rename(full_gz_file)

                    last_mtime[log_file.name] = current_mtime

                    # Log stats
                    orig_size = log_file.stat().st_size
                    live_size = live_gz_file.stat().st_size
                    full_size = full_gz_file.stat().st_size
                    log(f"[COMPRESS] Updated: live={live_size:,}B ({live_lines}/{total_lines} entries), "
                          f"full={full_size:,}B (from {orig_size:,}B)")

        except Exception as e:
            tb_lines = traceback.format_exc().strip().split('\n')[-3:]
            log(f"[COMPRESS] Error: {e}")
            for tb_line in tb_lines:
                log(f"[COMPRESS]   {tb_line}")
        time.sleep(interval)



def run_midnight_clearer(event_manager: EventManager, check_interval: int = 60):
    """Background thread to clear tracks at midnight in each event's timezone.

    Checks every `check_interval` seconds if any event has crossed midnight
    in its configured timezone. If so, clears tracks for that event (rotating
    log files so they can still be viewed in track review).
    """
    log(f"[MIDNIGHT] Auto-clear service started (check interval: {check_interval}s)")

    # Track which date we last cleared for each event (to avoid multiple clears)
    last_cleared_date: dict[int, date] = {}

    while True:
        try:
            for eid in event_manager.list_events():
                event_info = event_manager.get_event(eid)
                if not event_info:
                    continue
                tz_name = event_info.get('timezone', 'Australia/Sydney')

                try:
                    tz = ZoneInfo(tz_name)
                except Exception:
                    tz = ZoneInfo('Australia/Sydney')

                # Get current date in event's timezone
                now_in_tz = datetime.now(tz)
                today_in_tz = now_in_tz.date()

                # Check if we've already cleared for today
                if eid in last_cleared_date and last_cleared_date[eid] >= today_in_tz:
                    continue

                # Check if it's just after midnight (within first check_interval*2 seconds of the day)
                seconds_since_midnight = now_in_tz.hour * 3600 + now_in_tz.minute * 60 + now_in_tz.second
                if seconds_since_midnight < check_interval * 2:
                    # It's just after midnight - clear positions only (not the log file)
                    # DailyLogger already handles switching to a new date-named file
                    tracker = get_event_tracker(eid)
                    if tracker:
                        tracker.clear_positions_only()
                        last_cleared_date[eid] = today_in_tz
                        log(f"[MIDNIGHT] Auto-cleared positions for event {eid} ({event_info.get('name', 'Unknown')}) "
                            f"at midnight {tz_name}")

        except Exception as e:
            tb_lines = traceback.format_exc().strip().split('\n')[-3:]
            log(f"[MIDNIGHT] Error: {e}")
            for tb_line in tb_lines:
                log(f"[MIDNIGHT]   {tb_line}")

        time.sleep(check_interval)


def run_server(port: int, http_port: int | None = None,
               static_dir: Path | None = None,
               manager_password: str | None = None, events_file: Path | None = None,
               gt06_port: int | None = None, gt06_interval: int = 10, gt06_id_prefix: str = "G",
               gt06_config_path: Path | None = None, gt06_log_path: Path | None = None):
    """Main server loop (multi-event mode).

    Requires manager_password. Events are managed via events.json (or --events-file).
    Each event has its own data directory under static_dir/{eid}/.
    Per-event admin and tracker passwords are used.
    """
    global _static_dir, _event_manager, _udp_sock

    if not manager_password:
        log("[ERROR] manager_password is required")
        return
    if not static_dir:
        log("[ERROR] --static-dir is required")
        return
    if not events_file:
        events_file = Path("events.json")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    _udp_sock = sock

    log(f"Tracker server listening on UDP port {port}")
    log("Waiting for packets...")

    log(f"[EVENTS] Multi-event mode enabled")
    log(f"[EVENTS] Events file: {events_file}")
    log(f"[EVENTS] HTML directory: {static_dir}")

    _event_manager = EventManager(events_file, static_dir)
    _event_manager.manager_password = manager_password
    _static_dir = static_dir

    log(f"[EVENTS] Loaded {len(_event_manager.events)} events\n")

    if http_port:
        log(f"Multi-event API: http://SERVER:{http_port}/api/events")

    # Start HTTP server if enabled
    if http_port:
        http_thread = threading.Thread(target=run_http_server, args=(http_port,), daemon=True)
        http_thread.start()

    # Start GT06 TCP listener if enabled
    if gt06_port:
        gt06_config = load_gt06_config(gt06_config_path) if gt06_config_path else {"default_eid": 1, "devices": {}}

        def _gt06_get_tracker(eid):
            return get_event_tracker(eid)
        global _gt06_listener
        gt06_listener = GT06Listener(gt06_port, gt06_interval, gt06_id_prefix, _gt06_get_tracker, gt06_config,
                                      log_file=gt06_log_path)
        _gt06_listener = gt06_listener
        gt06_thread = threading.Thread(target=gt06_listener.run, daemon=True, name="gt06-listener")
        gt06_thread.start()

    # Start background summary/compressor for each event
    for eid in _event_manager.list_events():
        event_log_dir = _event_manager.get_event_data_dir(eid) / "logs"
        if event_log_dir.exists():
            summary_thread = threading.Thread(
                target=run_summary_generator,
                args=(event_log_dir,),
                daemon=True,
                name=f"summary-{eid}"
            )
            summary_thread.start()

            compressor_thread = threading.Thread(
                target=run_log_compressor,
                args=(event_log_dir,),
                daemon=True,
                name=f"compressor-{eid}"
            )
            compressor_thread.start()

    # Start midnight track clearer
    midnight_thread = threading.Thread(
        target=run_midnight_clearer,
        args=(_event_manager,),
        daemon=True,
        name="midnight-clearer"
    )
    midnight_thread.start()

    try:
        while True:
            # Increased buffer size to 4096 to handle 1Hz mode packets with 10 positions
            data, addr = sock.recvfrom(4096)
            recv_time = time.time()
            client_ip = addr[0]

            try:
                packet = json.loads(data.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                log(f"[{addr[0]}:{addr[1]}] Invalid packet: {e}")
                continue

            # Wrap processing in try/except to prevent crash on bad data
            try:
                # Sanitize packet inputs
                packet = sanitize_tracker_packet(packet)

                # Extract fields with defaults
                sailor_id = packet.get("id", "???")
                seq = packet.get("sq", 0)
                ts = packet.get("ts", 0)
                speed = packet.get("spd", 0.0)
                heading = packet.get("hdg", 0)
                assist = packet.get("ast", False)
                battery = packet.get("bat", -1)
                charging = packet.get("chg")  # Charging status (optional boolean)
                signal = packet.get("sig", -1)
                heart_rate = packet.get("hr")  # Heart rate in bpm (optional, from Wear OS)
                role = packet.get("role", "sailor")
                version = packet.get("ver", "?")
                flags = packet.get("flg", {})
                battery_drain_rate = packet.get("bdr")  # Battery drain rate %/hr
                os_version = packet.get("os")  # OS version string (optional)
                horizontal_accuracy = packet.get("hac")  # Horizontal accuracy in meters (optional)
                nsats = packet.get("nsats")  # Number of GPS satellites (optional)
                stopped = packet.get("stopped", False)  # User deliberately stopped tracking
                idle = packet.get("idle", False)  # Idle heartbeat (no GPS)
                device_id = packet.get("did")  # Stable device identifier (optional)

                # Extract event ID (default to 1 for backwards compatibility)
                eid = packet.get("eid", 1)

                # Check for 1Hz array format vs old single position format
                pos_array = packet.get("pos")  # [[ts, lat, lon], ...]
                if pos_array and isinstance(pos_array, list) and len(pos_array) > 0:
                    # New 1Hz array format - use last position for live display
                    last_pos = pos_array[-1]
                    lat = last_pos[1] if len(last_pos) > 1 else 0.0
                    lon = last_pos[2] if len(last_pos) > 2 else 0.0
                    # Use timestamp from last position
                    ts = last_pos[0] if len(last_pos) > 0 else ts
                else:
                    # Old single position format (backwards compatible)
                    lat = packet.get("lat", 0.0)
                    lon = packet.get("lon", 0.0)

                # Look up event and check per-event password
                event = _event_manager.get_event(eid)
                if not event:
                    log(f"[UDP] Event {eid} not found for {sailor_id}")
                    error_ack = json.dumps({"ack": seq, "ts": int(recv_time), "error": "event", "msg": f"Event {eid} not found"}).encode("utf-8")
                    sock.sendto(error_ack, addr)
                    continue
                if event.get('archived'):
                    log(f"[UDP] Event {eid} is archived, rejecting {sailor_id}")
                    error_ack = json.dumps({"ack": seq, "ts": int(recv_time), "error": "event", "msg": f"Event {eid} is archived"}).encode("utf-8")
                    sock.sendto(error_ack, addr)
                    continue

                # Check per-event tracker password
                event_tracker_pwds = event.get('tracker_password', [])
                if event_tracker_pwds:
                    if is_rate_limited(client_ip, sailor_id):
                        log(f"[UDP] Auth rate-limited for {sailor_id} from {client_ip}")
                        error_ack = json.dumps({"ack": seq, "ts": int(recv_time), "error": "auth", "msg": "Invalid password"}).encode("utf-8")
                        sock.sendto(error_ack, addr)
                        continue
                    packet_pwd = packet.get("pwd", "")
                    if packet_pwd not in event_tracker_pwds:
                        record_failed_auth(client_ip, sailor_id)
                        log(f"[UDP] Auth failed for {sailor_id} (event {eid}) from {client_ip} pwd='{packet_pwd}'")
                        error_ack = json.dumps({"ack": seq, "ts": int(recv_time), "error": "auth", "msg": "Invalid password"}).encode("utf-8")
                        sock.sendto(error_ack, addr)
                        continue

                # Record client address for proactive command sending
                _client_addrs[f"{eid}:{sailor_id}"] = addr

                # Get or create the event tracker
                event_tracker = get_event_tracker(eid)
                if not event_tracker:
                    log(f"[UDP] ERROR: Could not get tracker for event {eid}")
                    error_ack = json.dumps({"ack": seq, "ts": int(recv_time), "error": "server", "msg": "Could not initialize event tracker"}).encode("utf-8")
                    sock.sendto(error_ack, addr)
                    continue

                # Send ACK with event name and assist status
                event_name = event.get('name', f'Event {eid}')
                assist_enabled = event.get('assist_enabled', True)
                ack_data = {"ack": seq, "ts": int(recv_time), "event": event_name}
                if not assist_enabled:
                    ack_data["assist"] = False

                # Always include idle_interval so idle clients receive idle=0 when disabled
                idle_interval = event.get('idle_interval', 0)
                ack_data["idle"] = idle_interval

                # Check for pending command
                cmd_key = f"{eid}:{sailor_id}"
                if cmd_key in _pending_commands:
                    cmd, queued_time, expiry = _pending_commands[cmd_key]
                    if recv_time - queued_time < expiry:
                        ack_data["cmd"] = cmd
                        log(f"[UDP] Sending {cmd} command to {sailor_id} (event {eid})")
                    del _pending_commands[cmd_key]

                ack = json.dumps(ack_data).encode("utf-8")
                sock.sendto(ack, addr)

                # Clear assist flag if assist is disabled for this event
                if not assist_enabled:
                    assist = False

                # Process through event tracker
                event_tracker.process_position(
                    sailor_id=sailor_id,
                    lat=lat,
                    lon=lon,
                    speed=speed,
                    heading=heading,
                    ts=ts,
                    assist=assist,
                    battery=battery,
                    signal=signal,
                    role=role,
                    version=version,
                    flags=flags,
                    src_ip=client_ip,
                    source="UDP",
                    battery_drain_rate=battery_drain_rate,
                    heart_rate=heart_rate,
                    os_version=os_version,
                    horizontal_accuracy=horizontal_accuracy,
                    nsats=nsats,
                    pos_array=pos_array,
                    stopped=stopped,
                    idle=idle,
                    charging=charging,
                    sq=seq,
                    did=device_id
                )

            except Exception as e:
                tb_lines = traceback.format_exc().strip().split('\n')[-3:]
                log(f"[UDP] Error from {client_ip}: {e}")
                for tb_line in tb_lines:
                    log(f"[UDP]   {tb_line}")
                continue

    except KeyboardInterrupt:
        log("Shutting down...")
    finally:
        sock.close()


def load_settings(settings_file: Path = Path("settings.json")) -> dict:
    """Load settings from settings.json if it exists."""
    defaults = {
        "port": 41234,
        "static_dir": "html",
        "events_file": "events.json",
        "manager_password": None,
        "admin_password": None,
        "tracker_password": None,
        "log_dir": "logs",
        "users_file": "users.json",
        "course_file": "course.json",
        "http_port": None,
        "no_http": False,
        "no_track_logs": False,
        "gt06_port": None,
        "gt06_interval": 10,
        "gt06_id_prefix": "G",
        "gt06_config": "gt06.json",
        "gt06_log": "gt06.log",
    }

    if settings_file.exists():
        try:
            with open(settings_file) as f:
                file_settings = json.load(f)
            defaults.update(file_settings)
            log(f"Loaded settings from {settings_file}")
        except Exception as e:
            log(f"Warning: Could not load {settings_file}: {e}")

    return defaults


def main():
    # Load settings from settings.json first (if exists)
    settings = load_settings()

    parser = argparse.ArgumentParser(
        description="Windsurfer Tracker UDP Server",
        epilog="Settings can also be specified in settings.json. Command line args override file settings."
    )
    parser.add_argument(
        "-p", "--port",
        type=int,
        default=None,
        help=f"UDP port to listen on (default: {settings['port']})"
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=None,
        help="HTTP port for admin API (default: same as UDP port)"
    )
    parser.add_argument(
        "--no-http",
        action="store_true",
        default=None,
        help="Disable HTTP admin API"
    )
    parser.add_argument(
        "--static-dir",
        type=Path,
        default=None,
        help=f"Directory to serve static files from (default: {settings['static_dir']})"
    )
    parser.add_argument(
        "--manager-password",
        type=str,
        default=None,
        help="Manager password for multi-event mode (enables event management)"
    )
    parser.add_argument(
        "--events-file",
        type=Path,
        default=None,
        help=f"Events configuration file (default: {settings['events_file']})"
    )
    parser.add_argument(
        "--gt06-port",
        type=int,
        default=None,
        help="TCP port for GT06 GPS tracker protocol (disabled if not set)"
    )
    parser.add_argument(
        "--gt06-interval",
        type=int,
        default=None,
        help="GT06 location reporting interval in seconds (default: 10)"
    )
    parser.add_argument(
        "--gt06-id-prefix",
        type=str,
        default=None,
        help="Prefix for GT06 sailor IDs (default: G)"
    )
    parser.add_argument(
        "--gt06-config",
        type=Path,
        default=None,
        help="GT06 device config file for IMEI-to-event mapping (default: gt06.json)"
    )
    parser.add_argument(
        "--gt06-log",
        type=Path,
        default=None,
        help="GT06 binary packet log file (default: gt06.log)"
    )

    args = parser.parse_args()

    # Merge: command line args override settings.json, which overrides built-in defaults
    port = args.port if args.port is not None else settings['port']
    static_dir = Path(args.static_dir) if args.static_dir else (Path(settings['static_dir']) if settings['static_dir'] else None)
    events_file = Path(args.events_file) if args.events_file else Path(settings['events_file'])
    manager_password = args.manager_password if args.manager_password else settings['manager_password']

    no_http = args.no_http if args.no_http is not None else settings.get('no_http', False)
    http_port_setting = args.http_port if args.http_port else settings.get('http_port')
    http_port = None if no_http else (http_port_setting or port)

    # manager_password is required when HTTP is enabled
    if http_port and not manager_password:
        parser.error("manager_password is required when HTTP is enabled (use no_http: true to disable)")
    if manager_password and http_port is None:
        parser.error("manager_password requires HTTP to be enabled")

    # GT06 settings
    gt06_port = args.gt06_port if args.gt06_port is not None else settings.get('gt06_port')
    gt06_interval = args.gt06_interval if args.gt06_interval is not None else settings.get('gt06_interval', 10)
    gt06_id_prefix = args.gt06_id_prefix if args.gt06_id_prefix is not None else settings.get('gt06_id_prefix', 'G')
    gt06_config_path = args.gt06_config if args.gt06_config is not None else Path(settings.get('gt06_config', 'gt06.json'))
    gt06_log_path = args.gt06_log if args.gt06_log is not None else Path(settings.get('gt06_log', 'gt06.log'))

    run_server(port,
               http_port=http_port,
               static_dir=static_dir,
               manager_password=manager_password,
               events_file=events_file,
               gt06_port=gt06_port, gt06_interval=gt06_interval,
               gt06_id_prefix=gt06_id_prefix,
               gt06_config_path=gt06_config_path,
               gt06_log_path=gt06_log_path)


if __name__ == "__main__":
    main()
