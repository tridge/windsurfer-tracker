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
- Frequency mode: Cyan (`#00AAAA` / rgb 0,170,170)

### Assist Button
- Inactive: Green (`#00FF00`), black text
- Active: Red (`#FF0000`), white text
- 16px corner radius
- Pulsing opacity animation when active (0.7 to 1.0)

---

## Screen: Configuration (Pre-Tracking)

Shown when tracking is not active. Simple form to enter basic settings.

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

3. **Location Permission Warning** (conditional)
   - Shown only when location permission not granted
   - Orange warning icon + text
   - "Location permission required"
   - "Tap 'Start Tracking' to grant permission"
   - Light orange background

4. **Spacer**

5. **Start Tracking Button**
   - Full width
   - Text: "Start Tracking"
   - Font: title3, bold
   - Padding: 16px vertical
   - Background: light gray (#DEDEDE)
   - Corner radius: 4px

6. **Settings Button**
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

1. **Event Name**
   - Text: Event name from server ACK, or "---" if not received
   - Font: headline, bold
   - Color: Teal blue (#0066AA)

2. **Frequency Mode**
   - Text: "1Hz MODE" or "0.1Hz MODE"
   - Font: caption, bold
   - Color: Cyan (#00AAAA)

3. **Position Section**
   - Label: "Position" (caption, bold, dark gray)
   - Value: Formatted lat/lon (e.g., "-36.84850 174.76330")
   - Font: 18pt monospaced
   - Placeholder when no position: "---.----- ----.-----"

4. **Speed and Course Row** (two columns)
   - **Speed Column**
     - Label: "Speed" (caption, bold, dark gray)
     - Value: Speed in knots with "kn" suffix (e.g., "12.5 kn")
     - Font: 26pt monospaced
     - Placeholder: "-- kn"
   - **Course Column**
     - Label: "Course" (caption, bold, dark gray)
     - Value: Heading in degrees (e.g., "275°")
     - Font: 26pt monospaced
     - Placeholder: "---°"

5. **Connection Status Row** (three columns)
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

6. **Spacer**

7. **Assist Button**
   - Large, prominent button (min 80px, max 120px height)
   - See Assist Button section below

8. **Spacer**

9. **Stop Tracking Button**
   - Same style as Start button
   - Shows confirmation dialog before stopping

10. **Settings Button**
    - Same style as config screen

---

## Component: Assist Button

Emergency assistance request button with safety features.

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
- Requires **long press** (0.5 seconds minimum) to toggle
- Single tap does nothing (safety feature)
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

#### Authentication Section
- **Password**
  - Label + secure field (or text field when shown)
  - Field width: 150px
  - Placeholder: "Optional"

- **Show Password** toggle

#### Advanced Section
- **1Hz Mode** toggle
- When enabled, show helper text: "Sends 10 positions per packet for higher precision"

#### Version Section
- Display: "Version X.Y.Z (build) githash"
- Example: "1.9.3 (33) abc1234"
- Gray text, right-aligned

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
- Status pills row: battery + connection indicators
- Assist button (compact)
- Stop button

### Watch Settings
- Your ID field
- Server field
- Role picker
- 1Hz Mode toggle

---

## Typography

| Element | Size | Weight | Design |
|---------|------|--------|--------|
| Position value | 18pt | Regular | Monospaced |
| Speed/Course value | 26pt | Regular | Monospaced |
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

### Stop Tracking
- Title: "Stop Tracking?"
- Message: "Are you sure you want to stop tracking? Your position will no longer be reported."
- Actions: "Stop" (destructive), "Cancel"

---

## Error Handling

- Errors displayed as alert dialogs
- Title: "Error"
- OK button to dismiss
