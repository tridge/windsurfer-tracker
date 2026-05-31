# GT06 current issues — reconnect data-loss (G334189 / G334023)

_Investigated 2026-05-30 (event 8, "Saturday Sail"). Trackers all healthy on the
water; the data loss is server-side._

## Symptom

Two trackers "stopped recording on the far side of the lake" mid-sail despite
good GPS, cell signal (sig 4/4) and battery (96–98%):

- **G334189** — track frozen at the single fix `gps_time 13:56:12` for the rest
  of the session, even though the device kept sending valid 1 Hz LOC packets.
- **G334023** — track frozen at `15:00:04`; device sat in 15 s "GPS-wait"
  heartbeats afterwards.

Both are NT19D_V667 firmware. Device hardware was fine throughout.

## Root cause

A brief TCP dropout (cellular blip on the far shore) → reconnect, which trips a
chain of three server-side issues. Reconstructed from `gt06.log` (raw protocol,
8 dp + embedded `gps=` time) and `tracker.log` (per-packet verdicts).

### Timeline (G334189, local AEST)

| Time | Event |
|---|---|
| 13:56:03.725 | Last live LOC on conn `c9` (`gps=13:56:02`, sats 15). Device then goes off-air. |
| 13:56:36 | Server watchdog: "No heartbeat for 45 s — re-queuing HBT", sends `HBT,15,15#`. |
| 13:56:40 | TCP write to the dead socket fails → **Disconnected**. |
| 13:56:53 | Device **reconnects** (conn `c30`). |
| 13:56:53–57 | Login bootstrap re-runs `_active_cmds`: `SLPDISCONNECT=0`, `TIMER,1,1#`, `SENDS,0#`, `GPS_RST_TIME=300`, `VIBCHK=0:16`, `HBT,15,15#`. |
| 13:56:57.211 | First replayed buffered LOC, `gps=13:56:12` (sats 6) → **accepted = last fix in track**. |
| 13:56:57 | GPS-wait heartbeat fires, logged with `ts = now ≈ 13:56:57`. |
| 13:56:57+ | Every later buffered LOC (`gps=13:56:13…`, climbing 1 s/s but ~45 s behind wall-clock) → **all `[DUP]`, dropped**. |

### The three compounding defects

1. **nogps heartbeats poison the LOC dedup high-water mark.** GT06 packets carry
   no sequence number (sq=0), so the server dedups on timestamp
   (`tracker_server.py`): `is_dup = ts <= last_timestamp[sailor]`, advanced only
   on a non-dup. Real LOC are stamped with the device's embedded `gps_time`, but
   the **GPS-wait heartbeat** path (`protocol_GT06.py`, fires when no LOC for
   ≥15 s) logs a nogps point with **`ts = int(time.time())`** (server wall-clock)
   through the *same* dedup. After a reconnect the device replays a buffered
   backlog whose `gps_time` lags wall-clock by ~45 s; the heartbeat shoves the
   high-water mark to "now", so every real backlog fix then fails
   `ts <= last_timestamp` and is silently dropped. The 15 s heartbeats keep
   re-stamping "now", so the device can never catch up. **This is what froze the
   track** — the device was streaming valid 1 Hz fixes the whole time.

2. **Reconnect re-runs the full active bootstrap.** `_active_cmds`
   (`TIMER`/`SENDS,0`/`GPS_RST_TIME=300`/`VIBCHK`) is re-sent on *every* login,
   needlessly reconfiguring a tracker that is already reporting. Defensive issue
   rather than the proven cause here (the buffered `gps=13:56:12` fix was already
   sats 6, so the GPS degradation happened during the dropout, not from the
   re-push) — but for G334023, which went fully silent on LOC after reconnect,
   the re-bootstrap is the likely culprit.

3. **Monotonic timestamp dedup can't accept out-of-order buffered replay.** Even
   without (1), a device replaying a time-stamped backlog delivers `gps_time`s
   behind the latest accepted one; the monotonic rule rejects them. Lower
   priority — fixing (1) recovers the data in practice.

### Note: the `HBT,15,15#` OUT packet is NOT the disconnect trigger

`0e000000004842542c31352c313523` = a 0x80 command, payload `HBT,15,15#`. It is
the "no heartbeat for 45 s" watchdog writing to an already-dead socket (device
went off-air ~33 s earlier); the failed write surfaces the dead link. Messenger,
not cause.

### Aside: routine duplicate sends

With LOC lat/lon at 8 dp and the new `gps=` column, gt06_dump shows the device
normally sends ~2 LOC/sec with the *same* `gps_time` (byte-identical lat/lon);
the server correctly drops the second as `[DUP]`. Harmless, unrelated to the
data loss.

## Proposed fixes (drafted, not yet deployed)

All three edits below are in the working tree. `py_compile` clean;
`pytest test/test_gt06_sim.py` → 12 passed (no regression; reconnect/backlog not
yet covered — see tests to add).

### Fix 1 — nogps heartbeats must not advance the dedup high-water mark (high impact)

`server/tracker_server.py`, the `no_gps` branch (~line 1095): remove the
`self.last_timestamp[sailor_id] = ts` / `last_sq` assignment. The branch never
used it for its own dedup (it always writes the nogps entry); the only effect was
poisoning real-LOC dedup. Replaced with an explanatory comment.

```python
if no_gps:
    with self._lock:
        # Deliberately do NOT advance last_timestamp/last_sq here. nogps
        # heartbeats are stamped ts=server-now while real LOC carry the
        # device's embedded gps_time; after a reconnect a buffered backlog
        # whose gps_time lags wall-clock would all be dropped as [DUP] if a
        # heartbeat moved the high-water mark to "now". (G334189/G334023.)
        existing = self.current_positions.get(sailor_id, {})
```

### Fix 2 — reconnect probes instead of re-bootstrapping an already-active tracker

`server/protocol_GT06.py`, mirroring the existing overnight `cxzt#`-gated pattern.

- `GT06Connection.__init__`: add `self.want_active_interval = None`.
- **Active login branch**: send `["cxzt#", "SZCS#SLPDISCONNECT=0", "HBT,15,15#"]`
  (probe + idempotent keep-alives) and set
  `gt_conn.want_active_interval = self.interval`, instead of
  `["cxzt#"] + _active_cmds(self.interval) + ["HBT,15,15#"]`.
- **cxzt# handler, non-overnight branch**: after the mode check, if
  `want_active_interval is not None`, apply `_active_cmds(want)` **only if** the
  device's reported `F:` (TIMER interval) ≠ active interval; otherwise log
  "already active-configured — not re-pushing". Clear `want_active_interval`.

Effect: a mid-race reconnect of an already-reporting device (`F:1`) no longer
re-toggles `GPS_RST_TIME=300`/`SENDS,0`; a genuine idle→active device (`F:540`)
still gets the full config via the same cxzt path.

Caveat: if a cxzt# response ever omits `M:`/`F:`, the active config won't be
(re)applied — acceptable since these devices reliably report both, and it matches
the overnight branch's existing assumption. Consider a fallback if that proves
flaky.

### Fix 3 (optional, lower priority) — accept out-of-order buffered replay

Reconsider the monotonic timestamp dedup so that previously-unseen `gps_time`s
delivered out of order after a reconnect are accepted (e.g. keep a short set of
recently-seen gps seconds instead of a single high-water mark). Not needed once
Fix 1 lands, but would harden against future buffered-replay edge cases.

## Tests to add (gt06 simulator)

1. **Dedup / nogps poisoning (covers Fix 1).** Feed live LOCs; inject a nogps
   GPS-wait heartbeat (stamped server-now); then deliver buffered LOCs with
   *older* `gps_time`s. Assert the buffered fixes are written to the track
   (not `[DUP]`) and the track's last fix matches the latest backlog fix.

2. **Reconnect config gating (covers Fix 2).**
   - Active device reporting at `F:1` reconnects → assert the server queues
     `cxzt#` + `HBT,15,15#` (+ `SLPDISCONNECT=0`) but **not** `GPS_RST_TIME=300`
     or `SENDS,0#`.
   - Idle device at `F:540` reconnecting into a tracking event → assert it
     **does** receive `_active_cmds` (full active config) after the cxzt# probe.

## Tooling changes made during investigation

`scripts/gt06_dump.py`:
- LOC/ALARM lat/lon precision raised 4 → 8 dp (reveals byte-identical duplicate
  sends).
- Added a `gps=HH:MM:SS` column = the embedded GPS fix time (packet UTC →
  local), so the receive-vs-fix lag (the 45 s buffered-replay signature) is
  visible at a glance.

## Related

- Memory: `project_reconnect_clobbers_active_mode` (exact dedup-poisoning trace).
- `project_w07c_gps_power` (GPS_RST_TIME/VIBCHK semantics),
  `project_w07c_new_firmware`, `reference_w07c_undocumented_commands`
  (SENDS/SLPDISCONNECT).
