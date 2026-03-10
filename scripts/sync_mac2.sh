#!/bin/bash
# Sync repo to mac2 and regenerate Xcode project
# Syncs committed code via .git, then overlays uncommitted swift/ changes

set -e

MAC_HOST="mac2"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOCAL_REPO_DIR="$SCRIPT_DIR/.."
REMOTE_REPO_DIR="~/project/windsurfer-tracker"

echo "=== Syncing .git to $MAC_HOST ==="
rsync -av "$LOCAL_REPO_DIR/.git/" "$MAC_HOST:$REMOTE_REPO_DIR/.git/"

echo "=== Checking out working tree ==="
ssh "$MAC_HOST" "cd $REMOTE_REPO_DIR && git reset --hard HEAD"

echo "=== Syncing uncommitted swift changes ==="
rsync -av --delete \
    --exclude='build' \
    --exclude='*.xcodeproj' \
    --exclude='DerivedData' \
    "$LOCAL_REPO_DIR/swift/" "$MAC_HOST:$REMOTE_REPO_DIR/swift/"

echo "=== Regenerating Xcode project with xcodegen ==="
ssh "$MAC_HOST" "cd $REMOTE_REPO_DIR/swift/WindsurferTracker && /opt/homebrew/bin/xcodegen"
