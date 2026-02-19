#!/bin/bash
# Install pre-built iOS IPA to locally USB-connected iPhone
#
# Requires build_ios_sideload.sh to have been run first.
#
# Usage:
#   scripts/build_ios_sideload.sh   # build first
#   scripts/install_ios_usb.sh      # then install

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOCAL_IPA="$SCRIPT_DIR/../app/WindsurferTracker.ipa"

# Check local iPhone is connected
echo "=== Checking for USB-connected iPhone ==="
UDID=$(idevice_id -l 2>/dev/null | head -1)
if [ -z "$UDID" ]; then
    echo "ERROR: No iPhone connected via USB"
    exit 1
fi
DEVICE_NAME=$(ideviceinfo -k DeviceName 2>/dev/null || echo "iPhone")
echo "Found: $DEVICE_NAME ($UDID)"

# Check IPA exists locally (left by build_ios_sideload.sh)
if [ ! -f "$LOCAL_IPA" ]; then
    echo "ERROR: No IPA found at $LOCAL_IPA"
    echo "Run scripts/build_ios_sideload.sh first"
    exit 1
fi

echo "=== Installing on $DEVICE_NAME ==="
# ideviceinstaller sometimes hangs after install completes; use timeout
timeout 60 ideviceinstaller install "$LOCAL_IPA" || true

echo ""
echo "=== Done! ==="
echo "Installed on $DEVICE_NAME"
