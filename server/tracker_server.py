#!/usr/bin/env python3
"""
Windsurfer Tracker - Multi-Event UDP Server with HTTP Admin API
Receives position reports from sailor apps, sends ACKs, logs data.
Provides HTTP endpoints for admin functions, course management, and event management.
Supports multiple concurrent events, each with its own data directory and passwords.
"""

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


def _unique_archive_path(parent: Path, base_name: str, date_str: str) -> Path:
    """Return parent/old_logs/<base>.<date>[.<N>] picking the first non-existing N."""
    old_dir = parent / "old_logs"
    old_dir.mkdir(exist_ok=True)
    candidate = old_dir / f"{base_name}.{date_str}"
    if not candidate.exists():
        return candidate
    n = 1
    while True:
        candidate = old_dir / f"{base_name}.{date_str}.{n}"
        if not candidate.exists():
            return candidate
        n += 1


def rotate_copytruncate(path: Path, archive_path: Path) -> bool:
    """Copy `path` to `archive_path` and truncate the original in place.

    Used for log files we don't own the fd for (e.g. tracker.log, redirected
    from systemd stdout). Because systemd opened the file in append mode, new
    writes after truncation start at offset 0. A tiny window between copy and
    truncate may drop a write, which is acceptable for a daily rotator.
    """
    import shutil
    if not path.exists():
        return False
    try:
        shutil.copy2(path, archive_path)
        with open(path, "r+b") as f:
            f.truncate(0)
        return True
    except Exception as e:
        log(f"[ROTATE] copytruncate failed for {path}: {e}")
        return False


class LogRotator:
    """Daily log rotation at server-local midnight.

    Each registered handler is a callable(date_str) that performs one
    rotation. date_str is the YYYY-MM-DD label for the day that just ended.
    Runs in a daemon thread.
    """

    def __init__(self):
        self._handlers: list = []
        self._stop = threading.Event()

    def register(self, handler):
        """Register a rotation handler — callable(date_str) -> None."""
        self._handlers.append(handler)

    def stop(self):
        self._stop.set()

    def _seconds_until_next_midnight(self) -> float:
        from datetime import timedelta
        now = datetime.now()
        tomorrow = (now + timedelta(days=1)).date()
        next_midnight = datetime.combine(tomorrow, datetime.min.time())
        return max(1.0, (next_midnight - now).total_seconds())

    def run(self):
        from datetime import timedelta
        log("[ROTATE] Daily log rotator started")
        while not self._stop.is_set():
            sleep_s = self._seconds_until_next_midnight()
            if self._stop.wait(sleep_s):
                return
            # Label the rotated logs with yesterday's date (the day that just ended)
            date_str = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d")
            for handler in self._handlers:
                try:
                    handler(date_str)
                except Exception as e:
                    log(f"[ROTATE] Handler error: {e}")


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


from protocol_GT06 import GT06Listener, GT06Connection, load_gt06_config
from protocol_JT808 import JT808Listener, load_jt808_config


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
                        "home_lon": event.get("home_lon"),
                        "has_registration": bool(event.get("admin_emails")),
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
                              'idle_interval', 'admin_emails',
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
            # Update postfix virtual aliases if admin_emails changed
            if 'admin_emails' in updates or 'name' in updates:
                try:
                    update_postfix_virtual_aliases()
                except Exception as e:
                    log(f"[EVENTS] Failed to update postfix aliases: {e}")
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

    def get_event_state(self, eid: int) -> str | None:
        """Return the explicit event-scope tracking state, or None if unset.

        Distinguishing "explicitly idle" from "never set" matters for the
        login handler: an explicit choice (made via /admin/start-all,
        /admin/stop-all, or /admin/state) must override any per-sailor
        persisted state, while an unset event falls through to the
        persisted-per-sailor signal.
        """
        with self._lock:
            event = self.events.get(eid)
            if not event:
                return None
            return event.get("event_state")

    def set_event_state(self, eid: int, state: str, idle_submode: str | None = None) -> bool:
        """Set the event-scope tracking state ("tracking" or "idle"). Persisted to events.json.

        When state="idle", idle_submode picks "race" (default) or "overnight".
        Switching to "tracking" always clears the idle_submode back to "race".
        """
        if state not in ("tracking", "idle"):
            return False
        if idle_submode is not None and idle_submode not in ("race", "overnight"):
            return False
        with self._lock:
            if eid not in self.events:
                return False
            self.events[eid]["event_state"] = state
            self.events[eid]["event_state_updated"] = time.time()
            if state == "tracking":
                # Leaving idle entirely — reset submode so a future /admin/stop
                # without an explicit submode goes back to race-day idle.
                self.events[eid]["idle_submode"] = "race"
            elif idle_submode is not None:
                self.events[eid]["idle_submode"] = idle_submode
            self._save_events()
            sub_str = f" submode={self.events[eid].get('idle_submode')}" if state == "idle" else ""
            log(f"[EVENTS] Event {eid} state set to '{state}'{sub_str}")
            return True

    def get_event_idle_submode(self, eid: int) -> str:
        """Return the persisted idle-submode for an event. Default 'race'."""
        with self._lock:
            event = self.events.get(eid)
            if not event:
                return "race"
            return event.get("idle_submode", "race")


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
                    # Preserve idle/stopped/charging/bat_v across restart so
                    # admin overrides (set_idle) and start-all/stop-all see the
                    # correct state, and reconnecting trackers inherit it via
                    # the login handler.
                    for k in ("idle", "stopped", "sleep", "chg", "bat_v"):
                        if k in pos:
                            self.current_positions[sailor_id][k] = pos[k]
                    if "did" in pos:
                        self.current_positions[sailor_id]["did"] = pos["did"]
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
                         sq: int = 0, did: str | None = None,
                         battery_voltage: float | None = None) -> bool:
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
                if battery_voltage is not None:
                    pos_data["bat_v"] = battery_voltage
                if os_version:
                    pos_data["os"] = os_version
                # Preserve existing lat/lon if user previously tracked
                if "lat" in existing and "lon" in existing:
                    pos_data["lat"] = existing["lat"]
                    pos_data["lon"] = existing["lon"]
                # Preserve per-sailor SLEEP (overnight) flag — set by
                # /admin/sleep, persisted to current_positions.json so
                # the state survives server restart and isn't clobbered
                # by MODE5 wake-cycle heartbeats.
                if existing.get("sleep"):
                    pos_data["sleep"] = True
                self.current_positions[sailor_id] = pos_data

            bat_str = f"{battery}%" if battery >= 0 else "?"
            if battery_voltage is not None:
                bat_str += f"/{battery_voltage}V"
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
                if battery_voltage is not None:
                    pos_data["bat_v"] = battery_voltage
                if os_version:
                    pos_data["os"] = os_version
                # Preserve existing lat/lon if user previously tracked
                if "lat" in existing and "lon" in existing:
                    pos_data["lat"] = existing["lat"]
                    pos_data["lon"] = existing["lon"]
                # Preserve per-sailor SLEEP flag — see comment on the
                # idle branch above for rationale.
                if existing.get("sleep"):
                    pos_data["sleep"] = True
                self.current_positions[sailor_id] = pos_data

            bat_str = f"{battery}%" if battery >= 0 else "?"
            if battery_voltage is not None:
                bat_str += f"/{battery_voltage}V"
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
                if battery_voltage is not None:
                    track_entry["bat_v"] = battery_voltage
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
        if battery_voltage is not None:
            bat_str += f"/{battery_voltage}V"
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
                if battery_voltage is not None:
                    pos_data["bat_v"] = battery_voltage
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
                if battery_voltage is not None:
                    track_entry["bat_v"] = battery_voltage
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
        self.courses_file = data_dir / "courses.json"
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
                         did: str | None = None,
                         battery_voltage: float | None = None) -> bool:
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
                charging=charging, sq=sq, did=did,
                battery_voltage=battery_voltage
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
            if battery_voltage is not None:
                track_entry["bat_v"] = battery_voltage
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
            did=did,
            battery_voltage=battery_voltage
        )

        # No write here — PositionTracker.process_position() already writes
        # current_positions.json on the active / idle / no-gps paths with
        # the user_overrides we passed in above. Writing again would double
        # the per-packet serialization cost on the GT06 hot path; matters
        # at 200+ trackers.
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
_protocol_listeners: list = []  # all protocol listeners (GT06, JT808, etc.)

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

# Simulator globals
_active_simulations: dict[int, dict] = {}  # eid -> {thread, stop_event, config, start_time, status}
_simulations_lock = threading.Lock()
_server_port: int | None = None


def _run_simulator_thread(eid: int, config: dict, stop_event: "threading.Event",
                          course_file: str, tracker_pwd: str, udp_port: int,
                          speedup_ref: list | None = None):
    """Thread wrapper for running a simulation for an event."""
    from test_client import run_simulation

    def status_cb(status):
        with _simulations_lock:
            if eid in _active_simulations:
                _active_simulations[eid]['status'] = status

    log(f"[EVENT {eid}] Simulator starting: {config}")
    try:
        result = run_simulation(
            host="127.0.0.1", port=udp_port, eid=eid,
            num_sailors=config.get('num_sailors', 5),
            num_support=config.get('num_support', 1),
            num_spectators=0,
            wind_direction=config.get('wind_direction'),
            avg_speed=config.get('speed', 12.0),
            num_laps=config.get('laps', 0),
            delay=10.0,
            max_duration=config.get('max_duration', 3600),
            password=tracker_pwd,
            course_file=course_file,
            stop_event=stop_event,
            status_callback=status_cb,
            speedup=config.get('speedup', 1.0),
            start_at_start=config.get('start_at_start', True),
            speedup_ref=speedup_ref,
        )
        log(f"[EVENT {eid}] Simulator finished: {result}")
    except Exception as e:
        log(f"[EVENT {eid}] Simulator error: {e}")


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
    if ip == "127.0.0.1":
        return False
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


def _effective_role(user_overrides: dict, sailor_id: str, pos: dict) -> str:
    """Return effective role for a position, considering admin overrides."""
    override = get_user_override(user_overrides, sailor_id, pos.get('did'))
    if override and 'role' in override:
        return override['role']
    return pos.get('role', 'sailor')


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

def send_confirmation_email(to_email: str, code: str, event_name: str = "", confirm_url: str = "") -> bool:
    """Send a registration confirmation email with the 6-digit code.

    Uses local sendmail (postfix) to send from {eventname}@wstracker.org.
    Returns True if sent successfully, False otherwise.
    """
    import subprocess
    from email.mime.text import MIMEText

    # Build from address: EventName@wstracker.org (strip spaces)
    if event_name:
        from_local = re.sub(r'[^a-zA-Z0-9_-]', '', event_name.replace(' ', ''))
        from_addr = f"{from_local}@wstracker.org"
    else:
        from_addr = "noreply@wstracker.org"

    subject = f"{event_name or 'Windsurfer Tracker'} - Registration Confirmation"
    if confirm_url:
        body = (
            f"Click below to confirm your registration:\n\n"
            f"    {confirm_url}\n\n"
            f"— {event_name or 'Windsurfer Tracker'}"
        )
    else:
        body = (
            f"Your registration confirmation code is:\n\n"
            f"    {code}\n\n"
            f"Enter this code on the registration page to confirm your entry.\n\n"
            f"— {event_name or 'Windsurfer Tracker'}"
        )
    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = f"{event_name or 'Windsurfer Tracker'} <{from_addr}>"
    msg['To'] = to_email

    try:
        proc = subprocess.run(
            ['/usr/sbin/sendmail', '-t', '-f', from_addr],
            input=msg.as_string().encode('utf-8'),
            capture_output=True, timeout=10
        )
        if proc.returncode == 0:
            log(f"[REGISTRATION] Confirmation email sent to {to_email} from {from_addr}")
            return True
        else:
            log(f"[REGISTRATION] sendmail failed (rc={proc.returncode}): {proc.stderr.decode()}")
            return False
    except FileNotFoundError:
        log(f"[REGISTRATION] sendmail not found. Confirmation code for {to_email}: {code}")
        return True
    except Exception as e:
        log(f"[REGISTRATION] Failed to send email to {to_email}: {e}")
        return False


def send_admin_registration_notify(admin_emails: str, entry: dict, event_name: str = "") -> bool:
    """Notify event admins of a new registration.

    admin_emails is a comma-separated string of email addresses.
    """
    import subprocess
    from email.mime.text import MIMEText

    targets = [e.strip() for e in admin_emails.split(',') if e.strip()]
    if not targets:
        return False

    if event_name:
        from_local = re.sub(r'[^a-zA-Z0-9_-]', '', event_name.replace(' ', ''))
        from_addr = f"{from_local}@wstracker.org"
    else:
        from_addr = "noreply@wstracker.org"

    name = entry.get('name', '?')
    email = entry.get('email', '?')
    sail = entry.get('sail_number', '')
    wcaa = entry.get('wcaa', '')
    club = entry.get('club', '')
    phone = entry.get('phone', '')
    days = entry.get('days', '')

    subject = f"{event_name or 'Windsurfer Tracker'} - New registration: {name}"
    lines = [f"New registration for {event_name or 'event'}:", ""]
    lines.append(f"  Name:        {name}")
    lines.append(f"  Email:       {email}")
    if phone:
        lines.append(f"  Phone:       {phone}")
    if sail:
        lines.append(f"  Sail Number: {sail}")
    if wcaa:
        lines.append(f"  WCAA Number: {wcaa}")
    if club:
        lines.append(f"  Club:        {club}")
    if days:
        lines.append(f"  Days:        {days}")
    lines.append("")
    lines.append("They have been sent a confirmation email.")
    lines.append(f"\n— {event_name or 'Windsurfer Tracker'}")
    body = '\n'.join(lines)

    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = f"{event_name or 'Windsurfer Tracker'} <{from_addr}>"
    msg['To'] = ', '.join(targets)

    try:
        proc = subprocess.run(
            ['/usr/sbin/sendmail', '-t', '-f', from_addr],
            input=msg.as_string().encode('utf-8'),
            capture_output=True, timeout=10
        )
        if proc.returncode == 0:
            log(f"[REGISTRATION] Admin notification sent to {targets} for {email}")
            return True
        else:
            log(f"[REGISTRATION] Admin notify sendmail failed (rc={proc.returncode}): {proc.stderr.decode()}")
            return False
    except FileNotFoundError:
        log(f"[REGISTRATION] sendmail not found. Admin notification skipped for {email}")
        return True
    except Exception as e:
        log(f"[REGISTRATION] Failed to notify admins for {email}: {e}")
        return False


def update_postfix_virtual_aliases():
    """Regenerate /etc/postfix/virtual from event admin_emails and reload postfix.

    Creates entries like: CapitalCup2026@wstracker.org -> admin1@x.com, admin2@y.com
    Also maintains the static admin@wstracker.org forwarding.
    """
    import subprocess

    if not _event_manager:
        return

    lines = [
        "# Auto-generated by tracker_server.py — do not edit manually",
        "# Static aliases",
        "admin@wstracker.org tridge60@gmail.com",
        "",
        "# Event-specific forwarding",
    ]

    for eid, event in _event_manager.events.items():
        admin_emails = event.get('admin_emails', '')
        event_name = event.get('name', '')
        if not admin_emails or not event_name:
            continue
        # Build local part: strip non-alphanumeric
        local_part = re.sub(r'[^a-zA-Z0-9_-]', '', event_name.replace(' ', ''))
        if not local_part:
            continue
        # admin_emails is comma-separated
        targets = ', '.join(e.strip() for e in admin_emails.split(',') if e.strip())
        if targets:
            lines.append(f"{local_part}@wstracker.org {targets}")

    virtual_content = '\n'.join(lines) + '\n'

    try:
        virtual_path = Path('/etc/postfix/virtual')
        virtual_path.write_text(virtual_content)
        subprocess.run(['/usr/sbin/postmap', '/etc/postfix/virtual'],
                       capture_output=True, timeout=10)
        subprocess.run(['/usr/sbin/postfix', 'reload'],
                       capture_output=True, timeout=10)
        log(f"[EMAIL] Updated postfix virtual aliases ({len(lines) - 5} event entries)")
    except FileNotFoundError:
        log("[EMAIL] postfix not installed, skipping virtual alias update")
    except Exception as e:
        log(f"[EMAIL] Failed to update postfix virtual aliases: {e}")


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
            
            # Security: prevent directory traversal. Use path-ancestor
            # containment (Path.relative_to) rather than string-prefix match,
            # which would let a sibling like /srv/web-backup pass when
            # _static_dir is /srv/web.
            try:
                filepath = (_static_dir / path.lstrip('/')).resolve()
                filepath.relative_to(_static_dir.resolve())
            except ValueError:
                self._send_json({"error": "Forbidden"}, 403)
                return
            except Exception:
                self._send_json({"error": "Bad request"}, 400)
                return
            
            # Directory index: serve index.html if path is a directory
            if filepath.is_dir():
                index_file = filepath / 'index.html'
                if index_file.exists():
                    filepath = index_file
                else:
                    self._send_json({"error": "Not found"}, 404)
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

        elif subpath == '/courses':
            # Return all named courses for this event (public)
            tracker = get_event_tracker(eid)
            if tracker and tracker.courses_file.exists():
                try:
                    with open(tracker.courses_file, 'r') as f:
                        courses = json.load(f)
                    self._send_json({"courses": courses})
                except Exception as e:
                    self._send_json({"error": str(e)}, 500)
            else:
                self._send_json({"courses": {}})

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
                        log(f"[WARN] Error reading summary {summary_file}: {e}")
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
                    log(f"[WARN] Error reading info.json for event {eid}: {e}")

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
                sent = _gt06_listener.send_command_to(eid, user_id, cmd_str)
                if sent:
                    self._send_json({"success": True, "user_id": user_id, "cmd": cmd_str})
                else:
                    self._send_json({"error": f"GT06 device {user_id} not connected"}, 404)
            else:
                self._send_json({"error": "GT06 listener not running"}, 404)

        elif subpath.startswith('/admin/jt808-cmd/'):
            # Send a command to a JT808 device
            # URL: /api/event/{eid}/admin/jt808-cmd/{user_id}?cmd=query-params
            if not self._check_event_admin_auth(eid):
                self._send_json({"error": "Unauthorized"}, 401)
                return
            from urllib.parse import unquote
            user_id = unquote(subpath[len('/admin/jt808-cmd/'):])
            if not user_id:
                self._send_json({"error": "User ID required"}, 400)
                return
            params = parse_qs(urlparse(self.path).query)
            cmd_str = params.get("cmd", [None])[0]
            if not cmd_str:
                self._send_json({"error": "cmd parameter required"}, 400)
                return
            for listener in _protocol_listeners:
                if isinstance(listener, JT808Listener):
                    sent = listener.send_command_to(eid, user_id, cmd_str)
                    if sent:
                        self._send_json({"success": True, "user_id": user_id, "cmd": cmd_str})
                        return
            self._send_json({"error": f"JT808 device {user_id} not connected"}, 404)

        elif subpath == '/races':
            # Return all races for this event (public)
            tracker = get_event_tracker(eid)
            if tracker:
                next_id, races = load_races(tracker.data_dir)
                self._send_json({"races": races})
            else:
                self._send_json({"races": []})

        elif subpath == '/admin/state':
            # Return event-scope tracking state (admin only)
            if not self._check_event_admin_auth(eid):
                self._send_json({"error": "Unauthorized"}, 401)
                return
            state = _event_manager.get_event_state(eid) if _event_manager else "idle"
            self._send_json({"event_id": eid, "state": state})

        elif subpath == '/admin/simulator/status':
            # Return simulator status (admin only)
            if not self._check_event_admin_auth(eid):
                self._send_json({"error": "Unauthorized"}, 401)
                return
            with _simulations_lock:
                sim = _active_simulations.get(eid)
                if sim:
                    if sim['thread'].is_alive():
                        status = dict(sim['status'])
                        status['running'] = True
                        status['config'] = sim['config']
                        status['elapsed_s'] = time.time() - sim['start_time']
                        self._send_json(status)
                    else:
                        # Thread died — clean up
                        del _active_simulations[eid]
                        self._send_json({"running": False})
                else:
                    self._send_json({"running": False})

        elif subpath == '/registrations':
            # Return confirmed registrations (public — limited fields)
            tracker = get_event_tracker(eid)
            if not tracker:
                self._send_json({"registrations": []})
                return
            reg_file = tracker.data_dir / 'registrations.jsonl'
            registrations = []
            if reg_file.exists():
                with open(reg_file, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if entry.get('confirmed'):
                            registrations.append({
                                'name': entry.get('name', ''),
                                'sail_number': entry.get('sail_number', ''),
                                'wcaa': entry.get('wcaa', ''),
                                'club': entry.get('club', ''),
                                'gender': entry.get('gender', ''),
                                'days': entry.get('days', ''),
                            })
            self._send_json({"registrations": registrations})

        elif subpath == '/registrations/full':
            # Return all registrations with full details (admin only)
            if not self._check_event_admin_auth(eid):
                self._send_json({"error": "Unauthorized"}, 401)
                return
            tracker = get_event_tracker(eid)
            if not tracker:
                self._send_json({"registrations": []})
                return
            reg_file = tracker.data_dir / 'registrations.jsonl'
            registrations = []
            if reg_file.exists():
                with open(reg_file, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        # Strip internal code field from response
                        entry.pop('code', None)
                        registrations.append(entry)
            self._send_json({"registrations": registrations})

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

        # Registration endpoints - public, no auth required
        if subpath == '/register':
            self._handle_registration(eid)
            return
        if subpath == '/register/confirm':
            self._handle_registration_confirm(eid)
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

        elif subpath.startswith('/admin/courses/'):
            # Save a named course
            from urllib.parse import unquote
            course_name = unquote(subpath[len('/admin/courses/'):])
            if not course_name:
                self._send_json({"error": "Course name required"}, 400)
                return
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                course = json.loads(body)
                course['saved'] = time.time()

                tracker = get_event_tracker(eid)
                if tracker:
                    # Load existing courses
                    courses = {}
                    if tracker.courses_file.exists():
                        with open(tracker.courses_file, 'r') as f:
                            courses = json.load(f)
                    courses[course_name] = course
                    tmp_file = tracker.courses_file.with_suffix('.tmp')
                    with open(tmp_file, 'w') as f:
                        json.dump(courses, f, indent=2)
                    tmp_file.rename(tracker.courses_file)
                    log(f"[EVENT {eid}] Named course saved: '{course_name}'")
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

        elif subpath.startswith('/admin/registration/'):
            # Update registration fields (email is the key, cannot be changed)
            from urllib.parse import unquote
            email_key = unquote(subpath[len('/admin/registration/'):]).lower()
            if not email_key:
                self._send_json({"error": "Email required"}, 400)
                return

            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                data = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return

            tracker = get_event_tracker(eid)
            if not tracker:
                self._send_json({"error": "Could not get event tracker"}, 500)
                return

            reg_file = tracker.data_dir / 'registrations.jsonl'
            if not reg_file.exists():
                self._send_json({"error": "Registration not found"}, 404)
                return

            editable_fields = ('name', 'phone', 'sail_number', 'wcaa', 'club', 'gender', 'weight', 'dob', 'days')
            entries = []
            found = False
            with open(reg_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get('email', '').lower() == email_key and not found:
                        for field in editable_fields:
                            if field in data:
                                entry[field] = data[field]
                        found = True
                    entries.append(entry)

            if not found:
                self._send_json({"error": "Registration not found"}, 404)
                return

            tmp_file = reg_file.with_suffix('.tmp')
            with open(tmp_file, 'w') as f:
                for entry in entries:
                    f.write(json.dumps(entry) + '\n')
            tmp_file.rename(reg_file)
            log(f"[EVENT {eid}] Registration updated by admin for {email_key}")
            self._send_json({"success": True, "message": "Registration updated"})

        elif subpath == '/admin/state':
            # Set the event-scope tracking state. Body: {"state": "tracking"|"idle"}
            if not _event_manager:
                self._send_json({"error": "Multi-event mode required"}, 400)
                return
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body_raw = self.rfile.read(content_length).decode('utf-8')
                body = json.loads(body_raw) if body_raw else {}
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            new_state = body.get("state")
            if new_state not in ("tracking", "idle"):
                self._send_json({"error": "state must be 'tracking' or 'idle'"}, 400)
                return
            if not _event_manager.set_event_state(eid, new_state):
                self._send_json({"error": f"Event {eid} not found"}, 404)
                return
            self._send_json({"success": True, "event_id": eid, "state": new_state})

        elif subpath == '/admin/stop-all':
            # Send remote stop command to all active trackers
            tracker = get_event_tracker(eid)
            if not tracker:
                self._send_json({"error": f"Event {eid} not found"}, 404)
                return

            # Set event-scope state so trackers joining *after* this call also
            # default to race-day idle. Pass idle_submode="race" explicitly —
            # set_event_state() leaves submode untouched when omitted, which
            # would let a stale "overnight" from an earlier /admin/sleep-all
            # leak into future reconnects.
            if _event_manager:
                _event_manager.set_event_state(eid, "idle", idle_submode="race")

            stopped_ids = []
            for user_id, pos in tracker.position_tracker.current_positions.items():
                if not pos.get("stopped", False):
                    queue_pending_command(f"{eid}:{user_id}", "stop")
                    send_proactive_command(f"{eid}:{user_id}", "stop")
                    for listener in _protocol_listeners:
                        listener.set_idle(eid, user_id, True)
                    stopped_ids.append(user_id)

            log(f"[EVENT {eid}] Remote stop-all queued for {len(stopped_ids)} trackers: {stopped_ids}")
            self._send_json({"success": True, "stopped_count": len(stopped_ids), "user_ids": stopped_ids})

        elif subpath == '/admin/sleep-all':
            # Like /admin/stop-all but switches to OVERNIGHT idle (MODE5 deep
            # sleep). Sets event_state="idle" + idle_submode="overnight" so
            # any tracker reconnecting later also picks up overnight commands.
            tracker = get_event_tracker(eid)
            if not tracker:
                self._send_json({"error": f"Event {eid} not found"}, 404)
                return
            if _event_manager:
                _event_manager.set_event_state(eid, "idle", idle_submode="overnight")
            sleep_ids = []
            for user_id in list(tracker.position_tracker.current_positions.keys()):
                for listener in _protocol_listeners:
                    if hasattr(listener, "set_idle"):
                        try:
                            listener.set_idle(eid, user_id, True, submode="overnight")
                        except TypeError:
                            # JT808Listener doesn't yet take submode — fall back
                            listener.set_idle(eid, user_id, True)
                sleep_ids.append(user_id)
            log(f"[EVENT {eid}] Remote sleep-all (MODE5 overnight) queued for {len(sleep_ids)} trackers: {sleep_ids}")
            self._send_json({"success": True, "sleep_count": len(sleep_ids), "user_ids": sleep_ids})

        elif subpath.startswith('/admin/sleep/'):
            # Per-tracker overnight (MODE5) idle.
            from urllib.parse import unquote
            user_id = unquote(subpath[len('/admin/sleep/'):])
            if not user_id:
                self._send_json({"error": "User ID required"}, 400)
                return
            for listener in _protocol_listeners:
                if hasattr(listener, "set_idle"):
                    try:
                        listener.set_idle(eid, user_id, True, submode="overnight")
                    except TypeError:
                        listener.set_idle(eid, user_id, True)
            log(f"[EVENT {eid}] Remote sleep (MODE5 overnight) queued for {user_id}")
            self._send_json({"success": True, "user_id": user_id, "event_id": eid})

        elif subpath.startswith('/admin/stop/'):
            # Send remote stop command to a user
            from urllib.parse import unquote
            user_id = unquote(subpath[len('/admin/stop/'):])
            if not user_id:
                self._send_json({"error": "User ID required"}, 400)
                return

            queue_pending_command(f"{eid}:{user_id}", "stop")
            send_proactive_command(f"{eid}:{user_id}", "stop")
            for listener in _protocol_listeners:
                listener.set_idle(eid, user_id, True)
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
            for listener in _protocol_listeners:
                listener.cancel_assist(eid, user_id)
            log(f"[EVENT {eid}] Remote cancel assist queued for {user_id}")
            self._send_json({"success": True, "user_id": user_id, "event_id": eid})

        elif subpath == '/admin/start-all':
            # Send remote start command to all idle trackers
            tracker = get_event_tracker(eid)
            if not tracker:
                self._send_json({"error": f"Event {eid} not found"}, 404)
                return

            # Set event-scope state so trackers joining *after* this call also
            # default to active tracking.
            if _event_manager:
                _event_manager.set_event_state(eid, "tracking")

            started_ids = []
            for user_id, pos in tracker.position_tracker.current_positions.items():
                if pos.get("idle", False):
                    queue_pending_command(f"{eid}:{user_id}", "start")
                    send_proactive_command(f"{eid}:{user_id}", "start")
                    for listener in _protocol_listeners:
                        listener.set_idle(eid, user_id, False)
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
            for listener in _protocol_listeners:
                listener.set_idle(eid, user_id, False)
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
                    if race['finishers']:
                        log(f"[EVENT {eid}] Race {race_id} reset — clearing {len(race['finishers'])} results: {json.dumps(race['finishers'])}")
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

                # Look up display name from user overrides
                finisher = {
                    "sailor_id": sailor_id,
                    "finish_ts": finish_ts,
                    "status": "finished"
                }
                if tracker.user_overrides:
                    pos = tracker.position_tracker.current_positions.get(sailor_id, {})
                    override = get_user_override(tracker.user_overrides, sailor_id, pos.get("did"))
                    if override and override.get('name'):
                        finisher["displayid"] = override['name']
                race['finishers'].append(finisher)
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

                # Look up display name from user overrides
                finisher = {
                    "sailor_id": sailor_id,
                    "finish_ts": None,
                    "status": "dnf"
                }
                if tracker.user_overrides:
                    pos = tracker.position_tracker.current_positions.get(sailor_id, {})
                    override = get_user_override(tracker.user_overrides, sailor_id, pos.get("did"))
                    if override and override.get('name'):
                        finisher["displayid"] = override['name']
                race['finishers'].append(finisher)
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

                # Look up display name from user overrides
                finisher = {
                    "sailor_id": sailor_id,
                    "finish_ts": None,
                    "status": "dns"
                }
                if tracker.user_overrides:
                    pos = tracker.position_tracker.current_positions.get(sailor_id, {})
                    override = get_user_override(tracker.user_overrides, sailor_id, pos.get("did"))
                    if override and override.get('name'):
                        finisher["displayid"] = override['name']
                race['finishers'].append(finisher)
                save_races(tracker.data_dir, next_id, races)
                log(f"[EVENT {eid}] Race {race_id}: {sailor_id} DNS")
                self._send_json(race)

            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON"}, 400)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif subpath == '/admin/simulator/start':
            # Start a simulation for this event
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8') if content_length > 0 else '{}'
                data = json.loads(body) if body else {}

                with _simulations_lock:
                    if eid in _active_simulations and _active_simulations[eid]['thread'].is_alive():
                        self._send_json({"error": "Simulation already running for this event"}, 409)
                        return

                tracker = get_event_tracker(eid)
                if not tracker or not tracker.course_file.exists():
                    self._send_json({"error": "Course must be set before starting simulator"}, 400)
                    return

                course_path = str(tracker.course_file)
                tp = event.get('tracker_password', [])
                tracker_pwd = tp[0] if tp else ""

                config = {
                    'num_sailors': int(data.get('num_sailors', 5)),
                    'num_support': int(data.get('num_support', 1)),
                    'wind_direction': float(data['wind_direction']) if data.get('wind_direction') is not None else None,
                    'speed': float(data.get('speed', 12)),
                    'laps': int(data.get('laps', 3)),
                    'max_duration': int(data.get('max_duration', 3600)),
                    'speedup': max(1.0, min(50.0, float(data.get('speedup', 1)))),
                    'start_at_start': data.get('start_type', 'from_start') == 'from_start',
                }

                stop_ev = threading.Event()
                speedup_ref = [config['speedup']]
                t = threading.Thread(
                    target=_run_simulator_thread,
                    args=(eid, config, stop_ev, course_path, tracker_pwd, _server_port,
                          speedup_ref),
                    daemon=True
                )
                with _simulations_lock:
                    _active_simulations[eid] = {
                        'thread': t,
                        'stop_event': stop_ev,
                        'config': config,
                        'start_time': time.time(),
                        'status': {'updates_sent': 0, 'sailors_finished': 0, 'elapsed_s': 0},
                        'speedup_ref': speedup_ref,
                    }
                t.start()
                self._send_json({"success": True})

            except (json.JSONDecodeError, ValueError) as e:
                self._send_json({"error": f"Invalid request: {e}"}, 400)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif subpath == '/admin/simulator/stop':
            # Stop a running simulation
            with _simulations_lock:
                sim = _active_simulations.get(eid)
                if sim:
                    sim['stop_event'].set()
                    del _active_simulations[eid]
            self._send_json({"success": True})

        elif subpath == '/admin/simulator/speedup':
            # Update speedup of a running simulation
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8') if content_length > 0 else '{}'
                data = json.loads(body) if body else {}
                new_speedup = max(1.0, min(50.0, float(data.get('speedup', 1))))

                with _simulations_lock:
                    sim = _active_simulations.get(eid)
                    if sim and sim['thread'].is_alive():
                        sim['speedup_ref'][0] = new_speedup
                        sim['config']['speedup'] = new_speedup
                        self._send_json({"success": True, "speedup": new_speedup})
                    else:
                        self._send_json({"error": "No simulation running"}, 404)
            except (json.JSONDecodeError, ValueError) as e:
                self._send_json({"error": f"Invalid request: {e}"}, 400)

        else:
            self._send_json({"error": "Not found"}, 404)

    def _handle_registration(self, eid: int):
        """Handle POST /api/event/{eid}/register — public registration."""
        import random

        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        name = str(data.get('name', '')).strip()
        email_addr = str(data.get('email', '')).strip().lower()
        if not name or not email_addr:
            self._send_json({"error": "Name and email are required"}, 400)
            return

        tracker = get_event_tracker(eid)
        if not tracker:
            self._send_json({"error": "Could not get event tracker"}, 500)
            return

        # Get event name for email from address
        event = _event_manager.get_event(eid) if _event_manager else None
        event_name = event.get('name', '') if event else ''

        # Build confirmation URL from Referer or Origin header
        from urllib.parse import urlencode, quote
        referer = self.headers.get('Referer', '')
        if referer:
            # Strip any existing query/fragment from the referer
            page_url = referer.split('?')[0].split('#')[0]
        else:
            origin = self.headers.get('Origin', '')
            page_url = origin  # fallback; may be empty

        reg_file = tracker.data_dir / 'registrations.jsonl'

        # Build new entry from submitted data
        new_entry = {
            'name': name,
            'email': email_addr,
            'phone': str(data.get('phone', '')).strip(),
            'sail_number': str(data.get('sail_number', '')).strip(),
            'wcaa': str(data.get('wcaa', '')).strip(),
            'club': str(data.get('club', '')).strip(),
            'gender': str(data.get('gender', '')).strip(),
            'weight': data.get('weight'),
            'dob': str(data.get('dob', '')).strip(),
            'days': str(data.get('days', 'Both')).strip(),
        }

        # Validate DOB if provided
        dob = new_entry['dob']
        if dob:
            m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{4})$', dob)
            if not m:
                self._send_json({"error": "Date of birth must be DD/MM/YYYY"}, 400)
                return
            day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if month < 1 or month > 12 or day < 1 or day > 31 or year < 1920 or year > 2026:
                self._send_json({"error": "Date of birth out of range"}, 400)
                return
            import calendar
            if day > calendar.monthrange(year, month)[1]:
                self._send_json({"error": "Invalid day for that month"}, 400)
                return

        new_entry['registered'] = time.time()
        new_entry['registered_iso'] = datetime.now().isoformat()

        # Check if email already exists
        entries = []
        existing_idx = -1
        if reg_file.exists():
            with open(reg_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get('email', '').lower() == email_addr:
                        existing_idx = len(entries)
                    entries.append(entry)

        if existing_idx >= 0:
            existing = entries[existing_idx]
            if existing.get('confirmed'):
                # Already confirmed — update fields, keep confirmed
                new_entry['confirmed'] = True
                entries[existing_idx] = new_entry
                tmp_file = reg_file.with_suffix('.tmp')
                with open(tmp_file, 'w') as f:
                    for entry in entries:
                        f.write(json.dumps(entry) + '\n')
                tmp_file.rename(reg_file)
                log(f"[EVENT {eid}] Registration updated for {email_addr} (already confirmed)")
                self._send_json({"success": True, "already_confirmed": True, "message": "Registration updated"})
                return
            else:
                # Unconfirmed — regenerate code, update fields
                code = f"{random.randint(0, 999999):06d}"
                new_entry['confirmed'] = False
                new_entry['code'] = code
                entries[existing_idx] = new_entry
                tmp_file = reg_file.with_suffix('.tmp')
                with open(tmp_file, 'w') as f:
                    for entry in entries:
                        f.write(json.dumps(entry) + '\n')
                tmp_file.rename(reg_file)
                confirm_url = f"{page_url}?confirm={quote(email_addr)}&code={code}" if page_url else ""
                send_confirmation_email(email_addr, code, event_name, confirm_url)
                log(f"[EVENT {eid}] Registration re-submitted for {email_addr}")
                self._send_json({"success": True, "message": "Confirmation code sent to your email"})
                return

        # New registration
        code = f"{random.randint(0, 999999):06d}"
        new_entry['confirmed'] = False
        new_entry['code'] = code
        with open(reg_file, 'a') as f:
            f.write(json.dumps(new_entry) + '\n')
        confirm_url = f"{page_url}?confirm={quote(email_addr)}&code={code}" if page_url else ""
        send_confirmation_email(email_addr, code, event_name, confirm_url)
        admin_emails = event.get('admin_emails', '') if event else ''
        if admin_emails:
            send_admin_registration_notify(admin_emails, new_entry, event_name)
        log(f"[EVENT {eid}] New registration for {name} ({email_addr})")
        self._send_json({"success": True, "message": "Confirmation code sent to your email"})

    def _handle_registration_confirm(self, eid: int):
        """Handle POST /api/event/{eid}/register/confirm — confirm registration with code."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        email_addr = str(data.get('email', '')).strip().lower()
        code = str(data.get('code', '')).strip()
        if not email_addr or not code:
            self._send_json({"error": "Email and code are required"}, 400)
            return

        tracker = get_event_tracker(eid)
        if not tracker:
            self._send_json({"error": "Could not get event tracker"}, 500)
            return

        reg_file = tracker.data_dir / 'registrations.jsonl'
        if not reg_file.exists():
            self._send_json({"error": "Registration not found"}, 404)
            return

        entries = []
        found = False
        with open(reg_file, 'r') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get('email', '').lower() == email_addr and not found:
                    if entry.get('confirmed'):
                        self._send_json({"error": "Already confirmed"}, 400)
                        return
                    if entry.get('code') != code:
                        self._send_json({"error": "Invalid confirmation code"}, 400)
                        return
                    entry['confirmed'] = True
                    entry.pop('code', None)
                    entry['confirmed_at'] = time.time()
                    entry['confirmed_at_iso'] = datetime.now().isoformat()
                    found = True
                entries.append(entry)

        if not found:
            self._send_json({"error": "Registration not found"}, 404)
            return

        tmp_file = reg_file.with_suffix('.tmp')
        with open(tmp_file, 'w') as f:
            for entry in entries:
                f.write(json.dumps(entry) + '\n')
        tmp_file.rename(reg_file)
        log(f"[EVENT {eid}] Registration confirmed for {email_addr}")
        self._send_json({"success": True, "message": "Registration confirmed"})

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

        elif subpath.startswith('/admin/courses/'):
            from urllib.parse import unquote
            course_name = unquote(subpath[len('/admin/courses/'):])
            if not course_name:
                self._send_json({"error": "Course name required"}, 400)
                return
            tracker = get_event_tracker(eid)
            if tracker and tracker.courses_file.exists():
                try:
                    with open(tracker.courses_file, 'r') as f:
                        courses = json.load(f)
                    if course_name in courses:
                        del courses[course_name]
                        tmp_file = tracker.courses_file.with_suffix('.tmp')
                        with open(tmp_file, 'w') as f:
                            json.dump(courses, f, indent=2)
                        tmp_file.rename(tracker.courses_file)
                        log(f"[EVENT {eid}] Named course deleted: '{course_name}'")
                    self._send_json({"success": True})
                except Exception as e:
                    self._send_json({"error": str(e)}, 500)
            else:
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
            deleted_race = next((r for r in races if r['id'] == race_id), None)
            if not deleted_race:
                self._send_json({"error": f"Race {race_id} not found"}, 404)
                return

            races = [r for r in races if r['id'] != race_id]
            save_races(tracker.data_dir, next_id, races)
            log(f"[EVENT {eid}] Race {race_id} deleted: {json.dumps(deleted_race)}")
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

            removed = [f for f in race['finishers'] if f['sailor_id'] == sailor_id]
            if not removed:
                self._send_json({"error": f"Sailor {sailor_id} not found in race {race_id}"}, 404)
                return

            race['finishers'] = [f for f in race['finishers'] if f['sailor_id'] != sailor_id]
            save_races(tracker.data_dir, next_id, races)
            log(f"[EVENT {eid}] Race {race_id}: undid result for {sailor_id}: {json.dumps(removed[0])}")
            self._send_json(race)

        elif subpath.startswith('/admin/registration/'):
            # Delete a registration by email
            from urllib.parse import unquote
            email = unquote(subpath[len('/admin/registration/'):])
            if not email:
                self._send_json({"error": "Email required"}, 400)
                return
            tracker = get_event_tracker(eid)
            if not tracker:
                self._send_json({"error": "Could not get event tracker"}, 500)
                return
            reg_file = tracker.data_dir / 'registrations.jsonl'
            if not reg_file.exists():
                self._send_json({"error": "No registrations found"}, 404)
                return
            entries = []
            removed_entries = []
            with open(reg_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get('email', '').lower() == email.lower():
                        entry['deleted_at'] = time.time()
                        entry['deleted_at_iso'] = datetime.now().isoformat()
                        removed_entries.append(entry)
                        continue
                    entries.append(entry)
            if removed_entries:
                # Append to deleted log
                deleted_file = tracker.data_dir / 'registrations_deleted.jsonl'
                with open(deleted_file, 'a') as f:
                    for entry in removed_entries:
                        f.write(json.dumps(entry) + '\n')
                # Rewrite active registrations
                tmp_file = reg_file.with_suffix('.tmp')
                with open(tmp_file, 'w') as f:
                    for entry in entries:
                        f.write(json.dumps(entry) + '\n')
                tmp_file.rename(reg_file)
                log(f"[EVENT {eid}] Registration removed for {email}")
                self._send_json({"success": True})
            else:
                self._send_json({"error": f"Registration not found for {email}"}, 404)

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
            # Tracker JSON packets are tiny; cap at 64 KB so a hostile or
            # broken client can't make us slurp arbitrary bytes into memory.
            MAX_TRACKER_BODY = 64 * 1024
            if content_length > MAX_TRACKER_BODY:
                log(f"[HTTP] /api/tracker oversized body ({content_length} bytes) from {client_ip}")
                self._send_json({"error": "Payload too large"}, 413)
                return
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
                    # Log only the length, never the value — a misconfigured
                    # client could send a real event password to the wrong
                    # event and we don't want that in logs.
                    log(f"[AUTH] Failed for event {eid} user={sailor_id} pwd_len={len(packet_pwd)} os={os_version} ver={version} from {client_ip}")
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

            # Include effective role (admin override or self-reported)
            override = get_user_override(tracker.user_overrides, sailor_id, device_id)
            ack_response["eRole"] = override.get("role", role) if override else role

            # Always include idle_interval so idle clients receive idle=0 when disabled
            idle_interval = event.get('idle_interval', 0)
            ack_response["idle"] = idle_interval

            # Check if any sailor has active assist (for support boat alerts)
            any_assist = any(
                pos.get('ast', False)
                for sid, pos in tracker.position_tracker.current_positions.items()
                if _effective_role(tracker.user_overrides, sid, pos) == 'sailor'
            )
            if any_assist:
                ack_response["any_assist"] = True

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
            # iOS UDID plist (signed PKCS#7 envelope) is a few KB; cap at
            # 256 KB so a hostile client can't make us slurp arbitrary bytes.
            MAX_UDID_BODY = 256 * 1024
            if content_length > MAX_UDID_BODY:
                client_ip = self.client_address[0] if self.client_address else "?"
                log(f"[UDID] oversized body ({content_length} bytes) from {client_ip}")
                self.send_response(413)
                self.end_headers()
                return
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

            # Set additional fields via update
            extra = {}
            if data.get('admin_emails'):
                extra['admin_emails'] = data['admin_emails']
            if data.get('assist_enabled') is not None:
                extra['assist_enabled'] = data['assist_enabled']
            if data.get('idle_interval') is not None:
                extra['idle_interval'] = data['idle_interval']
            if extra:
                _event_manager.update_event(eid, extra)

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
               gt06_config_path: Path | None = None, gt06_log_path: Path | None = None,
               jt808_port: int | None = None, jt808_interval: int = 10, jt808_id_prefix: str = "J",
               jt808_config_path: Path | None = None, jt808_log_path: Path | None = None,
               tracker_log_path: Path | None = None,
):
    """Main server loop (multi-event mode).

    Requires manager_password. Events are managed via events.json (or --events-file).
    Each event has its own data directory under static_dir/{eid}/.
    Per-event admin and tracker passwords are used.
    """
    global _static_dir, _event_manager, _udp_sock, _server_port

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
    _server_port = port

    log(f"Tracker server listening on UDP port {port}")
    log("Waiting for packets...")

    log(f"[EVENTS] Multi-event mode enabled")
    log(f"[EVENTS] Events file: {events_file}")
    log(f"[EVENTS] HTML directory: {static_dir}")

    _event_manager = EventManager(events_file, static_dir)
    _event_manager.manager_password = manager_password
    _static_dir = static_dir

    log(f"[EVENTS] Loaded {len(_event_manager.events)} events\n")

    # Update postfix virtual aliases from event admin_emails
    try:
        update_postfix_virtual_aliases()
    except Exception as e:
        log(f"[EMAIL] Could not update postfix aliases on startup: {e}")

    if http_port:
        log(f"Multi-event API: http://SERVER:{http_port}/api/events")

    # Start HTTP server if enabled
    if http_port:
        http_thread = threading.Thread(target=run_http_server, args=(http_port,), daemon=True)
        http_thread.start()

    # Start GT06 TCP listener if enabled
    if gt06_port:
        gt06_config = load_gt06_config(gt06_config_path, log_func=log) if gt06_config_path else {"default_eid": 1, "devices": {}}

        def _gt06_get_tracker(eid):
            return get_event_tracker(eid)
        def _gt06_get_event_state(eid):
            return _event_manager.get_event_state(eid) if _event_manager else "idle"
        def _gt06_get_event_idle_submode(eid):
            return _event_manager.get_event_idle_submode(eid) if _event_manager else "race"
        global _gt06_listener
        gt06_listener = GT06Listener(gt06_port, gt06_interval, gt06_id_prefix, _gt06_get_tracker, gt06_config,
                                      log_file=gt06_log_path, log_func=log,
                                      save_overrides_func=save_user_overrides,
                                      write_positions_func=write_current_positions,
                                      get_event_state_func=_gt06_get_event_state,
                                      get_event_idle_submode_func=_gt06_get_event_idle_submode)
        _gt06_listener = gt06_listener
        _protocol_listeners.append(gt06_listener)
        gt06_thread = threading.Thread(target=gt06_listener.run, daemon=True, name="gt06-listener")
        gt06_thread.start()

    # Start JT808 TCP listener if enabled
    if jt808_port:
        jt808_config = load_jt808_config(jt808_config_path, log_func=log) if jt808_config_path else {"default_eid": 1, "devices": {}}

        def _jt808_get_tracker(eid):
            return get_event_tracker(eid)
        def _jt808_get_event_state(eid):
            return _event_manager.get_event_state(eid) if _event_manager else None
        def _jt808_get_event_idle_submode(eid):
            return _event_manager.get_event_idle_submode(eid) if _event_manager else "race"
        jt808_listener = JT808Listener(jt808_port, jt808_interval, jt808_id_prefix, _jt808_get_tracker, jt808_config,
                                        log_file=jt808_log_path, log_func=log,
                                        save_overrides_func=save_user_overrides,
                                        write_positions_func=write_current_positions,
                                        get_event_state_func=_jt808_get_event_state,
                                        get_event_idle_submode_func=_jt808_get_event_idle_submode)
        _protocol_listeners.append(jt808_listener)
        jt808_thread = threading.Thread(target=jt808_listener.run, daemon=True, name="jt808-listener")
        jt808_thread.start()

    # Daily log rotation at server-local midnight. Rotated files land in
    # old_logs/ alongside the source file, named "<base>.<YYYY-MM-DD>".
    rotator = LogRotator()
    if tracker_log_path:
        tlp = Path(tracker_log_path)
        def _rotate_tracker(date_str, _p=tlp):
            archive = _unique_archive_path(_p.parent, _p.name, date_str)
            if rotate_copytruncate(_p, archive):
                log(f"[ROTATE] {_p} -> {archive}")
        rotator.register(_rotate_tracker)
    if gt06_port and gt06_log_path:
        glp = Path(gt06_log_path)
        def _rotate_gt06(date_str, _p=glp, _l=gt06_listener):
            archive = _unique_archive_path(_p.parent, _p.name, date_str)
            _l.rotate_log_to(archive)
            log(f"[ROTATE] {_p} -> {archive}")
        rotator.register(_rotate_gt06)
    if jt808_port and jt808_log_path:
        jlp = Path(jt808_log_path)
        def _rotate_jt808(date_str, _p=jlp, _l=jt808_listener):
            archive = _unique_archive_path(_p.parent, _p.name, date_str)
            _l.rotate_log_to(archive)
            log(f"[ROTATE] {_p} -> {archive}")
        rotator.register(_rotate_jt808)
    threading.Thread(target=rotator.run, daemon=True, name="log-rotator").start()

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
                        # Log length only (not the value) — see HTTP path.
                        log(f"[UDP] Auth failed for {sailor_id} (event {eid}) from {client_ip} pwd_len={len(packet_pwd)}")
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

                # Include effective role (admin override or self-reported)
                override = get_user_override(event_tracker.user_overrides, sailor_id, device_id)
                ack_data["eRole"] = override.get("role", role) if override else role

                # Always include idle_interval so idle clients receive idle=0 when disabled
                idle_interval = event.get('idle_interval', 0)
                ack_data["idle"] = idle_interval

                # Check if any sailor has active assist (for support boat alerts)
                any_assist = any(
                    pos.get('ast', False)
                    for sid, pos in event_tracker.position_tracker.current_positions.items()
                    if _effective_role(event_tracker.user_overrides, sid, pos) == 'sailor'
                )
                if any_assist:
                    ack_data["any_assist"] = True

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
        "tracker_log": None,
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
    parser.add_argument(
        "--jt808-port",
        type=int,
        default=None,
        help="TCP port for JT808 GPS tracker protocol (disabled if not set)"
    )
    parser.add_argument(
        "--jt808-interval",
        type=int,
        default=None,
        help="JT808 location reporting interval in seconds (default: 10)"
    )
    parser.add_argument(
        "--jt808-id-prefix",
        type=str,
        default=None,
        help="Prefix for JT808 sailor IDs (default: J)"
    )
    parser.add_argument(
        "--jt808-config",
        type=Path,
        default=None,
        help="JT808 device config file for IMEI-to-event mapping (default: jt808.json)"
    )
    parser.add_argument(
        "--jt808-log",
        type=Path,
        default=None,
        help="JT808 binary packet log file (default: jt808.log)"
    )
    parser.add_argument(
        "--tracker-log",
        type=Path,
        default=None,
        help="Path to tracker.log (systemd stdout redirect target). If set, "
             "it is rotated daily via copytruncate alongside the protocol logs."
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

    # JT808 settings
    jt808_port = args.jt808_port if args.jt808_port is not None else settings.get('jt808_port')
    jt808_interval = args.jt808_interval if args.jt808_interval is not None else settings.get('jt808_interval', 10)
    jt808_id_prefix = args.jt808_id_prefix if args.jt808_id_prefix is not None else settings.get('jt808_id_prefix', 'J')
    jt808_config_path = args.jt808_config if args.jt808_config is not None else Path(settings.get('jt808_config', 'jt808.json'))
    jt808_log_path = args.jt808_log if args.jt808_log is not None else Path(settings.get('jt808_log', 'jt808.log'))
    _tracker_log_setting = args.tracker_log if args.tracker_log is not None else settings.get('tracker_log')
    tracker_log_path = Path(_tracker_log_setting) if _tracker_log_setting else None

    run_server(port,
               http_port=http_port,
               static_dir=static_dir,
               manager_password=manager_password,
               events_file=events_file,
               gt06_port=gt06_port, gt06_interval=gt06_interval,
               gt06_id_prefix=gt06_id_prefix,
               gt06_config_path=gt06_config_path,
               gt06_log_path=gt06_log_path,
               jt808_port=jt808_port, jt808_interval=jt808_interval,
               jt808_id_prefix=jt808_id_prefix,
               jt808_config_path=jt808_config_path,
               jt808_log_path=jt808_log_path,
               tracker_log_path=tracker_log_path)


if __name__ == "__main__":
    main()
