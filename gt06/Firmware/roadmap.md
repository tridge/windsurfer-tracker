# Standalone firmware — deployment roadmap

The from-scratch C firmware (`standalone/`) replaces the vendor app on the W07C GT06 tracker:
it boots, reads the vendor config from `c:/cwprm.bin`, connects + logs in with the real TERIID,
and speaks the GT06 protocol (LOC / heartbeat / command ACK) over cellular TCP, plus reads
battery mV from the on-board M0 "supervisor" coprocessor over UART.

This is the task list to get it from "works on the bench" to "deployable to the fleet".

## Status

| # | Task | Status |
|---|------|--------|
| 1 | Idle → GPS off + no LOC (active/idle derived from `TIMER`; T1≤5 ⇒ tracking) | ✅ Done |
| 2 | Real satellite count (was hardcoded 8; parse NMEA GGA, clamp to the 15-sat nibble) | ✅ Done |
| 3 | Signal level reporting (direct resident `get_csq`; no AT on the URC queue) | ✅ Done |
| 4 | Battery level + mV in as many packet types as possible | ✅ Done |
| 5 | Point at the real server, port 7711 | ✅ Done |
| 6 | Real movement test (vendor unit vs our-fw unit, compare tracks) | ⬜ TODO |
| 7 | FOTA working, reliable, brick-proof | ✅ Done |
| 8 | Battery usage: our-fw vs vendor-fw, idle **and** tracking | ⬜ TODO |
| 9 | Protocol-handling diff: `gt06.log` our-fw vs a vendor unit | ⬜ TODO |
| 10 | SMS commands still work — especially APN / server / port changes | ⬜ TODO |
| 11 | All GT06 control functions work in the management UI | ⬜ TODO |
| 12 | Blind-buffer handling + faster-than-1Hz catch-up on reconnect | ✅ Done (SITL-validated; confirm on HW with #6) |
| 13 | SITL simulation of the firmware to test main logic off-device | ✅ Done |
| 14 | LED tracking-state indication (drive the tracker LEDs to show tracking vs idle, like vendor fw) | ✅ Done (HW-confirmed 2026-07-23) |
| 15 | Charging indicator (heartbeat bit2) — operator must see a tracker is properly on charge | ✅ Done (resident `charger_state()`; HW-confirmed. NB: M0 payload byte 13 was UPTIME, not charge) |
| 16 | Cellular auto-recovery from camped-but-denied (no USB needed) | ✅ Done (SITL-validated; confirm on HW) |
| 17 | **SOS button support (SAFETY)** — detect the physical SOS press, send a GT06 0x16 SOS alarm | ✅ Done (M0 payload byte 1; SITL-verified, HW-RE'd) |

## Done (details)

1. **Idle / tracking state machine.** The server has no single active/idle opcode; state is derived
   from the `TIMER,T1,T2` online command. `T1 ≤ 5` ⇒ tracking (GPS on, LOC every T1 s); `T1 > 5` ⇒
   idle (GPS off, no LOC, heartbeat only). Default at boot = idle. The accelerometer is never used.
   Active tracking is `TIMER,1,1` (1 Hz); idle is `TIMER,60,1800`.
2. **Satellite count.** Parsed from `$G?GGA` field 6 (satellites-in-use), cached, clamped to the GT06
   low-nibble max of 15 (the true count can be higher — a protocol limit, not a bug). Was hardcoded 8.
3. **Signal.** `AT+CSQ` is sent via the resident AT executor; the `+CSQ:` reply arrives on the same URC
   queue as GNSS NMEA and is parsed to a 0–4 level. Polled every 20 s and on connect.
4. **Battery mV.** The M0 supervisor MCU owns the battery ADC and serves it over UART1 with a `55 AA`
   framed protocol (`55 AA 02 00 02 0D 0A` request → `0x82` reply, mV at payload[19:20] BE). Reported as
   full-resolution mV in the `STATUS#` reply (`Battery:X.XXXV`) and the `cxzt#` reply (`*BT:<mV>`), and as
   a 0–6 level in the 0x13 heartbeat. See `re_battery_voltage.md`.

## Remaining (details)

5. **Real server, port 7711. ✅ DONE (2026-07-22).** G375349 moved to the production server on 7711 via
   `SZCS#SERVPORT=7711` and confirmed login + idle/heartbeat/config reporting there, running `WSTR-STD1_
   f8c6937`. FOTA-hosted builds use `FORCE_TEST_SERVER=0` so they honour `cwprm.bin` (7711). Unblocks #6/#8/#9.
6. **Field movement test.** Carry an our-fw unit alongside a vendor unit around the block; diff the tracks
   for position accuracy, cadence, and dropouts.
7. **FOTA. ✅ DONE (validated 2026-07-22).** YCSJ handler in the app HTTP-downloads the `[clen][~crc32]
   [LZMA]` package, streams it into the resident flash API (auto-stages in `reserved`), CRC-gates, closes
   (sets the pending marker), verifies, and reboots so the resident commits `reserved→customer_app`.
   Brick/field-safe by design (customer_app never partially written). Proven live end-to-end via FOTA only
   (no USB): `WSTR-STD1 → WSTR-STD2` confirmed by the server's cxzt version. See `re_flash_write_api.md`.
8. **Battery usage vs vendor.** Measure idle and tracking drain for both firmwares on comparable cells;
   confirm our fw doesn't draw more (GPS power management, modem duty, loop wakeup rate, blind buffer).
9. **Protocol-handling diff.** Compare `gt06.log` for a vendor unit vs our fw side by side — packet types,
   field values, timing/cadence, ACK and reconcile behaviour. Flag any divergence the server cares about.
10. **SMS reconfig.** SMS is the durable fallback for reconfiguration. Verify SMS-set of APN, server IP,
    and port still take effect on our fw.
11. **Management-UI control functions.** Verify each management-UI GT06 control against our fw: reboot
    command, registration with IMEI→TERIID change (provisioning), and the rest.
12. **Blind-buffer handling + catch-up on reconnect. ✅ DONE (2026-07-23, SITL-validated).**
    In-RAM ring (300 fixes = 5 min at 1 Hz, ~4.8 KB bss; lost on reboot by design — no flash wear / no stale
    backlog, per user). Out of coverage each 1 Hz fix is buffered with its OWN RMC timestamp; on reconnect
    they replay at **5 Hz**, each LOC carrying its original datetime so the track back-fills to real time
    instead of bunching at the reconnect instant (core: commit f28a06d).
    **Non-blocking reconnect (the follow-up) done:** connects start non-blocking (`fcntl(s,4,1)`) and are
    polled with a `{0,0}` select on the write+except sets; success = writable → restore blocking + login,
    failure = except (lwIP errevent) or a 30 s deadline; attempts are paced at 5 s. The main loop — and 1 Hz
    blind capture — keeps running while a connect is pending. Also added: **`SZCS#BLIND_EN=<0/1>` honoured**
    (RAM-only; replies `SETOK:BLIND_EN=x`, query `CXCS#BLIND_EN` → `READOK:BLIND_EN=x`, the exact shapes the
    server reconcile scans — it pushes 1 tracking / 0 idle), and an **idle drain** (fixes buffered across a
    TIMER idle flip drain at the same 5/s paced by the 1 s idle wait, since idle has no RMC ticks to ride).
    SITL-validated (radio-off blackhole SIGUSR2 + coverage-loss SIGUSR1): 1 Hz capture continues while a
    connect hangs pending; 30 s pending timeout; coverage-loss connects fail fast via except (5 s retry
    cadence, no stall); reconnect ≤1 s after radio restore; 5 Hz replay with backfilled datetimes; BLIND_EN=0
    buffers nothing; idle drain works. Clean under ASAN, UBSAN, and valgrind. HW validation rides item 6.
13. **SITL simulation of the firmware. ✅ DONE + VALIDATED (2026-07-22).** `make sitl` / `sitl-asan` /
    `sitl-ubsan` build the SAME `main.c` for the host against `sitl_hal.c` mocks (seam = sapi.h `SL()` remap
    of every `SLOT_*` into a host `sitl_slots[]`; call sites unchanged). Mocks closely match real HW: real
    host TCP (select()-guarded recv), a thread injecting real `$GNGGA/$GNRMC` on the URC queue, AT+CSQ URC,
    M0 `55 AA/0x82` battery over the UART callback, file-backed FOTA staging, reset=commit+re-exec. Validated
    live vs the local test server (8711): connect/login/reconcile, TIMER tracking (1 Hz LOC) + idle, battery,
    and full YCSJ OTA of a dummy firmware (download→stage→verify→commit→reboot→reconnect). **Clean under
    ASAN, UBSAN, and valgrind.** Now the vehicle for developing #12/#9/#8 before HW. (Original plan below.)
    Dual build from ONE source: `make` = device image, a new host target =
    SITL x86 binary. Exercise the main logic (state machine, GT06 protocol, LOC/heartbeat/cxzt/STATUS
    builders, command handling, blind buffer) against the real test/prod server with no flash cycle. Like
    ArduPilot SITL. Factor the hardware-touching sAPI calls behind a seam so the same `main.c` compiles for
    device and host. **The HAL must CLOSELY match the real hardware environment, not a loose mock:** sockets →
    real host TCP (actual server); GNSS/NMEA → injected real `$G?GGA/RMC` sentences; battery/UART (M0 55 AA
    supervisor proto) → mock returning realistic mV; flash/FOTA staging → file-backed mock. Reproduce the
    real-HW quirks that matter — the resident lwIP recv timing/blocking semantics (so the recv fix + loop
    cadence are testable), sAPI return conventions, freestanding constraints (no libc). Rationale: most
    remaining features (blind buffer #12, protocol tuning #9, battery/loop #8) get developed in SITL FIRST,
    then validated on HW — so SITL comes before that feature work. See [[project-standalone-sitl-priority]].
    **Memory-safety:** the SITL host binary must run clean under **valgrind**, **ASAN** (`-fsanitize=address`)
    and **UBSAN** (`-fsanitize=undefined`) — provide sanitizer build variants and run the login/track/idle/OTA
    scenarios under all three (SITL is where the GT06 parsers/builders/blind buffer get memory-checked). Keep green.
    **Radio sim:** SIGUSR1 toggles coverage loss (connect/send fail fast, like the modem reporting no
    network); SIGUSR2 toggles radio-off/blackhole (sends fail, connects hang pending forever — the
    server-unreachable case). The HAL mirrors lwIP select semantics: a failed connect reports in the
    EXCEPT set (Linux SO_ERROR is folded into it), completion reports writable.
14. **LED tracking-state indication. ✅ DONE (2026-07-23, commit a87b58a; HW-confirmed).** The two app
    LEDs (gpio ids **7 = blue**, **0x78 = yellow**) drive off our own state via the resident GPIO sAPI
    (config slot 0x7e1e254c, set slot 0x7e1e2104 — both in sapi_pairs.h; RE in `re_leds.md`). Behaviour:
    idle → both off; tracking + GPS lock (valid fix < 6 s) + radio/data OK → both solid; tracking but
    failing (stale/no fix OR not connected) → blue solid + yellow flash; SOS active → both flash. Blink =
    `wd_uptime()` parity (~0.5 Hz), independent of loop rate; GPIO written only on a level change. Colour↔id
    was bench-confirmed (no `SZCS#LEDSWAP` needed; the command still flips it live if a batch ever differs).
    The **red** charge LED is the M0's own, not app-reachable. SITL: gpio mocks + `run_led_test` assert all
    three states; plain + ASAN clean. (RED LED = M0/charging, out of scope.)
15. **Charging indicator. ✅ DONE (2026-07-23, resident `charger_state()`; HW-confirmed — supersedes the
    earlier commit a289411 M0-byte-13 approach, which read UPTIME bits, not charge).** The operator
    must see a tracker is properly on charge (heartbeat 0x13 terminal-info **bit2 = 0x04**; the server
    parses exactly that bit → `chg`. Note `gt06_frames.py`/`gt06_listener.py` use 0x08 but production
    `protocol_GT06.py` reads 0x04, which is what vendor units send — matched). **Source RE'd on the bench:**
    the M0 supervisor's `0x82` UART status payload **byte 13 = 0x04 while charge current flows**, `0x00` at
    charge-complete/plateau or unplugged (held 0x04 through a natural charge-up 4158→4182 mV, dropped to 0
    at ~4185 mV). `batt_mcu_parse()` sets `g_charging = (pay[13] & 0x04)`. Also decoded the full payload:
    bytes 5..10 = M0 RTC, 14 = free-running counter, 15..18 = ASCII "B2.5" (M0 fw ver), 19..20 = mV;
    `pay[3]` pulses 1 on a charger-connect/complete edge (event, not steady). **Caveat:** a *full* battery
    on charge also reads 0 (no current) — acceptable, that's the desired end state, and at plug-in/overnight
    the batteries aren't full so a properly-seated unit reads charging and a poorly-seated splitter unit
    reads not-charging (the real diagnostic). SITL: M0 mock emits pay[13]=0x04 (SIGWINCH / `SITL_CHARGING=1`);
    verified heartbeat 0x40→0x44, server `chg` False→True. HW end-to-end (server shows chg while genuinely
    charging) rides the overnight test.
16. **Cellular auto-recovery from camped-but-denied. ✅ DONE (2026-07-23, SITL-validated).** Live incident
    (G375349, 2026-07-23 07:49): after a reboot the modem camped on an Optus cell that rejected the roaming
    SIM (EMM cause 15 "No Suitable Cells In Location Area", `+CEREG: 3` → emergency-only camp) and never
    fell back — app healthy, every connect failing for 45+ min, Simbase misleadingly "online", USB needed
    to fix it. A CFUN cycle alone did NOT recover (re-camped the same denied cell); manual
    `AT+COPS=1,2,"50501"` then `AT+COPS=0` did. The firmware now does that recovery itself via the AT
    executor (replies parse off the URC queue like +CSQ): while offline it polls `AT+CEREG?`/`AT+COPS?`;
    if unreachable AND unregistered (stat not 1/5) for `REC_OFFLINE_SEC` (5 min) it runs a ladder with
    settle time per rung — (1) CFUN 0/1, (2) COPS=2 then COPS=0, (3) COPS=? scan → manual-select the first
    *available* PLMN ≠ the camped one, then restore COPS=0 after 2 min of proven server connection (manual
    would strand a travelling unit). Exhausted ladder → 1 h holdoff (no attach storms — the Simbase penalty
    lesson). Registered-but-unreachable does NOT trigger it (that's not a PLMN problem). SITL: SIGHUP
    toggles carrier denial; full ladder + 5 Hz blind replay + restore-auto validated; ASAN/UBSAN/valgrind
    clean. The scan parser survives multi-chunk replies and long operator names ("Telstra Mobile").
17. **SOS button support. ✅ DONE (2026-07-23, commit 4616e8a; HW-RE'd + SITL-verified — SAFETY).** A sailor
    in trouble triggers an emergency alert from the tracker's SOS button. **Source RE'd on the bench:** the
    M0's `0x82` UART status payload **byte 1 = 1 while the SOS button is held** (`pay[1]` tracked two live
    20 s holds, 0 at rest; the RE leads pointed here — the M0 reads P3.0/3.1/3.5 = SOS/PK/GG and reports over
    the same `55 AA` link as battery/charge). `pay[3]` pulses on button edges but is unrelated/noisy — ruled
    out. **Detect:** a rising edge on `pay[1]` (in `batt_mcu_parse`) latches `g_sos_active` + arms a resend
    burst; the M0 poll was cut 15 s → 5 s so a press is caught promptly. **Send:** while latched the main loop
    sends a burst of **10 GT06 `0x16` alarms** — terminal-info alarm bits 3..5 = SOS(4) → `ti 0x60`, which the
    server (`gt06_parse_alarm_status`) reads → sets sticky `assist_active`, exits idle, WebUI audio alert.
    Burst survives packet loss; **re-armed on every reconnect** so an SOS raised out of coverage delivers on
    reconnect; fires in ANY state (idle too). Carries the last cached GPS fix (`g_fix_*`), else zeros (server
    uses last-known/receipt time). **Clear:** latched until `CANCELSOS#` (operator, via the manage API) or a
    reboot — the tracker can't drop a real alarm itself; releasing the button does NOT clear. SITL: M0 mock
    drives `pay[1]` (SIGTSTP / `SITL_SOS=1`), test server decodes 0x16 — verified press→10 alarms all
    `is_sos=True ti=0x60` self-limiting, CANCELSOS# halts + clears with button still down, reconnect
    re-delivers; ASAN + UBSAN clean. **Follow-up (item 14):** drive an SOS LED indication (blue/yellow via
    `SOSLEDTIME`, per re_leds.md) for local "press registered" feedback.

## Known robustness gaps / follow-ups

- **Idle heartbeat now honours `HBT`** (fixed 2026-07-22). It previously used TIMER T2 (up to 1800 s), so in
  idle the server saw no heartbeat for up to 30 min and dropped the unit at its 78 s timeout. Now `HBT,X,Y#`
  sets the 0x13 interval (default 15 s, clamped ≤ `HBT_MAX` 3600 s). Validated live on 7711.
- **FOTA hosting:** host the pad-free `make fotaimg` output (`standalone_fota.bin`), **not** `standalone_fw.bin`
  — the latter's trailing vendor XOR-pad byte makes our `fota_run` reject the image (`total ≠ clen+8`, CRC
  over the pad). Confirm on-device via the `FOTA:*` VCOM markers, not apache logs (root-only).
- **Non-blocking recv / fast idle commands — ✅ RESOLVED (2026-07-22).** The resident ignores `SO_RCVTIMEO`,
  so recv() blocked ~5 s in idle and start/stop-tracking was slow (~5 s, vs vendor ~1 s). RE'd `lwip_select`
  (idx309 → 0x8014644D) and `lwip_fcntl` (idx347, non-standard `fcntl(s,4,1)`). Fix: a `{0,0}` select() poll
  guards recv() (never blocks), and in idle the loop waits ON THE SOCKET via select(1 s) so a command wakes it
  immediately; the URC queue is still drained non-blocking (`mqr` timeout 0). Validated live on 7711: idle
  command latency ~1 s (was ~5 s), heartbeats steady at 15 s, CSQ fresh. Slots in `sapi.h` (`SLOT_SELECT`,
  `SLOT_FCNTL`).
- **`cxzt#` reply is partial** — it reports `*ID`/`*F`/`*H`/`*BT` but not `SLPDISCONNECT`/`SENDS`/
  `GPS_RST_TIME`/etc., so the server can't fully observe the config and re-sends those on each reconnect.
  (`BLIND_EN` is now observable: `CXCS#BLIND_EN` → `READOK:BLIND_EN=x`.)
- **~3-minute reconnect churn** — the connection drops periodically and re-runs the full bootstrap.
- **✅ FIXED: ~13.5-minute reboot cycle while tracking at 1 Hz** (root-caused 2026-07-23, commit 41a7a5e).
  Symptom: goes silent mid-stream, whole SoC reboots (USB re-enumerates), reconnects with boot defaults
  (`F:60|1800` until re-reconciled). ROOT CAUSE = **leaked URC payloads**. The resident allocates each URC
  message payload (`msg[3]`, the NMEA/AT text) and ownership passes to the consumer; the vendor app frees
  every non-NULL `msg[3]` after use (consumer 0x7e1d59c4 → free veneer 0x7e1d793c → `_free_r` slot
  0x7e1e2394, already in our resolver pairs, hash 0x931b3837). We never freed → ~29 URC msgs/s while
  tracking exhausted the resident heap after ~11-12 min (~17-18k messages) → resident dies (no watchdog
  stall — the loop was healthy to the last second; confirmed on VCOM). Idle has almost no URC traffic, so
  it was stable overnight. Fix: `urc_free()` frees each payload at one cleanup label, exactly once, with
  the vendor's observed reent (0x7e1e1970; this resident's `_free_r` ignores it and NULL-guards ptr).
  Out-of-range `msg[3]` is NOT freed (resident free has only a NULL guard — freeing garbage corrupts the
  heap). RE by codex (Ghidra). `URC:n` counter added (printed each 15 s battery cycle) — a death at a
  repeatable COUNT was the discriminator. Diagnostic `g_wd_phase` markers added (name the blocking call on
  a WD stall). Also fixed the fast ~2-min reboot-while-disconnected variant via item 12's non-blocking
  connect. HW soak on G375349 (build 41a7a5e) past the old death point: confirms.
- **✅ FIXED: corrupt LOC every ~20 s while tracking** (2026-07-23, commit d7b5d02). `parse_at_urc`
  recognised a `+CSQ` chunk but the SAME chunk still fell through into the NMEA line assembler; its leading
  CRLF terminated a sentence split mid-longitude and `parse_rmc` accepted the partial (`001.0` → 0d1m ≈
  0.0167°; the server log's absurd speeds were server-DERIVED from the coordinate jump — our frames carry
  speed 0). Fix: `parse_at_urc` returns 1 for AT/modem chunks (lead `+`, OK, ERROR, SMS, or a pending
  COPS=? scan) and the loop skips the assembler for them; NMEA never contains `+`. SITL now delivers RMC
  in two chunks split inside the longitude with a `+CSQ` between the halves every 20th sentence, so the
  fix stays regression-tested (0 corrupt coords over the soak).
- **Bench unit:** the RF hardware on one unit is damaged; use a healthy tracker as the bench unit.

## SITL goal (user, 2026-07-22)

Target a working SITL that: (1) **talks to the real server** (real host TCP, logs in, sends LOC/heartbeat,
ACKs commands); (2) **supports tracking AND idle** (TIMER-driven state machine, GPS from injected NMEA);
(3) **supports OTA of a dummy firmware** (YCSJ → download → stage → verify → commit → "reboot" against a
file-backed dummy flash). Then develop the remaining features (12/9/8 …) in SITL first, validate on HW.

## Suggested order

Done: 1–5, 7, 12, 13, 15, 16, 17, + the URC-leak reboot fix and the corrupt-LOC fix (both above). →
develop **9 / 8** largely in SITL, validating on HW → **6** (field movement test, HW-only; also validates
12/15/16/17 on HW) → **10 / 11** (SMS + management-UI coverage) → **14** (LED — RE done, `re_leds.md`;
includes the SOS LED indication follow-up for item 17). Remaining: 6, 8, 9, 10, 11, 14.

Dropped: the direct-PMIC/ADC battery probes (TWSI2 register reads of the 88PM8xx) — not needed, battery mV
comes from the M0 supervisor over UART1; the charge state is expected from the same source (item 15).
