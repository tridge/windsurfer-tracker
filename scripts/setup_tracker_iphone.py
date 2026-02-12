#!/usr/bin/env python3
"""
Check an iPhone's readiness as a dedicated GPS tracker for windsurfing races.

Reads device state via libimobiledevice/pymobiledevice3 and prints a checklist
of manual steps needed. Unlike Android, most iOS settings cannot be changed
programmatically without MDM.

Usage:
    python3 scripts/setup_tracker_iphone.py
"""

import subprocess
import sys
import json

PKG = "nz.co.tracker.windsurfer"

# ANSI colors
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"


def run(cmd, timeout=15):
    """Run a command, return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode, result.stdout, result.stderr
    except FileNotFoundError:
        return -1, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"


def print_check(label, ok, detail=None, warn=False):
    """Print a check result line."""
    if ok is None:
        tag = f"{YELLOW}???{RESET}"
    elif ok:
        tag = f"{GREEN} OK{RESET}"
    else:
        tag = f"{YELLOW}FIX{RESET}" if warn else f"{RED}FIX{RESET}"
    line = f"  [{tag}] {label}"
    if detail:
        line += f"  {DIM}({detail}){RESET}"
    print(line)
    return ok


def check_device():
    """Check device is connected and paired. Returns device info dict or None."""
    rc, out, err = run(["idevice_id", "-l"])
    if rc != 0 or not out.strip():
        print_check("Device connected", False, "no iPhone found — connect via USB and tap Trust")
        return None

    udid = out.strip().splitlines()[0]

    rc, out, err = run(["ideviceinfo"])
    if rc != 0:
        if "Invalid HostID" in err or "lockdownd" in err:
            print_check("Device connected", False, "tap Trust on the iPhone, then retry")
        else:
            print_check("Device connected", False, err.strip())
        return None

    info = {}
    for line in out.splitlines():
        if ": " in line and not line.startswith(" "):
            key, _, val = line.partition(": ")
            info[key.strip()] = val.strip()

    name = info.get("DeviceName", "iPhone")
    model = info.get("ProductType", "?")
    ios_ver = info.get("HumanReadableProductVersionString") or info.get("ProductVersion", "?")
    print_check("Device connected", True, f"{name} ({model}, iOS {ios_ver})")
    info["_udid"] = udid
    return info


def check_app_installed():
    """Check if WindsurferTracker is installed."""
    rc, out, err = run(["ideviceinstaller", "list"])
    if rc != 0:
        print_check("Windsurfer Tracker installed", None, "couldn't list apps")
        return None

    for line in out.splitlines():
        if PKG in line:
            # parse: nz.co.tracker.windsurfer, "1.10.21", "Windsurfer Tracker"
            parts = line.split(", ")
            version = parts[1].strip('"') if len(parts) > 1 else "?"
            print_check("Windsurfer Tracker installed", True, f"v{version}")
            return version

    print_check("Windsurfer Tracker installed", False, "install via Xcode or Apple Configurator")
    return None


def check_battery():
    """Check battery level."""
    rc, out, err = run(["ideviceinfo", "-q", "com.apple.mobile.battery"])
    if rc != 0:
        print_check("Battery", None, "couldn't read")
        return

    info = {}
    for line in out.splitlines():
        if ": " in line:
            key, _, val = line.partition(": ")
            info[key.strip()] = val.strip()

    level = info.get("BatteryCurrentCapacity", "?")
    charging = info.get("BatteryIsCharging", "false") == "true"
    state = f"{level}%"
    if charging:
        state += ", charging"

    try:
        ok = int(level) >= 50
    except (ValueError, TypeError):
        ok = None

    print_check("Battery level", ok, state)


def check_wifi():
    """Check WiFi state via pymobiledevice3 diagnostics."""
    rc, out, err = run(["pymobiledevice3", "diagnostics", "wifi"])
    if rc != 0:
        print_check("WiFi disabled", None, "couldn't check — disable manually in Settings")
        return

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        print_check("WiFi disabled", None, "couldn't parse response")
        return

    power_state = data.get("IOPowerManagement", {}).get("CurrentPowerState", 0)
    ssid = data.get("IO80211SSID", "")
    # PowerState 3 = on, SSID present = connected
    # SSID is "<SSID Redacted>" when connected (pymobiledevice3 redacts it)
    wifi_on = power_state >= 3 or bool(ssid and ssid not in ("", "<>"))

    if wifi_on:
        print_check("WiFi disabled", False, "WiFi is ON — disable in Settings > Wi-Fi")
    else:
        print_check("WiFi disabled", True)


def check_sim(device_info):
    """Check SIM info from device info."""
    mcc = device_info.get("MobileSubscriberCountryCode", "")
    mnc = device_info.get("MobileSubscriberNetworkCode", "")

    # Also check from CarrierBundleInfoArray if top-level missing
    if not mcc:
        for line in subprocess.run(
            ["ideviceinfo"], capture_output=True, text=True, timeout=10
        ).stdout.splitlines():
            line = line.strip()
            if line.startswith("MCC: "):
                mcc = line.split(": ", 1)[1]
            elif line.startswith("MNC: "):
                mnc = line.split(": ", 1)[1]

    if mcc and mnc:
        print_check("SIM detected", True, f"MCC={mcc} MNC={mnc}")
    elif mcc:
        print_check("SIM detected", True, f"MCC={mcc}")
    else:
        print_check("SIM detected", False, "no SIM — insert Hologram SIM")


def main():
    print(f"\n{BOLD}Windsurfer Tracker - iPhone Setup Check{RESET}\n")

    device_info = check_device()
    if not device_info:
        sys.exit(1)

    check_app_installed()
    check_battery()
    check_wifi()
    check_sim(device_info)

    # Manual checklist
    print(f"\n{BOLD}Manual steps required on the iPhone:{RESET}\n")
    steps = [
        ("Settings > Wi-Fi", "Turn OFF"),
        ("Settings > Bluetooth", "Turn OFF"),
        ("Settings > Display & Brightness > Raise to Wake", "Turn OFF"),
        ("Settings > Accessibility > Touch > Tap to Wake", "Turn OFF"),
        ("Settings > General > Background App Refresh", "ON for Windsurfer Tracker"),
        ("Settings > Windsurfer Tracker > Location", "Always"),
        ("Settings > Notifications > Windsurfer Tracker", "Allow Notifications ON"),
        ("Settings > Battery > Low Power Mode", "Turn OFF (reduces GPS frequency)"),
        ("Settings > Cellular > Cellular Data", "Turn ON"),
        ("Settings > Cellular > APN (if Hologram SIM)", "Set APN to 'hologram'"),
    ]

    for i, (path, action) in enumerate(steps, 1):
        print(f"  {i:2}. {BOLD}{path}{RESET}")
        print(f"      → {action}")

    print(f"\n  {DIM}Open the app and configure: Your Name, Event, Password, Auto-Start on Boot{RESET}")
    print()


if __name__ == "__main__":
    main()
