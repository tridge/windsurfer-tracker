#!/usr/bin/env python3
"""Find contact dropouts in a set of GT06 raw logs.

A dropout = a tracker goes silent (no incoming frames at all -- not even a
heartbeat or TCP keepalive) for longer than --gap seconds, then comes back (or
never does before the log ends while other units keep reporting). This is the
"would be bad on race day" failure: a unit that should be in steady contact
falls off and you stop getting positions.

For each dropout it reports:
  * state before the gap -- ACTIVE (had been in *sustained* contact: a stream of
    frames spaced <=--active-secs apart lasting at least --active-run, i.e. live
    tracking) vs IDLE (a parked unit's brief check-in burst, or a slow cadence; a
    long gap there is far less alarming). --active-only shows only the ACTIVE ones.
  * how it came back -- reconnect (a fresh 0x01 login -- the TCP link dropped and
    re-established), stall (frames resumed on the SAME connection with no login --
    the socket stayed up but the device went quiet), or no-return.
  * RESTART? -- if many units reconnect together within --restart-window, the gap
    is almost certainly a SERVER restart (all conns reset at once), not a device
    fault. Those are tagged so you don't mistake them for dropouts.

Needs v2 logs (per-packet stream IDs); v1 can't attribute frames per tracker.

Usage:
  scripts/gt06_dropout.py gt06.log gt06.log.old2 ...
  scripts/gt06_dropout.py *.log --gap 300 --active-only
"""
import sys
import re
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gt06_dump import (read_packets, detect_format, validate_frame, fmt_time,  # noqa: E402
                       identity_from_frame)

# cxzt/CXCS firmware banner, e.g. W07_MG133_10F8G_B53_V6.68-GT06 ... / NT19D_..._V667-
_FW_RE = re.compile(rb"([A-Za-z0-9][A-Za-z0-9_.]*?V6\.?\d+)-")


def _hm(ts):
    return fmt_time(ts)[5:19]   # MM-DD HH:MM:SS


def _dur(s):
    s = int(round(s))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{sec:02d}s"
    return f"{sec}s"


def first_ts(path):
    """Receive time of the first packet in a log (for ordering files)."""
    with open(path, "rb") as f:
        fmt = detect_format(f)
        for ts, _conn, _out, _frame in read_packets(f, fmt=fmt):
            return ts
    return float("inf")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logfiles", nargs="+", help="one or more GT06 v2 binary logs")
    ap.add_argument("--gap", type=float, default=300.0,
                    help="silence longer than this (seconds) is a dropout (default: 300)")
    ap.add_argument("--active-secs", type=float, default=30.0,
                    help="frames spaced more than this apart break a contact run; "
                         "a run is the unit being in steady contact (default: 30)")
    ap.add_argument("--active-run", type=float, default=120.0,
                    help="a contact run at least this long before the gap => the "
                         "unit was ACTIVE (live tracking), not idle (default: 120)")
    ap.add_argument("--active-only", action="store_true",
                    help="only report dropouts where the unit was ACTIVE beforehand")
    ap.add_argument("--restart-window", type=float, default=60.0,
                    help="reconnects within this window flag a likely server "
                         "restart (default: 60)")
    ap.add_argument("--restart-min", type=int, default=4,
                    help="this many co-reconnecting units => server restart (default: 4)")
    ap.add_argument("--session-gap", type=float, default=1800.0,
                    help="a jump larger than this between consecutive logs is a "
                         "separate capture/restart, not a dropout: state is reset "
                         "across it so the inter-capture gap is not flagged. "
                         "Contiguous (rotated) logs abut and stay stitched (default: 1800)")
    ap.add_argument("--prefix", default="G",
                    help="sailor-id prefix for the report (default: G)")
    args = ap.parse_args()

    paths = [Path(p) for p in args.logfiles]
    for p in paths:
        if not p.exists():
            print(f"Error: {p} not found", file=sys.stderr)
            sys.exit(1)
        with open(p, "rb") as f:
            if detect_format(f) == "v1":
                print(f"Error: {p.name} is a v1 log; gt06_dropout needs v2 "
                      "(no per-packet stream IDs in v1).", file=sys.stderr)
                sys.exit(2)
    # Stitch files into one timeline in chronological order.
    paths.sort(key=first_ts)

    conn_cur = {}       # conn_id -> imei (preserved across rotation; reset per session)
    last_ts = {}        # imei -> time of its last incoming frame
    last_conn = {}      # imei -> conn_id of that frame
    last_login = {}     # imei -> time of its last 0x01 login
    run_start = {}      # imei -> start of the current contact run (<=active-secs spaced)
    fw = {}             # imei -> firmware banner
    episodes = []       # closed dropouts: dict(imei,start,end,state,kind,conn_change)
    global_first = None # min receive time across everything
    global_last = 0.0   # max receive time across everything

    def flush_open(cutoff):
        """Emit no-return dropouts for units silent >gap before `cutoff` (the end
        of a session/the data) while the server was still receiving."""
        for imei, prev in last_ts.items():
            if (cutoff - prev) > args.gap:
                run_dur = prev - run_start.get(imei, prev)
                episodes.append({
                    "imei": imei, "start": prev, "end": None, "dur": cutoff - prev,
                    "state": "active" if run_dur >= args.active_run else "idle",
                    "kind": "no-return",
                })

    prev_file_last = None   # last incoming ts of the previously-processed file
    for path in paths:
        file_last = None
        with open(path, "rb") as f:
            fmt = detect_format(f)
            for ts, conn_id, outgoing, frame in read_packets(f, fmt=fmt):
                if outgoing:
                    continue
                # Session boundary: a big jump from the previous file's end means a
                # separate capture/restart. Close out the old session's open
                # dropouts and reset all state (conn ids reuse across restarts).
                if file_last is None and prev_file_last is not None \
                        and (ts - prev_file_last) > args.session_gap:
                    flush_open(prev_file_last)
                    conn_cur.clear(); last_ts.clear(); last_conn.clear()
                    last_login.clear(); run_start.clear()
                file_last = ts
                if ts > global_last:
                    global_last = ts
                if global_first is None:
                    global_first = ts
                res = validate_frame(frame)
                if not res:
                    continue
                proto = res[0]
                ident = identity_from_frame(proto, res[1], outgoing)
                if ident:
                    conn_cur[conn_id] = ident
                    if proto == 0x01:
                        # only a real login marks a reconnect for gap purposes
                        last_login[ident] = ts
                    # fall through: a login is also "contact" for gap purposes
                if proto == 0xFA:
                    # synthetic rotation identity marker — not device contact
                    continue
                imei = conn_cur.get(conn_id)
                if not imei:
                    continue
                m = _FW_RE.search(frame)
                if m:
                    fw[imei] = m.group(1).decode()

                prev = last_ts.get(imei)
                if prev is not None and (ts - prev) > args.gap:
                    run_dur = prev - run_start.get(imei, prev)
                    state = "active" if run_dur >= args.active_run else "idle"
                    # A login after the gap => TCP re-established. Same conn and no
                    # intervening login => the socket held but the device went quiet.
                    relogin = last_login.get(imei, -1) > prev
                    if relogin or conn_id != last_conn.get(imei):
                        kind = "reconnect"
                    else:
                        kind = "stall"
                    episodes.append({
                        "imei": imei, "start": prev, "end": ts,
                        "dur": ts - prev, "state": state, "kind": kind,
                    })

                # Extend the current contact run, or start a new one if the spacing
                # since the last frame exceeds the active cadence.
                if prev is None or (ts - prev) > args.active_secs:
                    run_start[imei] = ts
                last_ts[imei] = ts
                last_conn[imei] = conn_id
        if file_last is not None:
            prev_file_last = file_last

    # Open dropouts at the very end of the data.
    flush_open(global_last)

    # Flag server restarts: a restart resets every live socket, so the fleet
    # re-LOGS-IN together. Only reconnect episodes (fresh login) count -- a stall
    # (socket held, device merely quiet) can't be caused by a restart. Cluster on
    # the re-login (gap end) time; flag a reconnect if >=restart-min units re-login
    # within restart-window of it.
    for e in episodes:
        e["restart"] = False
    relog = sorted([e for e in episodes if e["kind"] == "reconnect"],
                   key=lambda e: e["end"])
    for a in relog:
        units = {b["imei"] for b in relog
                 if abs(b["end"] - a["end"]) <= args.restart_window}
        if len(units) >= args.restart_min:
            a["restart"] = True

    shown = [e for e in episodes if not (args.active_only and e["state"] != "active")]
    shown.sort(key=lambda e: e["start"])

    names = ", ".join(p.name for p in paths)
    span = f"{_hm(global_first)} .. {_hm(global_last)}" if global_first else "(empty)"
    print(f"Dropouts > {_dur(args.gap)} across {len(paths)} log(s): {names}")
    print(f"data span ~{span};  {len(last_ts)} trackers seen\n")
    if not shown:
        extra = " ACTIVE" if args.active_only else ""
        print(f"No{extra} dropouts over {_dur(args.gap)}.")
        return

    print(f"{'start':14s}  {'tracker':8s}{'firmware':27s}{'down':>8s}  "
          f"{'state':6s} {'kind':9s} {'back at':14s} note")
    for e in shown:
        back = _hm(e["end"]) if e["end"] is not None else "(still down)"
        note = "SERVER-RESTART?" if e["restart"] else ""
        print(f"{_hm(e['start']):14s}  {args.prefix + e['imei'][-6:]:8s}"
              f"{fw.get(e['imei'], '?'):27s}{_dur(e['dur']):>8s}  "
              f"{e['state']:6s} {e['kind']:9s} {back:14s} {note}")

    # Per-tracker rollup (device dropouts only; restart clusters excluded).
    per = {}
    n_restart = 0
    for e in episodes:
        if e["restart"]:
            n_restart += 1
            continue
        if args.active_only and e["state"] != "active":
            continue
        s = per.setdefault(e["imei"], {"n": 0, "max": 0.0, "tot": 0.0, "active": 0})
        s["n"] += 1
        s["max"] = max(s["max"], e["dur"])
        s["tot"] += e["dur"]
        if e["state"] == "active":
            s["active"] += 1
    if per:
        print(f"\nPer-tracker (excluding {n_restart} server-restart reconnects):")
        print(f"  {'tracker':8s}{'firmware':27s}{'#drop':>6s}{'#active':>8s}"
              f"{'longest':>9s}{'downtime':>10s}")
        for imei, s in sorted(per.items(), key=lambda kv: -kv[1]["max"]):
            print(f"  {args.prefix + imei[-6:]:8s}{fw.get(imei, '?'):27s}"
                  f"{s['n']:6d}{s['active']:8d}{_dur(s['max']):>9s}{_dur(s['tot']):>10s}")
    n_dev = sum(s["n"] for s in per.values())
    n_act = sum(s["active"] for s in per.values())
    print(f"\n{n_dev} device dropout(s) over {_dur(args.gap)} across {len(per)} "
          f"tracker(s); {n_act} while ACTIVE; {n_restart} reconnect(s) attributed "
          f"to server restarts.")


if __name__ == "__main__":
    main()
