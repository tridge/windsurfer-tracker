# Battery Life Tests

## Idle Battery Drain (2026-02-17)

All 14 trackers started at 100% battery, unplugged, sitting idle for 4h27m.

| Tracker     | Bat% | Drain | Rate    | Idle Life |
|-------------|------|-------|---------|-----------|
| Tracker1 SE |  80% |  20%  | 4.5%/hr |       22h |
| Tracker2    |  68% |  32%  | 7.2%/hr |       14h |
| Tracker3    |  81% |  19%  | 4.3%/hr |       23h |
| Tracker4    |  79% |  21%  | 4.7%/hr |       21h |
| Tracker5    |  82% |  18%  | 4.0%/hr |       25h |
| Tracker6    |  89% |  11%  | 2.5%/hr |       41h |
| Tracker7    |  87% |  13%  | 2.9%/hr |       34h |
| Tracker8    |  77% |  23%  | 5.2%/hr |       19h |
| Tracker9    |  87% |  13%  | 2.9%/hr |       34h |
| Tracker10   |  78% |  22%  | 4.9%/hr |       20h |
| Tracker11   |  97% |   3%  | 0.7%/hr |      149h |
| Tracker12   |  90% |  10%  | 2.2%/hr |       45h |
| Tracker13   |  89% |  11%  | 2.5%/hr |       41h |
| Tracker14   |  89% |  11%  | 2.5%/hr |       41h |

### Tiers

- **Good** (>30h idle): Tracker11, Tracker12, Tracker6, Tracker13, Tracker14, Tracker7, Tracker9
- **OK** (20-30h): Tracker5, Tracker3, Tracker1 SE, Tracker4
- **Concerning** (<20h): Tracker10, Tracker8, Tracker2

## Tracking Battery Drain (2026-02-19)

14 trackers actively tracking GPS for 2h 51m (18:07 → 20:58).

| Tracker     | Start | End | Drain | Rate     | Track Life | Notes              |
|-------------|-------|-----|-------|----------|------------|--------------------|
| Tracker1    |  95%  | 70% |  25%  | 8.8%/hr  |        11h |                    |
| Tracker2    |  86%  | 54% |  32%  | 11.2%/hr |         9h | GPS-wait 15%       |
| Tracker3    |  98%  | 84% |  14%  | 4.9%/hr  |        20h |                    |
| Tracker4    |  98%  | 84% |  14%  | 4.9%/hr  |        20h |                    |
| Tracker5    |  95%  | 70% |  25%  | 8.8%/hr  |        11h |                    |
| Tracker6    | 100%  | 89% |  11%  | 3.9%/hr  |        26h |                    |
| Tracker7    | 100%  | 97% |   3%  | 1.1%/hr  |        95h |                    |
| Tracker8    |  76%  | 60% |  16%  | 5.6%/hr  |        18h |                    |
| Tracker9    | 100%  | 74% |  26%  | 9.1%/hr  |        11h |                    |
| Tracker10   |  98%  | 83% |  15%  | 5.3%/hr  |        19h |                    |
| Tracker11   | 100%  | 96% |   4%  | 1.4%/hr  |        71h |                    |
| Tracker12   |  98%  | 90% |   8%  | 2.8%/hr  |        36h | briefly idle       |
| Tracker13   |  97%  | 87% |  10%  | 3.5%/hr  |        28h | GPS-wait 12%       |
| Tracker14   | 100%  | 89% |  11%  | 3.9%/hr  |        26h |                    |

**Notes:** Tracker2 had GPS fix 85% of the time but spent 26m in GPS-wait. Tracker13
had 21m of GPS-wait. Tracker12 went briefly idle mid-test (~3m). Data extracted from
`battery_tests/battest.log` using `battery_tests/analyze_battest.py`.

## Idle Battery Drain (2026-02-19)

Same 14 trackers left idle for 9h 38m after tracking ended (20:58 → 06:36+1d).

| Tracker     | Start | End | Drain | Rate    | Idle Life |
|-------------|-------|-----|-------|---------|-----------|
| Tracker1    |  70%  |  5% |  65%  | 6.7%/hr |       15h |
| Tracker2    |  54%  |  1% |  53%  | 5.5%/hr |       18h |
| Tracker3    |  84%  | 47% |  37%  | 3.8%/hr |       26h |
| Tracker4    |  84%  | 39% |  45%  | 4.7%/hr |       21h |
| Tracker5    |  70%  |  1% |  69%  | 7.2%/hr |       14h |
| Tracker6    |  89%  | 59% |  30%  | 3.1%/hr |       32h |
| Tracker7    |  97%  | 73% |  24%  | 2.5%/hr |       40h |
| Tracker8    |  60%  | 31% |  29%  | 3.0%/hr |       33h |
| Tracker9    |  74%  |  6% |  68%  | 7.1%/hr |       14h |
| Tracker10   |  83%  | 30% |  53%  | 5.5%/hr |       18h |
| Tracker11   |  96%  | 75% |  21%  | 2.2%/hr |       46h |
| Tracker12   |  90%  | 75% |  15%  | 1.6%/hr |       64h |
| Tracker13   |  87%  | 63% |  24%  | 2.5%/hr |       40h |
| Tracker14   |  89%  | 62% |  27%  | 2.8%/hr |       36h |

## W07C Continuous Tracking (2026-03-24)

W07C device (3000mAh battery) on a turntable with continuous GPS tracking for 24 hours.

| Parameter     | Value  |
|---------------|--------|
| Battery       | 3000mAh |
| Start voltage | 4.10V  |
| End voltage   | 3.30V  |
| Duration      | 23h    |
| Mode          | Continuous tracking |

**Notes:** Voltage readings show frequent single-sample downward spikes (0.02–0.14V)
caused by voltage sag during cellular TX. A median(9) filter on the STATUS voltage
readings cleanly removes these spikes without distorting the underlying discharge curve.

## W07C Charging (2026-03-26)

W07C (3000mAh): 5 hours from flat to full charge.
