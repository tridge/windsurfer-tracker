# GT06 tracker fleet — 2026 regatta performance analysis

Raw-log analysis of the tracker fleet over the racing week **2026-07-05 → 2026-07-11**,
driven entirely from the binary packet logs (`old_logs/gt06.log.*`, IMEI-ID markers
retrofitted). Tool: `scripts/gt06_regatta_report.py`; per-unit baseline from the quiet
pre-event week (06-27 → 07-04, `baseline.json`). Machine-readable results in
`summary.json` / `units.csv` / `anomalies.csv`; per-unit voltage/mode/lag plots in `plots/`.

**Fleet:** 41 units — 11×3Ah + 30×6Ah; firmware W07 V6.63 (1), W07 V6.68 (9+10),
NT19D V667 (20), plus the V665 custom-build bench unit (G375356, excluded from fleet
conclusions — it was under USB/firmware surgery all week).

**Volume:** 741,833 position fixes (728,716 while actively tracking, 1.08 fix/s
sustained across ~192 fleet active-hours), ~9,900 unit-hours of idle.

## Headline

The fleet did its job: **no persistent GPS lag anywhere all week** (per-unit lag
median 3 s, p95 4 s, worst unit p95 8 s), **zero settings drift**, every blind-buffer
replay caught up on its own within 3.5 minutes, and no tracker died mid-race
(closest: G375430 finished the last day at ~1.5 % SoC, G347082 at ~2 %).

The significant anomalies fall into six stories, three of them firmware-correlated:

### 1. Mass device reboot after a cellular outage — 07-07 01:35–02:01
At 01:35 the fleet's contact fell from 40 to 16 units while the server kept serving
the remaining 16 (so: a partial mobile-network outage, most plausibly the cell most
units were camped on, not a server or power problem). The ~24 affected units all
came back at 01:50–02:01 with **frame-serial regressions — the firmware
watchdog-reboots the whole device after ~15 min without registration**. Recovery
was clean and autonomous; both firmware families behaved identically. Worth knowing:
a coverage outage costs ~20 min of contact + a device reboot, and if it happened
mid-race the blind buffer would be holding fixes across the reboot (see §4).

### 2. Race-day battery burn is ~2× the bench model on a long day (07-10)
- 07-09 racing: fleet median measured drain **1.66 %/h ≈ 0.8× model** (115 mA bench figure).
- 07-10 (long-distance day): fleet median **3.15 %/h ≈ 1.4× model**, tails to
  5–5.8 %/h (≈2×), on **both** W07 and NT19D units; fleet median resting-voltage
  drop for the day 180 mV vs 30 mV on 07-09.
- Ruled out: upload rate (1.08 LOC/s both days), GSM signal during racing (actually
  *better* on 07-10), overnight/evening drain (both nights ~10 mV ≈ 0.12 %/h — normal),
  charging exclusion artifacts.
- The 0.381 W / 115 mA tracking figure comes from a static bench discharge; sustained
  on-water racing (cell handovers, TX distance, GPS under spray) plausibly runs
  0.55–0.7 W. **Planning guidance: budget ~2 full race days per 6Ah charge on long
  courses; recharge after any long-distance day.** The fleet was charged daily during
  practice (05–07) but ran 07-08→07-11 on one charge — that worked, but only just
  (six units finished below 15 % SoC).

### 3. Idle "GPS-restart flurries" are a V667 signature
49 idle-LOC episodes (bursts of 4–6 fixes over a few minutes while commanded idle):
**46 on V667 (0.142/10 idle-h), 3 on V6.68 (0.010), 0 on V6.63/V665.** Pattern from
the raw frames: an idle unit uploads an off-schedule no-fix LOC (sats 0–1), the server
re-asserts GPS-off (`GPS_RST_TIME=60` re-push visible in-log), the GPS resets, briefly
acquires (sats 10–13), uploads a handful of fixes, then settles back to the 30-min
cached-fix cadence. Self-limiting; only 2 episodes ever reached the ≥5 LOC/min "stuck"
rate and none needed a MODE1 bounce. Cosmetic at event scale, but it is a clear
NT19D-V667 firmware behaviour to raise with the vendor (14× the V6.68 rate).

### 4. Blind-buffer replay: 4 episodes all week, all drained, none needed remediation
- **07-10 13:21 G347082** and **07-10 13:53 G341846** (both V667): caused by
  **spontaneous mid-race device reboots** (serial regressions, ~2 min offline). On
  reconnect each replayed its buffered backlog (max lag 136 s / 114 s) and was fully
  live again in 3m14s / 2m14s. The V667 drain style is *interleaved* — live fixes
  slotted between backlog fixes (135/74 GPS-time backward steps) — messy on the wire
  but the server's timestamp handling absorbs it and the sailor's track is complete.
  **The blind buffer did exactly its job: a mid-race reboot cost no track data.**
- 07-07 10:23 G341887: 12 lagged fixes, drained in 70 s.
- 07-08 11:39 G378723: a single 34-hour-stale cached fix replayed into a live stream
  (the known stale-fix quirk), gone in one fix.
- The server's lag remediation (`TIMER,2` drain) never engaged and was never needed;
  no replay episode stalled or persisted to disconnect.

### 5. Reboots and resets
50 serial-regression reboots total: ~24 in the 07-07 network event (§1), 10 on the
bench unit, 5 after operator `RESET#` commands, 2 mid-race (§4), a handful during
07-11 pack-up on nearly-flat batteries (G375430 ×3 at ≤2 % SoC, G378657 with a
−227 mV brown-out step), leaving **~6 genuinely spontaneous single-unit reboots**
(G341887, G380521, G312359, G370589, G371116 — all V667 — and G375372, V6.68).
Roughly one spontaneous reboot per 1000 unit-hours, V667-weighted; each cost at most
a couple of minutes of idle contact.

Server-side: 9 restart clusters (07-05 12:07+12:17 ×41 units, 07-05 20:08, 07-07
18:59, 07-08 05:41+18:13, 07-09 04:09+06:09+16:43). Device serials continue across
all of them, confirming they are socket resets only. The 04:09/06:09 07-09 pair and
the other freeze-week restarts are server-side events to reconcile against ops notes.

### 6. Battery health outliers
- **G312268 (3Ah)** is the one suspect cell: idle drain 0.98 %/h on 07-11 (10× model),
  the worst overnight drops both nights (30–40 mV), and a −120 mV resting step on
  07-11 morning — same fingerprint as the known-bad G375356 cell. Recommend a bench
  discharge test before the next event.
- G380521 had a double −110 mV step + reboot on 07-05 morning (pre-event handling;
  clean afterwards). G371116 −70 mV near its 07-08 reboot.
- End-of-week SoC spread (no charging since 07-07): 1.5 %–90.6 %, median ~35 %;
  lowest six all units that raced every day: G375430 1.5, G347082 2.0, G375562 10.5,
  G312292 12.2, G375372 13.0, G375539 13.6.

## Firmware / capacity cross-correlation

| group | units | active h | idle h | idle-LOC /10h | replay /10h | spont. reboots | idle drain med %/h | notes |
|---|---|---|---|---|---|---|---|---|
| W07 V6.63 / 3Ah | 1 | 2.2 | 166 | 0 | 0 | 0 | 0.08 | clean |
| W07 V6.68 / 3Ah | 9 | 22 | 1488 | 0.02 | 0 | 1 | 0.07 | clean |
| W07 V6.68 / 6Ah | 10 | 49 | 1629 | 0.02 | 0.21¹ | 0 | 0.10 | ¹ one 70 s episode |
| NT19D V667 / 6Ah | 20 | 115 | 3241 | **0.14** | 0.26 | **5** | 0.10 | GPS flurries, spont. reboots |
| V665 bench (G375356) | 1 | — | 168 | 0 | — | — | — | excluded (USB surgery) |

Capacity class shows **no** anomaly correlation once G312268's bad cell is set aside —
3Ah and 6Ah behave alike per hour; they differ only in endurance. Firmware is the
discriminator: **all idle GPS flurries, all mid-race reboots and nearly all spontaneous
reboots are NT19D V667**; the W07 V6.6x units were essentially silent all week.

## Vendor follow-ups (for the V667 report)
1. Idle GPS-restart flurries (§3) — why does V667's GPS wake with no fix and upload
   off-schedule ~14× more often than V6.68 under identical idle config?
2. Spontaneous mid-race/mid-idle reboots (§5) — 7 events, 6 on V667.
3. Interleaved blind-buffer drain (§4) — live fixes mixed into backlog replay
   (V6.68 drains in order); harmless to us but likely a firmware bug.
4. Confirm the ~15-min no-registration watchdog reboot (§1) and whether its timeout
   is configurable.

## Method notes
- Mode (idle/active/overnight) reconstructed per unit from outgoing `SENDS,0/1` /
  `MODE4/5` commands in the binary log; verified against cxzt `*F:` settings echoes
  (zero mismatches all week).
- Reboot = login whose frame serial regresses to ≤5 after ≥20 (serials proven to
  continue across TCP reconnects, e.g. the 07-09 04:10 server restart: fleet logins
  at serial ≈ previous+2).
- SoC via the shared `scripts/gt06_soc.py` (parametric OCV fit, per-unit divider
  offsets, class IR add-back, charging windows excluded, idle drain fitted only on
  GPS:OFF/ACC:OFF resting readings over ≥4 h windows).
- Idle-LOC whitelisting: one cached-fix upload per ~30 min (TIMER T2) is expected
  idle behaviour, not an anomaly.
