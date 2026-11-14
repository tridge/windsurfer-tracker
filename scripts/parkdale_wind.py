#!/usr/bin/env python3
"""
Fetch live wind speed/gust/direction from an Ecowitt weather station share link.

The ecowitt.net share page (https://www.ecowitt.net/home/share?authorize=..&device_id=..)
is a Vue app that POSTs the same two parameters to /index/home for the live
readings and /index/get_device_info for station name/location/timezone. No login
is needed, only the share link's authorize code and device_id.

Speeds come back in whatever unit the station owner has chosen for display, so
they are normalised to knots (and m/s) here.

Examples:
  python3 scripts/parkdale_wind.py                      # Parkdale YC, print summary
  python3 scripts/parkdale_wind.py --json               # JSON to stdout
  python3 scripts/parkdale_wind.py --output wind.json   # atomic write (for cron)
  python3 scripts/parkdale_wind.py -o wind.json --history wind_history.jsonl
  python3 scripts/parkdale_wind.py --url 'https://www.ecowitt.net/home/share?authorize=XXX&device_id=YYY'
"""

import argparse
import datetime
import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

BASE = "https://www.ecowitt.net"
# Parkdale Yacht Club
DEFAULT_URL = ("https://www.ecowitt.net/home/share?authorize=VRT8BF"
               "&device_id=QjhUeXdwR3o3ZFcrSm8vWDcrYlBEQT09")

# conversion factors to knots
TO_KNOTS = {
    "knots": 1.0,
    "knot": 1.0,
    "kn": 1.0,
    "mph": 0.868976,
    "km/h": 0.539957,
    "kmh": 0.539957,
    "m/s": 1.943844,
    "ft/s": 0.592484,
}
KNOTS_TO_MS = 0.514444

COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def parse_share_url(url):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    try:
        return q["authorize"][0], q["device_id"][0]
    except KeyError:
        raise ValueError(f"share URL missing authorize/device_id: {url}")


def post(endpoint, authorize, device_id, timeout):
    data = urllib.parse.urlencode({"device_id": device_id,
                                   "authorize": authorize}).encode()
    req = urllib.request.Request(
        f"{BASE}/{endpoint}", data=data,
        headers={"User-Agent": "Mozilla/5.0 (windsurfer-tracker wind fetch)",
                 "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.load(r)
    if str(resp.get("errcode")) != "0":
        raise RuntimeError(f"{endpoint}: errcode={resp.get('errcode')} "
                           f"{resp.get('errmsg')}")
    return resp


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def speed_knots(item):
    """Convert an Ecowitt speed item {"value","unit"} to knots."""
    if not item:
        return None
    v = to_float(item.get("value"))
    if v is None:
        return None
    unit = str(item.get("unit", "")).strip().lower()
    f = TO_KNOTS.get(unit)
    if f is None:
        raise ValueError(f"unknown wind speed unit {item.get('unit')!r}")
    return round(v * f, 2)


def parse_obs_time(s, tz, now):
    """Ecowitt gives local times like 'Today 09:23', 'Yesterday 23:50' or a
    full date. Returns an aware datetime, or None if unparseable."""
    if not s:
        return None
    s = s.strip()
    today = now.astimezone(tz).date()
    day = today
    for prefix, delta in (("Today ", 0), ("Yesterday ", 1)):
        if s.startswith(prefix):
            day = today - datetime.timedelta(days=delta)
            s = s[len(prefix):]
            break
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            t = datetime.datetime.strptime(s, fmt).time()
            return datetime.datetime.combine(day, t, tzinfo=tz)
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M"):
        try:
            return datetime.datetime.strptime(s, fmt).replace(tzinfo=tz)
        except ValueError:
            pass
    return None


def fetch_wind(authorize, device_id, timeout=20):
    info = post("index/get_device_info", authorize, device_id, timeout)
    home = post("index/home", authorize, device_id, timeout)
    now = datetime.datetime.now(datetime.timezone.utc)
    tz = ZoneInfo(info.get("date_zone_id") or "UTC")

    wind = (home.get("data") or {}).get("wind", {}).get("data")
    if not wind:
        raise RuntimeError("no wind data in station response")

    spd = wind.get("windspeedmph")
    gust = wind.get("windgustmph")
    wdir = wind.get("winddir") or {}
    dir_deg = to_float(wdir.get("real_time", wdir.get("value")))
    avg10 = to_float(wdir.get("avg10m"))

    obs = parse_obs_time((spd or wdir).get("time"), tz, now)
    speed_kn = speed_knots(spd)
    gust_kn = speed_knots(gust)

    return {
        "station": {
            "name": info.get("name"),
            "lat": to_float(info.get("latitude")),
            "lon": to_float(info.get("longitude")),
            "timezone": info.get("date_zone_id"),
            "model": info.get("model"),
        },
        "fetched": int(now.timestamp()),
        "obs_ts": int(obs.timestamp()) if obs else None,
        "obs_local": obs.isoformat() if obs else None,
        "age_s": int((now - obs).total_seconds()) if obs else None,
        "speed_kn": speed_kn,
        "gust_kn": gust_kn,
        "speed_ms": round(speed_kn * KNOTS_TO_MS, 2) if speed_kn is not None else None,
        "gust_ms": round(gust_kn * KNOTS_TO_MS, 2) if gust_kn is not None else None,
        "dir_deg": dir_deg,
        "dir_avg10m_deg": avg10,
        "dir_compass": COMPASS[int((dir_deg % 360) / 22.5 + 0.5) % 16] if dir_deg is not None else None,
        "max_daily_speed_kn": speed_knots(wind.get("max_daily_windspeedmph")),
        "max_daily_gust_kn": speed_knots(wind.get("max_daily_windgustmph")),
        "source_units": (spd or {}).get("unit"),
    }


def atomic_write(path, obj):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".wind-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=1)
            f.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def last_history_ts(path):
    """obs ts of the last line in the history file, or None."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 512))
            lines = f.read().splitlines()
        return json.loads(lines[-1]).get("t") if lines else None
    except (OSError, ValueError, IndexError):
        return None


def append_history(path, w):
    """Append a compact record: t=obs unix ts, s/g=speed/gust kn,
    d=dir deg, a=10min avg dir deg. Skips repeats of the same observation."""
    t = w["obs_ts"] or w["fetched"]
    if last_history_ts(path) == t:
        return
    rec = {"t": t, "s": w["speed_kn"], "g": w["gust_kn"],
           "d": w["dir_deg"], "a": w["dir_avg10m_deg"]}
    for k in ("d", "a"):
        if rec[k] is not None and rec[k] == int(rec[k]):
            rec[k] = int(rec[k])
    with open(path, "a") as f:
        f.write(json.dumps(rec, separators=(",", ":")) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=DEFAULT_URL, help="ecowitt.net share URL")
    ap.add_argument("--json", action="store_true", help="print JSON to stdout")
    ap.add_argument("--output", "-o", help="write JSON atomically to this file")
    ap.add_argument("--history", help="append a compact record to this JSONL file")
    ap.add_argument("--timeout", type=float, default=20, help="HTTP timeout (s)")
    args = ap.parse_args()

    authorize, device_id = parse_share_url(args.url)
    try:
        w = fetch_wind(authorize, device_id, args.timeout)
    except Exception as e:
        print(f"parkdale_wind: {e}", file=sys.stderr)
        return 1
    w["url"] = args.url  # the station's full weather page, linked from the wind display

    if args.output:
        atomic_write(args.output, w)
    if args.history:
        append_history(args.history, w)
    if args.json:
        print(json.dumps(w, indent=1))
    elif not args.output and not args.history:
        print(f"{w['station']['name']}  obs {w['obs_local']} (age {w['age_s']}s)")
        print(f"  wind {w['speed_kn']} kn  gust {w['gust_kn']} kn  "
              f"from {w['dir_deg']:.0f}° {w['dir_compass']} "
              f"(10min avg {w['dir_avg10m_deg']}°)")
        print(f"  daily max {w['max_daily_speed_kn']} kn, gust {w['max_daily_gust_kn']} kn")
    return 0


if __name__ == "__main__":
    sys.exit(main())
