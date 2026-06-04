# GT06 lag remediation — bench-test runbook (Phases 0 & 1)

Goal: reproduce the post-reconnect replay "lag" on the bench, then find which
device command makes the live position current again **without** a full reboot.
The candidate commands (`BLINDINIT#`, `SZCS#BLINDSPD`, `BLINDINFO#`) are
undocumented (firmware strings only) — this validates their real behaviour per
firmware before any server automation. See plan + `current_issues.md`.

Run on **one device at a time**. Test each firmware: **V6.63** = G226122,
a **V6.68** (e.g. G375356), a **V667** (e.g. G334189).

Helpers (already in repo):
- `scripts/simbase_manage.py {status|disable|enable} [--iccid <ICCID>|--tag W07C]`
- `scripts/gt06_command.py <Gid> "<cmd>" [-e 8]` — send a command via the server
- `scripts/gt06_dump.py --imei <last6> <gt06.log>` — `gps=` column shows fix-time
  vs the line's receive time → the **lag** is `receive − gps`.

## Phase 0 — reproduce the lag

1. **Identify the device.** GPS-locked (outdoors / sky view), connected, tracking
   live. Get its ICCID:
   `scripts/gt06_command.py G226122 "cxzt#"` then read the `*ICCID:` field in
   `ssh wstracker "grep -a G226122 tracker/tracker.log | tail"` — or
   `scripts/simbase_manage.py status` (lists ICCID + tags).
2. **Baseline (lag ≈ 1–2 s).** Watch a few seconds of live fixes:
   `ssh wstracker "grep -a 'G226122] pos=' tracker/tracker.log | tail"` — the line
   timestamp and the `time=<gps>` should be ~1–2 s apart.
3. **Cut the data path** (device keeps GPS, can't uplink → fills the blind buffer):
   `scripts/simbase_manage.py disable --iccid <ICCID>` — leave it **~60–120 s**
   (longer = bigger backlog = clearer test).
4. **Restore:** `scripts/simbase_manage.py enable --iccid <ICCID>` — the device
   re-attaches GPRS, reconnects, and starts replaying.
5. **Confirm the lag.** Fetch + dump:
   `ssh wstracker "cat tracker/gt06.log" > /tmp/g.log; scripts/gt06_dump.py --imei 226122 /tmp/g.log | tail -40`
   → after reconnect the `gps=` time should sit **tens of seconds behind** the
   line's receive time, climbing 1 s/s but not catching up. (This is the bug.)

## Phase 1 — test the levers (device now lagged)

For each, send the command, then re-dump and note the effect + time cost.

| Command | What to look for |
|---|---|
| `BLINDINFO#` | Reply `Blind Info EN:%d,Total:%d,Use:%d,Leave:%d,rp:%d,wp:%d`. Backlog depth = `wp−rp`. (This is the server's future **detect/verify** signal.) |
| `BLINDINIT#` | Does the `gps=` lag **snap to ~0** (device jumps to current)? Confirm **no** reconnect (same conn id in `gt06_dump --list-streams`), **no** GPS re-acquisition gap, **no** reboot. Note: the un-sent tail is lost → a gap in the track. Measure how fast it recovers. |
| `SZCS#BLINDSPD=<N>` | Re-induce a lag (Phase 0), set BLINDSPD, watch whether the backlog **drains fast** (lag shrinks to 0 over seconds while the track stays complete). Try a few N — find the range/units. **Best outcome if it works.** |
| `SZCS#BLIND_EN=0` | Re-induce a dropout; confirm the device sends **current** position on reconnect with **no replay** (track has a gap for the outage). Simple fallback. |
| (reference) `BLINDINIT#`/`BLINDSPD` are undocumented | Record the **exact** ACK strings — some firmwares may reject (`...FAIL`) or ignore. |

**Record per firmware** (which lever works, the ACK string, the time cost, and
whether it reconnects/re-acquires GPS). Save results into
`log_analysis/<date>/` next to the raw logs.

> **WINNER (decided by the 2026-05-31 bench, see RESULTS below): temporarily
> lower the capture rate (`TIMER`) so the fixed-rate replay outpaces it and the
> buffer drains — full track preserved AND device catches up.** `BLINDSPD` was
> ruled out (no effect 3→10, idle or active). `BLINDINIT#` / `BLIND_EN=0` remain
> untested lossy fallbacks (only needed if a faster, gap-free drain isn't enough).

## Don'ts / caveats

- **Don't** iptables-block by source IP — devices come via China-Mobile carrier
  NAT (`112.96.x`, changes every reconnect) + the SimBase proxy
  (`35.156.18.25`); you can't target one device and the IP changes on the
  reconnect you're testing. (A port-7711 DROP works only on a *dedicated
  single-device* test instance, never production.)
- SimBase re-enable → carrier re-attach can take ~30 s–2 min; that's fine (bigger
  backlog). Confirm reconnect in `tracker.log` (`Login: IMEI …`).
- Test devices one at a time so the gt06.log stream is unambiguous.

## RESULTS — 2026-05-31 bench run (G334189, NT19D V667)

Tested with the device indoors on a roof GPS repeater (live fix, stationary),
using a real 373-record backlog it had accumulated from earlier flapping. The
SimBase dropout wasn't even needed — the parked backlog was the test subject.

**Baseline BLIND params (queried on all 3 firmwares — V6.63/V6.68/V667, uniform):**
`BLIND_EN=1`, `BLINDSPD=3`, buffer `Total=2000` slots (~33 min @ 1 Hz).
`BSTORNEW` is **not** a valid CXCS# key (`ERROR` everywhere). Nothing sets these
today — they're the factory/provisioned defaults; the reconciler never touches
BLIND params.

**1. Idle = no replay.** While the device is idle the buffer is frozen (`rp`/`wp`
static); it only replays when actively uplinking positions. So remediation only
applies in active tracking (which is the only state that matters for a race).

**2. Replay cadence is hard-wired to ~1.0 record/s, independent of TIMER and of
BLINDSPD.** Capture rate = the `TIMER` interval. The buffer drains at
`replay − capture = 1.0 − capture_rate`:

| `TIMER,N,N` | capture | replay (Δrp) | **net drain (ΔUse)** |
|---|---|---|---|
| 1,1 (1 Hz)    | 1.0/s  | 1.0/s | **0/s** (stuck) |
| 2,2 (0.5 Hz)  | 0.5/s  | 1.0/s | **0.5/s** |
| 4,4 (0.25 Hz) | 0.25/s | 1.0/s | **0.75/s** |
| 5,5 (0.2 Hz)  | 0.2/s  | 1.0/s | **0.8/s** |

`Use` fell 373→243→131→103 across the ramp; `rp` held a perfect 1.0/s at every
step while `wp` tracked the TIMER rate exactly. **This is why 1 Hz never catches
up** (replay = capture) and why the "perpetual lag" is only unavoidable *at 1 Hz
capture*.

**3. `BLINDSPD` is NOT the replay-rate knob.** 3→10 changed nothing in idle
(no replay to act on) and nothing in active (replay stayed exactly 1.0/s at 1 Hz
capture, buffer flat at 103). Set/read confirmed (`SETOK`/`READOK: BLINDSPD=10`).
Whatever BLINDSPD controls, it isn't the catch-up speed in the 3–10 range.

**4. Winner: temporarily lower `TIMER` to drain, then restore.** Drains the full
backlog with **no track loss** (every buffered fix still replays) AND brings the
device current. Recovery ≈ `backlog ÷ 0.8` s at 0.2 Hz (≈ 5 min for ~240, ≈ 8 min
for ~370). Drain asymptotes to 1.0/s as capture→0, so 0.2 Hz is near the floor;
lower TIMER buys little and thins the live track further during recovery.

**Caveat (live view during drain):** the device still emits the replay *tip*
(old fixes) at 1 Hz plus sparse new captures, so the live position stays lagged
until the buffer empties, then snaps current. So the operator sees the lag
*shrink* over the recovery window. (Pair with the Phase-3 LAGGED badge.)

**5. The server is NOT the throughput bottleneck (proven).** A sim streaming LOC
with distinct, past-dated timestamps (a 2x backlog replay) is received AND
recorded at 100%: ~2 Hz → 1.83/s (the *sim's* 0.1 s loop granularity, 0 dups),
and at the sim's max rate **9.17/s with zero loss**. So the ~1 Hz replay cap is
purely device-side; the server will happily record replay (1 Hz) + capture
during the drain. Regression guard: `test_gt06_sim.py::test_server_processes_loc_at_2hz`
(uses the temp sim flags `loc_interval_override`/`loc_ts_step`/`loc_ts_start_offset`).

**6. Sub-second `TIMER` is NOT supported — can't speed the device up instead.**
`TIMER,0.5,0.5#` is integer-parsed to `TIMER,0,0#` on ALL 3 firmwares (ACK
`TIMER ACC ON:0s,ACC OFF:0s`, `cxzt# F:0|0`); 0 s is degenerate, not 2 Hz. The
minimum valid interval is 1 s (1 Hz). So you cannot out-run the fixed ~1 Hz
replay from the device side — **lowering capture is the only lever**, confirming
the design.

## IMEI → ICCID (SimBase) — for the dropout method

The `/v2/simcards` **list** endpoint already includes `imei` per SIM (no per-SIM
detail call — rapid detail polls get rate-limited). 7 of the 11 sail GT06s are on
SimBase (G378848/G375356/G312334/G375547 are NOT — different SIM/provider). The
two lag-prone units: **G334189 → `8944538532055005304`**, **G334023 →
`8944538532055005288`**. Both reach the server via the SimBase proxy
`35.156.18.25`. (`events.json` holds the per-event admin password that
`gt06_command.py` needs; the manager password does NOT authorize `gt06-cmd`.)

## Phase 2 — IMPLEMENTED (not yet deployed)

`_check_lag(fd, gt_conn, now)` in `protocol_GT06.py`, called from the periodic
`run()` loop right after `_check_rates`. In **active tracking only** (not idle /
overnight / mid-reconcile) and only while the device is **actively sending LOC**
(`last_loc_mono` within 10 s — a genuine backlog replay, not GPS-loss silence):

- **Detect:** `lag = time.time() − gt_conn.last_ts` (last_ts = replay-tip
  gps_time). If `lag > lag_remediation_sec`, push `TIMER,<lag_drain_interval>,…#`
  (**2 s = 0.5 Hz**) so capture drops below the fixed ~1 Hz replay → backlog
  drains 0.5/s, full track preserved.
- **Restore:** once `lag ≤ lag_restore_sec`, push `TIMER,<active interval>` and
  reset rate monitoring. Recovery re-arms the retry budget.
- **Throttle** (like the overnight storm guard): `lag_remediation_max_retries`
  attempts, `lag_remediation_cooldown_sec` between starts, `lag_drain_max_sec`
  per-drain timeout. Drain only starts on a quiet command pipeline.
- **Detection is passive (lag-based), no `BLINDINFO#` polling** — simpler and
  robust; `BLINDINFO#` stays a manual diagnostic.
- Slow-mode TIMER pushes are suppressed while draining so they can't fight it.

Config (`_resolve_setting` precedence per-device > firmware > global) in
`gt06.json`: `lag_remediation_sec` (**30**, 0 = disabled), `lag_drain_interval`
(2), `lag_restore_sec` (8), `lag_remediation_cooldown_sec` (60),
`lag_remediation_max_retries` (3), `lag_drain_max_sec` (180).

Sim model: `gt06_device_sim.py` `replay_lag_s` / `replay_drain_freq` /
`replay_drain_step` (stamps LOC behind wall-clock; drains the lag once TIMER hits
the drain freq). Tests: `test_gt06_sim.py::test_lag_remediation_drains_then_restores`
and `::test_lag_remediation_skips_idle`. Full suite green (232 passed, 1 skip).

**NOT deployed.** `gt06.json` ships it ENABLED at 30 s; deploy + bench-validate
(SimBase disable/enable a GPS-locked active unit, watch for the drain→restore in
`tracker.log`) before relying on it race-day. Phase 3 (live-view LAGGED badge)
still pending.
