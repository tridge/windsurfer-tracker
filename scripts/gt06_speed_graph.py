#!/usr/bin/env python3
"""Graph raw GT06 GPS speed per tracker from a gt06.log (v2) file.

GT06 devices report speed as a single integer-km/h byte that intermittently
drops to 0 even while moving. The server works around this in
protocol_GT06.py by replacing a reported 0 with a position-derived estimate
over a rolling 3-sample (~3 s) history. The daily jsonl track therefore has
the *smoothed* speed; to see the raw device behaviour (and check whether all
three firmwares suffer the zero-velocity quirk, not just the oldest unit) we
must read the raw protocol log.

This reads the device-reported `speed_kmh` straight from each LOC packet,
converts to knots, and plots one stacked subplot per tracker. It overlays the
position-derived speed (the same calc the server's smoother uses) as a dashed
line so a "false zero" — reported 0 while actually moving — stands out, and
prints a per-tracker summary of how many such false zeros each firmware emits.

Usage:
  python3 scripts/gt06_speed_graph.py gt06.log --ids 334189,334023,226122 \\
      --start 14:53 --end 15:30
  python3 scripts/gt06_speed_graph.py gt06.log --ids 226122 --out speed.png

IDs are the last 6 digits of each IMEI (the "Gxxxxxx" tracker id without the G).
--start/--end accept "HH:MM", "HH:MM:SS" or "YYYY-MM-DD HH:MM:SS" (log local
time); they filter on (and the x-axis uses) the packet receive time by default,
or the embedded GPS fix time with --gps-time (cleaner where a buffered replay
makes the two diverge).
"""

import argparse
import math
import sys
from datetime import datetime
from pathlib import Path

import matplotlib  # backend selected in main() before pyplot import

# Reuse the frame machinery + parsers from gt06_dump (it also resolves the
# protocol_GT06 import path for us).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from gt06_dump import (  # noqa: E402
    detect_format, read_packets, validate_frame,
    gt06_parse_login, gt06_parse_location, _imei_matches,
)

# Per-device firmware (last-6 IMEI), from cxzt# strings on 2026-05-30.
FIRMWARE = {
    "378848": "W07_V6.68", "375356": "W07_V6.68",
    "312334": "W07_V6.68", "375547": "W07_V6.68",
    "226122": "W07_V6.63",
    "370589": "NT19D_V667", "388862": "NT19D_V667", "334189": "NT19D_V667",
    "334023": "NT19D_V667", "306088": "NT19D_V667", "347082": "NT19D_V667",
}
FW_COLOR = {"W07_V6.63": "tab:red", "W07_V6.68": "tab:orange",
            "NT19D_V667": "tab:blue", None: "tab:gray"}

KMH_TO_KN = 1.0 / 1.852


def parse_when(s, ref_dt):
    """Parse a --start/--end time string to a unix timestamp. Accepts a full
    'YYYY-MM-DD HH:MM[:SS]' or a bare 'HH:MM[:SS]' (combined with ref_dt's
    date — the log's date)."""
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            pass
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            t = datetime.strptime(s, fmt)
            return ref_dt.replace(hour=t.hour, minute=t.minute,
                                  second=t.second, microsecond=0).timestamp()
        except ValueError:
            pass
    raise SystemExit(f"error: could not parse time {s!r}")


def derived_knots(old, new):
    """Position-derived speed (knots) between two (lat, lon, gps_ts) samples,
    mirroring the server's smoother. Returns None if dt is out of (0, 5)s."""
    old_lat, old_lon, old_ts = old
    lat, lon, ts = new
    dt = ts - old_ts
    if not (0 < dt < 5):
        return None
    lat_nm = (lat - old_lat) * 60.0
    lon_nm = (lon - old_lon) * 60.0 * math.cos(math.radians(lat))
    dist_nm = math.hypot(lat_nm, lon_nm)
    return dist_nm * 3600.0 / dt


def collect(logfile, ids):
    """Single streaming pass over the log. Returns {id6: [sample, ...]} where
    sample = (recv_ts, gps_ts, speed_kn, lat, lon), in receive order. LOGIN
    frames precede that connection's LOC frames, so we can map conn->IMEI as
    we go."""
    out = {i: [] for i in ids}
    conn_to_id = {}
    with open(logfile, "rb") as f:
        fmt = detect_format(f)
        if fmt != "v2":
            raise SystemExit("error: need a v2 gt06.log (has per-connection "
                             "stream ids); this file is v1.")
        for recv_ts, conn_id, outgoing, frame in read_packets(f, fmt):
            if outgoing or not conn_id:
                continue
            res = validate_frame(frame)
            if not res:
                continue
            protocol, data, serial, crc_ok = res
            if protocol == 0x01:
                imei = gt06_parse_login(data)
                if imei:
                    match = next((i for i in ids if _imei_matches(i, imei)), None)
                    if match:
                        conn_to_id[conn_id] = match
            elif protocol in (0x12, 0x22):
                tid = conn_to_id.get(conn_id)
                if tid is None:
                    continue
                loc = gt06_parse_location(data)
                if loc is None:
                    continue
                out[tid].append((recv_ts, loc["ts"], loc["speed_kmh"] * KMH_TO_KN,
                                 loc["lat"], loc["lon"]))
    return out


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logfile", help="gt06.log (v2 format)")
    ap.add_argument("--ids", required=True,
                    help="comma-separated tracker ids (last 6 digits of IMEI)")
    ap.add_argument("--start", help="window start (HH:MM[:SS] or full datetime)")
    ap.add_argument("--end", help="window end (HH:MM[:SS] or full datetime)")
    ap.add_argument("--threshold", type=float, default=2.0,
                    help="speed (kn) above which a reported 0 counts as a "
                         "'false zero' vs derived speed (default: 2.0)")
    ap.add_argument("--no-derived", action="store_true",
                    help="don't overlay the position-derived speed line")
    ap.add_argument("--gps-time", action="store_true",
                    help="plot/filter against the embedded GPS fix time instead "
                         "of the packet receive time (cleaner across buffered "
                         "replay, where the two diverge)")
    ap.add_argument("--out", help="save PNG to this path instead of showing")
    args = ap.parse_args()

    if args.out:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    ids = [s.strip() for s in args.ids.split(",") if s.strip()]
    data = collect(args.logfile, ids)

    # time basis: index 0 = packet receive time, index 1 = embedded GPS fix time
    ti = 1 if args.gps_time else 0
    all_t = [s[ti] for tid in ids for s in data[tid]]
    if not all_t:
        raise SystemExit("error: no LOC packets for the requested ids")
    ref_dt = datetime.fromtimestamp(min(all_t))
    t0 = parse_when(args.start, ref_dt) if args.start else None
    t1 = parse_when(args.end, ref_dt) if args.end else None

    def in_window(ts):
        return (t0 is None or ts >= t0) and (t1 is None or ts <= t1)

    fig, axes = plt.subplots(len(ids), 1, sharex=True, squeeze=False,
                             figsize=(14, max(2.2 * len(ids), 3)))
    axes = [row[0] for row in axes]

    print(f"{'id':<8}{'fw':>12}{'n':>7}{'zeros':>7}{'false0':>8}{'false%':>8}"
          f"{'max kn':>8}")
    print("-" * 58)

    for ax, tid in zip(axes, ids):
        fw = FIRMWARE.get(tid)
        samples = [s for s in data[tid] if in_window(s[ti])]
        color = FW_COLOR.get(fw, "tab:gray")
        if not samples:
            ax.text(0.5, 0.5, f"{tid}: no data in window", ha="center",
                    va="center", transform=ax.transAxes)
            ax.set_ylabel(tid)
            print(f"{tid:<8}{str(fw):>12}{0:>7}")
            continue

        times = [datetime.fromtimestamp(s[ti]) for s in samples]
        rep = [s[2] for s in samples]

        # position-derived speed (rolling up-to-3-sample history, like server)
        hist = []
        derived = []
        for (_rts, gts, _spd, lat, lon) in samples:
            d = derived_knots(hist[0], (lat, lon, gts)) if hist else None
            derived.append(d)
            hist.append((lat, lon, gts))
            if len(hist) > 3:
                hist.pop(0)

        ax.plot(times, rep, "-", color=color, lw=0.9, label="reported")
        if not args.no_derived:
            dt = [t for t, d in zip(times, derived) if d is not None]
            dv = [d for d in derived if d is not None]
            ax.plot(dt, dv, "--", color="0.4", lw=0.8, alpha=0.7,
                    label="derived (pos)")

        # highlight reported zeros; flag the ones that are "false" (moving)
        zero_t = [t for t, r in zip(times, rep) if r == 0]
        zero_y = [0] * len(zero_t)
        ax.plot(zero_t, zero_y, ".", color="red", ms=3, label="reported 0")

        n = len(samples)
        n_zero = sum(1 for r in rep if r == 0)
        n_false = sum(1 for r, d in zip(rep, derived)
                      if r == 0 and d is not None and d > args.threshold)
        false_pct = 100.0 * n_false / n if n else 0.0
        ax.set_ylabel(f"{tid}\n{fw}", fontsize=8)
        ax.grid(True, alpha=0.3)
        ax.margins(x=0.01)
        ax.set_ylim(bottom=0)
        print(f"{tid:<8}{str(fw):>12}{n:>7}{n_zero:>7}{n_false:>8}"
              f"{false_pct:>7.1f}%{max(rep):>8.1f}")

    axes[0].legend(loc="upper right", fontsize=8, ncol=3)
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
    win = ""
    if args.start or args.end:
        win = f"  [{args.start or 'start'} .. {args.end or 'end'}]"
    fig.suptitle(f"GT06 raw GPS speed (knots){win}  —  "
                 f"red=reported 0, dashed=position-derived", fontsize=11)
    fig.supxlabel(f"time (local, {'GPS fix' if args.gps_time else 'packet receive'})")
    fig.tight_layout(rect=(0, 0, 1, 0.98))

    print("\n(false0 = reported speed 0 while position-derived speed "
          f"> {args.threshold:g} kn — i.e. a moving zero the smoother must fix)")

    if args.out:
        fig.savefig(args.out, dpi=110)
        print(f"\nwrote {args.out}")
    else:
        plt.show()


if __name__ == "__main__":
    main()
