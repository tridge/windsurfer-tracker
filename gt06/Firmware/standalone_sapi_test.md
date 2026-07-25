# Experiment: does the loader wire up the sAPI table for a fully-custom `customer_app`?

## ★★★★★★ RESULT 12 (2026-07-18): STAGE 3c — FULL from-scratch C tracker WORKS (boots, connects, logs in, parses GNSS). Live on G375356.
Flashed the integrated build (main.c: socket+login+reconnect + GNSS+URC + RMC parser). Boots STABLE. if04:
`S3c:valid=0 lat=0 lon=0` (RMC parser live; void = INDOORS no lock). **Server confirms a FRESH G375356 login
(last_seen 69s)** → the socket connect+login works in the integrated build. The reconnect loop was the fix:
3c connects once/loop and RETRIES until the PDP is up (a plain boot-time connect failed because after a
power-cycle the modem re-attaches over ~15-30s; CGATT/CGACT come up late). RMC->deg parser host-validated
(35.33148 S / 149.13182 E exact; void->0; N/W ok). datetime=0 (server receipt time), sats=8 placeholder,
speed/course 0 for now.

**A complete tracker from ZERO vendor code:** boot → resolver-replay table → cellular socket + GT06 login →
GNSS power + "URC QUEUE" NMEA → RMC parse. The ONLY untested link is valid-fix → build_loc_data → LOC → server
plots, which needs SKY VIEW (bench is indoors → RMC void). Everything up to the fix is proven.

**★ FLASH/REBOOT — the reliable software flow (finally worked out):**
- **CFUN opens arom ON DEMAND** (my earlier "AT+CFUN doesn't arom" was WRONG — it does, but enumerates as
  /dev/ttyACMn where n VARIES (0 or 1); asrflash + my catchers watched only ACM0). Send AT+CFUN=1,1 to if05,
  busy-watch ANY /dev/ttyACM* (glob, STRIP ls color codes), fire `sudo adownload -p ACMn -q zip` -> flashes
  reliably (adownload SIGABRTs after "all finished" — normal). **No power-cycle needed to FLASH.**
- **Reboot-into-image is the weak link:** the reboot DA (`adownload -p ACMn -r -q reboot.zip`) is FLAKY —
  sometimes "scheduled reboot command" lands (boots), sometimes rc=255 no-schedule -> device stuck cycling
  arom / off-bus, and the host xHCI bus gets unhappy (error -110/-62). RELIABLE FLOW = CFUN-flash (software)
  + ONE physical power-cycle to boot; SKIP the reboot DA. Recover a stuck/off-bus device by replug (don't
  reset the host controller — disrupts everything).

**NEXT:** put G375356 by a window/outdoors for a GPS lock -> confirm valid RMC -> real LOC on the server map.
Then: heartbeat frames, TERIID login, 0x80 command handling, blind buffer, OTA. Also refine: sats from GGA,
speed/course from RMC, real UTC datetime.

## ★★★★★ RESULT 11 (2026-07-18): RESOLVER-REPLAY VALIDATED + GNSS NMEA PIPELINE WORKS. Live on G375356.
Flashed the resolver-replay build (sapi_pairs.h, 456 pairs). Everything runs → resolver-replay fills the
COMPLETE table (incl. the below-0x7e1e2000 slots the bake missed). if04:
```
S3b:pwr=0 cfg=0 frq=0xFFFFFFFF(-1)   (GNSS power/cfg OK; set_freq errors but is NOT needed)
S3b:mqcreate=0  attach=0  qid=0x7e2c4bb0  recv=0    <- attach (slot 0x7e1e1f88) + MsgQRecv (0x7e1e1f18) WORK
S3b:txt=$GNRMC,062132.76,V,...  $GNGGA... $GNGSA... $GNGSV...   <- LIVE NMEA from the GNSS URC queue
```
**Proven:** (1) resolver-replay table-fill works end-to-end (whole app runs off it); (2) the URC-attach step
(0x7e1e1f88) + corrected MsgQRecv slot (0x7e1e1f18) — both below 0x7e1e2000 — work only because replay filled
them; (3) GNSS acquisition pipeline complete: power via sAPI → subscribe "URC QUEUE" → attach → MsgQRecv →
NMEA RMC/GGA/GSA/GSV text. RMC='V' (void, no fix) because the bench is INDOORS — GNSS runs + gives UTC time
(06:21:32 date 180726), just no lock without sky. Physical constraint, not firmware.

**Flash lessons (this session):** AT+CFUN reboot does NOT open arom with our standalone running (no vendor AT
handler) → needs a PHYSICAL power-cycle. Arom enumerates briefly (~0.28s) as /dev/ttyACMn where n cycles
(was ttyACM1, not 0) → watch RAW /dev/ttyACM* (by-id symlink too slow for udev), fire `sudo adownload -p
ACMn -q zip` instantly; **strip `ls` color codes from the node path** (that broke the -p arg). Reboot from
arom: adownload SIGABRTs but the reboot command lands first (device boots). asrflash.py hardcodes ACM0 — use
raw adownload with the real ACMn. Committed asrflash.py UnicodeDecodeError fix.

**STAGE 3b remaining (firmware done, just needs sky + wiring):** parse RMC → decimal degrees + UTC →
build_loc_data (parser design in the RE) → send LOC (STAGE 3a path) with a REAL fix. Then heartbeat, TERIID,
0x80 cmds, blind buffer, OTA.

## ⚠ RESULT 10 (2026-07-18): STAGE 3b — GNSS power WORKS via sAPI, but the table BAKE IS INCOMPLETE (missed 61 slots below 0x7e1e2000). Decision point.
Started real-GNSS work. Findings:
- **GNSS power via sAPI slot WORKS (proven):** standalone called gnss_power(1) slot idx107 (0x7e1e21ac) →
  modem `AT+CGNSSPWR?` flipped 0→1 (`+CGNSSPWR: 1,0,1`). gnss_cfg(1,1) idx262 ret 0. gnss_set_freq(1) idx209
  ret **-1** (needs correct arg). (AT+CGNSSINFO ERRORs — this tracker's modem has a customized AT set; the
  sAPI URC path is the real interface, not AT.)
- **GNSS fix delivery = NMEA RMC over the resident "URC QUEUE" msg bus** (RE agent): MsgQCreate idx372
  (0x7e1e25d0), **attach idx? via wrapper 0x7e1b1b4c**, MsgQRecv idx47 (0x7e1e20bc). We created the queue OK
  (qid 0x7e2c2370) but MsgQRecv returns 0x0E (empty) — because the **URC-ATTACH step was missing AND its
  slot is below our baked range**.
- **⚠ ROOT CAUSE — the table bake is INCOMPLETE.** The real sAPI table spans **0x7e1e1e8c .. 0x7e1e2892**;
  our capture/bake (RESULT 2) only covered **0x7e1e2000..0x7e1e2864**, MISSING **61 slots below 0x7e1e2000**
  — including the URC-attach slot **0x7e1e1f88**. STAGE 1/2/3a worked only because USB/socket/GNSS-power
  slots all sit ABOVE 0x7e1e2000. GNSS NMEA is the first feature needing a below-range slot.
- **FIX (two options):** (a) re-capture the FULL table range from the vendor fw (reflash golden, widen the
  SAPIDMP dump to 0x7e1e1e00.. , re-bake); OR (b) **switch to RESOLVER-REPLAY** (RESULT 4/5): replay init1's
  ~282 (hash,slot) pairs via the stashed r0 resolver — fills the COMPLETE table (incl. below 0x7e1e2000) with
  no capture. (b) is the robust long-term fix (the table values are runtime-resolved, NOT in the golden dump,
  so a wider bake still needs a runtime re-capture). Recommend (b).
Also need: correct gnss_set_freq arg; the URC-attach call (resolve wrapper 0x7e1b1b4c's slot); outdoor fix to
validate NMEA (bench is indoors). STAGE 3a (LOC→server) already proven; this is the GNSS half of 3b.

## ★★★★★ RESULT 9 (2026-07-18): STAGE 3a — the standalone sends a real GT06 LOC the server DECODES to the exact position. PROVEN.
The from-scratch standalone built a GT06 LOC frame IN C, streamed it over the logged-in socket, and the live
server decoded it byte-perfect. `tracker.log` on wstracker:
```
2026-07-18 14:12:5x [GT06] G375356 IDLE LOC -35.33145,149.13182 sats=8   (once/sec)
```
Exactly the lat/lon/sats the probe sent. Full tracker uplink now works from zero vendor code.

**LOC builder in C** (standalone/main.c, validated byte-identical to server/gt06_frames.py on the host):
crc_itu (0x8408 reflected, init/final 0xFFFF), build_loc_data (18B: datetime=0 → server uses receipt time;
gps_info 0xF0|sats; lat/lon = deg*1,800,000 magnitudes; course_status bit12=valid, bit10=North, bit11=West),
gt06_frame (78 78 | len | proto | data | serial | crc-over[len..serial] | 0d 0a). Sent proto 0x12, serial
incrementing. Bench coords -35.33145/149.13182 as raw 63596609/268437276 (South/East).

**Server nuance (NOT a firmware issue):** device state = eid 8, **idle:true** → by the idle-privacy rule the
server LOGS the LOC but does NOT record lat/lon to current_positions (idle trackers never plot). So the
position is received + parsed correctly (proven by the log line) but not map-plotted; that needs the operator
to set G375356 active (a production change — not done). The stale raw_cxzt/settings in gt06_state are from
the vendor firmware (our standalone doesn't send cxzt). This is the IDENTICAL server result the injected
fw-src loc_proto.c produced earlier — our standalone reproduces it with no vendor app.

**Proven sAPI surface now: USB + dispatch table + cellular sockets + GT06 login + GT06 LOC.** STAGE 3b (in
progress): real GNSS fix (RE agent running) → feed build_loc_data real coords + real time; then heartbeat
frames, TERIID login, 0x80 command handling, blind buffer, OTA.

## ★★★★★ RESULT 8 (2026-07-18): STAGE 2 — the standalone TALKS TO THE LIVE GT06 SERVER (full bidirectional). PROVEN.
The from-scratch standalone opened a real TCP socket to the production server and completed a GT06 login
handshake — all via resident sAPI slots, zero vendor app, over the resident-activated PDP.

**Data path is FREE (resident).** Queried the modem via if05 AT while our appless standalone ran: CGATT=1,
CGACT=1,1, IP 10.8.209.28, APN "simbase", LTE registered — the resident firmware brings up cellular+PDP
with NO app code. So NO init5/PPP-dial replay was needed; the socket layer is bound to the live PDP.

**Socket API (RE'd, all live in the baked table):** lwIP BSD sockets, resident at 0x80140000, via slots:
socket idx214 (0x7e1e2358→0x8014613f) `socket(2,1,0)`; connect idx153 (0x7e1e2264→0x80145403)
`connect(fd,&sockaddr_in,24)`; send idx168 (0x7e1e22a0→0x80145eb3); recv idx111 (0x7e1e21bc→0x80145b11);
close idx185 (0x7e1e22e4→0x80145365). sockaddr_in = 16B BSD {sin_len=16, fam=2, port=htons BE, addr BE}.
Connect by IP (skip DNS). ⚠ prior notes' `fn_connect 0x7e1d792c` + handle `*0x7e1e1968` were a libc/stdio
red herring — ignore them.

**Live result on G375356** (probe `standalone/main.c`, server wstracker.org=103.230.158.49:7711, login for
IMEI 866557081375356 built by server/gt06_frames.py). Reported over if04 VCOM every heartbeat:
```
S2:send  = 0x12        (18-byte GT06 login sent)
S2:rxlen = 0x0A  rxhead= 0x78780501   <- 10-byte GT06 LOGIN-ACK (proto 0x01 response) from the REAL server
S2:rxlen = 0x21  rxhead= 0x78781C80   <- server then PUSHED a 0x80 downlink command (28B) to the device
```
The server ACK proves connect() completed a real handshake (NOT optimistic) and send() delivered; the 0x80
command proves the server recognised the device and is managing it — full bidirectional GT06. NB the
"simbase" APN egresses all SIMs via a shared gateway (35.156.18.25), so the device shares the fleet's
server-visible IP; the login-ACK arriving back on our socket is the unambiguous proof, no server access
needed. (Flash on the RELIABLE USB port via `asrflash flash-app --reboot` — one-shot, no catch-loop.)

**STAGE 2 DONE.** A C-only firmware can now boot, fill the table, and do bidirectional GT06 over cellular
with zero vendor code. Remaining for a real firmware: GNSS fix read (slots RE'd in fw-src asr_api.h) → build
real LOC/heartbeat frames (builders RE'd) → the login/TERIID + command-response + blind-buffer app logic →
OTA. All app logic over a now fully-proven sAPI surface (USB, table, sockets).

## ★★★★★ RESULT 7 (2026-07-18): A FROM-SCRATCH C-ONLY IMAGE BOOTS AND RUNS ON THE DEVICE. End-to-end PROVEN live on G375356.
Built + flashed the first fully-standalone `customer_app` (our C only, zero vendor app; `standalone/`) and
it works. STAGE 0+1 image: 32-byte descriptor → `_start` (stash r0, zero bss) → fill sAPI table (bake) →
emit two markers on USB-VCOM (if04): "DIRECT" via the resident write VA `0x7e6916b9`, "SLOT" via the baked
dispatch slot `0x7e1e2580`. After flash + software-reboot the device came up `1e0e:9011` (if02–if05, resident
USB — our app can't compose USB, so that's resident) and **if04 streamed both markers in a steady heartbeat
(237× DIRECT, 238× SLOT over 6s), device stable, no crash-to-arom, no churn.**

**What this proves (all live, not inferred):**
1. The loader accepts our from-scratch 32-byte descriptor and jumps to our `_start` (RESULT 6 header → real).
2. Resident USB enumerates if04 with NO app participation (write path needs no bring-up — RE agent confirmed).
3. A resident sAPI call by FIXED VA works from a standalone (DIRECT: `0x7e6916b9`).
4. The table BAKE works: memcpy of 401 constants → `0x7e1e2000` landed + is readable + dispatches (SLOT via
   `*0x7e1e2580`). RESULT 2/5 bake path → validated on-device.
5. Our entry runs stable (no watchdog crash over the observation window; kernel/byte-pool resident as RESULT 4).

**The C-only firmware is no longer a research question — it RUNS.** Remaining is app logic on a proven sAPI
surface: data/PDP + socket lifecycle (replicate init5), GNSS session, GT06 app (login/TERIID/LOC/heartbeat/
cmds/blind — mostly RE'd), OTA. Flash mechanics note: arom entry via `asrflash flash-app` failed (25 cycles)
because this unit spontaneously churns (battery brownout drops if05 mid-reset); the reliable path was a tight
busy-poll catch loop (20ms) that induces `AT+CFUN=1,1` reboots and fires `adownload` the instant ttyACM0
appears — caught on round 1. (Interesting: the unit was STABLE running our minimal standalone; the churn
tracks the vendor app's load.)

## ★★★ RESULT 6 (2026-07-18): the 32-byte descriptor is decoded — a from-scratch header is trivial (no copy/clear table).
Decoded the vendor app's descriptor (first 0x20 bytes of the decompressed image, loads @0x7e1b0000):
```
+0x00 0x00000000   +0x04 0x00000000
+0x08 0x00002800   (10240 — stack size / metadata; copy verbatim)
+0x0c 0x0000001e   (30    — priority / version; copy verbatim)
+0x10 0x7e1dd244 -> "userSpaceMain\0"   (entry-point NAME string, inside the image)
+0x14 0x7e1e1a31   ENTRY (Thumb) — the ONLY field the loader jumps to  [our injected _start]
+0x18 0x7e1dd252 -> "J16_WSTRK\0"       (app/build NAME string, inside the image)
+0x1c 0x00000000
```
**+0x10 and +0x18 are just name-string pointers** (loader logs/ignores them), NOT a data-copy or bss-clear
descriptor. The sAPI table @0x7e1e2000 sits 0xf0 BEYOND the image end (0x31f10) — it is BSS the loader/app
zeroes, never loaded bytes. So a from-scratch descriptor = copy +0x08/+0x0c verbatim, point +0x10/+0x18 at
two short name strings inside our own image, set +0x14 = our `_start|1`. No region tables to reconstruct.
(Residual, low-risk: whether the loader ACTS on +0x08/+0x0c — mooted by copying them verbatim. Full loader
RE from the golden 8MB dump is available if a from-scratch flash ever misbehaves.)

**Standalone build is now fully specified** — linker @0x7e1b0000, 32-byte descriptor as above, reserve
0x7e1e2000.. (table BSS) + runtime BSS, entry = `_start` that (a) stashes r0->0x7e1e2444 + replays init1's
resolve loop OR memcpy's the baked table (RESULT 2/5), then (b) probes a slot (VCOM write 0x7e1e2580, or
socket-connect). Packaged via fwtool + asrflash. This is an ENGINEERING build now, no unknowns left.

## ★★★★ RESULT 5 (2026-07-18): the r0=resolver ABI for a CUSTOM entry is CONFIRMED — the last ABI unknown is retired. NO flash needed.
RESULT 4 left one on-device residual: "does the loader pass r0=resolver to a *fully-custom* entry, not just
the vendor's?" It's now answered YES by RE + the existing working build — no from-scratch flash required.

**Key realisation: our `_start` is ALREADY a custom entry.** inject_stub repoints descriptor +0x14 to our
`_start` at **0x7e1e1a31** — a DIFFERENT address from the vendor's original entry (0x7e1b3dd1). `startup.S`
does `push {r0-r6,lr}; bl fw_init; pop {r0-r6,lr}; bx userSpaceMain` — so it forwards the loader's `r0`
unchanged into userSpaceMain. The loader keys off the +0x14 field, not a hardcoded address, so this
generalises to any from-scratch entry.

**RE of init1 (FUN_7e1b1ce0) proves r0-as-passed is the SOLE resolver source (no fallback):**
init1 stores the incoming r0 at 0x7e1e2444 (its only write to that slot), then for each of ~282
(hash, slot) pairs calls the resolver thunk with the hash and stores the result into the slot.
The resolver thunk at 0x7e1b1cd0 tail-calls the
stashed resolver with the hash in r0; returns 0 if the resolver ptr is null). There is **no literal-loaded
resolver address anywhere** — every one of the ~282 slot fills flows through the r0 that init1 stashed.

**The proof (non-circular):** if the loader had NOT set r0 for our custom `_start`, the thunk's `cbz r3`
guard would return 0 for every hash -> all ~282 slots = 0 -> identical to the observed empty-table state
(RESULT 1 `SN000`) -> the app can call no sAPI -> dead. But the device is NOT dead (LOCR7 tracks, wrappers
work), so r0 WAS the resolver. ∴ **the loader passes r0=resolver to a custom entry.** QED.

**Two free wins for the standalone design:**
1. **No-crash degradation.** The `cbz r3` guard means a standalone entry that (somehow) does NOT receive r0
   just gets a zero table — graceful, not a fault. So the resolver path is safe to attempt, and the BAKE
   path (RESULT 2) is a clean fallback with no resolver needed at all. The table is solved either way, with
   no failure mode that bricks.
2. **The standalone can replay init1 verbatim.** Entry: `str r0,[0x7e1e2444]`, then for each (hash,slot)
   pair call the thunk and store — OR just memcpy the baked constants. Both need only what we already have.

**Net: every ABI-level unknown for a C-only firmware is now closed.** Remaining work is pure app logic over
a usable sAPI surface: data/PDP + socket lifecycle (replicate init5) + GT06 app (mostly RE'd) + OTA. The
from-scratch descriptor/linker + on-device bring-up is now an ENGINEERING task, not a research gamble.

## ★★★ RESULT 4 (2026-07-18): userSpaceMain init RE'd — C-only firmware is GO, no OpenSDK needed.
`userSpaceMain` (0x7e1b3dd0) = 8 init fns + main stage (0x7e1bb209). Findings:

**Table population = a loader-provided RESOLVER (not a static blob).** The loader calls the app entry with
**r0 = a resident symbol-resolver `fn(hash32)->ptr`**. init1 (**FUN_7e1b1ce0**) stashes r0 at slot
**0x7e1e2444**, then resolves ~282 compile-time hashes into the table slots (resolver thunk 0x7e1b1cd0).
Verified vs the live capture (slot 0x7e1e2444=0x7e6d2b51=the resolver; task_create slot=0x7e6d2c69). So a
standalone fills the table EITHER by replaying init1's resolve loop (r0 + the embedded hash list) OR by
baking the captured constants (`fromsrc/sapi_table_boot1.bin`, RESULT 2). **No embedded const pointer-blob
to bake from — the runtime-captured table IS the bake artifact.**

**No byte-pool / kernel init exists in the 8 init fns.** The ThreadX kernel + stack byte-pool are RESIDENT
and already running at entry; every task_create is handed app-owned STATIC buffers (TCB+stack in BSS). So
the ONLY hard prerequisite before task_create is init1 (table fill). **RESULT 3's fault is now explained:
it wasn't a missing byte pool — the resolver hadn't run / the call was outside the vendor's ordering / the
args were wrong. Not a blocker.** Safe rule: fill the table first, then call in vendor order.

**The 8 init fns:** 1 FUN_7e1b1ce0 table-populate [REQUIRED first]; 2 FUN_7e1d5b34 USB-VCOM [drop, keep for
tooling]; 3 FUN_7e1ca878 SIMCom "manage" core (config/GPS/blind-buffer, spawns manage task) [REQUIRED];
4 FUN_7e1d46a4 UART1/external-MCU [DROP]; 5 FUN_7e1bb9dc network task farm (DNS/PPP/MainSck + AGps/LBS/Sec)
[REQUIRED: PPP+DNS+MainSck; drop AGps/LBS/Sec]; 6 FUN_7e1d5aac modem AT-URC [keep]; 7 FUN_7e1d315c SMS
[DROP]; 8 FUN_7e1b5554 voice/telephony [DROP].

**Minimal standalone init sequence — GO (replicable from our C, NO OpenSDK):**
```
(i)  entry: save r0 -> *0x7e1e2444   (r0 = resident resolver)
(ii) table fill: replay init1's resolve loop  OR  memcpy the 402 captured constants into 0x7e1e2000
     (skip the 3 dynamic tail slots) -> sAPI dispatch live; kernel/byte-pool already resident.
(iii) data: replicate init5 PPP/DNS -> PDP context; wait network-ready flag 0x7e1fc825.
(iv) GNSS: init3 GPS setup (~0x7e1b8835) + GPS-power slot 0x7e1e21ac; fix copy 0x7e1b8931.
(v)  socket: fn_connect 0x7e1d792c -> MainSck-style task -> GT06 app (login/TERIID/LOC/heartbeat/cmds/blind).
```
**NO hard blocker found.** The one on-device residual (NOT a blocker): confirm a STANDALONE entry gets
r0=resolver (loader passes it to a fully-custom app, not just the vendor). If not, use the bake path (needs
no resolver). Either way the table is solved. Remaining real work = data/PDP + socket lifecycle + OTA (app
logic over an already-usable sAPI surface).

## ☆ RESULT 3 (2026-07-18): baking the table is NECESSARY but NOT SUFFICIENT — the runtime needs init.
Tested the bake-and-call directly: at fw_init (table empty, before userSpaceMain) wrote ONE captured slot
(task_create @0x7e1e211c = 0x7e6d2c69) and called it standalone-style (over-large stack, meant to error-
return without creating a task). **Result: the call FAULTED the boot** — device crashed to ROM (arom),
never reached USB init. Recovered by reflashing LOCR7 from arom.

**Interpretation:** the BAKE mechanism itself is fine (RESULT 2: the slot values are constant + correct).
What fails is calling the sAPI runtime at the *bare* loader handoff — the runtime STATE that even
task_create needs (the ThreadX byte-pool for stack alloc, app-level RTOS setup) is created by
userSpaceMain's init, which we skipped. So a standalone must **bake the table AND replicate userSpaceMain's
init sequence** before any sAPI call works; the table alone (dispatch) is not enough. Caveat: this test
called at the EARLIEST point (fw_init) with ZERO init — a real standalone would run the init sequence at
its own start, then call, so the fault is expected and does NOT prove infeasibility — it confirms the init
is required. **Next: RE or CAPTURE userSpaceMain's init sequence** (the 8 init fns — which sAPI slots they
call, with what args; or hook+log the boot call sequence and replay it). That, plus the baked table, is
the runtime-bringup a standalone needs. (The connection lifecycle + OTA remain on top of that.)

## ★★ RESULT 2 (2026-07-18): CAPTURE-AND-BAKE WORKS — the table is constant across boots.
Follow-up to RESULT 1. A patch build added `SZCS#SAPIDMP` (dumps 0x7e1e2000..0x7e1e2864 out USB VCOM,
framed `SAPI<<<`…`>>>SAPI`). Captured the POPULATED table on two separate boots (RESET# between) and
diffed:
- **534 of 537 words IDENTICAL across boots; 401 non-zero-and-identical = the sAPI fn pointers.**
- Non-zero entries live in two FIXED resident-blob regions: **334 at 0x7e6xxxxx, 67 at 0x80xxxxxx**
  (distinct from the app at 0x7e1b…). 135 slots are zero (unused).
- Only **3** words differ (idx 529/530/532 @0x7e1e2844/48/50) and they are tiny data values
  (0x0b00→0x100, 0->0x00020113, 0->0x100), NOT fn ptrs — per-boot counters/handles at the table tail.

**Conclusion: BAKE-AND-REPLAY IS VIABLE.** A standalone customer_app can carry the 401 constant fn
pointers as a `const` array, `memcpy` them into 0x7e1e2000 at boot, and the sAPI dispatch table is
populated — no OpenSDK needed for the table, no RE of userSpaceMain's population mechanism. The 3 dynamic
tail slots are excluded from the bake. Captured tables: `fromsrc/sapi_table_boot1.bin` (reference).

**Remaining for a standalone (unchanged by this):** the fn POINTERS are now free, but STATEFUL subsystems
(sockets need PDP+connect, GNSS needs its session opened, VCOM needs its transport bound) still need the
app-level init that userSpaceMain's 8 init fns do — a finite, RE-able (or capture-the-init-call-sequence)
subset. Next validation: in a patch build, at fw_init (table empty), memcpy the baked table + call a
STATELESS slot (task_create/CFUN) directly BEFORE userSpaceMain — proves the baked table works with the
resident runtime and no vendor init. Then test a stateful call (socket) to size the init we still owe.

## ☆ RESULT 1 (2026-07-18): the loader does NOT fill the table at handoff; the vendor app does.
Ran a lower-risk variant of the test (no from-scratch image needed): a normal patch build snapshots the
table `0x7e1e2000..0x7e1e2864` at the FIRST instruction of `fw_init` (the loader→app handoff, before
`userSpaceMain`) and encodes the non-zero-word count in the version tag. Device reported **`SN000`** =
**0 of 537 words filled at handoff.** Post-boot the table is populated (our wrappers work), so it is
filled by **`userSpaceMain`'s init**, not the loader.

**Implication:** a standalone `customer_app` that just "calls the sAPI slots directly" WON'T work — the
slots are null at our entry. A C-only firmware must **also bootstrap the runtime table itself.** This is
harder but not necessarily a dead end: the open question is now **HOW `userSpaceMain` populates
`0x7e1e2000`** — (a) it writes known FIXED sAPI fn addresses (a resident blob at a fixed load addr) →
a standalone can RE + replicate the population (or call the fns directly at those addresses), no OpenSDK
needed; or (b) it resolves them via a loader/ROM mechanism (a passed pointer / syscall) → we'd RE that
or need the SIMCom A7670SA OpenSDK. **Next step (safe, no device): RE which of `userSpaceMain`'s 8 init
fns writes `0x7e1e2000` and where it sources the values.** That decides feasibility.

(Original VCOM-write standalone experiment below is now moot for the primary question — the table-fill
finding above supersedes it. Keep it as the follow-on confirmation once the population mechanism is RE'd.)

---


**This is the single gating question for a C-only firmware (system.md §7 level-2, gap #1 / "2b").**
Answer it and either the whole standalone path unlocks, or we know we need SIMCom's A7670SA OpenSDK.
Everything else in a from-scratch firmware is known or well-understood; this is the one make-or-break
unknown, and it is cheap to test.

## The question, precisely
The platform runtime (tasks, sockets, GNSS, USB-VCOM, NV, CFUN) is reachable only through a
**function-pointer table the loader fills into fixed BSS at `0x7e1e2000..0x7e1e2864`**. Today (level-1
patch model) we borrow the *vendor app's* thin wrappers over that table. A **standalone** app (our C
only, zero vendor application code) has no wrappers — it must call those slots **directly**. That works
**iff the loader fills the table at the same fixed addresses when the resident `customer_app` is entirely
ours.** Strong hint it does (the vendor app hardcodes slot VAs like `0x7e1e211c` as compile-time
constants → the SDK/loader contract fixes the table location, not per-app), but it is **unverified**.

## What we already know (so this is a small experiment, not a leap)
- **A custom image boots.** Load path is understood + ungated: 32-byte descriptor (entry at `+0x14`),
  flash = `[u32 len][u32 ~crc32][LZMA payload]`, integrity = 8-bit XOR only (no signature). We build and
  flash arbitrary images over USB routinely. A from-scratch image with a valid descriptor **will run to
  our entry**. (Getting our code running is NOT the blocker.)
- **The slots we'd call are already pinned** (from the level-1 RE, `asr_api.h`/`vendor_api.h`):
  - USB-VCOM write: vendor wrapper `0x7E1B1B2D` dereferences sAPI slot **`0x7e1e2580`** — the cleanest
    host-observable call.
  - task_create: slot **`0x7e1e211c`** (vendor wrapper `0x7E1B1C59`).
  - GPS power: slot **`0x7e1e21ac`** (wrapper `0x7E1B185D`). CFUN: the slot `set_cfun` (`0x7e1b194d`)
    tail-calls.
  In a standalone image the *wrappers* (`0x7E1B18xx`) are gone, but the *slots* (`0x7e1e2xxx`) are what
  the loader fills — so we call them directly with a 3-instruction wrapper (`ldr slot; ldr [slot]; blx`).

## The test image (minimal standalone `customer_app`)
- Linked at `0x7E1B0000`, 32-byte descriptor first, entry `+0x14` → our `_start`.
- Contains ONLY our code — **no vendor app**. Decompressed size must fit the `0x7E1B0000` RAM window and
  must **not** touch `0x7e1e2000..` (that's the table BSS the loader fills) or the runtime BSS above the
  app.
- Entry body (primary probe — VCOM write, host-observable, no network/GPS/SIM needed):
  ```c
  /* call the VCOM-write sAPI slot DIRECTLY (loader-filled), not a vendor wrapper */
  typedef int (*vcom_write_fn)(const void *buf, int len);
  for (int i = 0; i < 200; i++) {                 /* retry — transport may need a beat to bind */
      vcom_write_fn w = *(vcom_write_fn *)0x7e1e2580u;
      if (w) w("SAPI-OK-<build-id>\r\n", 20);
      busy_delay(~50ms);
  }
  /* then spin (don't return into nothing) */
  ```
- Package with the existing `fwtool.py` (LZMA + `[len][~crc32]` wrapper) + XOR pad; flash via
  `simcom_da/asrflash.py flash-app <img> --reboot`.

## Observe
Host reads the USB VCOM port (`/dev/serial/by-id/usb-SIMCom_..._-if04-port0`) after flash+reboot:
```
python3 -c "import serial;s=serial.Serial('/dev/serial/by-id/...-if04-port0',115200,timeout=30);print(s.read(400))"
```

## Decision tree
- **Marker `SAPI-OK-…` appears → decisive YES.** The loader fills the sAPI table for a custom app; we can
  call any slot directly. **Gap #1 collapses.** The C-only firmware is a real project — proceed to build it
  up incrementally (next probe: socket-connect to our server via the socket sAPI slots → server sees the
  device; then port the GT06 app: login/TERIID, LOC, heartbeat, commands, blind buffer — all already RE'd).
- **Nothing appears → NOT decisive on its own.** Two causes: (a) table not filled for custom apps, or
  (b) the VCOM *transport* wasn't bound (in the vendor model the USB-VCOM task binds it; a standalone image
  has no such task). Disambiguate with the **network fallback probe**: a standalone image that calls the
  socket sAPI slots to open a TCP socket to our server and send one GT06 login/marker packet — if the
  **server** sees a connection, the table works (via a different, transport-independent path) and only VCOM
  needed its own init. If BOTH VCOM and socket probes produce nothing → the loader→app table contract is
  app-specific → **we need the SIMCom A7670SA OpenSDK** (FAE-gated), or the Logicrom SDK (caveat: its
  A7670SA cellular-band build is unverified).

## Tooling to build (the only real work — ~½–1 day)
1. A **standalone linker script + descriptor** (32-byte header, entry `+0x14`, reserve `0x7e1e2000..` and
   the runtime BSS). Distinct from `link.ld` (which injects into the vendor image's holes).
2. Reuse `fwtool.py` for compress/wrap, `asrflash.py` for the USB flash.
3. The tiny entry `.c`/`.S` above.

## Risk & recovery
- Flashing a standalone image **replaces the vendor app** → the device won't track during the test (it
  runs only our probe). Fully recoverable: reflash the golden dump or the reporter-only build over arom
  USB (`USBDEVFS_RESET` + `asrflash flash-app … --reboot` with a long timeout — the recipe from this
  session).
- **Use a bench/spare unit** with the golden dump on hand. G375356 has been fragile this session; a fresh
  unit is preferable for a clean result.

## Why this is the right first move
It is low-cost (one small image + existing flash path), low-risk (recoverable), and it converts "a C-only
firmware should be feasible" into a hard yes/no. On **yes**, the parts list is: connection lifecycle
(attach/PDP/connect/reconnect — the one genuinely-new build), the GT06 app (mostly RE'd already), and OTA
(only if field-updatable is required). On **no**, the answer is simply "acquire the OpenSDK first," and we
stop guessing.
