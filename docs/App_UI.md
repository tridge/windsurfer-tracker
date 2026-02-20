# Windsurfer Tracker App UI Design

This document describes the UI design for the Windsurfer Tracker mobile apps. Use this as a reference when implementing consistent UI across iOS (Swift), Android (Kotlin), and other platforms.

## Color Scheme

### Background
- Main background: White (`#FFFFFF`)
- Input field background: Light gray (`#EDEDED` / rgb 237,237,237 / ~93% white)

### Buttons
- Primary button (Start/Stop): Light gray (`#DEDEDE` / ~87% white), black text
- Secondary button (Settings): Darker gray (`#BABABA` / ~73% white), black text
- Both use 4px corner radius

### Text Colors
- Primary text: Black (`#000000`)
- Label text (field headers): Dark gray (`#454545` / ~27% white)
- Secondary info: Gray

### Status Colors
- Connection Good: Dark green (`#008800` / rgb 0,136,0)
- Connection Fair: Dark orange (`#CC6600` / rgb 204,102,0)
- Connection Poor: Dark red (`#CC0000` / rgb 204,0,0)
- Event name: Teal blue (`#0066AA` / rgb 0,102,170)

### Assist Button
- Inactive: Green (`#00FF00`), black text
- Active: Red (`#FF0000`), white text
- 16px corner radius
- Pulsing opacity animation when active (0.7 to 1.0)

---

## Screen: Configuration (Pre-Tracking)

Shown when tracking is not active (and not in idle mode). Simple form to enter basic settings.

### Layout (top to bottom)

1. **Your Name Field**
   - Label: "Your Name" (headline font, black)
   - Text input field
   - Placeholder: "e.g. John or S07"
   - Font: title3 size
   - Background: light gray (#EDEDED)
   - No autocapitalization, no autocorrect

2. **Server Address Field**
   - Label: "Server Address" (headline font, black)
   - Text input field
   - Placeholder: "IP address or hostname"
   - Font: body size
   - No autocapitalization, no autocorrect

3. **Live Tracking Link**
   - Tappable URL: `https://{server}/event.html?eid={eventId}`
   - Opens in system browser

4. **Location Permission Warning** (conditional)
   - Shown only when location permission not granted
   - Orange warning icon + text
   - "Location permission required"
   - "Tap 'Start Tracking' to grant permission"
   - Light orange background

5. **Spacer**

6. **Start Tracking Button**
   - Full width
   - Text: "Start Tracking"
   - Font: title3, bold
   - Padding: 16px vertical
   - Background: light gray (#DEDEDE)
   - Corner radius: 4px

7. **Settings Button**
   - Full width
   - Text: "Settings"
   - Font: body
   - Padding: 12px vertical
   - Background: darker gray (#BABABA)
   - Corner radius: 4px
   - 8px gap above, 16px padding at bottom

---

## Screen: Tracking (Active)

Shown when tracking is active. Displays current position and status.

### Layout (top to bottom)

1. **Status Line**
   - Shows connection state progression:
     - "GPS wait" - waiting for first GPS fix
     - "connecting ..." - have GPS, waiting for first ACK
     - "auth failure" - authentication failed (red text)
     - Event name - connected successfully (teal blue)
   - Font: headline, bold
   - Color: Red for "auth failure", Teal blue (#0066AA) otherwise

2. **Position Section**
   - Label: "Position" (caption, bold, dark gray)
   - Value: Formatted lat/lon (e.g., "-36.84850 174.76330")
   - Font: 18pt monospaced
   - Placeholder when no position: "---.----- ----.-----"

3. **Speed, Distance, and Course Row** (three columns)
   - **Speed Column**
     - Label: "Speed" (caption, bold, dark gray)
     - Value: Speed in knots with "kn" suffix (e.g., "12.5 kn")
     - Font: 26pt monospaced
     - Placeholder: "-- kn"
   - **Distance Column**
     - Label: "Distance" (caption, bold, dark gray)
     - Value: Distance in km or m (e.g., "2.3 km" or "450 m")
     - Font: 26pt monospaced
   - **Course Column**
     - Label: "Course" (caption, bold, dark gray)
     - Value: Heading in degrees (e.g., "275")
     - Font: 26pt monospaced
     - Placeholder: "---"

4. **Connection Status Row** (three columns)
   - **Connection Column**
     - Label: "Connection" (caption, bold, dark gray)
     - Value: ACK rate percentage (e.g., "85%")
     - Font: 20pt
     - Color: Green/Orange/Red based on quality
   - **Last ACK Column**
     - Label: "Last ACK" (caption, bold, dark gray)
     - Value: "ACK #12345" (sequence number)
     - Font: 16pt
   - **Updated Column**
     - Label: "Updated" (caption, bold, dark gray)
     - Value: Time of last update (e.g., "14:32:15")
     - Font: 16pt monospaced
     - Placeholder: "--:--:--"

5. **Spacer**

6. **Assist Button**
   - Large, prominent button (min 80px, max 120px height)
   - See Assist Button section below
   - Can be hidden per-event (server sends `assist: false`)

7. **Spacer**

8. **Stop Tracking Button**
   - Same style as Start button
   - Shows confirmation dialog before stopping

9. **Settings Button**
   - Same style as config screen

---

## Screen: Idle Mode

Shown after user stops tracking when server has idle mode enabled. The service stays running in the background sending periodic heartbeats.

### Layout
- **Status**: "Idle - waiting for admin start"
- **Notification**: Foreground service notification showing idle state
- **Behavior**: No GPS active, periodic heartbeat packets sent at server-configured interval
- **Exit conditions**:
  - User taps "Start Tracking" to resume tracking
  - Admin sends `start` command via WebUI
  - Admin sends `shutdown` command via WebUI
  - Server sends `idle=0` in ACK

### Android Boot-to-Idle
- Android supports auto-start on boot via `BootReceiver`
- On boot, service starts in idle mode (no GPS, sends heartbeats)
- Admin can remotely start tracking via WebUI
- Uses Direct Boot for device-encrypted storage access before unlock

---

## Component: Assist Button

Emergency assistance request button with safety features.

### Visibility

The assist button can be disabled per-event by the event manager:
- Server sends `assist: false` in ACK when disabled for event
- Client hides the assist button when `assist: false` received
- If client has assist active when disabled, it's automatically cleared
- Button reappears if manager re-enables assist (dynamic)

### States

**Inactive State:**
- Background: Green
- Text color: Black
- Main text: "REQUEST ASSISTANCE" (headline, bold)
- Sub text: "Long press to activate" (subheadline)

**Active State:**
- Background: Red
- Text color: White
- Main text: "ASSISTANCE REQUESTED" (headline, bold)
- Sub text: "Long press to cancel" (subheadline)
- Pulsing animation: opacity oscillates between 0.7 and 1.0

### Behavior
- **Phone apps**: Requires **long press** (0.5 seconds minimum) to toggle
- **Watch apps**: Tap shows slide-to-confirm overlay (see Watch UI section); cancel is immediate tap
- Single tap does nothing on phone (safety feature)
- Haptic feedback on activation:
  - Heavy impact on toggle
  - Warning notification pattern when activating
  - Light impact when canceling

### Styling
- Full width
- Vertical padding: 24px
- Corner radius: 16px
- Centered text

---

## Screen: Settings (Sheet/Dialog)

Full settings configuration, presented as a modal sheet.

### Sections

#### Identity Section
- **Your Name**
  - Label + text field on same row
  - Text field width: 120px
  - Right-aligned text
  - Placeholder: "e.g., S07"
  - No autocapitalization

- **Role** (Picker)
  - Options: Sailor, Support, Spectator
  - Default: Sailor

#### Server Section
- **Host**
  - Label + text field on same row
  - Text field width: 180px
  - Placeholder: "wstracker.org"
  - URL keyboard type
  - No autocapitalization

- **Port**
  - Label + text field on same row
  - Text field width: 100px
  - Placeholder: "41234"
  - Number keyboard
  - **No thousands separator** (display as "41234" not "41,234")

#### Event Section
- If events loaded from server: Show picker with event names
- If loading: Show "Loading events..." with spinner
- If no events: Show manual Event ID field + Refresh button
- Default event ID: **2**
- Passwords are cached per-event for quick switching

#### Authentication Section
- **Password**
  - Label + secure field (or text field when shown)
  - Field width: 150px
  - Placeholder: "Optional"

- **Show Password** toggle

#### Advanced Section
- **Tracker Beep** toggle (default: ON)
  - Vibrates once per minute when tracking (one buzz = connected, two buzzes = no connection)

#### Version Section
- Display: "X.Y.Z (build) githash"
- Example: "1.10.21 (97) abc1234"
- Gray text, right-aligned
- **iOS/Swift**: Uses `CFBundleShortVersionString`, `CFBundleVersion`, and `GIT_HASH` from Info.plist
- **Android/Kotlin**: Uses `versionName`, `versionCode`, and `BuildConfig.GIT_HASH`

### Navigation
- Title: "Settings"
- Done button in top-right to dismiss

---

## Watch UI (Compact)

Simplified interface for Apple Watch / Wear OS.

### Config Screen
- ID display (large, centered)
- Server host (small, gray)
- Start button (green, with play icon)
- Settings link

### Tracking Screen
- Status indicator: colored dot + "TRACKING" or "ASSIST"
- **Speed display** (very large, 42pt monospaced)
- Unit label: "kts"
- **Distance display** (below speed)
- Status pills row: battery + connection indicators
- Assist button (compact)
- Tap anywhere on tracking screen to stop (shows slide-to-confirm)
- Settings gear icon (top right on WearOS) — blocked during tracking with "Stop for settings" message

### Slide-to-Confirm Overlays (Watch)
Reusable full-screen overlay with a draggable slider thumb. Used for both stop and assist actions to prevent accidental activation during exercise.

- **Slide to Stop**: Red fill, square stop icon in thumb. Shown when tapping the tracking screen while active.
- **Slide for Assist**: Orange fill (`#FF8800`), ⚠ icon in thumb. Shown when tapping ASSIST button.
- **Cancel Assist**: Single tap (no slider) — cancelling is safe to do immediately.
- Slider track: 140dp wide, 40dp tall, dark gray (`#333333`) background
- Threshold: 85% drag to confirm
- Haptic feedback at threshold
- Auto-dismiss after 4 seconds if no action
- Tap outside the slider to dismiss

### Race Countdown Timer (watchOS/WearOS)
- 5-minute countdown with audio announcements
- Tap detection for start trigger
- watchOS: Action button support

### Watch Settings
- Your ID field
- Server field
- Role picker
- Settings access blocked during tracking (must stop first)

---

## Notification & Lock Screen

### Android
- Foreground service notification with custom icon
- Icon changes based on connection status (OK vs error)
- Shows "Tracking active", "Tracking - no connection", or "Idle - waiting for admin start"

### iOS
- Live Activities (iOS 16.1+) showing tracking status on lock screen
- Widget extension for Dynamic Island and lock screen

---

## WebUI Sidebar

The sidebar shows all tracked devices with:
- Name and ID
- Speed, battery, signal strength, satellite count
- Time since last update
- Status badges: IDLE (blue), NOGPS (orange), STOP (red)
- Green lightning bolt when charging
- Power saver/battery optimization warnings

---

## Typography

| Element | Size | Weight | Design |
|---------|------|--------|--------|
| Position value | 18pt | Regular | Monospaced |
| Speed/Course/Distance value | 26pt | Regular | Monospaced |
| Connection % | 20pt | Regular | Default |
| ACK/Updated values | 16pt | Regular | Monospaced (Updated) |
| Field labels | Caption | Bold | Default |
| Section headers | Headline | Bold | Default |
| Button text (primary) | Title3 | Bold | Default |
| Button text (secondary) | Body | Regular | Default |

---

## Spacing & Padding

- Screen edge padding: 16px
- Section spacing: 16px
- Field vertical spacing: 4px (label to input)
- Row spacing in status area: 12px
- Button vertical padding: 16px (primary), 12px (secondary)
- Button gap: 8px between buttons
- Bottom padding: 16px

---

## Animations

### Assist Button Pulse
- Duration: 0.5 seconds
- Easing: ease-in-out
- Repeat: forever, autoreverses
- Property: opacity (0.7 to 1.0)

### Screen Transitions
- Smooth crossfade between Config and Tracking views

---

## Confirmation Dialogs

### Stop Tracking (Phone)
- Title: "Stop Tracking?"
- Message: "Are you sure you want to stop tracking? Your position will no longer be reported."
- Actions: "Stop" (destructive), "Cancel"

### Stop Tracking (Watch)
- Slide-to-confirm overlay (see Watch UI section above)

---

## Stop Tracking Behavior

When user stops tracking:
1. Client sends final position packet with `stopped: true` flag
2. Server clears any active assist request for this tracker
3. Server marks position as stopped (shown as "STOP" in WebUI)
4. Retry up to 5 times with 500ms delays to ensure delivery
5. If server has idle mode enabled (idle interval > 0), client enters idle mode
6. If idle mode disabled, clean up and stop service

---

## Error Handling

- Errors displayed as alert dialogs
- Title: "Error"
- OK button to dismiss
