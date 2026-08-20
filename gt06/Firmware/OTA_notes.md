# YCSJ OTA — test log

Running record of every vendor-FOTA (`YCSJ`) test on W07C / NT19D trackers: what was
tried, what happened, and what it proved. Mechanism and container format are in
`README.md`; this file is the empirical log.

**Headline finding (2026-08-20): a `YCSJ` OTA can complete its entire visible cycle and
silently leave the old firmware running. The variable was the URL the image was served
from, not the image.** See test 3 vs tests 1-2.

## How to read a result

The visible cycle is identical whether the update takes or not:

1. `YCSJ START OK` — the trigger was accepted (says nothing about the outcome)
2. reboot #1 — device restarts into FOTA mode
3. HTTP GET of the image
4. reboot #2 — apply
5. device returns

**Only step 5's banner tells you if it worked.** Read it back with `cxzt#`; the first
field is the firmware build. There is no success/failure report from the device, no
error SMS, and nothing in the server log that distinguishes the two outcomes.

Two traps when judging a run:

- Every reboot transits the ASR ROM (`2ecc:3004` "Arom Usb Boot Port") on USB. This is
  normal for *any* reboot and is **not** an OTA signal.
- The server's cached `firmware` field only refreshes when the device answers a `cxzt#`,
  so a stale value is not evidence the flash failed. Send `cxzt#` and read the reply.

## Constraints

- Vendor app allows **3 FOTA sessions/day**. (No such limit on our standalone firmware —
  `fota_run` has no counter there.)
  ⚠ **When the counter resets is CONTESTED and unverified.** `README.md` has long said "GMT
  reset"; a 2026-08-20 review disassembling V665 reports the counter compares month/day off an
  **RTC at UTC+8**, so it would roll at **16:00 UTC**, and that it increments when a YCSJ session
  *starts* rather than when one succeeds. Neither has been confirmed against hardware. Ration
  attempts on the pessimistic reading until someone checks.
- The filename must contain `B53` or the device rejects it before downloading.
- ⚠ The trailing field of `YCSJ,<url>,<n>#` is **NOT a port** — it is only the expected
  8-bit XOR of the whole `.bin`, parsed as **hex** into param `0x14A` (`SetVerify`). The
  **port is hardcoded to 80** in the handler. Vendor images
  are padded to XOR `0x80`, which is why `,80#` looks like a port and works. Corrected
  2026-08-20 by disassembly; `README.md` previously called it dual-purpose.
  **Consequence: a FOTA host must be reachable on port 80.** Any `:8088`-style host cannot
  work via the manual `YCSJ` command.
- Downloaded image total length must be **40960..245760 bytes** or the device rejects it
  (`"File Length error:%d"`). Our small standalone builds (~17 KB) are UNDER the floor —
  this is why the padded `_cpad` / `_fill` / `pad2ebbcc2` variants exist.
- Settings survive a flash: TERIID, `SERVIP`/`SERVPORT` and APN were all intact
  afterwards (verified test 3).

## Image under test

`W07_NT19D_MG133_B53_V665.bin` — used in all three tests below.

```
file size            : 105387 bytes
wrapper.len          : 0x00019BA3 (105379)   OK
wrapper.~crc32       : 0xFFE7C88C            OK
payload.lzma props   : 0x5D (lc=3,lp=0,pb=2), dict 0x00800000 (8 MB)
payload.uncomp size  : 204560 bytes          decompress OK
8-bit XOR            : 0x80                  (matches the ",80#" field)
build string         : NT19D_MG133_10F8G_B53_V665
md5                  : ce002b8e334c28e347a48c239944cd56
```

The copy served from our host and the copy at the vendor URL are **byte-identical**
(same md5, same 105387 bytes) — confirmed by fetching both and comparing. The vendor
serves it from IIS 8.5 (`Last-Modified: Mon, 09 Mar 2026 07:03:31 GMT`,
`ETag: "d8187cd692afdc1:0"`, `Content-Length: 105387`).

Not to be confused with `fw-src/customer_app_fw.bin`, which is a *different* build —
`NT19D_MG133_WSTR2_B53_V665`, 196608 bytes, XOR `0x35`. It is not FOTA-ready as-is.

---

## Test 1 — 2026-08-19, G333462, our host — NO CHANGE

| | |
|---|---|
| Unit | G333462, IMEI 866557081333462 |
| Firmware before | `W07_MG133_10F8G_B53_V6.68-TQ` MCU B2.5-0, `ID:4072407273` |
| Trigger | `YCSJ,http://103.230.158.49/W07_FW/W07_NT19D_MG133_B53_V665.bin,80#` by SMS |
| Result | **unchanged — still `V6.68-TQ`** |

```
15:00:31  YCSJ sent
15:00:43  "YCSJ START OK"
15:00:50  reboot #1
15:01:50  Apache: 35.156.18.25 GET /W07_FW/W07_NT19D_MG133_B53_V665.bin 200 105691
15:02:01  reboot #2 (apply)
15:02:04  back up
15:03:01  cxzt# -> W07_MG133_10F8G_B53_V6.68-TQ ... *BT:4078   [UNCHANGED]
```

This unit ran the H02/`-TQ` protocol variant, so at the time the result was ambiguous:
it could have been a variant/model rejection (image is `NT19D`, device is `W07`) or a
downgrade block (V6.68 -> V665). Test 2 was designed to separate those.

Note this unit never needed an OTA at all — `SZCS#PTL_SEL=2` converts H02 -> GT06 at
runtime. See the project note on that unit.

## Test 2 — 2026-08-20, G378517, our host — NO CHANGE

Deliberately removes the model variable: this unit matches the image exactly
(`NT19D` / `MG133` / `10F8G` / `B53`), is the GT06 variant, provisioned and healthy.
Only the version differs, and backwards: V667 -> V665.

| | |
|---|---|
| Unit | G378517, IMEI 866557081378517, ICCID 8944538532055006195, eid 8, idle |
| Firmware before | `NT19D_MG133_10F8G_B53_V667-GT06` MCU B2.5-0, `ID:***378517`, `103.230.158.49:7711`, `A:simbase`, `BT:3786` |
| Trigger | `YCSJ,http://103.230.158.49/W07_FW/W07_NT19D_MG133_B53_V665.bin,80#` over the manage API (TCP) |
| Result | **unchanged — still `V667`** |

```
08:29:06  YCSJ sent (manage API)
08:29:08  "YCSJ START OK"
08:29:15  reboot #1
08:30:14  Apache: 35.156.18.25 GET /W07_FW/W07_NT19D_MG133_B53_V665.bin 200 105691
08:30:24  reboot #2 (apply)
08:30:27  back up
08:32:24  reconnected to the server, heartbeating on eid 8
08:32:27  cxzt# -> NT19D_MG133_10F8G_B53_V667-GT06 ... *BT:3820   [UNCHANGED]
```

At this point the model-mismatch hypothesis was dead and a downgrade block looked like
the strong explanation for both failures. Test 3 killed that too.

## Test 3 — 2026-08-20, G378517, VENDOR host — WORKED

Same unit, same image bytes, **only the URL changed**.

| | |
|---|---|
| Trigger | `YCSJ,http://139.9.60.174/update/W07_NT19D_MG133_B53_V665.bin,80#` over the manage API |
| Result | **`NT19D_MG133_10F8G_B53_V665` — applied** |

```
08:34:46  YCSJ sent (manage API)
08:34:48  "YCSJ START OK"
08:34:56  reboot #1
          ~3 min downloading (vs ~1 min from our host; that IP is far away)
08:36:13  server: "No traffic from G378517 for 79s - disconnecting"  (busy downloading)
08:37:50  reboot #2 (apply)
08:39:01  server: G378517 firmware: NT19D_MG133_10F8G_B53_V665
08:39:33  cxzt# -> NT19D_MG133_10F8G_B53_V665-GT06 ... *ID:***378517
          *103.230.158.49:7711 *A:simbase *BT:3870   [CHANGED — and settings intact]
```

### What test 3 rules out

- **Not a downgrade block.** V667 -> V665 is backwards and it applied.
- **Not a model/variant mismatch.** Test 2's unit matched the image exactly and failed.
- **Not the image.** Byte-identical file, XOR `0x80`, CRC OK, `B53` in the filename.
- Therefore test 1's failure is also explained by the URL, and **nothing was learned
  about `-TQ` FOTA from it.**

---

## ROOT CAUSE — CONFIRMED BY DISASSEMBLY AND FIXED (2026-08-20)

`/etc/apache2/conf-available/w07_fw.conf` on wstracker.org contains, added
**2026-07-23 20:25**:

```apache
# The VENDOR OTA downloader sends a Range header but only handles a plain 200
# full-file response (a 206 Partial Content makes it stop after the first chunk
# and fail the XOR verify). Strip Range so apache always returns the whole file.
RequestHeader unset Range
Header set Accept-Ranges none
```

**The vendor's own server does the opposite and works**: it advertises
`Accept-Ranges: bytes` and answers `Range: bytes=0-1023` with a real
`206 Partial Content` + `Content-Range`.

### The exact mechanism (from disassembly of the V665 image)

Request builder at `0x7E1B5FF4` emits `GET <path> HTTP/1.1`, `Connection: keep-alive`,
`Host: <host>:%d`, and **`Range:bytes=%d-%d`** — the downloader always fetches in ranges
(~1 KB at a time).

Response parser at `0x7E1B62C2`:

1. matches `HTTP/1.1 ` then accepts status `200` **or** `206` (so a 200 is NOT rejected on
   status — the previous config comment was wrong about this);
2. then requires the literal `Content-Range: bytes ` in the header — **absent => "File Info
   error 3" and the transfer is abandoned**;
3. parses the total after `/` and requires 40960..245760 bytes.

Completion at `0x7E1B6210` XORs every received byte and compares with param `0x14A`
(`"Fota DW Complete CalcVerify:%d,SetVerify:%d"`), then writes the app package.

So stripping `Range` gave a plain `200` with **no `Content-Range`**: the device read the
whole body off the socket (hence a complete-looking 200 in our access log) but failed the
header parse and never reached verify/flash. That is the whole bug.

The timing fits: our own WSTR builds loaded from our host on **2026-07-09**, i.e.
*before* this directive was added on **2026-07-23**. Our host worked until that change.

⚠ The `.bak` next to it shows the original working config had neither directive.

### Header comparison

| header | vendor IIS | ours `/W07_FW/` (broken) | ours `/update/` (new) |
|---|---|---|---|
| `Accept-Ranges` | `bytes` | `none` | `bytes` |
| Range request | `206 Partial Content` | `200` (Range stripped) | `206 Partial Content` |
| `Content-Type` | `application/octet-stream` | same | same |
| `Last-Modified` | `Mon, 09 Mar 2026 07:03:31 GMT` | file mtime | matched to vendor |
| `X-Powered-By` | `ASP.NET` | absent | `ASP.NET` |
| `Server` | `Microsoft-IIS/8.5` | `Apache/2.4.58 (Ubuntu)` | Apache (cannot spoof via mod_headers) |
| `ETag` | `"d8187cd692afdc1:0"` | apache-generated | absent (mod_headers cannot set it) |
| path case | `/update/` | `/W07_FW/` | `/update/` |

### New vendor-alike path

`/etc/apache2/conf-available/w07_update.conf` -> `Alias /update /srv/w07_update`,
serving a byte-identical copy (md5 `ce002b8e334c28e347a48c239944cd56`) at
`http://103.230.158.49/update/W07_NT19D_MG133_B53_V665.bin`. Ranges are deliberately
NOT stripped. `/W07_FW/` is left untouched as the control. Previous config backed up
on the server before the change.

## OPEN: why does our host fail?

(superseded by the section above, kept for the reasoning trail.) This blocks the custom-firmware workflow, which serves everything from `/srv/w07_fw/` at
`http://103.230.158.49/W07_FW/`.

Two facts constrain any theory:

1. **The download succeeds from our host.** Apache logs `200` with the full body, from
   the device's own IP. So the failure is *after* the transfer, not a fetch problem.
2. **Our host has worked before.** Our own WSTR builds were loaded via `YCSJ` from our
   infrastructure (G375356, 2026-07-09), so a blanket host whitelist cannot be the whole
   story.

Untested candidates, roughly in order of suspicion:

- **Path** — ours is uppercase `/W07_FW/`, the vendor's is lowercase `/update/`. A naive
  client that lowercases the URL would 404 on a case-sensitive server, though our access
  log shows the request arriving with the original case and returning 200.
- **Response headers** — IIS sends `Accept-Ranges`, `ETag`, `X-Powered-By`; a fixed-size
  header buffer or a parser expecting particular fields could behave differently.
- **URL length** (ours 56 chars, vendor's 54) or HTTP/1.0 vs 1.1 handling.

### Next test — bisect host vs path

Serve our identical copy at a **lowercase `/update/` path on our own server** and retry.
That separates "it must be the vendor's host" from "it must be that path shape".

Requirements for a valid run:

- a unit **not already on the target version** — a same-version push may simply be
  skipped, which would confound the result;
- respect the 3/day GMT limit (G378517 used 2 of 3 on 2026-08-20);
- a V667 image would let us push G378517 back and reuse it as the test mule — we do not
  currently hold one. Worth requesting from the vendor alongside the V665 link.

---

## Test 4 — 2026-08-20, G380521, OUR host with ranges honoured — WORKED

The fix, proven end to end from our own infrastructure.

| | |
|---|---|
| Unit | G380521, IMEI 866557081380521, eid 8, idle |
| Firmware before | `NT19D_MG133_10F8G_B53_V667-GT06`, `ID:***380521`, `BT:3312` (2%, flat) |
| Trigger | `YCSJ,http://103.230.158.49/update/W07_NT19D_MG133_B53_V665.bin,80#` over the manage API |
| Result | **`NT19D_MG133_10F8G_B53_V665` — applied** |

```
08:51:46  YCSJ sent (manage API)
08:51:48  "YCSJ START OK"
08:53:04  server: "No traffic from G380521 for 76s - disconnecting"  (downloading)
08:53:19  apache: GET /update/... 206 1345      <-- FIRST 206: it really does use Range
08:53:38  26 x 206
08:54:04  63 x 206
08:54:55  101 x 206 (transfer essentially complete)
08:57:30  back online, heartbeating on eid 8
08:57:37  cxzt# -> NT19D_MG133_10F8G_B53_V665-GT06 ... *ID:***380521
          *103.230.158.49:7711 *A:simbase *BT:3374   [CHANGED — settings intact]
```

~108 ranged requests of ~1 KB. Note this succeeded on a **3.31 V / 2%** cell (it read 3.374 V
afterwards). That is a single data point that this attempt completed — it does NOT measure OTA
current or energy (the post-reboot rebound cannot), so do not read it as "flashing is cheap".
Treat a nearly-empty cell as a real risk; this one was luck.

### Answering "is the vendor host special?" — NO

A full search of the decompressed image for a host/IP/subnet restriction on the download
path found **none**. The manual `YCSJ` handler at `0x7E1CD2C8` validates only: exactly two
commas, `YCSJ` prefix, URL length > 19, contains `http://`, host -> param `0x97` (20 B max),
path -> param `0x14C` (60 B max), the whole command contains `B53`, and field 2 hex-parsed
into `0x14A`. No host comparison anywhere.

`139.9.60.174` *is* hardcoded, but only for unrelated things: a **management socket**
(`139.9.60.174:7710`, packed `8B 09 3C AE` at `0x7E1BB488`) used by the separate
server-*pushed* upgrade flow, and a predicate testing whether the configured tracking
server (param `0x2C6`) is one of `.39gps.com` / `.008gps.com` / `139.9.60.174`. A third
hardcoded IP `121.41.40.95:2621` is the AGPS server. None are consulted by the FOTA
download. Subnet-mask-looking byte runs in rodata are coincidence.

## Fix applied to the server

`/W07_FW/` had the two offending directives removed (2026-08-20) and now returns
`206 Partial Content` with `Content-Range` like the vendor. Both paths are now good:

- `http://103.230.158.49/W07_FW/` — the standing FOTA host, fixed
- `http://103.230.158.49/update/` — vendor-alike A/B path (`/srv/w07_update`)

Backups of the previous configs are kept on the server. **Never re-add `RequestHeader unset
Range` / `Header set Accept-Ranges none`** — that is precisely what breaks OTA.

---

## Test 5 — 2026-08-20, G380521, OUR standalone firmware from OUR host — WORKED

First push of a **custom** build with the real constraints known. Vendor app -> our firmware.

| | |
|---|---|
| Unit | G380521, IMEI 866557081380521, eid 8 |
| Firmware before | `NT19D_MG133_10F8G_B53_V665-GT06` (from test 4), `BT:3410` |
| Source build | `gt06/Firmware/standalone/standalone_fw.bin`, **17844 B — UNDER the 40960 floor** |
| Served as | `W07_NT19D_MG133_B53_V669.bin` (vendor filename form, contains `B53`) |
| Trigger | `YCSJ,http://103.230.158.49/update/W07_NT19D_MG133_B53_V669.bin,80#` |
| Result | **`WSTR-STD1_890ac16-GT06 MCU:R5` — applied** |

### How the image was prepared

Padded with **zero bytes appended AFTER the container**, 17844 -> **65536** B:

- zeros do not change the 8-bit XOR, so the build's existing pad byte still lands the whole
  file on **XOR `0x80`** and `,80#` stays correct;
- `clen` and the LZMA payload are bit-identical to the build (still unpacks to 25788 B), so
  padding after the container cannot alter firmware behaviour — unlike re-packing a padded
  raw image (what `_cpad` / `_padded` / `_fill` did on 2026-07-24);
- 65536 sits comfortably inside the 40960..245760 window.

**Unpadded, this build would have been rejected outright** with `"File Length error"` — worth
remembering, because that failure looks nothing like a size problem from the outside.

```
09:02:52  YCSJ sent (manage API)
09:03:40  server: Disconnected (downloading)
09:03:42  apache: first 206
09:04:39  apache: last 206  — 66 ranged requests total
09:06:23  cxzt# -> WSTR-STD1_890ac16-GT06 MCU:R5*ID:***380521
          *103.230.158.49:7711 *A:simbase *BT:3426 *CHG:1   [CHANGED]
```

### What test 5 additionally establishes

- **The device downloads the FULL `total`, padding included** — 66 x ~1 KB requests covers all
  65536 B, not just the 17843 B container. So any padding is transferred and XOR-verified;
  it must not break the XOR (zeros are the safe choice).
- **Vendor app -> our standalone firmware over OTA works**, and **settings survive the
  transition**: TERIID (`ID:***380521`), `SERVIP:PORT` and APN all carried across, so the
  unit came back authenticated without intervention.
- ⚠ **The reverse direction (standalone -> vendor) is NOT proven on hardware.** Our firmware
  implements `YCSJ` and it has been exercised in SITL, so rollback is plausible and is the
  intended recovery path for units with no USB access — but no live reverse OTA has been done.
  Until one has, do not treat it as a guaranteed way back for an inaccessible unit.
- Our firmware's banner uses a different shape: `MCU:R5` and an extra `CHG:` field.

## Recipe — pushing a custom build over YCSJ

1. Build; check the `.bin` size. If **< 40960**, append zero bytes until it is (65536 is a
   good target). Do NOT repack the raw — pad after the container.
2. Confirm the whole file XORs to the value you will put in the command (`0x80` for our
   builds, since the pack step already adds a pad byte for that).
3. Name it `W07_MG133_WSTR_B53_<git tag>.bin` — our convention, and the `B53` rule
   is satisfied by it. (Do not bother mimicking a vendor version string; test 6 confirms
   the name only has to contain `B53`.)
4. Put it where it is served on **port 80** with **Range support** (`206` + `Content-Range`).
5. `YCSJ,http://<host>/<path>/<name>.bin,80#` — remember the `80` is the XOR, not the port.
6. Verify with `cxzt#`. Nothing else reports success or failure.

---

## Test 6 — 2026-08-20, G407273, standalone firmware onto a W07 unit via the repaired `/W07_FW/` — WORKED

Three things at once: the **W07** hardware variant, the **repaired `/W07_FW/` path** (first OTA
through it since the Range fix), and a **sensible filename** instead of the fake vendor version
used in test 5.

| | |
|---|---|
| Unit | G407273 — IMEI 866557081333462, login id `000004072407273`, eid 8, provisioned |
| Firmware before | `W07_MG133_10F8G_B53_V6.68-GT06` (vendor, **W07** variant — test 5 was NT19D) |
| Served as | `W07_MG133_WSTR_B53_890ac16.bin` — our own `W07_MG133_WSTR_B53_<git tag>` convention |
| From | `http://103.230.158.49/W07_FW/...` (the path fixed earlier today) |
| Trigger | `YCSJ,http://103.230.158.49/W07_FW/W07_MG133_WSTR_B53_890ac16.bin,80#` |
| Result | **`WSTR-STD1_890ac16-GT06 MCU:R5` — applied** |

Identical artefact to test 5 (65536 B, XOR `0x80`, payload untouched), renamed. The `V669` name
used in test 5 was only ever to mimic the vendor form — **use the `WSTR` + git-tag convention**,
it still satisfies the `B53` rule and says what the build actually is.

```
09:17:51  YCSJ sent (manage API)
09:18:55  server: Disconnected (downloading)
09:19:01  apache: first 206
09:20:03  apache: last 206  — 66 ranged requests, same as test 5
09:21:15  Login: id ***407273 -> G407273 (eid=8, auth)      <-- IDENTITY + TERIID INTACT
09:21:16  cxzt# -> WSTR-STD1_890ac16-GT06 MCU:R5*ID:***407273
          *103.230.158.49:7711 *A:simbase *BT:4128 *CHG:1
```

### What test 6 additionally establishes

- **The same image runs on both hardware variants** — NT19D (test 5) and W07 (here) — despite
  the vendor shipping separate `NT19D_...` and `W07_...` builds.
- **`/W07_FW/` is confirmed working** after removing the Range-stripping directives. Both our
  paths are now proven by a real OTA.
- **Credentials and config survive vendor -> standalone.** The unit came back `auth` (not
  `legacy_raw`, not a new unregistered device) on eid 8, with `SERVIP:PORT` and APN intact, so
  the flash does not disturb provisioning.
  ⚠ **Precisely what survived is the TERIID, not necessarily the raw id.** The server had
  pushed `SETOK: TERIID=***407273` at 09:14:26, so from then on the device logs in with that
  **TERIID** — whose HMAC prefix means it CANNOT be `000004072407273`. The logs redact all but
  the last 6, so they show a valid TERIID ending `407273` surviving and authenticating; they do
  NOT show the post-provisioning login id still being the raw zero-prefixed id. (Correction
  from review — the first write-up of this test conflated the two.) The raw converted id still
  matters for *initial* onboarding, which is what the `gt06_parse_login` leading-zero fix
  addresses.
