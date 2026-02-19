#!/usr/bin/env python3
"""Query Hologram SIMs by tag and show data usage vs plan limits."""

import argparse
import os
import sys
import requests

BASE_URL = "https://dashboard.hologram.io/api/1"


def load_api_key():
    path = os.path.expanduser("~/.hologram.key")
    try:
        return open(path).read().strip()
    except FileNotFoundError:
        print(f"Error: API key file not found: {path}", file=sys.stderr)
        sys.exit(1)


def find_orgid(auth):
    """Find the first non-personal org that has devices."""
    resp = requests.get(f"{BASE_URL}/organizations", auth=auth)
    resp.raise_for_status()
    orgs = resp.json().get("data", [])
    for org in orgs:
        if org.get("is_personal"):
            continue
        orgid = org["id"]
        r = requests.get(f"{BASE_URL}/devices", auth=auth, params={"orgid": orgid, "limit": 1})
        if r.ok and r.json().get("data"):
            return orgid
    # fall back to first org with devices
    for org in orgs:
        orgid = org["id"]
        r = requests.get(f"{BASE_URL}/devices", auth=auth, params={"orgid": orgid, "limit": 1})
        if r.ok and r.json().get("data"):
            return orgid
    return None


def get_devices(auth, orgid, tagname):
    devices = []
    params = {"limit": 100, "orgid": orgid, "tagname": tagname}
    url = f"{BASE_URL}/devices"
    while True:
        resp = requests.get(url, auth=auth, params=params)
        resp.raise_for_status()
        data = resp.json()
        devices.extend(data.get("data", []))
        if not data.get("continues"):
            break
        # pagination: use startafter with last device id
        last_id = data["data"][-1]["id"] if data["data"] else None
        if not last_id:
            break
        params["startafter"] = last_id
    return devices


def fmt(b):
    if b < 1024:
        return f"{b} B"
    if b < 1024 * 1024:
        return f"{b / 1024:.1f} KB"
    if b < 1024 * 1024 * 1024:
        return f"{b / (1024 * 1024):.1f} MB"
    return f"{b / (1024 * 1024 * 1024):.2f} GB"


def main():
    parser = argparse.ArgumentParser(description="Check Hologram SIM data usage")
    parser.add_argument("--tag", default="Trackers", help="Device tag to filter (default: Trackers)")
    parser.add_argument("--orgid", type=int, help="Hologram org ID (auto-detected if omitted)")
    parser.add_argument("--warn", type=float, default=80, help="Warn threshold percentage (default: 80)")
    args = parser.parse_args()

    api_key = load_api_key()
    auth = ("apikey", api_key)

    orgid = args.orgid
    if not orgid:
        orgid = find_orgid(auth)
        if not orgid:
            print("Error: no org with devices found", file=sys.stderr)
            sys.exit(1)

    devices = get_devices(auth, orgid, args.tag)

    if not devices:
        print(f"No devices found with tag '{args.tag}' in org {orgid}")
        return

    rows = []
    for dev in devices:
        name = dev.get("name", "?")
        for link in dev.get("links", {}).get("cellular", []):
            state = link.get("state", "?")
            used = link.get("cur_billing_data_used", 0)
            plan = link.get("plan", {})
            plan_data = plan.get("data", 0)
            overage = link.get("overagelimit", 0)
            expires = link.get("whenexpires", "")[:10]
            if overage == -1:
                cap = None
                pct = (used / plan_data * 100) if plan_data > 0 else 0
            else:
                cap = plan_data + overage
                pct = (used / cap * 100) if cap > 0 else 0
            rows.append((name, state, used, cap, pct, expires))

    rows.sort(key=lambda r: -r[4])

    hdr = f"{'Device':<20} {'State':<8} {'Used':>10} {'Cap':>10} {'Remaining':>10} {'%':>6}  {'Renews':<10}"
    print(hdr)
    print("-" * len(hdr))
    for name, state, used, cap, pct, expires in rows:
        cap_s = "Unlim" if cap is None else fmt(cap)
        rem_s = "Unlim" if cap is None else f"{max(0, cap - used) / (1024*1024):.1f} MB"
        warn = " <<<" if pct >= args.warn else ""
        print(f"{name:<20} {state:<8} {fmt(used):>10} {cap_s:>10} {rem_s:>10} {pct:>5.1f}%{warn}  {expires}")

    print(f"\n{len(rows)} SIMs, tag='{args.tag}'")


if __name__ == "__main__":
    main()
