#!/usr/bin/env python3
"""Report GT06 trackers that exhibited replay/blind-buffer lag in a log.

Lag = packet receive time (server clock) - the embedded GPS fix time. A unit
streaming live at 1 Hz sits at ~1-2 s; a unit replaying a buffered backlog after
a reconnect delivers fixes whose GPS time is well behind wall-clock. This scans a
v2 gt06.log and lists every tracker with at least one LOC whose lag exceeds
--lag, with its firmware version (captured from the cxzt/CXCS reply on the
connection).

Usage:
  scripts/gt06_lag.py gt06.log --lag 30
  scripts/gt06_lag.py gt06.log --lag 60 --valid-only
"""
import sys
import re
import argparse
from pathlib import Path

# Reuse the dump tool's log reader + protocol parsers (importing it also sets up
# the protocol_GT06 import path).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from gt06_dump import read_packets, detect_format, validate_frame, fmt_time  # noqa: E402
from protocol_GT06 import gt06_parse_login, gt06_parse_location  # noqa: E402

# The cxzt/CXCS reply carries the firmware banner, e.g.
#   W07_MG133_10F8G_B53_V6.68-GT06 MCU:...*ID:<id>*...
#   NT19D_MG133_10F8G_B53_V667-...*ID:<id>*...
_FW_RE = re.compile(rb"([A-Za-z0-9][A-Za-z0-9_.]*?V6\.?\d+)-")
_ID_RE = re.compile(rb"ID:(\d{6,15})")


def _hms(ts):
    return fmt_time(ts)[11:19]


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logfile", nargs="?", default="gt06.log",
                    help="GT06 v2 binary log (default: gt06.log)")
    ap.add_argument("--lag", type=float, default=30.0,
                    help="lag threshold in seconds (default: 30)")
    ap.add_argument("--valid-only", action="store_true",
                    help="only count LOC packets with a valid GPS fix")
    ap.add_argument("--prefix", default="G",
                    help="sailor-id prefix for the report (default: G)")
    args = ap.parse_args()

    logpath = Path(args.logfile)
    if not logpath.exists():
        print(f"Error: {logpath} not found", file=sys.stderr)
        sys.exit(1)
    with open(logpath, "rb") as f:
        if detect_format(f) == "v1":
            print("Error: gt06_lag needs a v2 log (no per-packet stream IDs in v1).",
                  file=sys.stderr)
            sys.exit(2)

    conn_cur = {}   # conn_id -> current login id (last-login-wins; conn ids reuse)
    fw = {}         # last6 -> firmware string (from cxzt)
    stats = {}      # last6 -> {n, max, first, last}

    with open(logpath, "rb") as f:
        fmt = detect_format(f)
        for ts, conn_id, outgoing, frame in read_packets(f, fmt=fmt):
            result = validate_frame(frame)
            if not result:
                continue
            proto, data, serial, crc_ok = result
            if proto == 0x01 and not outgoing:
                imei = gt06_parse_login(data)
                if imei:
                    conn_cur[conn_id] = imei
                continue
            if outgoing:
                continue
            # Firmware banner from the device's cxzt/CXCS reply.
            m = _FW_RE.search(frame)
            if m:
                mid = _ID_RE.search(frame)
                cur = conn_cur.get(conn_id)
                last6 = (mid.group(1).decode()[-6:] if mid
                         else (cur[-6:] if cur else None))
                if last6:
                    fw[last6] = m.group(1).decode()
            if proto in (0x12, 0x22):
                cur = conn_cur.get(conn_id)
                if not cur:
                    continue
                loc = gt06_parse_location(data)
                if not loc or not loc.get("ts"):
                    continue
                if args.valid_only and not loc.get("gps_valid"):
                    continue
                lag = ts - loc["ts"]
                if lag <= args.lag:
                    continue
                last6 = cur[-6:]
                s = stats.get(last6)
                if s is None:
                    stats[last6] = {"n": 1, "max": lag, "first": ts, "last": ts}
                else:
                    s["n"] += 1
                    s["max"] = max(s["max"], lag)
                    s["last"] = ts
                    if ts < s["first"]:
                        s["first"] = ts

    suffix = " (valid fix only)" if args.valid_only else ""
    if not stats:
        print(f"No trackers with LOC lag > {args.lag:.0f}s in {logpath.name}{suffix}.")
        return
    print(f"Trackers with LOC lag > {args.lag:.0f}s in {logpath.name}{suffix}:\n")
    print(f"{'tracker':9s}{'firmware':32s}{'#lag':>6s}{'maxlag':>9s}   "
          f"{'first':8s}  {'last':8s}")
    for last6, s in sorted(stats.items(), key=lambda kv: -kv[1]["max"]):
        print(f"{args.prefix + last6:9s}{fw.get(last6, '?'):32s}{s['n']:6d}"
              f"{s['max']:8.0f}s   {_hms(s['first'])}  {_hms(s['last'])}")
    print(f"\n{len(stats)} tracker(s) over threshold.")


if __name__ == "__main__":
    main()
