# GT06 overnight soak on WSTR-STD1_cb08cd3, 2026-11-09 21:43 → 2026-11-10 10:18 (12.6 h)

16 units in event 2, all OTA'd to cb08cd3 (15 s quiet before night sleep) at 21:42. 6 cycles of
track 25 / idle 25 / night sleep 59 / idle settle 14 min; start-all during sleep on cycles 2, 4, 6;
server restart in cycle 3; same-image OTA in cycle 5. WDSTALL=120 on G380521 G341846 G306088 G312326
G370589 G375430, production 12 s on the other 10. On battery throughout (off charge since 11-09 11:31).

Totals: 24 resets, 12 socket drops, 1 server restart, 1 OTA, 24 phases, 8 anomalies.

## Worked

- Night sleep: every wake on schedule for 16 units through cycles 1–5 (max gap 626 s); 18–19 s
  awake per wake, ~10 s more than 6be7199, as designed.
- Start during sleep: cycles 2 and 4, 16/16 activated at their first wake (max 540 s / 570 s).
- Server restart (02:34): 16/16 relogged as idle within a minute.
- Same-image OTA (G370589, 06:33): back on cb08cd3 in 79 s.
- All 24 planned resets and 12 drops recovered without help.

## Anomalies

| Time | Unit | What | Verdict |
|---|---|---|---|
| 06:44, 08:05, 08:07, 08:37 | G375547 | offline ~100 s | Cellular data stalls; weakest signal in the fleet (RSRP idx 9–16, SINR −10..−12). Each time the server timed out a command, and the unit reconnected in 20–54 s with **no reboot**. Placement/antenna. |
| 08:23 | G312300 | rr0 reset while tracking | Unexplained SoC-only reset ~10 s after traffic stopped, good signal. 4th rr0 overall; all on 12 s units. |
| 08:58 → | G375539 | never woke from night sleep | **New failure, open.** Normal sleep entry, then no wake for 80+ min. SIM still in a data session per Simbase. Stall watchdog is graced only 660 s, so a main-loop hang should have reset it: suspect lost alarm wake with the timer not ticking in SoC sleep (HW WDT not armed). First in ~1800 sleep wakes. Awaiting USB/physical check. |

## rr0 tally (both runs, ~40 h)

12 s units: 4 (G341846, G306161 ×2, G312300). 120 s units: 0. But no unit, including the 120 s
units, ever reported a main-loop stall over 5 s in ~430 BOOTSTATs, so the 12 s stall watchdog is not
shown to be the cause. Next test: WDSTALL=120 on all units.
