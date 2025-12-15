#!/bin/bash
# Start Google Pixel Watch (41mm) emulator for Wear OS testing
#
# To create the AVD if it doesn't exist:
#   1. Open Android Studio
#   2. Tools -> Device Manager -> Create Device
#   3. Select "Wear OS" category -> "Pixel Watch" (41mm)
#   4. Select a Wear OS system image (API 33+ recommended)
#   5. Name it "Pixel_Watch_41mm" (or update AVD_NAME below)

EMULATOR="$ANDROID_HOME/emulator/emulator"
AVD_NAME="Pixel_Watch_41mm"

if [ -z "$ANDROID_HOME" ]; then
    ANDROID_HOME="$HOME/Android/Sdk"
    EMULATOR="$ANDROID_HOME/emulator/emulator"
fi

if [ ! -f "$EMULATOR" ]; then
    echo "Error: emulator not found at $EMULATOR"
    exit 1
fi

# Check if AVD exists
if ! "$EMULATOR" -list-avds 2>/dev/null | grep -q "^${AVD_NAME}$"; then
    echo "Error: AVD '$AVD_NAME' not found"
    echo ""
    echo "Available AVDs:"
    "$EMULATOR" -list-avds
    echo ""
    echo "To create a Pixel Watch AVD:"
    echo "  1. Open Android Studio"
    echo "  2. Tools -> Device Manager -> Create Device"
    echo "  3. Select 'Wear OS' category -> 'Pixel Watch' (41mm)"
    echo "  4. Select a Wear OS system image (API 33+ recommended)"
    echo "  5. Name it '$AVD_NAME'"
    exit 1
fi

echo "Starting Pixel Watch emulator: $AVD_NAME"
echo "Use Ctrl+C to stop"

# Run emulator with reasonable defaults
# -gpu host: Use host GPU for better performance
exec "$EMULATOR" -avd "$AVD_NAME" -gpu host "$@"
