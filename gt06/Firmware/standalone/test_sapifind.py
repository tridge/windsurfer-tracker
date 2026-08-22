#!/usr/bin/env python3
"""SITL test for CXCS#SAPIFIND — the sAPI symbol-resolver oracle.

We can only call resident functions the vendor app already resolves, because our slot table is built
from the 458 compile-time hashes we extracted from its init1. Anything the vendor app never calls —
sAPI_GnssPowerStatusGet, for instance — is unreachable, since resolving it needs the 32-bit name hash
and we do not know how the vendor computes one.

Reversing that from the binaries may be impossible: both sides use PRECOMPUTED hashes, so the
name->hash function is probably a build-time artifact present in neither image. SAPIFIND sidesteps
that. The resident resolver is a lookup — hash in, pointer out, 0 for unknown — so we can guess the
algorithm and let the device adjudicate. A non-zero answer for a name we chose is essentially proof.

It is safe by construction: sapi_table_fill() already calls this same resolver 458 times every boot,
and SAPIFIND only looks symbols up, never calls what comes back.

The SITL mock implements ONE scheme (djb2 over the name with "sAPI_" stripped) without telling the
firmware which. The test passes only if SAPIFIND actually discovers it.
"""

import os
import re
import socket
import subprocess
import time

from test_loc_batch import finish_sitl

HERE = os.path.dirname(os.path.abspath(__file__))
SITL = os.path.abspath(os.environ.get("SITL_BIN") or os.path.join(HERE, "sitl"))

# What the mock resolver implements. The firmware must find this without being told.
XFORMS = 4          # must match SAPI_HASH_XFORMS
DJB2_FUNC = 0       # must match the case order in sapi_name_hash()
STRIP_PREFIX = 1
EXPECTED_ALGO = DJB2_FUNC * XFORMS + STRIP_PREFIX


def gt06_cmd(command, serial):
    text = command.encode()
    length = 10 + len(text)
    body = (bytes([0x80, 4 + len(text)]) + b"\x00\x00\x00\x00" + text
            + serial.to_bytes(2, "big") + b"\x00\x00")
    return b"\x78\x78" + bytes([length]) + body + b"\x0d\x0a"


def sapifind(conn, name, serial, timeout=20):
    """Send CXCS#SAPIFIND=<name> and return its reply text."""
    conn.sendall(gt06_cmd(f"CXCS#SAPIFIND={name}", serial))
    buf = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        conn.settimeout(1.0)
        try:
            chunk = conn.recv(4096)
        except socket.timeout:
            continue
        if not chunk:
            break
        buf += chunk
        if b"READOK:SAPIFIND" in buf:
            text = buf[buf.index(b"READOK:SAPIFIND"):].decode(errors="replace")
            # the reply ends at the frame trailer; keep the printable head
            return text.split("\r")[0].split("\x0d")[0][:200]
    raise AssertionError(f"no SAPIFIND reply for {name}\n{bytes(buf)[-300:]!r}")


def main():
    if not os.path.exists(SITL):
        raise SystemExit("build first: make sitl")

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    proc = subprocess.Popen(
        [SITL, "--server", f"127.0.0.1:{port}", "--teriid", "888800000000170", "--no-reexec"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(os.environ))

    conn = None
    try:
        listener.settimeout(8)
        conn, _ = listener.accept()
        time.sleep(2)

        # ---- a name the resolver knows: the oracle must identify the scheme ----
        text = sapifind(conn, "sAPI_TaskCreate", 1)
        hits = re.findall(r"a(\d+)=([0-9a-f]{8})", text)
        assert hits, f"no algorithm matched a known symbol: {text}"
        algos = [int(a) for a, _ in hits]
        assert EXPECTED_ALGO in algos, (
            f"did not discover the resolver's scheme (expected algo {EXPECTED_ALGO}, got {algos}): {text}")
        ptr = dict((int(a), p) for a, p in hits)[EXPECTED_ALGO]
        assert ptr != "00000000", f"reported a null pointer as a hit: {text}"
        print(f"discovered scheme: algo {EXPECTED_ALGO} -> 0x{ptr} PASS")

        # ---- the whole point: a symbol the vendor app never resolves ----
        text = sapifind(conn, "sAPI_GnssPowerStatusGet", 2)
        hits = re.findall(r"a(\d+)=([0-9a-f]{8})", text)
        assert hits, f"could not resolve a symbol outside our slot table: {text}"
        assert EXPECTED_ALGO in [int(a) for a, _ in hits], f"wrong algorithm matched: {text}"
        print(f"resolved a symbol absent from our 458-slot table PASS  ({text[:80]}...)")

        # ---- an invented name must resolve to nothing, or the oracle proves nothing ----
        text = sapifind(conn, "sAPI_NoSuchSymbolHere", 3)
        assert "none" in text, f"a nonexistent symbol reported a hit — false positives make this useless: {text}"
        print("nonexistent symbol -> none PASS")

        # ---- a missing argument must be rejected, not crash ----
        conn.sendall(gt06_cmd("CXCS#SAPIFIND", 4))
        time.sleep(2)
    finally:
        if conn is not None:
            conn.close()
        listener.close()
        finish_sitl(proc)

    print("SAPIFIND identifies the resolver's hash scheme from candidates")


if __name__ == "__main__":
    main()
