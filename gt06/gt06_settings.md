# GT06 device settings by state

What the server sends to a GT06/W07C tracker for each of the three operating
states. Source: `server/protocol_GT06.py` — builders `_active_cmds()`,
`_idle_cmds()`, `_overnight_cmds()`, applied by the login handler (~L868–929)
and the `cxzt#` response handler (~L1215–1343).

The goal of this doc is **coverage**: every setting we ever send should have a
defined value in every state, so a device can't carry a stale value across a
state transition. The matrix below makes the gaps explicit (see
[Coverage gaps](#coverage-gaps)). The settings exist to serve the **goals** of
each state — described next, so the chosen values can be checked against intent.

## State goals

Each state trades off accuracy, battery, and responsiveness differently. The
settings are just the means to these ends:

### Tracking — maximum accuracy, now
- **Aim:** highest-quality position, **1 Hz** location, minimum latency. Battery
  cost is accepted for the duration of a race/session.
- **Implies:** GPS **on** continuously; LOC every 1 s (`TIMER,1,1`); TCP held
  open and actively streaming (`SLPDISCONNECT=0`, `HBT,15,15`); continuous
  reporting mode (`MODE1`). GPS power-saving disabled (`GPS_RST_TIME=300` keeps
  the GPS engine running/retrying rather than shutting it down).

### Idle — low battery, instantly startable
- **Aim:** low battery draw **while staying instantly reachable** — the operator
  can hit "start tracking" and the device must respond within seconds.
- **Implies:** GPS **off** (`GPS_RST_TIME=0`) — the big daytime battery saver,
  and we don't need position while idle. **TCP connection retained**
  (`SLPDISCONNECT=0` + frequent `HBT`) so a start command lands immediately — we
  deliberately keep the radio up, accepting that cost, to buy fast response.
  Reporting slowed right down (long `TIMER`); heartbeat-only traffic.

### Sleep — extremely low battery, periodic check-in
- **Aim:** **lowest possible** battery use overnight. Instant reachability is
  given up; a minutes-late response is fine.
- **Implies:** radio/modem **off most of the time** — the device wakes every
  `overnight_interval_min` (e.g. 30 min), opens TCP, reports once, and powers
  down until the next wake (`MODE4`/`MODE5` deep sleep on **V667** firmware;
  `MODE1` + a long `TIMER` on **V6.6x** firmware). GPS off between wakes. This is
  the key difference from Idle:
  Idle **holds** the connection for responsiveness; Sleep only connects
  **periodically** so the radio isn't drawing current for most of the night.

### Cross-cutting: no accelerometer wake-up (all three states)
None of the states should wake or change behaviour on motion. Trackers ride on
windsurfers and boats that pitch and rock constantly — and overnight the boats
rock with wind and wave — so vibration-triggered wake would cause endless
spurious wakes (Sleep) or unwanted state changes (Idle), wrecking the battery
budget and the operator's control of state. State is driven **only** by operator
command and the schedule, never by the accelerometer. This is what the
`VIBCHK` / `ACCLINE` settings are for (`ACCLINE=1` = don't treat vibration as
ACC-ON; `MODE5` ignores vibration entirely). **Note:** this goal is currently
only partly realised — see [Coverage gaps](#coverage-gaps): the vibration-wake
suppression is not set consistently across all three states.

### At a glance

| | Tracking | Idle | Sleep |
|---|---|---|---|
| Position quality | max (1 Hz) | none (GPS off) | one fix per wake |
| GPS | on | off | off between wakes |
| TCP connection | held, streaming | **held** (fast start) | **periodic** (radio off between) |
| Battery | high | low | **lowest** |
| Start-command latency | n/a | **seconds** | until next wake (minutes) |
| Accelerometer wake | off | off | off |

## The three states (and how they're applied)

| State | Meaning | Command set | Applied |
|---|---|---|---|
| **Tracking** | active 1 Hz race tracking | `_active_cmds(interval)` + `HBT,15,15#` | on login when active; config re-applied by `cxzt#` handler only if not already active (Fix 2) |
| **Idle** | race-day idle (connected, slow LOC) | `_idle_cmds(idle_loc_interval)` + `HBT,idle_hbt#` | on login when idle |
| **Sleep** | overnight deep sleep (wake-report-sleep) | firmware-dependent (below) | login queues `cxzt#` only; handler pushes the set only if the device's mode/freq is wrong |

**Sleep bifurcates by firmware version** (resolved in the `cxzt#` handler). All
units are W07C hardware; the split is purely the firmware version they run:
- **V667** (`overnight_mode_number` 4 or 5): `_overnight_cmds()` →
  `SLPDISCONNECT=0`, `ACCLINE=1`, `MODE4,<sec>#` (or `MODE5,<min>#`).
- **V6.6x** (V6.63 / V6.68 — clamp the MODE4 freq arg → kept on MODE1 long-TIMER):
  the handler pushes `_idle_cmds(loc_int)` + `HBT,loc_int#` (i.e. the **idle** set
  with a long interval), plus a one-shot `MODE1,loc_int,loc_int#` if not already
  MODE1.

## Settings matrix

Values are what we send. **`—` = not sent in that state** (device retains
whatever it had → see gaps). `loc_int` = `overnight_interval_min`×60.

| Setting (cmd) | Tracking | Idle | Sleep · V667 | Sleep · V6.6x |
|---|---|---|---|---|
| `SZCS#SLPDISCONNECT` | `0` | `0` | `0` | `0` |
| `TIMER` (LOC interval) | `interval` (1 s) | `idle_loc_interval` (300 s) | — (MODE sets wake) | `loc_int` (1800 s) |
| `HBT` (heartbeat) | `15,15` | `15,15` | — (MODE sets wake) | `loc_int,loc_int` |
| `MODE` | `1` (enforced if ≠1) | `1` | `4` (or `5`) | `1` (long-TIMER) |
| `SENDS` | `0` | `1` | **—** | `1` |
| `SENALM` (alarm push) | **—** | `OFF` | **—** | `OFF` |
| `MOVING` (move alarm) | **—** | `OFF` | **—** | `OFF` |
| `SZCS#GPS_RST_TIME` | `300` | `0` | **—** | `0` |
| `SZCS#VIBCHK` | `0:16` | `0:16` | **—** | `0:16` |
| `SZCS#ACCLINE` | **—** | **—** | `1` | **—** |

Queries (not persistent settings, listed for completeness): `cxzt#` (device-info
probe, every login + overnight wake) and `STATUS#` (battery/GPS poll, every
`idle_keepalive_interval` = 60 s).

## Coverage gaps

Settings that are **not set in all three states** — on a transition the device
keeps its previous value, which is undefined/unintended:

1. **`SZCS#ACCLINE`** — set to `1` *only* in V667 sleep, never reset.
   The [no-accelerometer-wake goal](#cross-cutting-no-accelerometer-wake-up-all-three-states)
   applies to **all** states, so the vibration-wake suppression (`ACCLINE=1`)
   should be set in Tracking and Idle too — not left to chance. It's also never
   set in **V6.6x sleep**, so V6.6x overnight doesn't get the suppression that
   V667 does — an asymmetry. (`ACCLINE` semantics need bench/vendor confirmation — the
   vendor sheet's "default 1, detects ACC line" wording is ambiguous vs the
   `_overnight_cmds()` comment "`=1` stops treating vibration as ACC-ON".)

2. **`SENALM` / `MOVING`** — set to `OFF` *only* in Idle (and V6.6x sleep).
   Not sent in **Tracking** or **V667 sleep**. We presumably never want movement/
   alarm pushes in any state → set `OFF` in all four columns.

3. **`SENDS`** — `0` (tracking) / `1` (idle) but **not sent in V667 sleep**.
   A V667 device entering deep sleep keeps its last `SENDS`. Pick a value and
   send it in `_overnight_cmds()`.

4. **`SZCS#GPS_RST_TIME`** — `300` (tracking) / `0` (idle) but **not in V667
   sleep**. V667 sleep relies on MODE4/5 for GPS power, but the value is left
   stale; set it explicitly for determinism.

5. **`SZCS#VIBCHK`** — `0:16` (tracking/idle) but **not in V667 sleep**
   (which uses `ACCLINE=1` for vibration instead). Intentional, but worth a
   one-line note in `_overnight_cmds()` so it's not read as an omission.

6. **`TIMER` / `HBT`** — **not sent in V667 sleep** (the `MODE4,<sec>#` arg
   governs the wake cadence instead). Lower risk because MODE overrides the
   reporting cadence, but the TIMER/HBT registers are left at their prior values.

**The pattern:** the two sleep paths have *complementary* coverage —
`_overnight_cmds()` (V667) sets `ACCLINE` but none of the SZCS GPS knobs / SENDS /
SENALM / MOVING; the V6.6x path (via `_idle_cmds`) sets those but not `ACCLINE`.
Neither sleep path is a complete state definition.

## Recommendation

Make each state builder set the **full** list so state is fully defined and no
value leaks across transitions. Concretely:
- Add `SENALM,OFF#` and `MOVING,OFF#` to `_active_cmds()` (and the V667 sleep
  set) so they're `OFF` everywhere.
- Set the vibration-wake suppression (`ACCLINE`, and `VIBCHK` as needed) to the
  same no-accel-wake value in **all** states — add it to `_active_cmds()` /
  `_idle_cmds()` and the V6.6x sleep path, matching `_overnight_cmds()`. (Confirm
  the exact value on the bench first — see the ACCLINE note in the gaps above.)
- Add explicit `SENDS`, `GPS_RST_TIME`, and `VIBCHK` to `_overnight_cmds()` (or a
  comment if MODE4/5 genuinely makes them moot).
- Keep `TIMER`/`HBT` MODE-governed in V667 sleep, but note it.

Treat this as the canonical superset; if a value is genuinely don't-care in a
state, send it anyway (idempotent) rather than leaving it undefined.

## Config defaults

From the `GT06Listener` constructor / `gt06.json` (current deployment values):

| Knob | Default | Deployed | Meaning |
|---|---|---|---|
| `gt06_interval` (active `interval`) | 10 s | **1 s** | Tracking LOC/TIMER cadence |
| `idle_loc_interval` | =`idle_hbt` | **300 s** | Idle LOC/TIMER cadence |
| `idle_hbt_interval` | 15 s | 15 s | Idle heartbeat cadence |
| `idle_keepalive_interval` | =`idle_poll` (60 s) | 60 s | `STATUS#` poll cadence |
| `overnight_interval_min` | 15 min | **30 min** | Sleep wake cadence → `loc_int` = ×60 |
| `overnight_mode_number` | 4 | 4 | Sleep MODE (4 = vibration-responsive; 5 = strict time) |
| `slow_loc_interval` | 3 s | 3 s | Tracking LOC cadence when slow (<`slow_speed_knots`=2 kn for `slow_speed_seconds`=20 s) |

Active LOC drops to `slow_loc_interval` (3 s) when the boat is slow, restoring
`interval` when it speeds up (`_on_location` adaptive-rate block, ~L1018–1037).
