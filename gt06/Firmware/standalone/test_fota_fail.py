#!/usr/bin/env python3
"""SITL: FOTA failures are bounded and leave the running app in charge."""

import http.server
import os
import shutil
import struct
import tempfile
import threading
import time
import zlib

from sitl_util import Server, start, finish


class Handler(http.server.BaseHTTPRequestHandler):
    mode = "404"
    body = b""

    def do_GET(self):
        if self.mode == "404":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        if self.mode == "trickle":
            for b in self.body:
                try:
                    self.wfile.write(bytes([b])); self.wfile.flush()
                except OSError:
                    return
                time.sleep(3)
        else:
            self.wfile.write(self.body)

    def log_message(self, *a):
        pass


def serve(mode, body=b""):
    Handler.mode, Handler.body = mode, body
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def wrapped(payload):
    """[clen][~crc32][payload] with a valid CRC."""
    return struct.pack("<II", len(payload), zlib.crc32(payload) ^ 0xFFFFFFFF) + payload


def ycsj(srv, port):
    srv.send_cmd(f"YCSJ,http://127.0.0.1:{port}/W07_MG133_WSTR_B53_x.bin,{port}#")


def test_http_404_stays_in_service():
    httpd = serve("404")
    srv = Server()
    work = tempfile.mkdtemp(prefix="fotaf_")
    proc = start(srv, "888800000000701", cwd=work)
    try:
        srv.accept_login()
        ycsj(srv, httpd.server_address[1])
        srv.pump(4)
        m = srv.query("CXCS#BOOTSTAT", r" ff(\d+)")
        assert int(m.group(1)) == 8, f"expected bad-HTTP abort, ff{m.group(1)}"
        assert not os.path.exists(os.path.join(work, "sitl_fota_pending"))
    finally:
        srv.close(); httpd.shutdown()
        out, _ = finish(proc)
        shutil.rmtree(work, ignore_errors=True)
    assert b"FOTA: bad HTTP status" in out
    print("FOTA HTTP 404: aborted before touching flash, stayed in service PASS")


def expect_fail_reboot(mode, body, teriid, timeout):
    httpd = serve(mode, body)
    srv = Server()
    work = tempfile.mkdtemp(prefix="fotaf_")
    proc = start(srv, teriid, reexec=True, cwd=work)
    try:
        srv.accept_login()
        ycsj(srv, httpd.server_address[1])
        t0 = time.monotonic()
        srv.wait_closed(timeout)
        srv.accept_login(30)
        took = time.monotonic() - t0
        m = srv.query("CXCS#BOOTSTAT", r"rr(\d+)")
        assert int(m.group(1)) == 6, f"expected FOTA_FAIL reboot, got rr{m.group(1)}"
        assert not os.path.exists(os.path.join(work, "sitl_fota_pending")), "bad image was marked pending"
        assert not os.path.exists(os.path.join(work, "sitl_customer_app.bin")), "bad image was committed"
    finally:
        srv.close(); httpd.shutdown()
        finish(proc)
        shutil.rmtree(work, ignore_errors=True)
    return took


def test_trickle_hits_deadline():
    took = expect_fail_reboot("trickle", wrapped(b"\x5d" + b"\0" * 400), "888800000000702", 60)
    print(f"FOTA trickle: deadline abort + FOTA_FAIL reboot after {took:.0f}s, nothing committed PASS")


def test_valid_crc_bad_lzma_rejected():
    took = expect_fail_reboot("ok", wrapped(b"x" * 4000), "888800000000703", 30)
    print("FOTA valid-CRC garbage payload: rejected, nothing committed PASS")


if __name__ == "__main__":
    test_http_404_stays_in_service()
    test_valid_crc_bad_lzma_rejected()
    test_trickle_hits_deadline()
    print("FOTA failures are bounded and never commit a bad image")
