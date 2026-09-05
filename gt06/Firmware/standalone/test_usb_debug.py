#!/usr/bin/env python3
"""End-to-end WDBG test through the simulated if04 USB transport."""

import os
import socket
import struct
import subprocess
import time

import usb_debug


HERE = os.path.dirname(os.path.abspath(__file__))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def connect(port):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            return usb_debug.TcpTransport(f"127.0.0.1:{port}", 1)
        except (OSError, usb_debug.DebugError):
            time.sleep(0.05)
    raise AssertionError("USB-debug SITL listener did not start")


def main():
    port = free_port()
    proc = subprocess.Popen(
        [os.path.join(HERE, "usb-debug-sitl"), "--usb-listen", str(port), "--no-reexec"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    transport = None
    try:
        transport = connect(port)
        client = usb_debug.Client(transport, 2)

        # Noise and a partial non-frame must not desynchronise the next fixed request.
        transport.write(b"console-noise")
        status, payload = client.request(usb_debug.OP_PING)
        assert status == 0 and payload.startswith(b"W07C usbdbg/1")

        status, payload = client.request(usb_debug.OP_INFO)
        assert status == 0 and len(payload) == 14 * 4
        info = struct.unpack("<14I", payload)
        assert info[0] == 1 and info[1] == 0x7E1B0000 and info[12] == 256 and info[13] == 512

        status, payload = client.request(usb_debug.OP_READ, [0x7E1B0000, 32])
        assert status == 0 and payload == bytes(range(32))
        status, _ = client.request(usb_debug.OP_READ, [0x80000000, 4])
        assert status == 5                         # resident/XIP reads require F_UNSAFE_READ
        status, payload = client.request(
            usb_debug.OP_READ, [0x80000000, 4], usb_debug.F_UNSAFE_READ)
        assert status == 0 and payload == b"\x00\x01\x02\x03"

        # A recovered exact map resolves useful APIs without guessing the vendor hash algorithm.
        hits = usb_debug.resolve_name(client, "sAPI_GnssPowerStatusGet", quiet=True)
        assert hits == [("mapped", usb_debug.GNSS_POWER_STATUS_HASH, 0x7E600004)]
        ptr = hits[0][2]
        assert ptr == 0x7E600004
        status, payload = client.request(
            usb_debug.OP_CALL, [ptr, 0xFFFFFFFF, 0, 0], usb_debug.F_CALL_ACK)
        assert status == 0 and len(payload) == 260
        assert struct.unpack_from("<I", payload)[0] == 1 and payload[4] == 1

        # Unmapped names still exercise the old candidate-algorithm fallback.
        hits = usb_debug.resolve_name(client, "sAPI_SmsDelAllMsg", quiet=True)
        assert any(label == "djb2/strip-sAPI" and hit_ptr == 0x7E600008
                   for label, _, hit_ptr in hits)

        # Preload eight argument words and separate input data, pass a scratch+32 pointer, and verify
        # both the full AAPCS call path and output-buffer round trip.
        usb_debug.write_scratch(client, 32, b"hello\0")
        usb_debug.write_scratch(
            client, 0, struct.pack("<8I", 0xFFFF0020, 2, 3, 4, 5, 6, 7, 8))
        status, payload = client.request(
            usb_debug.OP_CALL8, [ptr, 8], usb_debug.F_CALL_ACK)
        assert status == 0 and len(payload) == 260
        assert struct.unpack_from("<I", payload)[0] == 1
        assert payload[4 + 32:4 + 38] == b"\x01ello\0"

        status, _ = client.request(usb_debug.OP_PING)
        assert status == 0
        print("WDBG framing/read guard/resolver/call3/call8 scratch PASS")
    finally:
        if transport is not None:
            transport.close()
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


if __name__ == "__main__":
    main()
