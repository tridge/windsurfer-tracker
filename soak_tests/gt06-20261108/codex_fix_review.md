# Review of the uncommitted GT06 reliability fixes — 2026-11-08

Base: `4581e788643e0a65856fdf8103098783f4f7fa22`. Reviewed the working-tree changes under `gt06/Firmware/standalone/`, `server/protocol_GT06.py`, the six new firmware test/helper files, and `test/test_gt06_bootstat.py`, against both earlier review reports. References below describe the **uncommitted files**, not the base revision.

**Assessment: further fixes are needed before relying on this build for unattended operation.** In particular, a second SOS can be suppressed, offline night sleep can stop connection attempts, and the FOTA acceptance predicate still accepts a non-decodable image. Several fixes also need stronger coverage for repeated outages and delayed ACKs.

This was read-only apart from this requested report. No devices, remote servers, local listening servers, or socket-based SITL suites were contacted/run. ARM and SITL warnings-as-errors syntax checks passed without generating build artifacts. Both BOOTSTAT tests passed using their in-memory fake sockets; Python AST checks passed for 17 test/helper/server files. Pure, local counterexamples checked the date arithmetic, unacknowledged-ring capacity and LZMA predicate. This is source-level verification, not hardware validation.

Severity: **P1** = missed emergency notification, persistent outage, loss of watchdog recovery, or acceptance of a nonbootable update; **P2** = significant data/recovery/configuration error under the stated trigger; **P3** = diagnostic/control-response loss. Findings distinguish new regressions from incomplete fixes of the prior findings.

## Findings, ranked by severity

### P1-01 — Acknowledging the first SOS disables subsequent SOS delivery on the same connection

**Code:** [main.c:1636](../../gt06/Firmware/standalone/main.c:1636), [main.c:2871](../../gt06/Firmware/standalone/main.c:2871), [main.c:2918](../../gt06/Firmware/standalone/main.c:2918), [main.c:4167](../../gt06/Firmware/standalone/main.c:4167). **New regression.**

A 0x16 ACK sets `g_sos_acked=1`. A rising button edge resets `g_sos_sends`, but does not clear `g_sos_acked`; CANCELSOS does not clear it either. The only explicit clear is on reconnect while an SOS is active.

**Failure:** press SOS, receive its ACK, release the button, have the operator send CANCELSOS, then press SOS again without reconnecting. The second event latches `g_sos_active` and flashes the LEDs, but the transmit gate `!g_sos_acked` remains false: **no second alarm is sent**. The server's assist-clear path explicitly queues CANCELSOS without requiring a reconnect (`server/protocol_GT06.py:3530`).

**Fix:** clear the delivery/ACK state on every newly latched SOS, and reset it when cancelling the event. Associate ACK acceptance with alarms actually sent for the current event so a delayed ACK from an earlier event cannot acknowledge the new one. Add a two-press test with release, ACK and CANCELSOS on one continuous connection; the current SOS test covers only the first event.

### P1-02 — Offline night sleep re-enters sleep before making its promised wake-up connection attempt

**Code:** [main.c:3894](../../gt06/Firmware/standalone/main.c:3894), [main.c:3928](../../gt06/Firmware/standalone/main.c:3928), [main.c:3943](../../gt06/Firmware/standalone/main.c:3943). **New regression.**

After sleep, `last_conn` is made immediately eligible, but `continue` restarts the loop **before** reconnect processing. Neither `g_conn_fails` nor another sleep-eligibility latch is reset. With `g_conn_fails>=3`, `offline_idle` is therefore still true on the next iteration, and the sleep block runs again before the connection block.

**Failure:** a registered unit in night idle has three failed server connections because the server/PDP route is unavailable. It begins sleeping. The route/server recovers while registration stays at 1/5. On every wake it sleeps again without trying TCP, so it cannot receive the morning command to leave night idle. The registration-edge reset at lines 3878–3884 does not help when registration never changed. No-data accounting deliberately excludes most of this time, so it supplies no prompt escape.

**Fix:** make wake-up grant an explicit connection-attempt phase before another offline sleep is allowed. For example, a wake latch must remain set through socket creation and connect resolution and be cleared only after that attempt succeeds or fails. Merely setting `last_conn` is insufficient because of branch order.

**Test gap:** `test_offline.py:56` restores SIGUSR1 coverage, which also changes the HAL's registration from 0 to 5. That edge clears `g_conn_fails`, masking this bug. Test a server/data-route failure with registration held at 5 and assert one real connection attempt after every wake.

### P1-03 — FOTA's new header predicate still marks a non-decodable package pending

**Code:** [main.c:3118](../../gt06/Firmware/standalone/main.c:3118), [main.c:3125](../../gt06/Firmware/standalone/main.c:3125), [main.c:3130](../../gt06/Firmware/standalone/main.c:3130). **Incomplete fix of previous P1-03.**

`lzma_ok` checks a properties-byte range and the advertised uncompressed size. It does not establish that a compressed stream exists, can produce that size, or produces an application descriptor. It even accepts an advertised output smaller than the 32-byte descriptor. The documented resident `FLASH_VERIFY` contract checks wrapper/CRC, not successful decompression/application validity (`sapi.h:231–245`).

**Verified counterexample:** use payload hex `5d00100000200000000000000000` (14 bytes): ordinary LZMA properties, dictionary 4096, advertised output 32, and only one compressed byte. Set wrapper `clen=14` and stored CRC `0x305c52a1`. The current size/CRC/LZMA predicates pass. A standard LZMA decoder rejects it as truncated. The firmware reaches `fcls()`, which makes this unusable package pending; a CRC-only resident verifier also accepts it. The exact boot-loader failure/rollback outcome remains unproven, so this report does not claim a hardware-demonstrated brick.

**Fix:** verify the stream and the decoded descriptor/entry/address bounds before setting pending, or constrain installation to an independently validated expected image/manifest that binds the complete payload. Enforce the actual application RAM limit rather than accepting any advertised output up to 0x40000; this standalone layout reserves the sAPI area starting 0x32000 bytes above its load base. Header checks alone cannot substantiate “never commit a bad image.”

**Test gap:** `test_fota_fail.py:103` uses `b'x'*4000`; that fails the advertised-size checks, so it does not distinguish a real decoder/descriptor validation from this weak header predicate. Add a valid-looking header with truncated stream and a decodable payload with an invalid descriptor. SITL renames the staged file and re-execs the existing executable (`sitl_hal.c:910–923`); a passing FOTA test does not boot the downloaded image.

### P1-04 — The boot watchdog still starts after potentially blocking filesystem calls

**Code:** [reliability.c:124](../../gt06/Firmware/standalone/reliability.c:124), [reliability.c:145](../../gt06/Firmware/standalone/reliability.c:145), [reliability.c:149](../../gt06/Firmware/standalone/reliability.c:149). **Incomplete fix of previous P1-02.**

Moving `reliability_init()` earlier protects subsequent boot work, but that function opens/reads/closes `wdstat.bin` and opens/writes/closes it again before creating/starting the timer. The 90-second deadline exists only inside the timer callback.

**Failure:** a resident filesystem operation hangs while reading or rearming the boot record. Uptime stays at 1; no timer callback runs and no boot deadline can reset the unit. This remains an unbounded startup hang. Timer allocation success also sets `timer_ok=1` without observing that the timer ever ticks; a timer-start failure is not covered by the create-failure retry.

**Fix:** start the timebase/watchdog before optional boot-record I/O, keeping the finite unprimed deadline active through that I/O. Verify that the timebase advances before treating initialization as successful, or use a checked start API if its real ABI provides status. A failure inside timer creation itself remains a bootstrap limitation and should be described honestly.

**Test gap:** `SITL_HANG_BOOT` blocks NetworkInit, which runs after timer initialization, so `test_watchdog.py:46` does not exercise this remaining gap. The timer HAL only injects creation failure, not failure to start/tick.

### P2-01 — Fast blind replay silently overwrites fixes that have not been confirmed delivered

**Code:** [main.c:177](../../gt06/Firmware/standalone/main.c:177), [main.c:186](../../gt06/Firmware/standalone/main.c:186), [main.c:934](../../gt06/Firmware/standalone/main.c:934). **New gap in the delivery-protection fix.**

The blind ring holds 300 fixes, but `g_unack` holds only 150 and overwrites its oldest entry when full. Replay moves fixes from blind to unack without reserving capacity or obtaining delivery confirmation. Batched replay can move 50 fixes per RMC, often twice around a live flush, before the next 15-second heartbeat.

**Failure:** reconnect with 300 buffered fixes, then lose the route again while replay sends are accepted into the socket but the next heartbeat has not been ACKed. Replay can empty the 300-entry blind ring while retaining only fixes 150–299 in unack. On the next disconnect only those 150 can be requeued; the older half disappears even though the blind ring now has space. A pure simulation of these exact head/count/overwrite rules returned precisely that result. This is separate from the documented oldest-drop policy when the blind ring itself fills with new captures.

**Fix:** reserve unack capacity before removing fixes from blind. Pause replay when it is full and use a heartbeat/ACK to release capacity, or size total delivery tracking for the maximum replay burst and enforce that bound. Test a full backlog followed by a second silent outage before its next ACK, not only the short outage in `test_link.py:41`.

### P2-02 — Sending a newer heartbeat invalidates a legitimate delayed ACK for the previous one

**Code:** [main.c:2867](../../gt06/Firmware/standalone/main.c:2867), [main.c:4152](../../gt06/Firmware/standalone/main.c:4152), [main.c:4078](../../gt06/Firmware/standalone/main.c:4078). **New regression.**

Only the newest heartbeat serial/mark is retained. Each successful heartbeat overwrites it even while the preceding heartbeat is pending; only an exact match releases fixes.

**Failure:** HBT=5 seconds and six-second server round-trip latency. HB0's ACK arrives after HB5 has replaced its serial; HB5's ACK arrives after HB10 replaces it, and so on. No ACK ever releases unack entries despite a working, in-order TCP connection. Incoming frames keep refreshing `g_last_rx_ts`, so liveness does not reconnect. The ring eventually overwrites entries; if GNSS subsequently goes silent, its reboot escalation is also blocked indefinitely by the falsely nonempty unack ring. The default interval has the same problem when latency exceeds 15 seconds.

**Fix:** retain outstanding heartbeat serial-to-mark mappings, or keep the oldest tracked heartbeat until it is ACKed and track a later one afterward. Never replace the only outstanding delivery proof. Test ACK delays spanning multiple heartbeat intervals; the helper currently ACKs immediately.

### P2-03 — Night-sleep bookkeeping can abandon the second half of no-data CFUN recovery

**Code:** [main.c:2826](../../gt06/Firmware/standalone/main.c:2826), [main.c:2830](../../gt06/Firmware/standalone/main.c:2830), [main.c:3895](../../gt06/Firmware/standalone/main.c:3895), [main.c:3927](../../gt06/Firmware/standalone/main.c:3927). **New regression.**

`nodata_recovery()` uses `g_offline_since` both as a duration baseline and as the episode identity that resets `stage`. Night sleep changes that baseline. The sleep gate has no exclusion for a pending no-data CFUN=1, and no-data recovery executes immediately before the sleep gate.

**Failure:** an eligible offline night-idle pass reaches the no-data threshold, sends CFUN=0, and enters sleep before the five-second second half. Wake advances `g_offline_since`; the next `nodata_recovery()` sees a different episode, resets `stage=0`, and forgets the pending CFUN=1. Because accumulated awake-offline time is still at least the CFUN threshold, it can send CFUN=0 again. The modem can remain off until a different recovery path intervenes. More generally, every sleep incorrectly resets the “once per offline episode” state.

**Fix:** use a stable episode identifier and a separate accumulated awake-offline duration; never sleep with any radio-off/on second half pending. Serialize the PLMN and no-data radio-cycle ownership as well. Model CFUN state in SITL: currently `sitl_hal.c:559` only emits a CEREG URC, while the getter at line 787 still reports registration from unrelated signal flags. Consequently forgetting CFUN=1 does not actually leave the simulated modem off.

### P2-04 — TIMER transitions still send a live batch ahead of an older backlog

**Code:** [main.c:4039](../../gt06/Firmware/standalone/main.c:4039), compared with the repaired periodic path at [main.c:4114](../../gt06/Firmware/standalone/main.c:4114). **Incomplete fix of previous P2-09.**

The periodic batch flush now queues live fixes behind an undrained blind backlog. The `changed`/TIMER transition flush still directly sends `g_batch` without checking `g_blind_count`.

**Failure:** reconnect with a full backlog, replay only the first bounded portion, and receive TIMER to idle while a fresh partial live batch exists. The transition sends that newer batch immediately; idle replay subsequently sends the older remaining fixes. Wire order is still reversed, and the server updates `last_ts`/position from each arriving batch (`server/protocol_GT06.py:2355`).

**Fix:** route transition flushes through the same backlog-first rule: spill the partial live batch behind retained older fixes whenever backlog remains. Assert raw transmitted timestamp order across a TIMER transition during a full-buffer catch-up. The day-gap test sorts timestamps into a set (`test_link.py:177`), so it cannot detect ordering errors.

### P2-05 — Backward GPS-time steps are still encoded as newer timestamps during blind replay

**Code:** [main.c:774](../../gt06/Firmware/standalone/main.c:774), [main.c:915](../../gt06/Firmware/standalone/main.c:915), [main.c:4277](../../gt06/Firmware/standalone/main.c:4277). **Incomplete fix of previous P2-10.**

The new full-date calculation correctly detects forward day gaps. It returns zero for a backward step, however, and blind serialization splits only when the unsigned delta exceeds 120 seconds. Offline capture puts fixes straight into blind without the live batch's backward-time check.

**Failure:** while offline, fixes arrive at 2026-11-08 12:00:00 and then 11:59:59 after a receiver clock correction. Replay packs both into a frame based at 12:00:00 with offset zero for the second; its original timestamp has been changed. A backward date correction can shift it by a day or more. Continuing fixes from the earlier time remain folded into the newer base until the frame boundary.

**Fix:** distinguish backward/invalid time from a real zero delta. End a blind batch before a backward timestamp and start the next frame with that fix's original date/time. Check adjacent timestamps too, not just base-to-fix offsets. Add backward-time and backward-date tests as well as the existing forward-day jump.

### P2-06 — Command-time config writes can overwrite the good backup with the primary rejected at boot

**Code:** [main.c:2015](../../gt06/Firmware/standalone/main.c:2015), [main.c:2035](../../gt06/Firmware/standalone/main.c:2035), [main.c:2175](../../gt06/Firmware/standalone/main.c:2175), [main.c:3724](../../gt06/Firmware/standalone/main.c:3724). **Gap exposed by the new boot fallback.**

Boot selects a file with valid TERIID, endpoint and port, falling back to the bank. Command-time `cwprm_read()` still always reads primary and validates only TERIID. It does not read the configuration boot actually selected.

**Failure:** primary has a 15-digit TERIID but a corrupt endpoint/zero port; bank is fully valid and boots the unit at a configured non-default server. A subsequent APN change reads primary, changes only APN, verifies that those same corrupt endpoint bytes were written, then copies them into bank. Both copies now have the bad endpoint; reboot loses the previously working server. A primary with invalid TERIID instead makes ordinary server/APN repair commands return `SET ERROR: cfg` despite a usable selected bank.

**Fix:** share the validated primary/bank selection routine between boot and read/modify/write operations, or explicitly restore the selected good copy before accepting changes. Preserve the raw-read exception only for intentional TERIID onboarding. Primary-first write/readback ordering is otherwise reasonable: it preserves a previous bank when primary write/readback fails.

**Test gap:** the corruption test changes only TERIID and stops after checking boot identity. Test endpoint-only corruption followed by an unrelated setting write and another boot. SITL also reconstructs bank from primary on persisted restart (`sitl_hal.c:713–716`) rather than persisting two independent copies, so it cannot establish real two-file power-loss behavior.

### P3-01 — SEND_BUSY silently discards command replies instead of retrying them

**Code:** [main.c:2900](../../gt06/Firmware/standalone/main.c:2900), [server/protocol_GT06.py:1717](../../server/protocol_GT06.py:1717), [server/protocol_GT06.py:2589](../../server/protocol_GT06.py:2589). **New response-loss path.**

For command responses, SEND_BUSY is treated as success, but `resp` is stack-local and no retry queue retains it. The command's effects still execute. Unlike fix replay or heartbeat/SOS retry, nothing retries that response once the socket becomes writable.

**Failure:** a short period of send pressure overlaps CXCS#BOOTSTAT or a readback/config command. The reply vanishes. The real server advances after ten seconds without an app ACK; it does not resend that command. Its once-per-connection `bootstat_queried` flag stays true, so that boot's reason may never be logged. RESET/FOTA can also begin despite their response never being queued.

**Fix:** retain bounded pending command replies and send them when writable, preserving response order. Defer reset/update actions until their reply was sent or a deliberate delivery deadline expires. Add a test with transmit pressure overlapping a query, then remove pressure before the link-busy deadline and require exactly one eventual reply.

### P3-02 — A rejected HBT command can still change the heartbeat settings

**Code:** [main.c:1810](../../gt06/Firmware/standalone/main.c:1810), [main.c:2899](../../gt06/Firmware/standalone/main.c:2899), [main.c:2929](../../gt06/Firmware/standalone/main.c:2929). **New rejection/action inconsistency.**

`cmd_recognized()` rejects the complete HBT command when either supplied interval is invalid, but the later application code independently applies each valid field. There is no shared validated command result.

**Failure:** send a valid-CRC `HBT,1,0#`. The tracker responds `HBT,1,0# ERROR` because OFF=0 is rejected, yet changes the tracking heartbeat to one second. An operator/server treating ERROR as no change is now wrong; the extra uplink cost can persist. `HBT,3601,300#` similarly returns ERROR while changing OFF to 300. The normal server's positive symmetric HBT commands are compatible; this is an invalid-argument error path.

**Fix:** validate once and use the same result for both reply and mutation. If partial updates are intentionally supported, explicitly define and report that behavior instead of returning a whole-command ERROR. Extend the malformed-command test beyond `HBT,0,0#`, which happens to change neither field.

## Checks that passed, and limits that remain

- **Watchdog callback:** no USB/FS operations remain in `wd_tick()`. Its normal first-feed/stall logic and bounded night grace are coherent once the timer is running. `wd_alive()` before revoking grace allows wake cleanup to be watched. Uptime starting at 1 correctly eliminates the previous immediate unsigned-underflow cases for zero-as-unset timestamps; I found no new ordinary-uptime wrap bug. BOOTSTAT deliberately reports unexpected resets as rr0 rather than claiming to distinguish watchdog from brownout.
- **GT06 CRC and normal server commands:** the CRC region, ACK lengths and serial extraction agree with `gt06_make_command()`/`gt06_make_response()` in `server/protocol_GT06.py:512–536`. The server's standard `TIMER,a,b#`, `HBT,a,b#`, RESET# and CANCELSOS# forms are accepted by the new helpers. Numeric command parsing avoids signed overflow with its six-digit limit. Command replies remain unqueued on SEND_BUSY, and invalid HBT application is inconsistent with its ERROR response, as described above.
- **Every GT06 send site:** login treats BUSY as a failed connection attempt (safe, paced); blind replay leaves unsent entries in place; failed/busy batches spill to blind; legacy fixes are captured on BUSY; heartbeat and SOS retry without advancing their sent timestamps. Forwarding is deliberately passed `send_traced` instead of `send_gt06`. The only directly lost GT06 send object is the command reply discussed above.
- **Date math:** independently checked the `dt_secs()` expression against all 36,525 valid Gregorian dates from 2000 through 2099, including leap days. Forward midnight/month/year and whole-day gaps work. It is not a full datetime validator (month-specific day limits and time fields are unchecked), and backward blind serialization remains wrong as described above.
- **Requeue ordering:** for a non-full blind ring and an ordered unack ring, pushing unack entries newest-first to the front restores their FIFO order correctly. `g_unack_pushed/released` arithmetic accounts for releases and eviction consistently. Full blind capacity deliberately refuses older front insertions; the report does not call that documented finite-buffer limit an index bug. Accelerated unack eviction and the transition-flush bypass are separate problems.
- **Liveness:** `3*HBT+15` uses time since **any valid inbound frame**, not time since the last heartbeat ACK. This detects a fully silent peer but should not be described as an independent heartbeat-ACK deadline. At HBT=15, the nominal silent-link threshold is just over 60 seconds, plus loop latency; longer idle HBT increases it. BOOTSTAT rr0/bc/up fields and the new one-query server logic are consistent; the two fake-socket tests passed.
- **GPS recovery:** silence triggers actual OFF/ON cycles rather than endless ON assertions; checksum-good NMEA resets the cycle count, and the reboot gate protects SOS/blind/unack data and prevents a consecutive RR_GPS_DEAD reboot loop. Idle now records received NMEA chunks before discarding them, repairing the old fallback evidence bug. The two-second OFF dwell is shorter than the approximately three-second asynchronous power transition documented at `main.c:1091`; the subsequent ten-second ON retries mitigate this, but synchronous SITL power state is not proof that each hardware cycle completes. The delayed-heartbeat ACK defect can also prevent GPS escalation by leaving unack falsely occupied.
- **FOTA transfer bounds:** nonblocking connect polling, HTTP 200/206 filtering and overall/stall deadlines address the earlier unbounded trickle/download loop. Split wrapper/header handling and the compressed-size/CRC cap are internally consistent. The absolute deadline is only checked between calls: `FLASH_PREPARE`, the request send, flash writes/close/verify and VCOM diagnostics can still block long enough to invoke the watchdog. `fcls()` still precedes resident re-verify and there is no proven cancellation of its pending marker on post-close failure. These are remaining hardware/API limitations, not evidence that the new deadlines cannot bound the cooperative receive loop.

## Hardware differences and battery cost

1. **Select is not proof of a bounded blocking call.** `send_gt06()` still restores the GT06 socket to blocking mode and assumes the resident's send-low-water threshold covers a complete frame (`main.c:3158–3181`, `3982–3984`). Select tests a readiness/event condition, not a reservation of all pbufs, TCP segments or resident IPC capacity. The supplied tree does not establish that threshold and every blocking resource in the send path for this exact resident; I cannot verify the comment's absolute “cannot block.” SITL's Linux select plus `SITL_TX_STUCK_FLAG` only tests BUSY propagation. The explicit hung-send test proves watchdog reset/re-exec, which loses RAM fixes; it does not prove send cannot hang. Verify the resident conditions or use proven nonblocking semantics rather than relying on MSG_DONTWAIT/SO_RCVTIMEO, which this resident is known to ignore.
2. **Five-millisecond units are now represented consistently.** Main queue waits use `MQ_TICKS()` and HAL waits multiply ticks by five. Offline/no-NMEA waits remain five seconds and forwarding waits 100 ms in that branch, preserving prior hardware behavior. Connected idle uses a one-second socket select. Those waits consume real latency/watchdog budget; shortened SITL recovery constants do not validate production-scale timing. The resident timer's behavior while automatic module sleep is granted also remains a hardware assumption: SITL keeps an ordinary host timer thread ticking.
3. **Idle cost is reduced, not established as low power.** Removing per-second watchdog USB output and backing off repeated VCOM stalls are improvements. Offline TCP retries now cap at 300 seconds, but the app still wakes about every five seconds, polls the M0 every five seconds and operator status every 30 seconds. Ordinary connected idle still has a one-second app wake, GNSS status checks and default HBT=15 (240 heartbeats/hour); automatic module sleep is granted only in the deliberate night path. `cxzt` still hardcodes `*H:15` (`main.c:1924`), so use actual HBT commands/wire timing for power comparisons. PLMN holdoff drops from an hour to 15 minutes, trading faster permitted-carrier recovery for more radio cycles/selection activity. No measured current or deep-sleep benefit follows from SITL success.
4. **No-data policy:** the idle reboot delay doubles from 40 to 80, 160 and then 320 minutes using persisted state, clearing on a successfully written login. The code intentionally applies CFUN to offline episodes even if unregistered/no-SIM; it is not limited to registered data failures despite the README wording. A TCP connection/login send is treated as recovery before the login ACK, so these counters measure socket/login-send success rather than confirmed application service. Avoid inferring a healthy server from nd0 alone.
5. **SITL still cannot validate resident memory pressure or update bootability.** URC payloads come from a static ring rather than separately allocated resident objects; no leak proof follows. Filesystem close/write failures and asynchronous radio/GNSS completion are weakly modeled. Reset re-execs the same host binary and clears injected process-local radio faults. `test_offline.py:43` calls SIGUSR1 a registered data failure, but the getter at `sitl_hal.c:787` actually makes it unregistered; its successful reboot/recovery therefore does not prove the registered-but-broken-bearer case or successful CFUN recovery. Keep registration and data/radio failure injection independent.

Deliberate night-sleep SOS polling remains unchanged by the user's decision and is **not** reported as a finding.

## Reviewed-source fingerprints

The substantive files remained unchanged through the final checks:

- `main.c`: `a8b7a4815c45cdef3478647d7676ec8b0536a8fcf01bb777eb9d4efa75cc9830`
- `reliability.c`: `ada13385a55c83603c1615c0ef81174d333efa247e2badf18a389d67eea9808d`
- `sitl_hal.c`: `6e516c75b60bf5b4f0844fb10488c33419d0a97e097837f0b3c287a84f5b0ee7`
- `server/protocol_GT06.py`: `4b8f0d4641761ec2bf917e5ba3bdb4ffad3ec57093eb287e12329f167eeafc00`

## Re-check

Rechecked only the twelve findings above and changes made to address them. **Eight fixed; four partially fixed/mitigated; one new battery-cost regression.** References in this section are to the updated working tree; earlier sections retain their original line numbers. ARM and SITL warnings-as-errors syntax checks, Python AST checks of the updated helpers/tests, and `git diff --check` passed. No socket-based tests, devices or servers were contacted; implementation files were not changed.

| Finding | Verdict | Current code / reason |
| --- | --- | --- |
| P1-01 | **Fixed** | New presses and CANCELSOS reset the ACK/event state (`main.c:1652`, `2994`); ACKs require an alarm sent for the current event (`2942`), and reconnect resets that state. The second-press test exercises release/ACK/cancel/press on the same connection. |
| P1-02 | **Fixed** | `g_wake_attempt` excludes immediate re-sleep and stays set through connect resolution (`main.c:3958`, `3992`, `4017–4054`). The registered/server-down test avoids the registration edge that masked the old bug. The new backoff interaction below still delays the attempt. |
| P1-03 | **Partially — documented mitigation** | `main.c:3200–3201` correctly requires props 0x5D, advertised output 0x1000..0x32000 and clen>=64. The old 14-byte counterexample is rejected. CRC-valid truncated streams with an acceptable header can still pass; README explicitly records this. This matches the stated mitigation, not decoder/descriptor validation. |
| P1-04 | **Partially** | `reliability.c:128–142` now starts the timer and observes a tick before boot-record I/O: the normal startup gap is fixed. However, when create/start/tick verification fails, execution still enters filesystem I/O at `151–163` before returning `timer_ok=0`. A hung file operation then prevents the caller's timer-failure reset/backoff with no watchdog running. Return through a no-FS failure path immediately when `ok==0`. |
| P2-01 | **Fixed for the reported full-backlog replay failure** | UNACK_CAP=320 exceeds BLIND_CAP=300 (`main.c:177`); both replay paths reserve room before sending/removing fixes (`444`, `951`). A 300-fix replay no longer evicts half the backlog. Retention remains finite: live sends still use the ring's overwrite-on-full behavior at 320 entries; this is not unlimited delivery storage. |
| P2-02 | **Partially** | Keeping the oldest outstanding heartbeat fixes ACK delay greater than one HBT, including the new 12-second/5-second test. But `main.c:4221` replaces it after three HBTs, recreating the same mismatch for longer valid delays; see the concrete trace below. |
| P2-03 | **Fixed** | `g_offline_episode` is separate from the sleep-adjusted baseline (`main.c:2888–2898`). An outstanding no-data CFUN=1 is completed before episode/connected early returns; both PLMN/no-data pending bits exclude sleep (`2702–2706`, `2894–2902`, `3961`). Sleep no longer erases the second half. The HAL's weak CFUN model remains a test limitation, not evidence against this source fix. |
| P2-04 | **Fixed** | The TIMER transition now spills a partial batch behind existing blind backlog before considering a live send (`main.c:4104–4116`). The periodic path retains the same backlog-first rule. |
| P2-05 | **Fixed** | Blind serialization checks adjacent timestamps and ends the frame before a backward step (`main.c:917–927`), so the next frame uses that fix's original base. The new backward-time test inspects transmitted order without sorting it away. |
| P2-06 | **Fixed** | Boot and ordinary command read/modify/write now share `cwprm_select()` (`main.c:2051–2066`), preferring a fully valid backup over the rejected primary. The raw TERIID-onboarding exception remains explicit. Verified primary-first write/readback ordering is preserved. |
| P3-01 | **Partially** | One BUSY reply is retained and retried (`main.c:2974–2978`, `3927–3932`), and RESET has a bounded wait. A second command while that slot is occupied still loses its reply; FOTA also runs before pending-reply retry. Details below. |
| P3-02 | **Fixed** | The same `cmd_recognized()` result now gates TIMER/HBT mutations (`main.c:2980–2981`, `3004`). `HBT,1,0#` returns ERROR without changing either interval; the new LINKSTAT test checks the actual interval globals. |

Remaining gaps and the new regression:

- **P2-02 remains for ACK RTT above the replacement window:** [main.c:4221](../../gt06/Firmware/standalone/main.c:4221). With HBT=5 and RTT=21 seconds, HB0 is replaced by HB20 before ACK0 arrives at second 21; HB20 is replaced by HB40 before ACK20 arrives at second 41. This repeats forever. Incoming ACKs still refresh link liveness (the silence threshold is 30 seconds), but none releases unack fixes. A receive-before-send state trace over 100 seconds matched six ACKs with RTT=12, **zero with RTT=21**. Retain the outstanding serial/mark until ACK or reconnect, or keep mappings for multiple outstanding heartbeats; expiry should not silently discard the only usable proof while retaining the connection. The new 12-second test does not cover this case.
- **P3-01 still drops the second deferred response:** [main.c:2974](../../gt06/Firmware/standalone/main.c:2974). When `g_reply_len` is nonzero, the second command is treated as BUSY, but the enqueue condition requires `!g_reply_len`, so its stack-local response vanishes while its effects execute. This is reachable with coalesced commands or when the real server advances its queue after ten seconds while TX remains busy for less than the 20-second link deadline. Use a bounded reply FIFO or stop dispatching further commands until space exists, retaining their bytes. Also gate queued FOTA on reply delivery/a bounded deadline: [main.c:3916](../../gt06/Firmware/standalone/main.c:3916) invokes the downloader before the retry at line 3927, so a BUSY YCSJ reply can remain unsent through the download and reboot. RESET's five-second wait does not repair either case.
- **New P2 — Offline wake can spend nearly five minutes awake before its attempt:** [main.c:3993](../../gt06/Firmware/standalone/main.c:3993), [main.c:4008](../../gt06/Firmware/standalone/main.c:4008). Wake sets `last_conn=now-5`, but eligibility still uses `conn_retry_sec()`, up to 300 seconds; the wake latch prevents returning to sleep throughout the remaining **295 seconds**. A registered unit with repeated server failures therefore spends almost five minutes awake per night check-in, rather than making an immediate attempt. SITL caps this backoff at 20 seconds, so the new test can pass after only 15 seconds of extra awake time. Let `g_wake_attempt` override the elapsed-backoff gate, or subtract the actual selected retry interval on wake. Keep the latch until the attempt resolves.

`CXCS#LINKSTAT` fits the existing response/scratch buffers and exposes the globals the new tests intend to inspect. No additional regression was found in its builder. Deliberate night-sleep SOS polling was left out of this re-check as requested. Reviewed updated source SHA-256: `main.c` = `552f5c1b60697f0f6ca8e573bd7356d78b5c2287b9dcea0e5acc57095f447b56`; `reliability.c` = `589909e30268f5d5f741991c83ab7d6d1b55bb455d8c5c7f6395b64cf64d7b4c`.
