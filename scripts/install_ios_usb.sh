#!/bin/bash
# Build iOS + watchOS app on mac2 and install to locally USB-connected iPhone
#
# Usage:
#   scripts/install_ios_usb.sh

set -e

MAC_HOST="mac2"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
LOCAL_SWIFT_DIR="$REPO_ROOT/swift"
REMOTE_PROJECT_DIR="~/project/windsurfer-tracker/swift"
ARCHIVE_PATH="$REMOTE_PROJECT_DIR/WindsurferTracker/build/WindsurferTracker.xcarchive"
EXPORT_PATH="$REMOTE_PROJECT_DIR/WindsurferTracker/build/export"

# Team and signing
TEAM_ID="76AR6DVKBC"

# Ad-hoc provisioning profile filenames
IOS_PP_FILE="WindsurferTrackerAdHoc.mobileprovision"
WATCH_PP_FILE="WindsurferTrackerWatchAdHoc.mobileprovision"
WIDGET_PP_FILE="WindsurferTrackerWidgetAdHoc.mobileprovision"

# Read keychain password from file (not in git)
KEYCHAIN_PASSWORD_FILE="$SCRIPT_DIR/keys/keychain_password"
if [ ! -f "$KEYCHAIN_PASSWORD_FILE" ]; then
    echo "ERROR: Keychain password file not found: $KEYCHAIN_PASSWORD_FILE"
    echo "Create it with: echo 'your-password' > scripts/keys/keychain_password"
    exit 1
fi
KEYCHAIN_PASSWORD=$(cat "$KEYCHAIN_PASSWORD_FILE")

# Check local iPhone is connected
echo "=== Checking for USB-connected iPhone ==="
UDID=$(idevice_id -l 2>/dev/null | head -1)
if [ -z "$UDID" ]; then
    echo "ERROR: No iPhone connected via USB"
    exit 1
fi
DEVICE_NAME=$(ideviceinfo -k DeviceName 2>/dev/null || echo "iPhone")
echo "Found: $DEVICE_NAME ($UDID)"

echo "=== Syncing Swift code to $MAC_HOST ==="
rsync -av --delete \
    --exclude='.git' \
    --exclude='build' \
    --exclude='*.xcodeproj' \
    --exclude='DerivedData' \
    "$LOCAL_SWIFT_DIR/" "$MAC_HOST:$REMOTE_PROJECT_DIR/"

echo "=== Unlocking keychain ==="
ssh "$MAC_HOST" "security unlock-keychain -p '$KEYCHAIN_PASSWORD' ~/Library/Keychains/build.keychain-db"

echo "=== Generating Xcode project ==="
ssh "$MAC_HOST" "cd $REMOTE_PROJECT_DIR/WindsurferTracker && /opt/homebrew/bin/xcodegen generate"

echo "=== Getting provisioning profile info ==="
IOS_PP_NAME=$(ssh "$MAC_HOST" "/usr/libexec/PlistBuddy -c 'Print Name' /dev/stdin <<< \$(/usr/bin/security cms -D -i ~/Library/MobileDevice/Provisioning\ Profiles/$IOS_PP_FILE)")
WATCH_PP_NAME=$(ssh "$MAC_HOST" "/usr/libexec/PlistBuddy -c 'Print Name' /dev/stdin <<< \$(/usr/bin/security cms -D -i ~/Library/MobileDevice/Provisioning\ Profiles/$WATCH_PP_FILE)")
WIDGET_PP_NAME=$(ssh "$MAC_HOST" "/usr/libexec/PlistBuddy -c 'Print Name' /dev/stdin <<< \$(/usr/bin/security cms -D -i ~/Library/MobileDevice/Provisioning\ Profiles/$WIDGET_PP_FILE)")
IOS_PP_UUID=$(ssh "$MAC_HOST" "/usr/libexec/PlistBuddy -c 'Print UUID' /dev/stdin <<< \$(/usr/bin/security cms -D -i ~/Library/MobileDevice/Provisioning\ Profiles/$IOS_PP_FILE)")
WATCH_PP_UUID=$(ssh "$MAC_HOST" "/usr/libexec/PlistBuddy -c 'Print UUID' /dev/stdin <<< \$(/usr/bin/security cms -D -i ~/Library/MobileDevice/Provisioning\ Profiles/$WATCH_PP_FILE)")
WIDGET_PP_UUID=$(ssh "$MAC_HOST" "/usr/libexec/PlistBuddy -c 'Print UUID' /dev/stdin <<< \$(/usr/bin/security cms -D -i ~/Library/MobileDevice/Provisioning\ Profiles/$WIDGET_PP_FILE)")

echo "=== Configuring project for manual signing ==="
ssh "$MAC_HOST" "cd $REMOTE_PROJECT_DIR/WindsurferTracker && \
    sed -i '' 's/CODE_SIGN_STYLE = Automatic;/CODE_SIGN_STYLE = Manual; CODE_SIGN_IDENTITY = \"Apple Distribution\";/g' WindsurferTracker.xcodeproj/project.pbxproj && \
    sed -i '' 's/PRODUCT_BUNDLE_IDENTIFIER = nz.co.tracker.windsurfer;/PRODUCT_BUNDLE_IDENTIFIER = nz.co.tracker.windsurfer; PROVISIONING_PROFILE_SPECIFIER = \"$IOS_PP_NAME\";/g' WindsurferTracker.xcodeproj/project.pbxproj && \
    sed -i '' 's/PRODUCT_BUNDLE_IDENTIFIER = nz.co.tracker.windsurfer.watchkitapp;/PRODUCT_BUNDLE_IDENTIFIER = nz.co.tracker.windsurfer.watchkitapp; PROVISIONING_PROFILE_SPECIFIER = \"$WATCH_PP_NAME\";/g' WindsurferTracker.xcodeproj/project.pbxproj && \
    sed -i '' 's/PRODUCT_BUNDLE_IDENTIFIER = nz.co.tracker.windsurfer.widget;/PRODUCT_BUNDLE_IDENTIFIER = nz.co.tracker.windsurfer.widget; PROVISIONING_PROFILE_SPECIFIER = \"$WIDGET_PP_NAME\";/g' WindsurferTracker.xcodeproj/project.pbxproj"

echo "=== Creating ExportOptions.plist (ad-hoc) ==="
ssh "$MAC_HOST" "cat > $REMOTE_PROJECT_DIR/WindsurferTracker/ExportOptions.plist << 'PLISTEOF'
<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\">
<dict>
    <key>method</key>
    <string>ad-hoc</string>
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
GIT_HASH=$(cd "$REPO_ROOT" && git rev-parse --short HEAD 2>/dev/null || echo "unknown")
ssh "$MAC_HOST" "cd $REMOTE_PROJECT_DIR/WindsurferTracker && \
    security unlock-keychain -p '$KEYCHAIN_PASSWORD' ~/Library/Keychains/build.keychain-db && \
    xcodebuild archive \
    -project WindsurferTracker.xcodeproj \
    -scheme WindsurferTracker \
    -destination 'generic/platform=iOS' \
    -archivePath build/WindsurferTracker.xcarchive \
    DEVELOPMENT_TEAM=$TEAM_ID \
    GIT_HASH=$GIT_HASH \
    SWIFT_ACTIVE_COMPILATION_CONDITIONS='\$(inherited) SIDELOAD' \
    OTHER_CODE_SIGN_FLAGS='--keychain ~/Library/Keychains/build.keychain-db'"

echo "=== Exporting IPA (ad-hoc) ==="
ssh "$MAC_HOST" "cd $REMOTE_PROJECT_DIR/WindsurferTracker && \
    security unlock-keychain -p '$KEYCHAIN_PASSWORD' ~/Library/Keychains/build.keychain-db && \
    xcodebuild -exportArchive \
    -archivePath build/WindsurferTracker.xcarchive \
    -exportPath build/export \
    -exportOptionsPlist ExportOptions.plist \
    OTHER_CODE_SIGN_FLAGS='--keychain ~/Library/Keychains/build.keychain-db'"

echo "=== Getting version info ==="
APP_VERSION=$(ssh "$MAC_HOST" "/usr/libexec/PlistBuddy -c 'Print CFBundleShortVersionString' $ARCHIVE_PATH/Products/Applications/Windsurfer\ Tracker.app/Info.plist")
BUILD_NUMBER=$(ssh "$MAC_HOST" "/usr/libexec/PlistBuddy -c 'Print CFBundleVersion' $ARCHIVE_PATH/Products/Applications/Windsurfer\ Tracker.app/Info.plist")
echo "Version: $APP_VERSION (build $BUILD_NUMBER, git $GIT_HASH)"

echo "=== Downloading IPA ==="
IPA_FILE=$(ssh "$MAC_HOST" "ls $EXPORT_PATH/*.ipa")
LOCAL_IPA="/tmp/WindsurferTracker.ipa"
scp "$MAC_HOST:$IPA_FILE" "$LOCAL_IPA"

echo "=== Installing on $DEVICE_NAME ==="
# ideviceinstaller sometimes hangs after install completes; use timeout
timeout 60 ideviceinstaller install "$LOCAL_IPA" || true
rm -f "$LOCAL_IPA"

echo ""
echo "=== Done! ==="
echo "Installed v$APP_VERSION (build $BUILD_NUMBER) on $DEVICE_NAME"
