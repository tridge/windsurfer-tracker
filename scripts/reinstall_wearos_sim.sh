#!/usr/bin/env bash
set -euo pipefail

# Reinstalls the Wear OS app on the emulator, wiping all app data/settings.
# Assumes the release APK exists at wear/app/build/outputs/apk/release/app-release.apk

ADB_BIN="${ADB:-adb}"
ADB_TARGET="${ADB_TARGET:--e}"  # default to emulator; override with ADB_TARGET="" for default device
PKG="nz.co.tracker.windsurfer"
APK_PATH="wear/app/build/outputs/apk/release/app-release.apk"

if [[ ! -f "$APK_PATH" ]]; then
  echo "APK not found at $APK_PATH. Build it first (scripts/build_wearos_apk.sh)."
  exit 1
fi

echo "Uninstalling $PKG (clears data/settings)..."
$ADB_BIN $ADB_TARGET uninstall "$PKG" >/dev/null 2>&1 || true

echo "Installing $APK_PATH..."
$ADB_BIN $ADB_TARGET install "$APK_PATH"

GRANT_NOTIFICATIONS=${GRANT_NOTIFICATIONS:-1}
if [[ "$GRANT_NOTIFICATIONS" == "1" ]]; then
  # Pre-grant POST_NOTIFICATIONS so ongoing activity surfaces work without prompting.
  $ADB_BIN $ADB_TARGET shell "cmd appops set $PKG POST_NOTIFICATION allow" >/dev/null 2>&1 || true
  $ADB_BIN $ADB_TARGET shell "pm grant $PKG android.permission.POST_NOTIFICATIONS" >/dev/null 2>&1 || true
else
  # Force a fresh permission state to test first-run UX.
  $ADB_BIN $ADB_TARGET shell "pm revoke $PKG android.permission.POST_NOTIFICATIONS" >/dev/null 2>&1 || true
  $ADB_BIN $ADB_TARGET shell "cmd appops set $PKG POST_NOTIFICATION default" >/dev/null 2>&1 || true
fi

echo "Done. Launch the app to verify first-run notification prompts/ongoing activity."
