/* reliability.c — software watchdog + reset-reason logging. See reliability.h.
 *
 * The watchdog runs from a RESIDENT PERIODIC TIMER (sAPI slots 0x7e1e21b8 create / 0x7e1e242c start): its
 * callback fires ~1 Hz from the resident timer thread — independent of our main loop, so it catches a hang
 * of the loop. task_create was abandoned (it faults; and a timer needs no app-owned thread/stack). The
 * reset (soc_reset) calls the resident sys_reset via sAPI slot 0x7e1e2544. Reset-reason is persisted to
 * the resident FS (fopen/fwrite). Ships DISARMED (arm=0) = reset-reason logging only.
 * NOTE: the timer callback context must stay non-blocking — it does NOT touch the FS; it only checks the
 * liveness counter and, on a stall, calls sys_reset (all non-blocking).
 * Two layers: (1) SOFTWARE watchdog — wd_tick checks liveness, sys_reset on main-loop hang (fast); (2)
 * optional MMP HARDWARE watchdog (hwwd.c, wd_hw_arm) fed from wd_tick — catches wd_tick/RTOS death and
 * backstops a failed sys_reset with an independent hard SoC reset. Both ship DISARMED.
 */
#include "reliability.h"
#include "hwwd.h"

#define WD_MAGIC        0x57443031u   /* "WD01" */
#define WD_STALL_ROUNDS 12            /* timer ticks with no liveness before reset (~ROUNDS * tick period) */
#define WD_TICK_TICKS   200           /* reschedule period in RTOS ticks (~200/sec -> ~1s); calibrate live */

static const char WDFILE[] = "c:/wdstat.bin";
static const char MODE_RB[] = "rb", MODE_WB[] = "wb";

struct wdstat { unsigned magic, reason, bootcount; };

static volatile unsigned g_alive;      /* liveness heartbeat (bumped by the main loop) */
volatile unsigned char g_wd_phase;     /* what blocking call the main loop is in (see reliability.h) */
static volatile unsigned g_uptime;     /* seconds since the tick started (~1 Hz), main-loop timebase */
static unsigned g_bootcount = 1;
static int g_armed;
static int g_hw_armed;                  /* MMP HW watchdog armed -> wd_tick feeds it */
static vcom_write_fn WR;
static void *g_wd_timer;               /* resident timer handle (BSS; the resident owns the TX_TIMER) */

void wd_alive(void) { g_alive++; }
void wd_arm(int on) { g_armed = on; }
unsigned wd_uptime(void) { return g_uptime; }

/* Blocking sleep in milliseconds: MsgQRecv takes 5 ms RTOS ticks on this ThreadX resident (live timing
 * also showed a timeout of 1000 lasts ~5 seconds). Convert here so every existing wd_sleep caller keeps
 * the millisecond contract its name and comments already assume. */
void wd_sleep(int ms)
{
    static unsigned sq;
    static const char SQNAME[] = "wdslp2";
    unsigned char buf[16];
    if (!sq) (*(msgqc_fn *)SLOT_MSGQ_CREATE)(&sq, SQNAME, 16, 4, 11);
    if (sq) {
        int ticks = ms > 0 ? (ms + 4) / 5 : 0;
        (*(msgqr_fn *)SLOT_MSGQ_RECV)(sq, buf, ticks);
    }
    else { volatile unsigned n = 4000000; while (n--) { } }
}

static void wr_hex(const char *label, unsigned v)
{
    if (!WR || g_fwd_en) return;       /* forwarding owns if04 -> no WD:* markers in the data stream */
    char b[48]; int i = 0;
    while (*label) b[i++] = *label++;
    b[i++] = '='; b[i++] = '0'; b[i++] = 'x';
    for (int s = 28; s >= 0; s -= 4) b[i++] = "0123456789ABCDEF"[(v >> s) & 0xF];
    b[i++] = '\r'; b[i++] = '\n';
    WR(b, i);
}

static void wd_write_stat(unsigned reason)
{
    fopen_fn  fo = *(fopen_fn  *)SLOT_FOPEN;
    fwrite_fn fw = *(fwrite_fn *)SLOT_FWRITE;
    fclose_fn fc = *(fclose_fn *)SLOT_FCLOSE;
    struct wdstat s = { WD_MAGIC, reason, g_bootcount };
    FILE_t f = fo(WDFILE, MODE_WB);
    if (f) { fw(&s, sizeof s, 1, f); fc(f); }
}

void wd_set_reason(int reason) { wd_write_stat((unsigned)reason); }

/* ── SoC software reset ── call the resident sys_reset via sAPI slot 0x7e1e2544 (RE-confirmed: it is
 * exactly what the app's own reboot wrapper 0x7e1b1afc invokes). NOT a raw MMIO write: the preboot
 * "Do Globel Reset" 0xD4282D18 RMW turned out to be a DDR clock-change register, not a reset controller
 * (writing it would hang the SoC) — so we use the vendor's own reset path, the same call-by-address
 * mechanism as set_cfun/GNSS. The slot is null-guarded: if unpopulated we just spin (never a bad write). */
static void soc_reset(void)
{
    sys_reset_fn rst = *(sys_reset_fn *)SLOT_SYS_RESET;
    if (rst) rst();
    for (;;) { }                       /* unreached once reset takes; guard if the slot was null */
}

void wd_reason_and_reset(int reason)
{
    wd_write_stat((unsigned)reason);
    soc_reset();
}

/* Periodic-timer callback (runs ~1 Hz from the resident timer thread — independent of our main loop).
 * MUST stay non-blocking: only checks the liveness counter and, on a stall, hard-resets. It does NOT
 * touch the FS (blocking) from this context; the reset-reason for a watchdog reset is bookkept elsewhere. */
static void wd_tick(unsigned long arg)
{
    (void)arg;
    static unsigned last;
    static int stalls;
    static int primed;
    g_uptime++;                                        /* ~1 Hz timebase for the main loop (always) */
    unsigned now = g_alive;
    if (!primed) {                                     /* first tick after arm: establish baseline */
        if (g_hw_armed) hwwd_feed();                   /* keep HW WDT fed through boot until the first feed */
        if (now != 0) { primed = 1; last = now; }      /* wait for the main loop's first feed (boot grace) */
        return;
    }
    if (now != last) {                                 /* main is feeding -> healthy */
        stalls = 0; last = now;
        if (g_hw_armed) hwwd_feed();                   /* feed the HW WDT on a healthy tick */
        wr_hex("WD:alive", now);
        return;
    }
    /* STALLED: deliberately do NOT feed the HW WDT — so if the software sys_reset below fails, the HW
     * watchdog still catches it after its timeout. The HW WDT's own purpose is catching wd_tick/RTOS death. */
    stalls++;
    wr_hex("WD:stall", (unsigned)stalls);              /* per-tick marker: also reveals the tick cadence */
    if (g_wd_phase) wr_hex("WD:phase", g_wd_phase);    /* which blocking call the loop is stuck in */
    if (stalls >= WD_STALL_ROUNDS && g_armed) {
        wr_hex("WD:FIRE", RR_WATCHDOG);
        soc_reset();                                   /* non-blocking hard reset (no FS from timer ctx) */
    }
}

/* Arm the MMP SoC hardware watchdog (see hwwd.c) with a `secs` timeout; wd_tick then feeds it. Logs the
 * pre-existing WSR first (nonzero => the LAST boot was a HW-watchdog reset). Requires the timer armed
 * (reliability_init arm=1) so wd_tick exists to feed it. ⚠ does raw reset-routing MMIO — validate first. */
void wd_hw_arm(unsigned secs)
{
    wr_hex("WD:hw_wsr", hwwd_status());                /* reset cause: !=0 => last boot was this HW WDT */
    hwwd_enable(secs);
    g_hw_armed = 1;
    wr_hex("WD:hw_armed", secs);
}

int reliability_init(vcom_write_fn wr, int arm)
{
    WR = wr; g_armed = arm;

    /* read the last reset reason from c:/wdstat.bin (validated by magic + content). */
    fopen_fn  fo = *(fopen_fn  *)SLOT_FOPEN;
    fread_fn  fr = *(fread_fn  *)SLOT_FREAD;
    fclose_fn fc = *(fclose_fn *)SLOT_FCLOSE;
    struct wdstat s;
    int reason = RR_FIRSTBOOT;
    FILE_t f = fo(WDFILE, MODE_RB);
    if (f) {
        fr(&s, sizeof s, 1, f);
        fc(f);
        if (s.magic == WD_MAGIC) { reason = (int)s.reason; g_bootcount = s.bootcount + 1; }
    }
    wr_hex("WD:reset_reason", (unsigned)reason);
    wr_hex("WD:bootcount", g_bootcount);

    /* re-arm the record as POWERON: if the NEXT reboot isn't one of our intentional resets (which
     * overwrite this first), a fresh boot will read POWERON = external cause (power pull / brownout / HW). */
    wd_write_stat(RR_POWERON);

    /* Start the resident periodic timer (the tick-hook approach). create() allocates a resident TX_TIMER
     * into g_wd_timer; start() runs wd_tick every ~WD_TICK_TICKS ticks. No thread/stack to own (unlike the
     * faulting task_create path). Always started — it also drives the ~1 Hz main-loop timebase (g_uptime);
     * the actual watchdog RESET is gated on g_armed inside wd_tick, so a disarmed build still ticks. */
    int tc = (*(timer_create_fn *)SLOT_TIMER_CREATE)(&g_wd_timer);
    wr_hex("WD:tmr_create", (unsigned)tc);             /* 0 = ok */
    if (tc == 0 && g_wd_timer)
        (*(timer_start_fn *)SLOT_TIMER_START)(g_wd_timer, WD_TICK_TICKS, WD_TICK_TICKS, wd_tick, 0);
    return reason;
}
