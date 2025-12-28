# Windsurfer Tracker Protocol

This document describes the UDP/HTTP protocol used between tracker clients and the server.

## Transport

- **Primary**: UDP port 41234
- **Fallback**: HTTP POST to same port (for networks blocking UDP)
- **Encoding**: JSON over UTF-8

## Position Packet (Client → Server)

Sent every 10 seconds (0.1Hz mode) or batched every 10 seconds with 1Hz samples.

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
| `ver` | string | App version (e.g., "1.9.16+42(abc123)") |

### Optional Fields

| Field | Type | Description |
|-------|------|-------------|
| `sig` | int | Signal strength (0-4, -1 if unavailable) |
| `pwd` | string | Event password for authentication |
| `os` | string | OS version (e.g., "iOS 17.2", "Android 15") |
| `bdr` | float | Battery drain rate (%/hour) |
| `chg` | bool | Device is charging |
| `ps` | bool | Power save / low power mode active |
| `hac` | float | Horizontal accuracy in meters |
| `hr` | int | Heart rate in BPM |
| `pos` | array | 1Hz position array (see below) |
| `stopped` | bool | User deliberately stopped tracking |

### 1Hz Position Array (`pos`)

When using 1Hz mode, positions are batched into an array:

```json
{
  "pos": [
    [1732615200, -36.8485, 174.7633],
    [1732615201, -36.8486, 174.7634],
    ...
  ]
}
```

Each entry is `[timestamp, latitude, longitude]`. The `lat` and `lon` fields are omitted when `pos` is present.

### Example Position Packet

```json
{
  "id": "S07",
  "eid": 2,
  "sq": 12345,
  "ts": 1732615200,
  "lat": -36.8485,
  "lon": 174.7633,
  "spd": 12.5,
  "hdg": 275,
  "ast": false,
  "bat": 85,
  "sig": 3,
  "role": "sailor",
  "ver": "1.9.16+42(abc123)",
  "os": "Android 15",
  "pwd": "eventpass",
  "hac": 5.2
}
```

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
- Mark the position as stopped (displayed as "STOPPED" in WebUI)
- Distinguish from signal loss (no stop packet)

---

## ACK Packet (Server → Client)

Sent in response to each position packet.

### Fields

| Field | Type | Description |
|-------|------|-------------|
| `ack` | int | Acknowledged sequence number |
| `ts` | int | Server timestamp |
| `event` | string | Event name (optional) |
| `error` | string | Error type if failed (optional) |
| `msg` | string | Error message (optional) |
| `assist` | bool | Assist enabled for event (optional, absence = true) |

### Success ACK

```json
{
  "ack": 12345,
  "ts": 1732615201,
  "event": "NZ Interdominion 2026"
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

## Connection State Machine

Clients track connection state for UI feedback:

```
┌─────────────┐
│  GPS wait   │  ← No GPS fix yet
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
└─────────────┘ ◄────────────── └─────────────┘
                  success ACK
```

---

## Event Configuration

Events are configured via the management API with these relevant fields:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `assist_enabled` | bool | true | Whether assist button is available |
| `tracker_password` | string | "" | Password required for trackers |

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

---

## HTTP Fallback

For networks blocking UDP, clients can POST to the same port:

```
POST /api/position HTTP/1.1
Content-Type: application/json

{same JSON as UDP packet}
```

Response is the same ACK format.
