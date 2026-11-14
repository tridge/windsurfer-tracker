/* sitl_hal.c — host-side hardware abstraction for the standalone firmware SITL build (-DSITL).
 *
 * Goal: run the SAME main.c logic on the host, talking to a REAL GT06 server over host TCP, so the
 * state machine / GT06 protocol / command handling / FOTA can be exercised with no flash cycle (like
 * ArduPilot SITL). The seam is the sAPI slot table: sapi.h remaps every SLOT_* into sitl_slots[], which
 * we fill below with host mock fn-pointers. main.c is UNCHANGED except a few small #ifdef SITL seams.
 *
 * Build 32-bit (-m32): the firmware passes URC text via a 32-bit `unsigned` message word (msg[3]) that it
 * casts to a pointer, so host pointers must fit in 32 bits — which also matches the 32-bit ARM ABI.
 *
 * HAL fidelity (closely match real HW):
 *   sockets  -> real host TCP (connect to the actual server; select() guards recv exactly as on device)
 *   GNSS     -> injected NMEA (a thread feeds real $GNGGA/$GNRMC onto the URC queue while GPS is "on")
 *   AT       -> AT+CSQ answered with a +CSQ URC (keeps g_csq fresh, mirrors the real URC path)
 *   battery  -> M0 supervisor 55 AA/0x82 mock over the UART callback ABI (realistic mV)
 *   flash    -> file-backed FOTA staging (reserved -> customer_app on "reboot") + cwprm config
 *   reset    -> commit staged image, then re-exec the process (mirrors OTA reboot -> reconnect)
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <time.h>
#include <pthread.h>
#include <signal.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/select.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <netdb.h>
#include <getopt.h>
#include "sapi.h"

/* ---- the fake slot table (sapi.h SL() maps SLOT_* addresses into this) ---- */
void *sitl_slots[1024];
#define PUTSLOT(addr, fn) (sitl_slots[((unsigned)(addr) - SITL_SLOT_BASE) >> 2] = (void *)(fn))

/* ---- config from the environment ---- */
static char g_srv_host[64] = "127.0.0.1";
static int  g_srv_port = 8711;
static char g_teriid[16] = "888800000000001";   /* 15-digit test terminal id (override: --teriid) */
static int  g_no_reexec;                          /* --no-reexec: sys_reset exits instead of re-exec'ing */
static int  g_persist_cwprm;                       /* SITL_PERSIST_CWPRM: back cwprm with a real file so a
                                                    * SZCS#TERIID/SERVIP write survives a reboot (as on HW) */

/* SITL_TIME_SCALE=N: every HAL wait is divided by N and NMEA UTC runs N x fast; firmware logic unchanged.
 * SITL_TIME_ANCHOR (real epoch) is where sim UTC == real UTC, shared with the harness and re-execs. */
static double g_tscale = 1.0;
static double g_tanchor;
static long long scale_ns(long long ns) { return g_tscale == 1.0 ? ns : (long long)(ns / g_tscale); }
static void sim_utc(struct timespec *ts)
{
    clock_gettime(CLOCK_REALTIME, ts);
    if (g_tscale == 1.0) return;
    double real = ts->tv_sec + ts->tv_nsec * 1e-9;
    double sim = g_tanchor + (real - g_tanchor) * g_tscale;
    ts->tv_sec = (time_t)sim;
    ts->tv_nsec = (long)((sim - (double)ts->tv_sec) * 1e9);
}

static void parse_server(const char *s)           /* "host[:port]" */
{
    char tmp[sizeof g_srv_host]; snprintf(tmp, sizeof tmp, "%s", s);
    char *c = strrchr(tmp, ':');
    if (c) { *c = 0; g_srv_port = atoi(c + 1); }
    snprintf(g_srv_host, sizeof g_srv_host, "%s", tmp);
}

/* ================= debug output ================= */
int sitl_vcom_write(const void *buf, int len)
{
    fwrite(buf, 1, (size_t)len, stdout);
    fflush(stdout);
    return len;
}

/* ================= hwwd stubs (reliability.c itself is the real code, see the timer model) ========= */
void hwwd_enable(unsigned s) { (void)s; }
void hwwd_enable_wdtonly(unsigned s) { (void)s; }
void hwwd_feed(void) {}
int  hwwd_was_reset_cause(void) { return 0; }
unsigned hwwd_status(void) { return 0; }
unsigned hwwd_wvr(void) { return 0; }
void hwwd_disable(void) {}

/* ================= sockets -> real host TCP ================= */
/* Radio simulation for testing the blind buffer + non-blocking reconnect:
 *   SIGUSR1 = coverage loss: connect() and send() fail FAST (the modem reports no network) — the
 *             firmware disconnects, buffers fixes, and its connect attempts fail via the except set.
 *   SIGUSR2 = radio off / server blackhole: send() fails but a connect() HANGS pending forever (SYN
 *             into the void) — exercises the non-blocking-connect path (capture must keep running).
 * Toggling either back lets the firmware reconnect and replay. */
static volatile sig_atomic_t g_cov_down;
static volatile sig_atomic_t g_radio_off;
/* SIGHUP = carrier denial: the current PLMN rejects the SIM (EMM cause 15 — the live G375349
 * 2026-07-23 incident). Data fails; registration reports denied (stat 3); CFUN/COPS=0/COPS=2 do NOT
 * fix it. Manual selection of an available alternate recovers without a server command. */
static volatile sig_atomic_t g_carrier_denied;
static volatile sig_atomic_t g_charging_sim;     /* SIGWINCH toggles the M0 charge-active flag (item 15) */
static void charging_toggle(int s)
{
    (void)s; g_charging_sim = !g_charging_sim;
    const char *m = g_charging_sim ? "[SITL] charging ON\n" : "[SITL] charging OFF\n";
    if (write(2, m, strlen(m))) { }
}
static volatile sig_atomic_t g_sos_sim;          /* SIGTSTP toggles the M0 SOS-button flag (item 17) */
static void sos_toggle(int s)
{
    (void)s; g_sos_sim = !g_sos_sim;
    const char *m = g_sos_sim ? "[SITL] SOS button DOWN\n" : "[SITL] SOS button UP\n";
    if (write(2, m, strlen(m))) { }
}
static int g_plmn_manual;                        /* fw requested manual selection (COPS=0 clears) */
static char g_sim_plmn[8] = "50502";
static char g_denied_plmn[8];
static const char *g_available_plmns = "50501,50502";
static void cov_toggle(int s)
{
    (void)s; g_cov_down = !g_cov_down;
    const char *m = g_cov_down ? "[SITL] coverage DOWN\n" : "[SITL] coverage UP\n";
    if (write(2, m, strlen(m))) { }
}
static void radio_toggle(int s)
{
    (void)s; g_radio_off = !g_radio_off;
    const char *m = g_radio_off ? "[SITL] radio OFF (connects blackhole)\n" : "[SITL] radio ON\n";
    if (write(2, m, strlen(m))) { }
}
static void carrier_toggle(int s)
{
    (void)s; g_carrier_denied = !g_carrier_denied;
    for (int i = 0; i < 8; i++) g_denied_plmn[i] = g_carrier_denied ? g_sim_plmn[i] : 0;
    const char *m = g_carrier_denied ? "[SITL] carrier DENIED (EMM cause 15)\n" : "[SITL] carrier OK\n";
    if (write(2, m, strlen(m))) { }
}
/* per-fd simulated connect outcome: PENDING = never completes (radio off), ERROR = failed (coverage
 * down at connect time, or a stale PENDING after the radio comes back) — select() reports it. */
enum { BH_NONE = 0, BH_PENDING, BH_ERROR };
static unsigned char g_bh[256];

static int sitl_socket(int af, int type, int proto)
{
    (void)af; (void)proto;                       /* firmware type: 1=SOCK_STREAM (TCP), 2=SOCK_DGRAM (UDP) */
    int fd = socket(AF_INET, type == 2 ? SOCK_DGRAM : SOCK_STREAM, 0);
    if (fd >= 0 && fd < 256) g_bh[fd] = BH_NONE;
    return fd;   /* small fd index, fits the 32-byte fd_set */
}
/* the firmware builds a 24-byte sockaddr: [0]=16 [1]=AF_INET [2..3]=port(BE) [4..7]=ip. Dial it VERBATIM so
 * both the main-server connection (cwprm holds the configured IP:port) and the FOTA download (a different
 * host:port from the YCSJ URL) reach the right place. */
static int sitl_connect(int fd, const void *saddr, int salen)
{
    (void)salen;
    const unsigned char *sa = saddr;
    struct sockaddr_in in;
    memset(&in, 0, sizeof in);
    in.sin_family = AF_INET;
    in.sin_port = htons((unsigned short)((sa[2] << 8) | sa[3]));
    in.sin_addr.s_addr = htonl(((unsigned)sa[4] << 24) | ((unsigned)sa[5] << 16) |
                               ((unsigned)sa[6] << 8) | sa[7]);
    if (g_cov_down || g_carrier_denied) {   /* no service: fail fast; select reports it via except */
        if (fd >= 0 && fd < 256) g_bh[fd] = BH_ERROR;
        errno = ENETUNREACH; return -1;
    }
    if (g_radio_off) {                   /* radio off: SYN into the void — pending forever */
        if (fd >= 0 && fd < 256) g_bh[fd] = BH_PENDING;
        errno = EINPROGRESS; return -1;
    }
    return connect(fd, (struct sockaddr *)&in, sizeof in);   /* non-blocking: -1/EINPROGRESS is normal */
}
/* SITL_HANG_SEND_FLAG=<path>: when that file appears, delete it and block the next send() forever (a
 * resident send that never returns). One-shot, so the re-exec after the watchdog reset runs normally. */
static void maybe_hang_send(void)
{
    const char *e = getenv("SITL_HANG_SEND_FLAG");
    if (!e || access(e, F_OK) != 0) return;
    unlink(e);
    fprintf(stderr, "[SITL] send() hang injected\n");
    for (;;) pause();
}
static int sitl_send(int fd, const void *buf, int len, int flags)
{
    (void)flags;
    maybe_hang_send();
    if (g_cov_down || g_radio_off || g_carrier_denied ||   /* no network: fw disconnects + buffers */
        (fd >= 0 && fd < 256 && g_bh[fd] != BH_NONE)) { errno = ENETUNREACH; return -1; }
    ssize_t n = send(fd, buf, (size_t)len, MSG_NOSIGNAL);
    return (int)n;
}
static int sitl_recv(int fd, void *buf, int len, int flags)
{
    (void)flags;
    ssize_t n = recv(fd, buf, (size_t)len, 0);
    return (int)n;   /* 0 = peer closed, <0 = error (guarded by select on the fw side) */
}
static int sitl_close(int fd)
{
    if (fd >= 0 && fd < 256) g_bh[fd] = BH_NONE;
    return close(fd);
}
static int sitl_setsockopt(int s, int level, int opt, const void *val, unsigned len)
{
    (void)s; (void)level; (void)opt; (void)val; (void)len;
    return 0;   /* accept-but-ignore, mirroring the resident (SO_RCVTIMEO not honoured) */
}
static int sitl_ppp_dial(int netid, int up) { (void)netid; (void)up; return 0; }
static int sitl_fcntl(int s, int cmd, int val)
{
    if (cmd == F_SETFL_NB) {   /* non-standard: val 0/1 = blocking/non-blocking */
        int fl = fcntl(s, F_GETFL, 0);
        if (val) fl |= O_NONBLOCK; else fl &= ~O_NONBLOCK;
        return fcntl(s, F_SETFL, fl) < 0 ? -1 : 0;
    }
    if (cmd == F_GETFL_NB) return (fcntl(s, F_GETFL, 0) & O_NONBLOCK) ? 1 : 0;
    return -1;
}
/* fd_set is a 32-byte bitmask on the device (bit = set[fd>>3] & (1<<(fd&7))); translate rd/wr/ex to and
 * from host fd_sets. Fidelity note: lwIP reports a FAILED non-blocking connect in the EXCEPT set
 * (errevent), while Linux reports it readable+writable with SO_ERROR set — so SO_ERROR is folded into
 * the except set here to give the firmware the device semantics. Radio-off (BH_PENDING) fds are never
 * ready; BH_ERROR fds report except immediately. */
#define FWBIT(set, fd) ((set) && ((set)[(fd) >> 3] & (1u << ((fd) & 7))))
static int sitl_select(int maxfdp1, void *rd, void *wr, void *ex, void *timeout)
{
    unsigned char *frd = rd, *fwr = wr, *fex = ex;
    unsigned char ord[32] = {0}, owr[32] = {0}, oex[32] = {0};
    fd_set hrd, hwr; FD_ZERO(&hrd); FD_ZERO(&hwr);
    int maxfd = -1, nready = 0;
    for (int fd = 0; fd < maxfdp1 && fd < 256; fd++) {
        int r = FWBIT(frd, fd), w = FWBIT(fwr, fd), e = FWBIT(fex, fd);
        if (!r && !w && !e) continue;
        if (g_bh[fd] == BH_PENDING) {
            if (g_radio_off) continue;              /* still blackholed: never ready */
            g_bh[fd] = BH_ERROR;                    /* radio back on: fail the stale attempt fast */
        }
        if (g_bh[fd] == BH_ERROR) {
            if (e) { oex[fd >> 3] |= (unsigned char)(1u << (fd & 7)); nready++; }
            continue;
        }
        if (r) FD_SET(fd, &hrd);
        if (w || e) FD_SET(fd, &hwr);               /* writability reveals the connect outcome */
        if (fd > maxfd) maxfd = fd;
    }
    struct timeval tv, *ptv = 0;
    if (timeout) {
        int *t = timeout;                      /* {int32 tv_sec, int32 tv_usec} */
        long long us = scale_ns(((long long)t[0] * 1000000LL + t[1]) * 1000LL) / 1000LL;
        tv.tv_sec = (time_t)(us / 1000000LL); tv.tv_usec = (suseconds_t)(us % 1000000LL);
        ptv = &tv;
    }
    if (nready) { tv.tv_sec = 0; tv.tv_usec = 0; ptv = &tv; }   /* something already reportable */
    int nr = select(maxfd + 1, &hrd, &hwr, 0, ptv);
    for (int fd = 0; nr > 0 && fd <= maxfd; fd++) {
        int hr = FD_ISSET(fd, &hrd), hw = FD_ISSET(fd, &hwr);
        if (!hr && !hw) continue;
        int soerr = 0; socklen_t sl = sizeof soerr;
        if (hw) getsockopt(fd, SOL_SOCKET, SO_ERROR, &soerr, &sl);
        if (soerr) {                                /* failed connect -> device reports except only */
            if (FWBIT(fex, fd)) { oex[fd >> 3] |= (unsigned char)(1u << (fd & 7)); nready++; }
            continue;
        }
        if (hr && FWBIT(frd, fd)) { ord[fd >> 3] |= (unsigned char)(1u << (fd & 7)); nready++; }
        if (hw && FWBIT(fwr, fd)) { owr[fd >> 3] |= (unsigned char)(1u << (fd & 7)); nready++; }
    }
    /* SITL_TX_STUCK_FLAG=<path>: while that file exists no socket reports writable (TCP send buffer
     * full on a link that has silently died). */
    { const char *e = getenv("SITL_TX_STUCK_FLAG");
      if (e && access(e, F_OK) == 0)
          for (int i = 0; i < 32; i++) if (owr[i]) { nready -= __builtin_popcount(owr[i]); owr[i] = 0; } }
    if (frd) memcpy(frd, ord, 32);
    if (fwr) memcpy(fwr, owr, 32);
    if (fex) memcpy(fex, oex, 32);
    return nready;
}

/* ================= URC message queue (GNSS NMEA + AT replies) ================= */
/* The resident delivers 16-byte messages; the fw reads word[3] as a pointer to URC text. We keep a small
 * ring of {unsigned[4]} messages with their own text storage so the pointer stays valid until consumed. */
#define URCQ_DEPTH 64
#define URC_TEXTSZ 256
/* mmap'd MAP_32BIT (see urc_init) so each .text address fits in the 32-bit URC message word msg[3] that the
 * firmware casts back to a pointer — the one place the 32-bit-ARM ABI leaks into the host build. */
static struct urcslot {
    unsigned msg[4];
    char text[URC_TEXTSZ];
} *urcq;
static int urc_head, urc_tail;
static void urc_init(void)
{
    urcq = mmap(0, sizeof(*urcq) * URCQ_DEPTH, PROT_READ | PROT_WRITE,
                MAP_PRIVATE | MAP_ANONYMOUS | MAP_32BIT, -1, 0);
    if (urcq == MAP_FAILED || (uintptr_t)urcq >= 0x100000000ull) {
        fprintf(stderr, "[SITL] MAP_32BIT for the URC queue failed (need low memory)\n");
        exit(1);
    }
    memset(urcq, 0, sizeof(*urcq) * URCQ_DEPTH);
}
static pthread_mutex_t urc_mtx = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t  urc_cnd = PTHREAD_COND_INITIALIZER;

static void urc_push(const char *text)
{
    pthread_mutex_lock(&urc_mtx);
    int nxt = (urc_head + 1) % URCQ_DEPTH;
    if (nxt != urc_tail) {                       /* drop if full (mirrors a bounded resident queue) */
        strncpy(urcq[urc_head].text, text, URC_TEXTSZ - 1);
        urcq[urc_head].text[URC_TEXTSZ - 1] = 0;
        urcq[urc_head].msg[0] = urcq[urc_head].msg[1] = urcq[urc_head].msg[2] = 0;
        urcq[urc_head].msg[3] = (unsigned)(uintptr_t)urcq[urc_head].text;   /* 32-bit host: fits */
        urc_head = nxt;
        pthread_cond_signal(&urc_cnd);
    }
    pthread_mutex_unlock(&urc_mtx);
}
/* qid 1 = the URC queue; anything else is a private, always-empty queue (wd_sleep's timer). */
static int sitl_msgq_create(void *qid, const char *name, int es, int depth, int flags)
{
    (void)es; (void)depth; (void)flags;
    static unsigned next_private = 2;
    *(unsigned *)qid = (name && strcmp(name, "URC QUEUE") == 0) ? 1 : next_private++;
    return 0;
}
static int sitl_urc_attach(unsigned qid, int arg) { (void)qid; (void)arg; return 0; }
/* HW fidelity: the resident timeout is in 5 ms RTOS ticks, not ms. */
static int sitl_msgq_recv(unsigned qid, void *msgout, int timeout_ticks)
{
    int timeout_ms = timeout_ticks > 0 ? timeout_ticks * 5 : timeout_ticks;
    long long wait_ns = timeout_ms > 0 ? scale_ns((long long)timeout_ms * 1000000LL) : 0;
    if (qid != 1) {
        if (wait_ns > 0) usleep((useconds_t)(wait_ns / 1000));
        return 1;
    }
    unsigned *out = msgout;
    pthread_mutex_lock(&urc_mtx);
    if (urc_tail == urc_head && timeout_ms != 0) {
        if (timeout_ms < 0) {
            while (urc_tail == urc_head) pthread_cond_wait(&urc_cnd, &urc_mtx);
        } else {
            struct timespec ts; clock_gettime(CLOCK_REALTIME, &ts);
            ts.tv_sec += (time_t)(wait_ns / 1000000000LL);
            ts.tv_nsec += (long)(wait_ns % 1000000000LL);
            if (ts.tv_nsec >= 1000000000L) { ts.tv_sec++; ts.tv_nsec -= 1000000000L; }
            while (urc_tail == urc_head)
                if (pthread_cond_timedwait(&urc_cnd, &urc_mtx, &ts) != 0) break;
        }
    }
    int got = 0;
    if (urc_tail != urc_head) {
        memcpy(out, urcq[urc_tail].msg, 16);
        urc_tail = (urc_tail + 1) % URCQ_DEPTH;
        got = 1;
    }
    pthread_mutex_unlock(&urc_mtx);
    return got ? 0 : 1;                           /* fw only reads msg[3]; leaves it 0 when empty */
}

/* ================= symbol resolver mock (for CXCS#SAPIFIND) =================
 * The real resident resolves a 32-bit name hash to a function pointer, returning 0 for anything it
 * does not export. We do not know the vendor's hashing scheme, so SAPIFIND guesses: it tries a set of
 * candidate algorithms and reports whichever the resolver accepts.
 *
 * This mock picks ONE scheme (djb2 over the name with the "sAPI_" prefix stripped) and knows a couple
 * of names, so a test can verify SAPIFIND actually discovers an unknown scheme rather than merely not
 * crashing. It deliberately does NOT tell the firmware which one it is. */
static unsigned sitl_secret_hash(const char *name)
{
    const char *p = name;
    if (!strncmp(p, "sAPI_", 5)) p += 5;             /* the transform the firmware must discover */
    unsigned h = 5381u;
    for (; *p; p++) h = h * 33u + (unsigned char)*p; /* djb2 */
    return h;
}
static u32 sitl_resolver(u32 hash, u32 zero)
{
    (void)zero;
    static const char *const known[] = {
        "sAPI_TaskCreate", "sAPI_GnssPowerStatusGet", "sAPI_SmsDelAllMsg",
    };
    for (unsigned i = 0; i < sizeof known / sizeof known[0]; i++)
        if (sitl_secret_hash(known[i]) == hash)
            return 0x7E600000u + i * 4u;             /* plausible resident code pointer */
    /* Exact hash recovered from the real generated API map.  Accepting it lets the USB host test
     * cover mapped-name resolution while the entries above continue to cover hash guessing. */
    if (hash == 0xE6A5FFADu)
        return 0x7E600004u;                          /* sAPI_GnssPowerStatusGet */
    return 0;
}

/* ================= GNSS: power flag + NMEA injector thread ================= */
static volatile int g_gps_on;
/* SITL_GNSS_INITIAL_ON: model a receiver left powered across the app start. */
static int g_gps_power_failures;                  /* SITL_GNSS_POWER_FAILS: fail the first N power-on calls */
static int g_gps_poweroff_failures;               /* SITL_GNSS_POWEROFF_FAILS: fail N power-off calls */
static int g_gps_poweroff_skip;                   /* SITL_GNSS_POWEROFF_SKIP: let the first N succeed first */
/* SITL_NMEA_HZ: fix rate. Above 1 Hz a batch window fills to its configured maximum, which is the only
 * way to exercise the largest frame (and the batch-full path) — at 1 Hz phase alignment leaves it one
 * short. Real hardware is 1 Hz; this exists for tests. */
static unsigned g_nmea_period_us = 1000 * 1000;
/* SITL_NMEA_CFGNAV: model the receiver obeying $CFGNAV,<ms>,... and forgetting it at power-off (the
 * real UC6228 comes back at 1 Hz), so the firmware's rate reassert is exercised. */
static int g_nmea_cfgnav;
/* SITL_NMEA_TIME_OFFSET_S: shift the UTC stamp the injector puts in GGA/RMC. Lets a test place the fix
 * stream just before midnight so a batch straddles 23:59:59 -> 00:00:00, which is otherwise only
 * reachable once a day. */
static long g_nmea_time_offset_s;
static int sitl_gnss_power(int on)
{
    /* Injected faults return -2, not -1: -1 is the resident's "already in that state", which is a
     * success. A mock that used -1 for a genuine failure could no longer express the difference. */
    if (on && g_gps_power_failures > 0) {
        g_gps_power_failures--;
        fprintf(stderr, "[SITL] GNSS power-on failure injected (%d remaining)\n", g_gps_power_failures);
        return -2;
    }
    if (!on && g_gps_poweroff_skip > 0) {
        g_gps_poweroff_skip--;                    /* let the boot power-off through before injecting */
    } else if (!on && g_gps_poweroff_failures > 0) {
        g_gps_poweroff_failures--;
        fprintf(stderr, "[SITL] GNSS power-off failure injected (%d remaining)\n", g_gps_poweroff_failures);
        return -2;
    }
    /* HW fidelity: the real resident returns 0 only on an actual STATE CHANGE and -1 when the receiver
     * is already in the requested state (measured on G306161 over USB, 2026-08-22: off->on and on->off
     * both return 0, repeating either returns -1). The mock used to return 0 unconditionally, which is
     * more forgiving than the hardware — which is why the firmware treating -1 as a failure went
     * unnoticed in SITL for so long. Checked AFTER the injections so a simulated fault still wins. */
    if (g_gps_on == (on ? 1 : 0)) return -1;
    g_gps_on = on ? 1 : 0;
    if (!on && g_nmea_cfgnav) g_nmea_period_us = 1000 * 1000;
    fprintf(stderr, "[SITL] GNSS power %s\n", on ? "on" : "off");
    return 0;
}
static int sitl_gnss_power_status(void) { return g_gps_on; }
static int sitl_gnss_cfg(int a, int b) { (void)a; (void)b; return 0; }
static int sitl_gnss_freq(int a) { (void)a; return 0; }

static unsigned char nmea_cksum(const char *s)   /* XOR between '$' and '*' */
{
    unsigned char c = 0;
    for (const char *p = s + 1; *p && *p != '*'; p++) c ^= (unsigned char)*p;
    return c;
}
static void *nmea_thread(void *arg)
{
    (void)arg;
    /* a slowly-moving track near Canberra so tracking produces changing LOCs */
    double lat = -35.33145, lon = 149.13182;
    for (;;) {
        usleep((useconds_t)(scale_ns((long long)g_nmea_period_us * 1000LL) / 1000LL));   /* 1 Hz default */
        if (!g_gps_on) continue;
        { const char *sf = getenv("SITL_GNSS_SILENT_FLAG");  /* receiver on but emitting nothing */
          if (sf && access(sf, F_OK) == 0) continue; }
        { const char *jf = getenv("SITL_NMEA_JUMP_FLAG");   /* one-shot: GPS time jumps a day + 1 s */
          if (jf && access(jf, F_OK) == 0) { unlink(jf); { const char *js = getenv("SITL_NMEA_JUMP_S"); g_nmea_time_offset_s += js ? atol(js) : 86401; } } }
        lat += 0.00002; lon += 0.00003;
        int lad = (int)(-lat);                   /* south -> magnitude */
        double lam = (-lat - lad) * 60.0;
        int lod = (int)lon;
        double lom = (lon - lod) * 60.0;
        char body[200], line[220];
        /* real UTC time/date so the LOC datetime (used for blind-buffer replay) is genuine wall-clock —
         * a backfilled fix then lands at its true past time, not the reconnect instant. */
        struct timespec ts; sim_utc(&ts);
        time_t stamp = ts.tv_sec + g_nmea_time_offset_s;
        struct tm tmv; gmtime_r(&stamp, &tmv);
        int hh = tmv.tm_hour, mm = tmv.tm_min, ss = tmv.tm_sec;
        int dd = tmv.tm_mday, mo = tmv.tm_mon + 1, yy = (tmv.tm_year + 1900) % 100;
        /* Deciseconds: real receivers stamp a sub-second fraction. Rotate it deterministically rather
         * than reading the host clock — the injector sleeps a whole second, so a clock-derived fraction
         * would be near-constant and inter-fix deltas would always land on whole seconds, never
         * exercising the 0.1 s resolution of the 0xF1 batch. */
        static int ds_seq;
        int dsf = ds_seq++ % 10;
        /* GGA: fix quality 1, 17 sats (>15 on purpose — the 0xF1 header carries 8 bits, the old 0x12
         * nibble saturates at 15), HDOP 1.2 */
        snprintf(body, sizeof body,
                 "$GNGGA,%02d%02d%02d.%d0,%02d%07.4f,S,%03d%07.4f,E,1,17,1.2,560.0,M,0.0,M,,",
                 hh, mm, ss, dsf, lad, lam, lod, lom);
        snprintf(line, sizeof line, "%s*%02X\r\n", body, nmea_cksum(body));
        urc_push(line);
        /* GSA: mode A, fix type 3 (3D), then 12 sat slots, PDOP/HDOP/VDOP */
        snprintf(body, sizeof body, "$GNGSA,A,3,01,02,03,04,05,06,07,08,09,10,11,12,2.1,1.2,1.7");
        snprintf(line, sizeof line, "%s*%02X\r\n", body, nmea_cksum(body));
        urc_push(line);
        /* RMC: valid 'A', speed 6.0 kn, course 45, date ddmmyy. SITL_NMEA_JITTER alternates
         * 5.0 kn/35 deg and 7.0 kn/55 deg (GNSSAVG test). */
        static int jit;
        int jon = getenv("SITL_NMEA_JITTER") != NULL, odd = jit++ & 1;
        snprintf(body, sizeof body,
                 "$GNRMC,%02d%02d%02d.%d0,A,%02d%07.4f,S,%03d%07.4f,E,%.1f,%.1f,%02d%02d%02d,,,A",
                 hh, mm, ss, dsf, lad, lam, lod, lom,
                 jon ? (odd ? 7.0 : 5.0) : 6.0, jon ? (odd ? 55.0 : 35.0) : 45.0, dd, mo, yy);
        snprintf(line, sizeof line, "%s*%02X\r\n", body, nmea_cksum(body));
        { const char *nf = getenv("SITL_NMEA_NORMC_FLAG");  /* receiver drops RMC, GGA/GSA continue */
          if (nf && access(nf, F_OK) == 0) continue; }
        /* HW fidelity: real URC chunks split sentences mid-field. Deliver the RMC in TWO chunks (cut
         * inside the longitude); every 20th sentence insert a +CSQ reply between the halves AND
         * swallow the first bytes of the tail — the modem loses those bytes with the AT interleave
         * and resumes mid-field (07-23 drive test: spliced lon "1" -> 0.0167°, corrupt LOC every
         * ~20 s). The NMEA checksum gate must drop the spliced line; a clean split must still
         * reassemble. Keeps both behaviours regression-tested. */
        {
            static int nsent;
            size_t cut = strlen(line) / 2;
            char h1[220], h2[220];
            memcpy(h1, line, cut); h1[cut] = 0;
            snprintf(h2, sizeof h2, "%s", line + cut);
            urc_push(h1);
            if ((++nsent % 20) == 0) {
                urc_push("\r\n+CSQ: 23,0\r\n");
                size_t eat = strlen(h2); if (eat > 8) eat = 8;
                urc_push(h2 + eat);                /* tail resumes with `eat` bytes swallowed */
            } else {
                urc_push(h2);
            }
            /* Every 60th sentence: a splice whose 8-bit checksum COLLIDES (valid *hh over corrupt
             * content, odds 1/256 on HW) — same "1" lon, status 'A', dot at the wrong offset would
             * be caught by the format belt, so use a full-width absurd lon. Only the continuity
             * (jump) gate can reject it. */
            if ((nsent % 60) == 0) {
                snprintf(body, sizeof body,
                         "$GNRMC,%02d%02d%02d.00,A,%02d%07.4f,S,00001.0000,E,6.0,45.0,%02d%02d%02d,,,A",
                         hh, mm, ss, lad, lam, dd, mo, yy);
                snprintf(line, sizeof line, "%s*%02X\r\n", body, nmea_cksum(body));
                urc_push(line);
            }
        }
    }
    return 0;
}

/* ================= AT command mock (replies on the URC queue, like the real executor) =============
 * Registration model: normally registered-roaming on 50502. SIGHUP denies the current PLMN;
 * SITL_PLMN_CURRENT/AVAILABLE/DENIED also model boot on a rejected carrier and absent alternatives.
 * The COPS=? scan reply is pushed in TWO chunks to exercise the firmware's chunk reassembly. */
static int sitl_at_send(const char *cmd, int len)
{
    if (!cmd || len <= 0) return len;
    if (strstr(cmd, "CSQ")) { urc_push("\r\n+CSQ: 22,0\r\n"); return len; }
    if (strstr(cmd, "CGNSSCMD=")) {                /* receiver command: log it, reply like the modem */
        fprintf(stderr, "[SITL] %.*s\n", len - 1, cmd);
        const char *nav = strstr(cmd, "$CFGNAV,");
        if (nav && g_nmea_cfgnav && g_gps_on) {
            int ms = atoi(nav + 8);
            if (ms >= 100 && ms <= 1000) g_nmea_period_us = (unsigned)ms * 1000;
        }
        urc_push("\r\nOK\r\n");
        return len;
    }
    if (strstr(cmd, "CEREG?")) {
        urc_push(g_carrier_denied ? "\r\n+CEREG: 3,3,\"4f47\",\"006f1e34\",7\r\n\r\nOK\r\n"
                                  : "\r\n+CEREG: 3,5,\"2036\",\"07fd1b01\",7\r\n\r\nOK\r\n");
        return len;
    }
    if (strstr(cmd, "COPS=?")) {                   /* network scan: denied PLMN current, alt available */
        urc_push("\r\n+COPS: (2,\"Optus\",\"Optus\",\"50502\",7),");
        urc_push("(1,\"Telstra Mobile\",\"Telstra\",\"50501\",7),,(0-4),(0-2)\r\n\r\nOK\r\n");
        return len;
    }
    const char *manual = strstr(cmd, "COPS=1,2,\"");
    if (manual) {
        char target[8] = {0}; int n = 0;
        manual += strlen("COPS=1,2,\"");
        while (n < 6 && manual[n] >= '0' && manual[n] <= '9') { target[n] = manual[n]; n++; }
        int available = 0;
        for (const char *p = g_available_plmns; *p; ) {
            if (strncmp(p, target, n) == 0 && (p[n] == ',' || p[n] == 0)) available = 1;
            while (*p && *p != ',') p++;
            if (*p) p++;
        }
        g_carrier_denied = !available || strcmp(target, g_denied_plmn) == 0;
        snprintf(g_sim_plmn, sizeof g_sim_plmn, "%s", target);
        g_plmn_manual = 1;
        fprintf(stderr, "[SITL] operator request %s: %s\n", target,
                g_carrier_denied ? "denied" : "registered");
        urc_push(g_carrier_denied ? "\r\n+CEREG: 3\r\n"
                 : (g_cov_down || g_radio_off) ? "\r\n+CEREG: 2\r\n" : "\r\n+CEREG: 5\r\n");
        return len;
    }
    if (strstr(cmd, "COPS=0")) {
        g_plmn_manual = 0;
        fprintf(stderr, "[SITL] operator request AUTO\n");
        return len;
    }
    if (strstr(cmd, "COPS=2")) { urc_push("\r\n+CEREG: 0\r\n"); return len; }
    if (strstr(cmd, "COPS?")) {
        urc_push(g_carrier_denied && !g_plmn_manual ? "\r\n+COPS: 0,2,\"50502\",7\r\n\r\nOK\r\n"
                                                    : "\r\n+COPS: 0,2,\"50501\",7\r\n\r\nOK\r\n");
        return len;
    }
    if (strstr(cmd, "CFUN=0")) { urc_push("\r\n+CEREG: 0\r\n"); return len; }
    if (strstr(cmd, "CFUN=1")) {                   /* re-attach: still denied while the flag is set */
        urc_push(g_carrier_denied ? "\r\n+CEREG: 3\r\n"
                 : (g_cov_down || g_radio_off) ? "\r\n+CEREG: 2\r\n"
                 : "\r\n+CEREG: 5,\"2036\",\"07fd1b01\",7\r\n");
        return len;
    }
    return len;
}

/* ================= M0 supervisor battery over UART (55 AA / 0x82) ================= */
/* SITL_BATT_MV: what the M0 mock reports. A unit ON CHARGE reads well above a resting cell — the real
 * G378517 sat at 4486 mV overnight and its true value climbed past the firmware's old 4500 ceiling,
 * which rejected every sample and made a full battery report as 0. */
static unsigned g_batt_mv_sim = 4100;
static void (*g_uart_cb)(u32, u32, u32);
static unsigned char g_uart_rx[64];
static int g_uart_rxlen, g_uart_rxpos;
static int sitl_uart_setcfg(int port, const void *cfg) { (void)port; (void)cfg; return 0; }
static int sitl_uart_regcb(int port, void (*cb)(u32, u32, u32), void *res)
{
    (void)port; (void)res; g_uart_cb = cb; return 0;
}
static int sitl_uart_read(u32 ref, void *buf, int len)
{
    (void)ref;
    int n = g_uart_rxlen - g_uart_rxpos;
    if (n > len) n = len;
    if (n > 0) { memcpy(buf, g_uart_rx + g_uart_rxpos, (size_t)n); g_uart_rxpos += n; }
    return n < 0 ? 0 : n;
}
static int sitl_uart_write(int port, const void *buf, int len)
{
    (void)port;
    const unsigned char *b = buf;
    /* battery query 55 AA 02 00 02 0D 0A -> reply 55 AA 82 <plen=22> <payload> <sum> 0D 0A, mV @payload[19,20] */
    if (len >= 5 && b[0] == 0x55 && b[1] == 0xAA && b[2] == 0x02) {
        unsigned mv = g_batt_mv_sim;             /* realistic resting mV; SITL_BATT_MV overrides */
        unsigned char r[29];
        memset(r, 0, sizeof r);
        r[0] = 0x55; r[1] = 0xAA; r[2] = 0x82; r[3] = 22;
        r[4 + 1]  = g_sos_sim ? 0x01 : 0x00;       /* M0 SOS-button flag (SIGTSTP toggles; item 17) */
        r[4 + 13] = g_charging_sim ? 0x04 : 0x00;  /* M0 charge-active flag (SIGWINCH toggles; item 15) */
        r[4 + 19] = (unsigned char)(mv >> 8);
        r[4 + 20] = (unsigned char)(mv & 0xFF);
        unsigned sum = 0;
        for (int k = 2; k < 4 + 22; k++) sum += r[k];
        r[4 + 22] = (unsigned char)(sum & 0xFF);
        r[27] = 0x0D; r[28] = 0x0A;
        memcpy(g_uart_rx, r, sizeof r);
        g_uart_rxlen = sizeof r; g_uart_rxpos = 0;
        if (g_uart_cb) g_uart_cb(1, (u32)sizeof r, 0);   /* deliver via the RX callback, like the resident */
    }
    return len;
}

/* ================= USB-VCOM (if04) host<->device passthrough — simulated as a TCP listener =============
 * --usb-listen <port> opens a loopback TCP listener standing in for the if04 CDC-ACM port a Raspberry Pi
 * would open. A connected client's bytes are delivered to the firmware exactly like the resident CDC RX
 * (the fw registers a callback via SLOT_VCOM_REGCB and drains via SLOT_VCOM_READ); firmware if04 writes
 * (usb_tx) are sent back to the client. Default off. Mirrors the UART RX mock model above. */
static int g_usb_listen_port;                    /* 0 = off (--usb-listen / SITL_USB_LISTEN) */
static volatile int g_usb_cli_fd = -1;           /* connected "USB host" client, -1 if none */
static void (*g_usb_rx_cb)(u32 pending);         /* firmware-registered if04 RX callback (SLOT_VCOM_REGCB) */
static unsigned char g_usb_stage[2048];          /* staging drained by SLOT_VCOM_READ, filled before the cb */
static int g_usb_stage_len, g_usb_stage_pos;

static int sitl_usb_regcb(void (*cb)(u32 pending), const char *name)
{
    (void)name; g_usb_rx_cb = cb; return 0;
}
static int sitl_usb_read(void *buf, int len)     /* SLOT_VCOM_READ: drain staged host->device bytes */
{
    int n = g_usb_stage_len - g_usb_stage_pos;
    if (n > len) n = len;
    if (n > 0) { memcpy(buf, g_usb_stage + g_usb_stage_pos, (size_t)n); g_usb_stage_pos += n; }
    return n < 0 ? 0 : n;
}
int sitl_usb_data_write(const void *buf, int len)   /* firmware if04 TX (usb_tx) -> the connected client */
{
    int fd = g_usb_cli_fd;
    if (fd < 0) return len;                      /* no host attached: drop (an unread CDC IN endpoint) */
    ssize_t n = send(fd, buf, (size_t)len, MSG_NOSIGNAL);
    return n < 0 ? 0 : (int)n;
}
static void *usb_listen_thread(void *arg)
{
    (void)arg;
    int ls = socket(AF_INET, SOCK_STREAM, 0);
    if (ls < 0) { perror("[SITL] usb listen socket"); return 0; }
    int one = 1; setsockopt(ls, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    struct sockaddr_in a; memset(&a, 0, sizeof a);
    a.sin_family = AF_INET; a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    a.sin_port = htons((unsigned short)g_usb_listen_port);
    if (bind(ls, (struct sockaddr *)&a, sizeof a) < 0) { perror("[SITL] usb bind"); close(ls); return 0; }
    if (listen(ls, 1) < 0) { perror("[SITL] usb listen"); close(ls); return 0; }
    fprintf(stderr, "[SITL] USB(if04) listener on 127.0.0.1:%d\n", g_usb_listen_port);
    for (;;) {
        int cs = accept(ls, 0, 0);
        if (cs < 0) { if (errno == EINTR) continue; break; }
        g_usb_cli_fd = cs;
        fprintf(stderr, "[SITL] USB host connected\n");
        for (;;) {
            unsigned char buf[2048];
            ssize_t n = recv(cs, buf, sizeof buf, 0);
            if (n <= 0) break;
            int len = (int)n; if (len > (int)sizeof g_usb_stage) len = (int)sizeof g_usb_stage;
            memcpy(g_usb_stage, buf, (size_t)len);   /* stage, then invoke the fw cb with the count */
            g_usb_stage_len = len; g_usb_stage_pos = 0;
            if (g_usb_rx_cb) g_usb_rx_cb((u32)len);
        }
        g_usb_cli_fd = -1; close(cs);
        fprintf(stderr, "[SITL] USB host disconnected\n");
    }
    close(ls);
    return 0;
}
/* Seed sitl_wstrfwd.bin (== the fw's c:/wstrfwd.bin via sitl_fopen) so fwd_load() enables the bridge at
 * boot without needing the SZCS#FWDSERVER command path. Format matches main.c fwd_store(). */
static void fwd_seed_file(const char *ipport)
{
    char tmp[128]; snprintf(tmp, sizeof tmp, "%s", ipport);
    char *c = strrchr(tmp, ':'); int port = 0;
    if (c) { *c = 0; port = atoi(c + 1); }
    struct in_addr a; unsigned char ip[4] = {0, 0, 0, 0};
    if (inet_pton(AF_INET, tmp, &a) == 1) { unsigned v = ntohl(a.s_addr);
        ip[0] = (unsigned char)(v >> 24); ip[1] = (unsigned char)(v >> 16);
        ip[2] = (unsigned char)(v >> 8);  ip[3] = (unsigned char)v; }
    unsigned char b[32]; memset(b, 0, sizeof b);
    b[0] = 'W'; b[1] = 'F'; b[2] = '1';
    b[4] = ip[0]; b[5] = ip[1]; b[6] = ip[2]; b[7] = ip[3];
    b[8] = (unsigned char)(port >> 8); b[9] = (unsigned char)port;
    int udp = getenv("SITL_FWD_UDP") ? 1 : 0;
    b[10] = (unsigned char)udp;                   /* flags: bit0 = UDP uplink (SITL_FWD_UDP) */
    FILE *f = fopen("sitl_wstrfwd.bin", "wb");
    if (f) { fwrite(b, 1, sizeof b, f); fclose(f); }
    fprintf(stderr, "[SITL] FWD preset -> %d.%d.%d.%d:%d %s\n", ip[0], ip[1], ip[2], ip[3], port,
            udp ? "UDP" : "TCP");
}

/* ================= config file (cwprm.bin) + FOTA flash (file-backed) ================= */
struct sitl_file { unsigned char *buf; unsigned len, pos; int writable; char path[64]; int isfile; };
static unsigned char g_cwprm[0x800];
static unsigned char g_cwbank[0x800];          /* the backup copy, separate so a bad primary can be tested */

#define CWPRM_PERSIST_PATH "sitl_cwprm.bin"
static void cwprm_persist(void)                    /* flush g_cwprm to disk so a reboot re-reads the write */
{
    if (!g_persist_cwprm) return;
    FILE *pf = fopen(CWPRM_PERSIST_PATH, "wb");
    if (pf) { fwrite(g_cwprm, 1, sizeof g_cwprm, pf); fclose(pf); }
}
static void cwprm_build(void)
{
    if (g_persist_cwprm) {                          /* prefer the persisted config (survives re-exec, as HW flash) */
        FILE *pf = fopen(CWPRM_PERSIST_PATH, "rb");
        if (pf) { size_t got = fread(g_cwprm, 1, sizeof g_cwprm, pf); fclose(pf);
                  if (got == sizeof g_cwprm) { memcpy(g_cwbank, g_cwprm, sizeof g_cwbank); return; } }
    }
    memset(g_cwprm, 0, sizeof g_cwprm);
    /* server IP/host ASCII @0x034; the fw parses a dotted quad here, else keeps its default. We store the
     * configured host if it's a dotted quad (connect() honours g_srv_host regardless). */
    snprintf((char *)g_cwprm + CFG_OFF_IP, 0x28, "%.39s", g_srv_host);   /* IP field is 0x28 B (0x034..0x05c) */
    g_cwprm[CFG_OFF_PORT] = (unsigned char)((g_srv_port >> 8) & 0xFF);
    g_cwprm[CFG_OFF_PORT + 1] = (unsigned char)(g_srv_port & 0xFF);
    snprintf((char *)g_cwprm + CFG_OFF_TERIID, 16, "%s", g_teriid);
    snprintf((char *)g_cwprm + CFG_OFF_APN, 0x20, "%s", "simbase");
    cwprm_persist();                               /* seed the persist file on first boot */
    memcpy(g_cwbank, g_cwprm, sizeof g_cwbank);
    if (getenv("SITL_CWPRM_CORRUPT")) {            /* readable primary with a garbage TERIID */
        memset(g_cwprm + CFG_OFF_TERIID, 'X', 15);
        fprintf(stderr, "[SITL] primary config corrupted\n");
    }
}
static FILE_t sitl_fopen(const char *name, const char *mode)
{
    struct sitl_file *f = calloc(1, sizeof *f);
    if (!f) return 0;
    f->writable = (mode && (mode[0] == 'w' || (mode[0] && mode[1] == '+')));
    if (strstr(name, "cwprm") || strstr(name, "cwbank")) {
        f->buf = strstr(name, "cwbank") ? g_cwbank : g_cwprm; f->len = sizeof g_cwprm; f->isfile = 0;
        return f;
    }
    /* otherwise a real host file (rarely used) */
    snprintf(f->path, sizeof f->path, "sitl_%s", strrchr(name, '/') ? strrchr(name, '/') + 1 : name);
    f->isfile = 1;
    FILE *fp = fopen(f->path, f->writable ? "wb" : "rb");
    if (!fp) { free(f); return 0; }
    f->buf = (unsigned char *)fp;               /* stash the FILE* */
    return f;
}
static unsigned sitl_fread(void *dst, unsigned size, unsigned count, FILE_t fp)
{
    /* The resident FS's fread returns BYTES read (not element-count) — cwprm_read relies on
     * got==0x800. Mirror that here so config-command tests are faithful (HAL fidelity). */
    struct sitl_file *f = fp; if (!f) return 0;
    unsigned want = size * count;
    if (f->isfile) return (unsigned)fread(dst, 1, want, (FILE *)f->buf);
    if (f->pos + want > f->len) want = f->len - f->pos;
    memcpy(dst, f->buf + f->pos, want); f->pos += want;
    return want;
}
static unsigned sitl_fwrite(const void *src, unsigned size, unsigned count, FILE_t fp)
{
    /* Resident fwrite returns BYTES written (cwprm_write_one checks wrote==0x800) — mirror it. */
    struct sitl_file *f = fp; if (!f) return 0;
    unsigned want = size * count;
    if (f->isfile) return (unsigned)fwrite(src, 1, want, (FILE *)f->buf);
    if (f->pos + want > f->len) want = f->len - f->pos;
    memcpy(f->buf + f->pos, src, want); f->pos += want;
    return want;
}
static int sitl_fclose(FILE_t fp)
{
    struct sitl_file *f = fp; if (!f) return -1;
    if (f->isfile && f->buf) fclose((FILE *)f->buf);
    else if (f->writable && f->buf == g_cwprm) cwprm_persist();   /* a cwprm write -> flush to disk */
    free(f); return 0;
}
static int sitl_imei(char *out) { memcpy(out, g_teriid, 16); return 0; }

/* Direct resident getters (vendor-style; replace AT+CSQ / AT+CEREG? polls so no AT text rides the
 * URC queue). Mirror the AT mock's registration model: denied -> stat 3, radio off -> 0, else 5. */
static int sitl_get_csq(unsigned char *out)
{
    if (g_cov_down || g_radio_off) { *out = 99; return 1; }
    *out = 23; return 0;
}
static int sitl_get_reg(unsigned *out)
{
    *out = (g_radio_off || g_cov_down) ? 0u : (g_carrier_denied ? 3u : 5u);
    return 0;
}
static void sitl_netstat_start(void) { }   /* getters are always armed in SITL — nothing to enable */
static int sitl_network_init(void)
{
    if (getenv("SITL_HANG_BOOT")) {                /* resident init that never returns (boot deadline test) */
        fprintf(stderr, "[SITL] boot hang injected\n");
        unsetenv("SITL_HANG_BOOT");               /* the re-exec after the reset boots normally */
        for (;;) pause();
    }
    return 0;
}
static int sitl_network_get_cops(char *out)
{
    if (getenv("SITL_COPS_UNAVAILABLE")) return -1;
    snprintf(out, 64, "+COPS: %d,2,\"%s\",7", g_plmn_manual, g_sim_plmn);
    return 0;
}
static int sitl_network_get_cpsi(cpsi_t *out)
{
    if (!out || g_cov_down || g_radio_off) return -1;
    memset(out, 0, sizeof *out);
    snprintf(out->networkmode, sizeof out->networkmode, "LTE,Online");
    snprintf(out->Mnc_Mcc, sizeof out->Mnc_Mcc, "%.3s-%s", g_sim_plmn, g_sim_plmn + 3);
    snprintf(out->LTEBandStr, sizeof out->LTEBandStr, "EUTRAN-BAND3");
    out->TAC = 52200;
    out->CellID = 36519691;
    out->Rsrp = -102;
    out->Rsrq = -12;
    out->Rssi = -75;
    out->SINR = 8;
    out->observedPhysicalCellId = 101;
    return 0;
}
static int sitl_sleep_enabled;
static int sitl_sleep_set_failures;
static int sitl_sleep_alarm_failures;
static int sitl_system_sleep_set(int flag)
{
    if (flag != 0 && flag != 1) return -1;
    if (flag && sitl_sleep_set_failures > 0) {
        sitl_sleep_set_failures--;
        fprintf(stderr, "[SITL] injected system sleep failure\n");
        return -1;
    }
    sitl_sleep_enabled = flag;
    fprintf(stderr, "[SITL] system sleep %s\n", flag ? "enabled" : "disabled");
    return 0;
}
static int sitl_system_sleep_get(void) { return sitl_sleep_enabled; }
static int sitl_system_alarm_wake(unsigned long ms)
{
    if (sitl_sleep_alarm_failures > 0) {
        sitl_sleep_alarm_failures--;
        fprintf(stderr, "[SITL] injected wake alarm failure\n");
        return -1;
    }
    fprintf(stderr, "[SITL] wake alarm %lu ms\n", ms);
    return ms ? 0 : -1;
}

/* Status-LED GPIO mocks (item 14): log every level change so a test can assert the LED pattern. */
static int sitl_gpio_config(unsigned id, unsigned mode) { (void)id; (void)mode; return 0; }
static int sitl_gpio_set(unsigned id, unsigned level)
{
    struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
    fprintf(stderr, "[SITL] LED id=%u level=%u t=%lld\n", id, level,
            (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000);
    return 0;
}

/* FOTA staging: reserved partition -> a host file; close sets a pending marker; reboot commits it. */
#define FOTA_RESERVED "sitl_reserved.bin"
#define FOTA_CUSTOMER "sitl_customer_app.bin"
#define FOTA_PENDING  "sitl_fota_pending"
static FILE *g_fota_fp;
static void sitl_flash_prepare(void) {}
static int  sitl_flash_open(const unsigned char *mode)
{
    (void)mode;
    g_fota_fp = fopen(FOTA_RESERVED, "wb");
    return g_fota_fp ? 0 : -1;
}
static int sitl_flash_write(const void *src, unsigned len)
{
    if (!g_fota_fp) return -1;
    return fwrite(src, 1, len, g_fota_fp) == len ? 0 : -1;
}
static int sitl_flash_close(void)
{
    if (!g_fota_fp) return -1;
    fclose(g_fota_fp); g_fota_fp = 0;
    FILE *p = fopen(FOTA_PENDING, "wb"); if (p) fclose(p);   /* pending-update marker */
    return 0;
}
static unsigned crc32_reflected(const unsigned char *d, unsigned n)
{
    unsigned crc = 0xFFFFFFFFu;
    for (unsigned i = 0; i < n; i++) {
        crc ^= d[i];
        for (int b = 0; b < 8; b++) crc = (crc & 1) ? (crc >> 1) ^ 0xEDB88320u : crc >> 1;
    }
    return crc ^ 0xFFFFFFFFu;
}
static int sitl_flash_verify(unsigned *out)      /* {valid, clen, ~crc32} — mirrors the resident re-verify */
{
    out[0] = 0; out[1] = 0; out[2] = 0;
    FILE *f = fopen(FOTA_RESERVED, "rb"); if (!f) return -1;
    static unsigned char buf[0x40000]; size_t n = fread(buf, 1, sizeof buf, f); fclose(f);
    if (n < 8) return -1;
    unsigned clen = buf[0] | (buf[1] << 8) | (buf[2] << 16) | ((unsigned)buf[3] << 24);
    unsigned want = buf[4] | (buf[5] << 8) | (buf[6] << 16) | ((unsigned)buf[7] << 24);
    if (8u + clen > n) return -1;
    /* the header stores ~crc32 (crc32 ^ 0xFFFFFFFF); return it in the same representation as `want` so the
     * firmware's info[2]==wantcrc check passes (mirrors the resident re-verify). */
    unsigned got = crc32_reflected(buf + 8, clen) ^ 0xFFFFFFFFu;
    out[0] = (got == want) ? 1 : 0; out[1] = clen; out[2] = got;
    return 0;
}

/* ================= sys_reset: commit staged image, then re-exec (OTA "reboot") ================= */
static char **g_argv;
static void sitl_sys_reset(void)
{
    if (access(FOTA_PENDING, F_OK) == 0) {       /* commit reserved -> customer_app, like the boot path */
        rename(FOTA_RESERVED, FOTA_CUSTOMER);
        unlink(FOTA_PENDING);
        fprintf(stderr, "[SITL] FOTA committed reserved->customer_app; rebooting (re-exec)\n");
    } else {
        fprintf(stderr, "[SITL] sys_reset -> re-exec\n");
    }
    fflush(0);
    if (g_no_reexec || getenv("SITL_NO_REEXEC")) exit(0);
    for (int fd = 3; fd < 1024; fd++) close(fd);   /* a real reset drops every socket */
    execv("/proc/self/exe", g_argv);
    _exit(0);
}

/* Resident periodic timer: a thread calling the callback every `period` 5 ms ticks, like the
 * ThreadX timer thread. SITL_TIMER_FAIL makes create fail (watchdog timer-failure path). */
static void (*g_tmr_cb)(unsigned long);
static unsigned long g_tmr_arg, g_tmr_period, g_tmr_initial;
static void *timer_thread(void *arg)
{
    (void)arg;
    /* absolute deadlines: a periodic RTOS timer does not drift by the callback's run time */
    struct timespec next; clock_gettime(CLOCK_MONOTONIC, &next);
    long long step = scale_ns((long long)g_tmr_initial * 5000000LL);
    for (;;) {
        next.tv_nsec += (long)(step % 1000000000LL); next.tv_sec += (time_t)(step / 1000000000LL);
        if (next.tv_nsec >= 1000000000L) { next.tv_nsec -= 1000000000L; next.tv_sec++; }
        while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &next, 0) != 0) { }
        g_tmr_cb(g_tmr_arg);
        step = scale_ns((long long)g_tmr_period * 5000000LL);
    }
    return 0;
}
static int sitl_timer_create(void **h)
{
    if (getenv("SITL_TIMER_FAIL")) { fprintf(stderr, "[SITL] timer create failure injected\n"); return -1; }
    if (h) *h = (void *)1;
    return 0;
}
static void sitl_timer_start(void *h, unsigned long initial, unsigned long period,
                             void (*cb)(unsigned long), unsigned long in)
{
    (void)h;
    if (g_tmr_cb) return;
    g_tmr_cb = cb; g_tmr_arg = in; g_tmr_initial = initial; g_tmr_period = period ? period : 200;
    pthread_t th; pthread_create(&th, 0, timer_thread, 0); pthread_detach(th);
}
static int  sitl_task_create(void **h0, void **h1, unsigned st, unsigned pr,
                             const char *nm, void (*e)(void), unsigned ar)
{ (void)h0; (void)h1; (void)st; (void)pr; (void)nm; (void)e; (void)ar; return 0; }

/* ================= fill the slot table (called from standalone_main via sapi_table_fill) ================= */
int sapi_table_fill(u32 loader_resolver)
{
    (void)loader_resolver;
    PUTSLOT(0x7E1E2358u, sitl_socket);       /* SOCKET   */
    PUTSLOT(0x7E1E2264u, sitl_connect);      /* CONNECT  */
    PUTSLOT(0x7E1E22A0u, sitl_send);         /* SEND     */
    PUTSLOT(0x7E1E21BCu, sitl_recv);         /* RECV     */
    PUTSLOT(0x7E1E22E4u, sitl_close);        /* CLOSE    */
    PUTSLOT(0x7E1E24D4u, sitl_select);       /* SELECT   */
    PUTSLOT(0x7E1E256Cu, sitl_fcntl);        /* FCNTL    */
    PUTSLOT(0x7E1E23F8u, sitl_setsockopt);   /* SETSOCKOPT */
    PUTSLOT(0x7E1E2578u, sitl_ppp_dial);     /* PPP_DIAL */

    PUTSLOT(0x7E1E2444u, sitl_resolver);     /* symbol resolver (CXCS#SAPIFIND oracle) */
    PUTSLOT(0x7E1E21ACu, sitl_gnss_power);   /* GNSS_POWER (== GPS_POWER) */
    PUTSLOT(0x7E1E25DCu, sitl_gnss_power_status); /* sAPI_GnssPowerStatusGet */
    PUTSLOT(0x7E1E2418u, sitl_gnss_cfg);     /* GNSS_CFG  */
    PUTSLOT(0x7E1E2344u, sitl_gnss_freq);    /* GNSS_FREQ */
    PUTSLOT(0x7E1E25D0u, sitl_msgq_create);  /* MSGQ_CREATE */
    PUTSLOT(0x7E1E1F88u, sitl_urc_attach);   /* URC_ATTACH */
    PUTSLOT(0x7E1E1F18u, sitl_msgq_recv);    /* MSGQ_RECV */
    PUTSLOT(0x7E1E2268u, sitl_at_send);      /* AT_SEND  */

    PUTSLOT(0x7E1E1F80u, sitl_fopen);        /* FOPEN    */
    PUTSLOT(0x7E1E227Cu, sitl_fread);        /* FREAD    */
    PUTSLOT(0x7E1E2088u, sitl_fwrite);       /* FWRITE   */
    PUTSLOT(0x7E1E25A8u, sitl_fclose);       /* FCLOSE   */
    PUTSLOT(0x7E1E2374u, sitl_imei);         /* IMEI     */
    PUTSLOT(0x7E1E2544u, sitl_sys_reset);    /* SYS_RESET */
    PUTSLOT(0x7E1E1F54u, sitl_get_csq);      /* GET_CSQ (direct, no AT) */
    PUTSLOT(0x7E1E23C0u, sitl_get_reg);      /* GET_REG (CEREG-equivalent) */
    PUTSLOT(0x7E1E202Cu, sitl_netstat_start);/* NETSTAT_START (getter enable) */
    PUTSLOT(0x7E1E25C4u, sitl_network_init); /* sAPI_NetworkInit */
    PUTSLOT(0x7E1E2230u, sitl_network_get_cpsi); /* sAPI_NetworkGetCpsi */
    PUTSLOT(0x7E1E263Cu, sitl_network_get_cops); /* sAPI_NetworkGetCops */
    PUTSLOT(0x7E1E1F60u, sitl_system_sleep_set); /* sAPI_SystemSleepSet */
    PUTSLOT(0x7E1E2194u, sitl_system_sleep_get); /* sAPI_SystemSleepGet */
    PUTSLOT(0x7E1E22CCu, sitl_system_alarm_wake); /* sAPI_SystemAlarmClock2Wakeup */
    PUTSLOT(0x7E1E254Cu, sitl_gpio_config);  /* GPIO_CONFIG (LED) */
    PUTSLOT(0x7E1E2104u, sitl_gpio_set);     /* GPIO_SET (LED) */

    PUTSLOT(0x7E1E1F3Cu, sitl_uart_read);    /* UART_READ */
    PUTSLOT(0x7E1E21E0u, sitl_uart_regcb);   /* UART_REGCB */
    PUTSLOT(0x7E1E2478u, sitl_uart_setcfg);  /* UART_SETCFG */
    PUTSLOT(0x7E1E24A0u, sitl_uart_write);   /* UART_WRITE */

    PUTSLOT(0x7E1E202Cu, sitl_flash_prepare);/* FLASH_PREPARE */
    PUTSLOT(0x7E1E2620u, sitl_flash_open);   /* FLASH_OPEN */
    PUTSLOT(0x7E1E23ECu, sitl_flash_write);  /* FLASH_WRITE */
    PUTSLOT(0x7E1E2144u, sitl_flash_close);  /* FLASH_CLOSE */
    PUTSLOT(0x7E1E237Cu, sitl_flash_verify); /* FLASH_VERIFY */

    PUTSLOT(0x7E1E211Cu, sitl_task_create);  /* TASK_CREATE */
    PUTSLOT(0x7E1E21B8u, sitl_timer_create); /* TIMER_CREATE */
    PUTSLOT(0x7E1E242Cu, sitl_timer_start);  /* TIMER_START */
    PUTSLOT(0x7E1E2580u, sitl_vcom_write);   /* VCOM_WRITE */
    PUTSLOT(0x7E1E2440u, sitl_usb_regcb);    /* VCOM_REGCB (if04 host->device RX callback register) */
    PUTSLOT(0x7E1E20CCu, sitl_usb_read);     /* VCOM_READ  (drain staged host->device bytes) */
    return 1;
}

/* ================= entry ================= */
extern void standalone_main(u32 loader_resolver);

static void usage(const char *prog)
{
    fprintf(stderr,
        "usage: %s [options]   — SITL host build of the standalone GT06 firmware\n"
        "  -s, --server HOST[:PORT]   GT06 server to connect to (default 127.0.0.1:8711)\n"
        "  -t, --teriid ID15          15-digit terminal id used at login\n"
        "  -n, --no-reexec            on a FOTA 'reboot', exit instead of re-exec'ing\n"
        "  -u, --usb-listen PORT      simulate the if04 USB port as a loopback TCP listener (default off)\n"
        "  -f, --fwd IP:PORT          preset FWDSERVER/FWDPORT (the UAV forward server) without a command\n"
        "  -h, --help                 this help\n"
        "Env fallbacks (overridden by flags): SITL_SERVER, SITL_TERIID, SITL_NO_REEXEC, SITL_USB_LISTEN, SITL_FWD.\n"
        "Test injection: SITL_GNSS_POWER_FAILS=N fails the first N GNSS power-on calls,\n"
        "                SITL_GNSS_POWEROFF_FAILS=N power-off calls, after\n"
        "                SITL_GNSS_POWEROFF_SKIP=N of them have been allowed to succeed.\n"
        "                SITL_SLEEP_ALARM_FAILS=N fails the first N wake-alarm calls,\n"
        "                SITL_SLEEP_SET_FAILS=N fails the first N sleep-enable calls,\n"
        "                SITL_NMEA_HZ=N sets the GNSS fix rate (default 1),\n"
        "                SITL_NMEA_TIME_OFFSET_S=N shifts the stamped UTC (midnight tests),\n"
        "                SITL_BATT_MV=N sets the supervisor battery reading.\n"
        "Clock: SITL_TIME_SCALE=N runs sim time N x real (1..100); SITL_TIME_ANCHOR=epoch pins sim UTC.\n"
        "Signals: SIGUSR1 toggles coverage loss (connect/send fail fast);\n"
        "         SIGUSR2 toggles radio-off (sends fail, connects hang pending — blackhole);\n"
        "         SIGHUP  toggles carrier denial (camped PLMN rejects the SIM; item-16 ladder);\n"
        "         SIGWINCH toggles the M0 charge-active flag (item-15 charging bit; or SITL_CHARGING=1).\n",
        prog);
}

int main(int argc, char **argv)
{
    g_argv = argv;
    urc_init();

    /* env defaults first; command-line flags override them */
    const char *e;
    if ((e = getenv("SITL_PLMN_CURRENT"))) snprintf(g_sim_plmn, sizeof g_sim_plmn, "%s", e);
    if ((e = getenv("SITL_PLMN_AVAILABLE"))) g_available_plmns = e;
    if (getenv("SITL_PLMN_DENIED")) g_carrier_denied = 1;
    if ((e = getenv("SITL_SERVER"))) parse_server(e);
    if ((e = getenv("SITL_TERIID"))) snprintf(g_teriid, sizeof g_teriid, "%s", e);
    if (getenv("SITL_NO_REEXEC")) g_no_reexec = 1;
    if (getenv("SITL_PERSIST_CWPRM")) g_persist_cwprm = 1;   /* cwprm survives reboot (TERIID/SERVIP persist test) */
    if (getenv("SITL_CHARGING")) g_charging_sim = 1;   /* start with the M0 charge-active flag set */
    if (getenv("SITL_SOS")) g_sos_sim = 1;             /* start with the SOS button down (edge fires) */
    if ((e = getenv("SITL_GNSS_POWER_FAILS"))) g_gps_power_failures = atoi(e);
    if ((e = getenv("SITL_GNSS_POWEROFF_FAILS"))) g_gps_poweroff_failures = atoi(e);
    if ((e = getenv("SITL_GNSS_POWEROFF_SKIP"))) g_gps_poweroff_skip = atoi(e);
    if ((e = getenv("SITL_SLEEP_ALARM_FAILS"))) sitl_sleep_alarm_failures = atoi(e);
    if ((e = getenv("SITL_SLEEP_SET_FAILS"))) sitl_sleep_set_failures = atoi(e);
    if (getenv("SITL_GNSS_INITIAL_ON")) g_gps_on = 1;
    if ((e = getenv("SITL_BATT_MV"))) { int v = atoi(e); if (v > 0) g_batt_mv_sim = (unsigned)v; }
    if ((e = getenv("SITL_NMEA_TIME_OFFSET_S"))) g_nmea_time_offset_s = atol(e);
    if (getenv("SITL_NMEA_CFGNAV")) g_nmea_cfgnav = 1;
    if ((e = getenv("SITL_NMEA_HZ"))) {
        int hz = atoi(e);
        if (hz > 0 && hz <= 20) g_nmea_period_us = (unsigned)(1000000 / hz);
    }
    if ((e = getenv("SITL_USB_LISTEN"))) g_usb_listen_port = atoi(e);
    if ((e = getenv("SITL_TIME_SCALE"))) {
        double s = atof(e);
        if (s >= 1.0 && s <= 100.0) g_tscale = s;
    }
    { struct timespec now; clock_gettime(CLOCK_REALTIME, &now);
      g_tanchor = (e = getenv("SITL_TIME_ANCHOR")) ? atof(e) : (double)now.tv_sec; }
    const char *fwd_arg = getenv("SITL_FWD");           /* preset FWDSERVER/FWDPORT (seeded after cwprm) */

    static const struct option lo[] = {
        {"server",     required_argument, 0, 's'},
        {"teriid",     required_argument, 0, 't'},
        {"no-reexec",  no_argument,       0, 'n'},
        {"usb-listen", required_argument, 0, 'u'},
        {"fwd",        required_argument, 0, 'f'},
        {"help",       no_argument,       0, 'h'},
        {0, 0, 0, 0}
    };
    int c;
    while ((c = getopt_long(argc, argv, "s:t:u:f:nh", lo, 0)) != -1) {
        switch (c) {
        case 's': parse_server(optarg); break;
        case 't': snprintf(g_teriid, sizeof g_teriid, "%s", optarg); break;
        case 'n': g_no_reexec = 1; break;
        case 'u': g_usb_listen_port = atoi(optarg); break;
        case 'f': fwd_arg = optarg; break;
        case 'h': usage(argv[0]); return 0;
        default:  usage(argv[0]); return 2;
        }
    }

    /* cwprm stores a dotted quad (the fw parses an IP there); resolve a hostname if given */
    {
        struct in_addr a;
        if (inet_pton(AF_INET, g_srv_host, &a) != 1) {
            struct addrinfo hints, *res = 0;
            memset(&hints, 0, sizeof hints); hints.ai_family = AF_INET;
            if (getaddrinfo(g_srv_host, 0, &hints, &res) == 0 && res) {
                a = ((struct sockaddr_in *)res->ai_addr)->sin_addr;
                inet_ntop(AF_INET, &a, g_srv_host, sizeof g_srv_host);
                freeaddrinfo(res);
            }
        }
    }
    cwprm_build();
    signal(SIGUSR1, cov_toggle);                  /* kill -USR1 <pid>: coverage loss (fast-fail) */
    signal(SIGUSR2, radio_toggle);                /* kill -USR2 <pid>: radio off (connects blackhole) */
    signal(SIGHUP, carrier_toggle);               /* kill -HUP <pid>: PLMN denies the SIM (item 16) */
    signal(SIGWINCH, charging_toggle);            /* kill -WINCH <pid>: M0 charge-active flag (item 15) */
    signal(SIGTSTP, sos_toggle);                  /* kill -TSTP <pid>: M0 SOS-button flag (item 17) */

    if (fwd_arg) fwd_seed_file(fwd_arg);          /* preset FWDSERVER/FWDPORT before the fw reads wstrfwd.bin */

    pthread_t th; pthread_create(&th, 0, nmea_thread, 0);
    if (g_usb_listen_port > 0) { pthread_t ut; pthread_create(&ut, 0, usb_listen_thread, 0); }

    fprintf(stderr, "[SITL] standalone firmware -> %s:%d  teriid=%s\n", g_srv_host, g_srv_port, g_teriid);
    if (g_tscale != 1.0) fprintf(stderr, "[SITL] time scale x%g\n", g_tscale);
    standalone_main(0);
    return 0;
}
