#!/bin/bash
# Build iOS + watchOS app and upload to TestFlight
# Builds on mac2 with local signing

set -e

MAC_HOST="mac2"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOCAL_SWIFT_DIR="$SCRIPT_DIR/../swift"
REMOTE_PROJECT_DIR="~/project/windsurfer-tracker/swift"
ARCHIVE_PATH="$REMOTE_PROJECT_DIR/WindsurferTracker/build/WindsurferTracker.xcarchive"
EXPORT_PATH="$REMOTE_PROJECT_DIR/WindsurferTracker/build/export"

# App Store Connect API key info
API_KEY_ID="87N266B8J3"
API_KEY_ISSUER="05c0921d-a6cd-4fa6-8e45-a57d22250f7a"

# Team and signing
TEAM_ID="76AR6DVKBC"
CODE_SIGN_IDENTITY="Apple Distribution: Andrew Tridgell (76AR6DVKBC)"

# Read keychain password from file (not in git)
KEYCHAIN_PASSWORD_FILE="$SCRIPT_DIR/keys/keychain_password"
if [ ! -f "$KEYCHAIN_PASSWORD_FILE" ]; then
    echo "ERROR: Keychain password file not found: $KEYCHAIN_PASSWORD_FILE"
    echo "Create it with: echo 'your-password' > keys/keychain_password"
    exit 1
fi
KEYCHAIN_PASSWORD=$(cat "$KEYCHAIN_PASSWORD_FILE")

echo "=== Syncing Swift code to $MAC_HOST ==="
rsync -av --delete \
    --exclude='.git' \
    --exclude='build' \
    --exclude='*.xcodeproj' \
    --exclude='DerivedData' \
    "$LOCAL_SWIFT_DIR/" "$MAC_HOST:$REMOTE_PROJECT_DIR/"

echo "=== Unlocking keychain ==="
ssh "$MAC_HOST" "security unlock-keychain -p '$KEYCHAIN_PASSWORD' ~/Library/Keychains/build.keychain-db"

# App Store build is iOS-only: drop the embedded watchOS app so the watch
# (and its assist UI) is not part of the submission. The sideload build
# (build_ios_sideload.sh) keeps the watch. This edits only the synced copy
# on the build host; rsync above restores it from the repo each run.
echo "=== Dropping embedded watch app (iOS-only App Store build) ==="
ssh "$MAC_HOST" "cd $REMOTE_PROJECT_DIR/WindsurferTracker && \
    perl -0pi -e 's/\n      - target: WindsurferTrackerWatch\n        embed: true\n        codeSign: true//' project.yml && \
    echo 'removed watch embed dependency from project.yml'"

echo "=== Generating Xcode project ==="
ssh "$MAC_HOST" "cd $REMOTE_PROJECT_DIR/WindsurferTracker && /opt/homebrew/bin/xcodegen generate"

echo "=== Getting provisioning profile UUIDs ==="
# Get profile names from the mobileprovision files
IOS_PP_NAME=$(ssh "$MAC_HOST" "/usr/libexec/PlistBuddy -c 'Print Name' /dev/stdin <<< \$(/usr/bin/security cms -D -i ~/Library/MobileDevice/Provisioning\ Profiles/WindsurferTracker2.mobileprovision)")
WATCH_PP_NAME=$(ssh "$MAC_HOST" "/usr/libexec/PlistBuddy -c 'Print Name' /dev/stdin <<< \$(/usr/bin/security cms -D -i ~/Library/MobileDevice/Provisioning\ Profiles/WindsurferTrackerWatch.mobileprovision)")
WIDGET_PP_NAME=$(ssh "$MAC_HOST" "/usr/libexec/PlistBuddy -c 'Print Name' /dev/stdin <<< \$(/usr/bin/security cms -D -i ~/Library/MobileDevice/Provisioning\ Profiles/WindsurferTrackerWidget.mobileprovision)")
echo "iOS Profile Name: $IOS_PP_NAME"
echo "watchOS Profile Name: $WATCH_PP_NAME"
echo "Widget Profile Name: $WIDGET_PP_NAME"
IOS_PP_UUID=$(ssh "$MAC_HOST" "/usr/libexec/PlistBuddy -c 'Print UUID' /dev/stdin <<< \$(/usr/bin/security cms -D -i ~/Library/MobileDevice/Provisioning\ Profiles/WindsurferTracker2.mobileprovision)")
WATCH_PP_UUID=$(ssh "$MAC_HOST" "/usr/libexec/PlistBuddy -c 'Print UUID' /dev/stdin <<< \$(/usr/bin/security cms -D -i ~/Library/MobileDevice/Provisioning\ Profiles/WindsurferTrackerWatch.mobileprovision)")
WIDGET_PP_UUID=$(ssh "$MAC_HOST" "/usr/libexec/PlistBuddy -c 'Print UUID' /dev/stdin <<< \$(/usr/bin/security cms -D -i ~/Library/MobileDevice/Provisioning\ Profiles/WindsurferTrackerWidget.mobileprovision)")
echo "iOS Profile UUID: $IOS_PP_UUID"
echo "watchOS Profile UUID: $WATCH_PP_UUID"
echo "Widget Profile UUID: $WIDGET_PP_UUID"

echo "=== Configuring project for manual signing ==="
ssh "$MAC_HOST" "cd $REMOTE_PROJECT_DIR/WindsurferTracker && \
    sed -i '' 's/CODE_SIGN_STYLE = Automatic;/CODE_SIGN_STYLE = Manual; CODE_SIGN_IDENTITY = \"Apple Distribution\";/g' WindsurferTracker.xcodeproj/project.pbxproj && \
    sed -i '' 's/PRODUCT_BUNDLE_IDENTIFIER = nz.co.tracker.windsurfer;/PRODUCT_BUNDLE_IDENTIFIER = nz.co.tracker.windsurfer; PROVISIONING_PROFILE_SPECIFIER = \"$IOS_PP_NAME\";/g' WindsurferTracker.xcodeproj/project.pbxproj && \
    sed -i '' 's/PRODUCT_BUNDLE_IDENTIFIER = nz.co.tracker.windsurfer.watchkitapp;/PRODUCT_BUNDLE_IDENTIFIER = nz.co.tracker.windsurfer.watchkitapp; PROVISIONING_PROFILE_SPECIFIER = \"$WATCH_PP_NAME\";/g' WindsurferTracker.xcodeproj/project.pbxproj && \
    sed -i '' 's/PRODUCT_BUNDLE_IDENTIFIER = nz.co.tracker.windsurfer.widget;/PRODUCT_BUNDLE_IDENTIFIER = nz.co.tracker.windsurfer.widget; PROVISIONING_PROFILE_SPECIFIER = \"$WIDGET_PP_NAME\";/g' WindsurferTracker.xcodeproj/project.pbxproj"

echo "=== Creating ExportOptions.plist ==="
ssh "$MAC_HOST" "cat > $REMOTE_PROJECT_DIR/WindsurferTracker/ExportOptions.plist << 'PLISTEOF'
<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\">
<dict>
    <key>method</key>
    <string>app-store-connect</string>
    <key>destination</key>
    <string>export</string>
    <key>signingStyle</key>
    <string>manual</string>
    <key>teamID</key>
    <string>$TEAM_ID</string>
    <key>provisioningProfiles</key>
    <dict>
        <key>nz.co.tracker.windsurfer</key>
        <string>$IOS_PP_UUID</string>
        <key>nz.co.tracker.windsurfer.watchkitapp</key>
        <string>$WATCH_PP_UUID</string>
        <key>nz.co.tracker.windsurfer.widget</key>
        <string>$WIDGET_PP_UUID</string>
    </dict>
</dict>
</plist>
PLISTEOF"

echo "=== Archiving iOS + watchOS app ==="
GIT_HASH=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
ssh "$MAC_HOST" "cd $REMOTE_PROJECT_DIR/WindsurferTracker && \
    security unlock-keychain -p '$KEYCHAIN_PASSWORD' ~/Library/Keychains/build.keychain-db && \
    xcodebuild archive \
    -project WindsurferTracker.xcodeproj \
    -scheme WindsurferTracker \
    -destination 'generic/platform=iOS' \
    -archivePath build/WindsurferTracker.xcarchive \
    DEVELOPMENT_TEAM=$TEAM_ID \
    GIT_HASH=$GIT_HASH \
    SWIFT_ACTIVE_COMPILATION_CONDITIONS='\$(inherited) APPSTORE' \
    OTHER_CODE_SIGN_FLAGS='--keychain ~/Library/Keychains/build.keychain-db'"

echo "=== Verifying iOS-only archive (no embedded watch app) ==="
ssh "$MAC_HOST" "if [ -d '$ARCHIVE_PATH/Products/Applications/Windsurfer Tracker.app/Watch' ]; then \
    echo 'ERROR: watch app present in archive — aborting before upload'; exit 1; \
    else echo 'OK: no watch app in archive'; fi"

echo "=== Exporting IPA ==="
ssh "$MAC_HOST" "cd $REMOTE_PROJECT_DIR/WindsurferTracker && \
    rm -rf build/export && \
    security unlock-keychain -p '$KEYCHAIN_PASSWORD' ~/Library/Keychains/build.keychain-db && \
    xcodebuild -exportArchive \
    -archivePath build/WindsurferTracker.xcarchive \
    -exportOptionsPlist ExportOptions.plist \
    -exportPath build/export \
    OTHER_CODE_SIGN_FLAGS='--keychain ~/Library/Keychains/build.keychain-db'"

echo "=== Uploading to TestFlight ==="
# Workaround for macOS 26.2: altool's NSURLSession times out fetching
# Defaults.properties from Apple's servers. Setting ITunesTransporterDefaultsURL
# to a non-existent file:// URL makes it fail fast instead of hanging, and
# altool continues uploading successfully despite the errors.
ssh "$MAC_HOST" "defaults write com.apple.itunes.altool ITunesTransporterDefaultsURL 'file:///dev/null/Defaults.properties' && \
    xcrun altool --upload-app \
    -f $REMOTE_PROJECT_DIR/WindsurferTracker/build/export/Windsurfer\ Tracker.ipa \
    -t ios \
    --apiKey $API_KEY_ID \
    --apiIssuer $API_KEY_ISSUER"

echo ""
echo "=== Done! ==="
echo "Check App Store Connect for the new build"
echo "TestFlight testers will be notified automatically"
