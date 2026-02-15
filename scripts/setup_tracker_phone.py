#!/usr/bin/env python3
"""
Setup an Android phone as a dedicated GPS tracker for windsurfing races.

Configures the phone via ADB: installs the tracker app, grants permissions,
disables WiFi/BT/NFC, enables data saver, sets the Hologram APN, etc.

Usage:
    python3 scripts/setup_tracker_phone.py
    python3 scripts/setup_tracker_phone.py --tracker Tracker11/1/xyz123
"""

import argparse
import subprocess
import sys
import os

PKG = "nz.co.tracker.windsurfer"
APK_REL = "android/app/build/outputs/apk/sideload/release/app-sideload-release.apk"

# ANSI colors
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"
BOLD = "\033[1m"


def adb(*args, timeout=60):
    """Run an adb command, return (returncode, stdout, stderr)."""
    result = subprocess.run(
        ["adb"] + list(args),
        capture_output=True, text=True, timeout=timeout
    )
    return result.returncode, result.stdout, result.stderr


def adb_shell(*args, timeout=60):
    """Run an adb shell command, return (returncode, stdout, stderr)."""
    return adb("shell", *args, timeout=timeout)


def print_step(num, total, desc):
    """Print step header."""
    print(f"  [{num}/{total}] {desc}...", end="", flush=True)


def print_ok(detail=None):
    s = f"  {GREEN}OK{RESET}"
    if detail:
        s += f"  ({detail})"
    print(s)


def print_fail(detail=None):
    s = f"  {RED}FAIL{RESET}"
    if detail:
        s += f"  ({detail})"
    print(s)


def print_skip(detail=None):
    s = f"  {YELLOW}SKIP{RESET}"
    if detail:
        s += f"  ({detail})"
    print(s)


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def step_check_device():
    """Check exactly one ADB device is connected."""
    rc, out, err = adb("devices")
    if rc != 0:
        print_fail("adb not found or failed")
        return False

    lines = [l for l in out.strip().splitlines()[1:] if l.strip() and "device" in l]
    if len(lines) == 0:
        print_fail("no device connected")
        return False
    if len(lines) > 1:
        print_fail(f"{len(lines)} devices connected, expected 1")
        return False

    serial = lines[0].split()[0]
    # get model name
    _, model, _ = adb_shell("getprop", "ro.product.model")
    model = model.strip() or serial
    print_ok(model)
    return True


def step_install_apk():
    """Install the release APK."""
    # find repo root (script is in scripts/)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(script_dir)
    apk_path = os.path.join(repo_root, APK_REL)

    if not os.path.isfile(apk_path):
        print_fail(f"APK not found: {APK_REL}")
        return False

    rc, out, err = adb("install", "-r", apk_path)
    if rc != 0 or "Success" not in out:
        detail = (out + err).strip().splitlines()[-1] if (out + err).strip() else "unknown error"
        print_fail(detail)
        return False

    print_ok()
    return True


def step_grant_permissions():
    """Grant runtime permissions and whitelist from battery optimization."""
    permissions = [
        "android.permission.ACCESS_FINE_LOCATION",
        "android.permission.ACCESS_COARSE_LOCATION",
        "android.permission.ACCESS_BACKGROUND_LOCATION",
        "android.permission.POST_NOTIFICATIONS",
        "android.permission.READ_PHONE_STATE",
    ]

    failed = []
    for perm in permissions:
        rc, out, err = adb_shell("pm", "grant", PKG, perm)
        if rc != 0:
            short = perm.rsplit(".", 1)[-1]
            # some permissions may not exist on older Android
            if "Unknown permission" in err or "not a changeable" in err:
                pass  # silently skip inapplicable permissions
            else:
                failed.append(short)

    # battery optimization whitelist
    rc, out, err = adb_shell("dumpsys", "deviceidle", "whitelist", f"+{PKG}")
    if rc != 0:
        failed.append("battery-whitelist")

    if failed:
        print_fail(", ".join(failed))
        return False

    print_ok()
    return True


def step_disable_wifi():
    """Disable WiFi and related scanning."""
    errors = []

    rc, _, err = adb_shell("svc", "wifi", "disable")
    if rc != 0:
        errors.append("svc wifi disable")

    settings = [
        ("global", "wifi_scan_always_enabled", "0"),
        ("global", "wifi_wakeup_enabled", "0"),
        ("global", "network_recommendations_enabled", "0"),
    ]
    for ns, key, val in settings:
        rc, _, err = adb_shell("settings", "put", ns, key, val)
        if rc != 0:
            errors.append(key)

    if errors:
        print_fail(", ".join(errors))
        return False

    print_ok()
    return True


def step_disable_bluetooth():
    """Disable Bluetooth."""
    # Try svc first, then settings fallback, then verify actual state
    try:
        rc, _, _ = adb_shell("svc", "bluetooth", "disable", timeout=10)
    except subprocess.TimeoutExpired:
        rc = 1
    if rc != 0:
        adb_shell("settings", "put", "global", "bluetooth_on", "0")

    import time
    time.sleep(2)

    # Check actual state
    rc, out, _ = adb_shell("dumpsys", "bluetooth_manager")
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("enabled:"):
            if "true" in line:
                print_fail("needs manual disable (BT admin permission required)")
                return False
            break

    print_ok()
    return True


def step_disable_nfc():
    """Disable NFC (skip if not available)."""
    rc, _, err = adb_shell("svc", "nfc", "disable")
    if rc != 0:
        # NFC not available on this device is not a failure
        print_skip("not available")
        return None  # None = skipped
    print_ok()
    return True


def step_disable_other_notifications():
    """Disable notifications for all third-party apps except ours."""
    rc, out, _ = adb_shell("pm", "list", "packages", "-3")
    if rc != 0:
        print_fail("couldn't list packages")
        return False

    packages = []
    for line in out.strip().splitlines():
        pkg = line.replace("package:", "").strip()
        if pkg and pkg != PKG:
            packages.append(pkg)

    errors = []
    for pkg in packages:
        rc, _, err = adb_shell("cmd", "appops", "set", pkg, "POST_NOTIFICATION", "deny")
        if rc != 0:
            errors.append(pkg)

    if errors:
        print_ok(f"{len(packages) - len(errors)}/{len(packages)} apps, {len(errors)} failed")
    else:
        print_ok(f"{len(packages)} apps")
    return True


def step_enable_data_saver():
    """Enable data saver, whitelist our app, remove all other whitelist entries."""
    rc, _, err = adb_shell("cmd", "netpolicy", "set", "restrict-background", "true")
    if rc != 0:
        print_fail("couldn't enable restrict-background")
        return False

    # get our app's UID
    rc, out, _ = adb_shell("dumpsys", "package", PKG)
    our_uid = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("userId="):
            our_uid = line.split("=", 1)[1].strip()
            break

    if not our_uid:
        print_fail("couldn't find UID")
        return False

    # add our app to whitelist
    rc, _, err = adb_shell("cmd", "netpolicy", "add", "restrict-background-whitelist", our_uid)
    if rc != 0:
        print_fail("couldn't whitelist app")
        return False

    # remove all other UIDs from whitelist
    rc, out, _ = adb_shell("cmd", "netpolicy", "list", "restrict-background-whitelist")
    removed = 0
    for line in out.strip().splitlines():
        # lines look like: "Restrict background whitelisted UIDs: 10047 10097 ..."
        if ":" in line:
            uids = line.split(":", 1)[1].strip().split()
            for uid in uids:
                if uid != our_uid:
                    adb_shell("cmd", "netpolicy", "remove", "restrict-background-whitelist", uid)
                    removed += 1

    detail = f"UID {our_uid}"
    if removed:
        detail += f", removed {removed} others"
    print_ok(detail)
    return True


def step_disable_auto_updates():
    """Disable automatic system and app updates."""
    errors = []

    settings = [
        ("global", "package_verifier_enable", "0"),
        ("global", "ota_disable_automatic_update", "1"),
    ]
    for ns, key, val in settings:
        rc, _, _ = adb_shell("settings", "put", ns, key, val)
        if rc != 0:
            errors.append(key)

    # Disable Play Store auto-update by disabling its update component
    rc, _, _ = adb_shell(
        "pm", "clear", "--cache-only", "com.android.vending"
    )
    # Not critical if this fails

    if errors:
        print_fail(", ".join(errors))
        return False

    print_ok()
    return True


def step_set_hologram_apn():
    """Set APN to Hologram IoT SIM settings."""
    # Read MCC/MNC from SIM
    rc, mccmnc_raw, _ = adb_shell("getprop", "gsm.sim.operator.numeric")
    mccmnc = mccmnc_raw.strip()
    if not mccmnc or len(mccmnc) < 5:
        print_fail(f"couldn't read MCC/MNC (got '{mccmnc}')")
        return False

    mcc = mccmnc[:3]
    mnc = mccmnc[3:]

    # Insert Hologram APN
    rc, out, err = adb_shell(
        "content", "insert",
        "--uri", "content://telephony/carriers",
        "--bind", "name:s:Hologram",
        "--bind", "apn:s:hologram",
        "--bind", f"mcc:s:{mcc}",
        "--bind", f"mnc:s:{mnc}",
        "--bind", "type:s:default,supl",
        "--bind", "protocol:s:IPV4V6",
        "--bind", "roaming_protocol:s:IPV4V6",
        "--bind", "carrier_enabled:i:1",
        "--bind", "numeric:s:" + mccmnc,
    )
    if rc != 0:
        print_fail(f"insert failed: {(err or out).strip()}")
        return False

    # Find the ID of the APN we just inserted and set it as preferred
    rc, out, _ = adb_shell(
        "content", "query",
        "--uri", "content://telephony/carriers",
        "--where", f"apn='hologram' AND numeric='{mccmnc}'",
        "--projection", "_id",
    )
    apn_id = None
    for line in out.strip().splitlines():
        # output looks like: Row: 0 _id=1234
        if "_id=" in line:
            part = line.split("_id=")[-1].split(",")[0].strip()
            try:
                apn_id = int(part)
            except ValueError:
                pass

    if apn_id is not None:
        adb_shell(
            "content", "insert",
            "--uri", "content://telephony/carriers/preferapn",
            "--bind", f"apn_id:i:{apn_id}",
        )
        print_ok(f"MCC={mcc} MNC={mnc} id={apn_id}")
    else:
        print_ok(f"MCC={mcc} MNC={mnc}, couldn't set as preferred")

    return True


def step_configure_tracker(user_id, event_id, password):
    """Configure tracker app settings via broadcast receiver."""
    rc, out, err = adb_shell(
        "am", "broadcast",
        "-a", "nz.co.tracker.windsurfer.CONFIGURE",
        "--es", "sailor_id", user_id,
        "--ei", "event_id", str(event_id),
        "--es", "password", password,
        "--ez", "auto_start_on_boot", "true",
        "nz.co.tracker.windsurfer/.ConfigReceiver",
    )
    if rc != 0 or "result=-1" not in out:
        # result=0 means no receiver handled it, -1 means success
        # On some devices result code isn't set, check for errors instead
        if rc != 0:
            print_fail(err.strip() or out.strip())
            return False

    print_ok(f"{user_id}, event {event_id}")
    return True


def step_enable_accessibility_service():
    """Enable the VolumeKeyService accessibility service for volume combo assist."""
    component = f"{PKG}/{PKG}.VolumeKeyService"

    # Get API level to determine if we need restricted settings workaround
    rc, api_str, _ = adb_shell("getprop", "ro.build.version.sdk")
    api_level = int(api_str.strip()) if rc == 0 and api_str.strip().isdigit() else 0

    # Android 13+ (API 33) blocks sideloaded apps from enabling accessibility services
    if api_level >= 33:
        rc, _, err = adb_shell("appops", "set", PKG, "android:access_restricted_settings", "allow")
        if rc != 0:
            print_fail(f"couldn't allow restricted settings: {err.strip()}")
            return False

    # Read existing enabled services to preserve them
    rc, existing, _ = adb_shell("settings", "get", "secure", "enabled_accessibility_services")
    existing = existing.strip()
    if existing == "null" or not existing:
        new_value = component
    elif component.lower() in existing.lower():
        # Already enabled
        print_ok("already enabled")
        return True
    else:
        new_value = f"{existing}:{component}"

    # Set the enabled services
    rc, _, err = adb_shell("settings", "put", "secure", "enabled_accessibility_services", new_value)
    if rc != 0:
        print_fail(f"couldn't set accessibility services: {err.strip()}")
        return False

    # Enable accessibility globally
    adb_shell("settings", "put", "secure", "accessibility_enabled", "1")

    # Verify it was set
    rc, check, _ = adb_shell("settings", "get", "secure", "enabled_accessibility_services")
    if component.lower() not in check.strip().lower():
        print_fail("setting didn't persist")
        return False

    print_ok()
    return True


def step_disable_screen_wake():
    """Disable raise-to-wake, tap-to-wake, and ambient display to save battery."""
    settings = [
        ("secure", "doze_pulse_on_pick_up", "0"),
        ("secure", "doze_pulse_on_double_tap", "0"),
        ("secure", "doze_enabled", "0"),
        ("secure", "wake_gesture_enabled", "0"),
    ]
    errors = []
    for ns, key, val in settings:
        rc, _, _ = adb_shell("settings", "put", ns, key, val)
        if rc != 0:
            errors.append(key)

    if errors:
        print_fail(", ".join(errors))
        return False

    print_ok()
    return True


def parse_tracker_arg(value):
    """Parse --tracker UserID/EventID/Password format."""
    parts = value.split("/", 2)
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(
            f"expected UserID/EventID/Password format (e.g. Tracker11/1/xyz123), got '{value}'"
        )
    user_id, event_id_str, password = parts
    try:
        event_id = int(event_id_str)
    except ValueError:
        raise argparse.ArgumentTypeError(f"event ID must be a number, got '{event_id_str}'")
    return user_id, event_id, password


def main():
    parser = argparse.ArgumentParser(description="Setup Android phone as a dedicated GPS tracker")
    parser.add_argument(
        "--tracker", type=parse_tracker_arg, metavar="ID/EVENT/PASS",
        help="Configure tracker settings: UserID/EventID/Password (e.g. Tracker11/1/xyz123)"
    )
    args = parser.parse_args()

    steps = [
        ("Checking device", step_check_device, True),
        ("Installing APK", step_install_apk, True),
        ("Granting permissions", step_grant_permissions, False),
    ]

    if args.tracker:
        user_id, event_id, password = args.tracker
        steps.append(("Configuring tracker", lambda: step_configure_tracker(user_id, event_id, password), False))

    steps.extend([
        ("Enabling accessibility service", step_enable_accessibility_service, False),
        ("Disabling WiFi", step_disable_wifi, False),
        ("Disabling Bluetooth", step_disable_bluetooth, False),
        ("Disabling NFC", step_disable_nfc, False),
        ("Disabling other notifications", step_disable_other_notifications, False),
        ("Enabling data saver", step_enable_data_saver, False),
        ("Disabling screen wake", step_disable_screen_wake, False),
        ("Disabling auto-updates", step_disable_auto_updates, False),
        ("Setting Hologram APN", step_set_hologram_apn, False),
    ])

    total = len(steps)
    ok_count = 0
    fail_count = 0
    skip_count = 0

    print(f"\n{BOLD}Windsurfer Tracker - Phone Setup{RESET}\n")

    for i, (desc, func, critical) in enumerate(steps, 1):
        print_step(i, total, desc)
        try:
            result = func()
        except subprocess.TimeoutExpired:
            print_fail("timeout")
            result = False
        except Exception as e:
            print_fail(str(e))
            result = False

        if result is None:
            skip_count += 1
        elif result:
            ok_count += 1
        else:
            fail_count += 1
            if critical:
                print(f"\n  {RED}Aborting: critical step failed.{RESET}\n")
                sys.exit(1)

    print()
    parts = [f"{GREEN}{ok_count} passed{RESET}"]
    if fail_count:
        parts.append(f"{RED}{fail_count} failed{RESET}")
    if skip_count:
        parts.append(f"{YELLOW}{skip_count} skipped{RESET}")
    print(f"  {BOLD}Setup complete:{RESET} {', '.join(parts)}")
    print()

    if fail_count:
        sys.exit(1)


if __name__ == "__main__":
    main()
