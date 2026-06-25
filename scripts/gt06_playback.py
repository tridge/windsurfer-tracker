#!/usr/bin/env python3
"""Replay a captured GT06 v2 log into a live server as a FAKED device.

Reads a `gt06.log` (v2), filters to the device→server frames, and replays them
over TCP at wall-clock rate while rewriting:

  * the login IMEI  -> --map-imei  (so it shows up as a new device)
  * each LOC GPS time -> shifted to "now" while PRESERVING that frame's lag
    (receive_ts - gps_ts), so a blind-buffer interleave (live lag ~2s mixed with
    blind lag ~45s) is reproduced exactly against the current wall clock.

The capture's TCP reconnects are mirrored (one socket per conn_id) so an offline
gap reproduces a real disconnect+reconnect rather than tripping the server's
heartbeat timeout. Server commands are read and DISCARDED — the playback never
ACKs anything, which also exercises the server's tolerance of a device that does
not answer requests.

The tool only does the wire replay. With TERIID anti-spoofing on (a login master
key set, the production default since 2026-06-23) the server only PUBLISHES — i.e.
records a track for — units that authenticate as a provisioned TERIID or as a
'sim' device. A raw/legacy IMEI logs in but is dropped before process_position, so
nothing reaches the event jsonl. So fake a SIM device: use a 999-prefixed IMEI
whose digits 3-4 are the target event id, e.g. 999 + 04 + ... for RaceTest eid=4:

  --map-imei 999040000900001   # -> sim, eid 4, sailor G900001, publishes

(No UI mapping needed — the eid is encoded in the IMEI.) The target event must be
in tracking state (start-all), else idle-privacy records no lat/lon.

  # eyeball the rewrites, no sockets:
  scripts/gt06_playback.py gt06/blind/G378848_V668_2026-05-30.log \
      --map-imei 999040000900001 --start "2026-05-30 13:50:42" \
      --end "2026-05-30 13:56:30" --dry-run -v

  # live replay against the server's GT06 port:
  scripts/gt06_playback.py gt06/blind/G378848_V668_2026-05-30.log \
      --map-imei 999040000900001 --host wstracker.org --port 7711 \
      --start "2026-05-30 13:50:42" --end "2026-05-30 13:56:30"
"""
import sys
import time
import socket
import select
import struct
import calendar
import argparse
from pathlib import Path
from datetime import datetime, timezone

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here))
for _c in (_here.parent / "server", _here.parent, _here):
    if (_c / "gt06_frames.py").exists():
        sys.path.insert(0, str(_c))
        break

from gt06_dump import detect_format, read_packets, validate_frame, dump_packet, fmt_time  # noqa: E402
from gt06_frames import build_frame, build_login_data  # noqa: E402

LOGIN = 0x01
LOC = (0x12, 0x22)
HEARTBEAT = 0x13


def parse_dt(s):
    """Local-time datetime string -> epoch (matches gt06_dump's display tz)."""
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(s, fmt).timestamp()
            except ValueError:
                continue
    raise SystemExit(f"bad --start/--end time: {s!r} (use 'YYYY-MM-DD HH:MM:SS[.fff]')")


def gps_epoch(data):
    """Decode a LOC frame's embedded GPS time (data[0:6], UTC) -> epoch, or None."""
    if len(data) < 6:
        return None
    try:
        return calendar.timegm((2000 + data[0], data[1], data[2], data[3], data[4], data[5], 0, 0, 0))
    except (ValueError, OverflowError):
        return None


def rewrite(proto, data, serial, frame, imei, send_epoch, orig_lag):
    """Return the frame bytes to send, or None to skip this frame.

    - login: replace the 8 BCD IMEI bytes, keep any trailing bytes, rebuild (CRC).
    - LOC:   shift GPS time to send_epoch - orig_lag, rebuild (CRC) — preserves
      this frame's receive-minus-gps lag against the current wall clock.
    - heartbeat: pass through unchanged (keeps the connection alive).
    - everything else (CMDRESP carrying the old IMEI banner, alarms): skip.
    """
    if proto == LOGIN:
        return build_frame(LOGIN, build_login_data(imei) + data[8:], serial)
    if proto in LOC:
        if gps_epoch(data) is None:
            return frame  # undecodable date; send as captured
        tt = time.gmtime(send_epoch - orig_lag)
        head = struct.pack(">BBBBBB", tt.tm_year - 2000, tt.tm_mon, tt.tm_mday,
                           tt.tm_hour, tt.tm_min, tt.tm_sec)
        return build_frame(proto, head + data[6:], serial)
    if proto == HEARTBEAT:
        return frame
    return None


def drain(sock):
    """Read and discard anything the server has sent (never ACK)."""
    try:
        while select.select([sock], [], [], 0)[0]:
            if not sock.recv(4096):
                return
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logfile")
    ap.add_argument("--map-imei", required=True, help="15-digit IMEI to present as the device")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7711)
    ap.add_argument("--start", default=None, help="replay from this time (local 'YYYY-MM-DD HH:MM:SS[.fff]')")
    ap.add_argument("--end", default=None, help="replay up to this time")
    ap.add_argument("--speed", type=float, default=1.0, help="wall-clock speed multiplier (default 1.0)")
    ap.add_argument("--max-gap", type=float, default=None,
                    help="cap an inter-connection (offline) gap to this many seconds")
    ap.add_argument("--dry-run", action="store_true", help="decode the rewritten frames, open no sockets")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    imei = a.map_imei.strip()
    if not (imei.isdigit() and len(imei) == 15):
        raise SystemExit(f"--map-imei must be 15 digits, got {imei!r}")
    start = parse_dt(a.start) if a.start else None
    end = parse_dt(a.end) if a.end else None

    with open(a.logfile, "rb") as f:
        fmt = detect_format(f)
        frames = []
        for ts, conn_id, outgoing, frame in read_packets(f, fmt):
            if outgoing:
                continue
            if start is not None and ts < start:
                continue
            if end is not None and ts > end:
                continue
            frames.append((ts, conn_id, frame))
    if not frames:
        raise SystemExit("no incoming frames in the selected window")

    sailor_id = "G" + imei[-6:]
    print(f"# replay {len(frames)} frames as IMEI {imei} (sailor {sailor_id}) "
          f"-> {a.host}:{a.port}{'  [DRY RUN]' if a.dry_run else ''}", file=sys.stderr)
    print(f"# window {fmt_time(frames[0][0])} .. {fmt_time(frames[-1][0])} "
          f"(span {(frames[-1][0] - frames[0][0]):.0f}s){' x%.1f' % a.speed if a.speed != 1 else ''}",
          file=sys.stderr)
    if imei.startswith("999"):
        print(f"# sim device -> event {int(imei[3:5])} (digits 3-4); ensure that event is tracking",
              file=sys.stderr)
    else:
        print("# WARNING: non-999 IMEI logs in as legacy_raw — with TERIID on it will NOT publish "
              "(no track recorded). Use a 999<EE>... sim IMEI (EE = event id).", file=sys.stderr)

    play_epoch = time.time()
    log_prev = frames[0][0]
    cur_conn = None
    sock = None
    sent = skipped = conns = 0

    def sleep_until(target):
        if a.dry_run:
            return
        dt = target - time.time()
        if dt > 0:
            time.sleep(dt)

    for ts, conn_id, frame in frames:
        parsed = validate_frame(frame)
        if not parsed:
            continue
        proto, data, serial, _crc_ok = parsed
        new_conn = conn_id != cur_conn

        delta = (ts - log_prev) / a.speed
        if new_conn and a.max_gap is not None and delta > a.max_gap:
            delta = a.max_gap
        target = play_epoch + delta

        if new_conn and sock is not None:
            sock.close()
            sock = None
        sleep_until(target)
        play_epoch = target
        log_prev = ts

        if new_conn:
            cur_conn = conn_id
            conns += 1
            if not a.dry_run:
                sock = socket.create_connection((a.host, a.port), timeout=10)
            if proto != LOGIN:  # window cut mid-connection: synthesise a login first
                synth = build_frame(LOGIN, build_login_data(imei), 1)
                if a.dry_run:
                    print(f"[c{conn_id}] SYNTH-LOGIN", end="  ")
                    dump_packet(target, synth, verbose=a.verbose)
                else:
                    sock.sendall(synth)
                    drain(sock)

        orig_lag = ts - (gps_epoch(data) or ts)
        out = rewrite(proto, data, serial, frame, imei, target, orig_lag)
        if out is None:
            skipped += 1
            continue
        if a.dry_run:
            dump_packet(target, out, verbose=a.verbose, conn_id=conn_id)
        else:
            try:
                sock.sendall(out)
                drain(sock)
            except OSError as e:
                print(f"# send failed on c{conn_id}: {e}", file=sys.stderr)
                sock = None
                cur_conn = None
                continue
        sent += 1

    if sock is not None:
        sock.close()
    print(f"# done: {sent} sent, {skipped} skipped (CMDRESP/alarm), {conns} connection(s)",
          file=sys.stderr)


if __name__ == "__main__":
    main()
