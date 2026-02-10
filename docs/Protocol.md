# Windsurfer Tracker Protocol

This document describes the UDP/HTTP protocol used between tracker clients and the server.

## Transport

- **Primary**: UDP port 41234
- **Fallback**: HTTP POST to same port (for networks blocking UDP)
- **Encoding**: JSON over UTF-8

## Position Packet (Client → Server)

Always sent in 1Hz mode: GPS samples at 1Hz, batched into packets every 10 seconds.

### Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Tracker identifier (e.g., "S07", "John") |
| `eid` | int | Event ID (multi-event mode) |
| `sq` | int | Sequence number for ACK tracking |
| `ts` | int | Unix timestamp (seconds) |
| `lat` | float | Latitude in degrees (omit if using `pos` array) |
| `lon` | float | Longitude in degrees (omit if using `pos` array) |
| `spd` | float | Speed in knots |
| `hdg` | int | Heading 0-360 degrees |
| `ast` | bool | Assist requested flag |
| `bat` | int | Battery percentage (0-100) |
| `role` | string | "sailor", "support", or "spectator" |
| `ver` | string | App version (e.g., "1.10.21+97(abc123)") |

### Optional Fields

| Field | Type | Description |
|-------|------|-------------|
| `sig` | int | Signal strength (0-4, -1 if unavailable) |
| `pwd` | string | Event password for authentication |
| `os` | string | OS version (e.g., "iOS 18.2", "Android 15") |
| `bdr` | float | Battery drain rate (%/hour) |
| `chg` | bool | Device is charging |
| `hac` | float | Horizontal accuracy in meters |
| `nsats` | int | GPS satellites used in fix (0 = no fix) |
| `hr` | int | Heart rate in BPM |
| `pos` | array | 1Hz position array (see below) |
| `flg` | object | Status flags: `ps` (power saver), `bo` (battery opt ignored) |
| `stopped` | bool | User deliberately stopped tracking |
| `idle` | bool | Idle mode heartbeat (no GPS) |

### 1Hz Position Array (`pos`)

Positions are batched into an array with speed per sample:

```json
{
  "pos": [
    [1732615200, -36.8485, 174.7633, 12.5],
    [1732615201, -36.8486, 174.7634, 13.1],
    ...
  ]
}
```

Each entry is `[timestamp, latitude, longitude, speed_knots]`. The `lat` and `lon` fields are omitted when `pos` is present.

### Example Position Packet

```json
{
  "id": "S07",
  "eid": 2,
  "sq": 12345,
  "ts": 1732615200,
  "pos": [[1732615190, -36.8485, 174.7633, 12.5], ...],
  "spd": 12.5,
  "hdg": 275,
  "ast": false,
  "bat": 85,
  "chg": false,
  "sig": 3,
  "nsats": 12,
  "role": "sailor",
  "ver": "1.10.21+97(abc123)",
  "os": "Android 15",
  "pwd": "eventpass",
  "hac": 5.2,
  "flg": {"ps": false, "bo": true}
}
```

### GPS-Wait Heartbeat

When tracking is active but GPS has no fix yet, clients send a heartbeat every 10 seconds so the server knows they're alive and can send commands back:

```json
{
  "id": "S07",
  "eid": 2,
  "sq": 100,
  "ts": 1732615200,
  "spd": 0,
  "hdg": 0,
  "ast": false,
  "bat": 85,
  "chg": false,
  "sig": 3,
  "nsats": 0,
  "role": "sailor",
  "ver": "1.10.21+97(abc123)"
}
```

The server identifies GPS-wait packets by `nsats: 0` with no `lat`/`lon`. These are shown with a "NOGPS" badge in the WebUI.

### Idle Heartbeat

When the service is in idle mode (user stopped tracking, waiting for admin start command), periodic heartbeats are sent with no GPS data:

```json
{
  "id": "S07",
  "eid": 2,
  "sq": 200,
  "ts": 1732615200,
  "idle": true,
  "bat": 95,
  "chg": true,
  "sig": 3,
  "role": "sailor",
  "ver": "1.10.21+97(abc123)"
}
```

The idle interval is configured by the server via the `idle` field in ACKs (in seconds). On a fresh boot, clients use a 15-second default until the server ACK overrides it.

### Stop Packet

When user deliberately stops tracking, send a final packet with `stopped: true`:

```json
{
  "id": "S07",
  "eid": 2,
  "sq": 12346,
  "ts": 1732615210,
  "lat": -36.8485,
  "lon": 174.7633,
  "spd": 0,
  "hdg": 0,
  "ast": false,
  "stopped": true,
  ...
}
```

The server will:
- Clear any active assist flag
- Mark the position as stopped (displayed as "STOP" in WebUI)
- Distinguish from signal loss (no stop packet)

After sending the stop packet, if the server has idle mode enabled, the client enters idle mode instead of shutting down completely.

---

## ACK Packet (Server → Client)

Sent in response to each position/idle/GPS-wait packet.

### Fields

| Field | Type | Description |
|-------|------|-------------|
| `ack` | int | Acknowledged sequence number |
| `ts` | int | Server timestamp |
| `event` | string | Event name (optional) |
| `error` | string | Error type if failed (optional) |
| `msg` | string | Error message (optional) |
| `assist` | bool | Assist enabled for event (optional, absence = true) |
| `idle` | int | Idle heartbeat interval in seconds (0 = disabled) |
| `cmd` | string | Remote command (optional, see below) |

### Success ACK

```json
{
  "ack": 12345,
  "ts": 1732615201,
  "event": "NZ Interdominion 2026",
  "idle": 15
}
```

### ACK with Assist Disabled

When event has assist disabled:

```json
{
  "ack": 12345,
  "ts": 1732615201,
  "event": "NZ Interdominion 2026",
  "assist": false
}
```

Client should:
- Hide the assist button
- Clear any active assist flag locally

### Auth Error ACK

```json
{
  "ack": 12345,
  "ts": 1732615201,
  "error": "auth",
  "msg": "Invalid password"
}
```

---

## Proactive Commands (Server → Client)

The server can push commands to clients without waiting for a client packet. These are sent as UDP packets to the client's last known address:

```json
{
  "ack": 0,
  "proactive": true,
  "cmd": "stop"
}
```

The `proactive: true` flag distinguishes these from normal ACKs. Commands can also be included in normal ACKs via the `cmd` field.

### Remote Commands

| Command | Description | When Valid |
|---------|-------------|------------|
| `stop` | Stop tracking, enter idle mode | While tracking |
| `cancel_assist` | Clear assist flag | While tracking |
| `start` | Start tracking from idle | While idle |
| `shutdown` | Exit idle mode, stop service | While idle |

When `idle` interval in ACK is 0 while in idle mode, the client shuts down (equivalent to `shutdown` command).

---

## Connection State Machine

Clients track connection state for UI feedback:

```
┌─────────────┐
│  GPS wait   │  ← No GPS fix yet (sends heartbeats with nsats=0)
└──────┬──────┘
       │ GPS fix received
       ▼
┌─────────────┐
│ connecting  │  ← Have GPS, no ACK yet
└──────┬──────┘
       │ First ACK received
       ▼
┌─────────────┐      auth error
│ Event Name  │ ──────────────► ┌─────────────┐
│  (normal)   │                 │ auth failure│
└──────┬──────┘ ◄────────────── └─────────────┘
       │ user stops              success ACK
       ▼
┌─────────────┐      admin start cmd
│    IDLE     │ ──────────────► ┌─────────────┐
│ (heartbeat) │                 │  GPS wait   │
└──────┬──────┘ ◄────────────── └─────────────┘
       │ admin shutdown /        user stops
       │ idle=0 from server
       ▼
┌─────────────┐
│   Stopped   │
└─────────────┘
```

---

## Event Configuration

Events are configured via the management API with these relevant fields:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `assist_enabled` | bool | true | Whether assist button is available |
| `tracker_password` | string | "" | Password required for trackers |
| `tracker_password2` | string | "" | Second accepted password (dual password support) |
| `idle_interval` | int | 0 | Idle heartbeat interval in seconds (0 = idle mode disabled) |

When `assist_enabled` is false:
- ACK includes `assist: false`
- Server clears any incoming assist flags
- Clients hide the assist button

---

## Retry Behavior

### Position Packets
- Retry up to 3 times with 1.5s delay between attempts
- Stop retrying if ACK received for this sequence
- Record success/failure in sliding window for connection quality

### Stop Packets
- Retry up to 5 times with 500ms delay
- More aggressive to ensure server knows user stopped
- Proceed with cleanup after retries exhausted

### Idle Heartbeats
- No retries (sent on a regular interval)
- On send failure, socket is closed and recreated on next attempt

---

## Network Resilience

### DNS Fallback
Both Android and iOS clients cache DNS lookups for 5 minutes. If DNS fails completely for `wstracker.org`, a hardcoded IP fallback (`103.230.158.49`) is used.

### Socket Recovery (Android)
Android registers a `ConnectivityManager.NetworkCallback` to detect WiFi/cellular transitions. On network loss, the UDP socket is closed and lazily recreated on the next send via `ensureSocket()`. The ACK listener automatically restarts when the socket is recreated.

### Socket Recovery (iOS)
iOS uses `NWPathMonitor` to detect network changes and creates a new `NWConnection` for each send, avoiding stale socket issues.

---

## HTTP Fallback

For networks blocking UDP, clients can POST to the same port:

```
POST /api/tracker HTTP/1.1
Content-Type: application/json

{same JSON as UDP packet}
```

Response is the same ACK format. Clients switch to HTTP after 3 consecutive UDP failures and retry UDP after 60 seconds.
