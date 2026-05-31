# W07C — low-power "wake but don't locate" sleep: questions for vendor

**Context for the vendor:** Outside of racing, we want these trackers in the
**lowest-power deep sleep possible**, but with one requirement: the device must
**wake periodically, open a TCP connection to the server (so we can confirm it is
alive and push a command to it), and then go back to deep sleep** — **without
powering on the GNSS/GPS**. We do **not** need a position while sleeping; a
periodic check-in (LBS/cell-tower fix is fine) is enough. Two reasons we must
keep GPS off in sleep:
1. **Battery** — GNSS acquisition is the dominant draw; we want MCU + modem off
   between wakes.
2. **Indoors** — when a device is stored in a hotel room overnight it must NOT sit
   there trying (and failing) to get a GPS lock, burning battery.

We also never want the **accelerometer/vibration** to wake the device or change
its state (the trackers ride on boats that rock all night) — wake should be
**purely time-scheduled**.

**What we observe today (from our logs):** in `MODE4`/`MODE5` deep sleep the
device wakes on schedule and **turns the GNSS on every wake to report a
location** (we see GPS go from `OFF` to `Successful`/`Fail positioning`, and LOC
packets at each wake; `cxzt#` shows `G:A`). We believe `SZCS#ACC_OFF_FREQ`
couples "wake" and "locate" — and that setting it to `0` ("sleep and not report
location") stops the periodic wake entirely, which we do **not** want.

Please answer **per command**: exact syntax (SMS form **and** the GT06 0x80
server-command form we send over TCP), the **ACK string**, value **range/default**,
and whether it **persists across reboot**. Where behaviour differs between our
three firmware builds — `W07_..._V6.63`, `W07_..._V6.68`, `NT19D_..._V667` —
please note the difference.

---

## 1. The core question — periodic TCP wake WITHOUT GPS
1. **How do we configure a deep-sleep mode that wakes on a fixed time schedule,
   opens a TCP connection to report it is alive (LBS-only is fine), and returns
   to deep sleep — with the GNSS never powered?** If there is a single
   recommended command set for this, please give it (this is what we most want).
2. In `MODE4`/`MODE5` deep sleep: between wakes, are **both the MCU and the 4G
   modem fully powered off** (TCP torn down)? What exactly triggers each wake —
   the `MODE` timer, `SZCS#SLEEPUPDATE`, `SZCS#ACC_OFF_FREQ`, or the heartbeat?
3. Is the GNSS power-on at each wake **mandatory** (the wake intrinsically
   produces a GNSS fix), or can the wake report be **LBS-only with GNSS left
   off**? If LBS-only is possible, what command enables it?

## 2. Undocumented parameters we found in the firmware — please define
We extracted these tokens from the V665/V667 firmware image and have **no docs**
for them. For each: syntax, meaning, range, default, persistence — and its role
in the deep-sleep wake cycle:
4. **`SZCS#GPS_FLAG=<n>`** (query `CXCS#GPS_FLAG`) — we believe this is the per-
   sleep-cycle **GPS enable flag** (it appears next to `ACC_OFF_FREQ`, and the
   firmware logs a sleep command `Send Sleep Cmd 0x05 sos,Vib,GG,Timing` plus
   `SetGG/GetGG`, so we suspect `GPS_FLAG` == `GG`). **Does `GPS_FLAG=0` make the
   device wake and report on schedule but leave the GNSS powered off?** This is
   the single setting we most want confirmed.
5. **`SZCS#SLEEPUPDATE=<n>`** — referenced in your MODE4 notes ("set to 1, max
   65535 s"). Is this the master enable for the scheduled deep-sleep wake-report,
   and does it pair with `ACC_OFF_FREQ` as the cadence? Units of the value?
6. **`SZCS#HEART_ACCOFF=<n>`** — meaning and units. Is this a **heartbeat-only**
   wake while ACC is off (i.e. a periodic TCP check-in that does **not** locate)?
   Does it fire in MCU-off deep sleep, or only in a lighter ACC-off state?
7. **`SZCS#ACC_OFF_FREQ=<n>`** (documented as "sleep upload location", 0 = sleep
   and not report) — does `0` stop only the **location report**, or also the
   **periodic wake/TCP check-in**? Is the wake-without-report achievable by
   combining `ACC_OFF_FREQ>0` with `GPS_FLAG=0`?
8. **`SZCS#SLEEPT=<n>`** — confirm: minutes of no-vibration/ACC-off before
   entering sleep; `0` = never sleep. Correct?

## 3. MODE4 vs MODE5
9. Please confirm the difference: we read `MODE5` = **timed flight** (time-only
   wake, no vibration wake) and `MODE4` = **vibration flight** (vibration + timed).
   For our "no accelerometer wake" requirement, is **`MODE5` the correct choice**,
   and does `MODE5` fully ignore vibration for wake?
10. `MODE4` takes seconds, `MODE5` takes minutes for the wake interval — correct?
    What are the **min/max** wake intervals for each? (Our older `V6.63`/`V6.68`
    units do not accept the `MODE4` frequency argument — they clamp to 120 s — so
    we currently keep them on `MODE1` with a long `TIMER`. Is that the right
    approach for those firmwares, or is there a fix?)

## 4. Recommendation
11. Given section 1's goal — **lowest-power, time-scheduled deep sleep that wakes
    periodically for a TCP/LBS check-in but never powers the GNSS, and never wakes
    on vibration** — what is the **single command set you recommend** for each of
    our three firmware versions (`V6.63`, `V6.68`, `V667`)? We will push it as our
    "sleep" state. If this is **not achievable** on a given firmware (e.g. the
    wake always lights the GNSS), please say so plainly so we can plan around it.

---

*Internal notes (not for vendor): firmware tokens seen in*
*`devices/W07C/unpacked/W07_NT19D_MG133_B53_V665.strings`: `GPS_FLAG`,*
*`SLEEPUPDATE`, `HEART_ACCOFF`, `ACC_OFF_FREQ`, `SLEEPT`, `GPS_QUICK_UP`,*
*`GPS_HOTFUN`, `WORK_MODE`; sleep cmd `Send Sleep Cmd 0x04/0x05 ...GG,Timing`,*
*`Re Agps By Wakeup`, `AGPS_ADDR/PORT`. `GPS_FLAG`/`SLEEPUPDATE`/`HEART_ACCOFF`*
*are NOT in either customer command spreadsheet. See memory*
*`project_sleep_wake_turns_gps_on` and `gt06/gt06_settings.md` for the analysis.*
