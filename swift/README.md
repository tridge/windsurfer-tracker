# Windsurfer Tracker - Swift iOS/watchOS App

Native Swift implementation of the Windsurfer Tracker for iOS and Apple Watch.

## Quick Start with XcodeGen

The project uses [XcodeGen](https://github.com/yonaskolb/XcodeGen) to generate the Xcode project from `project.yml`. This avoids committing `.xcodeproj` files to git.

```bash
# Install XcodeGen
brew install xcodegen

# Generate and open project
cd swift/WindsurferTracker
xcodegen generate
open WindsurferTracker.xcodeproj
```

## GitHub Actions

The project includes CI/CD via GitHub Actions (`.github/workflows/swift-build.yml`):

- **On push to master**: Builds unsigned iOS and watchOS apps
- **On manual trigger**: Builds signed IPA and optionally uploads to TestFlight

### Required Secrets for Signed Builds

Add these GitHub secrets for signed builds:
- `BUILD_CERTIFICATE_BASE64` - Apple Distribution certificate (.p12, base64)
- `P12_PASSWORD` - Password for the .p12 file
- `KEYCHAIN_PASSWORD` - Temporary keychain password
- `SWIFT_PROVISIONING_PROFILE_BASE64` - Provisioning profile for `nz.co.tracker.windsurfer.swift`

For TestFlight uploads, also add:
- `APP_STORE_CONNECT_API_KEY_BASE64`
- `APP_STORE_CONNECT_KEY_ID`
- `APP_STORE_CONNECT_ISSUER_ID`

## Manual Project Setup (Alternative)

If you prefer to create the Xcode project manually instead of using XcodeGen:

1. Open Xcode and create a new project:
   - Choose **App** under iOS
   - Product Name: `WindsurferTracker`
   - Team: Your development team
   - Organization Identifier: `nz.co.tracker`
   - Interface: **SwiftUI**
   - Language: **Swift**

2. Add watchOS target:
   - File → New → Target
   - Choose **App** under watchOS
   - Product Name: `WindsurferTrackerWatch`
   - Embed in: `WindsurferTracker`

3. Add source files:
   - Drag all `.swift` files from `Shared/` into the Shared group
   - Set target membership for Shared files to **both** iOS and watchOS targets
   - Drag iOS-specific files to the iOS target
   - Drag watchOS-specific files to the watchOS target

### Configuring Capabilities

#### iOS Target

1. Select the iOS target → Signing & Capabilities
2. Add capabilities:
   - **Background Modes**:
     - ✓ Location updates
     - ✓ Background fetch
   - **App Groups** (optional, for sharing data with watch):
     - Add: `group.nz.co.tracker.windsurfer`

3. Replace the generated `Info.plist` with the one from `WindsurferTracker/Resources/Info.plist`

#### watchOS Target

1. Select the watchOS target → Signing & Capabilities
2. Add capabilities:
   - **Background Modes**:
     - ✓ Location updates
     - ✓ Workout processing
   - **App Groups** (same as iOS):
     - Add: `group.nz.co.tracker.windsurfer`

3. Replace the generated `Info.plist` with the one from `WindsurferTrackerWatch/Resources/Info.plist`

### Build Settings

1. Set deployment targets:
   - iOS: 15.0
   - watchOS: 8.0

2. Set Swift version: 5.9+

## Architecture

### Shared Code (`Shared/`)

Code shared between iOS and watchOS:

- **Models/**
  - `TrackerConfig.swift` - Configuration constants
  - `TrackerPacket.swift` - JSON packet structures
  - `TrackerPosition.swift` - Position data model
  - `TrackerState.swift` - State enums

- **Services/**
  - `TrackerService.swift` - Main coordinator (actor)
  - `LocationManager.swift` - CoreLocation wrapper
  - `NetworkManager.swift` - UDP/HTTP networking
  - `DNSResolver.swift` - DNS caching
  - `BatteryMonitor.swift` - Battery tracking
  - `PreferencesManager.swift` - UserDefaults wrapper

- **Utilities/**
  - `GeoCalculations.swift` - Speed/bearing calculations

### iOS App (`WindsurferTracker/`)

- `App/WindsurferTrackerApp.swift` - Entry point
- `App/AppDelegate.swift` - Background mode handling
- `ViewModels/TrackerViewModel.swift` - UI state management
- `Views/` - SwiftUI views

### watchOS App (`WindsurferTrackerWatch/`)

- `App/WindsurferTrackerWatchApp.swift` - Entry point
- `Views/WatchTrackerViewModel.swift` - Watch UI state
- `Views/` - Watch-optimized SwiftUI views

## Features

- GPS tracking every 10 seconds (or 1Hz batched mode)
- UDP communication with server
- HTTP fallback after 3 UDP failures
- DNS caching (5-minute TTL)
- Background location tracking
- Assist button with long-press activation
- Battery drain rate calculation
- Auto-resume tracking on app launch

## Protocol Compatibility

Uses the same JSON packet format as Android/Flutter apps:

```json
{
  "id": "S07",
  "eid": 1,
  "sq": 12345,
  "ts": 1732615200,
  "lat": -36.8485,
  "lon": 174.7633,
  "spd": 12.5,
  "hdg": 275,
  "ast": false,
  "bat": 85,
  "sig": -1,
  "role": "sailor",
  "ver": "1.0+1(swift)",
  "os": "iOS 17.2"
}
```

## Testing

1. Build and run on simulator or device
2. Grant location permissions (Always recommended)
3. Configure server settings
4. Start tracking and verify positions appear on web UI

## Troubleshooting

### Location not updating in background

- Ensure "Always" location permission is granted
- Check that Background Modes capability includes "Location updates"
- Verify `allowsBackgroundLocationUpdates = true` in LocationManager

### UDP packets not reaching server

- Check server host/port configuration
- Verify network connectivity
- App will automatically fall back to HTTP after 3 failures

### Watch not connecting

- Ensure both iOS and watchOS apps use the same App Group
- Verify the watch has WiFi or cellular connectivity
- Check that the watch is not relying on iPhone for network
