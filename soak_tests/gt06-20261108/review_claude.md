# Standalone fw review (Claude) — 2026-11-08

Scope: `gt06/Firmware/standalone/` main.c, reliability.c, sapi.h, plus sitl_hal.c where it differs from HW.
Read-only review of the source that is HEAD = `f655071`. hwwd.c (disabled, `WD_HW_ARM 0`) and
usb_debug.c (separate bench build) were skimmed only.

## Known issues the brief asked about

- **"~13.5-min reboot @1Hz"** and **"corrupt LOC ~20 s"** are both already FIXED and documented
  (gt06/Firmware/roadmap.md:184 and :199). The reboot was leaked URC payloads exhausting the resident heap
  (fixed by `urc_free`, main.c:1163 / main.c:3833). The corrupt LOC was AT text spliced into NMEA (fixed by
  `parse_at_urc` returning 1 for AT chunks, main.c:1326, plus the NMEA checksum and jump gate, main.c:448 / main.c:474).
  The memory note that calls them open is stale.
- **G318430's exact 18m23s reboot cycle is almost certainly vendor firmware, not ours.**
  - Our fw has only three reset paths: the software watchdog, `RESET#` and FOTA. The PLMN policy and the
    recovery ladder never reboot.
  - Our `wd_tick` writes `WD:alive` to if04 on every healthy tick (reliability.c, `wd_tick`). Yesterday
    if04 gave 0 bytes in 35 s.
  - G318430 has never logged in to the server, so its app fw is unknown.
  - The vendor fw is known to reboot after ~15 min offline (the 07-07 regatta outage).
  - To confirm: open if04 for 3 s. Our fw prints `WD:alive=` at 1 Hz; vendor fw prints nothing.

## Findings (most severe first)

### 1. HIGH — No application-level link liveness. A silent link loss bypasses the blind buffer, and a blocking send can reboot the unit

- **Where:** main.c:3547 (socket set back to BLOCKING after connect); main.c:3657-3664 (a 0xF1 batch counts
  as delivered once `send()` returns `flen`); main.c:3685 (heartbeat); main.c:2597 (`handle_rx` only parses
  0x80 frames and ignores every server ACK).
- **What the server does:** it ACKs login, 0x13 heartbeat and 0x16 alarm (server/protocol_GT06.py:1963,
  :2430, :2486). The fw never looks at those ACKs.
- **Scenario:** a sailor goes out of coverage at sea while the modem still thinks it is attached. That's the
  usual case: no RST, the socket is not in error.
  1. `send()` keeps "succeeding" into the lwIP send buffer, so `connected` stays 1.
  2. The fixes go into the TCP buffer instead of `g_blind`.
  3. When lwIP finally aborts the connection, everything in its buffer is lost.
  4. If the send buffer fills first, the blocking `send()` stalls longer than 12 ticks. The software
     watchdog then resets the SoC (phase `S`), which also wipes the RAM blind buffer.
  - Net effect: the blind buffer only protects fixes taken after the fw notices the outage, and the fw is
    the slowest part of the system to notice.
- **Fix:**
  - (a) Track the 0x13 ACK: no ACK within ~2×HBT (or ~30 s while tracking) means the link is dead. Close,
    reconnect, and keep the last N seconds of fixes in a "sent but not confirmed" ring.
  - (b) Check writability with `select()` before every GT06 send (as `fwd_poll` already does). Not writable
    means treat it as disconnected and spill to blind, rather than blocking.
  - (c) Optionally have the server ACK 0xF1 by serial, so the batch path gets exact confirmation.

### 2. HIGH (safety) — The SOS burst counts sends, not ACKs

- **Where:** main.c:3695-3699. `g_sos_sends++` runs whenever `send()` returns the full length. After 10 sends
  the burst stops until the next reconnect re-arms it (main.c:3561).
- **Scenario:** SOS is pressed on a half-open link (same conditions as #1). All 10 alarms go into a dead TCP
  buffer, and the unit stops alerting. The server only hears about it after a reconnect, which per #1 may be
  many minutes later or never (if the watchdog reboots first, `g_sos_active` is cleared).
- **Fix:** keep resending 0x16, e.g. every 2-5 s, until the server's 0x16 ACK arrives. Treat a missing ACK
  as a dead link (close and reconnect). Consider persisting the SOS latch across a reboot (FS flag).

### 3. HIGH when USB is attached, MEDIUM otherwise — `wd_tick` does blocking VCOM writes from the resident timer thread

- **Where:** reliability.c `wd_tick` calls `wr_hex("WD:alive")` on every healthy tick, plus `WD:stall` and
  `WD:phase`. `wr_hex` goes straight to `WR` and is gated only by `g_fwd_en`. It ignores `g_vcom_en`
  (`SZCS#VCOM=0` does not silence it) and ignores `g_vcom_hold`.
- **Conflict with main.c:** main.c:214-221 documents that this resident VCOM write BLOCKS for seconds when
  if04 is enumerated with no reader. It relies on "the timer tick advancing `wd_uptime()` independently" to
  detect the stall (main.c:299).
- **Scenario:** the unit is charging from a PC or hub port with nobody reading if04.
  1. The timer callback blocks inside `WR`.
  2. `g_uptime` stops, so every cadence in the main loop (HBT, batch flush, GPS reassert, connect timeout,
     PLMN timers) freezes or slows.
  3. The main loop's VCOM stall guard can never fire, because its clock is frozen by the same write.
  4. The watchdog itself is the blocked thread, so it cannot catch a hang.
  5. The ThreadX timer thread is also shared with resident timers.
- This contradicts the file's own rule: "the timer callback context must stay non-blocking".
- **Fix:** no I/O in `wd_tick`. Set flags or counters and let the main loop print them through
  `vcom_write_guarded`. Drop the per-second `WD:alive` (or print it every 60 s from the main loop).

### 4. MEDIUM-HIGH — The PLMN policy can pin a unit to a carrier with no coverage for up to ~1 h

- **Where:** main.c:2447-2473 (`plmn_policy`), `REC_HOLDOFF_SEC 3600` (main.c:2356), manual `AT+COPS=1,2`
  (no automatic fallback, main.c:2484-2489).
- **Scenario:** the unit is registered on Optus and sails into an area where only Telstra has coverage.
  1. 300 s grace (main.c:2457).
  2. Select Telstra and give it 120 s. In weak signal, registration often takes longer than that.
  3. Select Optus again and give it 120 s.
  4. `attempts >= count`, so `paused = 1` for 3600 s, still manually locked to Optus.
  - Result: up to ~68 min offline while a permitted carrier is available. The blind buffer only holds 5 min.
- **Fix:**
  - Keep cycling the allowed list with a short holdoff (e.g. 5-10 min) instead of 60 min.
  - Lengthen the per-attempt settle when CSQ is low.
  - Or select with `COPS=4` (manual with automatic fallback) and let the existing "blocked operator"
    branch correct a disallowed camp.

### 5. MEDIUM — A `recv()` error after `select` says readable is ignored, leaving a spin loop

- **Where:** main.c:3580-3583: `if (n == 0) connected = 0; else if (n > 0) …`. The `n < 0` case is dropped.
- **Scenario:** after an RST or error, lwIP keeps the socket readable and returns -1 every time.
  1. In idle the bottom-of-loop `select(fd, 1 s)` (main.c:3729) returns at once, and `mqr(..., 0)`
     doesn't wait.
  2. The main loop spins at full CPU until the next heartbeat `send()` fails.
  3. That's up to `g_hbt_off`: 15 s today, but up to 3600 s if the idle HBT is raised as recommended below.
- **Fix:** treat `n < 0` (on a blocking socket that `select` reported readable) as a disconnect.

### 6. MEDIUM — Idle drains only one URC per loop pass (~1/s)

- **Where:** main.c:3732, `mqr(qid, msg, 0)` once per pass. The queue was created with depth 50 (main.c:3314).
- **Scenario:** the GNSS fails to power off in idle, or is still emitting in the seconds after an off
  request. At the ~29 msg/s rate seen while tracking, the queue is full within ~2 s.
  - Whatever the resident does on a full queue (block the producer task, or drop and possibly leak the
    payload it already allocated) is untested. The latter is exactly the class of bug behind the old
    13.5-min heap death.
- **Fix:** in the idle path, loop `mqr(..., 0)` until it is empty (bounded, e.g. 64 per pass), running the
  same parse and `urc_free` for each message.

### 7. MEDIUM — No last-resort recovery when the modem is registered but has no working data path

- **Where:** `cell_recovery` bails whenever CEREG is 1 or 5 (main.c:2536), and is skipped entirely when
  `plmn_policy` returns 1 (main.c:2513). The ladder never escalates to `CFUN` or a reboot in that state.
- **Scenario:** PDP context lost, or the bearer is wedged while registration is fine. The fw retries
  `connect()` every 5 s forever and the unit stays offline until a power cycle.
  - The vendor's crude ~15-min offline reboot covered this case (it's also the G318430 behaviour).
- **Fix:** add a slow escalation while not intentionally sleeping:
  - offline > 10 min while registered → `CFUN 0/1`;
  - offline > 30-60 min → `sys_reset`.
  - Make the thresholds idle-aware so a unit parked out of coverage doesn't reboot-loop.

### 8. MEDIUM (observability, needed for the multi-day test) — Reset causes are invisible

- **Where:**
  - reliability.c `reliability_init` rewrites `RR_POWERON` on every boot.
  - A watchdog fire calls `soc_reset()` without recording anything (no FS from timer context), so watchdog
    resets read back as POWERON, the same as a brownout.
  - main.c:3250-3251 computes `wd_reason` and then discards it.
  - cxzt and STATUS carry no uptime, bootcount or last reason.
- **Fix:** report `bootcount`, last reason, last `g_wd_phase` and uptime (new `CXCS#BOOTSTAT`). Record a
  watchdog fire in a `.noinit` RAM word, or set a flag the main task persists on its next pass, if RAM
  survives `sys_reset`.

### 9. LOW-MEDIUM — Boot config fallback lands on the test server port and ignores the backup copy

- **Where:**
  - main.c:3328-3334: the backup `cwbank.bin` is only tried when `fopen(primary)` fails. A primary that
    opens but is short or invalid gives `have = 0`, with no bank fallback.
  - main.c:3350-3351: the fallback server is `103.230.158.49:8711`, which is the test port. Production
    is 7711.
- **Scenario:** a torn or corrupt `cwprm.bin`. The unit logs in with its raw IMEI to 8711 forever. Our fw
  has no SMS command path (roadmap #10 is still TODO), so there's no remote way back.
- **Fix:** validate the primary by content and fall back to the bank copy, then default to 7711. (Also,
  `cwprm_write` writes primary-then-bank, main.c:1846. `fwd_store` uses the safer bank-first order.)

### 10. LOW — FOTA edge cases reboot instead of failing gracefully

- **Where:**
  - main.c:2704 is a BLOCKING `connect()` with no watchdog feeding. If the host doesn't answer, SYN
    retries run past 12 s and the watchdog reboots, rather than printing "connect fail".
  - main.c:2737-2745 never checks the HTTP status line. A 404 or 302 body is staged as an image; the
    CRC then rejects it and the unit reboots.
- No brick risk (nothing is committed), but a mistyped YCSJ costs a reboot. The server only sends YCSJ on
  operator action (protocol_GT06.py:1309), so there's no loop.
- **Fix:** use a non-blocking connect with a polled deadline; require `HTTP/1.x 200` or `206`.

### 11. LOW — Night disconnected sleep doesn't poll the SOS button

- **Where:** main.c:3492 calls `wd_sleep(sleep_ms + 1000)`, up to 3601 s. The M0 is only polled from the
  main loop (main.c:3429), so an SOS press during sleep isn't seen until the alarm.
- Night idle is currently disabled server-side (`night_idle.enabled = False`). Fix this before enabling it:
  sleep in ≤5 s slices and poll the M0 in between.

### 12. LOW — SITL message-queue timeout units don't match HW

- **Where:** sitl_hal.c:310 treats the `mqr` timeout as ms. On HW it is in 5 ms ticks (reliability.c
  `wd_sleep` comment: "a timeout of 1000 lasts ~5 seconds").
- **Consequence:** main.c:3722 `mqr(..., 1000)` is ~5 s on HW but 1 s in SITL, and `FWD_PUMP_MS 20` is
  100 ms on HW. SITL timing results (disconnected cadence, GPS-wait loop rate, FWD pump) are therefore
  ~5× optimistic. This goes against the "HAL must closely match HW" rule.

### 13. LOW — The reported version is the repo HEAD, not the fw commit

- **Where:** Makefile:9 uses `git rev-parse HEAD`. A clean rebuild today reports `WSTR-STD1_4581e78` for
  source identical to `f655071`, so the server and fleet tracking can't tell builds apart.
- **Fix:** `git log -1 --format=%h -- .` (plus `-dirty` when the tree is modified).

### 14. INFO — The blind buffer is small

- **Where:** `BLIND_CAP 300` (main.c:108) is 5 min at 1 Hz. At ~24 B per entry, 30 min is ~43 KB, which
  fits comfortably in the ~180 KB free. A 5-min hole is short for a sea-level coverage dip, especially
  combined with #4.

## Idle battery: observations

1. **The server config, not the fw, dominates idle drain right now.**
   - Live `gt06.json` has `idle_hbt_interval: 15`, so an idle WSTR unit sends a heartbeat every 15 s, day
     and night.
   - The server queues STATUS# on heartbeats at most every 60 s (protocol_GT06.py, heartbeat handler).
   - Uplink every 15 s keeps LTE in RRC-connected (carrier inactivity timers are ~10-20 s), so the modem
     never reaches idle DRX.
   - The vendor-fw measurement already showed HBT 900 at ~18-22 mW versus far more at 15 s.
   - For WSTR idle, use HBT 300-900 and STATUS no more than every 10-15 min. That needs #1 or #5 fixed
     first, so a dead link is still noticed promptly.
2. **Module sleep is only ever permitted in the night-disconnect path.** `SystemSleepSet(1)` is called
   only at main.c:3483, and only when `SLPDISCONNECT=1`, which only happens with night idle (disabled). In
   ordinary connected idle the SoC is never allowed to auto-sleep. Worth a bench test: permit sleep while
   connected and idle, and check that downlink data (a TIMER command) still wakes the loop.
3. **Idle loop wake rate:**
   - `select(fd, 1 s)` every second (main.c:3728);
   - the GNSS status getter once a second via `gps_state_from_evidence` (main.c:3641 → main.c:1139);
   - an M0 UART query every 5 s (main.c:3429);
   - COPS/CPSI getters every 30 s (main.c:2507, probably IPC to the CP).
   - Cheap individually; it matters once sleep is permitted. With sleep permitted, a 5-10 s idle wait is
     enough: commands still wake `select`.
4. **Out of coverage while idle:**
   - `connect()` every 5 s forever (`CONN_RETRY_SEC`, main.c:930 / main.c:3509), with no backoff;
   - no CFUN duty-cycling (the MBK01 idea from the vendor-fw work);
   - the night sleep requires `connected`, so an out-of-coverage unit never sleeps.
   - This is the ~6× drain case seen on vendor fw. Suggest idle backoff growing to 60-300 s, and CFUN-off
     duty-cycling after N min unregistered.
5. **GPS off in idle looks correct.**
   - Boot `apply_gps(0)` (main.c:3389);
   - retry every 10 s until confirmed (main.c:3631);
   - confirmation from the resident getter, or 30 s of NMEA silence;
   - NMEA is ignored in idle (main.c:3750).
   - Verify per unit with `CXCS#GPSSTAT` (`pwr1`, `gst0`, `nage` climbing to 999).

## Measurements worth running during the multi-day test

- **Reboot accounting:** count logins per unit per day from gt06.log. Better, add #8 and record bootcount
  and reason per cxzt poll.
- **Idle drain on battery** (off charger, the morning after charge), 24 h, on comparable 6 Ah cells:
  - HBT 15 vs HBT 900 (server config change only);
  - our fw vs a vendor unit.
  - Use resting mV from STATUS, not % (load sag).
- **Idle GPS check:** `CXCS#GPSSTAT` every few hours on every unit: `pwr` must be 1 and `nage` 999.
- **Silent-link test (exercises #1 and #2):** while a unit tracks, silently DROP its packets at the server
  (iptables DROP on 7711 for that source, no RST) for 2, 5 and 10 min. Record:
  - whether it reboots (watchdog on a blocking send);
  - how many fixes are lost versus replayed;
  - how long until it reconnects.
  - Repeat with an SOS press during the outage.
- **Coverage-loss test** (Faraday box or foil) for 10 and 30 min, both tracking and idle. Check blind
  replay, reconnect time, no reboot, and drain.
- **Carrier-switch test (#4):** with `PLMNOK=50501,50502`, make one carrier unavailable (or set
  `PLMNOK` to a single carrier and change it live). Time how long until it is back online.
- **USB-attached with nobody reading (#3):** plug a unit into a PC with the ttyUSB closed for a few
  hours. Check whether heartbeat cadence slips (server `hbt` gaps), and look for reboots or a frozen
  uptime.
- **Long uptime:** let at least two units run 72 h with no reboot. That shows any slow leak (heap, sockets
  from the 5 s connect retries, file handles).
