#!/usr/bin/env python3
"""
Windsurfer Tracker - Multi-Event UDP Server with HTTP Admin API
Receives position reports from sailor apps, sends ACKs, logs data.
Provides HTTP endpoints for admin functions, course management, and event management.
Supports multiple concurrent events, each with its own data directory and passwords.
"""

import socket
import json
import time
import argparse
import os
import re
import threading
import email.utils
from datetime import datetime, date, timezone
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse


def format_timestamp(ts: int) -> str:
    """Convert unix timestamp to readable format."""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def format_position(lat: float, lon: float) -> str:
    """Format lat/lon for display."""
    lat_dir = "S" if lat < 0 else "N"
    lon_dir = "W" if lon < 0 else "E"
    return f"{abs(lat):.5f}°{lat_dir} {abs(lon):.5f}°{lon_dir}"


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
    print(f"Rotated {filepath} -> {new_path}")
    return new_path


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
            sailors: dict[str, dict] = {}  # id -> {points, first_ts, last_ts}

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

                            if ts is None or sailor_id is None:
                                continue

                            point_count += 1

                            if start_ts is None or ts < start_ts:
                                start_ts = ts
                            if end_ts is None or ts > end_ts:
                                end_ts = ts

                            if sailor_id not in sailors:
                                sailors[sailor_id] = {
                                    'points': 0,
                                    'first_ts': ts,
                                    'last_ts': ts
                                }

                            sailors[sailor_id]['points'] += 1
                            if ts < sailors[sailor_id]['first_ts']:
                                sailors[sailor_id]['first_ts'] = ts
                            if ts > sailors[sailor_id]['last_ts']:
                                sailors[sailor_id]['last_ts'] = ts

                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                print(f"[SUMMARY] Error reading {log_file}: {e}")
                continue

            if point_count > 0:
                logs_data.append({
                    'file': log_file.name,
                    'index': rotation_idx,
                    'start_ts': start_ts,
                    'end_ts': end_ts,
                    'point_count': point_count,
                    'sailors': sailors
                })

        if not logs_data:
            continue

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
            print(f"[SUMMARY] Generated {summary_file.name}: {len(logs_data)} logs, {total_points} points")
        except Exception as e:
            print(f"[SUMMARY] Error writing {summary_file}: {e}")

    return updated_count


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
            print(f"[EVENTS] No events file found at {self.events_file}")
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
                    self.events[eid] = event
                except ValueError:
                    print(f"[EVENTS] Skipping invalid event ID: {eid_str}")
            print(f"[EVENTS] Loaded {len(self.events)} events from {self.events_file}")
        except Exception as e:
            print(f"[EVENTS] Error loading events file: {e}")

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
            print(f"[EVENTS] Saved {len(self.events)} events to {self.events_file}")
        except Exception as e:
            print(f"[EVENTS] Error saving events file: {e}")

    def get_event(self, eid: int) -> dict | None:
        """Get event by ID."""
        with self._lock:
            return self.events.get(eid)

    def get_public_events(self) -> list[dict]:
        """Get list of active (non-archived) events without passwords."""
        with self._lock:
            result = []
            for eid, event in self.events.items():
                if not event.get('archived', False):
                    result.append({
                        "eid": eid,
                        "name": event.get("name", f"Event {eid}"),
                        "description": event.get("description", "")
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
                     admin_password: str, tracker_password: str = "") -> int:
        """Create new event, return event ID."""
        with self._lock:
            eid = self.next_eid
            self.next_eid += 1
            self.events[eid] = {
                "name": name,
                "description": description,
                "admin_password": admin_password,
                "tracker_password": tracker_password,
                "archived": False,
                "created": time.time(),
                "created_iso": datetime.now().isoformat()
            }
            self._save_events()
            # Create event data directory
            self._ensure_event_dir(eid)
            print(f"[EVENTS] Created event {eid}: {name}")
            return eid

    def update_event(self, eid: int, updates: dict) -> bool:
        """Update event properties (name, description, archived, passwords)."""
        with self._lock:
            if eid not in self.events:
                return False
            event = self.events[eid]
            # Only allow updating certain fields
            allowed_fields = ['name', 'description', 'archived',
                              'admin_password', 'tracker_password']
            for field in allowed_fields:
                if field in updates:
                    event[field] = updates[field]
            event['updated'] = time.time()
            event['updated_iso'] = datetime.now().isoformat()
            self._save_events()
            print(f"[EVENTS] Updated event {eid}: {updates}")
            return True

    def _ensure_event_dir(self, eid: int):
        """Ensure event data directory exists."""
        event_dir = self.html_dir / str(eid)
        logs_dir = event_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        print(f"[EVENTS] Ensured directory exists: {event_dir}")

    def get_event_data_dir(self, eid: int) -> Path:
        """Get data directory for event, creating if needed."""
        event_dir = self.html_dir / str(eid)
        if not event_dir.exists():
            self._ensure_event_dir(eid)
        return event_dir


def write_current_positions(positions: dict, positions_file: Path, user_overrides: dict | None = None):
    """Write current positions to a JSON file for web UI consumption."""
    # Apply user overrides for display (name, role, hidden)
    display_positions = {}
    for sailor_id, pos in positions.items():
        display_pos = pos.copy()
        if user_overrides and sailor_id in user_overrides:
            override = user_overrides[sailor_id]
            if 'name' in override:
                display_pos['name'] = override['name']
            if 'role' in override:
                display_pos['role'] = override['role']
            if override.get('hidden'):
                display_pos['hidden'] = True
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
        print(f"[WARNING] Failed to write positions file: {e}")


class DailyLogger:
    """Handles daily log file rotation."""

    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.current_date = None
        self.log_fh = None
        self._open_log_for_today()

    def _get_log_filename(self, d: date) -> Path:
        return self.log_dir / f"{d.strftime('%Y_%m_%d')}.jsonl"

    def _open_log_for_today(self):
        today = date.today()
        if self.current_date != today:
            if self.log_fh:
                self.log_fh.close()
            self.current_date = today
            log_path = self._get_log_filename(today)
            self.log_fh = open(log_path, 'a')
            print(f"Logging to: {log_path}")

    def write(self, entry: dict):
        """Write a log entry, rolling over at midnight if needed."""
        self._open_log_for_today()
        self.log_fh.write(json.dumps(entry) + "\n")
        self.log_fh.flush()

    def close(self):
        if self.log_fh:
            self.log_fh.close()
            self.log_fh = None

    def clear_today(self):
        """Clear today's log file by rotating it to .1, .2, etc."""
        self._open_log_for_today()
        if self.log_fh:
            self.log_fh.close()
            self.log_fh = None
        log_path = self._get_log_filename(date.today())
        # Rotate the file instead of truncating
        rotate_file(log_path)
        # Open a fresh log file
        self.log_fh = open(log_path, 'a')
        print(f"Cleared track log: {log_path}")


class PositionTracker:
    """Handles position tracking state and processing."""

    def __init__(self, positions_file: Path | None, daily_logger: DailyLogger | None):
        self.positions_file = positions_file
        self.daily_logger = daily_logger
        self.current_positions: dict[str, dict] = {}
        self.last_timestamp: dict[str, int] = {}
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
                    # Restore timestamp tracking for duplicate detection
                    if pos.get("ts"):
                        self.last_timestamp[sailor_id] = pos["ts"]
            print(f"[STARTUP] Loaded {len(sailors)} positions from {positions_path}")
        except Exception as e:
            print(f"[STARTUP] Could not load positions file: {e}")

    def clear(self):
        """Clear all position state."""
        with self._lock:
            self.current_positions.clear()
            self.last_timestamp.clear()
        print("[ADMIN] Cleared internal position state")

    def process_position(self, sailor_id: str, lat: float, lon: float, speed: float,
                         heading: int, ts: int, assist: bool, battery: int, signal: int,
                         role: str, version: str, flags: dict, src_ip: str, source: str = "UDP",
                         battery_drain_rate: float | None = None, heart_rate: int | None = None,
                         os_version: str | None = None, skip_log: bool = False) -> bool:
        """
        Process a position update from any source (UDP or HTTP).
        Returns True if this was a new position, False if duplicate.
        """
        recv_time = time.time()

        with self._lock:
            # Check for duplicate using timestamp
            is_dup = False
            if sailor_id in self.last_timestamp:
                if ts <= self.last_timestamp[sailor_id]:
                    is_dup = True

            if not is_dup:
                self.last_timestamp[sailor_id] = ts

        # Format output
        dup_marker = " [DUP]" if is_dup else ""
        assist_marker = " *** ASSIST REQUESTED ***" if assist else ""
        bat_str = f"{battery}%" if battery >= 0 else "?"
        sig_str = f"{signal}/4" if signal >= 0 else "?"

        log_line = (
            f"[{sailor_id}] "
            f"pos={format_position(lat, lon)} "
            f"spd={speed:.1f}kn hdg={heading:03d}° "
            f"bat={bat_str} sig={sig_str} "
            f"ver={version} "
            f"time={format_timestamp(ts)} "
            f"[{source}] "
            f"ip={src_ip}"
            f"{dup_marker}{assist_marker}"
        )
        print(log_line)

        if assist:
            print("!" * 60)
            print(f"!!! SAILOR {sailor_id} REQUESTING ASSISTANCE !!!")
            print(f"!!! Position: {format_position(lat, lon)}")
            print("!" * 60)

        # Update current positions (only if not a duplicate)
        if not is_dup:
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
                if battery_drain_rate is not None:
                    pos_data["bdr"] = battery_drain_rate
                if heart_rate is not None and heart_rate > 0:
                    pos_data["hr"] = heart_rate
                if os_version:
                    pos_data["os"] = os_version
                self.current_positions[sailor_id] = pos_data

            # Write current positions file
            if self.positions_file:
                write_current_positions(self.current_positions, self.positions_file, _user_overrides)

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
                if battery_drain_rate is not None:
                    track_entry["bdr"] = battery_drain_rate
                if heart_rate is not None and heart_rate > 0:
                    track_entry["hr"] = heart_rate
                if os_version:
                    track_entry["os"] = os_version
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

        # Create daily logger
        self.daily_logger = DailyLogger(self.log_dir)

        # Load user overrides
        self.user_overrides = load_user_overrides(self.users_file)

        # Create position tracker
        self.position_tracker = PositionTracker(self.positions_file, self.daily_logger)

        # Ensure current_positions.json exists
        if not self.positions_file.exists():
            write_current_positions({}, self.positions_file, self.user_overrides)

        print(f"[EVENT {eid}] Initialized tracker for '{event_config.get('name', 'Unnamed')}'")

    def process_position(self, sailor_id: str, lat: float, lon: float, speed: float,
                         heading: int, ts: int, assist: bool, battery: int, signal: int,
                         role: str, version: str, flags: dict, src_ip: str, source: str = "UDP",
                         battery_drain_rate: float | None = None, heart_rate: int | None = None,
                         os_version: str | None = None, skip_log: bool = False,
                         pos_array: list | None = None) -> bool:
        """Process a position update for this event."""
        recv_time = time.time()

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
            if battery_drain_rate is not None:
                track_entry["bdr"] = battery_drain_rate
            if heart_rate is not None and heart_rate > 0:
                track_entry["hr"] = heart_rate
            if os_version:
                track_entry["os"] = os_version
            self.daily_logger.write(track_entry)

        # Process through position tracker
        # We pass user_overrides via the global for now (will refactor later)
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
            skip_log=has_batch or skip_log
        )

        # Write positions with event-specific user overrides
        if result and self.positions_file:
            write_current_positions(
                self.position_tracker.current_positions,
                self.positions_file,
                self.user_overrides
            )

        return result

    def clear_tracks(self):
        """Clear tracks for this event."""
        if self.daily_logger:
            self.daily_logger.clear_today()
        if self.positions_file and self.positions_file.exists():
            self.positions_file.unlink()
        self.position_tracker.clear()
        # Recreate empty positions file
        write_current_positions({}, self.positions_file, self.user_overrides)
        print(f"[EVENT {self.eid}] Tracks cleared")

    def close(self):
        """Clean up resources."""
        if self.daily_logger:
            self.daily_logger.close()


# Global references for HTTP handler to access
# Multi-event mode globals
_event_manager: EventManager | None = None
_event_trackers: dict[int, EventTracker] = {}  # eid -> EventTracker
_event_trackers_lock = threading.Lock()

# Legacy single-event mode globals (for backwards compatibility)
_daily_logger: DailyLogger | None = None
_position_tracker: PositionTracker | None = None
_admin_password: str = "admin"
_tracker_password: str | None = None  # Password for UDP tracker packets (None = no password required)
_course_file: Path | None = None
_users_file: Path | None = None
_user_overrides: dict[str, dict] = {}  # id -> {"name": "...", "role": "..."}

# Rate limiting for password guessing protection
# Maps IP address -> timestamp of last failed auth attempt
_failed_auth_times: dict[str, float] = {}
_RATE_LIMIT_SECONDS = 5.0


def is_rate_limited(ip: str) -> bool:
    """Check if an IP is rate limited due to recent failed auth."""
    if ip in _failed_auth_times:
        elapsed = time.time() - _failed_auth_times[ip]
        if elapsed < _RATE_LIMIT_SECONDS:
            return True
    return False


def record_failed_auth(ip: str):
    """Record a failed authentication attempt for rate limiting."""
    _failed_auth_times[ip] = time.time()


def get_event_tracker(eid: int) -> EventTracker | None:
    """Get or create an EventTracker for the given event ID."""
    global _event_trackers

    if not _event_manager:
        return None

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
            print(f"Warning: Could not load users file: {e}")
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
    print(f"[ADMIN] Saved user overrides: {len(overrides)} users")


_static_dir: Path | None = None
_positions_file: Path | None = None


class AdminHTTPHandler(BaseHTTPRequestHandler):
    """HTTP handler for admin API endpoints and optional static file serving."""
    
    def log_message(self, format, *args):
        """Override to prefix with [HTTP]"""
        print(f"[HTTP] {args[0]}")
    
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
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self._send_json({"error": "Not found"}, 404)
    
    def _get_client_ip(self) -> str:
        """Get client IP address, preferring X-Forwarded-For for proxied requests."""
        return self.headers.get('X-Forwarded-For', self.client_address[0])

    def _check_auth(self) -> bool:
        """Check admin password from header with rate limiting (legacy single-event mode)."""
        client_ip = self._get_client_ip()

        # Check rate limiting first
        if is_rate_limited(client_ip):
            print(f"[HTTP] Admin auth rate-limited for {client_ip}")
            return False

        password = self.headers.get('X-Admin-Password', '')
        if password != _admin_password:
            record_failed_auth(client_ip)
            print(f"[HTTP] Admin auth failed from {client_ip}")
            return False
        return True

    def _check_manager_auth(self) -> bool:
        """Check manager password from header with rate limiting."""
        client_ip = self._get_client_ip()

        if is_rate_limited(client_ip):
            print(f"[HTTP] Manager auth rate-limited for {client_ip}")
            return False

        if not _event_manager:
            print(f"[HTTP] Manager auth failed - multi-event mode not enabled")
            return False

        password = self.headers.get('X-Manager-Password', '')
        if password != _event_manager.manager_password:
            record_failed_auth(client_ip)
            print(f"[HTTP] Manager auth failed from {client_ip}")
            return False
        return True

    def _check_event_admin_auth(self, eid: int) -> bool:
        """Check per-event admin password from header with rate limiting."""
        client_ip = self._get_client_ip()

        if is_rate_limited(client_ip):
            print(f"[HTTP] Event {eid} admin auth rate-limited for {client_ip}")
            return False

        if not _event_manager:
            # Fall back to legacy mode
            return self._check_auth()

        event = _event_manager.get_event(eid)
        if not event:
            print(f"[HTTP] Event {eid} not found")
            return False

        password = self.headers.get('X-Admin-Password', '')
        if password != event.get('admin_password', ''):
            record_failed_auth(client_ip)
            print(f"[HTTP] Event {eid} admin auth failed from {client_ip}")
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
        
        if path == '/api/course':
            # Return current course (public endpoint)
            if _course_file and _course_file.exists():
                try:
                    with open(_course_file, 'r') as f:
                        course = json.load(f)
                    self._send_json(course)
                except Exception as e:
                    self._send_json({"error": str(e)}, 500)
            else:
                self._send_json({"course": None})
        
        elif path == '/api/auth/check':
            # Check if password is correct
            if self._check_auth():
                self._send_json({"authenticated": True})
            else:
                self._send_json({"authenticated": False}, 401)

        elif path == '/api/users':
            # Return user overrides (admin only) - legacy single-event mode
            if not self._check_auth():
                self._send_json({"error": "Unauthorized"}, 401)
                return
            self._send_json({"users": _user_overrides})

        elif path == '/api/events':
            # Return list of active events (public endpoint)
            if _event_manager:
                self._send_json({"events": _event_manager.get_public_events()})
            else:
                # Legacy mode - return single default event
                self._send_json({"events": [{"eid": 1, "name": "Default Event", "description": ""}]})

        elif path == '/api/manage/events':
            # Return full event list with details (manager only)
            if not self._check_manager_auth():
                self._send_json({"error": "Unauthorized"}, 401)
                return
            if _event_manager:
                self._send_json({"events": _event_manager.get_all_events()})
            else:
                self._send_json({"error": "Multi-event mode not enabled"}, 400)

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
        if _event_manager:
            event = _event_manager.get_event(eid)
            if not event:
                self._send_json({"error": f"Event {eid} not found"}, 404)
                return
        else:
            # Legacy mode - only allow eid=1
            if eid != 1:
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
            elif _course_file and _course_file.exists() and eid == 1:
                # Fall back to legacy course file for event 1
                try:
                    with open(_course_file, 'r') as f:
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
                self._send_json({"users": tracker.user_overrides})
            else:
                self._send_json({"users": {}})

        else:
            self._send_json({"error": "Not found"}, 404)

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
                    tmp_file = tracker.course_file.with_suffix('.tmp')
                    with open(tmp_file, 'w') as f:
                        json.dump(course, f, indent=2)
                    tmp_file.rename(tracker.course_file)
                    print(f"[EVENT {eid}] Course saved: {len(course.get('marks', []))} marks")
                    self._send_json({"success": True})
                else:
                    self._send_json({"error": "Could not get event tracker"}, 500)

            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON"}, 400)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif subpath.startswith('/admin/user/'):
            # Create or update a user override for this event
            user_id = subpath[len('/admin/user/'):]
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

                if override:
                    tracker.user_overrides[user_id] = override
                    save_user_overrides(tracker.users_file, tracker.user_overrides)
                    # Refresh positions file
                    write_current_positions(
                        tracker.position_tracker.current_positions,
                        tracker.positions_file,
                        tracker.user_overrides
                    )
                    print(f"[EVENT {eid}] User override set for {user_id}: {override}")
                    self._send_json({"success": True, "user_id": user_id, "override": override})
                else:
                    self._send_json({"error": "No valid fields (name, role)"}, 400)

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
                print(f"[EVENT {eid}] Course deleted (rotated)")
            self._send_json({"success": True})

        elif subpath.startswith('/admin/user/'):
            user_id = subpath[len('/admin/user/'):]
            if not user_id:
                self._send_json({"error": "User ID required"}, 400)
                return

            tracker = get_event_tracker(eid)
            if tracker and user_id in tracker.user_overrides:
                del tracker.user_overrides[user_id]
                save_user_overrides(tracker.users_file, tracker.user_overrides)
                write_current_positions(
                    tracker.position_tracker.current_positions,
                    tracker.positions_file,
                    tracker.user_overrides
                )
                print(f"[EVENT {eid}] User override removed for {user_id}")
            self._send_json({"success": True, "user_id": user_id})

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

        # Legacy admin endpoints require admin auth
        if not self._check_auth():
            self._send_json({"error": "Unauthorized"}, 401)
            return

        if path == '/api/admin/clear-tracks':
            # Clear today's track log and current positions
            if _daily_logger:
                _daily_logger.clear_today()
                # Also remove current_positions.json to clear map
                if _positions_file and _positions_file.exists():
                    _positions_file.unlink()
                    print(f"[ADMIN] Removed {_positions_file}")
                # Clear internal state via position tracker
                if _position_tracker:
                    _position_tracker.clear()
                self._send_json({"success": True, "message": "Tracks cleared"})
            else:
                self._send_json({"error": "Track logging not enabled"}, 400)

        elif path == '/api/admin/course':
            # Save course
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                course = json.loads(body)

                # Add timestamp
                course['updated'] = time.time()
                course['updated_iso'] = datetime.now().isoformat()

                if _course_file:
                    # Write atomically
                    tmp_file = _course_file.with_suffix('.tmp')
                    with open(tmp_file, 'w') as f:
                        json.dump(course, f, indent=2)
                    tmp_file.rename(_course_file)
                    print(f"[ADMIN] Course saved: {len(course.get('marks', []))} marks")
                    self._send_json({"success": True})
                else:
                    self._send_json({"error": "Course file not configured"}, 500)

            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON"}, 400)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path.startswith('/api/admin/user/'):
            # Create or update a user override
            user_id = path[len('/api/admin/user/'):]
            if not user_id:
                self._send_json({"error": "User ID required"}, 400)
                return
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                data = json.loads(body)

                global _user_overrides
                # Only allow name, role, and hidden overrides
                override = {}
                if 'name' in data:
                    override['name'] = str(data['name'])
                if 'role' in data and data['role'] in ('sailor', 'support', 'spectator'):
                    override['role'] = data['role']
                if 'hidden' in data:
                    override['hidden'] = bool(data['hidden'])

                if override:
                    _user_overrides[user_id] = override
                    if _users_file:
                        save_user_overrides(_users_file, _user_overrides)
                    # Refresh current positions to apply the override
                    if _position_tracker and _position_tracker.positions_file:
                        write_current_positions(
                            _position_tracker.current_positions,
                            _position_tracker.positions_file,
                            _user_overrides
                        )
                    print(f"[ADMIN] User override set for {user_id}: {override}")
                    self._send_json({"success": True, "user_id": user_id, "override": override})
                else:
                    self._send_json({"error": "No valid fields (name, role)"}, 400)

            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON"}, 400)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

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

            # Extract fields with defaults (same as UDP handler)
            sailor_id = packet.get("id", "???")
            seq = packet.get("sq", 0)
            ts = packet.get("ts", 0)
            speed = packet.get("spd", 0.0)
            heading = packet.get("hdg", 0)
            assist = packet.get("ast", False)
            battery = packet.get("bat", -1)
            signal = packet.get("sig", -1)
            heart_rate = packet.get("hr")  # Heart rate in bpm (optional, from Wear OS)
            role = packet.get("role", "sailor")
            version = packet.get("ver", "?")
            flags = packet.get("flg", {})
            battery_drain_rate = packet.get("bdr")
            os_version = packet.get("os")  # OS version string (optional)

            # Extract event ID (default to 1 for backwards compatibility)
            eid = packet.get("eid", 1)

            # Multi-event mode: look up event and check per-event password
            if _event_manager:
                event = _event_manager.get_event(eid)
                if not event:
                    print(f"[POST] Event {eid} not found for {sailor_id}")
                    self._send_json({"ack": seq, "ts": int(recv_time), "error": "event", "msg": f"Event {eid} not found"}, 404)
                    return
                if event.get('archived'):
                    print(f"[POST] Event {eid} is archived, rejecting {sailor_id}")
                    self._send_json({"ack": seq, "ts": int(recv_time), "error": "event", "msg": f"Event {eid} is archived"}, 400)
                    return

                # Check per-event tracker password
                event_tracker_pwd = event.get('tracker_password', '')
                if event_tracker_pwd:
                    if is_rate_limited(client_ip):
                        print(f"[POST] Auth rate-limited for {sailor_id} from {client_ip}")
                        self._send_json({"ack": seq, "ts": int(recv_time), "error": "auth", "msg": "Invalid password"}, 401)
                        return
                    packet_pwd = packet.get("pwd", "")
                    if packet_pwd != event_tracker_pwd:
                        record_failed_auth(client_ip)
                        print(f"[POST] Auth failed for {sailor_id} (event {eid}) from {client_ip}")
                        self._send_json({"ack": seq, "ts": int(recv_time), "error": "auth", "msg": "Invalid password"}, 401)
                        return

                # Get or create the event tracker
                tracker = get_event_tracker(eid)
                if not tracker:
                    print(f"[POST] ERROR: Could not get tracker for event {eid}")
                    self._send_json({"error": "Could not initialize event tracker"}, 500)
                    return

            else:
                # Legacy single-event mode
                # Check rate limiting and password if required
                if _tracker_password:
                    if is_rate_limited(client_ip):
                        print(f"[POST] Auth rate-limited for {sailor_id} from {client_ip}")
                        self._send_json({"ack": seq, "ts": int(recv_time), "error": "auth", "msg": "Invalid password"}, 401)
                        return
                    packet_pwd = packet.get("pwd", "")
                    if packet_pwd != _tracker_password:
                        record_failed_auth(client_ip)
                        print(f"[POST] Auth failed for {sailor_id} from {client_ip} (bad password)")
                        self._send_json({"ack": seq, "ts": int(recv_time), "error": "auth", "msg": "Invalid password"}, 401)
                        return

                if not _position_tracker:
                    print(f"[POST] ERROR: Position tracking not enabled")
                    self._send_json({"error": "Position tracking not enabled"}, 500)
                    return
                tracker = None  # Will use legacy globals

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

            # Process through event tracker (multi-event) or legacy tracker
            if tracker:
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
                    pos_array=pos_array
                )
            else:
                # Legacy single-event mode
                has_batch = pos_array and isinstance(pos_array, list) and len(pos_array) > 1
                if has_batch and _daily_logger:
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
                    if battery_drain_rate is not None:
                        track_entry["bdr"] = battery_drain_rate
                    if heart_rate is not None and heart_rate > 0:
                        track_entry["hr"] = heart_rate
                    if os_version:
                        track_entry["os"] = os_version
                    _daily_logger.write(track_entry)

                _position_tracker.process_position(
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
                    skip_log=has_batch
                )

            # Send ACK response (same format as UDP)
            self._send_json({"ack": seq, "ts": int(recv_time)})

        except json.JSONDecodeError as e:
            print(f"[POST] JSON PARSE ERROR from {client_ip}: {e}")
            self._send_json({"error": "Invalid JSON"}, 400)
        except Exception as e:
            print(f"[POST] ERROR from {client_ip}: {e}")
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

            print(f"[UDID] Received {content_length} bytes, Content-Type: {content_type}")
            print(f"[UDID] First 100 bytes: {body[:100]}")

            data = None

            # Try parsing as raw plist first
            try:
                data = plistlib.loads(body)
                print(f"[UDID] Parsed as raw plist")
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
                        print(f"[UDID] Parsed from CMS envelope")
                    else:
                        print(f"[UDID] openssl failed: {result.stderr.decode()}")
                except Exception as e:
                    print(f"[UDID] CMS extraction failed: {e}")

            if data is None:
                print(f"[UDID] Could not parse plist from body")
                self.send_response(302)
                self.send_header('Location', '/install/flutter-ios.html?error=parse')
                self.end_headers()
                return

            # Extract UDID and device info
            udid = data.get('UDID', '')
            product = data.get('PRODUCT', '')
            version = data.get('VERSION', '')
            serial = data.get('SERIAL', '')

            print(f"[UDID] Received: UDID={udid}, Product={product}, Version={version}")

            # Redirect back to install page with UDID in URL
            redirect_url = f'/install/flutter-ios.html?udid={udid}&device={product}'

            self.send_response(301)
            self.send_header('Location', redirect_url)
            self.end_headers()

        except Exception as e:
            print(f"[UDID] Error handling request: {e}")
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

            eid = _event_manager.create_event(
                name=name,
                description=description,
                admin_password=admin_password,
                tracker_password=tracker_password
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

                if not _event_manager:
                    self._send_json({"error": "Multi-event mode not enabled"}, 400)
                    return

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

        # Legacy endpoints require admin auth
        if not self._check_auth():
            self._send_json({"error": "Unauthorized"}, 401)
            return

        if path == '/api/admin/course':
            # Delete course by rotating to .1, .2, etc.
            if _course_file and _course_file.exists():
                rotate_file(_course_file)
                print("[ADMIN] Course deleted (rotated)")
            self._send_json({"success": True})

        elif path.startswith('/api/admin/user/'):
            # Delete a user override
            user_id = path[len('/api/admin/user/'):]
            if not user_id:
                self._send_json({"error": "User ID required"}, 400)
                return
            global _user_overrides
            if user_id in _user_overrides:
                del _user_overrides[user_id]
                if _users_file:
                    save_user_overrides(_users_file, _user_overrides)
                # Refresh current positions to remove the override
                if _position_tracker and _position_tracker.positions_file:
                    write_current_positions(
                        _position_tracker.current_positions,
                        _position_tracker.positions_file,
                        _user_overrides
                    )
                print(f"[ADMIN] User override removed for {user_id}")
            self._send_json({"success": True, "user_id": user_id})

        else:
            self._send_json({"error": "Not found"}, 404)


def run_http_server(port: int):
    """Run HTTP server in a thread."""
    server = ThreadingHTTPServer(('0.0.0.0', port), AdminHTTPHandler)
    print(f"Admin HTTP server listening on port {port}")
    server.serve_forever()


def run_summary_generator(log_dir: Path, interval: int = 60):
    """Background thread to periodically generate log summaries."""
    print(f"[SUMMARY] Background generator started (interval: {interval}s)")
    while True:
        try:
            updated = generate_log_summaries(log_dir)
            if updated > 0:
                print(f"[SUMMARY] Updated {updated} summary file(s)")
        except Exception as e:
            print(f"[SUMMARY] Error in background generator: {e}")
        time.sleep(interval)


def run_log_compressor(log_dir: Path, interval: int = 10, live_window_minutes: int = 20):
    """Background thread to compress log files for efficient serving.

    Creates two compressed files every `interval` seconds if source changed:
    1. YYYY_MM_DD_live.jsonl.gz - Rolling window of last `live_window_minutes` (for live tracking)
    2. YYYY_MM_DD.jsonl.gz - Full compressed log (for historical review)

    Uses atomic writes (temp file + rename) for concurrent read safety.
    """
    import gzip

    print(f"[COMPRESS] Background compressor started (interval: {interval}s, live window: {live_window_minutes}min)")
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
                    print(f"[COMPRESS] Updated: live={live_size:,}B ({live_lines}/{total_lines} entries), "
                          f"full={full_size:,}B (from {orig_size:,}B)")

        except Exception as e:
            print(f"[COMPRESS] Error: {e}")
        time.sleep(interval)


def run_server(port: int, log_file: Path | None, positions_file: Path | None, log_dir: Path | None,
               http_port: int | None = None, admin_password: str = "admin", course_file: Path | None = None,
               static_dir: Path | None = None,
               users_file: Path | None = None, tracker_password: str | None = None,
               manager_password: str | None = None, events_file: Path | None = None):
    """Main server loop.

    If manager_password is provided, runs in multi-event mode where:
    - Events are managed via events.json (or --events-file)
    - Each event has its own data directory under static_dir/{eid}/
    - Per-event admin and tracker passwords are used

    Otherwise, runs in legacy single-event mode with global passwords.
    """
    global _daily_logger, _position_tracker, _admin_password, _tracker_password
    global _course_file, _static_dir, _positions_file, _users_file, _user_overrides
    global _event_manager

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))

    print(f"Tracker server listening on UDP port {port}")
    print("Waiting for packets...\n")

    # Multi-event mode initialization
    if manager_password:
        if not static_dir:
            print("[ERROR] Multi-event mode requires --static-dir to be set")
            return
        if not events_file:
            events_file = Path("events.json")

        print(f"[EVENTS] Multi-event mode enabled")
        print(f"[EVENTS] Events file: {events_file}")
        print(f"[EVENTS] HTML directory: {static_dir}")

        _event_manager = EventManager(events_file, static_dir)
        _event_manager.manager_password = manager_password

        # In multi-event mode, legacy globals are not used for data
        # but we still need static_dir for serving files
        _static_dir = static_dir
        _admin_password = ""  # Not used in multi-event mode
        _tracker_password = None
        _course_file = None
        _positions_file = None
        _users_file = None
        _user_overrides = {}
        daily_logger = None
        position_tracker = None

        print(f"[EVENTS] Loaded {len(_event_manager.events)} events\n")

    else:
        # Legacy single-event mode
        _event_manager = None

        if positions_file:
            print(f"Writing current positions to: {positions_file}\n")

        # Daily logger for track history
        daily_logger = None
        if log_dir:
            daily_logger = DailyLogger(log_dir)
            print(f"Track logs directory: {log_dir}\n")

        # Load user overrides
        user_overrides = {}
        if users_file:
            user_overrides = load_user_overrides(users_file)
            print(f"Users file: {users_file} ({len(user_overrides)} overrides)\n")

        # Create position tracker
        position_tracker = PositionTracker(positions_file, daily_logger)

        # Ensure current_positions.json exists (so web client doesn't get 404 on startup)
        if positions_file and not positions_file.exists():
            write_current_positions({}, positions_file, user_overrides)
            print(f"[STARTUP] Created empty positions file: {positions_file}")

        # Set up globals for HTTP handler
        _daily_logger = daily_logger
        _position_tracker = position_tracker
        _admin_password = admin_password
        _tracker_password = tracker_password
        _course_file = course_file
        _static_dir = static_dir
        _positions_file = positions_file
        _users_file = users_file
        _user_overrides = user_overrides

        if course_file:
            print(f"Course file: {course_file}\n")

        if static_dir:
            print(f"Serving static files from: {static_dir}\n")

        if tracker_password:
            print(f"Tracker password: enabled (clients must send 'pwd' field)\n")

    if http_port:
        if _event_manager:
            print(f"Multi-event API: http://SERVER:{http_port}/api/events\n")

    # Start HTTP server if enabled
    if http_port:
        http_thread = threading.Thread(target=run_http_server, args=(http_port,), daemon=True)
        http_thread.start()

    # Start background summary generator if track logging is enabled (legacy mode)
    if log_dir and not _event_manager:
        summary_thread = threading.Thread(target=run_summary_generator, args=(log_dir,), daemon=True)
        summary_thread.start()

        # Start background log compressor for efficient .gz serving
        compressor_thread = threading.Thread(target=run_log_compressor, args=(log_dir,), daemon=True)
        compressor_thread.start()

    # Open legacy log file if specified
    log_fh = None
    if log_file:
        log_fh = open(log_file, "a")
        print(f"Legacy log: {log_file}\n")

    try:
        while True:
            data, addr = sock.recvfrom(1024)
            recv_time = time.time()
            client_ip = addr[0]

            try:
                packet = json.loads(data.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                print(f"[{addr[0]}:{addr[1]}] Invalid packet: {e}")
                continue

            # Extract fields with defaults
            sailor_id = packet.get("id", "???")
            seq = packet.get("sq", 0)
            ts = packet.get("ts", 0)
            speed = packet.get("spd", 0.0)
            heading = packet.get("hdg", 0)
            assist = packet.get("ast", False)
            battery = packet.get("bat", -1)
            signal = packet.get("sig", -1)
            heart_rate = packet.get("hr")  # Heart rate in bpm (optional, from Wear OS)
            role = packet.get("role", "sailor")
            version = packet.get("ver", "?")
            flags = packet.get("flg", {})
            battery_drain_rate = packet.get("bdr")  # Battery drain rate %/hr
            os_version = packet.get("os")  # OS version string (optional)

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

            # Multi-event mode: look up event and check per-event password
            if _event_manager:
                event = _event_manager.get_event(eid)
                if not event:
                    print(f"[UDP] Event {eid} not found for {sailor_id}")
                    error_ack = json.dumps({"ack": seq, "ts": int(recv_time), "error": "event", "msg": f"Event {eid} not found"}).encode("utf-8")
                    sock.sendto(error_ack, addr)
                    continue
                if event.get('archived'):
                    print(f"[UDP] Event {eid} is archived, rejecting {sailor_id}")
                    error_ack = json.dumps({"ack": seq, "ts": int(recv_time), "error": "event", "msg": f"Event {eid} is archived"}).encode("utf-8")
                    sock.sendto(error_ack, addr)
                    continue

                # Check per-event tracker password
                event_tracker_pwd = event.get('tracker_password', '')
                if event_tracker_pwd:
                    if is_rate_limited(client_ip):
                        print(f"[UDP] Auth rate-limited for {sailor_id} from {client_ip}")
                        error_ack = json.dumps({"ack": seq, "ts": int(recv_time), "error": "auth", "msg": "Invalid password"}).encode("utf-8")
                        sock.sendto(error_ack, addr)
                        continue
                    packet_pwd = packet.get("pwd", "")
                    if packet_pwd != event_tracker_pwd:
                        record_failed_auth(client_ip)
                        print(f"[UDP] Auth failed for {sailor_id} (event {eid}) from {client_ip}")
                        error_ack = json.dumps({"ack": seq, "ts": int(recv_time), "error": "auth", "msg": "Invalid password"}).encode("utf-8")
                        sock.sendto(error_ack, addr)
                        continue

                # Get or create the event tracker
                event_tracker = get_event_tracker(eid)
                if not event_tracker:
                    print(f"[UDP] ERROR: Could not get tracker for event {eid}")
                    error_ack = json.dumps({"ack": seq, "ts": int(recv_time), "error": "server", "msg": "Could not initialize event tracker"}).encode("utf-8")
                    sock.sendto(error_ack, addr)
                    continue

                # Send ACK
                ack = json.dumps({"ack": seq, "ts": int(recv_time)}).encode("utf-8")
                sock.sendto(ack, addr)

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
                    pos_array=pos_array
                )

            else:
                # Legacy single-event mode
                # Check rate limiting and password if required
                if tracker_password:
                    if is_rate_limited(client_ip):
                        print(f"[UDP] Auth rate-limited for {sailor_id} from {client_ip}")
                        error_ack = json.dumps({"ack": seq, "ts": int(recv_time), "error": "auth", "msg": "Invalid password"}).encode("utf-8")
                        sock.sendto(error_ack, addr)
                        continue

                    packet_pwd = packet.get("pwd", "")
                    if packet_pwd != tracker_password:
                        record_failed_auth(client_ip)
                        print(f"[UDP] Auth failed for {sailor_id} from {client_ip} (bad password)")
                        error_ack = json.dumps({"ack": seq, "ts": int(recv_time), "error": "auth", "msg": "Invalid password"}).encode("utf-8")
                        sock.sendto(error_ack, addr)
                        continue

                # Send ACK
                ack = json.dumps({"ack": seq, "ts": int(recv_time)}).encode("utf-8")
                sock.sendto(ack, addr)

                # If 1Hz array format, log as single entry with pos array (more compact)
                has_batch = pos_array and isinstance(pos_array, list) and len(pos_array) > 1
                if has_batch and daily_logger:
                    track_entry = {
                        "id": sailor_id,
                        "ts": ts,  # timestamp of last position (for sorting)
                        "recv_ts": recv_time,
                        "pos": pos_array,  # [[ts, lat, lon], ...] - compact array format
                        "spd": speed,
                        "hdg": heading,
                        "ast": assist,
                        "bat": battery,
                        "sig": signal,
                        "role": role,
                        "ver": version,
                        "flg": flags
                    }
                    if battery_drain_rate is not None:
                        track_entry["bdr"] = battery_drain_rate
                    if heart_rate is not None and heart_rate > 0:
                        track_entry["hr"] = heart_rate
                    if os_version:
                        track_entry["os"] = os_version
                    daily_logger.write(track_entry)

                # Process position through shared tracker (updates live display)
                # skip_log if we already logged the batch above
                position_tracker.process_position(
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
                    skip_log=has_batch
                )

            # Write to legacy log file (JSON lines format for easy parsing later)
            if log_fh:
                log_entry = {
                    "recv_ts": recv_time,
                    "src_ip": addr[0],
                    "src_port": addr[1],
                    **packet
                }
                log_fh.write(json.dumps(log_entry) + "\n")
                log_fh.flush()

    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        sock.close()
        if log_fh:
            log_fh.close()
        if daily_logger:
            daily_logger.close()


def main():
    parser = argparse.ArgumentParser(description="Windsurfer Tracker UDP Server")
    parser.add_argument(
        "-p", "--port",
        type=int,
        default=41234,
        help="UDP port to listen on (default: 41234)"
    )
    parser.add_argument(
        "-l", "--log",
        type=Path,
        default=None,
        help="Legacy log file path (JSON lines format)"
    )
    parser.add_argument(
        "-c", "--current",
        type=Path,
        default=Path("current_positions.json"),
        help="Current positions file for web UI (default: current_positions.json)"
    )
    parser.add_argument(
        "--no-current",
        action="store_true",
        help="Disable current positions file"
    )
    parser.add_argument(
        "-d", "--log-dir",
        type=Path,
        default=Path("logs"),
        help="Directory for daily track logs (default: logs/)"
    )
    parser.add_argument(
        "--no-track-logs",
        action="store_true",
        help="Disable daily track logging"
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
        help="Disable HTTP admin API"
    )
    parser.add_argument(
        "--admin-password",
        type=str,
        default=None,
        help="Admin password for HTTP API (required)"
    )
    parser.add_argument(
        "--tracker-password",
        type=str,
        default=None,
        help="Password for tracker UDP packets (default: no password required)"
    )
    parser.add_argument(
        "--course-file",
        type=Path,
        default=Path("course.json"),
        help="Course file path (default: course.json)"
    )
    parser.add_argument(
        "--static-dir",
        type=Path,
        default=None,
        help="Directory to serve static files from (e.g., web UI)"
    )
    parser.add_argument(
        "--users-file",
        type=Path,
        default=Path("users.json"),
        help="User overrides file path (default: users.json)"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Root directory for data files (logs, course.json, users.json, current_positions.json)"
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
        default=Path("events.json"),
        help="Events configuration file (default: events.json)"
    )

    args = parser.parse_args()

    # If data-dir specified, make paths relative to it (unless explicitly overridden)
    if args.data_dir:
        data_dir = args.data_dir
        # Only override defaults (check if user explicitly set these)
        if args.current == Path("current_positions.json"):
            args.current = data_dir / "current_positions.json"
        if args.log_dir == Path("logs"):
            args.log_dir = data_dir / "logs"
        if args.course_file == Path("course.json"):
            args.course_file = data_dir / "course.json"
        if args.users_file == Path("users.json"):
            args.users_file = data_dir / "users.json"
        if args.events_file == Path("events.json"):
            args.events_file = data_dir / "events.json"
    positions_file = None if args.no_current else args.current
    log_dir = None if args.no_track_logs else args.log_dir
    http_port = None if args.no_http else (args.http_port or args.port)

    # Multi-event mode vs legacy mode password requirements
    if args.manager_password:
        # Multi-event mode - manager password provided
        if http_port is None:
            parser.error("--manager-password requires HTTP to be enabled")
    else:
        # Legacy single-event mode - require admin password if HTTP is enabled
        if http_port and not args.admin_password:
            parser.error("--admin-password is required when HTTP is enabled (use --no-http to disable, or use --manager-password for multi-event mode)")

    run_server(args.port, args.log, positions_file, log_dir,
               http_port=http_port, admin_password=args.admin_password or "",
               course_file=args.course_file, static_dir=args.static_dir,
               users_file=args.users_file,
               tracker_password=args.tracker_password,
               manager_password=args.manager_password,
               events_file=args.events_file)


if __name__ == "__main__":
    main()
