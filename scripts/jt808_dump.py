#!/usr/bin/env python3
"""Dump JT808 binary packet log.

Reads the binary log file written by the JT808 listener (each record is a
10-byte header followed by the raw JT808 frame) and prints decoded packets.
"""

import sys
import struct
import time
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Add server dir to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from protocol_JT808 import (
    jt808_unescape,
    jt808_checksum,
    jt808_parse_header,
    phone_bcd_to_imei,
    parse_location,
)

HEADER_SIZE = 10  # 8 (float64) + 2 (uint16)

# Message ID names
MSG_NAMES = {
    # Device -> Server
    0x0001: "TermGeneralResp",
    0x0002: "Heartbeat",
    0x0100: "Registration",
    0x0102: "Authentication",
    0x0104: "QueryParamResp",
    0x0107: "TermAttributes",
    0x0109: "TermUpgrade",
    0x0112: "TermTransfer",
    0x0200: "Location",
    0x0704: "BatchLocation",
    0x1007: "Vendor1007",
    0x1107: "Vendor1107",
    # Server -> Device
    0x8001: "PlatGeneralResp",
    0x8100: "RegistrationResp",
    0x8103: "SetParameters",
    0x8104: "QueryParameters",
    0x8202: "TrackingControl",
    0x8203: "AlarmConfirm",
}

ALARM_BITS = {
    0: "SOS", 1: "Overspeed", 2: "Fatigue", 3: "Dangerous",
    4: "GNSS Fault", 5: "GNSS Antenna Cut", 6: "GNSS Short",
    7: "Power Low", 8: "Power Off", 18: "Day Overspeed",
    19: "Day Fatigue",
}


def read_packets(f):
    """Yield (timestamp, frame) tuples from log file."""
    while True:
        header = f.read(HEADER_SIZE)
        if len(header) < HEADER_SIZE:
            return
        ts, frame_len = struct.unpack("<dH", header)
        frame = f.read(frame_len)
        if len(frame) < frame_len:
            return
        yield ts, frame


def fmt_time(ts):
    """Format unix timestamp as local datetime string."""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S.") + f"{ts % 1:.3f}"[2:]


def fmt_gps_time(gps_ts):
    """Format GPS timestamp."""
    return datetime.fromtimestamp(gps_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def decode_frame(frame_bytes):
    """Decode a JT808 frame. Returns (msg_id, phone_bcd, serial, body, checksum_ok) or None."""
    # Strip 0x7e delimiters
    if len(frame_bytes) < 2:
        return None
    raw = frame_bytes
    if raw[0:1] == b'\x7e':
        raw = raw[1:]
    if raw and raw[-1:] == b'\x7e':
        raw = raw[:-1]
    if not raw:
        return None

    # Unescape
    data = jt808_unescape(raw)
    if len(data) < 13:  # 12-byte header + 1-byte checksum
        return None

    # Verify checksum
    payload = data[:-1]
    cs_ok = jt808_checksum(payload) == data[-1]

    # Parse header
    parsed = jt808_parse_header(payload[:12])
    if parsed is None:
        return None
    msg_id, attributes, phone_bcd, serial, body_offset = parsed
    body = payload[body_offset:]
    return msg_id, phone_bcd, serial, body, cs_ok


def fmt_alarm_flags(alarm):
    """Format alarm flags into human-readable string."""
    if alarm == 0:
        return ""
    bits = []
    for bit, name in ALARM_BITS.items():
        if alarm & (1 << bit):
            bits.append(name)
    if not bits:
        bits.append(f"0x{alarm:08X}")
    return " ALARM[" + ",".join(bits) + "]"


def fmt_status_flags(status):
    """Format status flags."""
    parts = []
    if status & 0x01:
        parts.append("ACC-ON")
    if status & 0x02:
        parts.append("GPS-Valid")
    else:
        parts.append("GPS-Invalid")
    if status & 0x04:
        parts.append("South")
    if status & 0x08:
        parts.append("West")
    return ",".join(parts)


def decode_tlvs(body, offset):
    """Decode TLV additional info items from location body."""
    tlvs = {}
    while offset < len(body):
        if offset + 2 > len(body):
            break
        tag = body[offset]
        length = body[offset + 1]
        offset += 2
        if offset + length > len(body):
            break
        val = body[offset:offset + length]
        tlvs[tag] = val
        offset += length
    return tlvs


def dump_location(body, prefix="", verbose=False):
    """Decode and print a single location body."""
    loc = parse_location(body)
    if loc is None:
        return f"{prefix}[parse error] raw={body.hex()}"

    valid = "" if loc.get("gps_valid", True) else " [NO FIX]"
    alarm = fmt_alarm_flags(loc.get("alarm_flags", 0))

    line = (f"{prefix}lat={loc['lat']:.6f} lon={loc['lon']:.6f} "
            f"spd={loc['speed_knots']:.1f}kn hdg={loc['heading']} "
            f"ts={fmt_gps_time(loc['ts'])}{valid}{alarm}")

    if verbose:
        status = loc.get("status", 0)
        alarm_flags = loc.get("alarm_flags", 0)
        extra = f"           status=0x{status:08X}({fmt_status_flags(status)}) alarm=0x{alarm_flags:08X}"
        if loc.get("battery") is not None:
            extra += f" bat={loc['battery']}%"
        if loc.get("signal") is not None:
            extra += f" sig={loc['signal']}"

        # Decode TLVs from raw body (after 28-byte base)
        if len(body) > 28:
            tlvs = decode_tlvs(body, 28)
            tlv_parts = []
            for tag, val in tlvs.items():
                if tag == 0x01:  # Mileage
                    tlv_parts.append(f"mileage={struct.unpack('>I', val)[0]/10:.1f}km")
                elif tag == 0x02:  # Fuel
                    tlv_parts.append(f"fuel={struct.unpack('>H', val)[0]/10:.1f}L")
                elif tag == 0x03:  # Speed from recorder
                    tlv_parts.append(f"rec_speed={struct.unpack('>H', val)[0]/10:.1f}km/h")
                elif tag == 0x30:  # Signal strength
                    tlv_parts.append(f"signal={val[0]}")
                elif tag == 0x31:  # Satellite count
                    tlv_parts.append(f"sats={val[0]}")
                elif tag in (0xE4, 0xE5, 0xE6, 0xE7, 0xEE):
                    tlv_parts.append(f"0x{tag:02X}={val.hex()}")
                else:
                    tlv_parts.append(f"0x{tag:02X}={val.hex()}")
            if tlv_parts:
                extra += "  TLV: " + " ".join(tlv_parts)
        line += "\n" + extra
    return line


def dump_packet(ts, frame, verbose=False):
    """Format and print one packet."""
    result = decode_frame(frame)
    if result is None:
        print(f"{fmt_time(ts)}  ???        Bad frame: {frame.hex()}")
        return

    msg_id, phone_bcd, serial, body, cs_ok = result
    cs_tag = "" if cs_ok else " [CS BAD]"
    ts_str = fmt_time(ts)
    imei = phone_bcd_to_imei(phone_bcd)
    name = MSG_NAMES.get(msg_id, f"0x{msg_id:04X}")

    if msg_id == 0x0100:
        # Registration
        print(f"{ts_str}  {name:<18s} IMEI={imei}{cs_tag}")
        if verbose and len(body) >= 8:
            province = struct.unpack(">H", body[0:2])[0]
            city = struct.unpack(">H", body[2:4])[0]
            mfr = body[4:9].decode("ascii", errors="replace").rstrip('\x00')
            model = body[9:29].decode("ascii", errors="replace").rstrip('\x00') if len(body) >= 29 else "?"
            dev_id = body[29:36].decode("ascii", errors="replace").rstrip('\x00') if len(body) >= 36 else "?"
            print(f"           province={province} city={city} mfr={mfr!r} model={model!r} devid={dev_id!r}")
            print(f"           serial={serial}")

    elif msg_id == 0x0102:
        # Authentication
        auth_code = body.decode("ascii", errors="replace") if body else ""
        print(f"{ts_str}  {name:<18s} IMEI={imei} auth={auth_code!r}{cs_tag}")
        if verbose:
            print(f"           serial={serial}")

    elif msg_id == 0x0002:
        # Heartbeat
        print(f"{ts_str}  {name:<18s} IMEI={imei}{cs_tag}")
        if verbose:
            print(f"           serial={serial}")

    elif msg_id == 0x0200:
        # Single location report
        loc_str = dump_location(body, prefix="", verbose=verbose)
        print(f"{ts_str}  {name:<18s} {loc_str}{cs_tag}")
        if verbose:
            print(f"           serial={serial}")

    elif msg_id == 0x0704:
        # Batch location upload
        if len(body) < 3:
            print(f"{ts_str}  {name:<18s} [too short]{cs_tag}")
            return
        count = struct.unpack(">H", body[0:2])[0]
        loc_type = body[2]
        type_str = "normal" if loc_type == 0 else "blind"
        print(f"{ts_str}  {name:<18s} count={count} type={type_str}{cs_tag}")
        # Parse individual locations
        offset = 3
        for i in range(count):
            if offset + 2 > len(body):
                print(f"           [{i}] [truncated at offset {offset}]")
                break
            item_len = struct.unpack(">H", body[offset:offset + 2])[0]
            offset += 2
            if offset + item_len > len(body):
                print(f"           [{i}] [truncated: need {item_len} bytes, have {len(body) - offset}]")
                break
            loc_body = body[offset:offset + item_len]
            offset += item_len
            loc_str = dump_location(loc_body, prefix="", verbose=verbose)
            print(f"           [{i}] {loc_str}")

    elif msg_id == 0x0001:
        # Terminal general response
        if len(body) >= 5:
            resp_serial = struct.unpack(">H", body[0:2])[0]
            resp_id = struct.unpack(">H", body[2:4])[0]
            result_code = body[4]
            result_str = {0: "OK", 1: "Fail", 2: "Bad msg", 3: "Unsupported"}.get(result_code, f"?({result_code})")
            resp_name = MSG_NAMES.get(resp_id, f"0x{resp_id:04X}")
            print(f"{ts_str}  {name:<18s} ack {resp_name} serial={resp_serial} result={result_str}{cs_tag}")
        else:
            print(f"{ts_str}  {name:<18s} body={body.hex()}{cs_tag}")

    elif msg_id == 0x8001:
        # Platform general response
        if len(body) >= 5:
            resp_serial = struct.unpack(">H", body[0:2])[0]
            resp_id = struct.unpack(">H", body[2:4])[0]
            result_code = body[4]
            result_str = {0: "OK", 1: "Fail", 2: "Bad msg", 3: "Unsupported", 4: "Alarm ACK"}.get(result_code, f"?({result_code})")
            resp_name = MSG_NAMES.get(resp_id, f"0x{resp_id:04X}")
            print(f"{ts_str}  {name:<18s} ack {resp_name} serial={resp_serial} result={result_str}{cs_tag}")
        else:
            print(f"{ts_str}  {name:<18s} body={body.hex()}{cs_tag}")

    elif msg_id == 0x8100:
        # Registration response
        if len(body) >= 3:
            resp_serial = struct.unpack(">H", body[0:2])[0]
            result_code = body[2]
            result_str = {0: "OK", 1: "Vehicle exists", 2: "No vehicle", 3: "Terminal exists", 4: "No terminal"}.get(result_code, f"?({result_code})")
            auth_code = body[3:].decode("ascii", errors="replace") if len(body) > 3 else ""
            print(f"{ts_str}  {name:<18s} serial={resp_serial} result={result_str} auth={auth_code!r}{cs_tag}")
        else:
            print(f"{ts_str}  {name:<18s} body={body.hex()}{cs_tag}")

    elif msg_id == 0x8202:
        # Tracking control
        if len(body) >= 6:
            interval = struct.unpack(">H", body[0:2])[0]
            validity = struct.unpack(">I", body[2:6])[0]
            print(f"{ts_str}  {name:<18s} interval={interval}s validity={validity}s{cs_tag}")
        else:
            print(f"{ts_str}  {name:<18s} body={body.hex()}{cs_tag}")

    elif msg_id == 0x8103:
        # Set terminal parameters
        if len(body) >= 1:
            param_count = body[0]
            print(f"{ts_str}  {name:<18s} params={param_count}{cs_tag}")
            offset = 1
            for i in range(param_count):
                if offset + 5 > len(body):
                    break
                param_id = struct.unpack(">I", body[offset:offset + 4])[0]
                param_len = body[offset + 4]
                offset += 5
                if offset + param_len > len(body):
                    break
                param_val = body[offset:offset + param_len]
                offset += param_len
                if param_len == 4:
                    val = struct.unpack(">I", param_val)[0]
                    print(f"           0x{param_id:04X} = {val} (DWORD)")
                elif param_len == 2:
                    val = struct.unpack(">H", param_val)[0]
                    print(f"           0x{param_id:04X} = {val} (WORD)")
                elif param_len == 1:
                    print(f"           0x{param_id:04X} = {param_val[0]} (BYTE)")
                else:
                    print(f"           0x{param_id:04X} = {param_val.hex()} (len={param_len})")
        else:
            print(f"{ts_str}  {name:<18s} body={body.hex()}{cs_tag}")

    elif msg_id == 0x0104:
        # Query parameter response
        if len(body) >= 3:
            resp_serial = struct.unpack(">H", body[0:2])[0]
            param_count = body[2]
            print(f"{ts_str}  {name:<18s} resp_serial={resp_serial} params={param_count}{cs_tag}")
            offset = 3
            for i in range(param_count):
                if offset + 5 > len(body):
                    break
                param_id = struct.unpack(">I", body[offset:offset + 4])[0]
                param_len = body[offset + 4]
                offset += 5
                if offset + param_len > len(body):
                    break
                param_val = body[offset:offset + param_len]
                offset += param_len
                if param_len == 4:
                    val = struct.unpack(">I", param_val)[0]
                    print(f"           0x{param_id:04X} = {val} (DWORD)")
                elif param_len == 2:
                    val = struct.unpack(">H", param_val)[0]
                    print(f"           0x{param_id:04X} = {val} (WORD)")
                elif param_len == 1:
                    print(f"           0x{param_id:04X} = {param_val[0]} (BYTE)")
                else:
                    print(f"           0x{param_id:04X} = {param_val.hex()} (len={param_len})")
        else:
            print(f"{ts_str}  {name:<18s} body={body.hex()}{cs_tag}")

    elif msg_id == 0x0107:
        # Terminal attributes
        print(f"{ts_str}  {name:<18s} len={len(body)}{cs_tag}")
        if verbose:
            print(f"           raw={body.hex()}")
            print(f"           serial={serial}")

    else:
        print(f"{ts_str}  {name:<18s} len={len(body)} body={body.hex()}{cs_tag}")
        if verbose:
            print(f"           serial={serial}")


def main():
    parser = argparse.ArgumentParser(description="Dump JT808 binary packet log")
    parser.add_argument("logfile", nargs="?", default="jt808.log",
                        help="Path to JT808 binary log file (default: jt808.log)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Show all decoded fields")
    parser.add_argument("-f", "--follow", action="store_true",
                        help="Tail mode — keep reading as new packets arrive")
    parser.add_argument("-n", "--last", type=int, default=0,
                        help="Only show last N packets")
    args = parser.parse_args()

    logpath = Path(args.logfile)
    if not logpath.exists():
        print(f"Error: {logpath} not found", file=sys.stderr)
        sys.exit(1)

    try:
        with open(logpath, "rb") as f:
            if args.last > 0:
                # Read all, keep last N
                packets = []
                for ts, frame in read_packets(f):
                    packets.append((ts, frame))
                for ts, frame in packets[-args.last:]:
                    dump_packet(ts, frame, verbose=args.verbose)
            elif args.follow:
                for ts, frame in read_packets(f):
                    dump_packet(ts, frame, verbose=args.verbose)
                while True:
                    for ts, frame in read_packets(f):
                        dump_packet(ts, frame, verbose=args.verbose)
                    time.sleep(0.5)
            else:
                for ts, frame in read_packets(f):
                    dump_packet(ts, frame, verbose=args.verbose)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
