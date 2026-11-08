/* reliability.h — software watchdog + reset-reason logging for the standalone firmware.
 *
 * A resident periodic timer (~1 Hz) watches a liveness counter fed by the main loop and resets the SoC
 * when it stalls. It also provides the main loop's seconds timebase. The timer callback does no I/O at
 * all (no USB, no FS): a blocked write there would disable the watchdog itself. Reset reasons are
 * persisted to the resident FS from main-task context only.
 */
#ifndef RELIABILITY_H
#define RELIABILITY_H
#include "sapi.h"

/* reset-reason codes stored in c:/wdstat.bin (why the LAST reboot happened). */
enum reset_reason {
    RR_UNEXPECTED = 0, /* none of our intentional resets: power loss, brownout, watchdog or crash */
    RR_WATCHDOG = 1,   /* reserved: the timer callback cannot write the FS, so watchdog resets read 0 */
    RR_COMMAND  = 2,   /* RESET# from the server */
    RR_FIRSTBOOT = 3,  /* wdstat.bin absent/invalid -> first boot on this fw */
    RR_FWD_RX_DEAD = 4,/* forwarding bridge: resident if04 RX callback died -> reboot to recover it */
    RR_FOTA     = 5,   /* FOTA staged OK -> reboot to commit */
    RR_FOTA_FAIL = 6,  /* FOTA aborted after the flash was opened */
    RR_NODATA   = 7,   /* idle, registered but no server link for a long time */
    RR_GPS_DEAD = 8,   /* tracking, GNSS silent through every power-cycle */
    RR_TIMER_FAIL = 9, /* the watchdog timer could not be created */
    RR_ST_HUNG  = 0x50,/* self-test bookkeeping: main task marked itself about-to-hang (see WD_SELFTEST) */
};

/* Read the last reset reason, re-arm the record as RR_UNEXPECTED and start the timer. Returns the last
 * boot's reason. *timer_ok is 0 if the timer could not be created (no watchdog and no timebase). */
int reliability_init(int arm, int *timer_ok);

/* Call once per main-loop iteration — the liveness heartbeat the monitor watches. */
void wd_alive(void);

/* Stall-phase marker: the main loop sets this around every potentially-blocking call ('C' connect,
 * 'S' send, 'R' recv, 'A' at_cmd, 'Q' queue/select wait, 'F' FS, 'U' uart, 'G' gnss power,
 * 'V' vcom write, 'O' FOTA, 'N' network getter; 0 = none). The tick records the phase of the longest stall. */
extern volatile unsigned char g_wd_phase;

/* Set while the USB<->TCP forwarding bridge is CONFIGURED (owns if04) — held from boot through any FWD
 * outage (main.c). All debug output is suppressed then so it can't corrupt the raw data stream. */
extern volatile unsigned char g_fwd_en;

/* Seconds since boot (~1 Hz, from the watchdog tick) — the main loop's timebase. */
unsigned wd_uptime(void);

/* Enable/disable the watchdog reset at runtime (the self-test uses this). */
void wd_arm(int on);

/* Tolerate a deliberate stall of up to `secs` from now (night sleep). The deadline still applies. */
void wd_grace(unsigned secs);

/* Blocking sleep (~ms) via a timed MsgQRecv on a private empty queue. */
void wd_sleep(int ms);

/* Persist a reset-reason code to wdstat.bin (main-task/FS-safe context only). */
void wd_set_reason(int reason);

/* Persist `reason` then reset the SoC (main-task context only). */
void wd_reason_and_reset(int reason);

/* Diagnostics for CXCS#BOOTSTAT. */
unsigned wd_bootcount(void);
unsigned wd_stall_max(void);          /* longest stall (ticks) the main loop has had this boot */
unsigned char wd_stall_phase(void);   /* g_wd_phase during that stall */

/* What the previous boot recorded in the RAM reset trace just before resetting (watchdog phase, or the
 * reason of a reset we caused). 0 if there is none (power loss, crash, or RAM not retained). */
int wd_prev_trace(unsigned *reason, unsigned *phase, unsigned *uptime);

/* Persisted counter of consecutive no-data reboots (reset on a successful login). */
unsigned wd_nodata_count(void);
void wd_set_nodata_count(unsigned n);

/* Arm the MMP SoC HARDWARE watchdog (hwwd.c). NOT viable on this SoC — see main.c WD_HW_ARM. */
void wd_hw_arm(unsigned secs);

#endif
