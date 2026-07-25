/* hwwd.c — Marvell MMP SoC watchdog @0xD4080000. See hwwd.h. */
#include "hwwd.h"

#define WDT_BASE   0xD4080000u
#define MPMU_BASE  0xD4050000u

/* WDT (timer-2) registers. Every protected access must be preceded by the WFAR/WSAR unlock magic. */
#define TMR_WMER (WDT_BASE + 0x64)   /* bit0 count-enable, bit1 1->SoC-reset on match (0x3 = armed) */
#define TMR_WMR  (WDT_BASE + 0x68)   /* 16-bit match/timeout, in 256Hz ticks */
#define TMR_WVR  (WDT_BASE + 0x6C)   /* 16-bit up-counter (read) */
#define TMR_WSR  (WDT_BASE + 0x70)   /* status: !=0 => last reset was this WDT; write 0 to clear */
#define TMR_WCR  (WDT_BASE + 0x98)   /* write 1 -> zero the counter (the FEED) */
#define TMR_WFAR (WDT_BASE + 0x9C)   /* write 0xBABA */
#define TMR_WSAR (WDT_BASE + 0xA0)   /* write 0xEB10 */

#define MPMU_WDTPCR (MPMU_BASE + 0x200)    /* WDT peripheral clock + reset-route (bit6 = route WDT->reset) */
#define MPMU_APRR   (MPMU_BASE + 0x1020)   /* bit4 (WDTR) = release WDT's HW-reset gate */
#define MPMU_APRR_WDTR (1u << 4)

#define WR32(a, v) (*(volatile unsigned *)(a) = (unsigned)(v))
#define RD32(a)    (*(volatile unsigned *)(a))

#define WDT_HZ 256u   /* 16-bit counter @ 256 Hz -> ticks = secs*256, max ~255 s */

static void __attribute__((unused)) spin(volatile unsigned n) { while (n--) { } }

/* Unlock exactly the NEXT protected register access (per-access window; re-locks after). */
static void unlock(void) { WR32(TMR_WFAR, 0xBABAu); WR32(TMR_WSAR, 0xEB10u); }

static unsigned secs_to_ticks(unsigned secs)
{
    unsigned ticks = secs * WDT_HZ;
    if (ticks == 0 || ticks > 0xFFFF) ticks = 0xFFFF;
    return ticks;
}

/* Arm the WDT-block registers only (timeout, counter, reset-on-match) — no MPMU reset-routing writes. */
static void wdt_block_arm(unsigned ticks)
{
    unlock(); WR32(TMR_WMR, ticks);    /* timeout */
    unlock(); WR32(TMR_WCR, 0x1);      /* zero the counter */
    unlock(); WR32(TMR_WSR, 0x0);      /* clear stale status */
    for (int i = 0; i < 10; i++) {     /* enable count + reset-on-match (readback-retry: can fail on ASR) */
        unlock(); WR32(TMR_WMER, 0x3);
        unlock(); if (RD32(TMR_WMER) == 0x3) break;
    }
}

void hwwd_enable(unsigned secs)
{
    /* Route WDT match -> FULL SoC reset. Empirically on this ASR1603:
     *  - WDTPCR bit6 (RMW) is what routes to a full SoC reset (self-test: clean reboot, WSR=1). APRR.WDTR
     *    (bit4) alone only resets the CP/modem subsystem (USB flaps, R5 keeps running) — not enough.
     *  - The FREEZE was the WDTPCR 0x7 clock-RESET pulse (0xD4080000 is CP_TIMERS2; WDTPCR gates a clock
     *    SHARED with the live modem). So set bit6 as a READ-MODIFY-WRITE (preserving the clock bits, no
     *    0x7/0x3 pulse) — routes without glitching the modem. Clock is already on (resident/boot). */
    WR32(MPMU_WDTPCR, RD32(MPMU_WDTPCR) | (1u << 6));   /* route WDT -> SoC reset (RMW, no clock pulse) */
    WR32(MPMU_APRR, RD32(MPMU_APRR) | MPMU_APRR_WDTR);  /* + release the WDT reset gate */
    wdt_block_arm(secs_to_ticks(secs));
}

/* Current WDT up-counter (read WVR twice ~1s apart: if it advances, the WDT clock is on / WDTPCR unneeded). */
unsigned hwwd_wvr(void) { unlock(); return RD32(TMR_WVR); }

/* Diagnostic: arm the WDT block ONLY, skipping the MPMU reset-routing writes. Tests whether the bootloader
 * already routed the WDT reset (if so, this still resets the SoC on no-feed, and avoids the freeze). */
void hwwd_enable_wdtonly(unsigned secs)
{
    wdt_block_arm(secs_to_ticks(secs));
}

void hwwd_feed(void)
{
    unlock();
    WR32(TMR_WCR, 0x1);                /* zero the counter -> restart the window */
}

unsigned hwwd_status(void)
{
    unlock();
    return RD32(TMR_WSR);
}

int hwwd_was_reset_cause(void) { return hwwd_status() != 0; }

void hwwd_disable(void)
{
    unlock(); WR32(TMR_WCR, 0x1);
    unlock(); WR32(TMR_WMER, 0x0);
}
