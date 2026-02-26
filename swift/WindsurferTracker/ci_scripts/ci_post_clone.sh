#!/bin/bash
# Xcode Cloud post-clone script
# Runs after Xcode Cloud clones the repo, before building.
#
# This script:
# 1. Installs XcodeGen (project uses XcodeGen instead of a checked-in .xcodeproj)
# 2. Sets GIT_HASH build setting from the current commit
# 3. Switches signing to Automatic for Xcode Cloud's cloud-managed signing
# 4. Generates the Xcode project

set -e

PROJECT_DIR="$CI_PRIMARY_REPOSITORY_PATH/swift/WindsurferTracker"
cd "$PROJECT_DIR"

echo "=== Installing XcodeGen ==="
brew install xcodegen

echo "=== Setting GIT_HASH ==="
GIT_HASH=$(git -C "$CI_PRIMARY_REPOSITORY_PATH" rev-parse --short HEAD)
echo "GIT_HASH: $GIT_HASH"
sed -i '' "s/GIT_HASH: \"\"/GIT_HASH: \"$GIT_HASH\"/" project.yml

echo "=== Switching to Automatic signing for Xcode Cloud ==="
# The checked-in project.yml uses Manual signing in Release configs for the
# local build_testflight.sh fallback. Xcode Cloud uses cloud-managed signing,
# so we switch to Automatic. The PROVISIONING_PROFILE_SPECIFIER and
# CODE_SIGN_IDENTITY settings are ignored when signing is Automatic.
sed -i '' 's/CODE_SIGN_STYLE: Manual/CODE_SIGN_STYLE: Automatic/' project.yml

echo "=== Generating Xcode project ==="
xcodegen generate

echo "=== Done ==="
