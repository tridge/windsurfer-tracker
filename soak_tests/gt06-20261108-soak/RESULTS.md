# GT06 standalone firmware soak, 2026-11-08 18:33 → 2026-11-09 21:27 (27 h)

13 units in event 2 on WSTR-STD1_6be7199 (6 later on ca21b0f, phase markers only), 13 cycles of
track 25 min / idle 25 min / night sleep 59 min / idle settle 14 min. Even cycles sent a start-all
6 min before the end of sleep. Injected per cycle: 2 resets while tracking, 1 socket drop while
tracking, 1 reset + 1 drop while idle; server restarts in cycles 4 and 9, one OTA in cycle 5.
Units on charge until 11:31 on 11-09, then on battery for the last 10 h.

Totals: 40 resets, 20 socket drops, 2 planned + 3 deploy server restarts, 1 planned OTA + 1 fleet OTA,
40 phases. Raw logs: `anomalies.jsonl`, `boots.jsonl`, `phases.jsonl`, `notes.jsonl`.

## Anomalies (965 logged)

| Kind | Count | Verdict |
|---|---|---|
| position_while_idle | 948 | Harness false positives: midnight rollover (13), planned asleep start-all (3), manual walk start-all (rest) |
| missed_wake | 13 | Artefact of the manual walk start-all (units stayed connected), except G304307 below |
| unexpected_reboot rr5 | 6 | Deliberate: the 07:14 OTA to ca21b0f |
| unexpected_reboot rr0 | 3 | **Real, open** (below) |
| offline / not_back_after_sleep | 8 | Each is the run-up to one of the reboots above, or the G304307 wake misses |

No unit needed a power-cycle; every injected reset, drop, restart and OTA recovered on its own.

## Real findings

1. **rr0 SoC-only resets (open).** G341846 00:57 (idle), G306161 05:11 (idle) and 08:54 (tracking).
   Each happened about 12 s into a cellular data stall. All three units were on the production 12 s
   stall watchdog. The 6 units on WDSTALL=120 had none in ~20 h. That points at the 12 s watchdog
   firing on a stall that would have recovered; the cause is not yet proven.
2. **A start sent during sleep could miss a wake (fixed).** The server spent each wake on the
   settings read-back, and the firmware went back to sleep 5 s after the last command if one ACK was
   slow. G304307 missed one wake at 16:32 and three in a row at 19:00–19:21, staying asleep past the
   window end. Fixed by:
   - server ce4a45a, 725c4f9, e01239a: send SLPDISCONNECT=0 first, and use the last known firmware at
     login (deployed 19:47, verified live);
   - firmware cb08cd3: 15 s quiet before night sleep (OTA'd at the end of the soak).
   Cycle 13 had no wake misses.
3. **Server restarts keep tracking state** (33096a2), including per-unit start/stop. Verified live:
   16/16 units re-logged in as active after each of the 3 deploy restarts during tracking.

## Walk test (16:25–17:13)

5 units carried, plus the watch. 5 s fixes, no gaps over 10 s, median lag 1 s, units within ~3.6 m
of each other (median). One 98 m cold-start outlier.

## Harness fixes for later runs

- Ignore the None positions written at the midnight rollover.
- Skip position_while_idle once a wake start has been sent in the phase.
- The runner waits for `--min-units` online at start, since units may be between night wakes.
