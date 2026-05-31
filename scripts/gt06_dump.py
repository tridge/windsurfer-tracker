#!/usr/bin/env python3
"""Dump GT06 binary packet log.

Reads the binary log file written by the GT06 listener and prints decoded
packets. Auto-detects format:

  v1 (legacy): 10-byte record header (8B ts + 2B length) + frame
  v2:          8-byte file magic "GT06LOG2", then 14-byte record header
               (8B ts + 4B conn_id-with-dir-bit + 2B length) + frame

Pass --imei to filter to a single device's stream (v2 logs only — v1 has no
stream IDs to filter on).
"""

import sys
import struct
import time
import argparse
from pathlib import Path
from datetime import datetime, timezone

# Locate protocol_GT06.py — try the project layout (../server) first, then
# fall back to a flat layout (../, e.g. the deployed wstracker tree where the
# server modules sit next to the data files).
_here = Path(__file__).resolve().parent
for _candidate in (_here.parent / "server", _here.parent, _here):
    if (_candidate / "protocol_GT06.py").exists():
        sys.path.insert(0, str(_candidate))
        break
from protocol_GT06 import (
    gt06_parse_login,
    gt06_parse_location,
    gt06_parse_heartbeat,
    gt06_crc_itu,
    GT06_LOG_MAGIC_V2,
    GT06_LOG_DIR_OUT,
)

V1_HEADER_SIZE = 10  # 8 (float64) + 2 (uint16)
V2_HEADER_SIZE = 14  # 8 (float64) + 4 (uint32 conn_id) + 2 (uint16 length)
HEADER_SIZE = V1_HEADER_SIZE  # retained for backward compat with anything importing this

_GT06_BATTERY_MAP = {0: 0, 1: 5, 2: 15, 3: 30, 4: 50, 5: 75, 6: 100}

ALARM_TYPES = {0: "Normal", 1: "SOS", 2: "Power Cut", 3: "Shock", 4: "Fence In", 5: "Fence Out"}
TERMINAL_ALARM = {0: "Normal", 1: "Shock", 2: "Power Cut", 3: "Low Battery", 4: "SOS"}


def detect_format(f):
    """Return "v2" if file starts with magic, "v1" otherwise. Leaves file pointer
    positioned past the magic (v2) or at start (v1)."""
    head = f.read(len(GT06_LOG_MAGIC_V2))
    if head == GT06_LOG_MAGIC_V2:
        return "v2"
    f.seek(0)
    return "v1"


def read_packets(f, fmt="v1"):
    """Yield (timestamp, conn_id, outgoing, frame) tuples.

    For v1 logs, conn_id is always 0 and outgoing is always False (unknown).
    """
    if fmt == "v2":
        while True:
            header = f.read(V2_HEADER_SIZE)
            if len(header) < V2_HEADER_SIZE:
                return
            ts, raw_conn, frame_len = struct.unpack("<dIH", header)
            outgoing = bool(raw_conn & GT06_LOG_DIR_OUT)
            conn_id = raw_conn & 0x7FFFFFFF
            frame = f.read(frame_len)
            if len(frame) < frame_len:
                return
            yield ts, conn_id, outgoing, frame
    else:
        while True:
            header = f.read(V1_HEADER_SIZE)
            if len(header) < V1_HEADER_SIZE:
                return
            ts, frame_len = struct.unpack("<dH", header)
            frame = f.read(frame_len)
            if len(frame) < frame_len:
                return
            yield ts, 0, False, frame


def fmt_time(ts):
    """Format unix timestamp as local datetime string."""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S.") + f"{ts % 1:.3f}"[2:]


def gps_time_str(data):
    """Embedded GPS fix time (first 6 bytes = UTC Y/M/D h:m:s), shown in local
    time as HH:MM:SS so it lines up with the packet receive time. Returns '?'
    if the bytes aren't a valid date."""
    if len(data) < 6:
        return "?"
    try:
        gps_dt = datetime(2000 + data[0], data[1], data[2],
                          data[3], data[4], data[5], tzinfo=timezone.utc)
    except ValueError:
        return "?"
    return gps_dt.astimezone().strftime("%H:%M:%S")


def validate_frame(frame):
    """Validate frame structure. Returns (protocol, data, serial, crc_ok) or None."""
    if len(frame) < 10 or frame[0:2] != b"\x78\x78":
        return None
    length = frame[2]
    protocol = frame[3]
    crc_offset = 3 + length - 2
    serial_offset = 3 + length - 4
    if crc_offset + 2 > len(frame) or serial_offset < 4:
        return None
    crc_received = struct.unpack(">H", frame[crc_offset:crc_offset + 2])[0]
    serial = struct.unpack(">H", frame[serial_offset:serial_offset + 2])[0]
    crc_calc = gt06_crc_itu(frame[2:crc_offset])
    data = frame[4:serial_offset]
    return protocol, data, serial, crc_received == crc_calc


def decode_lbs(data):
    """Decode LBS (cell tower) data. Returns dict with MCC, MNC, LAC, Cell ID."""
    result = {}
    if len(data) >= 2:
        result["mcc"] = struct.unpack(">H", data[0:2])[0]
    if len(data) >= 3:
        result["mnc"] = data[2]
    if len(data) >= 5:
        result["lac"] = struct.unpack(">H", data[3:5])[0]
    if len(data) >= 8:
        result["cell_id"] = (data[5] << 16) | (data[6] << 8) | data[7]
    elif len(data) >= 7:
        result["cell_id"] = (data[5] << 8) | data[6]
    return result


def fmt_lbs(lbs):
    """Format LBS dict for display."""
    parts = []
    if "mcc" in lbs:
        parts.append(f"MCC={lbs['mcc']}")
    if "mnc" in lbs:
        parts.append(f"MNC={lbs['mnc']}")
    if "lac" in lbs:
        parts.append(f"LAC=0x{lbs['lac']:04X}")
    if "cell_id" in lbs:
        parts.append(f"CellID=0x{lbs['cell_id']:X}")
    return "  ".join(parts)


def decode_terminal_info(info_byte):
    """Decode terminal information byte into dict."""
    return {
        "oil_connected": bool(info_byte & 0x80),
        "gps_tracking": bool(info_byte & 0x40),
        "alarm": TERMINAL_ALARM.get((info_byte >> 3) & 0x07, f"Unknown({(info_byte >> 3) & 0x07})"),
        "alarm_bits": (info_byte >> 3) & 0x07,
        "charging": bool(info_byte & 0x04),
        "acc": bool(info_byte & 0x02),
        "activated": bool(info_byte & 0x01),
    }


def dump_packet(ts, frame, verbose=False, conn_id=0, outgoing=False):
    """Format and print one packet."""
    result = validate_frame(frame)
    # Prefix shows direction + conn_id when available (v2 format)
    if conn_id:
        prefix = f"c{conn_id:<4d} {'OUT' if outgoing else 'IN '}  "
    else:
        prefix = ""
    if result is None:
        print(f"{fmt_time(ts)}  {prefix}???     Bad frame: {frame.hex()}")
        return

    protocol, data, serial, crc_ok = result
    crc_tag = "" if crc_ok else " [CRC BAD]"
    ts_str = fmt_time(ts) + ("  " + prefix if prefix else "")

    if protocol == 0x01:
        # Login
        imei = gt06_parse_login(data)
        print(f"{ts_str}  LOGIN   IMEI={imei}{crc_tag}")
        if verbose:
            print(f"           serial={serial}  raw={data.hex()}")

    elif protocol in (0x12, 0x22):
        # Location
        loc = gt06_parse_location(data)
        proto_name = "LOC" if protocol == 0x12 else "LOC22"
        if loc is None:
            print(f"{ts_str}  {proto_name:<7s} [parse error]{crc_tag}")
            return
        spd_kn = loc["speed_kmh"] / 1.852
        valid = "" if loc["gps_valid"] else " [NO FIX]"
        print(f"{ts_str}  {proto_name:<7s} lat={loc['lat']:.8f} lon={loc['lon']:.8f} "
              f"spd={spd_kn:.1f}kn hdg={loc['heading']} sats={loc['satellites']} "
              f"gps={gps_time_str(data)}{valid}{crc_tag}")
        if verbose:
            print(f"           speed_kmh={loc['speed_kmh']} gps_valid={loc['gps_valid']}")
            if len(data) >= 18:
                course_status = struct.unpack(">H", data[16:18])[0]
                print(f"           course_status=0x{course_status:04X}  "
                      f"realtime_gps={bool(course_status & (1 << 13))}  "
                      f"gps_positioned={bool(course_status & (1 << 12))}  "
                      f"west={bool(course_status & (1 << 11))}  "
                      f"south={not bool(course_status & (1 << 10))}")
            gps_dt = datetime(2000 + data[0], data[1], data[2],
                              data[3], data[4], data[5], tzinfo=timezone.utc)
            print(f"           gps_time={gps_dt.isoformat()}")
            if len(data) > 18:
                lbs = decode_lbs(data[18:])
                if lbs:
                    print(f"           {fmt_lbs(lbs)}")
            print(f"           serial={serial}")

    elif protocol == 0x13:
        # Heartbeat
        hb = gt06_parse_heartbeat(data)
        bat_str = f"{hb.get('battery', '?')}%"
        sig_str = f"{hb.get('signal', '?')}/4"
        chrg = "+" if hb.get("charging") else ""
        print(f"{ts_str}  HB      bat={bat_str}{chrg} sig={sig_str}{crc_tag}")
        if verbose:
            if len(data) >= 1:
                info = data[0]
                ti = decode_terminal_info(info)
                print(f"           terminal_info=0x{info:02X}  alarm={ti['alarm']}  "
                      f"oil={ti['oil_connected']}  gps_track={ti['gps_tracking']}  "
                      f"charging={ti['charging']}  acc={ti['acc']}  activated={ti['activated']}")
            if len(data) >= 2:
                print(f"           voltage_level={data[1]}  battery={_GT06_BATTERY_MAP.get(data[1], '?')}%")
            if len(data) >= 3:
                print(f"           gsm_signal={data[2]}")
            if len(data) >= 5:
                alarm_lang = struct.unpack(">H", data[3:5])[0]
                alarm_type = (alarm_lang >> 8) & 0xFF
                lang = alarm_lang & 0xFF
                print(f"           alarm_lang=0x{alarm_lang:04X}  "
                      f"alarm_type={ALARM_TYPES.get(alarm_type, f'Unknown({alarm_type})')}  "
                      f"language={lang}")
            print(f"           serial={serial}")

    elif protocol in (0x16, 0x23):
        # Alarm — location data + LBS + terminal info + voltage + signal + alarm/lang
        loc = gt06_parse_location(data)
        if loc is None:
            print(f"{ts_str}  ALARM   [parse error]{crc_tag}")
            return
        spd_kn = loc["speed_kmh"] / 1.852
        # Try to extract SOS info from extended data after 18-byte location block
        alarm_label = ""
        if len(data) > 18:
            extra = data[18:]
            # Skip LBS: first byte is LBS length, then LBS data
            if len(extra) >= 1:
                lbs_len = extra[0]
                after_lbs = extra[1 + lbs_len:] if len(extra) > 1 + lbs_len else b""
                if len(after_lbs) >= 1:
                    ti = decode_terminal_info(after_lbs[0])
                    alarm_label = f" {ti['alarm']}"
                    if ti["alarm_bits"] == 4:
                        alarm_label = " SOS"

        if not alarm_label:
            alarm_label = " ALARM"

        valid = "" if loc["gps_valid"] else " [NO FIX]"
        print(f"{ts_str}  ALARM   lat={loc['lat']:.8f} lon={loc['lon']:.8f} "
              f"spd={spd_kn:.1f}kn hdg={loc['heading']} gps={gps_time_str(data)}"
              f"{alarm_label}{valid}{crc_tag}")
        if verbose:
            print(f"           sats={loc['satellites']}  speed_kmh={loc['speed_kmh']}  "
                  f"gps_valid={loc['gps_valid']}")
            if len(data) >= 18:
                course_status = struct.unpack(">H", data[16:18])[0]
                print(f"           course_status=0x{course_status:04X}  "
                      f"realtime_gps={bool(course_status & (1 << 13))}  "
                      f"gps_positioned={bool(course_status & (1 << 12))}  "
                      f"west={bool(course_status & (1 << 11))}  "
                      f"south={not bool(course_status & (1 << 10))}")
                gps_dt = datetime(2000 + data[0], data[1], data[2],
                                  data[3], data[4], data[5], tzinfo=timezone.utc)
                print(f"           gps_time={gps_dt.isoformat()}")
            if len(data) > 18:
                extra = data[18:]
                if len(extra) >= 1:
                    lbs_len = extra[0]
                    lbs_data = extra[1:1 + lbs_len] if len(extra) > 1 else b""
                    lbs = decode_lbs(lbs_data)
                    if lbs:
                        print(f"           {fmt_lbs(lbs)}")
                    else:
                        print(f"           lbs_len={lbs_len}  lbs={lbs_data.hex()}")
                    after_lbs = extra[1 + lbs_len:] if len(extra) > 1 + lbs_len else b""
                    if len(after_lbs) >= 1:
                        ti = decode_terminal_info(after_lbs[0])
                        print(f"           terminal_info=0x{after_lbs[0]:02X}  alarm={ti['alarm']}  "
                              f"oil={ti['oil_connected']}  gps_track={ti['gps_tracking']}  "
                              f"charging={ti['charging']}  acc={ti['acc']}  activated={ti['activated']}")
                    if len(after_lbs) >= 2:
                        print(f"           voltage_level={after_lbs[1]}  "
                              f"battery={_GT06_BATTERY_MAP.get(after_lbs[1], '?')}%")
                    if len(after_lbs) >= 3:
                        print(f"           gsm_signal={after_lbs[2]}")
                    if len(after_lbs) >= 5:
                        alarm_lang = struct.unpack(">H", after_lbs[3:5])[0]
                        alarm_type = (alarm_lang >> 8) & 0xFF
                        lang = alarm_lang & 0xFF
                        print(f"           alarm_lang=0x{alarm_lang:04X}  "
                              f"alarm_type={ALARM_TYPES.get(alarm_type, f'Unknown({alarm_type})')}  "
                              f"language={lang}")
            print(f"           serial={serial}")

    elif protocol == 0x15:
        # Server command response — content_len(1) + server_flag(4) + ASCII text
        text = ""
        if len(data) >= 5:
            text = data[5:].decode("ascii", errors="replace")
        print(f"{ts_str}  CMDRESP {text!r}{crc_tag}")
        if verbose:
            print(f"           raw={data.hex()}  serial={serial}")

    else:
        print(f"{ts_str}  0x{protocol:02X}    len={len(data)} data={data.hex()}{crc_tag}")
        if verbose:
            print(f"           serial={serial}")


def _imei_matches(target, imei):
    """Match user-supplied IMEI string against a parsed IMEI.

    Accepts full IMEI (e.g. 866557081304307), trailing digits (304307),
    or "G304307"-style sailor IDs (strip non-digit prefix).
    """
    if not target or not imei:
        return False
    digits = ''.join(c for c in target if c.isdigit())
    if not digits:
        return False
    return imei.endswith(digits) or imei == digits


def main():
    parser = argparse.ArgumentParser(description="Dump GT06 binary packet log")
    parser.add_argument("logfile", nargs="?", default="gt06.log",
                        help="Path to GT06 binary log file (default: gt06.log)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Show all decoded fields")
    parser.add_argument("-f", "--follow", action="store_true",
                        help="Tail mode — keep reading as new packets arrive")
    parser.add_argument("--imei", default=None,
                        help="Filter to one device's stream(s) (v2 logs). "
                             "Match by full IMEI, trailing digits, or G-prefixed sailor ID.")
    parser.add_argument("--list-streams", action="store_true",
                        help="List all connection streams (conn_id → IMEI) and exit")
    args = parser.parse_args()

    logpath = Path(args.logfile)
    if not logpath.exists():
        print(f"Error: {logpath} not found", file=sys.stderr)
        sys.exit(1)

    # First pass for v2 logs when --imei or --list-streams is requested: build
    # conn_id → IMEI map by scanning LOGIN packets.
    matching_conn_ids = None
    conn_to_imei = {}
    if args.imei or args.list_streams:
        with open(logpath, "rb") as f:
            fmt = detect_format(f)
            if fmt == "v1":
                print("Error: --imei / --list-streams require a v2 log "
                      "(no per-packet stream IDs in v1).", file=sys.stderr)
                sys.exit(2)
            for ts, conn_id, outgoing, frame in read_packets(f, fmt=fmt):
                if outgoing or not conn_id:
                    continue
                result = validate_frame(frame)
                if result is None:
                    continue
                protocol, data, _serial, _crc_ok = result
                if protocol == 0x01:
                    imei = gt06_parse_login(data)
                    if imei and conn_id not in conn_to_imei:
                        conn_to_imei[conn_id] = imei
        if args.list_streams:
            for cid in sorted(conn_to_imei):
                print(f"conn_id={cid}  IMEI={conn_to_imei[cid]}")
            return
        matching_conn_ids = {cid for cid, imei in conn_to_imei.items()
                              if _imei_matches(args.imei, imei)}
        if not matching_conn_ids:
            print(f"No streams matched IMEI {args.imei!r}", file=sys.stderr)
            sys.exit(0)

    def _emit(f, fmt):
        for ts, conn_id, outgoing, frame in read_packets(f, fmt=fmt):
            if matching_conn_ids is not None and conn_id not in matching_conn_ids:
                continue
            dump_packet(ts, frame, verbose=args.verbose,
                        conn_id=conn_id, outgoing=outgoing)

    try:
        with open(logpath, "rb") as f:
            fmt = detect_format(f)
            if args.follow:
                _emit(f, fmt)
                while True:
                    _emit(f, fmt)
                    time.sleep(0.5)
            else:
                _emit(f, fmt)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
