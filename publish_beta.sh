#!/bin/bash

set -e

SERVER=tracker@wstracker.org
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
GRADLE_FILE="$SCRIPT_DIR/android/app/build.gradle.kts"
WEAR_GRADLE_FILE="$SCRIPT_DIR/wear/app/build.gradle.kts"
FLUTTER_PUBSPEC="$SCRIPT_DIR/flutter/windsurfer_tracker/pubspec.yaml"

# Extract version info from build.gradle.kts (native Android)
VERSION_CODE=$(grep -E 'versionCode\s*=' "$GRADLE_FILE" | head -1 | sed 's/.*=\s*\([0-9]*\).*/\1/')
VERSION_NAME=$(grep -E 'versionName\s*=' "$GRADLE_FILE" | head -1 | sed 's/.*=\s*"\([^"]*\)".*/\1/')

# Extract Wear OS version info
WEAR_VERSION_CODE=$(grep -E 'versionCode\s*=' "$WEAR_GRADLE_FILE" | head -1 | sed 's/.*=\s*\([0-9]*\).*/\1/')
WEAR_VERSION_NAME=$(grep -E 'versionName\s*=' "$WEAR_GRADLE_FILE" | head -1 | sed 's/.*=\s*"\([^"]*\)".*/\1/')

# Extract Flutter version from pubspec.yaml
FLUTTER_VERSION=$(grep -E '^version:' "$FLUTTER_PUBSPEC" | sed 's/version:\s*\([0-9.]*\)+.*/\1/')
FLUTTER_BUILD=$(grep -E '^version:' "$FLUTTER_PUBSPEC" | sed 's/version:\s*[0-9.]*+\([0-9]*\)/\1/')

echo "Publishing to BETA environment..."
echo "Native Android: $VERSION_NAME (code: $VERSION_CODE)"
echo "Wear OS: $WEAR_VERSION_NAME (code: $WEAR_VERSION_CODE)"
echo "Flutter: $FLUTTER_VERSION (build: $FLUTTER_BUILD)"

# Generate beta version.json for native Android
cat > "$SCRIPT_DIR/beta_version.json" << EOF
{
  "version": "$VERSION_NAME",
  "versionCode": $VERSION_CODE,
  "url": "https://beta.wstracker.org/app/tracker.apk",
  "changelog": ""
}
EOF

# Generate beta flutter_version.json for Flutter
cat > "$SCRIPT_DIR/beta_flutter_version.json" << EOF
{
  "version": "$FLUTTER_VERSION",
  "versionCode": $FLUTTER_BUILD,
  "url": "https://beta.wstracker.org/app/tracker-flutter.apk",
  "changelog": ""
}
EOF

echo "Generated beta_version.json and beta_flutter_version.json"

ssh $SERVER pwd

# Upload native Android APK
rsync -Pav --chmod=F644 android/app/build/outputs/apk/release/app-release.apk $SERVER:tracker.beta/html/app/tracker.apk
rsync -Pav --chmod=F644 "$SCRIPT_DIR/beta_version.json" $SERVER:tracker.beta/html/app/version.json

# Upload Wear OS APK
WEAR_APK="$SCRIPT_DIR/wear/app/build/outputs/apk/release/app-release.apk"
if [ -f "$WEAR_APK" ]; then
    rsync -Pav --chmod=F644 "$WEAR_APK" $SERVER:tracker.beta/html/app/WearOS-tracker.apk
    echo "  Wear OS APK uploaded"
else
    echo "  WARNING: Wear OS APK not found at $WEAR_APK - skipping"
fi

# Upload Flutter APK
FLUTTER_APK="$SCRIPT_DIR/flutter/windsurfer_tracker/build/app/outputs/flutter-apk/app-release.apk"
if [ -f "$FLUTTER_APK" ]; then
    rsync -Pav --chmod=F644 "$FLUTTER_APK" $SERVER:tracker.beta/html/app/tracker-flutter.apk
    rsync -Pav --chmod=F644 "$SCRIPT_DIR/beta_flutter_version.json" $SERVER:tracker.beta/html/app/flutter_version.json
    echo "  Flutter APK uploaded"
else
    echo "  WARNING: Flutter APK not found at $FLUTTER_APK - skipping"
fi

# Upload iOS IPA and manifest if available
IOS_IPA="$SCRIPT_DIR/flutter/windsurfer_tracker/build/ios/ipa/windsurfer_tracker.ipa"
if [ -f "$IOS_IPA" ]; then
    echo "Publishing iOS: $FLUTTER_VERSION+$FLUTTER_BUILD"

    # Generate beta manifest.plist with current version
    mkdir -p "$SCRIPT_DIR/WebUI/app"
    cat > "$SCRIPT_DIR/WebUI/app/beta_manifest.plist" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>items</key>
    <array>
        <dict>
            <key>assets</key>
            <array>
                <dict>
                    <key>kind</key>
                    <string>software-package</string>
                    <key>url</key>
                    <string>https://beta.wstracker.org/app/windsurfer_tracker.ipa</string>
                </dict>
            </array>
            <key>metadata</key>
            <dict>
                <key>bundle-identifier</key>
                <string>nz.co.tracker.windsurfer</string>
                <key>bundle-version</key>
                <string>$FLUTTER_VERSION</string>
                <key>kind</key>
                <string>software</string>
                <key>title</key>
                <string>Windsurfer Tracker (Beta)</string>
            </dict>
        </dict>
    </array>
</dict>
</plist>
EOF

    rsync -Pav --chmod=F644 "$IOS_IPA" $SERVER:tracker.beta/html/app/windsurfer_tracker.ipa
    rsync -Pav --chmod=F644 "$SCRIPT_DIR/WebUI/app/beta_manifest.plist" $SERVER:tracker.beta/html/app/manifest.plist
else
    echo "No iOS IPA found at $IOS_IPA - skipping iOS upload"
fi

# Upload WebUI (ensure directories are world-readable)
rsync -Pav --chmod=D755,F644 WebUI/ $SERVER:tracker.beta/html/

# Upload server
rsync -Pav --chmod=D755,F644 server/ $SERVER:tracker.beta/

echo ""
echo "Published to BETA environment at $SERVER:"
echo "  Native Android: $VERSION_NAME (tracker.apk)"
if [ -f "$WEAR_APK" ]; then
    echo "  Wear OS: $WEAR_VERSION_NAME (WearOS-tracker.apk)"
fi
if [ -f "$FLUTTER_APK" ]; then
    echo "  Flutter Android: $FLUTTER_VERSION+$FLUTTER_BUILD (tracker-flutter.apk)"
fi
if [ -f "$IOS_IPA" ]; then
    echo "  Flutter iOS: $FLUTTER_VERSION+$FLUTTER_BUILD (windsurfer_tracker.ipa)"
fi
echo ""
echo "Beta URLs:"
echo "  Web UI: https://beta.wstracker.org/"
echo "  API: https://beta.wstracker.org/api/..."
echo "  APKs: https://beta.wstracker.org/app/"
