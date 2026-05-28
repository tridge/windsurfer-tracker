"""GT06 binary frame primitives — shared by test/conftest.py:GT06Client and
server/gt06_device_sim.py:GT06DeviceSim.

Only the device-side helpers live here (build login/LOC/HBT/alarm/ACK payloads,
parse incoming 0x80 server commands). The listener's own builders stay in
server/protocol_GT06.py to avoid disturbing that module.
"""

import struct


def crc_itu(data):
    """CRC-ITU (CRC-16/X.25): polynomial 0x8408 reflected, init 0xFFFF.

    Matches server/protocol_GT06.py:gt06_crc_itu — kept identical so tests
    and the simulator produce frames the real listener accepts byte-for-byte.
    """
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0x8408
            else:
                crc >>= 1
    return crc ^ 0xFFFF


def build_frame(protocol, data, serial):
    """Build a complete GT06 frame: 78 78 [len] [protocol] [data] [serial] [crc] 0d 0a."""
    length = 1 + len(data) + 2 + 2
    payload = struct.pack(">B", length) + struct.pack(">B", protocol) + data
    payload += struct.pack(">H", serial)
    crc = crc_itu(payload)
    return b"\x78\x78" + payload + struct.pack(">H", crc) + b"\x0d\x0a"


def build_login_data(imei):
    """Pack a 15-digit IMEI as 8 BCD bytes (left-padded with one zero nibble)."""
    imei_padded = imei.rjust(16, "0")
    return bytes.fromhex(imei_padded)


def build_location_data(lat=-35.2999, lon=149.1003, speed_kmh=0,
                        heading=180, satellites=8, gps_valid=True,
                        year=26, month=2, day=21, hour=12, minute=0, second=0,
                        course_status_zero=False):
    """Build the 18-byte location data block carried by protocol 0x12 / 0x22.

    course_status_zero: V667 firmware quirk — emit course_status = 0x0000
    even on a valid fix (no GPS-valid bit, no N/S/E/W bits, no heading).
    The real server soft-accepts these when the rest of the frame is sane.
    """
    data = struct.pack(">BBBBBB", year, month, day, hour, minute, second)
    gps_info = (satellites & 0x0F) | 0xF0  # high nibble = GPS data length
    data += struct.pack(">B", gps_info)

    lat_raw = int(abs(lat) * 1_800_000)
    lon_raw = int(abs(lon) * 1_800_000)
    data += struct.pack(">II", lat_raw, lon_raw)

    data += struct.pack(">B", speed_kmh)

    if course_status_zero:
        course_status = 0x0000
    else:
        course_status = heading & 0x03FF
        if gps_valid:
            course_status |= (1 << 12)
        if lat >= 0:
            course_status |= (1 << 10)  # North
        if lon < 0:
            course_status |= (1 << 11)  # West
    data += struct.pack(">H", course_status)

    return data


def build_heartbeat_data(battery_level=6, signal=4, charging=False):
    """Build the 3-byte heartbeat data block (protocol 0x13).

    battery_level: 0-6 (the server maps this to 0-100% via _GT06_BATTERY_MAP)
    signal: 0-4
    """
    info = 0x08 if charging else 0x00
    return struct.pack(">BBB", info, battery_level, signal)


_ALARM_BITS = {"Normal": 0, "Shock": 1, "Power Cut": 2,
               "Low Battery": 3, "SOS": 4}


def build_alarm_data(loc_data, alarm_type="SOS", battery_level=6,
                     signal=4, charging=False):
    """Build the alarm data block (protocols 0x16 / 0x23): LOC + minimal LBS + terminal_info + batt + sig."""
    alarm_bits = _ALARM_BITS.get(alarm_type, 0)
    lbs_len = 0
    ti = (alarm_bits << 3)
    if charging:
        ti |= 0x04
    extra = struct.pack(">BBBB", lbs_len, ti, battery_level, signal)
    return loc_data + extra


def build_command_ack_data(server_flag, response_text):
    """Build the payload of a 0x15 command-response frame.

    The 0x15 frame mirrors the server's 0x80 command structure:
      content_len(1) + server_flag(4) + ASCII response.

    server_flag echoes the flag from the original 0x80 the device is replying to
    (real devices echo it back). Our listener doesn't currently check it but
    keeping the echo makes captures realistic.
    """
    if isinstance(response_text, str):
        response_text = response_text.encode("ascii")
    content_len = 4 + len(response_text)
    return struct.pack(">B", content_len) + server_flag + response_text


def parse_frame(frame):
    """Parse a GT06 frame into (protocol, data_bytes, serial).

    Caller is responsible for having extracted a complete frame (start 78 78,
    len byte, end 0d 0a). Returns None if the frame is too short to parse.
    """
    if len(frame) < 7:
        return None
    protocol = frame[3]
    length = frame[2]
    serial_offset = 3 + length - 4
    if serial_offset + 2 > len(frame):
        return None
    serial = struct.unpack(">H", frame[serial_offset:serial_offset + 2])[0]
    data = frame[4:serial_offset]
    return protocol, data, serial


def extract_command(frame):
    """Pull (server_flag, ascii_cmd) from a 0x80 server-command frame.

    Returns (server_flag_bytes, command_text) or None if frame isn't 0x80
    or the payload is too short.
    """
    parsed = parse_frame(frame)
    if not parsed:
        return None
    protocol, data, _serial = parsed
    if protocol != 0x80:
        return None
    if len(data) < 5:
        return None
    server_flag = data[1:5]
    cmd_text = data[5:].decode("ascii", errors="replace")
    return server_flag, cmd_text


def iter_frames(buf):
    """Yield complete GT06 frames from a buffer; return the leftover bytes.

    Usage:
        frames, buf = iter_frames(buf)  # returns (list_of_frames, remaining_buf)
    """
    frames = []
    while len(buf) >= 5:
        if buf[0:2] != b"\x78\x78":
            idx = buf.find(b"\x78\x78", 1)
            if idx < 0:
                buf = b""
                break
            buf = buf[idx:]
            continue
        length = buf[2]
        frame_size = 2 + 1 + length + 2  # start(2) + len(1) + payload + end(2)
        if len(buf) < frame_size:
            break
        frames.append(buf[:frame_size])
        buf = buf[frame_size:]
    return frames, buf
