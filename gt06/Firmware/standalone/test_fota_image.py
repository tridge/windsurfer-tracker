#!/usr/bin/env python3
"""SITL test: our firmware must accept the SAME image form we hand to the vendor FOTA.

The vendor downloader demands an image of 40960..245760 bytes whose whole-file 8-bit XOR matches the
value in the YCSJ command, so our builds are zero-padded up to that floor. Maintaining a second,
pad-free build purely for our own firmware is an obvious foot-gun -- it already caused one failed
flash when the wrong form was sent.

Our FOTA code caps the write/CRC at the wrapper's clen+8 and discards the pad tail, so it should
accept the padded image too. This proves it end to end rather than by reading the source:

  - the padded (vendor-form) image is served over HTTP and triggered with YCSJ
  - the staged partition must contain exactly [wrapper][payload] (clen+8) with the pad dropped
  - the pending-commit marker must be set
  - and the pad-free form must still work, so one build can serve both consumers
"""

import functools
import http.server
import operator
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# absolute: the SITL process runs with cwd set to a temp dir so its staging files are isolated
SITL = os.path.abspath(os.environ.get("SITL_BIN") or os.path.join(HERE, "sitl"))
IMAGE = os.path.join(HERE, "standalone_fw.bin")

FOTA_RESERVED = "sitl_reserved.bin"
FOTA_PENDING = "sitl_fota_pending"
# SITL commits reserved -> customer_app on the reboot that follows a good stage, so a successful
# FOTA leaves the COMMITTED image behind rather than the staging file.
FOTA_CUSTOMER = "sitl_customer_app.bin"

VENDOR_MIN = 40960          # the vendor downloader's lower bound
VENDOR_XOR = 0x80           # the value our YCSJ commands assert


def gt06_cmd(command, serial):
    text = command.encode()
    length = 10 + len(text)
    body = (bytes([0x80, 4 + len(text)]) + b"\x00\x00\x00\x00" + text
            + serial.to_bytes(2, "big") + b"\x00\x00")
    return b"\x78\x78" + bytes([length]) + body + b"\x0d\x0a"


def pad_to_vendor_form(raw):
    """Zero-pad to the vendor's size floor. Zeros leave the 8-bit XOR untouched, so the build's
    existing pad byte still lands the whole file on 0x80."""
    out = bytearray(raw)
    out += b"\x00" * (65536 - len(out))
    assert functools.reduce(operator.xor, out, 0) == VENDOR_XOR, "padded image lost its XOR"
    assert VENDOR_MIN <= len(out) <= 245760
    return bytes(out)


class _Serve(http.server.BaseHTTPRequestHandler):
    payload = b""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, *a):
        pass


def serve(payload):
    _Serve.payload = payload
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Serve)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def run_fota(image_bytes, label):
    """Feed `image_bytes` to a SITL device over YCSJ; return the staged bytes (or None)."""
    workdir = tempfile.mkdtemp(prefix="fota_")
    listing = []
    httpd, http_port = serve(image_bytes)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    gt_port = listener.getsockname()[1]

    proc = subprocess.Popen(
        [SITL, "--server", f"127.0.0.1:{gt_port}", "--teriid", "888800000000140", "--no-reexec"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=workdir, env=dict(os.environ))

    conn = None
    try:
        listener.settimeout(8)
        conn, _ = listener.accept()
        time.sleep(2)
        # NB the trailing YCSJ field means different things to the two consumers: the VENDOR reads it
        # as the expected 8-bit XOR (its port is hardcoded to 80), while OUR firmware reads it as the
        # port and lets it override the URL's. Both are 80 in production so one command serves both;
        # here the test server is on an ephemeral port, so it has to go in the trailing field.
        url = f"http://127.0.0.1:{http_port}/W07_MG133_WSTR_B53_test.bin"
        conn.sendall(gt06_cmd(f"YCSJ,{url},{http_port}#", 1))

        # a successful FOTA stages the image, sets the marker and reboots (exits, with --no-reexec)
        deadline = time.monotonic() + 30
        committed = os.path.join(workdir, FOTA_CUSTOMER)
        staged = os.path.join(workdir, FOTA_RESERVED)
        pending = os.path.join(workdir, FOTA_PENDING)
        while time.monotonic() < deadline:
            if os.path.exists(committed) or (os.path.exists(pending) and os.path.exists(staged)):
                time.sleep(0.5)
                break
            time.sleep(0.25)

        out = None
        src = committed if os.path.exists(committed) else staged
        if os.path.exists(src):
            with open(src, "rb") as f:
                out = f.read()
        # committed (image applied) or still staged with the pending marker — either proves acceptance
        marker = os.path.exists(committed) or os.path.exists(pending)
        listing = sorted(os.listdir(workdir))
        result = [out, marker]
    finally:
        if conn is not None:
            conn.close()
        listener.close()
        httpd.shutdown()
        proc.terminate()
        try:
            so, se = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            so, se = proc.communicate(timeout=5)
        shutil.rmtree(workdir, ignore_errors=True)
    # the firmware narrates the FOTA on its debug port, which SITL maps to stdout
    log = b"\n".join(l for l in (so or b"").splitlines() if b"FOTA" in l or b"fota" in l)
    log += b"\nworkdir contained: " + repr(listing).encode()
    return result[0], result[1], log


def main():
    if not os.path.exists(SITL):
        raise SystemExit("build first: make sitl")
    if not os.path.exists(IMAGE):
        raise SystemExit("build first: make")

    raw = open(IMAGE, "rb").read()
    clen = int.from_bytes(raw[0:4], "little")
    want = clen + 8

    # ---- the vendor form: padded to the 40960 floor, whole-file XOR 0x80 ----
    padded = pad_to_vendor_form(raw)
    assert len(padded) != want, "padded image should be larger than the wrapper payload"
    staged, marker, out = run_fota(padded, "padded")
    assert staged is not None, f"vendor-form image was never staged\n{out.decode(errors='replace')[-2000:]}"
    assert marker, "vendor-form image was never committed or staged"
    assert len(staged) == want, (
        f"image is {len(staged)} B, expected {want} (clen+8) — the pad tail was not dropped")
    assert staged == raw[:want], "staged bytes differ from [wrapper][payload]"
    print(f"vendor-form (padded {len(padded)} B) accepted, staged {len(staged)} B PASS")

    # ---- and the pad-free form must keep working ----
    padfree = raw[:want]
    staged2, marker2, out2 = run_fota(padfree, "padfree")
    assert staged2 is not None, f"pad-free image was never staged\n{out2.decode(errors='replace')[-2000:]}"
    assert marker2, "pad-free image was never committed or staged"
    assert staged2 == raw[:want], "pad-free staging differs from [wrapper][payload]"
    print(f"pad-free ({len(padfree)} B) accepted, staged {len(staged2)} B PASS")

    print("ONE image serves both the vendor FOTA and our own firmware")


if __name__ == "__main__":
    main()
