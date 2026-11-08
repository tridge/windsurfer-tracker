/* reliability.c — software watchdog + reset-reason logging. See reliability.h.
 *
 * The watchdog runs from a RESIDENT PERIODIC TIMER (sAPI slots 0x7e1e21b8 create / 0x7e1e242c start): its
 * callback fires ~1 Hz from the resident timer thread, independent of our main loop. The reset calls the
 * resident sys_reset via sAPI slot 0x7e1e2544.
 *
 * The callback must never block: it does no USB and no FS I/O. A blocking USB write there (as the old
 * WD:alive marker did) could stop the watchdog and freeze the timebase together.
 */
#include "reliability.h"
#include "hwwd.h"

#define WD_MAGIC        0x57443032u   /* "WD02": magic, reason, bootcount, nodata */
#define WD_MAGIC_V1     0x57443031u   /* "WD01": magic, reason, bootcount */
#define WD_STALL_ROUNDS 12            /* ticks with no liveness before reset */
#ifdef SITL
#define WD_BOOT_TICKS   20
#else
#define WD_BOOT_TICKS   90            /* first feed must arrive within this many ticks of boot */
#endif
#define WD_TICK_TICKS   200           /* reschedule period in RTOS ticks (5 ms each -> 1 s) */

static const char WDFILE[] = "c:/wdstat.bin";
static const char MODE_RB[] = "rb", MODE_WB[] = "wb";

struct wdstat { unsigned magic, reason, bootcount, nodata; };

static volatile unsigned g_alive;      /* liveness heartbeat (bumped by the main loop) */
volatile unsigned char g_wd_phase;
static volatile unsigned g_uptime = 1; /* seconds since boot, from 1: 0 is the "unset" timestamp everywhere */
static volatile unsigned g_grace_until;
static volatile unsigned g_stall_max;
static volatile unsigned char g_stall_phase;
static unsigned g_bootcount = 1;
static unsigned g_nodata;
static volatile int g_armed;
static int g_hw_armed;
static void *g_wd_timer;

void wd_alive(void) { g_alive++; }
void wd_arm(int on) { g_armed = on; }
unsigned wd_uptime(void) { return g_uptime; }
void wd_grace(unsigned secs) { g_grace_until = g_uptime + secs; }
unsigned wd_bootcount(void) { return g_bootcount; }
unsigned wd_stall_max(void) { return g_stall_max; }
unsigned char wd_stall_phase(void) { return g_stall_phase; }
unsigned wd_nodata_count(void) { return g_nodata; }

/* MsgQRecv takes 5 ms RTOS ticks on this ThreadX resident; convert so callers keep a ms contract. */
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

static void wd_write_stat(unsigned reason)
{
    fopen_fn  fo = *(fopen_fn  *)SLOT_FOPEN;
    fwrite_fn fw = *(fwrite_fn *)SLOT_FWRITE;
    fclose_fn fc = *(fclose_fn *)SLOT_FCLOSE;
    if (!fo || !fw || !fc) return;
    struct wdstat s = { WD_MAGIC, reason, g_bootcount, g_nodata };
    unsigned char pp = g_wd_phase; g_wd_phase = 'F';
    FILE_t f = fo(WDFILE, MODE_WB);
    if (f) { fw(&s, sizeof s, 1, f); fc(f); }
    g_wd_phase = pp;
}

void wd_set_reason(int reason) { wd_write_stat((unsigned)reason); }
void wd_set_nodata_count(unsigned n) { g_nodata = n; wd_write_stat(RR_UNEXPECTED); }

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

/* ~1 Hz from the resident timer thread. Only counters; never I/O. */
static void wd_tick(unsigned long arg)
{
    (void)arg;
    static unsigned last;
    static unsigned stalls;
    static int primed;
    g_uptime++;
    unsigned now = g_alive;
    if (!primed) {                     /* boot: wait for the first feed, but not forever */
        if (g_hw_armed) hwwd_feed();
        if (now != 0) { primed = 1; last = now; }
        else if (g_uptime > WD_BOOT_TICKS && g_armed) soc_reset();
        return;
    }
    if (now != last) {
        stalls = 0; last = now;
        if (g_hw_armed) hwwd_feed();
        return;
    }
    stalls++;
    if (stalls > g_stall_max) { g_stall_max = stalls; g_stall_phase = g_wd_phase; }
    if ((int)(g_grace_until - g_uptime) > 0) return;       /* deliberate sleep in progress */
    if (stalls >= WD_STALL_ROUNDS && g_armed) soc_reset();
}

void wd_hw_arm(unsigned secs)
{
    hwwd_enable(secs);
    g_hw_armed = 1;
}

int reliability_init(int arm, int *timer_ok)
{
    g_armed = arm;

    /* Timer first: the boot deadline then also covers the boot-record FS I/O below. */
    int ok = 0;
    for (int i = 0; i < 3 && !ok; i++) {
        int tc = (*(timer_create_fn *)SLOT_TIMER_CREATE)(&g_wd_timer);
        if (tc == 0 && g_wd_timer) {
            (*(timer_start_fn *)SLOT_TIMER_START)(g_wd_timer, WD_TICK_TICKS, WD_TICK_TICKS, wd_tick, 0);
            ok = 1;
        } else {
            wd_sleep(100);
        }
    }
    if (ok) {                          /* the start call has no status: prove it ticks */
        unsigned u0 = g_uptime;
        for (int i = 0; i < 25 && g_uptime == u0; i++) wd_sleep(100);
        ok = g_uptime != u0;
    }
    if (timer_ok) *timer_ok = ok;
    if (!ok) return RR_TIMER_FAIL;     /* no watchdog: skip FS I/O that could hang, caller resets */

    fopen_fn  fo = *(fopen_fn  *)SLOT_FOPEN;
    fread_fn  fr = *(fread_fn  *)SLOT_FREAD;
    fclose_fn fc = *(fclose_fn *)SLOT_FCLOSE;
    struct wdstat s = { 0, 0, 0, 0 };
    int reason = RR_FIRSTBOOT;
    FILE_t f = (fo && fr && fc) ? fo(WDFILE, MODE_RB) : 0;
    if (f) {
        unsigned got = fr(&s, sizeof s, 1, f);   /* this FS's fread returns BYTES */
        fc(f);
        if (got == sizeof s && s.magic == WD_MAGIC) {
            reason = (int)s.reason; g_bootcount = s.bootcount + 1; g_nodata = s.nodata;
        } else if (got >= 12 && s.magic == WD_MAGIC_V1) {
            reason = (int)s.reason; g_bootcount = s.bootcount + 1;
        }
    }

    /* Unless the next reset is one of ours (which overwrite this first), the next boot reads UNEXPECTED. */
    wd_write_stat(RR_UNEXPECTED);
    return reason;
}
