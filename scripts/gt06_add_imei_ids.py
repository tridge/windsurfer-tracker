#!/usr/bin/env python3
"""One-off: retrofit IMEI-ID (0xFA) markers into archived gt06.log files.

Since b2c857a the server injects a synthetic 0xFA IMEI-ID frame per open
connection into each fresh log at rotation, making daily files
self-describing. Older archives in old_logs/ predate that: connections whose
LOGIN happened on a previous day have no in-file identity until their first
cxzt reply (or none at all if they disconnected before one).

This script rewrites such archives to look as if the marker feature had
always existed: for every connection whose first frame is NOT a login but
whose identity is discoverable later in the file (cxzt *ID:* reply, or a
late login on the same conn), it prepends a 0xFA record at the start of the
file (timestamped with that connection's first frame time). Files that
already contain any 0xFA marker are skipped, so the script is idempotent and
safe to run across a whole directory.

Default is a dry run (report only). --write rewrites atomically: the new
content goes to a temp file in the same directory, is fully re-parsed and
validated, then os.replace()d over the original with mtime preserved (the
archive mtime encodes the rotation date).

Usage:
  scripts/gt06_add_imei_ids.py old_logs/gt06.log.2026-07-*        # dry run
  scripts/gt06_add_imei_ids.py --write old_logs/gt06.log.2026-07-*
"""
import argparse
import os
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gt06_dump import (read_packets, detect_format, validate_frame,  # noqa: E402
                       identity_from_frame, fmt_time)
from protocol_GT06 import GT06_LOG_MAGIC_V2, GT06_PROTO_LOG_IMEI  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from gt06_frames import build_frame, build_login_data  # noqa: E402


def scan(path):
    """Single pass. Returns (has_marker, needs, no_identity, n_records) where
    needs = [(conn_id, first_frame_ts, imei), ...] for connections whose first
    frame is not a login but whose identity appears later in the file."""
    first_ts = {}        # conn_id -> ts of its first frame in this file
    first_is_login = {}  # conn_id -> True if that first frame was a login
    identity = {}        # conn_id -> first identity seen (login or cxzt)
    has_marker = False
    n = 0
    with open(path, "rb") as f:
        fmt = detect_format(f)
        if fmt != "v2":
            return None
        for ts, conn_id, outgoing, frame in read_packets(f, fmt=fmt):
            n += 1
            result = validate_frame(frame)
            proto = result[0] if result else None
            if proto == GT06_PROTO_LOG_IMEI:
                has_marker = True
            if conn_id not in first_ts:
                first_ts[conn_id] = ts
                first_is_login[conn_id] = (proto == 0x01 and not outgoing)
            if result and conn_id not in identity:
                imei = identity_from_frame(proto, result[1], outgoing)
                if imei:
                    identity[conn_id] = imei
    needs = [(cid, first_ts[cid], identity[cid])
             for cid in sorted(first_ts, key=lambda c: first_ts[c])
             if not first_is_login[cid] and cid in identity]
    no_identity = [cid for cid in first_ts
                   if not first_is_login[cid] and cid not in identity]
    return has_marker, needs, no_identity, n


def rewrite(path, needs, n_orig):
    """Prepend one 0xFA record per entry in `needs`. Atomic + validated."""
    injected = b""
    for conn_id, ts, imei in needs:
        frame = build_frame(GT06_PROTO_LOG_IMEI, build_login_data(imei), 0)
        injected += struct.pack("<dIH", ts, conn_id & 0x7FFFFFFF, len(frame)) + frame

    orig_bytes = path.read_bytes()
    assert orig_bytes.startswith(GT06_LOG_MAGIC_V2)
    tmp = path.with_name(path.name + ".tmp-imeiid")
    tmp.write_bytes(GT06_LOG_MAGIC_V2 + injected
                    + orig_bytes[len(GT06_LOG_MAGIC_V2):])

    # Validate the rewrite before replacing the archive: full re-parse, record
    # count, and the injected markers decode to the expected identities.
    with open(tmp, "rb") as f:
        fmt = detect_format(f)
        assert fmt == "v2", "temp file lost its magic"
        recs = 0
        for i, (ts, conn_id, outgoing, frame) in enumerate(read_packets(f, fmt=fmt)):
            if i < len(needs):
                result = validate_frame(frame)
                assert result and result[0] == GT06_PROTO_LOG_IMEI, \
                    f"record {i} is not a 0xFA marker"
                exp_cid, exp_ts, exp_imei = needs[i]
                assert conn_id == exp_cid and not outgoing
                assert identity_from_frame(result[0], result[1], outgoing) == exp_imei
            recs += 1
        assert recs == n_orig + len(needs), \
            f"record count mismatch: {recs} != {n_orig}+{len(needs)}"

    st = path.stat()
    os.replace(tmp, path)
    os.utime(path, (st.st_atime, st.st_mtime))  # archive mtime = rotation date


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logfiles", nargs="+", help="archived v2 gt06.log files")
    ap.add_argument("--write", action="store_true",
                    help="rewrite files in place (default: dry-run report)")
    args = ap.parse_args()

    for name in args.logfiles:
        path = Path(name)
        if not path.exists():
            print(f"{path}: not found", file=sys.stderr)
            continue
        res = scan(path)
        if res is None:
            print(f"{path.name}: v1 log (no conn ids) — skipped")
            continue
        has_marker, needs, no_identity, n = res
        if has_marker:
            print(f"{path.name}: already has IMEI-ID markers — skipped")
            continue
        extra = f", {len(no_identity)} conn(s) with no identity in file" \
            if no_identity else ""
        if not needs:
            print(f"{path.name}: nothing to add ({n} records{extra})")
            continue
        if args.write:
            rewrite(path, needs, n)
            print(f"{path.name}: injected {len(needs)} IMEI-ID marker(s){extra}")
        else:
            print(f"{path.name}: would inject {len(needs)} marker(s){extra}:")
            for cid, ts, imei in needs:
                print(f"    conn {cid:<6d} {fmt_time(ts)}  IMEI={imei}")


if __name__ == "__main__":
    main()
