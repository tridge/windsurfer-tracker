/* reliability.h — software watchdog + reset-reason logging for the standalone firmware.
 *
 * Goal (see reference_w07c_watchdog_por): catch a hang of OUR main loop and reset, and record WHY we
 * reset so a fresh boot can report it. The SoC sys_wdt is resident-fed (won't catch our hang) and the
 * PMIC HW watchdog needs a feed register we don't have yet; so step 1 is a SOFTWARE watchdog: an
 * independent monitor task watches a liveness counter and triggers a SoC reset if the main loop stalls.
 * Reset-reason is persisted to the resident FS (proven: fopen/fread/fwrite) — HW POR (PMIC reg 0x10) is a
 * later addition (needs the resident PMIC path / a live probe).
 */
#ifndef RELIABILITY_H
#define RELIABILITY_H
#include "sapi.h"

/* reset-reason codes stored in c:/wdstat.bin (why the LAST reboot happened). */
enum reset_reason {
    RR_POWERON  = 0,   /* not one of our intentional resets -> external: power pull / brownout / HW */
    RR_WATCHDOG = 1,   /* our software watchdog fired (main loop stalled) */
    RR_COMMAND  = 2,   /* an intentional reset we issued (e.g. a future RESET command) */
    RR_FIRSTBOOT = 3,  /* wdstat.bin absent/invalid -> first boot on this fw */
    RR_FWD_RX_DEAD = 4,/* forwarding bridge: resident if04 RX callback died -> reboot to recover it */
    RR_ST_HUNG  = 0x50,/* self-test bookkeeping: main task marked itself about-to-hang (see WD_SELFTEST) */
};

/* Read+log the last reset reason, then re-arm the record; optionally start the monitor task.
 * `wr` is the USB-VCOM write fn (for logging). `arm` starts the software watchdog (ship disarmed until
 * the SoC-reset primitive is confirmed on-device). Returns the reset_reason of the last boot. */
int reliability_init(vcom_write_fn wr, int arm);

/* Call once per main-loop iteration — the liveness heartbeat the monitor watches. */
void wd_alive(void);

/* Stall-phase marker: the main loop sets this around every potentially-blocking call ('C' connect,
 * 'S' send, 'R' recv, 'A' at_cmd, 'Q' queue/select wait, 'F' cwprm FS, 'U' uart, 'G' gnss power,
 * 'V' vcom write; 0 = none). wd_tick prints it on each stalled tick, so a WD:FIRE capture names the
 * blocking call (added for the ~13.5-min tracking stall hunt, 2026-07-23). */
extern volatile unsigned char g_wd_phase;

/* Set while the USB<->TCP forwarding bridge is CONFIGURED (owns if04) — held from boot through any FWD
 * outage (main.c). The watchdog's diagnostic writes (WD:alive etc.) are suppressed then, so they don't
 * corrupt the raw data stream — and, since those run from the resident timer thread, a stuck if04
 * endpoint can't block the watchdog task either. */
extern volatile unsigned char g_fwd_en;

/* Seconds since boot (~1 Hz, from the watchdog tick) — the main loop's timebase for LOC/heartbeat cadence. */
unsigned wd_uptime(void);

/* Enable/disable the watchdog at runtime (e.g. quiet it after a self-test proves out). */
void wd_arm(int on);

/* Blocking sleep (~ms) via a timed MsgQRecv on a private empty queue. */
void wd_sleep(int ms);

/* Persist a reset-reason code to wdstat.bin (main-task/FS-safe context only). */
void wd_set_reason(int reason);

/* Arm the MMP SoC HARDWARE watchdog (hwwd.c) with a `secs` timeout, fed from wd_tick. Needs arm=1.
 * Independent of the RTOS: if wd_tick/RTOS dies (nothing feeds it) the SoC hard-resets. Ship disarmed. */
void wd_hw_arm(unsigned secs);

/* Persist `reason` then hard-reset the SoC (used by the monitor and any future intentional reset). */
void wd_reason_and_reset(int reason);

#endif
