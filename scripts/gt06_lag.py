#!/usr/bin/env python3
"""Report GT06 trackers that exhibited replay/blind-buffer lag in a log.

Lag = packet receive time (server clock) - the embedded GPS fix time. A unit
streaming live at 1 Hz sits at ~1-2 s; a unit replaying a buffered backlog after
a reconnect delivers fixes whose GPS time is well behind wall-clock. This scans a
v2 gt06.log and lists every tracker with at least one LOC whose lag exceeds
--lag, with its firmware version (captured from the cxzt/CXCS reply on the
connection).

It also flags whether the lagged fixes drained as a pure in-order catch-up or
whether *live* fixes were interleaved with the blind-buffer backlog. A pure drain
walks the buffer old->new (GPS time monotonic, lag shrinking) and settles to live
once (one LAG->LIVE transition). Interleaving is when the stream goes back to
lagged AFTER it had already caught up to live (a LIVE->LAG transition), or the
GPS time steps backwards mid-stream -- i.e. the firmware mixes current fixes in
among the backlog replay. The `intl` column counts those LIVE->LAG transitions;
`back` counts GPS-time backward steps. Use --detail to print the per-fix timeline.

Usage:
  scripts/gt06_lag.py gt06.log --lag 30
  scripts/gt06_lag.py gt06.log --lag 60 --valid-only
  scripts/gt06_lag.py gt06.log --lag 30 --detail            # timeline for all
  scripts/gt06_lag.py gt06.log --lag 30 --detail --id G123456
"""
import sys
import re
import argparse
from pathlib import Path

# Reuse the dump tool's log reader + protocol parsers (importing it also sets up
# the protocol_GT06 import path).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from gt06_dump import (read_packets, detect_format, validate_frame, fmt_time,  # noqa: E402
                       identity_from_frame)
from protocol_GT06 import gt06_parse_location  # noqa: E402

# The cxzt/CXCS reply carries the firmware banner, e.g.
#   W07_MG133_10F8G_B53_V6.68-GT06 MCU:...*ID:<id>*...
#   NT19D_MG133_10F8G_B53_V667-...*ID:<id>*...
_FW_RE = re.compile(rb"([A-Za-z0-9][A-Za-z0-9_.]*?V6\.?\d+)-")
_ID_RE = re.compile(rb"ID:(\d{6,15})")

# GPS time stepping back by more than this (seconds) counts as a backward step.
_BACK_EPS = 2.0


def _hms(ts):
    return fmt_time(ts)[11:19]


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logfiles", nargs="*", default=["gt06.log"],
                    help="one or more GT06 v2 binary logs (default: gt06.log); "
                         "processed in the given order, results merged")
    ap.add_argument("--lag", type=float, default=30.0,
                    help="lag threshold in seconds (default: 30)")
    ap.add_argument("--live", type=float, default=5.0,
                    help="lag at/below which a fix is 'live' for interleave "
                         "detection (default: 5)")
    ap.add_argument("--valid-only", action="store_true",
                    help="only count LOC packets with a valid GPS fix")
    ap.add_argument("--detail", action="store_true",
                    help="print the per-fix lag timeline for each tracker")
    ap.add_argument("--id", default=None,
                    help="restrict --detail to this tracker (e.g. G123456)")
    ap.add_argument("--prefix", default="G",
                    help="sailor-id prefix for the report (default: G)")
    args = ap.parse_args()

    logpaths = [Path(p) for p in args.logfiles]
    for logpath in logpaths:
        if not logpath.exists():
            print(f"Error: {logpath} not found", file=sys.stderr)
            sys.exit(1)
        with open(logpath, "rb") as f:
            if detect_format(f) == "v1":
                print(f"Error: {logpath.name} is a v1 log; gt06_lag needs v2 "
                      "(no per-packet stream IDs in v1).", file=sys.stderr)
                sys.exit(2)

    detail_filter = None
    if args.id:
        detail_filter = args.id[len(args.prefix):] if args.id.startswith(args.prefix) else args.id
        detail_filter = detail_filter[-6:]

    fw = {}         # last6 -> firmware string (from cxzt)
    stats = {}      # last6 -> {n, max, first, last}
    inter = {}      # last6 -> {live, l2v, v2l, back}
    detail = {}     # last6 -> [(recv_ts, gps_ts, lag, label)] (only when --detail)

    def inter_for(last6):
        d = inter.get(last6)
        if d is None:
            d = {"live": 0, "l2v": 0, "v2l": 0, "back": 0}
            inter[last6] = d
        return d

    for logpath in logpaths:
      # conn_ids reset per server restart and so collide across files; clear the
      # per-connection state at each file boundary to avoid blending sessions.
      conn_cur = {}   # conn_id -> current login id (last-login-wins; conn ids reuse)
      # Per-connection interleave-detection state, reset on each login. Tracks the
      # last firm LIVE/LAG label and previous GPS time so transitions and GPS-time
      # backward steps can be attributed to the tracker as they happen.
      conn_st = {}    # conn_id -> {"label", "prev_gps"}
      with open(logpath, "rb") as f:
        fmt = detect_format(f)
        for ts, conn_id, outgoing, frame in read_packets(f, fmt=fmt):
            result = validate_frame(frame)
            if not result:
                continue
            proto, data, serial, crc_ok = result
            imei = identity_from_frame(proto, data, outgoing)
            if imei and (proto == 0x01 or conn_cur.get(conn_id) != imei):
                # A login always resets the interleave state; the 0xFA rotation
                # marker / cxzt *ID:* replies only do so on an identity change.
                conn_cur[conn_id] = imei
                conn_st[conn_id] = {"label": None, "prev_gps": None}
            if proto in (0x01, 0xFA):
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
                last6 = cur[-6:]
                lag = ts - loc["ts"]
                gps_ts = loc["ts"]

                # Classify this fix and track LIVE<->LAG transitions + GPS-time
                # backward steps, per connection, attributed to the tracker.
                st = conn_st.get(conn_id)
                if st is None:
                    st = conn_st[conn_id] = {"label": None, "prev_gps": None}
                if lag > args.lag:
                    label = "L"
                elif lag <= args.live:
                    label = "V"
                else:
                    label = None   # ambiguous: keep prior label
                idc = inter_for(last6)
                if label == "V":
                    idc["live"] += 1
                if label is not None:
                    prev = st["label"]
                    if prev == "V" and label == "L":
                        idc["v2l"] += 1   # caught up, then fell back -> interleave
                    elif prev == "L" and label == "V":
                        idc["l2v"] += 1   # normal end-of-drain catch-up
                    st["label"] = label
                if st["prev_gps"] is not None and gps_ts < st["prev_gps"] - _BACK_EPS:
                    idc["back"] += 1
                st["prev_gps"] = gps_ts

                if args.detail and (detail_filter is None or last6 == detail_filter):
                    detail.setdefault(last6, []).append(
                        (ts, gps_ts, lag, label or "."))

                if lag <= args.lag:
                    continue
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
    names = ", ".join(p.name for p in logpaths)
    if not stats:
        print(f"No trackers with LOC lag > {args.lag:.0f}s in {names}{suffix}.")
        return
    print(f"Trackers with LOC lag > {args.lag:.0f}s in {names}{suffix}:\n")
    print(f"{'tracker':9s}{'firmware':28s}{'#lag':>5s}{'maxlag':>8s}"
          f"{'intl':>6s}{'back':>6s}   {'first':8s}  {'last':8s}")
    for last6, s in sorted(stats.items(), key=lambda kv: -kv[1]["max"]):
        idc = inter.get(last6, {"v2l": 0, "back": 0})
        print(f"{args.prefix + last6:9s}{fw.get(last6, '?'):28s}{s['n']:5d}"
              f"{s['max']:7.0f}s{idc['v2l']:6d}{idc['back']:6d}   "
              f"{_hms(s['first'])}  {_hms(s['last'])}")
    n_intl = sum(1 for k in stats if inter.get(k, {}).get("v2l", 0) > 0
                 or inter.get(k, {}).get("back", 0) > 0)
    print(f"\n{len(stats)} tracker(s) over threshold; {n_intl} with blind-buffer "
          f"catch-up interleaving (intl/back > 0).")
    print("  intl = LIVE->LAG transitions (caught up then fell back to backlog)")
    print("  back = GPS-time backward steps (live fix slotted among older replay)")

    if args.detail:
        for last6 in sorted(detail, key=lambda k: stats.get(k, {}).get("max", 0),
                            reverse=True):
            if last6 not in stats:
                continue
            print(f"\n--- {args.prefix + last6} timeline "
                  f"(recv / gps / lag, V=live L=lagged) ---")
            for recv_ts, gps_ts, lag, label in detail[last6]:
                print(f"  {_hms(recv_ts)}  gps {_hms(gps_ts)}  "
                      f"lag {lag:6.0f}s  {label}")


if __name__ == "__main__":
    main()
