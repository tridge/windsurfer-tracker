#!/usr/bin/env python3
"""Emit a text2pcap-ready hexdump of one valid GT06 frame of each type.

Each frame carries a correct CRC-ITU so the dissector's CRC check passes. Turn the
output into a pcap with:

    python3 make_sample_pcap.py > sample.hex
    text2pcap -T 1024,7711 sample.hex sample.pcap
    tshark -r sample.pcap -O gt06        # or open sample.pcap in Wireshark

Framing mirrors server/protocol_GT06.py (start 78 78, 1-byte length, serial+CRC-ITU,
stop 0D 0A). Standalone — no server import.
"""

import struct


def crc_itu(data):
    """CRC-ITU (CRC-16/X.25): poly 0x8408 reflected, init 0xFFFF, xorout 0xFFFF."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def build(proto, payload, serial):
    """78 78 | LEN | PROTO | PAYLOAD | SERIAL(2) | CRC(2) | 0D 0A."""
    length = 1 + len(payload) + 2 + 2            # proto + payload + serial + crc
    head = bytes([length, proto]) + payload + struct.pack(">H", serial)
    return b"\x78\x78" + head + struct.pack(">H", crc_itu(head)) + b"\x0d\x0a"


def gps_block(lat, lon, kmh, heading, sats, fix=True):
    """18-byte location block for 2026-06-21 12:00:00 UTC at lat/lon."""
    dt = bytes([26, 6, 21, 12, 0, 0])            # YY MO DD HH MI SS (raw bytes)
    gpsinfo = bytes([(12 << 4) | (sats & 0x0F)])
    lat_raw = int(round(abs(lat) * 1_800_000))
    lon_raw = int(round(abs(lon) * 1_800_000))
    course = heading & 0x03FF
    if fix:        course |= 0x1000              # bit12 GPS positioned
    if lat >= 0:   course |= 0x0400              # bit10 set => North (is_south = not bit10)
    if lon < 0:    course |= 0x0800              # bit11 set => West
    return (dt + gpsinfo + struct.pack(">I", lat_raw) + struct.pack(">I", lon_raw)
            + bytes([kmh]) + struct.pack(">H", course))


def command(cmd, serial):
    payload = bytes([4 + len(cmd)]) + b"\x00\x00\x00\x00" + cmd.encode("ascii")
    return build(0x80, payload, serial)


def response(text, serial):
    payload = b"\x00\x00\x00\x00" + bytes([len(text) & 0xFF]) + text.encode("ascii")
    return build(0x15, payload, serial)


def hexdump(frame):
    """Offset-prefixed lines for one packet (offset resets to 0 => new packet)."""
    out = []
    for i in range(0, len(frame), 16):
        chunk = frame[i:i + 16]
        out.append("%06x " % i + " ".join("%02x" % b for b in chunk))
    return "\n".join(out)


def main():
    frames = [
        ("Login (IMEI 866557081378657)", build(0x01, bytes.fromhex("0866557081378657"), 1)),
        ("Location 0x12 (Canberra)",     build(0x12, gps_block(-35.28090, 149.13000, 12, 90, 11), 2)),
        ("Heartbeat 0x13",               build(0x13, bytes([0x44, 5, 4]) + struct.pack(">H", 0x0002), 3)),
        ("Server command STATUS#",       command("STATUS#", 4)),
        ("String response Battery:3.47V", response("Battery:3.47V", 5)),
        ("Alarm/SOS 0x16",
         build(0x16, gps_block(-35.28090, 149.13000, 0, 0, 9)
               + bytes([0x00, 0x60, 5, 4]) + struct.pack(">H", 0x0100), 6)),
    ]
    blocks = []
    for name, fr in frames:
        blocks.append("# %s  (%d bytes)\n%s" % (name, len(fr), hexdump(fr)))
    print("\n\n".join(blocks))


if __name__ == "__main__":
    main()
