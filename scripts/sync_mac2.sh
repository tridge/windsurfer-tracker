#!/bin/bash
# Build iOS + watchOS app and upload to TestFlight
# Builds on mac2 with local signing

set -e

MAC_HOST="mac2"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOCAL_SWIFT_DIR="$SCRIPT_DIR/../swift"
REMOTE_PROJECT_DIR="~/project/windsurfer-tracker/swift"

echo "=== Syncing Swift code to $MAC_HOST ==="
rsync -av --delete \
    --exclude='.git' \
    --exclude='build' \
    --exclude='*.xcodeproj' \
    --exclude='DerivedData' \
    "$LOCAL_SWIFT_DIR/" "$MAC_HOST:$REMOTE_PROJECT_DIR/"

echo "=== Regenerating Xcode project with xcodegen ==="
ssh "$MAC_HOST" "cd $REMOTE_PROJECT_DIR/WindsurferTracker && /opt/homebrew/bin/xcodegen"
