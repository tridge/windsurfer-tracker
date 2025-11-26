# Windsurfer Tracker - Flutter App

Cross-platform GPS tracker for windsurfing races. Works on both iOS and Android.

## Features

- GPS tracking every 10 seconds
- UDP packet transmission to race server
- DNS caching for unreliable networks
- Assist request button (long-press to activate)
- Background tracking when screen locked
- Auto-resume tracking on app restart
- High contrast UI for outdoor use

## Setup Instructions

### Prerequisites

1. Install Flutter SDK:
   ```bash
   sudo snap install flutter --classic
   flutter doctor
   ```

2. For Android development:
   - Android Studio or just Android SDK command line tools
   - Accept licenses: `flutter doctor --android-licenses`

3. For iOS builds (requires Apple Developer Account):
   - Sign up at https://developer.apple.com ($99/year)
   - Use Codemagic or similar CI/CD for builds

### Development

1. Navigate to the Flutter project:
   ```bash
   cd flutter/windsurfer_tracker
   ```

2. Get dependencies:
   ```bash
   flutter pub get
   ```

3. Run on connected Android device:
   ```bash
   flutter run
   ```

### Building APK (Android)

```bash
cd flutter/windsurfer_tracker
flutter build apk --release
```

APK will be at: `build/app/outputs/flutter-apk/app-release.apk`

### Building for iOS

#### Option 1: Codemagic (Recommended - no Mac required)

1. Sign up at https://codemagic.io
2. Connect your Git repository
3. Create a new Flutter iOS app
4. Add your Apple Developer credentials
5. Build and get the IPA

#### Option 2: Local Mac

```bash
cd flutter/windsurfer_tracker
flutter build ios --release
```

Then archive and distribute via Xcode.

## Configuration

Default settings (matching Android app):
- Server: `track.tridgell.net`
- Port: `41234`
- Location interval: 10 seconds
- UDP retry count: 3
- Retry delay: 1.5 seconds

## Testing

1. Build and install the app
2. Configure server address in Settings
3. Start tracking
4. Verify your position appears on https://track.tridgell.net

## iOS Background Location Notes

iOS will show a blue status bar when the app is tracking in the background. This is required by Apple for user privacy and cannot be disabled.

The app requests "Always" location permission to enable background tracking when the screen is locked.

## Differences from Native Android App

- Signal strength (`sig` field) always returns -1 (not easily available in Flutter)
- Uses platform location APIs through geolocator package
- Same UDP protocol and JSON format

## Project Structure

```
lib/
├── main.dart                    # App entry point
├── screens/
│   └── home_screen.dart         # Main UI
├── services/
│   ├── tracker_service.dart     # UDP, GPS, DNS caching
│   └── preferences_service.dart # Settings storage
└── widgets/
    ├── assist_button.dart       # Assist button with animations
    └── settings_dialog.dart     # Settings configuration
```
