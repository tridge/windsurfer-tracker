# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Windsurfer Tracker is a GPS tracking system for windsurfing races. It uses UDP for position reporting (optimized for poor mobile connections) and provides a web-based map UI for race organizers.

## Architecture

### Three Main Components

1. **Server** (`server/tracker_server.py`) - Python UDP server that:
   - Receives position reports on UDP port 41234
   - Sends acknowledgements back to clients
   - Writes `current_positions.json` for the web UI (atomic writes)
   - Maintains daily track logs in `logs/YYYY_MM_DD.jsonl` format
   - Runs an HTTP server on the same port for admin API and static file serving
   - Admin endpoints: `/api/admin/clear-tracks`, `/api/admin/course`, `/api/auth/check`
   - Public endpoints: `/api/course`
   - OwnTracks endpoint: `/api/owntracks` (HTTP POST with Basic Auth)

2. **Android App** (`android/`) - Native Kotlin app that:
   - Tracks GPS every 10 seconds
   - Sends UDP packets with position, speed, heading, battery, signal strength
   - Supports multiple roles: sailor, support, spectator
   - Has "Request Assist" emergency button

3. **Web UI** (`WebUI/index.html`) - Single-page Leaflet map application that:
   - Polls `current_positions.json` every 3 seconds
   - Displays different icons per role (windsurfer, powerboat, binoculars)
   - Shows track history from daily log files using Range requests for incremental loading
   - Has admin panel for course management and track clearing
   - Plays audio alerts when assist is requested

### JSON Packet Format

Position reports (phone to server):
```json
{"id": "S07", "sq": 12345, "ts": 1732615200, "lat": -36.8485, "lon": 174.7633, "spd": 12.5, "hdg": 275, "ast": false, "bat": 85, "sig": 3, "role": "sailor", "ver": "f66aaf8"}
```

Acknowledgements (server to phone):
```json
{"ack": 12345, "ts": 1732615201}
```

## Common Commands

### Server

```bash
# Start server (default port 41234, serves WebUI)
cd server
python3 tracker_server.py --static-dir ../WebUI --admin-password yourpassword

# Disable HTTP admin API (no password required)
python3 tracker_server.py --no-http
```

### Test Client

```bash
# Simulate 5 sailors, 1 support boat, 2 spectators
python3 server/test_client.py -H localhost --num-sailors 5 --num-support 1 --num-spectators 2

# Test assist flag
python3 server/test_client.py -H localhost --assist S03

# Custom start/end locations
python3 server/test_client.py --start-loc "-36.85,174.76" --end-loc "-36.84,174.77"
```

### Android Build

```bash
cd android
./gradlew assembleDebug
# APK: app/build/outputs/apk/debug/
```

## Key Configuration

- Default UDP/HTTP port: 41234
- Admin password: required (no default)
- Position file: `current_positions.json`
- Track logs: `logs/YYYY_MM_DD.jsonl`
- Course file: `course.json`
- Android server config: `android/app/src/main/java/nz/co/tracker/windsurfer/TrackerService.kt`

## OwnTracks Client Support

The server supports [OwnTracks](https://owntracks.org/) clients via HTTP endpoint.

### Server Setup

```bash
# With separate OwnTracks password
python3 tracker_server.py --static-dir ../WebUI --owntracks-password secretpass

# Uses admin password if --owntracks-password not specified
```

### OwnTracks App Configuration

- **Mode**: HTTP
- **URL**: `http://yourserver:41234/api/owntracks`
- **Authentication**: Username (any), Password (admin password or --owntracks-password)
- **TrackerID**: 2 characters (e.g., S1, o9)

OwnTracks clients appear with ID prefix `OT-` (e.g., `OT-S1`), default to sailor role, and show version `owntracks`. The display name is auto-set from the topic field (e.g., `owntracks/user/Andrew` → "Andrew"). Role can be changed via Web UI admin.

## User Overrides

Admins can customize display names and roles for any tracker client via the Web UI or API.

### Web UI

1. Enter Admin mode (click Admin button, enter password)
2. Click on any tracker marker on the map
3. Click the "Edit" button in the popup
4. Set a display name and/or override the role
5. Click Save

### API Endpoints

- `GET /api/users` - List all user overrides (requires admin auth)
- `POST /api/admin/user/{id}` - Set override for a user (requires admin auth)
  ```json
  {"name": "Andrew's Phone", "role": "spectator"}
  ```
- `DELETE /api/admin/user/{id}` - Remove override for a user (requires admin auth)

### Users File

Overrides are stored in `users.json`:
```json
{
  "updated": 1732615200,
  "updated_iso": "2024-11-26T12:00:00",
  "users": {
    "OT-S1": {"name": "Andrew", "role": "sailor"},
    "S03": {"name": "Race Official"}
  }
}
```
