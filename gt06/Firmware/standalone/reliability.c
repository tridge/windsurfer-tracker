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
static volatile unsigned g_stall_limit = WD_STALL_ROUNDS;   /* SZCS#WDSTALL: diagnostic override */
static int g_hw_armed;
static void *g_wd_timer;

/* Reset trace: a checksummed record in RAM outside our zeroed BSS, written just before any reset we
 * cause (including the watchdog, which cannot touch the FS) and read at the next boot. It only helps if
 * RAM survives the resident's warm reset; the checksum rejects garbage when it does not. */
struct wdtrace { unsigned magic, reason, phase, uptime, stalls, check; };
#define WDT_MAGIC 0x57445452u                 /* "WDTR" */
#ifdef SITL
#include <stdio.h>
static struct wdtrace g_trace_mem;
#define TRACE (&g_trace_mem)
#else
#define TRACE ((volatile struct wdtrace *)0x7E1E1B00u)   /* below the sAPI table, above our BSS */
#endif
static struct wdtrace g_prev_trace;           /* what the previous boot left (magic 0 = none) */

static unsigned trace_check(unsigned r, unsigned p, unsigned u, unsigned st)
{
    return WDT_MAGIC ^ r ^ (p << 8) ^ (u * 2654435761u) ^ (st << 16) ^ 0xA5A5A5A5u;
}
static void trace_write(unsigned reason, unsigned phase, unsigned stalls)
{
    unsigned u = g_uptime;
    TRACE->reason = reason; TRACE->phase = phase; TRACE->uptime = u; TRACE->stalls = stalls;
    TRACE->check = trace_check(reason, phase, u, stalls);
    TRACE->magic = WDT_MAGIC;
#ifdef SITL
    FILE *f = fopen("sitl_wdtrace.bin", "wb");   /* stands in for RAM surviving the re-exec */
    if (f) { fwrite(TRACE, sizeof *TRACE, 1, f); fclose(f); }
#endif
}
static void trace_load(void)
{
#ifdef SITL
    FILE *f = fopen("sitl_wdtrace.bin", "rb");
    if (f) { if (fread(TRACE, sizeof *TRACE, 1, f) != 1) TRACE->magic = 0; fclose(f); remove("sitl_wdtrace.bin"); }
    else TRACE->magic = 0;
#endif
    struct wdtrace t = { TRACE->magic, TRACE->reason, TRACE->phase, TRACE->uptime, TRACE->stalls, TRACE->check };
    if (t.magic == WDT_MAGIC && t.check == trace_check(t.reason, t.phase, t.uptime, t.stalls))
        g_prev_trace = t;
    TRACE->magic = 0;
}
int wd_prev_trace(unsigned *reason, unsigned *phase, unsigned *uptime)
{
    if (g_prev_trace.magic != WDT_MAGIC) return 0;
    *reason = g_prev_trace.reason; *phase = g_prev_trace.phase; *uptime = g_prev_trace.uptime;
    return 1;
}

void wd_alive(void) { g_alive++; }
void wd_arm(int on) { g_armed = on; }
unsigned wd_uptime(void) { return g_uptime; }
void wd_grace(unsigned secs) { g_grace_until = g_uptime + secs; }
unsigned wd_bootcount(void) { return g_bootcount; }
unsigned wd_stall_max(void) { return g_stall_max; }
unsigned char wd_stall_phase(void) { return g_stall_phase; }
unsigned wd_nodata_count(void) { return g_nodata; }
unsigned wd_stall_limit(void) { return g_stall_limit; }
void wd_set_stall_limit(unsigned secs)
{
    if (secs < WD_STALL_ROUNDS) secs = WD_STALL_ROUNDS;
    if (secs > 300) secs = 300;
    g_stall_limit = secs;
}

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
    trace_write((unsigned)reason, g_wd_phase, 0);
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
        else if (g_uptime > WD_BOOT_TICKS && g_armed) { trace_write(RR_WATCHDOG, 'B', g_uptime); soc_reset(); }
        return;
    }
    if (now != last) {
        stalls = 0; last = now;
        if (g_hw_armed) hwwd_feed();
        return;
    }
    stalls++;
    if ((int)(g_grace_until - g_uptime) > 0) return;       /* deliberate sleep: not a stall */
    if (stalls > g_stall_max) { g_stall_max = stalls; g_stall_phase = g_wd_phase; }
    if (stalls >= g_stall_limit && g_armed) { trace_write(RR_WATCHDOG, g_wd_phase, stalls); soc_reset(); }
}

void wd_hw_arm(unsigned secs)
{
    hwwd_enable(secs);
    g_hw_armed = 1;
}

int reliability_init(int arm, int *timer_ok)
{
    g_armed = arm;
    trace_load();                      /* before anything can write a new one */

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
