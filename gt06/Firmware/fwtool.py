#!/usr/bin/env python3
"""
fwtool.py - unpack / repack / patch tool for Tutrack W07 (SIMCom A7670SA / ASR1603)
OTA firmware images, as delivered by the vendor's `YCSJ,<url>,<port>#` FOTA command.

Container format (reverse-engineered, see container_format.md):

    offset  size  field
    0x00    4     u32 LE  compressed_payload_length  (= filesize - 8)
    0x04    4     u32 LE  ~crc32(payload)            (bitwise NOT of CRC-32 of payload)
    0x08    ...   payload = legacy .lzma stream (FORMAT_ALONE)
                    props 0x5d (lc=3,lp=0,pb=2), dict 0x00800000 (8 MB),
                    uncompressed-size field = real size of decompressed image.

The payload decompresses to the raw application image (Thumb-2, links @ ~0x7E1B_0000).
There is NO cryptographic signature - only the CRC-32 integrity word above.

Usage:
    fwtool.py info    <fw.bin>
    fwtool.py unpack  <fw.bin> <out.raw>
    fwtool.py pack    <in.raw> <out.bin>
    fwtool.py patch   <fw.bin> <out.bin> --find OLD --replace NEW [--count N] [--encoding ascii]
    fwtool.py verify  <fw.bin>

`patch` only allows a SAME-LENGTH replacement so that no code/data pointers move
(the image is not relocated by us; changing lengths would corrupt it).
"""
import sys, struct, lzma, binascii, argparse

WRAP = 8                       # wrapper header size
LZMA_PROPS = 0x5d
LZMA_DICT  = 0x00800000
LZMA_FILTERS = [{"id": lzma.FILTER_LZMA1, "dict_size": LZMA_DICT, "lc": 3, "lp": 0, "pb": 2}]


def crc_word(payload: bytes) -> int:
    """Vendor integrity word = bitwise-NOT of CRC-32 of the compressed payload."""
    return (binascii.crc32(payload) ^ 0xFFFFFFFF) & 0xFFFFFFFF


def unpack(bindata: bytes) -> bytes:
    """Return the decompressed raw image from a wrapped OTA .bin."""
    if len(bindata) < WRAP + 13:
        raise ValueError("file too small to be a wrapped OTA image")
    clen, cword = struct.unpack("<II", bindata[:WRAP])
    payload = bindata[WRAP:WRAP + clen]          # trailing bytes after clen are XOR pad
    if len(payload) != clen:
        raise ValueError("length word 0x%08X > available bytes %d" % (clen, len(payload)))
    if cword != crc_word(payload):
        raise ValueError("CRC word mismatch: header 0x%08X computed 0x%08X"
                         % (cword, crc_word(payload)))
    if payload[0] != LZMA_PROPS:
        raise ValueError("unexpected LZMA props 0x%02X" % payload[0])
    return lzma.decompress(payload, format=lzma.FORMAT_ALONE)


def xor8(b: bytes) -> int:
    x = 0
    for c in b:
        x ^= c
    return x


def pack(raw: bytes, xor_target: int = 0x80) -> bytes:
    """Compress + wrap a raw image into a vendor-format OTA .bin.

    The device's FOTA verify (Rlt:9 on failure) is an 8-bit XOR over the whole
    downloaded file, compared against the hex value in the trailing YCSJ command
    field (the vendor's `,80#` means port 80 AND expected XOR 0x80). The vendor pads
    each firmware so its whole-file XOR == 0x80. We reproduce that by appending one
    pad byte: pad = (current whole-file XOR) ^ xor_target. Pass xor_target=None to
    skip (then you must pass the real XOR as the YCSJ trailing field instead).
    """
    payload = bytearray(lzma.compress(raw, format=lzma.FORMAT_ALONE, filters=LZMA_FILTERS))
    # liblzma writes the uncompressed-size field as 0xFFFF... (unknown); the vendor
    # stores the real size and the on-device loader reads it. Force the real size.
    struct.pack_into("<Q", payload, 5, len(raw))
    payload = bytes(payload)
    wrapper = struct.pack("<II", len(payload), crc_word(payload))
    out = wrapper + payload
    if xor_target is not None:
        out += bytes([xor8(out) ^ (xor_target & 0xFF)])  # trailing pad → whole-file XOR = target
    return out


def info(bindata: bytes):
    clen, cword = struct.unpack("<II", bindata[:WRAP])
    payload = bindata[WRAP:]
    props = payload[0]
    dic, = struct.unpack("<I", payload[1:5])
    usz, = struct.unpack("<Q", payload[5:13])
    print("file size            : %d bytes" % len(bindata))
    print("wrapper.len          : 0x%08X (%d)   %s" % (clen, clen,
          "OK" if clen == len(payload) else "MISMATCH(%d)" % len(payload)))
    print("wrapper.~crc32       : 0x%08X          %s" % (cword,
          "OK" if cword == crc_word(payload) else "MISMATCH(0x%08X)" % crc_word(payload)))
    print("payload.lzma props   : 0x%02X (lc=3,lp=0,pb=2)" % props)
    print("payload.lzma dict    : 0x%08X (%d MB)" % (dic, dic // (1024 * 1024)))
    print("payload.uncomp size  : %d bytes" % usz)
    try:
        raw = unpack(bindata)
        print("decompress           : OK -> %d bytes" % len(raw))
        h = struct.unpack("<8I", raw[:32])
        print("image header (32B)   :")
        names = ["+00", "+04", "+08 size?", "+0C count", "+10 ptr", "+14 entry", "+18 ptr", "+1C"]
        for n, v in zip(names, h):
            print("    %-10s 0x%08X" % (n, v))
    except Exception as e:
        print("decompress           : FAIL %s" % e)


def patch(bindata: bytes, find: bytes, repl: bytes, count: int) -> bytes:
    if len(find) != len(repl):
        raise ValueError("find (%d) and replace (%d) must be the same length; "
                         "changing length would move pointers and corrupt the image"
                         % (len(find), len(repl)))
    raw = unpack(bindata)
    n = raw.count(find)
    if n == 0:
        raise ValueError("string %r not found in image" % find)
    print("found %d occurrence(s) of %r" % (n, find))
    newraw = raw.replace(find, repl, count if count > 0 else -1)
    print("replaced %d occurrence(s) -> %r" % (n if count <= 0 else min(n, count), repl))
    out = pack(newraw)
    # self-check: our output must decompress back to the patched image
    assert unpack(out) == newraw, "internal round-trip check failed"
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("info");   p.add_argument("bin")
    p = sub.add_parser("unpack"); p.add_argument("bin"); p.add_argument("raw")
    p = sub.add_parser("pack");   p.add_argument("raw"); p.add_argument("bin")
    p = sub.add_parser("verify"); p.add_argument("bin")
    p = sub.add_parser("patch")
    p.add_argument("bin"); p.add_argument("out")
    p.add_argument("--find", required=True)
    p.add_argument("--replace", required=True)
    p.add_argument("--count", type=int, default=0, help="max replacements (0 = all)")
    p.add_argument("--encoding", default="ascii")
    a = ap.parse_args()

    if a.cmd == "info":
        info(open(a.bin, "rb").read())
    elif a.cmd == "unpack":
        raw = unpack(open(a.bin, "rb").read())
        open(a.raw, "wb").write(raw)
        print("wrote %s (%d bytes)" % (a.raw, len(raw)))
    elif a.cmd == "pack":
        out = pack(open(a.raw, "rb").read())
        open(a.bin, "wb").write(out)
        print("wrote %s (%d bytes)" % (a.bin, len(out)))
    elif a.cmd == "verify":
        info(open(a.bin, "rb").read())
    elif a.cmd == "patch":
        out = patch(open(a.bin, "rb").read(),
                    a.find.encode(a.encoding), a.replace.encode(a.encoding), a.count)
        open(a.out, "wb").write(out)
        print("wrote %s (%d bytes)" % (a.out, len(out)))


if __name__ == "__main__":
    main()
