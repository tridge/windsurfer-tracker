/* hwwd.h — Marvell MMP SoC hardware watchdog (Timer-WDT @0xD4080000) for the R5 standalone.
 *
 * A TRUE independent HW watchdog: enable it, feed it periodically; if our firmware stops feeding it,
 * the SoC hard-resets. Register spec RE'd from Linux mmp_wdt.c + ASR1901 vendor u-boot (same ASR/MMP
 * family). The WDT is UNUSED by the resident (confirmed: 0 refs to 0xD4080000 / magics in the golden
 * dump), so we can own it. Clock 256 Hz, 16-bit counter -> max ~256 s.
 *
 * ⚠ Does raw MMIO to reset-routing regs (MPMU_WDTPCR/APRR) we have not exercised before, and the exact
 * ASR1603 WDTPCR bit layout is unverified. Ship DISARMED; validate via a deliberate-hang self-test.
 */
#ifndef HWWD_H
#define HWWD_H

/* Arm the HW watchdog: a hard SoC reset happens if hwwd_feed() isn't called within `secs` seconds.
 * Programs the 3 reset-routing gates (WMER.reset-en + MPMU_APRR.WDTR + MPMU_WDTPCR route) itself. */
void hwwd_enable(unsigned secs);

/* Diagnostic: arm the WDT block ONLY (no MPMU reset-routing writes). If the bootloader already routed the
 * WDT reset, this still hard-resets on no-feed — and avoids the freeze the MPMU writes cause. */
void hwwd_enable_wdtonly(unsigned secs);

/* Feed/kick: zero the counter, restarting the timeout window. Must be called faster than the timeout. */
void hwwd_feed(void);

/* Nonzero if the last boot was caused by THIS watchdog (WSR!=0). Call before hwwd_enable(). */
int hwwd_was_reset_cause(void);

/* Read the raw WSR status (nonzero = we reset via this WDT). */
unsigned hwwd_status(void);

/* Read the WDT up-counter (WVR). Read twice ~1s apart: advancing => the WDT clock is running. */
unsigned hwwd_wvr(void);

/* Stop the watchdog (disarm count + reset-on-match). */
void hwwd_disable(void);

#endif
