#!/usr/bin/env python3
"""Host client for the W07C dedicated USB debug firmware (if04 / WDBG v1)."""

import argparse
import os
from pathlib import Path
import socket
import struct
import sys
import time
import zlib


MAGIC_REQ = b"WDBG"
MAGIC_RESP = b"WDBR"
VERSION = 1
REQ = struct.Struct("<4sBBHIIIII")       # through arg3; CRC32 follows
RESP = struct.Struct("<4sBBHII")         # payload and CRC32 follow

OP_PING, OP_INFO, OP_READ, OP_RESOLVE, OP_CALL, OP_RESET, OP_SCRATCH_WRITE, OP_CALL8 = range(1, 9)
F_UNSAFE_READ = 1
F_CALL_ACK = 2
STATUS = {
    0: "ok", 1: "bad protocol version", 2: "bad CRC", 3: "bad opcode",
    4: "bad argument", 5: "operation denied", 6: "symbol not found",
    7: "unsupported by this build",
}
DEFAULT_EXPORT_TABLE = 0x7E4643F0
GNSS_POWER_STATUS_HASH = 0xE6A5FFAD


class DebugError(RuntimeError):
    pass


class DeviceUnresponsive(DebugError):
    pass


def number(text):
    try:
        return int(text, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an integer: {text}") from exc


def find_if04():
    """Map USB interface 04 to its option-driver tty (e.g. :1.4 -> /dev/ttyUSB1)."""
    for tty in sorted(Path("/sys/class/tty").glob("ttyUSB*")):
        try:
            real = tty.resolve()
        except OSError:
            continue
        if any(part.endswith(":1.4") for part in real.parts):
            return f"/dev/{tty.name}"
    return None


class SerialTransport:
    def __init__(self, path, timeout):
        try:
            import serial
        except ImportError as exc:
            raise DebugError("pyserial is required for a real USB port") from exc
        try:
            self.io = serial.Serial(path, 115200, timeout=min(timeout, 0.25), write_timeout=timeout)
            self.io.reset_input_buffer()
        except Exception as exc:
            raise DebugError(f"cannot open {path}: {exc}") from exc

    def write(self, data):
        self.io.write(data)
        self.io.flush()

    def read(self, length):
        return self.io.read(length)

    def close(self):
        self.io.close()


class TcpTransport:
    def __init__(self, endpoint, timeout):
        host, sep, port = endpoint.rpartition(":")
        if not sep:
            raise DebugError("--tcp must be HOST:PORT")
        self.io = socket.create_connection((host or "127.0.0.1", int(port)), timeout=timeout)
        self.io.settimeout(min(timeout, 0.25))

    def write(self, data):
        self.io.sendall(data)

    def read(self, length):
        try:
            return self.io.recv(length)
        except socket.timeout:
            return b""

    def close(self):
        self.io.close()


class Client:
    def __init__(self, transport, timeout):
        self.io = transport
        self.timeout = timeout
        self.seq = int(time.time()) & 0xFFFFFFFF
        self.buf = bytearray()

    def request(self, op, args=(), flags=0):
        vals = list(args)[:4] + [0] * (4 - len(args))
        self.seq = (self.seq + 1) & 0xFFFFFFFF
        head = REQ.pack(MAGIC_REQ, VERSION, op, flags, self.seq, *vals)
        frame = head + struct.pack("<I", zlib.crc32(head) & 0xFFFFFFFF)
        try:
            self.io.write(frame)
        except Exception as exc:
            raise DeviceUnresponsive(f"USB write failed: {exc}") from exc

        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            start = self.buf.find(MAGIC_RESP)
            if start > 0:
                del self.buf[:start]
            if start < 0 and len(self.buf) > 3:
                del self.buf[:-3]
            if len(self.buf) >= RESP.size:
                magic, ver, rop, status, seq, length = RESP.unpack_from(self.buf)
                if length > 512:
                    del self.buf[0]
                    continue
                total = RESP.size + length + 4
                if len(self.buf) >= total:
                    raw = bytes(self.buf[:RESP.size + length])
                    got_crc = struct.unpack_from("<I", self.buf, RESP.size + length)[0]
                    payload = bytes(self.buf[RESP.size:RESP.size + length])
                    del self.buf[:total]
                    if zlib.crc32(raw) & 0xFFFFFFFF != got_crc:
                        raise DebugError("response CRC mismatch")
                    if magic != MAGIC_RESP or ver != VERSION:
                        raise DebugError("unexpected response header")
                    if seq != self.seq:
                        continue
                    if rop != op:
                        raise DebugError(f"response opcode {rop} did not match request {op}")
                    return status, payload
            try:
                chunk = self.io.read(1024)
            except Exception as exc:
                raise DeviceUnresponsive(f"USB read failed: {exc}") from exc
            if chunk:
                self.buf.extend(chunk)
            else:
                time.sleep(0.01)
        raise DeviceUnresponsive(
            "device stopped responding; the probe may have crashed the CPU and a physical reboot/power-cycle may be required")


def need_ok(result, operation):
    status, payload = result
    if status:
        raise DebugError(f"{operation}: {STATUS.get(status, f'status {status}')}")
    return payload


def hexdump(data, base):
    for off in range(0, len(data), 16):
        row = data[off:off + 16]
        hx = " ".join(f"{v:02x}" for v in row)
        asc = "".join(chr(v) if 32 <= v < 127 else "." for v in row)
        print(f"{base + off:08x}  {hx:<47}  |{asc}|")


def read_memory(client, address, length, unsafe=False):
    """Read a contiguous target range, splitting it into protocol-sized requests."""
    if length <= 0:
        raise DebugError("read length must be positive")
    data = bytearray()
    while len(data) < length:
        addr = address + len(data)
        count = min(length - len(data), 512)
        flags = F_UNSAFE_READ if unsafe else 0
        data.extend(need_ok(client.request(OP_READ, [addr, count], flags), "read"))
    return bytes(data)


def read_exports(client, table, max_entries):
    """Enumerate the resident resolver's {hash, function} table up to its null terminator."""
    entries = []
    while len(entries) < max_entries:
        count = min(64, max_entries - len(entries))
        raw = read_memory(client, table + len(entries) * 8, count * 8, unsafe=True)
        for off in range(0, len(raw), 8):
            value, function = struct.unpack_from("<II", raw, off)
            if function == 0:
                return entries
            entries.append((value, function))
    raise DebugError(f"export table has no terminator within {max_entries} entries")


def transforms(name):
    yield "as-is", name
    yield "strip-sAPI", name[5:] if name.startswith("sAPI_") else name
    yield "lower", name.lower()
    yield "upper", name.upper()


def hash_candidates(name):
    """Same compact candidate set as firmware SAPIFIND; yield (label, hash)."""
    for transform, text in transforms(name):
        raw = text.encode()
        funcs = []
        h = 5381
        for c in raw:
            h = (h * 33 + c) & 0xFFFFFFFF
        funcs.append(("djb2", h))
        h = 5381
        for c in raw:
            h = ((h * 33) ^ c) & 0xFFFFFFFF
        funcs.append(("djb2xor", h))
        h = 0
        for c in raw:
            h = (c + (h << 6) + (h << 16) - h) & 0xFFFFFFFF
        funcs.append(("sdbm", h))
        h = 0x811C9DC5
        for c in raw:
            h = ((h * 0x01000193) & 0xFFFFFFFF) ^ c
        funcs.append(("fnv1", h))
        h = 0x811C9DC5
        for c in raw:
            h = ((h ^ c) * 0x01000193) & 0xFFFFFFFF
        funcs.append(("fnv1a", h))
        h = 0
        for c in raw:
            h = (h << 4) + c
            g = h & 0xF0000000
            if g:
                h ^= g >> 24
            h &= ~g
        funcs.append(("elf", h & 0xFFFFFFFF))
        h = 0
        for c in raw:
            h = (h + c) & 0xFFFFFFFF
            h = (h + (h << 10)) & 0xFFFFFFFF
            h ^= h >> 6
        h = (h + (h << 3)) & 0xFFFFFFFF
        h ^= h >> 11
        h = (h + (h << 15)) & 0xFFFFFFFF
        funcs.append(("joaat", h))
        funcs.append(("crc32", zlib.crc32(raw) & 0xFFFFFFFF))
        funcs.append(("sum", sum(raw) & 0xFFFFFFFF))
        for func, value in funcs:
            yield f"{func}/{transform}", value


def resolve_hash(client, value):
    status, payload = client.request(OP_RESOLVE, [value])
    ptr = struct.unpack("<I", payload)[0] if len(payload) == 4 else 0
    if status not in (0, 6):
        raise DebugError(f"resolve: {STATUS.get(status, status)}")
    return ptr


def resolve_name(client, name, quiet=False):
    hits = []
    seen = set()
    for label, value in hash_candidates(name):
        if value in seen:
            continue
        seen.add(value)
        ptr = resolve_hash(client, value)
        if ptr:
            hits.append((label, value, ptr))
            if not quiet:
                print(f"{name}: {label} hash=0x{value:08x} -> 0x{ptr:08x}")
    if not hits and not quiet:
        print(f"{name}: no candidate hash resolved")
    return hits


def do_call(client, address, args):
    status, payload = client.request(OP_CALL, [address, *args], F_CALL_ACK)
    if status:
        raise DebugError(f"call: {STATUS.get(status, status)}")
    if len(payload) < 4:
        raise DebugError(f"call: malformed {len(payload)}-byte result")
    ret = struct.unpack_from("<I", payload)[0]
    print(f"return: 0x{ret:08x} ({struct.unpack('<i', payload[:4])[0]})")
    print("scratch (use argument 'scratch' to pass its address):")
    hexdump(payload[4:], 0)
    # A reply proves the call returned.  A follow-up ping proves it did not poison USB/app state.
    pong = need_ok(client.request(OP_PING), "post-call ping")
    print(f"post-call health: {pong.decode(errors='replace')}")


def scratch_token(text):
    if text == "scratch":
        return 0xFFFF0000
    if text.startswith("scratch+"):
        offset = number(text[8:])
        if not 0 <= offset <= 255:
            raise DebugError("scratch offset must be in 0..255")
        return 0xFFFF0000 | offset
    return number(text)


def write_scratch(client, offset, data):
    if offset < 0 or offset + len(data) > 256:
        raise DebugError("scratch write exceeds the 256-byte buffer")
    for pos in range(0, len(data), 8):
        chunk = data[pos:pos + 8]
        padded = chunk + bytes(8 - len(chunk))
        lo, hi = struct.unpack("<II", padded)
        need_ok(client.request(OP_SCRATCH_WRITE, [offset + pos, len(chunk), lo, hi]), "scratch write")


def main():
    ap = argparse.ArgumentParser(description="W07C USB debug firmware client")
    ap.add_argument("--port", help="if04 tty; auto-detected from USB interface 04")
    ap.add_argument("--tcp", help="SITL transport HOST:PORT instead of a tty")
    ap.add_argument("--timeout", type=float, default=3.0)
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("ping")
    sub.add_parser("info")
    rd = sub.add_parser("read")
    rd.add_argument("address", type=number)
    rd.add_argument("length", type=number)
    rd.add_argument("--unsafe", action="store_true", help="allow non-app/non-table addresses; may crash")
    rd.add_argument("--output", type=Path, help="write exact bytes instead of a formatted hex dump")
    rs = sub.add_parser("resolve")
    rs.add_argument("symbol_or_hash", help="sAPI name or numeric hash")
    ex = sub.add_parser("exports", help="enumerate every hash/function pair known to the resolver")
    ex.add_argument("--table", type=number, default=DEFAULT_EXPORT_TABLE,
                    help=f"export-table address (default 0x{DEFAULT_EXPORT_TABLE:08x} for this resident)")
    ex.add_argument("--max-entries", type=number, default=4096)
    ex.add_argument("--output", type=Path, help="write CSV instead of printing the table")
    ca = sub.add_parser("call")
    ca.add_argument("address", type=number)
    ca.add_argument("args", nargs="*", help="up to three integers; 'scratch' passes a zeroed 256-byte output buffer")
    ca.add_argument("--i-accept-crash-risk", action="store_true", required=True)
    cx = sub.add_parser("callx", help="call with up to eight arguments and optional scratch input data")
    cx.add_argument("address", type=number)
    cx.add_argument("args", nargs="*", help="up to eight integers or scratch[+offset] pointers")
    cx.add_argument("--data-hex", help="bytes to preload at scratch+32")
    cx.add_argument("--data-ascii", help="UTF-8 text to preload at scratch+32 (a NUL is appended)")
    cx.add_argument("--i-accept-crash-risk", action="store_true", required=True)
    gp = sub.add_parser("gps-power-status")
    gp.add_argument("--hash", type=number, default=GNSS_POWER_STATUS_HASH,
                    help=f"resolver hash (default: proven getter hash 0x{GNSS_POWER_STATUS_HASH:08x})")
    gp.add_argument("--i-accept-crash-risk", action="store_true", required=True)
    sub.add_parser("reset")
    args = ap.parse_args()

    port = args.port or find_if04()
    if args.tcp:
        transport = TcpTransport(args.tcp, args.timeout)
    elif port:
        transport = SerialTransport(port, args.timeout)
    else:
        raise DebugError("cannot find USB interface 04; use --port /dev/ttyUSBn")
    client = Client(transport, args.timeout)
    try:
        if args.command == "ping":
            print(need_ok(client.request(OP_PING), "ping").decode(errors="replace"))
        elif args.command == "info":
            data = need_ok(client.request(OP_INFO), "info")
            names = ("protocol", "image_base", "image_end", "bss_end", "table_low", "table_high",
                     "resolver", "vcom_write", "vcom_read", "vcom_regcb", "gnss_power_set",
                     "scratch", "scratch_size", "max_read")
            if len(data) != 4 * len(names):
                raise DebugError(f"info: unexpected payload size {len(data)}")
            for name, value in zip(names, struct.unpack(f"<{len(names)}I", data)):
                print(f"{name:16s} 0x{value:08x}" if name not in ("protocol", "scratch_size", "max_read")
                      else f"{name:16s} {value}")
        elif args.command == "read":
            data = read_memory(client, args.address, args.length, args.unsafe)
            if args.output:
                args.output.write_bytes(data)
                print(f"wrote {len(data)} bytes from 0x{args.address:08x} to {args.output}")
            else:
                hexdump(data, args.address)
        elif args.command == "resolve":
            try:
                value = int(args.symbol_or_hash, 0)
            except ValueError:
                if not resolve_name(client, args.symbol_or_hash):
                    return 1
            else:
                ptr = resolve_hash(client, value)
                print(f"0x{value:08x} -> " + (f"0x{ptr:08x}" if ptr else "not found"))
                if not ptr:
                    return 1
        elif args.command == "exports":
            entries = read_exports(client, args.table, args.max_entries)
            lines = ["index,hash,function"]
            lines.extend(f"{i},0x{value:08x},0x{function:08x}"
                         for i, (value, function) in enumerate(entries))
            text = "\n".join(lines) + "\n"
            if args.output:
                args.output.write_text(text)
                print(f"wrote {len(entries)} exports from 0x{args.table:08x} to {args.output}")
            else:
                print(text, end="")
        elif args.command == "call":
            if len(args.args) > 3:
                raise DebugError("call accepts at most three arguments")
            call_args = [0xFFFFFFFF if value == "scratch" else number(value) for value in args.args]
            call_args += [0] * (3 - len(call_args))
            do_call(client, args.address, call_args)
        elif args.command == "callx":
            if len(args.args) > 8:
                raise DebugError("callx accepts at most eight arguments")
            if args.data_hex is not None and args.data_ascii is not None:
                raise DebugError("use only one of --data-hex and --data-ascii")
            if args.data_hex is not None:
                try:
                    data = bytes.fromhex(args.data_hex)
                except ValueError as exc:
                    raise DebugError(f"invalid --data-hex: {exc}") from exc
            elif args.data_ascii is not None:
                data = args.data_ascii.encode() + b"\0"
            else:
                data = b""
            values = [scratch_token(value) for value in args.args]
            values += [0] * (8 - len(values))
            write_scratch(client, 32, data)
            write_scratch(client, 0, struct.pack("<8I", *values))
            status, payload = client.request(OP_CALL8, [args.address, len(args.args)], F_CALL_ACK)
            if status:
                raise DebugError(f"callx: {STATUS.get(status, status)}")
            if len(payload) < 4:
                raise DebugError(f"callx: malformed {len(payload)}-byte result")
            ret = struct.unpack_from("<I", payload)[0]
            print(f"return: 0x{ret:08x} ({struct.unpack('<i', payload[:4])[0]})")
            print("scratch:")
            hexdump(payload[4:], 0)
            pong = need_ok(client.request(OP_PING), "post-call ping")
            print(f"post-call health: {pong.decode(errors='replace')}")
        elif args.command == "gps-power-status":
            ptr = resolve_hash(client, args.hash)
            if not ptr:
                raise DebugError(f"GNSS power-status hash 0x{args.hash:08x} is absent from this resident")
            print(f"calling GPS power-status getter at 0x{ptr:08x}")
            do_call(client, ptr, [0, 0, 0])
        elif args.command == "reset":
            need_ok(client.request(OP_RESET), "reset")
            print("software reset requested")
    finally:
        transport.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DeviceUnresponsive as exc:
        print(f"CRASH/REBOOT NOTICE: {exc}", file=sys.stderr)
        raise SystemExit(3)
    except DebugError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
