#!/usr/bin/env python3
"""
Windsurfer Tracker - UDP Server with HTTP Admin API
Receives position reports from sailor apps, sends ACKs, logs data.
Provides HTTP endpoints for admin functions and course management.
"""

import socket
import json
import time
import argparse
import os
import threading
from datetime import datetime, date
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
                         os_version: str | None = None) -> bool:
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

            # Write to daily track log
            if self.daily_logger:
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


# Global references for HTTP handler to access
_daily_logger: DailyLogger | None = None
_position_tracker: PositionTracker | None = None
_admin_password: str = "admin"
_owntracks_password: str | None = None  # Separate password for OwnTracks (None = use admin password)
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
    
    def _send_json(self, data: dict, status: int = 200):
        """Send JSON response."""
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'X-Admin-Password, Content-Type')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode('utf-8'))
    
    def _send_file(self, filepath: Path, content_type: str):
        """Send a static file."""
        try:
            with open(filepath, 'rb') as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', len(content))
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self._send_json({"error": "Not found"}, 404)
    
    def _get_client_ip(self) -> str:
        """Get client IP address, preferring X-Forwarded-For for proxied requests."""
        return self.headers.get('X-Forwarded-For', self.client_address[0])

    def _check_auth(self) -> bool:
        """Check admin password from header with rate limiting."""
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

    def _check_owntracks_auth(self) -> tuple[bool, str]:
        """Check OwnTracks authentication (HTTP Basic Auth) with rate limiting. Returns (success, reason)."""
        import base64
        client_ip = self._get_client_ip()

        # Check rate limiting first
        if is_rate_limited(client_ip):
            return False, "rate-limited"

        auth_header = self.headers.get('Authorization', '')
        if not auth_header:
            record_failed_auth(client_ip)
            return False, "no Authorization header"
        if not auth_header.startswith('Basic '):
            record_failed_auth(client_ip)
            return False, f"invalid auth type (expected Basic, got {auth_header.split()[0] if auth_header else 'none'})"
        try:
            credentials = base64.b64decode(auth_header[6:]).decode('utf-8')
            if ':' not in credentials:
                record_failed_auth(client_ip)
                return False, "malformed credentials (no colon separator)"
            username, password = credentials.split(':', 1)
            # Use OwnTracks password if set, otherwise fall back to admin password
            expected = _owntracks_password if _owntracks_password else _admin_password
            if password != expected:
                record_failed_auth(client_ip)
                return False, f"wrong password for user '{username}'"
            return True, "ok"
        except Exception as e:
            record_failed_auth(client_ip)
            return False, f"auth decode error: {e}"
    
    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'X-Admin-Password, Content-Type')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
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
            # Return user overrides (admin only)
            if not self._check_auth():
                self._send_json({"error": "Unauthorized"}, 401)
                return
            self._send_json({"users": _user_overrides})

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
    
    def do_POST(self):
        """Handle POST requests."""
        path = urlparse(self.path).path

        # Tracker endpoint - UDP fallback via HTTP POST
        if path == '/api/tracker':
            self._handle_tracker_post()
            return

        # OwnTracks endpoint - has its own auth
        if path == '/api/owntracks' or path == '/owntracks':
            self._handle_owntracks()
            return

        # iOS UDID collection endpoint - no auth required
        if path == '/api/udid':
            self._handle_udid_collection()
            return

        # Admin endpoints require admin auth
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
        Uses tracker password for authentication if configured.
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

            # Check rate limiting and password if required
            if _tracker_password:
                # Check rate limiting first
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

            if not _position_tracker:
                print(f"[POST] ERROR: Position tracking not enabled")
                self._send_json({"error": "Position tracking not enabled"}, 500)
                return

            # If 1Hz array format, log ALL positions FIRST (in chronological order)
            # This must happen before process_position to ensure correct timestamp ordering
            if pos_array and isinstance(pos_array, list) and len(pos_array) > 1 and _daily_logger:
                # batch_ts = timestamp of last position (when batch was sent)
                batch_ts = pos_array[-1][0] if len(pos_array[-1]) > 0 else None
                for pos in pos_array[:-1]:
                    if len(pos) >= 3:
                        pos_ts, pos_lat, pos_lon = pos[0], pos[1], pos[2]
                        track_entry = {
                            "id": sailor_id,
                            "ts": pos_ts,
                            "recv_ts": recv_time,
                            "lat": pos_lat,
                            "lon": pos_lon,
                            "spd": speed,
                            "hdg": heading,
                            "ast": assist,
                            "bat": battery,
                            "sig": signal,
                            "role": role,
                            "ver": version,
                            "flg": flags
                        }
                        if batch_ts is not None:
                            track_entry["batch_ts"] = batch_ts
                        if battery_drain_rate is not None:
                            track_entry["bdr"] = battery_drain_rate
                        _daily_logger.write(track_entry)

            # Process position through shared tracker (logs last position)
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
                os_version=os_version
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

    def _handle_owntracks(self):
        """Handle OwnTracks location updates."""
        # Get client info for logging
        client_ip = self.headers.get('X-Forwarded-For', self.client_address[0])
        user_agent = self.headers.get('User-Agent', 'unknown')

        # Check authentication
        auth_ok, auth_reason = self._check_owntracks_auth()
        if not auth_ok:
            print(f"[OwnTracks] AUTH FAILED from {client_ip}: {auth_reason} (User-Agent: {user_agent})")
            self.send_response(401)
            self.send_header('WWW-Authenticate', 'Basic realm="OwnTracks"')
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'[]')
            return

        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body)

            # OwnTracks sends different message types
            msg_type = data.get('_type', '')

            if msg_type != 'location':
                # Ignore non-location messages (waypoints, transitions, etc.)
                print(f"[OwnTracks] Ignoring message type '{msg_type}' from {client_ip}")
                self._send_json([])
                return

            if not _position_tracker:
                print(f"[OwnTracks] ERROR: Position tracking not enabled")
                self._send_json({"error": "Position tracking not enabled"}, 500)
                return

            # Map OwnTracks fields to internal format
            tid = data.get('tid', '??')  # 2-char tracker ID
            sailor_id = f"OT-{tid}"  # Prefix to distinguish OwnTracks clients

            # Extract username from topic field (e.g., "owntracks/user/AndrewOT" -> "AndrewOT")
            topic = data.get('topic', '')
            auto_set_name = False
            if topic and '/' in topic:
                topic_username = topic.rsplit('/', 1)[-1]
                # Auto-create name override if not already set
                if topic_username and sailor_id not in _user_overrides:
                    _user_overrides[sailor_id] = {'name': topic_username}
                    auto_set_name = True
                    if _users_file:
                        save_user_overrides(_users_file, _user_overrides)
                    print(f"[OwnTracks] Auto-set name for {sailor_id}: {topic_username}")

            lat = data.get('lat', 0.0)
            lon = data.get('lon', 0.0)
            ts = data.get('tst', int(time.time()))

            # vel is in km/h, convert to knots (÷ 1.852)
            speed_kmh = data.get('vel', 0)
            speed_kn = speed_kmh / 1.852 if speed_kmh and speed_kmh >= 0 else 0.0

            heading = data.get('cog', 0)  # Course over ground
            if heading is None or heading < 0:
                heading = 0

            battery = data.get('batt', -1)
            if battery is None:
                battery = -1

            # Default role for OwnTracks clients is sailor
            # Can be overridden via user overrides in Web UI
            role = "sailor"

            # Get client IP (prefer X-Forwarded-For for proxied requests)
            src_ip = client_ip

            # Process the position
            _position_tracker.process_position(
                sailor_id=sailor_id,
                lat=lat,
                lon=lon,
                speed=speed_kn,
                heading=int(heading),
                ts=ts,
                assist=False,  # OwnTracks doesn't have assist concept
                battery=battery,
                signal=-1,  # OwnTracks doesn't report signal
                role=role,
                version="owntracks",
                flags={},  # OwnTracks doesn't have battery saver info
                src_ip=src_ip,
                source="HTTP/OT"
            )

            # If we auto-set a name, refresh positions file to include it
            if auto_set_name and _position_tracker.positions_file:
                write_current_positions(
                    _position_tracker.current_positions,
                    _position_tracker.positions_file,
                    _user_overrides
                )

            # OwnTracks expects empty array on success
            self._send_json([])

        except json.JSONDecodeError as e:
            print(f"[OwnTracks] JSON PARSE ERROR from {client_ip}: {e}")
            self._send_json({"error": "Invalid JSON"}, 400)
        except Exception as e:
            print(f"[OwnTracks] ERROR from {client_ip}: {e}")
            self._send_json({"error": str(e)}, 500)
    
    def do_DELETE(self):
        """Handle DELETE requests."""
        path = urlparse(self.path).path
        
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


def run_server(port: int, log_file: Path | None, positions_file: Path | None, log_dir: Path | None,
               http_port: int | None = None, admin_password: str = "admin", course_file: Path | None = None,
               static_dir: Path | None = None, owntracks_password: str | None = None,
               users_file: Path | None = None, tracker_password: str | None = None):
    """Main server loop."""
    global _daily_logger, _position_tracker, _admin_password, _owntracks_password, _tracker_password
    global _course_file, _static_dir, _positions_file, _users_file, _user_overrides

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))

    print(f"Tracker server listening on UDP port {port}")
    print("Waiting for packets...\n")

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

    # Set up globals for HTTP handler
    _daily_logger = daily_logger
    _position_tracker = position_tracker
    _admin_password = admin_password
    _owntracks_password = owntracks_password
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
        print(f"OwnTracks endpoint: http://SERVER:{http_port}/api/owntracks\n")

    # Start HTTP server if enabled
    if http_port:
        http_thread = threading.Thread(target=run_http_server, args=(http_port,), daemon=True)
        http_thread.start()

    # Start background summary generator if track logging is enabled
    if log_dir:
        summary_thread = threading.Thread(target=run_summary_generator, args=(log_dir,), daemon=True)
        summary_thread.start()

    # Open legacy log file if specified
    log_fh = None
    if log_file:
        log_fh = open(log_file, "a")
        print(f"Legacy log: {log_file}\n")

    try:
        while True:
            data, addr = sock.recvfrom(1024)
            recv_time = time.time()

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

            # Check rate limiting and password if required
            if tracker_password:
                client_ip = addr[0]
                # Check rate limiting first
                if is_rate_limited(client_ip):
                    print(f"[UDP] Auth rate-limited for {sailor_id} from {client_ip}")
                    error_ack = json.dumps({"ack": seq, "ts": int(recv_time), "error": "auth", "msg": "Invalid password"}).encode("utf-8")
                    sock.sendto(error_ack, addr)
                    continue

                packet_pwd = packet.get("pwd", "")
                if packet_pwd != tracker_password:
                    record_failed_auth(client_ip)
                    print(f"[UDP] Auth failed for {sailor_id} from {client_ip} (bad password)")
                    # Send error ACK so client knows authentication failed
                    error_ack = json.dumps({"ack": seq, "ts": int(recv_time), "error": "auth", "msg": "Invalid password"}).encode("utf-8")
                    sock.sendto(error_ack, addr)
                    continue

            # Send ACK
            ack = json.dumps({"ack": seq, "ts": int(recv_time)}).encode("utf-8")
            sock.sendto(ack, addr)

            # If 1Hz array format, log ALL positions to daily track log FIRST (in chronological order)
            # This must happen before process_position to ensure correct timestamp ordering
            if pos_array and isinstance(pos_array, list) and len(pos_array) > 1 and daily_logger:
                # batch_ts = timestamp of last position (when batch was sent)
                batch_ts = pos_array[-1][0] if len(pos_array[-1]) > 0 else None
                # Log all positions EXCEPT the last one (which will be logged by process_position)
                for i, pos in enumerate(pos_array[:-1]):
                    if len(pos) >= 3:
                        pos_ts, pos_lat, pos_lon = pos[0], pos[1], pos[2]
                        track_entry = {
                            "id": sailor_id,
                            "ts": pos_ts,
                            "recv_ts": recv_time,
                            "lat": pos_lat,
                            "lon": pos_lon,
                            "spd": speed,
                            "hdg": heading,
                            "ast": assist,
                            "bat": battery,
                            "sig": signal,
                            "role": role,
                            "ver": version,
                            "flg": flags
                        }
                        if batch_ts is not None:
                            track_entry["batch_ts"] = batch_ts
                        if battery_drain_rate is not None:
                            track_entry["bdr"] = battery_drain_rate
                        daily_logger.write(track_entry)

            # Process position through shared tracker (uses last position for live display)
            # This also logs the last position to the daily log
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
                src_ip=addr[0],
                source="UDP",
                battery_drain_rate=battery_drain_rate,
                heart_rate=heart_rate,
                os_version=os_version
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
        "--owntracks-password",
        type=str,
        default=None,
        help="Password for OwnTracks HTTP Basic Auth (default: use admin password)"
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

    args = parser.parse_args()
    positions_file = None if args.no_current else args.current
    log_dir = None if args.no_track_logs else args.log_dir
    http_port = None if args.no_http else (args.http_port or args.port)

    # Require admin password if HTTP is enabled
    if http_port and not args.admin_password:
        parser.error("--admin-password is required when HTTP is enabled (use --no-http to disable)")

    run_server(args.port, args.log, positions_file, log_dir,
               http_port=http_port, admin_password=args.admin_password or "",
               course_file=args.course_file, static_dir=args.static_dir,
               owntracks_password=args.owntracks_password,
               users_file=args.users_file,
               tracker_password=args.tracker_password)


if __name__ == "__main__":
    main()
