#!/usr/bin/env python3
"""End-to-end SITL test for the USB<->TCP forwarding bridge (FWDSERVER/FWDPORT).

Simulates the UAV modem-link use: the tracker firmware (SITL) bridges bytes between a USB serial
port (here a loopback TCP listener via --usb-listen) and a TCP connection to a forward/ground server
(--fwd). We drive the "USB host" (the Raspberry Pi side) and the "forward server" and assert:

  A. USB->TCP throughput: ~8 KB/s streamed into USB arrives byte-exact + in order at the fwd server.
  B. TCP->USB: data from the fwd server arrives byte-exact at the USB host.
  C. Auto-reconnect + flush: drop the fwd server mid-stream; bytes sent while disconnected are NOT
     replayed after reconnect (the USB->TCP buffer is flushed on connect); fresh bytes flow after.
  D. No GT06 regression: the GT06 tracking socket stays connected throughout (it logs in + heartbeats).

Run from the repo root (needs gt06/Firmware/standalone/sitl built):
    make -C gt06/Firmware/standalone sitl && python3 scripts/sitl_fwd_test.py
"""
import os, re, signal, socket, subprocess, sys, threading, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITL = os.environ.get("SITL_BIN") or os.path.join(ROOT, "gt06", "Firmware", "standalone", "sitl")

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

class DrainServer(threading.Thread):
    """Accept one client and drain (store) everything it sends. Used for the GT06 socket and as the
    forward server. Exposes the accepted connection so a test can also send to the firmware."""
    def __init__(self, port, name):
        super().__init__(daemon=True)
        self.port, self.name = port, name
        self.ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.ls.bind(("127.0.0.1", port)); self.ls.listen(1)
        self.conn = None
        self.rx = bytearray()
        self.lock = threading.Lock()
        self.accepted = threading.Event()
        self._run = True
        self._drop = False
    def run(self):
        while self._run:
            try:
                self.ls.settimeout(0.5)
                c, _ = self.ls.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            c.settimeout(0.2)
            self.conn = c
            self.accepted.set()
            while self._run and not self._drop:
                try:
                    d = c.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not d:
                    break
                with self.lock:
                    self.rx += d
            try: c.close()
            except OSError: pass
            self.conn = None
            if self._drop:
                self._drop = False           # ready to accept the reconnect
                self.accepted.clear()
    def send(self, data):
        c = self.conn
        if c: c.sendall(data)
    def connected(self):
        return self.conn is not None
    def drop_client(self):
        self._drop = True
    def got(self):
        with self.lock:
            return bytes(self.rx)
    def clear(self):
        with self.lock:
            self.rx = bytearray()
    def stop(self):
        self._run = False
        try: self.ls.close()
        except OSError: pass

def gt06_cmd(ascii_cmd):
    """Build a GT06 0x80 server->device command frame (the firmware validates the 0d0a terminator, not
    the CRC): 78 78 len 80 content_len flag(4) ascii serial(2) crc(2) 0d 0a."""
    a = ascii_cmd.encode()
    length = 10 + len(a)                       # proto..crc
    body = bytes([0x80, 4 + len(a)]) + b"\x00\x00\x00\x00" + a + b"\x00\x01" + b"\x00\x00"
    return b"\x78\x78" + bytes([length]) + body + b"\x0d\x0a"

def wait(cond, timeout, msg):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond(): return True
        time.sleep(0.02)
    raise SystemExit("TIMEOUT: " + msg)

def main():
    if not os.path.exists(SITL):
        raise SystemExit("build first: make -C gt06/Firmware/standalone sitl")
    gt06_port, fwd_port, usb_port = free_port(), free_port(), free_port()

    gt06 = DrainServer(gt06_port, "gt06"); gt06.start()
    fwd  = DrainServer(fwd_port, "fwd");   fwd.start()

    env = dict(os.environ)
    proc = subprocess.Popen(
        [SITL, "--server", f"127.0.0.1:{gt06_port}", "--usb-listen", str(usb_port),
         "--fwd", f"127.0.0.1:{fwd_port}", "--teriid", "888800000000123", "--no-reexec"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    try:
        # D: GT06 socket connects (login) and the fwd socket connects — both concurrently.
        wait(lambda: gt06.accepted.is_set(), 8, "firmware never connected to the GT06 server")
        wait(lambda: fwd.accepted.is_set(), 8, "firmware never connected to the fwd server")
        wait(lambda: len(gt06.got()) > 0, 8, "no GT06 login frame received")
        print("[D] GT06 + FWD sockets both up (concurrent)          PASS")

        # USB host connects to the simulated if04 port.
        usb = socket.create_connection(("127.0.0.1", usb_port), timeout=5)
        usb.settimeout(0.2)
        time.sleep(0.3)

        # A: stream ~8 KB/s USB->TCP for 3 s, byte-exact + in order.
        fwd.clear()
        sent = bytearray()
        seq = 0
        t0 = time.time()
        while time.time() - t0 < 3.0:
            block = bytes((seq + i) & 0xFF for i in range(400)); seq += 400
            usb.sendall(block); sent += block
            time.sleep(0.05)                                   # 400 B / 50 ms = 8 KB/s
        wait(lambda: len(fwd.got()) >= len(sent), 5,
             f"USB->TCP short: got {len(fwd.got())} of {len(sent)}")
        assert fwd.got()[:len(sent)] == bytes(sent), "USB->TCP data corrupt/out-of-order"
        print(f"[A] USB->TCP {len(sent)} B @8KB/s byte-exact          PASS")

        # B: TCP->USB, byte-exact.
        down = bytes((i * 7) & 0xFF for i in range(2000))
        fwd.send(down)
        got = bytearray()
        t0 = time.time()
        while len(got) < len(down) and time.time() - t0 < 5:
            try: got += usb.recv(4096)
            except socket.timeout: pass
        assert bytes(got) == down, f"TCP->USB corrupt: {len(got)}/{len(down)}"
        print(f"[B] TCP->USB {len(down)} B byte-exact                 PASS")

        # C: reconnect + flush. Drop the fwd server; stream "stale" bytes while down; reconnect; then
        # send a fresh marker. The fwd server (new connection) must see ONLY post-reconnect bytes.
        fwd.drop_client()
        wait(lambda: not fwd.accepted.is_set(), 5, "fwd client did not drop")
        for _ in range(20):                                    # ~8 KB streamed into USB while TCP down
            usb.sendall(b"\xDE\xAD" * 200); time.sleep(0.02)
        time.sleep(0.5)
        fwd.clear()
        wait(lambda: fwd.accepted.is_set(), 10, "firmware did not reconnect to the fwd server")
        time.sleep(0.3)
        marker = bytes(range(0, 250))
        usb.sendall(marker)
        wait(lambda: len(fwd.got()) >= len(marker), 5, "post-reconnect bytes not received")
        rx = fwd.got()
        assert b"\xDE\xAD" not in rx, "STALE data replayed after reconnect (flush failed)"
        assert rx[:len(marker)] == marker, "post-reconnect data corrupt"
        print("[C] reconnect flushes stale USB->TCP buffer          PASS")

        # E: runtime config path — disable via SZCS#FWDPORT=0, then re-enable via SZCS#FWDSERVER, over GT06.
        gt06.clear()
        gt06.send(gt06_cmd("SZCS#FWDPORT=0"))
        wait(lambda: not fwd.connected(), 5, "FWDPORT=0 did not tear down the bridge")
        wait(lambda: b"SETOK" in gt06.got() and b"FWDPORT" in gt06.got(), 3, "no SETOK for disable")
        print("[E] SZCS#FWDPORT=0 disables + tears down bridge      PASS")
        gt06.clear()
        gt06.send(gt06_cmd(f"SZCS#FWDSERVER=127.0.0.1#FWDPORT={fwd_port}"))
        wait(lambda: fwd.connected(), 10, "re-enable did not reconnect the bridge")
        fwd.clear(); time.sleep(0.3)
        usb.sendall(b"re-enabled!"); wait(lambda: b"re-enabled!" in fwd.got(), 5, "no data after re-enable")
        print("[E] SZCS#FWDSERVER re-enables bridge + data flows    PASS")

        # E: an empty FWDPORT= must be REJECTED (not silently disable) and leave the bridge up.
        gt06.clear()
        gt06.send(gt06_cmd("SZCS#FWDPORT="))
        wait(lambda: b"SET ERROR" in gt06.got(), 3, "empty FWDPORT not rejected")
        time.sleep(0.5)
        assert fwd.connected(), "empty FWDPORT wrongly tore down the bridge"
        print("[E] empty FWDPORT= rejected, bridge stays up         PASS")

        # D (still): GT06 socket survived the whole test.
        assert gt06.connected() and proc.poll() is None, "GT06 socket or firmware died"
        print("[D] GT06 socket + firmware alive at end              PASS")
        print("\nALL PASS")
    finally:
        proc.terminate()
        try: proc.wait(timeout=3)
        except subprocess.TimeoutExpired: proc.kill()
        gt06.stop(); fwd.stop()

class UdpFwdServer(threading.Thread):
    """UDP forward server: recvfrom to collect device up-data (and learn the peer), sendto to reply.
    Mirrors how SupportProxy handles the device side over UDP (bind + recvfrom + connect-back)."""
    def __init__(self, port):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port)); self.sock.settimeout(0.3)
        self.rx = bytearray(); self.lock = threading.Lock(); self.peer = None; self._run = True
    def run(self):
        while self._run:
            try: d, a = self.sock.recvfrom(65536)
            except socket.timeout: continue
            except OSError: break
            self.peer = a
            with self.lock: self.rx += d
    def send(self, data):
        if self.peer: self.sock.sendto(data, self.peer)
    def got(self):
        with self.lock: return bytes(self.rx)
    def clear(self):
        with self.lock: self.rx = bytearray()
    def stop(self):
        self._run = False
        try: self.sock.close()
        except OSError: pass

def main_udp():
    """Same bridge, UDP uplink (SZCS#FWDUDP=1 via SITL_FWD_UDP). Loopback UDP is lossless+ordered for one
    flow, so byte-exact still holds. Down datagrams kept <=500 B (fw recv buffer is 1024)."""
    if not os.path.exists(SITL):
        raise SystemExit("build first: make -C gt06/Firmware/standalone sitl")
    gt06_port, fwd_port, usb_port = free_port(), free_port(), free_port()
    gt06 = DrainServer(gt06_port, "gt06"); gt06.start()
    fwd = UdpFwdServer(fwd_port); fwd.start()
    env = dict(os.environ); env["SITL_FWD_UDP"] = "1"
    proc = subprocess.Popen(
        [SITL, "--server", f"127.0.0.1:{gt06_port}", "--usb-listen", str(usb_port),
         "--fwd", f"127.0.0.1:{fwd_port}", "--teriid", "888800000000124", "--no-reexec"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    try:
        wait(lambda: gt06.accepted.is_set(), 8, "firmware never connected to GT06 (udp)")
        usb = socket.create_connection(("127.0.0.1", usb_port), timeout=5); usb.settimeout(0.2)
        time.sleep(0.3)
        # UA: USB->UDP throughput, byte-exact + in order (learns the peer on the first datagram).
        fwd.clear(); sent = bytearray(); seq = 0; t0 = time.time()
        while time.time() - t0 < 3.0:
            block = bytes((seq + i) & 0xFF for i in range(400)); seq += 400
            usb.sendall(block); sent += block; time.sleep(0.05)
        wait(lambda: len(fwd.got()) >= len(sent), 5, f"USB->UDP short: {len(fwd.got())}/{len(sent)}")
        assert fwd.got()[:len(sent)] == bytes(sent), "USB->UDP data corrupt/out-of-order"
        print(f"[UA] USB->UDP {len(sent)} B byte-exact               PASS")

        # UH: data arriving faster than one pass can drain must be CARRIED OVER, not discarded. The
        # per-pass cap is 4 x 256 B = 1024 B; the old "no buildup" policy discarded whatever was still
        # queued after the cap, which cost ~90% of the uplink on the aircraft where the ring had ample
        # room. Chunks are kept under 1 KB because the RX callback refuses a record larger than its
        # static buffer (real hardware chunks at 512 B; SITL would otherwise hand over one huge record).
        fwd.clear(); time.sleep(0.3)
        chunks = [bytes(((0x40 + c) + (i % 50)) & 0xFF for i in range(500)) for c in range(6)]
        burst = b"".join(chunks)
        for c in chunks:
            usb.sendall(c); time.sleep(0.002)                # 3000 B queued inside one 20 ms pass
        wait(lambda: burst in fwd.got(), 8,
             f"burst was TRUNCATED by the per-pass cap, not carried over ({len(fwd.got())} B seen)")
        print(f"[UH] {len(burst)} B burst carried across passes, byte-exact PASS")

        # UB: UDP->USB (small datagrams, under the 1024 fw recv buffer).
        down = bytes((i * 7) & 0xFF for i in range(2000))
        for k in range(0, len(down), 500): fwd.send(down[k:k + 500])
        got = bytearray(); t0 = time.time()
        while len(got) < len(down) and time.time() - t0 < 5:
            try: got += usb.recv(4096)
            except socket.timeout: pass
        assert bytes(got) == down, f"UDP->USB corrupt: {len(got)}/{len(down)}"
        print(f"[UB] UDP->USB {len(down)} B byte-exact               PASS")

        # UC: recovery — coverage loss makes send() fail while select still reports writable. The
        # bad_since timer must trip and RECREATE the UDP socket (cn increments), the firmware must not
        # wedge, and forwarding must resume once coverage returns.
        def fwd_cn():
            gt06.clear(); gt06.send(gt06_cmd("CXCS#FWDSTAT"))
            wait(lambda: b"READOK:FWDSTAT" in gt06.got(), 4, "no FWDSTAT reply")
            return int(re.search(rb" cn(\d+)", gt06.got()).group(1))
        cn0 = fwd_cn()
        os.kill(proc.pid, signal.SIGUSR1)                    # coverage DOWN
        t0 = time.time()
        while time.time() - t0 < 14:                         # stream through the outage (>FWD_TX_STALL_SEC)
            try: usb.sendall(b"\xAA" * 400)
            except OSError: pass
            time.sleep(0.05)
        assert proc.poll() is None, "firmware died during UDP coverage loss"
        os.kill(proc.pid, signal.SIGUSR1)                    # coverage BACK
        wait(lambda: gt06.connected(), 12, "GT06 did not recover after coverage loss")
        cn1 = fwd_cn()
        assert cn1 > cn0, f"UDP socket was NOT recreated during coverage loss (cn {cn0}->{cn1})"
        fwd.clear(); time.sleep(0.5)
        usb.sendall(b"udp-recovered!")
        wait(lambda: b"udp-recovered!" in fwd.got(), 12, "UDP forwarding did not resume after recovery")
        print(f"[UC] UDP fd recreated (cn {cn0}->{cn1}) + resumed        PASS")

        assert gt06.connected() and proc.poll() is None, "GT06 socket or firmware died (udp)"
        print("[UD] GT06 socket + firmware alive with UDP uplink    PASS")

        # UE: KEEPALIVE. With the USB side silent the uplink must NOT go quiet — the real proxy forgets
        # our source tuple after ~10 s and then cannot deliver the down direction at all (measured on HW
        # 2026-07-25: ~50 s of every 60 with no downlink). Require traffic inside the proxy's window, and
        # require the keepalive to be a no-op payload-wise (1 B, 0x00 = not a MAVLink start byte).
        fwd.clear(); t0 = time.time()
        wait(lambda: len(fwd.got()) > 0, 8, "no UDP keepalive while USB idle")
        ka = fwd.got()
        assert set(ka) == {0}, f"keepalive is not 0x00 padding: {ka[:16]!r}"
        assert time.time() - t0 < 10, "keepalive slower than the proxy's ~10 s idle timeout"
        gt06.clear(); gt06.send(gt06_cmd("CXCS#FWDSTAT"))
        wait(lambda: b"READOK:FWDSTAT" in gt06.got(), 4, "no FWDSTAT reply")
        kacount = int(re.search(rb" ka(\d+)", gt06.got()).group(1))
        assert kacount > 0, "ka counter did not move"
        print(f"[UE] keepalive while USB idle (ka{kacount}, 0x00)       PASS")

        # UF: a busy uplink must not pay for keepalives — they only fire on an idle interval.
        gt06.clear(); gt06.send(gt06_cmd("CXCS#FWDSTAT"))
        wait(lambda: b"READOK:FWDSTAT" in gt06.got(), 4, "no FWDSTAT reply")
        ka0 = int(re.search(rb" ka(\d+)", gt06.got()).group(1))
        t0 = time.time()
        while time.time() - t0 < 6:
            usb.sendall(b"\x55" * 200); time.sleep(0.1)
        gt06.clear(); gt06.send(gt06_cmd("CXCS#FWDSTAT"))
        wait(lambda: b"READOK:FWDSTAT" in gt06.got(), 4, "no FWDSTAT reply")
        m = re.search(rb" ka(\d+).*", gt06.got())
        ka1 = int(m.group(1))
        assert ka1 == ka0, f"keepalive fired on a busy uplink (ka {ka0}->{ka1})"
        print(f"[UF] no keepalives while uplink busy (ka{ka1})          PASS")

        # UG: the down direction must be counted (dn/dw), so a dead command path is diagnosable on HW.
        fwd.send(b"\xFD" * 300)
        time.sleep(0.5)
        gt06.clear(); gt06.send(gt06_cmd("CXCS#FWDSTAT"))
        wait(lambda: b"READOK:FWDSTAT" in gt06.got(), 4, "no FWDSTAT reply")
        st = gt06.got()
        dn = int(re.search(rb" dn(\d+)/(\d+)", st).group(2))
        dw = int(re.search(rb" dw(\d+)", st).group(1))
        assert dn >= 300 and dw >= 300, f"down counters did not move (dn{dn} dw{dw})"
        print(f"[UG] down-direction counters live (dn{dn} dw{dw})     PASS")

        # UI: a down datagram larger than the old 1024-byte recv buffer must not be truncated. recv()
        # discards the remainder of an oversized datagram, silently corrupting the stream.
        try:                                                 # drain leftovers from earlier down-tests
            while True:
                if not usb.recv(65536): break
        except socket.timeout: pass
        big = bytes((i * 13) & 0xFF for i in range(1800))
        fwd.send(big)
        got = bytearray(); t0 = time.time()
        while len(got) < len(big) and time.time() - t0 < 6:
            try: got += usb.recv(4096)
            except socket.timeout: pass
        assert bytes(got) == big, f"oversized down datagram truncated: {len(got)}/{len(big)}"
        print(f"[UI] down datagram {len(big)} B not truncated          PASS")

        # UJ: on a QUIET uplink the keepalive is the only traffic, so a failing keepalive must still drive
        # socket recovery. UC misses this because it streams data continuously throughout.
        wait(lambda: gt06.connected(), 12, "GT06 socket not up before the keepalive-recovery test")
        gt06.clear(); gt06.send(gt06_cmd("CXCS#FWDSTAT"))
        wait(lambda: b"READOK:FWDSTAT" in gt06.got(), 10, "no FWDSTAT reply")
        cn0 = int(re.search(rb" cn(\d+)", gt06.got()).group(1))
        os.kill(proc.pid, signal.SIGUSR1)                    # coverage DOWN, and send NOTHING over USB
        time.sleep(14)                                       # > FWD_TX_STALL_SEC with keepalives only
        os.kill(proc.pid, signal.SIGUSR1)                    # coverage BACK
        wait(lambda: gt06.connected(), 12, "GT06 socket did not recover")
        gt06.clear(); gt06.send(gt06_cmd("CXCS#FWDSTAT"))
        wait(lambda: b"READOK:FWDSTAT" in gt06.got(), 10, "no FWDSTAT reply")
        cn1 = int(re.search(rb" cn(\d+)", gt06.got()).group(1))
        assert cn1 > cn0, f"keepalive-only failure did NOT recreate the socket (cn {cn0}->{cn1})"
        print(f"[UJ] keepalive-only failure recovers socket (cn {cn0}->{cn1})  PASS")
        print("\nUDP ALL PASS")
    finally:
        proc.terminate()
        try: proc.wait(timeout=3)
        except subprocess.TimeoutExpired: proc.kill()
        gt06.stop(); fwd.stop()

if __name__ == "__main__":
    main()
    print()
    main_udp()
