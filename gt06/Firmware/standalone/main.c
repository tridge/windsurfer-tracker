/* main.c — from-scratch customer_app entry (our C only; no vendor application code).
 *
 * Boot: loader -> descriptor +0x14 -> _start (start.S, r0 = resident resolver) -> stash r0 + zero bss
 * -> standalone_main(resolver). Table filled by resolver-replay (sapi_table.c, RESULT 11).
 *
 * STAGE 3c — full tracker from zero vendor code: connect the GT06 server socket + log in (STAGE 2/3a),
 * bring up GNSS + subscribe the "URC QUEUE" (STAGE 3b), then on each NMEA RMC parse the fix and send a
 * real-position GT06 LOC (0x12). Progress is mirrored on if04 (USB-VCOM). Server = wstracker.org:8711 (local test server).
 */
#include "sapi.h"
#include "reliability.h"
#include "hwwd.h"

/* WD_SELFTEST: build a self-terminating watchdog validation image. It ARMS the watchdog and, unless the
 * last boot was itself a watchdog reset, deliberately stops feeding so the monitor fires the reset. So:
 * boot -> hang -> watchdog resets -> next boot reads RR_WATCHDOG -> prints PASS + disarms + runs normally
 * (exactly one reset, no boot-loop). Set to 0 for a normal build. */
/* BATT_MCU: read battery mV from the M0 supervisor MCU over UART1 (55 AA/0x82 protocol — RE:
 * re_battery_voltage.md). Feeds read_batt_level() and echoes "BT:mv" on if04 for bench verification.
 * UART1 is the dedicated supervisor link (unused elsewhere), so this is safe to leave enabled. */
#define BATT_MCU 1
/* FOTA: handle the YCSJ,<http-url>,<port># command — HTTP-download a [clen][~crc32][LZMA] app package,
 * stream it into the resident flash API (stages in `reserved`), verify CRC, then reboot so the resident
 * commits reserved->customer_app. Brick/field-safe (customer_app untouched by a partial write). RE:
 * re_flash_write_api.md. */
#define FOTA 1
/* Firmware version reported in the cxzt# reply as "<FW_VER>-GT06 MCU:<mcu>" (the management UI's fw field,
 * and a repeated marker to confirm a FOTA commit landed). Bump on each build/OTA. */
#define FW_VER "WSTR-STD1"
#ifndef GIT_HASH
#define GIT_HASH "nogit"                        /* set by the Makefile: -DGIT_HASH='"<short sha>"' */
#endif
#define WD_SELFTEST 0
/* WD_ARM: arm the software watchdog in normal operation (validated live 2026-07-19 via the timer tick-hook
 * — see reliability.c). The main loop feeds wd_alive() each iteration; if it stalls ~WD_STALL_ROUNDS ticks
 * the resident timer callback hard-resets. Disarm (0) to ship reset-reason logging only. */
#define WD_ARM 1

/* MMP SoC HARDWARE watchdog (hwwd.c, @0xD4080000). WD_HW_ARM=1 arms it in production (fed from wd_tick,
 * independent hard SoC reset if the RTOS/timer dies). WD_HW_SELFTEST=1 builds a validation image: arm the
 * HW WDT, DON'T feed it, and confirm the SoC hard-resets (next boot reads RR_ST_HUNG + WSR!=0). Both ship
 * 0 (disarmed) — the reset-routing MMIO is RE'd but unproven on ASR1603; validate via the self-test first. */
#define WD_HW_ARM      0     /* ⚠ NOT VIABLE on this SoC — keep 0. The 0xD4080000 WDT is the CP/modem-subsystem
                              * watchdog: routing it to a full SoC/R5 reset needs the WDTPCR 0x7 clock pulse,
                              * which resets the modem's SHARED clock and freezes the running R5 (armed build
                              * boot-loops). Without the pulse (bit6/APRR RMW only) it merely resets the modem
                              * (USB thrash), not the R5. The self-test's "clean reset" was the WDT rebooting an
                              * already-frozen R5. Bisected fully (WD_HW_DIAG). Software watchdog is the shipped
                              * protection. To revisit: an R5-side WDT, or boot-time routing before modem bring-up. */
#define WD_HW_SELFTEST 0
#define WD_HW_SECS     30    /* HW watchdog timeout (production); self-test uses a short one */

/* WD_HW_DIAG: diagnostic — arm the WDT block ONLY (no MPMU routing writes), don't feed, run normally.
 * Tells us in one flash whether (a) the system RUNS (CFG/GNSS/S3c output appears = MPMU writes were the
 * freeze) and (b) it still RESETS ~WD_HW_DIAG_SECS later (= bootloader pre-routed the WDT reset). */
#define WD_HW_DIAG      0
#define WD_HW_DIAG_SECS 15

/* Freestanding libc primitives the compiler may emit calls to (-nostdlib provides none).
 * On the SITL host build libc provides these — don't redefine them. */
#ifndef SITL
void *memset(void *d, int c, __SIZE_TYPE__ n)
{
    unsigned char *p = d;
    while (n--) *p++ = (unsigned char)c;
    return d;
}
void *memcpy(void *d, const void *s, __SIZE_TYPE__ n)
{
    unsigned char *a = d; const unsigned char *b = s;
    while (n--) *a++ = *b++;
    return d;
}
#endif

/* ---- GT06 frame builder in C (validated byte-identical to server/gt06_frames.py) ---- */
static unsigned short crc_itu(const unsigned char *d, int n)
{
    unsigned crc = 0xFFFF;
    for (int i = 0; i < n; i++) {
        crc ^= d[i];
        for (int b = 0; b < 8; b++)
            crc = (crc & 1) ? (crc >> 1) ^ 0x8408 : crc >> 1;
    }
    return (unsigned short)(crc ^ 0xFFFF);
}

/* 18-byte LOC data block (proto 0x12). datetime=0 -> the server uses receipt time (fresh). lat/lon are
 * magnitudes * 1,800,000; hemisphere/validity go in course_status. */
static void build_loc_data(unsigned char *d, const unsigned char *dt, u32 lat_raw, u32 lon_raw,
                           int south, int west, int valid, int sats, int speed, int heading)
{
    for (int i = 0; i < 6; i++) d[i] = dt ? dt[i] : 0;   /* dt=0 => datetime 0 => server uses receipt time */
    d[6] = 0xF0 | (sats & 0x0F);
    d[7] = lat_raw >> 24; d[8] = lat_raw >> 16; d[9] = lat_raw >> 8; d[10] = lat_raw;
    d[11] = lon_raw >> 24; d[12] = lon_raw >> 16; d[13] = lon_raw >> 8; d[14] = lon_raw;
    d[15] = speed;
    unsigned cs = heading & 0x3FF;
    if (valid) cs |= (1 << 12);
    if (!south) cs |= (1 << 10);      /* North bit set when NOT south */
    if (west)  cs |= (1 << 11);
    d[16] = cs >> 8; d[17] = cs;
}

/* ---- Blind buffer: store fixes while out of coverage, replay on reconnect (in RAM; lost on reboot) ----
 * The vendor keeps this in flash; we keep it in RAM — no flash wear, and a stale backlog can't survive a
 * reboot. A fix is 16 B; BLIND_CAP*16 lives in bss (well under the ~180 KB free below the sAPI table). */
#define BLIND_CAP       300     /* 5 min at 1 Hz */
#define BLIND_REPLAY_HZ 5       /* catch-up rate: 5 buffered fixes per 1 Hz live fix => 5 Hz to the server */
typedef struct {
    u32 lat, lon;
    unsigned char dt[6];        /* yy mo dd hh mi ss (from the fix's own RMC) */
    unsigned char sats;         /* 0..15 */
    unsigned char flags;        /* bit0=south bit1=west bit2=valid */
    unsigned char ds;           /* deciseconds 0..9 — needed for a lossless 0xF1 replay */
    unsigned short spd_dkn;     /* 0.1 knots  \ carried so a batched replay is not poorer */
    unsigned short hdg_deg;     /* degrees    / than the live batch it replaces */
} blindfix_t;                   /* 22 bytes */
static blindfix_t g_blind[BLIND_CAP];
static int g_blind_head;         /* next write slot */
static int g_blind_count;        /* buffered fixes (<= BLIND_CAP; oldest dropped when full) */
/* Server capture policy (SZCS#BLIND_EN=<0/1>): 1 while tracking (preserve a racer's track through a
 * coverage hole), 0 in every non-tracking state. Default on — the server reconciles it at login. */
static volatile unsigned char g_blind_en = 1;

static void blind_push_full(u32 lat, u32 lon, const unsigned char *dt,
                            int south, int west, int valid, int sats,
                            unsigned ds, unsigned spd_dkn, unsigned hdg_deg)
{
    blindfix_t *f = &g_blind[g_blind_head];
    f->lat = lat; f->lon = lon;
    for (int i = 0; i < 6; i++) f->dt[i] = dt[i];
    f->sats  = (unsigned char)(sats & 0x0F);
    f->flags = (unsigned char)((south ? 1 : 0) | (west ? 2 : 0) | (valid ? 4 : 0));
    f->ds = (unsigned char)(ds <= 9 ? ds : 0);
    f->spd_dkn = (unsigned short)(spd_dkn > 65535u ? 65535u : spd_dkn);
    f->hdg_deg = (unsigned short)(hdg_deg > 359u ? 0u : hdg_deg);
    g_blind_head = (g_blind_head + 1) % BLIND_CAP;
    if (g_blind_count < BLIND_CAP) g_blind_count++;   /* full => the just-overwritten slot was the oldest */
}
/* Legacy call shape (no speed/heading/sub-second available at the call site). */
static void blind_push(u32 lat, u32 lon, const unsigned char *dt,
                       int south, int west, int valid, int sats)
{
    blind_push_full(lat, lon, dt, south, west, valid, sats, 0, 0, 0);
}
static int blind_peek(blindfix_t *out)                /* copy oldest (FIFO) without removing; 0 if empty */
{
    if (g_blind_count == 0) return 0;
    int tail = (g_blind_head - g_blind_count + BLIND_CAP) % BLIND_CAP;
    *out = g_blind[tail];
    return 1;
}
static int blind_peek_at(int i, blindfix_t *out)       /* i-th oldest (0 = oldest); 0 if out of range */
{
    if (i < 0 || i >= g_blind_count) return 0;
    int tail = (g_blind_head - g_blind_count + BLIND_CAP) % BLIND_CAP;
    *out = g_blind[(tail + i) % BLIND_CAP];
    return 1;
}
static void blind_drop(void) { if (g_blind_count > 0) g_blind_count--; }  /* remove oldest after a good send */
static void blind_drop_n(int n) { while (n-- > 0) blind_drop(); }

static int gt06_frame(unsigned char proto, const unsigned char *data, int dlen,
                      unsigned short serial, unsigned char *out)
{
    out[0] = 0x78; out[1] = 0x78;
    out[2] = (unsigned char)(1 + dlen + 2 + 2);
    out[3] = proto;
    for (int j = 0; j < dlen; j++) out[4 + j] = data[j];
    out[4 + dlen] = serial >> 8; out[5 + dlen] = serial & 0xff;
    unsigned short c = crc_itu(&out[2], dlen + 4);
    out[6 + dlen] = c >> 8; out[7 + dlen] = c & 0xff;
    out[8 + dlen] = 0x0d; out[9 + dlen] = 0x0a;
    return 10 + dlen;
}

/* Build the GT06 login frame (proto 0x01) from a 15-digit terminal ID (TERIID or real IMEI): left-pad
 * to 16 chars, pack to 8 BCD bytes, frame. Returns frame length (18). */
static int build_login(const char *id, unsigned short serial, unsigned char *out)
{
    int n = 0; while (id[n] && n < 15) n++;
    char full[16];
    int pad = 16 - n;
    for (int i = 0; i < 16; i++) full[i] = (i < pad) ? '0' : id[i - pad];
    unsigned char data[8];
    for (int i = 0; i < 8; i++)
        data[i] = ((full[2*i] - '0') << 4) | (full[2*i+1] - '0');
    return gt06_frame(0x01, data, 8, serial, out);
}

/* parse a dotted-decimal IP "a.b.c.d" -> 4 bytes (network order). returns 1 on success. */
static int parse_ip(const char *s, unsigned char out[4])
{
    for (int i = 0; i < 4; i++) {
        unsigned v = 0; int digits = 0;
        while (*s >= '0' && *s <= '9') { v = v * 10 + (*s - '0'); s++; digits++; }
        if (!digits || v > 255) return 0;
        out[i] = (unsigned char)v;
        if (i < 3) { if (*s != '.') return 0; s++; }
    }
    return 1;
}

/* BSD sockaddr_in (16 bytes; connect passed len 24 like the vendor) from ip[4] + port. */
static void fill_server_addr(unsigned char sa[24], const unsigned char ip[4], unsigned port)
{
    for (int i = 0; i < 24; i++) sa[i] = 0;
    sa[0] = 16; sa[1] = 2;                        /* sin_len, AF_INET */
    sa[2] = (port >> 8) & 0xff; sa[3] = port & 0xff;   /* sin_port, network order */
    sa[4] = ip[0]; sa[5] = ip[1]; sa[6] = ip[2]; sa[7] = ip[3];
}

/* ---- USB-VCOM progress reporting ----
 * The resident VCOM write (RES_USB_VCOM_WRITE) is BLOCKING: it returns only once the CDC IN endpoint
 * has drained. Plugged into a CHARGER (enumerated but nothing reading the port), or during a plug/
 * unplug enumeration transition, the FIFO fills and the write blocks for seconds. We call it several
 * times per 1 Hz iteration, so that stalls the main loop — starving LOC capture and, past ~12 s, tripping
 * the software watchdog into a reboot. Guard every write: the resident timer tick advances wd_uptime()
 * independently of the (blocked) main loop, so if a write spans >=VCOM_STALL_SEC it blocked -> suppress
 * VCOM for VCOM_HOLD_SEC. Self-healing: when a reader is present again, writes are fast and resume. */
static vcom_write_fn WR;
static volatile unsigned char g_vcom_en = 1;      /* SZCS#VCOM=<0/1> master switch (bench aid) */
static volatile unsigned char g_fwd_active;        /* FWD TCP connected: gates ring-fill + fast-pump (main.c) */
/* Set whenever forwarding is CONFIGURED (FWDPORT>0), i.e. if04 is owned by the data bridge — established
 * before any boot debug and held through FWD-socket outages/reconnects. All debug (main.c vcom_raw + the
 * watchdog WD:* in reliability.c) is suppressed while set, so a configured UAV link never sees markers.
 * Shared with reliability.c (extern in reliability.h). NOT g_fwd_active: that drops on every TCP outage,
 * which is exactly when we must still keep if04 clean. */
volatile unsigned char g_fwd_en;
/* Forwarding up-path instrumentation (reported via cxzt *FWD:) — pins WHERE the USB->TCP path dies:
 * rx_calls/rx_bytes stop => resident if04 RX stalled; drops climb => ring overflow; tx_bytes flat while
 * rx climbs => TCP send stuck; conns climbing => reconnect churn; passes => actual loop rate. */
static volatile unsigned g_fwd_drops;                      /* ring-overflow byte drops */
static volatile unsigned g_fwd_rx_calls, g_fwd_rx_bytes;   /* resident RX callback invocations + bytes read */
static volatile unsigned g_fwd_tx_bytes, g_fwd_tx_fail;    /* bytes sent to TCP + failed/would-block sends */
static volatile unsigned g_fwd_conns;                      /* successful FWD TCP connects */
static volatile unsigned g_fwd_passes;                     /* fwd_poll invocations (loop-rate probe) */
/* g_fwd_rearm removed with the RX-stall watchdog */
static volatile unsigned g_fwd_rx_max;                     /* largest `pending` seen on the if04 RX callback */
static volatile unsigned char g_fwd_txen = 1;              /* SZCS#FWDTX: TCP->USB direction on/off (diagnostic) */
/* Resident Modem1 USB-RX pipeline probes (RE 2026-07-24). Pins WHICH stage is broken when our callback
 * goes silent: prod advanced by the lower USB completion (not moving => endpoint unarmed / USB silent);
 * cons advanced by the Mdm1RxH handler (prod moves but cons doesn't => that handler is dead); atrdy is
 * the "AT ready" gate — if 0 the handler DISCARDS received records without ever calling us. Read-only. */
#ifdef SITL
static unsigned rx_probe(int w) { (void)w; return 0; }
#else
static unsigned rx_probe(int w)
{
    if (w == 0) return *(volatile unsigned char *)0x7e466057u;   /* producer/write index */
    if (w == 1) return *(volatile unsigned char *)0x7e466059u;   /* consumer/read index  */
    return *(volatile unsigned char *)0x7e45a6a0u;               /* AT-ready gate        */
}
#endif
static volatile unsigned char g_usbrst_req;     /* SZCS#USBRST -> main loop does the USB off/on */
static volatile unsigned char g_fwd_nb;                    /* SZCS#FWDNB: leave FWD socket non-blocking?
                                                            * DEFAULT 0 = revert to BLOCKING after connect,
                                                            * matching the GT06 socket, which is the only
                                                            * config proven to actually transmit on this
                                                            * resident (its non-blocking socket semantics
                                                            * are known-quirky: MSG_DONTWAIT/SO_RCVTIMEO
                                                            * are documented as not honored). */
static volatile unsigned g_fwd_nowr;                       /* passes where the FWD socket was NOT writable */
static volatile unsigned g_fwd_rereg;                      /* if04 RX registration retries (rx never delivered) */
/* Down-direction (socket -> if04) instrumentation. Until 2026-07-25 this direction had NO counters at all,
 * so "I can never command the vehicle" could not be localised: proxy-side tcpdump proved datagrams left the
 * ground station, and nothing on the device said whether they were received, written, or silently dropped. */
static volatile unsigned g_fwd_dn_pkts, g_fwd_dn_bytes;    /* datagrams/bytes received from the FWD socket */
static volatile unsigned g_fwd_dn_wr;                      /* bytes handed to the if04 write */
static volatile unsigned g_fwd_dn_drop;                    /* datagrams dropped by the per-pass write cap */
static volatile unsigned g_fwd_ka;                         /* UDP keepalive datagrams sent */
static volatile unsigned g_fwd_dtd_block;                  /* writes skipped on a REAL dTD shortage */
static volatile unsigned g_fwd_up_drop;                    /* up-path bytes shed (backlog too big/too old) */
static volatile unsigned g_fwd_dn_trunc;                   /* down datagrams that filled the recv buffer */
static volatile unsigned g_fwd_dtd_min = 65535;            /* lowest free-dTD count ever sampled */
static volatile unsigned g_fwd_dtd_zero;                   /* times the dTD pool was seen fully empty */
static unsigned usb_dtd_free(void);                        /* free dTDs (the pool that actually starves) */
static int usb_pool_ok(unsigned now);                       /* pool has headroom for the RX repost? */
static unsigned g_vcom_hold;                       /* suppress VCOM until this uptime (blocked-write backoff) */
static volatile unsigned g_vcom_holds;             /* times a blocked write put if04 into backoff */
#define VCOM_STALL_SEC 2                            /* wd_uptime() delta across a write that means "blocked" */
#define VCOM_HOLD_SEC 30                            /* how long to skip VCOM after a blocked write */
/* While forwarding owns if04 the backoff must be SHORT: a blocked write means the host closed/reopened the
 * port (mavproxy restart, USB re-enumeration), which is transient — but a 30 s mute of the down direction is
 * 30 s in which the aircraft cannot be commanded. Debug markers can afford 30 s; commands cannot. */
#define VCOM_HOLD_FWD_SEC 3
/* Watchdog-safe raw write to if04 with the stall backoff, but NO debug master-switch/FWD gate — used for
 * BOTH debug (via vcom_raw) and the FWD bridge's TCP->USB data (via usb_tx). */
/* Returns 1 if the bytes were handed to the resident write, 0 if suppressed by the backoff — the caller
 * needs that to count what actually reached if04 rather than what it hoped to send. */
static int vcom_write_guarded(const void *buf, int n)
{
    unsigned now = wd_uptime();
    if (g_vcom_hold && (int)(g_vcom_hold - now) > 0) return 0;  /* in backoff: don't risk another block */
    unsigned char pp = g_wd_phase; g_wd_phase = 'V';
    WR(buf, n);
    g_wd_phase = pp;
    if (wd_uptime() - now >= VCOM_STALL_SEC) {      /* the write blocked (no reader) -> back off */
        unsigned hold = g_fwd_en ? VCOM_HOLD_FWD_SEC : VCOM_HOLD_SEC;
        g_vcom_hold = wd_uptime() + hold; if (!g_vcom_hold) g_vcom_hold = 1;
        g_vcom_holds++;
    }
    return 1;
}
static void vcom_raw(const void *buf, int n)
{
    if (!g_vcom_en) return;
#ifndef SITL
    if (g_fwd_en) return;              /* forwarding owns if04: suppress debug markers for the whole time the
                                        * feature is configured (incl. outages), not just while TCP is up.
                                        * SITL keeps debug on stdout — data goes to a separate client fd. */
#endif
    vcom_write_guarded(buf, n);
}

static void vputs(const char *s)
{
    int n = 0;
    while (s[n]) n++;
    vcom_raw(s, n);
}

static void vhex(const char *label, unsigned v)
{
    char b[48]; int i = 0;
    while (*label) b[i++] = *label++;
    b[i++] = '='; b[i++] = '0'; b[i++] = 'x';
    for (int s = 28; s >= 0; s -= 4) b[i++] = "0123456789ABCDEF"[(v >> s) & 0xF];
    b[i++] = '\r'; b[i++] = '\n';
    vcom_raw(b, i);
}

/* dump up to `max` printable chars from a payload (non-printables as '.') for NMEA debugging. */
static void vdump(const char *label, const unsigned char *p, int max)
{
    char b[128]; int i = 0;
    while (*label) b[i++] = *label++;
    b[i++] = '=';
    for (int k = 0; k < max && i < (int)sizeof b - 3; k++) {
        unsigned char c = p[k];
        if (c == 0) break;
        b[i++] = (c >= 32 && c < 127) ? c : '.';
    }
    b[i++] = '\r'; b[i++] = '\n';
    vcom_raw(b, i);
}

/* dump `n` bytes as hex (RE/bench aid) */
static void vhexbuf(const char *label, const unsigned char *p, int n)
{
    char b[96]; int i = 0;
    while (*label) b[i++] = *label++;
    b[i++] = '=';
    for (int k = 0; k < n && i < (int)sizeof b - 4; k++) {
        b[i++] = "0123456789ABCDEF"[p[k] >> 4];
        b[i++] = "0123456789ABCDEF"[p[k] & 15];
    }
    b[i++] = '\r'; b[i++] = '\n';
    vcom_raw(b, i);
}

/* Replay the backlog as BATCHED frames when batching is on. This is the whole point of the message for
 * catch-up: the legacy path sends one 0x12 per fix, BLIND_REPLAY_HZ per RMC (5 fixes/s), so a full
 * 300-fix buffer takes a minute to drain. Packing LOC_BATCH_MAX fixes per 0xF1 frame moves the same
 * backlog in BLIND_REPLAY_HZ * LOC_BATCH_MAX fixes per second — a full buffer drains in ~6 s.
 * Declared here; defined after the batch serialiser it depends on. */
static int blind_replay_batched(int fd, send_fn se, unsigned short *serial);

/* Replay up to BLIND_REPLAY_HZ buffered fixes (each LOC carries its ORIGINAL datetime so the track
 * back-fills to real time). Called once per ~1 s tick — N sends per tick == N Hz to the server.
 * Returns 0 on a failed/short send (the fix stays buffered; caller drops the connection). */
static int blind_replay(int fd, send_fn se, unsigned short *serial)
{
    unsigned char d[18], fr[40];
    for (int rep = 0; rep < BLIND_REPLAY_HZ; rep++) {
        blindfix_t bf;
        if (!blind_peek(&bf)) break;
        build_loc_data(d, bf.dt, bf.lat, bf.lon, bf.flags & 1, bf.flags & 2,
                       (bf.flags & 4) ? 1 : 0, bf.sats, 0, 0);
        int flen = gt06_frame(0x12, d, 18, (*serial)++, fr);
        if (se(fd, fr, flen, 0) != flen) return 0;
        blind_drop();
    }
    vhex("BLIND:remaining", (unsigned)g_blind_count);
    return 1;
}

static void __attribute__((unused)) busy_delay(volatile unsigned n)   /* used only by WD self-test builds */
{
    while (n--)
        __asm__ volatile("nop");
}

/* ---- NMEA RMC parsing ----
 * A GNSS fix arrives as an RMC sentence on the URC queue:
 *   $GNRMC,hhmmss.ss,A,ddmm.mmmm,N,dddmm.mmmm,E,spd,cog,ddmmyy,...*cs
 * We convert ddmm.mmmm -> deg*1,800,000 (GT06 wire units) with pure integer math (no libc float). */

/* parse "ddmm.mmmm" -> magnitude in deg*1,800,000. 0 if empty/invalid. */
static u32 parse_coord(const char *s)
{
    u32 intpart = 0; int seen = 0;
    while (*s && *s != '.') {
        if (*s < '0' || *s > '9') return 0;
        intpart = intpart * 10 + (*s - '0'); s++; seen++;
    }
    if (!seen) return 0;
    u32 deg = intpart / 100, min_int = intpart % 100;     /* dd(d) and mm */
    u32 frac = 0, nd = 0, pow = 1;
    if (*s == '.') {
        s++;
        while (*s >= '0' && *s <= '9' && nd < 5) { frac = frac * 10 + (*s - '0'); s++; nd++; pow *= 10; }
    }
    /* raw = deg*1.8e6 + (min_int + frac/pow)*30000 */
    u32 raw = deg * 1800000u + min_int * 30000u;
    if (nd) raw += (frac * 30000u) / pow;
    return raw;
}

/* "12.34" -> 123 (i.e. value * 10, one decimal kept). Empty/garbage -> 0. Integer only, no libc float. */
static unsigned parse_dec1(const char *s)
{
    unsigned v = 0; int seen = 0;
    while (*s >= '0' && *s <= '9') { v = v * 10 + (unsigned)(*s - '0'); s++; seen++; if (v > 100000u) return 0; }
    if (!seen) return 0;
    v *= 10;
    if (*s == '.') {
        s++;
        if (*s >= '0' && *s <= '9') { v += (unsigned)(*s - '0'); s++; }
        while (*s >= '0' && *s <= '9') s++;          /* remaining decimals are simply truncated */
    }
    if (*s) return 0;                                /* trailing garbage => the field is mangled */
    return v;
}

typedef struct { int found, valid, south, west; u32 lat_raw, lon_raw;
                 unsigned char dt[6];            /* {yy,mo,dd,hh,mi,ss} for blind-buffer replay (0=none) */
                 unsigned spd_dkn;               /* speed, 0.1 knots (RMC field 6) */
                 unsigned hdg_deg;               /* course over ground, whole degrees (RMC field 7) */
                 unsigned char ds;               /* deciseconds from the RMC time fraction (0..9) */
               } rmc_t;

/* NMEA "*hh" checksum: XOR of the chars between '$' and '*'. The modem interleaves AT replies (CSQ
 * poll, every ~20 s) INTO the NMEA stream mid-sentence and the swallowed bytes are unrecoverable, so
 * a spliced line can look plausible (e.g. lon field "1" -> 0.0167 deg, the corrupt-LOC bug seen on
 * 07-23's drive test). Only the checksum reliably rejects such lines. */
static int nmea_cksum_ok(const char *s)
{
    while (*s && *s != '$') s++;
    if (*s != '$') return 0;
    s++;
    unsigned x = 0;
    while (*s && *s != '*') x ^= (unsigned char)*s++;
    if (*s != '*') return 0;
    unsigned h = 0;
    for (int i = 1; i <= 2; i++) {
        char c = s[i]; h <<= 4;
        if (c >= '0' && c <= '9') h |= (unsigned)(c - '0');
        else if (c >= 'A' && c <= 'F') h |= (unsigned)(c - 'A' + 10);
        else return 0;
    }
    return h == x;
}

/* Continuity gate: the 8-bit NMEA checksum still passes ~1/256 mangled lines (a splice every ~20 s
 * -> one slips through every ~85 min of tracking), so also reject any "valid" fix implying an
 * impossible jump from the last accepted fix. Raw units: 1.8e6/deg -> ~0.062 m; 1620 raw/s ~ 100 m/s
 * (legit max ~40 m/s driving, x2 lon-scale margin). A hemisphere flip is treated as a jump. Splices
 * are isolated lines, so 5 consecutive rejects = a genuine relocation (cold fix) -> re-anchor. */
static u32 g_jmp_lat, g_jmp_lon;
static unsigned char g_jmp_hemi, g_jmp_have, g_jmp_rej;
static unsigned g_jmp_t, g_nmea_jump;
static int fix_plausible(const rmc_t *rc, unsigned now)
{
    unsigned char hemi = (unsigned char)((rc->south ? 1 : 0) | (rc->west ? 2 : 0));
    if (g_jmp_have && (now - g_jmp_t) <= 30) {
        unsigned dt = now - g_jmp_t; if (!dt) dt = 1;
        u32 dlat = rc->lat_raw > g_jmp_lat ? rc->lat_raw - g_jmp_lat : g_jmp_lat - rc->lat_raw;
        u32 dlon = rc->lon_raw > g_jmp_lon ? rc->lon_raw - g_jmp_lon : g_jmp_lon - rc->lon_raw;
        if (hemi != g_jmp_hemi || dlat > 1620u * dt || dlon > 1620u * dt) {
            g_nmea_jump++;
            if (++g_jmp_rej < 5) return 0;
        }
    }
    g_jmp_rej = 0;
    g_jmp_lat = rc->lat_raw; g_jmp_lon = rc->lon_raw; g_jmp_hemi = hemi;
    g_jmp_t = now; g_jmp_have = 1;
    return 1;
}

/* scan a (bounded) NMEA payload for an RMC sentence and parse it. found=1 if an RMC was present. */
static int parse_rmc(const char *p, rmc_t *o)
{
    o->found = o->valid = o->south = o->west = 0;
    o->lat_raw = o->lon_raw = 0;
    const char *r = 0;
    for (int i = 0; i < 180 && p[i]; i++)
        if (p[i] == 'R' && p[i+1] == 'M' && p[i+2] == 'C') { r = p + i; break; }
    if (!r) return 0;
    const char *c = r;
    while (*c && *c != ',') c++;
    if (*c != ',') return 0;
    c++;                                    /* -> field 0 (time) */
    char f[9][16] = {{0}};                  /* time,status,lat,ns,lon,ew,spd,cog,date (zero: a short
                                             * sentence leaves late fields empty, not garbage -> dt stays 0) */
    for (int fi = 0; fi < 9; fi++) {
        int k = 0;
        while (*c && *c != ',' && *c != '*' && *c != '\r' && *c != '\n' && k < 15) f[fi][k++] = *c++;
        f[fi][k] = 0;
        if (*c == ',') c++; else break;
    }
    o->found = 1;
    o->valid = (f[1][0] == 'A');
    o->spd_dkn = parse_dec1(f[6]);       /* knots -> 0.1 kn */
    o->hdg_deg = parse_dec1(f[7]) / 10;  /* degrees (drop the tenth) */
    o->ds = 0;
    o->lat_raw = parse_coord(f[2]); o->south = (f[3][0] == 'S');
    o->lon_raw = parse_coord(f[4]); o->west  = (f[5][0] == 'W');
    /* belt (splice defense): NMEA coord fields are fixed-format ddmm.mmmm / dddmm.mmmm — a dot
     * anywhere else means a mangled line, never a real fix. */
    if (f[2][4] != '.' || f[4][5] != '.') o->valid = 0;
    /* time f[0]="hhmmss[.sss]" + date f[8]="ddmmyy" -> GT06 datetime {yy,mo,dd,hh,mi,ss}. Left 0 (=> the
     * server uses receipt time) if either field is short; only used to timestamp buffered blind-buffer fixes. */
    for (int i = 0; i < 6; i++) o->dt[i] = 0;
    {
        const char *tm = f[0], *dm = f[8];
        int tok = 1, dok = 1;
        for (int i = 0; i < 6; i++) { if (tm[i] < '0' || tm[i] > '9') tok = 0; if (dm[i] < '0' || dm[i] > '9') dok = 0; }
        if (tok && dok) {
            unsigned hh = (unsigned)((tm[0]-'0')*10 + (tm[1]-'0'));
            unsigned mi = (unsigned)((tm[2]-'0')*10 + (tm[3]-'0'));
            unsigned sc = (unsigned)((tm[4]-'0')*10 + (tm[5]-'0'));
            unsigned dd = (unsigned)((dm[0]-'0')*10 + (dm[1]-'0'));
            unsigned mo = (unsigned)((dm[2]-'0')*10 + (dm[3]-'0'));
            /* digits alone are not enough: a spliced line can read as hour 99 / month 99, which the
             * server's timegm() silently normalises into a wildly wrong timestamp. */
            if (hh > 23 || mi > 59 || sc > 60 || dd < 1 || dd > 31 || mo < 1 || mo > 12)
                tok = dok = 0;
        }
        if (tok && dok) {
            o->dt[3] = (unsigned char)((tm[0]-'0')*10 + (tm[1]-'0'));   /* hh */
            o->dt[4] = (unsigned char)((tm[2]-'0')*10 + (tm[3]-'0'));   /* mi */
            o->dt[5] = (unsigned char)((tm[4]-'0')*10 + (tm[5]-'0'));   /* ss */
            o->dt[2] = (unsigned char)((dm[0]-'0')*10 + (dm[1]-'0'));   /* dd */
            o->dt[1] = (unsigned char)((dm[2]-'0')*10 + (dm[3]-'0'));   /* mo */
            o->dt[0] = (unsigned char)((dm[4]-'0')*10 + (dm[5]-'0'));   /* yy (2000+) */
        }
        /* sub-second: "hhmmss.sss" -> first fraction digit = deciseconds (item: 0.1 s batch resolution) */
        if (tok && tm[6] == '.' && tm[7] >= '0' && tm[7] <= '9')
            o->ds = (unsigned char)(tm[7] - '0');
    }
    return 1;
}

/* NMEA GGA sats-in-use: RMC carries no satellite count, so read GGA field 6 (0-based, after "GGA,") and
 * cache the last good value for the LOC frame. GGA and RMC arrive as independent reassembled lines. */
static unsigned char g_numsats = 8;             /* last good GGA sats-in-use, capped 15 for the 0x12 nibble */
static unsigned char g_numsats8 = 8;            /* same value uncapped (0..255) for the 0xF1 batch header */
static unsigned char g_hdop_x10;                /* GGA HDOP * 10, 0 = unknown */
static unsigned char g_fixtype;                 /* GSA fix type: 0=unknown 1=no fix 2=2D 3=3D */
static void parse_gga(const char *p)
{
    const char *g = 0;
    for (int i = 0; i < 180 && p[i]; i++)
        if (p[i] == 'G' && p[i+1] == 'G' && p[i+2] == 'A') { g = p + i; break; }
    if (!g) return;
    const char *c = g;
    while (*c && *c != ',') c++;
    if (*c != ',') return;
    c++;                                        /* -> field 0 (time) */
    char f[8][16] = {{0}};                      /* time,lat,ns,lon,ew,fixqual,numsat,hdop (zero-init: a
                                                 * short sentence leaves late fields empty, not garbage) */
    for (int fi = 0; fi < 8; fi++) {
        int k = 0;
        while (*c && *c != ',' && *c != '*' && *c != '\r' && *c != '\n' && k < 15) f[fi][k++] = *c++;
        f[fi][k] = 0;
        if (*c == ',') c++; else break;
    }
    const char *q = f[6];
    int v = 0, any = 0, nd = 0;
    while (*q >= '0' && *q <= '9' && nd < 3) { v = v * 10 + (*q - '0'); q++; any = 1; nd++; }
    if (*q) any = 0;                                   /* trailing junk => not a satellite count */
    if (any && v > 0) {
        g_numsats = (unsigned char)(v > 15 ? 15 : v);      /* legacy 0x12 low nibble caps at 15 */
        g_numsats8 = (unsigned char)(v > 255 ? 255 : v);   /* 0xF1 carries the real count in 8 bits */
    }
    {   /* HDOP (field 7) * 10 — the only horizontal-accuracy figure the NMEA stream gives us.
         * 0 = unknown, 1..254 = HDOP 0.1..25.4, 255 = "worse than 25.4" so the server can map it to a
         * clearly-bad accuracy instead of a plausible-looking 63 m. */
        unsigned h = parse_dec1(f[7]);
        g_hdop_x10 = (unsigned char)(h > 254 ? 255 : h);
    }
}

/* GSA field 1 is the fix type (1 = no fix, 2 = 2D, 3 = 3D) — RMC's A/V flag cannot distinguish 2D from 3D.
 * Cached like the GGA fields: GSA arrives as its own line, independent of RMC. */
static void parse_gsa(const char *p)
{
    const char *g = 0;
    for (int i = 0; i < 180 && p[i]; i++)
        if (p[i] == 'G' && p[i+1] == 'S' && p[i+2] == 'A') { g = p + i; break; }
    if (!g) return;
    const char *c = g;
    while (*c && *c != ',') c++;
    if (*c != ',') return;
    c++;                                        /* -> field 0 (mode1 A/M) */
    while (*c && *c != ',') c++;                /* -> comma before field 1 */
    if (*c != ',') return;
    c++;
    if (*c >= '1' && *c <= '3' && (c[1] == ',' || c[1] == '*' || c[1] == 0))
        g_fixtype = (unsigned char)(*c - '0');         /* exactly one digit, else the field is mangled */
}

/* ---- item: batched LOC (protocol 0xF1) ----------------------------------------------------------
 * While tracking, GNSS gives a fix per second but one GT06 0x12 frame per fix is mostly overhead, and
 * the server writes one jsonl record per frame. Accumulate up to LOCBATCH fixes and ship them as ONE
 * 0xF1 frame every LOCBATCH seconds; the server logs a single record with a `pos` array, exactly as the
 * phone apps already do at 1 Hz.
 *
 * Rules (from the spec):
 *   - flush every LOCBATCH seconds even if fewer than LOCBATCH fixes arrived (send what we have);
 *   - zero fixes in the window => send NOTHING and behave as today's no-fix case;
 *   - LOCBATCH = 0 disables batching entirely and restores the legacy per-fix 0x12 path.
 *
 * Wire format (big-endian), 12-byte header + 14 bytes per element:
 *   0  u8  fmt = 1            5  u8[6] base datetime {yy,mo,dd,hh,mi,ss} (UTC, as 0x12)
 *   1  u8  count N            11 u8  base deciseconds (0..9)
 *   2  u8  satellites 0..255  -- 8 bits, unlike the 4-bit nibble in 0x12
 *   3  u8  fix type           -- 0 unknown, 1 no fix, 2 = 2D, 3 = 3D (from GSA)
 *   4  u8  HDOP * 10          -- 0 = unknown (NMEA gives no metres figure; the server converts)
 *   element: u16 dt_ds (deciseconds after base) | i32 lat 1e-7 deg | i32 lon 1e-7 deg
 *            | u16 speed 0.1 kn | u16 heading deg
 * Signed 1e-7 degrees replaces the 0x12 magnitude+hemisphere-bit encoding: it is finer than the
 * 1/1800000 deg wire unit and removes the hemisphere bits entirely. */
#define LOC_BATCH_MAX     10            /* 12 + 14*10 = 152 B payload — inside the 1-byte 0x78 length */
#define LOC_BATCH_DEFAULT 5
static unsigned char g_loc_batch = LOC_BATCH_DEFAULT;   /* 0 = off (legacy 0x12), else fixes per frame */

typedef struct {
    unsigned char dt[6];        /* datetime of this fix */
    unsigned char ds;           /* deciseconds 0..9 */
    u32 lat_raw, lon_raw;       /* rmc-native magnitude (deg*1800000) — kept so an undelivered batch can
                                 * be handed to the blind buffer, which speaks that format */
    unsigned char south, west;
    unsigned short spd_dkn;     /* 0.1 knots */
    unsigned short hdg_deg;     /* degrees */
} batchfix_t;
static batchfix_t g_batch[LOC_BATCH_MAX];
static int g_batch_n;
/* Frame scratch. Static, not on the stack: the main loop's own frame buffer is sized for the 18-byte
 * 0x12 LOC, and a batch frame is up to 162 bytes — writing one into that buffer smashed the stack. */
/* Flush pacing lives at file scope so SZCS#LOCBATCH can re-phase the window; as a loop local it could
 * only be reset on a tracking change, so the first window after a rate change inherited the old phase. */
static unsigned g_last_batch;
/* Set by SZCS#LOCBATCH; the loop applies it with its OWN `now`. Reading the clock in the command
 * handler could land one tick ahead of the loop's snapshot, making the unsigned `now - g_last_batch`
 * wrap and fire the gate immediately. */
static volatile unsigned char g_batch_rephase;
static unsigned char g_batch_pay[12 + 14 * LOC_BATCH_MAX];
static unsigned char g_batch_frame[10 + 12 + 14 * LOC_BATCH_MAX];

/* A batch spans one flush window (seconds). Anything further from the base than this means the fix
 * stream jumped (clock step, stale partial batch), so the batch is re-based rather than encoding a
 * nonsense delta — a saturated delta would place the point ~109 minutes in the future and stall the
 * server's duplicate suppression until real time caught up. */
#define BATCH_MAX_SPAN_DS 1200          /* 120 s */

/* deg*1800000 magnitude + hemisphere -> signed deg*1e7. 1800000 raw = 1 deg, so e7 = raw * 100 / 18. */
static int raw_to_e7(u32 raw, int neg)
{
    u32 e7 = (raw / 18u) * 100u + ((raw % 18u) * 100u) / 18u;
    return neg ? -(int)e7 : (int)e7;
}

static unsigned batch_ds_of(const batchfix_t *b);

/* Forward delta from `base` to `t` in deciseconds, both seconds-of-day*10.
 *
 * ONE rule, shared by the span check and the serialiser — they previously disagreed: batch_push
 * treated every backward step as a midnight wrap (so a 0.1 s regression looked like +24 h and blew the
 * batch away), while the serialiser clamped small regressions. Only a jump of more than 12 h backwards
 * is a real midnight crossing; anything smaller is jitter or an out-of-order line and clamps to 0. */
#define BATCH_WRAP_DS 432000u                   /* 12 h */
static unsigned batch_delta_ds(unsigned base, unsigned t)
{
    if (t >= base) return t - base;
    if ((base - t) > BATCH_WRAP_DS) return t + 864000u - base;
    return 0;
}

static void batch_fill(batchfix_t *b, const rmc_t *rc)
{
    for (int i = 0; i < 6; i++) b->dt[i] = rc->dt[i];
    b->ds = rc->ds <= 9 ? rc->ds : 0;
    b->lat_raw = rc->lat_raw; b->lon_raw = rc->lon_raw;
    b->south = (unsigned char)(rc->south ? 1 : 0);
    b->west  = (unsigned char)(rc->west ? 1 : 0);
    b->spd_dkn = (unsigned short)(rc->spd_dkn > 65535u ? 65535u : rc->spd_dkn);
    b->hdg_deg = (unsigned short)(rc->hdg_deg > 359u ? 0u : rc->hdg_deg);
}

static void batch_spill_to_blind(void);

static void batch_push(const rmc_t *rc)
{
    batchfix_t nb;
    batch_fill(&nb, rc);

    if (g_batch_n >= LOC_BATCH_MAX) {
        /* Full before the flush ran (bunched NMEA). Push the OLDER batch out to the blind buffer and
         * keep the new fix as the start of the next one. Buffering the new fix instead would invert
         * the order — blind replay runs in this same pass and would transmit the newest fix ahead of
         * the ten older ones still sitting in the batch — and it would drop the fix outright when the
         * blind buffer is disabled. */
        batch_spill_to_blind();
        g_batch[0] = nb;
        g_batch_n = 1;
        return;
    }
    if (g_batch_n > 0) {
        unsigned base = batch_ds_of(&g_batch[0]), t = batch_ds_of(&nb);
        if (batch_delta_ds(base, t) > BATCH_MAX_SPAN_DS) {
            /* clock step or a stale base: the accumulated fixes can't share this base, so keep them
             * (blind buffer) rather than discarding, and restart the batch on the new fix. */
            batch_spill_to_blind();
            g_batch[0] = nb;
            g_batch_n = 1;
            return;
        }
        /* Keep the batch monotonic: the server publishes the LAST element, so an out-of-order line
         * would rewind the live position. Comparing only against the base cannot catch this.
         * The comparison must go through batch_delta_ds, NOT a raw seconds-of-day `<`: at midnight
         * 00:00:00 is numerically less than 23:59:59, so a raw compare rejected every post-midnight
         * fix until the batch flushed. A clamped (zero) delta for a fix that is not simply the same
         * instant is the real "went backwards" signal. */
        unsigned prev = batch_ds_of(&g_batch[g_batch_n - 1]), tnew = batch_ds_of(&nb);
        if (tnew != prev && batch_delta_ds(prev, tnew) == 0)
            return;
    }
    g_batch[g_batch_n++] = nb;
}

/* Hand every pending fix to the blind buffer and clear. Used whenever a batch cannot be delivered
 * (link down, short send, rate change): the legacy per-fix path buffers a failed fix, so batching must
 * not be the reason a racer's track gets a hole. Speed/heading are not carried by the blind-buffer
 * record — a replayed fix goes out as a plain 0x12, exactly as before. */
static void batch_spill_to_blind(void)
{
    if (g_blind_en)
        for (int i = 0; i < g_batch_n; i++)
            blind_push_full(g_batch[i].lat_raw, g_batch[i].lon_raw, g_batch[i].dt,
                            g_batch[i].south, g_batch[i].west, 1, g_numsats,
                            g_batch[i].ds, g_batch[i].spd_dkn, g_batch[i].hdg_deg);
    g_batch_n = 0;
}

/* seconds-of-day of a batch entry, for the delta against the base entry (the batch spans a few seconds,
 * so only hh:mm:ss matters; a midnight wrap is handled by adding a day's worth of deciseconds). */
static unsigned batch_ds_of(const batchfix_t *b)
{
    return ((unsigned)b->dt[3] * 3600u + (unsigned)b->dt[4] * 60u + (unsigned)b->dt[5]) * 10u + b->ds;
}

/* Serialise the accumulated fixes into d (needs 12 + 14*n bytes). Returns the payload length. */
static int build_batch_data(unsigned char *d)
{
    int n = g_batch_n;
    if (n > LOC_BATCH_MAX) n = LOC_BATCH_MAX;
    d[0] = 1;
    d[1] = (unsigned char)n;
    d[2] = g_numsats8;
    d[3] = g_fixtype;
    d[4] = g_hdop_x10;
    for (int i = 0; i < 6; i++) d[5 + i] = g_batch[0].dt[i];
    d[11] = g_batch[0].ds;
    unsigned base = batch_ds_of(&g_batch[0]);
    int o = 12;
    for (int i = 0; i < n; i++) {
        const batchfix_t *b = &g_batch[i];
        unsigned dt_ds = batch_delta_ds(base, batch_ds_of(b));
        if (dt_ds > BATCH_MAX_SPAN_DS) dt_ds = BATCH_MAX_SPAN_DS;
        d[o++] = (unsigned char)(dt_ds >> 8); d[o++] = (unsigned char)dt_ds;
        u32 la = (u32)raw_to_e7(b->lat_raw, b->south), lo = (u32)raw_to_e7(b->lon_raw, b->west);
        d[o++] = (unsigned char)(la >> 24); d[o++] = (unsigned char)(la >> 16);
        d[o++] = (unsigned char)(la >> 8);  d[o++] = (unsigned char)la;
        d[o++] = (unsigned char)(lo >> 24); d[o++] = (unsigned char)(lo >> 16);
        d[o++] = (unsigned char)(lo >> 8);  d[o++] = (unsigned char)lo;
        d[o++] = (unsigned char)(b->spd_dkn >> 8); d[o++] = (unsigned char)b->spd_dkn;
        d[o++] = (unsigned char)(b->hdg_deg >> 8); d[o++] = (unsigned char)b->hdg_deg;
    }
    return o;
}

/* Serialise `n` blind-buffer entries starting at the oldest into a 0xF1 payload. Mirrors
 * build_batch_data(), but reads the blind ring instead of the live batch. Returns the payload length,
 * or 0 if nothing could be packed. */
static int build_batch_from_blind(unsigned char *d, int n)
{
    blindfix_t f0;
    if (n <= 0 || !blind_peek_at(0, &f0)) return 0;
    if (n > LOC_BATCH_MAX) n = LOC_BATCH_MAX;
    d[0] = 1;
    d[1] = (unsigned char)n;
    d[2] = g_numsats8;
    d[3] = g_fixtype;
    d[4] = g_hdop_x10;
    for (int i = 0; i < 6; i++) d[5 + i] = f0.dt[i];
    d[11] = f0.ds;
    unsigned base = ((unsigned)f0.dt[3] * 3600u + (unsigned)f0.dt[4] * 60u + (unsigned)f0.dt[5]) * 10u + f0.ds;
    int o = 12;
    for (int i = 0; i < n; i++) {
        blindfix_t f;
        if (!blind_peek_at(i, &f)) { d[1] = (unsigned char)i; break; }
        unsigned t = ((unsigned)f.dt[3] * 3600u + (unsigned)f.dt[4] * 60u + (unsigned)f.dt[5]) * 10u + f.ds;
        unsigned dt_ds = batch_delta_ds(base, t);
        if (dt_ds > BATCH_MAX_SPAN_DS) { d[1] = (unsigned char)i; break; }   /* older span than one frame
                                                                             * can express: stop short */
        d[o++] = (unsigned char)(dt_ds >> 8); d[o++] = (unsigned char)dt_ds;
        u32 la = (u32)raw_to_e7(f.lat, f.flags & 1), lo = (u32)raw_to_e7(f.lon, f.flags & 2);
        d[o++] = (unsigned char)(la >> 24); d[o++] = (unsigned char)(la >> 16);
        d[o++] = (unsigned char)(la >> 8);  d[o++] = (unsigned char)la;
        d[o++] = (unsigned char)(lo >> 24); d[o++] = (unsigned char)(lo >> 16);
        d[o++] = (unsigned char)(lo >> 8);  d[o++] = (unsigned char)lo;
        d[o++] = (unsigned char)(f.spd_dkn >> 8); d[o++] = (unsigned char)f.spd_dkn;
        d[o++] = (unsigned char)(f.hdg_deg >> 8); d[o++] = (unsigned char)f.hdg_deg;
    }
    return d[1] ? o : 0;
}

/* Note: replay frames carry up to LOC_BATCH_MAX fixes regardless of the configured LOCBATCH. LOCBATCH
 * sets the LIVE window (how long the device waits before shipping), which has no bearing on how densely
 * an already-recorded backlog should be packed — packing it full is exactly what makes catch-up fast. */
static int blind_replay_batched(int fd, send_fn se, unsigned short *serial)
{
    for (int rep = 0; rep < BLIND_REPLAY_HZ && g_blind_count > 0; rep++) {
        int want = g_blind_count < LOC_BATCH_MAX ? g_blind_count : LOC_BATCH_MAX;
        int blen = build_batch_from_blind(g_batch_pay, want);
        if (blen <= 0) break;
        int sent_n = g_batch_pay[1];
        int flen = gt06_frame(0xF1, g_batch_pay, blen, (*serial)++, g_batch_frame);
        if (se(fd, g_batch_frame, flen, 0) != flen) return 0;   /* fixes stay buffered */
        blind_drop_n(sent_n);
    }
    vhex("BLIND:remaining", (unsigned)g_blind_count);
    return 1;
}

/* ---- sAPI symbol-hash candidates (CXCS#SAPIFIND) -------------------------------------------------
 * We do not know how the vendor turns a symbol name into the 32-bit hash its resolver expects, and it
 * is probably a build-time artifact rather than something present in either binary. So enumerate the
 * usual suspects and let the live resolver adjudicate: a non-zero return means that hash is a real
 * exported symbol, which for a name we chose is essentially proof of the algorithm.
 *
 * Index = algorithm * SAPI_HASH_XFORMS + transform, so one number identifies both. */
typedef u32 (*resolver_fn)(u32 hash, u32 zero);

#define SAPI_HASH_XFORMS 4u
#define SAPI_HASH_FUNCS  9u
#define SAPI_HASH_ALGOS  (SAPI_HASH_XFORMS * SAPI_HASH_FUNCS)

/* Apply transform t to the name, one character at a time (no buffers: names are short and we only
 * need the byte stream). Returns 0 when the character should be skipped. */
static int sapi_xform_skip(unsigned t, const char *name, int i)
{
    if (t == 1) {                                   /* drop a leading "sAPI_" */
        if (name[0]=='s' && name[1]=='A' && name[2]=='P' && name[3]=='I' && name[4]=='_')
            return i < 5;
    }
    return 0;
}
static unsigned char sapi_xform_ch(unsigned t, unsigned char c)
{
    if (t == 2 && c >= 'A' && c <= 'Z') return (unsigned char)(c + 32);   /* lower */
    if (t == 3 && c >= 'a' && c <= 'z') return (unsigned char)(c - 32);   /* UPPER */
    return c;
}

static u32 sapi_name_hash(unsigned idx, const char *name)
{
    unsigned fn = idx / SAPI_HASH_XFORMS, t = idx % SAPI_HASH_XFORMS;
    u32 h;
    switch (fn) {
    case 0: h = 5381u; break;                       /* djb2   */
    case 1: h = 5381u; break;                       /* djb2^  */
    case 2: h = 0u; break;                          /* sdbm   */
    case 3: h = 0x811c9dc5u; break;                 /* fnv1   */
    case 4: h = 0x811c9dc5u; break;                 /* fnv1a  */
    case 5: h = 0u; break;                          /* elf    */
    case 6: h = 0u; break;                          /* joaat  */
    case 7: h = 0xFFFFFFFFu; break;                 /* crc32  */
    default: h = 0u; break;                         /* sum    */
    }
    for (int i = 0; name[i]; i++) {
        if (sapi_xform_skip(t, name, i)) continue;
        unsigned char c = sapi_xform_ch(t, (unsigned char)name[i]);
        switch (fn) {
        case 0: h = h * 33u + c; break;
        case 1: h = (h * 33u) ^ c; break;
        case 2: h = c + (h << 6) + (h << 16) - h; break;
        case 3: h = h * 0x01000193u; h ^= c; break;
        case 4: h ^= c; h = h * 0x01000193u; break;
        case 5: { h = (h << 4) + c; u32 g = h & 0xF0000000u; if (g) h ^= g >> 24; h &= ~g; break; }
        case 6: h += c; h += h << 10; h ^= h >> 6; break;
        case 7: { h ^= c; for (int b = 0; b < 8; b++) h = (h & 1u) ? ((h >> 1) ^ 0xEDB88320u) : (h >> 1); break; }
        default: h += c; break;
        }
    }
    if (fn == 6) { h += h << 3; h ^= h >> 11; h += h << 15; }
    if (fn == 7) h ^= 0xFFFFFFFFu;
    return h;
}

static int put_hex32(char *b, u32 v)
{
    static const char hx[] = "0123456789abcdef";
    for (int i = 7; i >= 0; i--) b[7 - i] = hx[(v >> (i * 4)) & 0xF];
    return 8;
}

/* ---- item 1: idle/tracking state, driven by the server TIMER command ----
 * The server pushes TIMER,T1,T2 (0x80 online command). We NEVER use the accelerometer / never wake on
 * movement — TIMER alone is the state+rate signal: T1<=5 => TRACKING (GPS on, LOC every T1 s); T1>5 =>
 * IDLE (GPS OFF, no LOC, a heartbeat every T2 s). Default at boot = IDLE. */
#define TRACK_T1_MAX 5

/* item 12 follow-up: non-blocking reconnect. A pending connect() must never stall the main loop (it
 * would starve 1 Hz blind-buffer capture during an outage), so connects start non-blocking and are
 * polled with a {0,0} select; these pace the attempts. */
#define CONN_RETRY_SEC   5     /* wait between connect attempts */
#define CONN_TIMEOUT_SEC 30    /* abandon a pending connect (covers lwIP SYN retransmits) */

/* GPS power follows the state. Tracking brings GNSS up with its default 1 Hz NMEA URC — the LOC send is
 * gated to T1 s in the loop; idle forces GNSS power OFF. The resident power API is a setter, not a getter,
 * so g_gps_power_state records only states confirmed by a successful API return. A failed setter leaves the
 * previous confirmed state intact (or UNKNOWN at boot), rather than claiming the requested state happened.
 *
 * GNSS_FREQ is intentionally not called: live probing proved gnss_set_freq(1) returns -1 on this resident,
 * while the receiver already defaults to the required 1 Hz. */
#define GPS_RETRY_SEC 10
#define GPS_POWER_UNKNOWN 0
#define GPS_POWER_OFF     1
#define GPS_POWER_ON      2
static volatile unsigned char g_gps_power_state = GPS_POWER_UNKNOWN;
/* Whether the MOST RECENT power-off request was acknowledged. g_gps_power_state is not enough to drive the
 * idle retry: a failed power-ON leaves the last confirmed state at OFF, so (boot OFF ok -> ON fails -> back
 * to idle -> OFF fails) would look "already off" and never retry, which is the case this flag exists for.
 * Any power-on request invalidates it, so the retry runs until the resident acknowledges THIS off. */
static volatile unsigned char g_gps_off_confirmed;
/* What we last ASKED for, as distinct from what the resident confirmed. A -1 return tells us the call
 * was accepted but not whether the receiver moved, so the request and the confirmed state can differ
 * until evidence settles it. */
static volatile unsigned char g_gps_req = GPS_POWER_UNKNOWN;
#define GPS_OFF_QUIET_SEC 30            /* NMEA silence after an off request that we accept as proof */
/* Why a GPS power call failed, not just that it did. Both diagnostics the firmware already had —
 * "GPS power API missing" and the errno-ish return — print only to VCOM, which needs USB; a unit in
 * the field could report pwr=UNKNOWN forever with no way to tell a null slot pointer from a resident
 * that keeps returning an error. */
static volatile int g_gps_pwr_rc;               /* last return from the GNSS power setter */
static volatile int g_gps_cfg_rc;               /* last return from the NMEA-config call */
static volatile unsigned g_gps_pwr_fails;       /* power calls that genuinely failed */
static volatile unsigned g_gps_pwr_nochange;    /* ... vs "already in that state", which is success */
/* What the resident returns when the receiver is already in the requested state. Counted separately
 * so that if this assumption is ever wrong on some unit it shows up as a huge nochange count rather
 * than hiding as silent success. */
#define GNSS_PWR_NOCHANGE (-1)
static volatile unsigned char g_gps_api_ok = 1; /* 0 = the resident slot pointer was NULL */

static int apply_gps(int track)
{
    gnss1_fn pw = *(gnss1_fn *)SLOT_GNSS_POWER;
    if (!pw) {
        g_gps_api_ok = 0;
        g_gps_pwr_fails++;
        vputs("ST: GPS power API missing\r\n");
        return 0;
    }
    g_gps_api_ok = 1;

    g_gps_off_confirmed = 0;                       /* unconfirmed until THIS call returns 0 for an off */
    g_wd_phase = 'G';
    int pwr_ret = pw(track ? 1 : 0);
    g_wd_phase = 0;
    g_gps_pwr_rc = pwr_ret;
    /* The resident returns 0 on an actual change and GNSS_PWR_NOCHANGE when the receiver is ALREADY in
     * the requested state — measured on G306161 over USB (2026-08-22): off->on and on->off both return
     * 0, repeating either returns -1. That is NOT a failure, and treating it as one did real damage:
     * an idle unit never confirmed OFF (GPSSTAT reported pwr=UNKNOWN forever), and the 10-second
     * tracking reassert bailed here on every pass, so the NMEA re-assertion below — the entire reason
     * the reassert exists — had never executed even once. */
    /* -1 is AMBIGUOUS, not a confirmation. Measurement showed "already in that state" is ONE cause of
     * it, but not that it is the only one: a busy/uninitialised GNSS service, a driver error or an
     * unresponsive receiver could plausibly return it too, and promoting the state on that basis would
     * make us report GPS:ON over a dead receiver — a confident lie, worse than the noisy truth it
     * replaced. So on -1 we do NOT touch the confirmed state; we only carry on to the NMEA re-assert
     * below, which is the part that was actually broken. The confirmed state is instead driven by
     * EVIDENCE: fresh NMEA proves ON, and a request for OFF followed by the stream going quiet proves
     * OFF (see gps_state_from_evidence). */
    if (pwr_ret == GNSS_PWR_NOCHANGE) {
        g_gps_pwr_nochange++;
        g_gps_req = track ? GPS_POWER_ON : GPS_POWER_OFF;
        if (track) goto assert_nmea;             /* skip the state promotion, keep the cfg re-assert */
        return 1;
    }
    if (pwr_ret != 0) {
        g_gps_pwr_fails++;
        vhex(track ? "ST:gps_on_err" : "ST:gps_off_err", (unsigned)pwr_ret);
        vputs(track ? "ST: GPS power on FAILED\r\n" : "ST: GPS power off FAILED\r\n");
        return 0;
    }
    /* rc == 0 means the resident actually performed the transition — that IS authoritative. */
    g_gps_power_state = track ? GPS_POWER_ON : GPS_POWER_OFF;
    g_gps_req = g_gps_power_state;
    if (!track) g_gps_off_confirmed = 1;

assert_nmea:
    if (track) {
        gnss2_fn cfg = *(gnss2_fn *)SLOT_GNSS_CFG;
        g_wd_phase = 'G';
        int cfg_ret = cfg ? cfg(1, 1) : -1;
        g_wd_phase = 0;
        g_gps_cfg_rc = cfg_ret;
        if (cfg_ret != 0) {
            vhex("ST:gps_cfg_err", (unsigned)cfg_ret);
            vputs("ST: GPS NMEA config FAILED\r\n");
            return 0;
        }
        vputs("ST: GPS on (tracking)\r\n");
    } else {
        vputs("ST: GPS off (idle)\r\n");
    }
    return 1;
}

/* Signal (item 3): the vendor app never sends AT — it reads rssi via the resident getter (SLOT_GET_CSQ,
 * its 0x7e1ca468 poller; RE 07-23). We do the same: a direct call keeps AT text OFF the URC queue, where
 * an AT reply interleaved into a split NMEA sentence was the corrupt-LOC source. The AT executor below
 * remains for the recovery ladder (CFUN/COPS) only. sig_map maps rssi 0..31 -> GT06 level 0..4. */
#define AT_SEND_SLOT  SL(0x7E1E2268u)
typedef int (*at_send_fn)(const char *cmd, int len);
static void at_cmd(const char *s)
{
    at_send_fn f = *(at_send_fn *)AT_SEND_SLOT;
    int n = 0; while (s[n]) n++;
    if (f) { g_wd_phase = 'A'; f(s, n); g_wd_phase = 0; }
}
static volatile unsigned char g_csq = 99;    /* last rssi (99 = unknown) */
static void csq_refresh(void)
{
    get_csq_fn f = *(get_csq_fn *)SLOT_GET_CSQ;
    unsigned char v;
    if (f && f(&v) == 0 && v <= 33) g_csq = v;
}
static unsigned char sig_map(unsigned s)
{
    if (s > 31)    return 0;                  /* 99 (unknown) -> 0 */
    if (s <= 2)    return 0;
    if (s <= 7)    return 1;
    if (s <= 0x0e) return 2;
    if (s <= 0x13) return 3;
    return 4;
}
/* ---- item 16: cellular auto-recovery (carrier-denial wedge) ----
 * Seen live 2026-07-23 (G375349): after a reboot the modem camped on an Optus cell that rejected the
 * roaming SIM (EMM cause 15 "No Suitable Cells In Location Area") and sat emergency-only FOREVER —
 * app healthy, every connect failing, USB needed to fix it. The AT executor can do the whole recovery
 * in-firmware. Ladder (only while offline AND not registered, each rung given time to settle):
 *   rung 1: CFUN 0/1 (clears cached rejects)
 *   rung 2: COPS=2 then COPS=0 (force reselection)
 *   rung 3: COPS=? scan -> manual-select an available PLMN != the camped/denied one (this is the step
 *           that fixed the live incident), then restore COPS=0 once the server link has proven out
 *           (manual selection would strand a unit that travels). Holdoff-limited so a genuinely dead
 *           network can't drive an attach storm (Simbase penalty lesson from night-idle). */
static volatile signed char g_cereg_stat = -1;  /* last +CEREG stat; 1/5 = registered (-1 unknown) */
static char g_plmn_cur[8];                      /* camped PLMN ("50502") from the +COPS? reply */
static char g_plmn_alt[8];                      /* alternate PLMN picked from the COPS=? scan */
static volatile unsigned char g_scan_pend;      /* COPS=? issued: accumulate URC chunks + parse */
static char g_scan_acc[480]; static int g_scan_len;
static volatile unsigned char g_cops_manual;    /* registered via manual COPS -> restore auto later */
static unsigned g_urc_n;                        /* URC messages consumed since boot (payload-lifetime diagnostics) */
static unsigned g_nmea_bad;                     /* NMEA lines dropped on checksum (AT-interleave splices) */
/* GNSS health counters for CXCS#GPSSTAT. During a GPS-wait the batch message is never sent (no fixes),
 * so without these the server cannot tell a dead receiver from one that is simply still acquiring —
 * which is exactly the hole hit by G226122's 2h22m GPS-wait through a flight on 2026-08-21. */
static unsigned g_nmea_lines;                   /* checksum-good NMEA sentences reassembled */
static unsigned g_rmc_seen;                     /* RMC sentences parsed */
static unsigned g_rmc_valid;                    /* ... of which reported a valid fix ('A') */
static unsigned g_nmea_ts;                      /* wd_uptime() of the last good NMEA sentence */

/* Resolve the GNSS power state from evidence rather than from a return code we cannot fully trust.
 *
 * ON  is proved by fresh NMEA: the receiver cannot emit sentences while unpowered.
 * OFF is proved by asking for off and then hearing nothing for GPS_OFF_QUIET_SEC.
 *
 * Anything else leaves the previous confirmed state alone — a request whose outcome we do not know
 * must not masquerade as a confirmation. This is what makes GPSSTAT's pwr trustworthy: it reports what
 * the receiver is observably doing, not what we last asked it to do. */
static void gps_state_from_evidence(unsigned now)
{
    if (g_nmea_ts && (now - g_nmea_ts) <= 3) {
        g_gps_power_state = GPS_POWER_ON;        /* sentences are arriving: it is powered, whatever
                                                  * the last call returned */
        g_gps_off_confirmed = 0;
        return;
    }
    /* Silence only corroborates an off request that the resident did not hard-reject. After a genuine
     * error the retry must keep running — otherwise a receiver that failed to power down would be
     * quietly declared off (it emits nothing while wedged either), which is how the retry added in
     * 693bc40 would have been defeated. -1 is ambiguous but not an error, so it settles here. */
    if (g_gps_req == GPS_POWER_OFF &&
        (g_gps_pwr_rc == 0 || g_gps_pwr_rc == GNSS_PWR_NOCHANGE) &&
        (!g_nmea_ts || (now - g_nmea_ts) >= GPS_OFF_QUIET_SEC)) {
        g_gps_power_state = GPS_POWER_OFF;       /* asked for off, and the stream really did stop */
        g_gps_off_confirmed = 1;
    }
}

/* Release the heap payload carried in word 3 of a resident 16-byte URC envelope. The vendor consumer
 * (0x7e1d5948) calls free() for every successfully received non-NULL msg[3], after all type-specific use.
 * Its free veneer loads _impure_ptr from 0x7e1e1968; the vendor image initializes that word to 0x7e1e1970.
 * Our standalone image does not contain that vendor .data, so use the observed value directly. This
 * firmware's resolved _free_r (slot target 0x7e6f2244) discards the reent argument before freeing ptr. */
static void urc_free(unsigned payload)
{
    if (!payload) return;                         /* the vendor's only payload guard */
    free_r_fn fr = *(free_r_fn *)SLOT_FREE_R;
    if (fr) fr((void *)0x7E1E1970u, (void *)(unsigned long)payload);
}

/* Pick from the accumulated COPS=? scan the first "(1,...)" (available) network whose quoted NUMERIC
 * plmn differs from g_plmn_cur -> g_plmn_alt. Format: (<stat>,"<long>","<short>","<numeric>"[,<act>]) */
/* PLMNs this SIM is actually allowed to use. WITHOUT this filter the ladder happily manual-selects the
 * first *available* network that isn't the one denying us — on 2026-07-24 that was vodafone AU (50503),
 * which this roaming SIM cannot register with, so AT+COPS=1,2,"50503" STRANDED the unit in manual mode
 * on an unusable carrier for 35 min (restore-to-auto only runs once the server link proves out, which
 * can never happen while stranded). Empty string = allow any (legacy). SZCS#PLMNOK=<csv> to change. */
static char g_plmn_ok[32] = "50501,50502";      /* Telstra, Optus */
static int plmn_permitted(const char *p)
{
    if (!g_plmn_ok[0]) return 1;
    for (int i = 0; g_plmn_ok[i]; ) {
        int j = 0;
        while (g_plmn_ok[i + j] && g_plmn_ok[i + j] != ',' && p[j] && g_plmn_ok[i + j] == p[j]) j++;
        if (!p[j] && (g_plmn_ok[i + j] == 0 || g_plmn_ok[i + j] == ',')) return 1;
        while (g_plmn_ok[i] && g_plmn_ok[i] != ',') i++;
        if (g_plmn_ok[i] == ',') i++;
    }
    return 0;
}
static int scan_pick_alt(void)
{
    for (int i = 0; i < g_scan_len && g_scan_acc[i]; i++) {
        if (g_scan_acc[i] != '(' || g_scan_acc[i + 1] != '1' || g_scan_acc[i + 2] != ',') continue;
        char best[8]; int bl = 0;
        for (int j = i; j < g_scan_len && g_scan_acc[j] && g_scan_acc[j] != ')'; j++) {
            if (g_scan_acc[j] != '"') continue;
            char tmp[8]; int dl = 0, k = j + 1;            /* long names ("Telstra Mobile") are skipped
                                                            * to their closing quote, not aborted on */
            while (k < g_scan_len && g_scan_acc[k] && g_scan_acc[k] != '"') {
                if (dl < 7) tmp[dl] = g_scan_acc[k];
                dl++; k++;
            }
            if (k >= g_scan_len || g_scan_acc[k] != '"') break;   /* split mid-group: wait for more */
            int digits = (dl >= 5 && dl <= 6);             /* numeric PLMN = 5-6 digits */
            for (int d = 0; d < dl && digits; d++) if (tmp[d] < '0' || tmp[d] > '9') digits = 0;
            if (digits) { for (int d = 0; d < dl; d++) best[d] = tmp[d]; bl = dl; }
            j = k;
        }
        if (bl) {
            best[bl] = 0;
            int same = 1;
            for (int d = 0; d <= bl; d++) if (best[d] != g_plmn_cur[d]) { same = 0; break; }
            /* must differ from the denying PLMN *and* be one the SIM can actually use */
            if (!same && plmn_permitted(best)) { for (int d = 0; d <= bl; d++) g_plmn_alt[d] = best[d]; return 1; }
        }
    }
    return 0;
}

/* parse a small unsigned int at *pp (skipping leading spaces); returns -1 if no digits */
static int at_num(const char **pp)
{
    const char *p = *pp;
    while (*p == ' ') p++;
    int v = 0, any = 0;
    while (*p >= '0' && *p <= '9' && v < 100000) { v = v * 10 + (*p - '0'); p++; any = 1; }
    *pp = p;
    return any ? v : -1;
}

/* Scan a URC chunk for AT replies we care about: +CSQ (signal), +CEREG (registration state), and
 * +COPS (camped PLMN / network scan). Robust to leading/framing bytes; chunks may split a long
 * COPS=? reply, so while a scan is pending every non-NMEA chunk is accumulated.
 * Returns 1 if the chunk is AT/modem text (leads with '+', OK, ERROR, SMS..., or belongs to a pending
 * COPS=? scan): the caller must NOT feed such chunks to the NMEA line assembler — an AT reply landing
 * between the two halves of a split sentence terminated the partial line and produced a corrupt LOC
 * (lon 001.0 -> 0.0167 deg) about every 20 s, the CSQ poll period. NMEA text never contains '+', so a
 * mid-sentence continuation chunk cannot be misclassified. */
static int parse_at_urc(const char *s)
{
    int at = 0;
    if (g_scan_pend && s[0] != '$') {              /* COPS=? reply may span chunks; NMEA excluded */
        for (int i = 0; s[i] && g_scan_len < (int)sizeof g_scan_acc - 1; i++) g_scan_acc[g_scan_len++] = s[i];
        g_scan_acc[g_scan_len] = 0;
        if (scan_pick_alt()) g_scan_pend = 0;      /* found a usable alternate PLMN */
        at = 1;
    }
    {
        const char *q = s;
        while (*q == '\r' || *q == '\n' || *q == ' ') q++;
        if (*q == '+') at = 1;                     /* any +XXX reply/URC (also +CGEV, +SIMCARD, ...) */
        else if (q[0] == 'O' && q[1] == 'K') at = 1;
        else if (q[0] == 'E' && q[1] == 'R' && q[2] == 'R') at = 1;
        else if (q[0] == 'S' && q[1] == 'M' && q[2] == 'S') at = 1;   /* "SMS DONE" boot URC */
    }
    for (int i = 0; i < 200 && s[i]; i++) {
        if (s[i]=='C' && s[i+1]=='S' && s[i+2]=='Q') {
            const char *p = s + i + 3;
            while (*p && (*p < '0' || *p > '9')) p++;    /* skip ": " to first digit */
            int v = 0, any = 0;
            while (*p >= '0' && *p <= '9') { v = v*10 + (*p-'0'); p++; any = 1; }
            if (any) g_csq = (unsigned char)v;
            return 1;
        }
        if (s[i]=='C' && s[i+1]=='E' && s[i+2]=='R' && s[i+3]=='E' && s[i+4]=='G') {
            /* solicited "+CEREG: <mode>,<stat>[,...]" has TWO leading numbers; unsolicited
             * "+CEREG: <stat>[,"tac"...]" has ONE (the next field is quoted). stat = last number. */
            const char *p = s + i + 5;
            while (*p && *p != ':') p++;
            if (*p != ':') return at;
            p++;
            int a = at_num(&p);
            if (a < 0) return at;
            int stat = a;
            while (*p == ' ') p++;
            if (*p == ',') { p++; int b = at_num(&p); if (b >= 0) stat = b; }
            g_cereg_stat = (signed char)stat;
            return 1;
        }
        if (s[i]=='C' && s[i+1]=='O' && s[i+2]=='P' && s[i+3]=='S') {
            const char *p = s + i + 4;
            while (*p && *p != ':') p++;
            if (*p != ':') return at;
            p++;
            while (*p == ' ') p++;
            if (*p == '(') return 1;               /* scan reply: handled by the accumulator above */
            /* "+COPS: <mode>[,<fmt>,"<plmn>"[,<act>]]" -> cache the camped numeric PLMN */
            const char *q = p;
            while (*q && *q != '"' && *q != '\r' && *q != '\n') q++;
            if (*q != '"') return 1;
            q++;
            int n = 0;
            while (q[n] >= '0' && q[n] <= '9' && n < 7) { g_plmn_cur[n] = q[n]; n++; }
            if (n >= 5 && q[n] == '"') g_plmn_cur[n] = 0; else if (n == 0) g_plmn_cur[0] = 0;
            return 1;
        }
    }
    return at;
}
static int read_signal(void)     { return sig_map(g_csq); }
/* Battery (item 4): the M0 supervisor MCU owns the battery ADC and serves it over UART1 in a 55 AA framed
 * packet (RE: re_battery_voltage.md). We query it and cache the millivolts; read_batt_level() maps mV to the
 * GT06 0..6 level using the vendor's own thresholds. AT+CBC/CADC/CVAUXV all ERROR on this A7670SA, and the
 * ADC-ch-0x3c path only fills dead vendor-app globals, so this UART query is the only working source. */
#if BATT_MCU
static volatile unsigned char g_urx[128];      /* supervisor RX bytes, filled async by uart_rx_cb */
static volatile int g_urxlen;
static volatile unsigned short g_batt_mv;       /* last good supervisor mV (0 = none yet) */
static volatile unsigned g_batt_mv_rejects;     /* samples outside the plausibility window (diagnostic) */
/* Upper plausibility bound for the supervisor mV. Charging pushes the measured rail past a resting
 * cell's 4.2 V, so this has to be generous; 5.5 V still rejects framing garbage (a bad 16-bit read is
 * almost always far outside 2.6..5.5 V) while accepting any real charge voltage. */
#define BATT_MV_MAX 5500
/* SOS button (item 17, SAFETY): 0x82 payload byte 1. g_sos_active latches on press; the main loop sends
 * a burst of 0x16 SOS alarms (survives packet loss + a reconnect) and re-arms on each reconnect. */
static volatile unsigned char g_sos_active, g_sos_sends, g_sos_prev;
#define SOS_BURST 10                            /* 0x16 alarms per delivery burst (per trigger / reconnect) */
static volatile unsigned g_hbt_on = 15, g_hbt_off = 15;  /* HBT,X,Y heartbeat interval: X=ACC-ON, Y=ACC-OFF(idle) */
#define HBT_MAX 3600  /* sanity ceiling (s): reject garbage that would silently disable the heartbeat */

/* Resident UART RX callback — the resident UART thread calls this with (uart_ref, pending_len, reserve)
 * (ABI RE'd from vendor 0x7e1d4988). Drain the pending bytes via UartRead(uart_ref,...) into g_urx. */
static void uart_rx_cb(u32 ref, u32 pending, u32 reserve)
{
    (void)reserve;
    uart_read_fn rd = *(uart_read_fn *)SLOT_UART_READ;
    if (!rd) return;
    while (pending) {
        unsigned char tmp[64];
        int want = pending > 64 ? 64 : (int)pending;
        int n = rd(ref, tmp, want);
        if (n <= 0) break;
        for (int i = 0; i < n; i++)
            if (g_urxlen < (int)sizeof g_urx) g_urx[g_urxlen++] = tmp[i];
        pending -= (unsigned)n;
    }
}

/* Configure UART1 (9600 8-N-1) and register the RX callback. Call once at startup. */
static void batt_mcu_init(void)
{
    uart_setcfg_fn cfgf = *(uart_setcfg_fn *)SLOT_UART_SETCFG;
    uart_regcb_fn  regf = *(uart_regcb_fn  *)SLOT_UART_REGCB;
    static unsigned char cfg[8] = {0x80, 0x25, 0, 0, 3, 0, 0, 0};  /* baud=9600 @+0, u16 dataBits=3 @+4, parity=0 @+6 */
    static unsigned char reserve[64];                              /* opaque ctx the resident may use; give it real RAM */
    if (cfgf) cfgf(1, cfg);
    if (regf) regf(1, uart_rx_cb, reserve);
}

/* Send the 7-byte battery query (cmd 0x02, no payload, checksum 0x02). The 0x82 reply arrives async on the
 * next loop cycles; batt_mcu_parse() picks it up. Clears the RX buffer first so we parse only fresh bytes. */
static void batt_mcu_request(void)
{
    uart_write_fn wr = *(uart_write_fn *)SLOT_UART_WRITE;
    static const unsigned char req[7] = {0x55, 0xAA, 0x02, 0x00, 0x02, 0x0D, 0x0A};
    g_urxlen = 0;
    if (wr) { g_wd_phase = 'U'; wr(1, req, 7); g_wd_phase = 0; }
}

/* Scan g_urx for a complete `55 AA CMD LEN PAYLOAD[LEN] SUM 0D 0A` frame. On a checksum-valid 0x82 with
 * LEN>=22, cache mV = payload[19]<<8 | payload[20] (sane 2600..4500). SUM = 8-bit additive over CMD..payload. */
static void batt_mcu_parse(void)
{
    int len = g_urxlen;
    for (int i = 0; i + 7 <= len; i++) {
        if (g_urx[i] != 0x55 || g_urx[i+1] != 0xAA) continue;
        int cmd = g_urx[i+2], plen = g_urx[i+3];
        int total = plen + 7;                       /* 55 AA CMD LEN payload[plen] SUM 0D 0A */
        if (i + total > len) break;                 /* frame not fully received yet */
        if (g_urx[i+total-2] != 0x0D || g_urx[i+total-1] != 0x0A) continue;
        unsigned sum = 0;
        for (int k = i + 2; k < i + 4 + plen; k++) sum += g_urx[k];
        if ((sum & 0xFF) != g_urx[i + 4 + plen]) continue;
        if (cmd == 0x82 && plen >= 22) {
            const volatile unsigned char *pay = &g_urx[i + 4];
            unsigned mv = (pay[19] << 8) | pay[20];
            /* Plausibility window, to reject a mis-framed reply rather than publish garbage. The upper
             * bound must clear a CHARGING unit: the M0 measures the charge rail, not a resting cell, so
             * it sits well above 4.2 V on charge. A 4500 ceiling silently rejected EVERY sample on a
             * unit left charging overnight (G378517, 2026-08-21) — it reported BT:4486 on vendor
             * firmware and BT:0 on ours the moment it was updated, because the true value had climbed
             * past the limit. Rejecting a real reading is worse than passing a slightly odd one: the
             * device then looks flat rather than full. */
            if (mv >= 2600 && mv <= BATT_MV_MAX) g_batt_mv = (unsigned short)mv;
            else g_batt_mv_rejects++;
            /* (pay[11..14] is a BE u32 uptime-minutes counter — the earlier "byte 13 = charge flag"
             * read was uptime bits coinciding with a bench power-cycle. Charging now comes from the
             * resident charger_state() sAPI instead; the M0 has no charger/VBUS state at all.) */
            /* SOS button (item 17, SAFETY): M0 status byte 1 = 1 while the SOS button is held (RE'd on
             * the bench 2026-07-23 — pay[1] tracked two 20 s holds, 0 at rest; pay[3] was an unrelated
             * edge pulse). A rising edge latches g_sos_active and arms a resend burst; the main loop then
             * sends 0x16 SOS alarms. Latched — stays set through release/reconnect until CANCELSOS# or a
             * reboot, so a real alarm can't be lost. */
            if ((pay[1] & 0x01) && !g_sos_prev) {
                g_sos_active = 1; g_sos_sends = 0;
                vputs("SOS: button pressed\r\n");
            }
            g_sos_prev = (unsigned char)(pay[1] & 0x01);
            /* M0:pay RE dump (item 17 SOS button — the M0 reads P3.0/3.1/3.5 = SOS/PK/GG and should flip
             * another status byte on a press). Fire only on a STATE-byte change: mask the RTC (5..10),
             * the free-running counter (14) and mV (19..20), which change every cycle and would bury it. */
            static unsigned char prev[32]; static int seen;
            int pn = plen > 32 ? 32 : plen, diff = !seen;
            for (int k = 0; k < pn; k++) {
                if ((k >= 5 && k <= 10) || k == 14 || k == 19 || k == 20) continue;
                if (prev[k] != pay[k]) diff = 1;
            }
            for (int k = 0; k < pn; k++) prev[k] = pay[k];
            if (diff) { seen = 1; vhexbuf("M0:pay", (const unsigned char *)pay, pn); }
        }
    }
}
#endif /* BATT_MCU */

static int read_batt_level(void)
{
#if BATT_MCU
    unsigned mv = g_batt_mv;
    if (mv) {                                       /* vendor mV->level thresholds (RE: BATT_LEVEL 0x7e1be545) */
        if (mv >= 4000) return 6;
        if (mv >  3849) return 5;
        if (mv >  3699) return 4;
        if (mv >  3549) return 3;
        if (mv >  3399) return 2;
        return 1;
    }
#endif
    return 6;                                        /* no reading yet: report full */
}

/* GT06 heartbeat/status (0x13): [terminal-info(bit2=charging)][battery level][gsm signal][alarm/lang 2B].
 * g_charging (polled from the resident charger_state() sAPI in the main loop) drives bit2 — the operator's
 * "is it charging" indicator; the server gt06_parse_heartbeat reads 0x04. Item 15. */
static volatile unsigned char g_charging;       /* charger/VBUS present (resident charger_state()); item 15 */
static int build_heartbeat(unsigned char *d)
{
    d[0] = (unsigned char)(0x40 | (g_charging ? 0x04 : 0));   /* terminal info (bit2 = charging) */
    d[1] = (unsigned char)read_batt_level();
    d[2] = (unsigned char)read_signal();
    d[3] = 0x00; d[4] = 0x01;                     /* alarm / language */
    return 5;
}

/* Last valid GPS fix, cached for the SOS alarm's LOC block so a press reports a real position even in idle
 * (GPS off) or before the next fix. All zero until the first fix (=> server uses last-known / receipt time). */
static u32 g_fix_lat, g_fix_lon;
static unsigned char g_fix_flags, g_fix_sats, g_fix_dt[6], g_have_fix;
static unsigned g_fix_ts;                        /* wd_uptime() of the last valid fix (GPS-lock freshness) */

/* ---- item 14: status LEDs. Two app LEDs via the resident GPIO sAPI (RE re_leds.md): gpio ids 7 & 0x78,
 * gpio_set_level(id,1|0). Behaviour (user spec 2026-07-23):
 *   idle                                    -> both off
 *   tracking + GPS lock + radio(data) OK    -> both solid on
 *   tracking but failing (stale/no fix OR not connected) -> BLUE solid, YELLOW flashing
 *   SOS active                              -> both flashing
 * Blink = wd_uptime() parity (~0.5 Hz on/off), independent of loop rate. Colour<->gpio-id confirmed on
 * the bench 2026-07-23: id 7 = BLUE, id 0x78 = YELLOW. SZCS#LEDSWAP=1 still flips it live if ever needed. */
#define LED_ID_A 7u                              /* BLUE  (bench-confirmed) */
#define LED_ID_B 0x78u                           /* YELLOW (bench-confirmed) */
#define LED_FIX_STALE_SEC 6                      /* a valid fix older than this = GPS lock considered lost */
static volatile unsigned char g_led_swap;        /* 0: A=blue,B=yellow ; 1: swapped (SZCS#LEDSWAP) */

static void leds_init(void)                      /* call once after the resolver table is filled */
{
    gpio_cfg_fn cfg = *(gpio_cfg_fn *)SLOT_GPIO_CONFIG;
    gpio_set_fn set = *(gpio_set_fn *)SLOT_GPIO_SET;
    if (cfg) { cfg(LED_ID_A, 1); cfg(LED_ID_B, 1); }   /* configure both pins as outputs */
    if (set) { set(LED_ID_A, 0); set(LED_ID_B, 0); }   /* start off */
}

static void led_write(unsigned id, int on)       /* only touch the resident GPIO on an actual change */
{
    static int last_a = -1, last_b = -1;
    int *last = (id == LED_ID_A) ? &last_a : &last_b;
    if (*last == on) return;
    gpio_set_fn set = *(gpio_set_fn *)SLOT_GPIO_SET;
    if (set) set(id, on ? 1u : 0u);
    *last = on;
}

static void leds_update(int track, int connected, int sos, unsigned now)
{
    int blink  = (int)(now & 1u);                /* ~0.5 Hz: 1 s on, 1 s off */
    int gps_ok = g_have_fix && (now - g_fix_ts) <= LED_FIX_STALE_SEC;
    int blue, yellow;                            /* blue = "trying to track", yellow = health */
    if (sos)         { blue = blink; yellow = blink; }
    else if (!track) { blue = 0;     yellow = 0; }
    else             { blue = 1;     yellow = (connected && gps_ok) ? 1 : blink; }
    led_write(g_led_swap ? LED_ID_B : LED_ID_A, blue);
    led_write(g_led_swap ? LED_ID_A : LED_ID_B, yellow);
}

/* Build the 0x16 SOS alarm data block (item 17): 18-byte LOC + LBS-len(0) + terminal-info + battery + gsm
 * + alarm/lang. The server (gt06_parse_alarm_status) reads the alarm type from terminal-info bits 3..5,
 * so SOS = 4 there (0x20); it also reads bit2 = charging, and the battery LEVEL + signal after it. */
static int build_alarm_data(unsigned char *d)
{
    if (g_have_fix)
        build_loc_data(d, g_fix_dt, g_fix_lat, g_fix_lon,
                       g_fix_flags & 1, g_fix_flags & 2, (g_fix_flags & 4) ? 1 : 0, g_fix_sats, 0, 0);
    else
        for (int i = 0; i < 18; i++) d[i] = 0;    /* no fix yet: 0 => server uses last-known / receipt time */
    int n = 18;
    d[n++] = 0x00;                                 /* LBS length = 0 (no cell block) */
    d[n++] = (unsigned char)(0x40 | (4 << 3) | (g_charging ? 0x04 : 0));  /* term-info: alarm bits3..5 = SOS(4) */
    d[n++] = (unsigned char)read_batt_level();     /* battery voltage level */
    d[n++] = (unsigned char)read_signal();         /* gsm signal */
    d[n++] = 0x04;                                 /* alarm byte = SOS (standard tail; server uses term-info) */
    d[n++] = 0x01;                                 /* language */
    return n;
}

/* 0x15 command response ("<cmd> OK") so the server reconcile advances promptly. Layout mirrors the 0x80
 * command: data = [content_len][server_flag 4B=0][ascii]; the server reads the ascii at data[5:]. */
static int build_cmd_resp(const char *ascii, int alen, int ok, unsigned short serial, unsigned char *out)
{
    unsigned char data[80];
    if (alen > 60) alen = 60;
    data[1] = data[2] = data[3] = data[4] = 0;    /* server flag */
    for (int i = 0; i < alen; i++) data[5 + i] = (unsigned char)ascii[i];
    const char *suf = ok ? " OK" : " ERROR";       /* recognized command -> OK, unknown -> ERROR */
    int sl = 0; while (suf[sl]) sl++;
    for (int i = 0; i < sl; i++) data[5 + alen + i] = (unsigned char)suf[i];
    int clen = 4 + alen + sl;                      /* content_len = flag(4) + ascii(alen) + suffix */
    data[0] = (unsigned char)clen;
    return gt06_frame(0x15, data, 5 + alen + sl, serial, out);
}

static int prefix_eq(const char *a, const char *b);   /* defined below with the config-command helpers */

/* Is this a GT06 command we recognise (act on, or intentionally accept as a no-op the server sends
 * during reconcile)? Recognised -> "<cmd> OK"; anything else -> "<cmd> ERROR" so an unknown/garbage
 * command isn't silently claimed as applied. cxzt/STATUS/SZCS#SERVIP/APN/BLIND_EN have their own replies
 * upstream and never reach here; SLPDISCONNECT/GPS_RST_TIME/VIBCHK/... arrive under SZCS#/CXCS#. */
static int cmd_recognized(const char *cmd)
{
    static const char *const known[] = {
        "SZCS#", "CXCS#", "TIMER", "HBT", "SENDS", "SENALM", "MOVING", "CANCELSOS", "YCSJ", "RESET",
    };
    for (unsigned i = 0; i < sizeof known / sizeof known[0]; i++)
        if (prefix_eq(cmd, known[i])) return 1;
    return 0;
}

/* parse a decimal int from a NUL-terminated string, advancing *pp; -1 if no digits. The value is capped
 * (intervals are small) so a long/garbage digit run can't overflow. */
static int parse_int(const char **pp)
{
    const char *s = *pp; int v = 0, any = 0;
    while (*s >= '0' && *s <= '9') { if (v < 100000) v = v * 10 + (*s - '0'); s++; any = 1; }
    *pp = s; return any ? v : -1;
}

/* --- query-reply builders (STATUS# / cxzt#): report battery at mV resolution + device info. The server
 * regex-scans the 0x15 reply text for Battery:(\d+\.\d+)V and *BT:<mV>. Freestanding: format by hand. --- */
static char g_login_id_s[16];                   /* IMEI/TERIID for cxzt *ID (set in standalone_main) */
static unsigned char g_srv_ip_s[4];
static unsigned g_port_s;
static char g_apn_s[48];

static int put_str(char *b, const char *s) { int n = 0; while (s[n]) { b[n] = s[n]; n++; } return n; }
/* bounded copy: at most maxn chars, stops at NUL (for possibly-unterminated flash fields) */
static int put_strn(char *b, const char *s, int maxn) { int n = 0; while (n < maxn && s[n]) { b[n] = s[n]; n++; } return n; }
static int put_uint(char *b, unsigned v)
{
    char t[10]; int n = 0;
    if (!v) { b[0] = '0'; return 1; }
    while (v) { t[n++] = (char)('0' + v % 10); v /= 10; }
    for (int i = 0; i < n; i++) b[i] = t[n - 1 - i];
    return n;
}
/* Frame raw ASCII as a 0x15 command response (no "<cmd> OK" wrapper): [content_len][flag 4B=0][ascii]. */
static int build_text_resp(const char *ascii, int alen, unsigned short serial, unsigned char *out)
{
    unsigned char data[208];
    if (alen > 200) alen = 200;   /* frame len byte = 10+alen, so <=245 is safe; cxzt+*M0 needs ~190 */
    data[1] = data[2] = data[3] = data[4] = 0;
    for (int i = 0; i < alen; i++) data[5 + i] = (unsigned char)ascii[i];
    data[0] = (unsigned char)(4 + alen);
    return gt06_frame(0x15, data, 5 + alen, serial, out);
}
/* STATUS# reply: battery at mV resolution (Battery:X.XXXV) + GPRS/signal for vendor-format parity.
 * GPS is the last power state confirmed by a successful resident GNSS API return, not a lock indicator. */
static int status_str(char *b)
{
    unsigned mv = g_batt_mv, f = mv % 1000;
    int n = put_str(b, "Battery:");
    n += put_uint(b + n, mv / 1000); b[n++] = '.';
    b[n++] = (char)('0' + f / 100); b[n++] = (char)('0' + (f / 10) % 10); b[n++] = (char)('0' + f % 10);
    n += put_str(b + n, "V;GPRS:Online;GSM Signal Level:");
    n += put_uint(b + n, g_csq <= 31 ? g_csq : 0);
    n += put_str(b + n, ";ACC:ON;GPS:");
    if (g_gps_power_state == GPS_POWER_ON) n += put_str(b + n, "ON");
    else if (g_gps_power_state == GPS_POWER_OFF) n += put_str(b + n, "OFF");
    else n += put_str(b + n, "UNKNOWN");
    n += put_str(b + n, ";Defense:OFF;");
    return n;
}
/* cxzt# reply: the device-info line the server parses — *ID, server, *A(pn), *F(TIMER), *H(bt), *BT(mV). */
static int cxzt_str(char *b, int t1, int t2)
{
    int n = put_str(b, FW_VER "_" GIT_HASH "-GT06 MCU:R5*ID:");
    n += put_str(b + n, g_login_id_s); b[n++] = '*';
    for (int i = 0; i < 4; i++) { n += put_uint(b + n, g_srv_ip_s[i]); if (i < 3) b[n++] = '.'; }
    b[n++] = ':'; n += put_uint(b + n, g_port_s);
    n += put_str(b + n, "*A:"); n += put_str(b + n, g_apn_s);
    n += put_str(b + n, "*G:A*4G:14*M:1|2|0*F:");
    n += put_uint(b + n, (unsigned)t1); b[n++] = '|'; n += put_uint(b + n, (unsigned)t2);
    n += put_str(b + n, "*H:15*SP:1*SF:0*V:0*BT:");
    n += put_uint(b + n, g_batt_mv);
    n += put_str(b + n, "*LBS:1*R:14|1|1*CHG:");
    b[n++] = (char)('0' + (g_charging ? 1 : 0));
    /* Forwarding-bridge up-path counters (only when configured, to keep the normal reply short).
     * a=active rx=<cb calls>/<bytes> dp=ring drops tx=<bytes sent> tf=send fails cn=connects ps=poll passes */
    /* NOTE: do NOT append forwarding counters here. This reply is already ~200 chars and the caller's
     * buffer is 208, so appending unbounded counters overflowed it (build_text_resp only truncates
     * afterwards — too late). The counters live in CXCS#FWDSTAT instead, which has room to spare. */
    return n;
}

/* RESET# reboot request (set by handle_rx, run from the main loop so the ACK flushes first). item 11. */
static volatile int g_reset_req;

#if FOTA
/* --- FOTA request (set by handle_rx on YCSJ, run from the main loop) --- */
static volatile int g_fota_req;
static unsigned char g_fota_ip[4];
static unsigned g_fota_port;
static char g_fota_path[128];

/* reflected CRC-32 (poly 0xEDB88320, init 0xFFFFFFFF, NO final XOR) — matches the resident's stored
 * ~zlib.crc32(payload) (re_flash_write_api.md). Bitwise, table-free, to save flash. */
static unsigned crc32_upd(unsigned crc, const unsigned char *p, int n)
{
    for (int i = 0; i < n; i++) {
        crc ^= p[i];
        for (int b = 0; b < 8; b++) crc = (crc >> 1) ^ (0xEDB88320u & (unsigned)-(int)(crc & 1u));
    }
    return crc;
}

/* Parse "http://<a.b.c.d>[:<port>]/<path>,..." (the arg after "YCSJ,") into g_fota_ip/port/path.
 * IP-only host (our socket layer connects by IP, no DNS). Returns 1 on success. */
static int fota_parse(const char *cmd)
{
    static const char pfx[] = "http://";
    const char *p = cmd;
    for (int i = 0; i < 7; i++) if (p[i] != pfx[i]) return 0;
    p += 7;
    unsigned ip[4], port = 80;
    for (int k = 0; k < 4; k++) {
        int v = parse_int(&p);
        if (v < 0 || v > 255) return 0;
        ip[k] = (unsigned)v;
        if (k < 3) { if (*p != '.') return 0; p++; }
    }
    if (*p == ':') { p++; int v = parse_int(&p); if (v <= 0 || v > 65535) return 0; port = (unsigned)v; }
    if (*p != '/') return 0;
    int pl = 0;
    while (*p && *p != ',' && *p != '#' && pl < (int)sizeof g_fota_path - 1) g_fota_path[pl++] = *p++;
    g_fota_path[pl] = 0;
    if (pl == 0) return 0;                          /* need a non-empty path */
    /* Format is <url>,<port># — require the trailing field (its number is the port; also rejects a
     * command truncated before the comma, which would otherwise queue a wrong/short filename). */
    if (*p != ',') return 0;
    p++;
    { int v = parse_int(&p); if (v > 0 && v <= 65535) port = (unsigned)v; }
    for (int k = 0; k < 4; k++) g_fota_ip[k] = (unsigned char)ip[k];
    g_fota_port = port;
    return 1;
}
#endif /* FOTA */

/* --- server/APN config commands (SZCS#SERVIP/SERVPORT/APN set, CXCS# query). Persist to the vendor
 * config file c:/cwprm.bin (+ backup cwbank.bin) at the vendor offsets, so a change survives reboot and
 * stays compatible with the vendor fw: IP@0x034 (ASCII), PORT@0x05c (BE u16), APN@0x210 (ASCII). --- */
static volatile int g_srv_req;                  /* SERVIP/SERVPORT set -> main loop reconnects to new server */
static unsigned char g_srv_new_ip[4];
static unsigned g_srv_new_port;

static int cwprm_read_raw(unsigned char *buf)   /* read the active c:/cwprm.bin (exactly 0x800), no field checks */
{
    fopen_fn fo = *(fopen_fn *)SLOT_FOPEN; fread_fn fr = *(fread_fn *)SLOT_FREAD; fclose_fn fc = *(fclose_fn *)SLOT_FCLOSE;
    static const char nm[] = "c:/cwprm.bin", rb[] = "rb";
    for (int i = 0; i < 0x800; i++) buf[i] = 0;
    unsigned char pp = g_wd_phase; g_wd_phase = 'F';
    FILE_t fp = fo(nm, rb); if (!fp) { g_wd_phase = pp; return 0; }
    unsigned got = fr(buf, 0x800, 1, fp); fc(fp); g_wd_phase = pp;   /* this FS's fread returns BYTES */
    return got == 0x800;                            /* a short read must NOT be modify-written back over the file */
}
static int cwprm_read(unsigned char *buf)       /* read + validate the active c:/cwprm.bin (exactly 0x800) */
{
    if (!cwprm_read_raw(buf)) return 0;
    for (int i = 0; i < 15; i++)                    /* validate the full 15-digit TERIID (not just byte 0) */
        if (buf[CFG_OFF_TERIID + i] < '0' || buf[CFG_OFF_TERIID + i] > '9') return 0;
    return 1;
}
static int cwprm_write_one(const char *nm, const unsigned char *buf)
{
    fopen_fn fo = *(fopen_fn *)SLOT_FOPEN; fwrite_fn fw = *(fwrite_fn *)SLOT_FWRITE; fclose_fn fc = *(fclose_fn *)SLOT_FCLOSE;
    static const char wb[] = "wb";
    unsigned char pp = g_wd_phase; g_wd_phase = 'F';
    FILE_t fp = fo(nm, wb); if (!fp) { g_wd_phase = pp; return 0; }
    unsigned wrote = fw(buf, 0x800, 1, fp); fc(fp); g_wd_phase = pp;
    return (wrote == 0x800);                         /* require the FULL 0x800 write (no truncated config) */
}
static int cwprm_write(const unsigned char *buf)
{
    static const char prm[] = "c:/cwprm.bin", bank[] = "c:/cwbank.bin";
    int a = cwprm_write_one(prm, buf);              /* boot prefers any openable primary -> it MUST succeed */
    cwprm_write_one(bank, buf);                     /* backup best-effort */
    return a;
}
static void cfg_set_ascii(unsigned char *buf, int off, int maxlen, const char *s)  /* NUL-padded ASCII field */
{
    int i = 0;
    for (; i < maxlen - 1 && s[i]; i++) buf[off + i] = (unsigned char)s[i];
    for (; i < maxlen; i++) buf[off + i] = 0;
}
/* ---- USB<->TCP forwarding bridge config (FWDSERVER/FWDPORT): repurpose the tracker as a UAV LTE modem
 * link. Persisted in a DEDICATED resident-FS file c:/wstrfwd.bin (brick-safe: never touches vendor
 * cwprm.bin). 32-byte record: "WF1" magic, ip[4], port BE u16. port 0 = disabled. ---- */
static unsigned char g_fwd_ip[4];
static unsigned g_fwd_port;                         /* 0 = feature disabled */
static volatile unsigned char g_fwd_udp;            /* SZCS#FWDUDP: proxy uplink is UDP not TCP (persisted).
                                                     * Connected UDP bypasses the resident's 2nd-TCP-PCB
                                                     * send defect and removes reconnect churn (RE'd). */
/* g_fwd_en is defined up top (shared with reliability.c): forwarding configured -> if04 owned by the bridge */
static volatile unsigned char g_fwd_req;            /* config changed -> loop drops + reconnects/tears down */
#define FWD_FILE  "c:/wstrfwd.bin"
#define FWD_FILE_BAK "c:/wstrfwd.bak"
static int fwd_load(void)                            /* read c:/wstrfwd.bin into g_fwd_ip/port; 1 if valid */
{
    fopen_fn fo = *(fopen_fn *)SLOT_FOPEN; fread_fn fr = *(fread_fn *)SLOT_FREAD; fclose_fn fc = *(fclose_fn *)SLOT_FCLOSE;
    static const char nm[] = FWD_FILE, bak[] = FWD_FILE_BAK, rb[] = "rb";
    unsigned char b[32];
    unsigned char pp = g_wd_phase; g_wd_phase = 'F';
    FILE_t fp = fo(nm, rb);
    unsigned got = 0;
    if (fp) { got = fr(b, 32, 1, fp); fc(fp); }                 /* this FS's fread returns BYTES */
    if (got < 12 || b[0] != 'W' || b[1] != 'F') {               /* primary missing/short -> try the backup */
        fp = fo(bak, rb);
        if (fp) { got = fr(b, 32, 1, fp); fc(fp); }
    }
    g_wd_phase = pp;
    if (got < 12 || b[0] != 'W' || b[1] != 'F' || (b[2] != '1' && b[2] != '2')) return 0;
    for (int k = 0; k < 4; k++) g_fwd_ip[k] = b[4 + k];
    g_fwd_port = (unsigned)((b[8] << 8) | b[9]);
    g_fwd_udp = (unsigned char)(b[10] & 1);          /* flags byte; 0 in pre-UDP records => TCP */
    /* WF2 keeps its layout so records written by earlier builds still load, but the recovery settings it
     * carries (wd/arm/fire-count at 12..21) are no longer honoured — both automatic mechanisms are gone. */
    if (b[2] == '2' && got < 22) return 0;   /* truncated WF2: reject rather than silently run with
                                              * default (disarmed) recovery settings */
    return 1;
}
static int fwd_write_one(const char *nm, const unsigned char *b)
{
    fopen_fn fo = *(fopen_fn *)SLOT_FOPEN; fwrite_fn fw = *(fwrite_fn *)SLOT_FWRITE; fclose_fn fc = *(fclose_fn *)SLOT_FCLOSE;
    static const char wb[] = "wb";
    unsigned char pp = g_wd_phase; g_wd_phase = 'F';
    FILE_t fp = fo(nm, wb); if (!fp) { g_wd_phase = pp; return 0; }
    unsigned wrote = fw(b, 32, 1, fp); fc(fp); g_wd_phase = pp;
    return wrote == 32;
}
/* Write the BACKUP first, then the primary. fopen("wb") truncates, so a failure or power loss partway
 * through the primary would otherwise destroy the only copy and come back with forwarding disabled.
 * With this order there is always one intact record on disk, and fwd_load falls back to it. */
static int fwd_store(const unsigned char ip[4], unsigned port, unsigned char flags)
{
    static const char nm[] = FWD_FILE, bak[] = FWD_FILE_BAK;
    unsigned char b[32]; for (int i = 0; i < 32; i++) b[i] = 0;
    b[0] = 'W'; b[1] = 'F'; b[2] = '2';
    for (int k = 0; k < 4; k++) b[4 + k] = ip[k];
    b[8] = (unsigned char)(port >> 8); b[9] = (unsigned char)port;
    b[10] = flags;                                    /* bit0 = UDP uplink */
    if (!fwd_write_one(bak, b)) return 0;
    return fwd_write_one(nm, b);
}
static int prefix_eq(const char *a, const char *b) { int i = 0; while (b[i]) { if (a[i] != b[i]) return 0; i++; } return 1; }
/* find `field` in cmd, then skip optional spaces + '=' + optional spaces; return the value ptr, or 0 */
static const char *skip_to_val(const char *cmd, const char *field)
{
    int fl = 0; while (field[fl]) fl++;
    for (const char *p = cmd; *p; p++) {
        int k = 0; while (field[k] && p[k] == field[k]) k++;
        if (k == fl) {
            const char *q = p + fl; while (*q == ' ') q++;
            if (*q != '=') continue;
            q++; while (*q == ' ') q++; return q;
        }
    }
    return 0;
}
/* Handle a SZCS#/CXCS# server/APN config command: build the reply into `out`, return its length, or 0 if
 * `cmd` isn't one of these. Sets persist to cwprm.bin (+ backup) at the vendor offsets; SERVIP/SERVPORT
 * also request a live reconnect via g_srv_req. */
static int handle_config_cmd(const char *cmd, char *out)
{
    static unsigned char cfg[0x800];
    if (prefix_eq(cmd, "CXCS#SERVIP")) {                     /* query server */
        if (!cwprm_read(cfg)) return put_str(out, "SERVIP =?#SERVPORT =?");
        int n = put_str(out, "SERVIP =");
        n += put_strn(out + n, (const char *)(cfg + CFG_OFF_IP), 17);      /* bounded: fields may be unterminated */
        n += put_str(out + n, "#SERVPORT =");
        n += put_uint(out + n, (cfg[CFG_OFF_PORT] << 8) | cfg[CFG_OFF_PORT + 1]);
        return n;
    }
    if (prefix_eq(cmd, "CXCS#APN")) {                        /* query APN + credentials */
        if (!cwprm_read(cfg)) return put_str(out, "APN =?");
        int n = put_str(out, "APN =");
        n += put_strn(out + n, (const char *)(cfg + CFG_OFF_APN), 29);
        n += put_str(out + n, "#USERPPP =");
        n += put_strn(out + n, (const char *)(cfg + CFG_OFF_USER), 29);
        n += put_str(out + n, "#PWPPP =");
        n += put_strn(out + n, (const char *)(cfg + CFG_OFF_PW), 29);
        return n;
    }
    if (prefix_eq(cmd, "SZCS#SERVIP")) {                     /* set server IP (+ optional port) */
        const char *pi = skip_to_val(cmd, "SERVIP");
        const char *pp = skip_to_val(cmd, "SERVPORT");
        unsigned char ip[4];
        if (!pi || !parse_ip(pi, ip)) return put_str(out, "SET ERROR: SERVIP");
        unsigned port = 0; int have_port = 0;
        if (pp) {                                            /* validate: 1..65535 and no trailing junk */
            const char *q = pp; int v = parse_int(&q);
            if (v <= 0 || v > 65535 || (*q != '#' && *q != 0)) return put_str(out, "SET ERROR: SERVPORT");
            port = (unsigned)v; have_port = 1;
        }
        if (!cwprm_read(cfg)) return put_str(out, "SET ERROR: cfg");
        char ipbuf[16]; int il = 0;
        for (int k = 0; k < 4; k++) { il += put_uint(ipbuf + il, ip[k]); if (k < 3) ipbuf[il++] = '.'; }
        ipbuf[il] = 0;
        cfg_set_ascii(cfg, CFG_OFF_IP, 18, ipbuf);           /* IP field 0x034..0x045; leaves 0x046 intact */
        if (!have_port) port = (cfg[CFG_OFF_PORT] << 8) | cfg[CFG_OFF_PORT + 1];
        cfg[CFG_OFF_PORT] = (unsigned char)(port >> 8); cfg[CFG_OFF_PORT + 1] = (unsigned char)port;
        if (!cwprm_write(cfg)) return put_str(out, "SET ERROR: write");
        for (int k = 0; k < 4; k++) { g_srv_new_ip[k] = ip[k]; g_srv_ip_s[k] = ip[k]; }
        g_srv_new_port = port; g_port_s = port; g_srv_req = 1;   /* apply live + update cxzt-reported values */
        int n = put_str(out, "SETOK: SERVIP =");
        n += put_str(out + n, ipbuf); n += put_str(out + n, "#SERVPORT =");
        n += put_uint(out + n, port);
        return n;
    }
    if (prefix_eq(cmd, "SZCS#APN")) {                        /* set APN (+ optional USERPPP/PWPPP) */
        const char *pa = skip_to_val(cmd, "APN");
        const char *pu = skip_to_val(cmd, "USERPPP");
        const char *pw = skip_to_val(cmd, "PWPPP");
        if (!pa) return put_str(out, "SET ERROR: APN");
        if (!cwprm_read(cfg)) return put_str(out, "SET ERROR: cfg");
        char fld[30]; int fl;
        fl = 0; while (pa[fl] && pa[fl] != '#' && fl < 29) { fld[fl] = pa[fl]; fl++; } fld[fl] = 0;
        cfg_set_ascii(cfg, CFG_OFF_APN, 30, fld);
        cfg_set_ascii((unsigned char *)g_apn_s, 0, (int)sizeof g_apn_s, fld);  /* update cxzt-reported APN */
        if (pu) { fl = 0; while (pu[fl] && pu[fl] != '#' && fl < 29) { fld[fl] = pu[fl]; fl++; } fld[fl] = 0; cfg_set_ascii(cfg, CFG_OFF_USER, 30, fld); }
        if (pw) { fl = 0; while (pw[fl] && pw[fl] != '#' && fl < 29) { fld[fl] = pw[fl]; fl++; } fld[fl] = 0; cfg_set_ascii(cfg, CFG_OFF_PW, 30, fld); }
        if (!cwprm_write(cfg)) return put_str(out, "SET ERROR: write");
        int n = put_str(out, "SETOK: APN =");
        n += put_strn(out + n, (const char *)(cfg + CFG_OFF_APN), 29);
        return n;
    }
    if (prefix_eq(cmd, "SZCS#TERIID")) {                     /* set TERIID (registration/onboarding) */
        const char *pv = skip_to_val(cmd, "TERIID");
        if (!pv) return put_str(out, "SET ERROR: TERIID");
        char id[16]; int il = 0;                             /* require EXACTLY 15 digits, then a terminator */
        while (pv[il] >= '0' && pv[il] <= '9' && il < 15) { id[il] = pv[il]; il++; }
        if (il != 15 || (pv[15] != 0 && pv[15] != '#' && pv[15] != ' '))
            return put_str(out, "SET ERROR: TERIID");
        id[15] = 0;
        /* RAW read: onboarding a raw-IMEI unit means the current TERIID field is NOT valid digits, so
         * the validating cwprm_read would reject it — we're setting the very field it validates. */
        if (!cwprm_read_raw(cfg)) return put_str(out, "SET ERROR: cfg");
        cfg_set_ascii(cfg, CFG_OFF_TERIID, 16, id);          /* 15 digits + NUL at 0x104 */
        if (!cwprm_write(cfg)) return put_str(out, "SET ERROR: write");
        for (int i = 0; i < 16; i++) g_login_id_s[i] = id[i];  /* reflect in cxzt *ID until RESET# re-logs-in */
        int n = put_str(out, "SETOK: TERIID =");
        n += put_str(out + n, id);
        return n;
    }
    /* Blind-buffer capture policy — the server reconciles this at every login (1 tracking / 0 idle) and
     * scans replies for SETOK:/READOK:<KEY>=<val>, so answer in exactly that shape (RAM-only, no cwprm). */
    if (prefix_eq(cmd, "CXCS#BLIND_EN")) {
        int n = put_str(out, "READOK:BLIND_EN=");
        out[n++] = (char)('0' + (g_blind_en ? 1 : 0));
        return n;
    }
    if (prefix_eq(cmd, "SZCS#BLIND_EN")) {
        const char *pv = skip_to_val(cmd, "BLIND_EN");
        if (!pv || (pv[0] != '0' && pv[0] != '1')) return put_str(out, "SET ERROR: BLIND_EN");
        g_blind_en = (unsigned char)(pv[0] - '0');
        int n = put_str(out, "SETOK:BLIND_EN=");
        out[n++] = (char)('0' + g_blind_en);
        return n;
    }
    if (prefix_eq(cmd, "CXCS#LOCBATCH")) {
        int n = put_str(out, "READOK:LOCBATCH=");
        n += put_uint(out + n, g_loc_batch);
        return n;
    }
    if (prefix_eq(cmd, "SZCS#LOCBATCH")) {           /* 0 = off (legacy per-fix 0x12), 1..LOC_BATCH_MAX */
        const char *pv = skip_to_val(cmd, "LOCBATCH");
        if (!pv || pv[0] < '0' || pv[0] > '9') return put_str(out, "SET ERROR: LOCBATCH");
        const char *q = pv;
        unsigned v = 0;
        while (*q >= '0' && *q <= '9' && v <= 999) { v = v * 10 + (unsigned)(*q - '0'); q++; }
        if (v > LOC_BATCH_MAX) return put_str(out, "SET ERROR: LOCBATCH");
        g_loc_batch = (unsigned char)v;
        if (g_batch_n > 0) batch_spill_to_blind();   /* a rate change must not silently drop fixes */
        g_batch_rephase = 1;                         /* re-phase: don't inherit the old window */
        int n = put_str(out, "SETOK:LOCBATCH=");
        n += put_uint(out + n, g_loc_batch);
        return n;
    }
    if (prefix_eq(cmd, "SZCS#LEDSWAP")) {                    /* swap the blue/yellow gpio-id mapping */
        const char *pv = skip_to_val(cmd, "LEDSWAP");
        if (!pv || (pv[0] != '0' && pv[0] != '1')) return put_str(out, "SET ERROR: LEDSWAP");
        g_led_swap = (unsigned char)(pv[0] - '0');
        int n = put_str(out, "SETOK:LEDSWAP=");
        out[n++] = (char)('0' + g_led_swap);
        return n;
    }
    if (prefix_eq(cmd, "SZCS#VCOM")) {                       /* master switch for USB-VCOM debug output */
        const char *pv = skip_to_val(cmd, "VCOM");
        if (!pv || (pv[0] != '0' && pv[0] != '1')) return put_str(out, "SET ERROR: VCOM");
        g_vcom_en = (unsigned char)(pv[0] - '0');
        g_vcom_hold = 0;                                     /* clear any backoff on explicit set */
        int n = put_str(out, "SETOK:VCOM=");
        out[n++] = (char)('0' + g_vcom_en);
        return n;
    }
    /* USB<->TCP forwarding bridge (UAV modem link). Either field settable alone or together:
     *   SZCS#FWDSERVER=<ip>#FWDPORT=<port>   SZCS#FWDPORT=0 (disable)   SZCS#FWDSERVER=<ip> (keep port).
     * port<=0 disables; port>0 needs a valid IP (new or previously stored). Persists to c:/wstrfwd.bin;
     * g_fwd_req applies it live. Query with CXCS#FWDSERVER (or CXCS#FWDPORT) — both fields returned. */
    if (prefix_eq(cmd, "SZCS#PLMNOK")) {                     /* CSV of PLMNs this SIM may use ("" = any) */
        const char *pv = skip_to_val(cmd, "PLMNOK");
        if (!pv) return put_str(out, "SET ERROR: PLMNOK");
        int i = 0;
        while (pv[i] && pv[i] != '#' && pv[i] != ' ' && i < (int)sizeof g_plmn_ok - 1) {
            if (!((pv[i] >= '0' && pv[i] <= '9') || pv[i] == ',')) return put_str(out, "SET ERROR: PLMNOK");
            g_plmn_ok[i] = pv[i]; i++;
        }
        g_plmn_ok[i] = 0;
        int n = put_str(out, "SETOK:PLMNOK="); n += put_str(out + n, g_plmn_ok);
        return n;
    }
    if (prefix_eq(cmd, "CXCS#CELLSTAT")) {                   /* cell/recovery state (if04 debug is muted
                                                              * while forwarding owns the port) */
        int n = put_str(out, "READOK:CELLSTAT reg");
        n += put_uint(out + n, (unsigned)(g_cereg_stat < 0 ? 99 : g_cereg_stat));
        n += put_str(out + n, " plmn"); n += put_str(out + n, g_plmn_cur[0] ? g_plmn_cur : "-");
        n += put_str(out + n, " man");  out[n++] = (char)('0' + (g_cops_manual ? 1 : 0));
        n += put_str(out + n, " ok");   n += put_str(out + n, g_plmn_ok[0] ? g_plmn_ok : "any");
        return n;
    }
    if (prefix_eq(cmd, "CXCS#SAPIFIND")) {
        /* Symbol-resolver oracle: CXCS#SAPIFIND=<name>
         *
         * Both sides of the sAPI mechanism use PRECOMPUTED hashes — the app resolves 458 compile-time
         * constants — so the name->hash function probably exists only in the vendor's build tooling,
         * not in either binary. But the resident resolver is a lookup we can query: resolver(hash)
         * returns a pointer, or 0 for an unknown hash. So instead of reversing the algorithm we can
         * GUESS it and let the device tell us which guess is right.
         *
         * This is safe: sapi_table_fill() already calls this exact resolver 458 times on every boot.
         * We are only looking symbols up, never calling what comes back.
         *
         * A hit is self-validating when the returned pointer equals one already sitting in a known
         * slot — that confirms the algorithm AND the naming convention in one shot. */
        const char *nm = skip_to_val(cmd, "SAPIFIND");
        char name[48];
        int nl = 0;
        if (nm) while (nm[nl] && nm[nl] != '#' && nl < (int)sizeof name - 1) { name[nl] = nm[nl]; nl++; }
        name[nl] = 0;
        if (!nl) return put_str(out, "SET ERROR: SAPIFIND needs a symbol name");

        /* SL() so this works on the host too: RESOLVER_SLOT is a bare device address, and SITL
         * remaps slot addresses into sitl_slots[]. Dereferencing it raw segfaults under SITL. */
        resolver_fn rv = *(resolver_fn *)SL(RESOLVER_SLOT);
        if (!rv) return put_str(out, "READOK:SAPIFIND resolver=0");

        int n = put_str(out, "READOK:SAPIFIND ");
        n += put_str(out + n, name);
        int hits = 0;
        for (unsigned a = 0; a < SAPI_HASH_ALGOS && n < 150; a++) {
            u32 h = sapi_name_hash(a, name);
            u32 p = rv(h, 0);
            if (!p) continue;                      /* unknown hash: the overwhelmingly common case */
            hits++;
            n += put_str(out + n, " a");
            n += put_uint(out + n, a);
            n += put_str(out + n, "=");
            n += put_hex32(out + n, p);
        }
        if (!hits) n += put_str(out + n, " none");
        return n;
    }
    if (prefix_eq(cmd, "CXCS#GPSSTAT")) {
        /* return codes are errno-ish and usually negative */
        #define PUT_SIGNED(v) do { int _v = (v); if (_v < 0) { out[n++] = '-'; \
                                   n += put_uint(out + n, (unsigned)(-_v)); } \
                                   else n += put_uint(out + n, (unsigned)_v); } while (0)                    /* GNSS health — its OWN reply (FWDSTAT is
                                                              * already at its buffer limit) */
        unsigned now = wd_uptime();
        int n = put_str(out, "READOK:GPSSTAT pwr");
        /* 0=unknown (no power call has succeeded), 1=off, 2=on. NOT a query of the receiver: the
         * resident API is a setter with no getter, so this is only what we last confirmed. */
        n += put_uint(out + n, g_gps_power_state);
        n += put_str(out + n, " nmea"); n += put_uint(out + n, g_nmea_lines);   /* 0 => nothing at all
                                                                                * from the receiver */
        n += put_str(out + n, " bad");  n += put_uint(out + n, g_nmea_bad);
        n += put_str(out + n, " rmc");  n += put_uint(out + n, g_rmc_seen);
        n += put_str(out + n, " vrmc"); n += put_uint(out + n, g_rmc_valid);    /* 0 with nmea>0 =>
                                                                                * receiver alive, no lock */
        n += put_str(out + n, " sats"); n += put_uint(out + n, g_numsats8);
        n += put_str(out + n, " fix");  n += put_uint(out + n, g_fixtype);      /* 2=2D 3=3D */
        n += put_str(out + n, " hdop"); n += put_uint(out + n, g_hdop_x10);
        /* seconds since the last NMEA sentence / the last valid fix; 999 = never */
        n += put_str(out + n, " nage");
        n += put_uint(out + n, g_nmea_ts ? (now - g_nmea_ts > 999 ? 999 : now - g_nmea_ts) : 999);
        n += put_str(out + n, " fage");
        n += put_uint(out + n, g_have_fix ? (now - g_fix_ts > 999 ? 999 : now - g_fix_ts) : 999);
        /* api0 => the resident slot pointer is NULL (nothing to call); api1 with a non-zero prc =>
         * the resident is there and rejecting us. Different faults, different fixes. */
        n += put_str(out + n, " api"); out[n++] = (char)('0' + (g_gps_api_ok ? 1 : 0));
        n += put_str(out + n, " prc"); PUT_SIGNED(g_gps_pwr_rc);
        n += put_str(out + n, " cfg"); PUT_SIGNED(g_gps_cfg_rc);
        n += put_str(out + n, " pf");  n += put_uint(out + n, g_gps_pwr_fails);
        n += put_str(out + n, " pnc"); n += put_uint(out + n, g_gps_pwr_nochange);
        n += put_str(out + n, " req"); n += put_uint(out + n, g_gps_req);   /* asked-for vs confirmed */
        return n;
        #undef PUT_SIGNED
    }
    if (prefix_eq(cmd, "CXCS#FWDSTAT")) {                    /* forwarding counters (own reply: room to spare) */
        int n = put_str(out, "READOK:FWDSTAT a");
        out[n++] = (char)('0' + (g_fwd_active ? 1 : 0));
        n += put_str(out + n, " rx");  n += put_uint(out + n, g_fwd_rx_calls);
        out[n++] = '/';                n += put_uint(out + n, g_fwd_rx_bytes);
        n += put_str(out + n, " mx");  n += put_uint(out + n, g_fwd_rx_max);
        n += put_str(out + n, " dp");  n += put_uint(out + n, g_fwd_drops);
        n += put_str(out + n, " tx");  n += put_uint(out + n, g_fwd_tx_bytes);
        n += put_str(out + n, " cn");  n += put_uint(out + n, g_fwd_conns);
        n += put_str(out + n, " w");
        out[n++] = (char)('0' + (g_fwd_txen ? 1 : 0));
        n += put_str(out + n, " rr"); n += put_uint(out + n, g_fwd_rereg);
        n += put_str(out + n, " p");  n += put_uint(out + n, rx_probe(0));
        n += put_str(out + n, " c");  n += put_uint(out + n, rx_probe(1));
        n += put_str(out + n, " at"); n += put_uint(out + n, rx_probe(2));
        n += put_str(out + n, " nw"); n += put_uint(out + n, g_fwd_nowr);
        n += put_str(out + n, " nb"); out[n++] = (char)('0' + (g_fwd_nb ? 1 : 0));
        n += put_str(out + n, " u");  out[n++] = (char)('0' + (g_fwd_udp ? 1 : 0));
        /* down direction (socket -> if04): dn = datagrams/bytes received, dw = bytes handed to the write,
         * dd = datagrams dropped by the per-pass cap, vh = if04 write backoffs, ka = keepalives sent */
        n += put_str(out + n, " dn"); n += put_uint(out + n, g_fwd_dn_pkts);
        out[n++] = '/';               n += put_uint(out + n, g_fwd_dn_bytes);
        n += put_str(out + n, " dw"); n += put_uint(out + n, g_fwd_dn_wr);
        n += put_str(out + n, " dd"); n += put_uint(out + n, g_fwd_dn_drop);
        n += put_str(out + n, " vh"); n += put_uint(out + n, g_vcom_holds);
        n += put_str(out + n, " ka"); n += put_uint(out + n, g_fwd_ka);
        n += put_str(out + n, " ud"); n += put_uint(out + n, g_fwd_up_drop);   /* up-path bytes shed */
        n += put_str(out + n, " dt"); n += put_uint(out + n, g_fwd_dn_trunc);  /* down datagrams truncated */
        return n;
    }
    /* USB-level probes live in their OWN reply. They used to be appended to FWDSTAT, but that reply is
     * already long and build_text_resp clamps the WIRE at 200 chars AFTER the buffer is built — so
     * appending both overflowed tb[] on the stack and truncated away the very fields we added. Two short
     * replies are correct; one growing string is not. */
    if (prefix_eq(cmd, "CXCS#USBSTAT")) {
        /* USB-level probes live in their OWN reply: FWDSTAT is already long and build_text_resp clamps the
         * WIRE at 200 chars AFTER the buffer is built, so appending to it both overflowed tb[] and
         * truncated away the newest fields. Two short replies, not one growing string.
         * p/c   = resident Modem1 producer/consumer indices (frozen together => nothing is arriving)
         * at    = AT-ready gate
         * df/dm/dz = free dTDs now / lowest ever seen / times seen empty  (dm falling to 0 is the
         *         predicted signature of the repost-starvation defect)
         * db    = our writes skipped on a real dTD shortage
         * wf    = persisted consecutive RX-death reboots */
        int n = put_str(out, "READOK:USBSTAT p");
        n += put_uint(out + n, rx_probe(0));
        n += put_str(out + n, " c");  n += put_uint(out + n, rx_probe(1));
        n += put_str(out + n, " at"); n += put_uint(out + n, rx_probe(2));
        unsigned dfree = usb_dtd_free();
        n += put_str(out + n, " df"); n += put_uint(out + n, dfree > 65535u ? 65535u : dfree);
        n += put_str(out + n, " dm"); n += put_uint(out + n, g_fwd_dtd_min);
        n += put_str(out + n, " dz"); n += put_uint(out + n, g_fwd_dtd_zero);
        n += put_str(out + n, " db"); n += put_uint(out + n, g_fwd_dtd_block);
        n += put_str(out + n, " rx"); n += put_uint(out + n, g_fwd_rx_calls);
        return n;
    }
    if (prefix_eq(cmd, "SZCS#FWDNB")) {                      /* 1 = keep FWD socket non-blocking (diag) */
        const char *pv = skip_to_val(cmd, "FWDNB");
        if (!pv || (pv[0] != '0' && pv[0] != '1')) return put_str(out, "SET ERROR: FWDNB");
        g_fwd_nb = (unsigned char)(pv[0] - '0');
        int n = put_str(out, "SETOK:FWDNB="); out[n++] = (char)('0' + g_fwd_nb);
        return n;
    }
    if (prefix_eq(cmd, "SZCS#FWDUDP")) {                     /* 1 = UDP proxy uplink (persisted) */
        const char *pv = skip_to_val(cmd, "FWDUDP");
        if (!pv || (pv[0] != '0' && pv[0] != '1')) return put_str(out, "SET ERROR: FWDUDP");
        unsigned char nv = (unsigned char)(pv[0] - '0');
        if (!fwd_store(g_fwd_ip, g_fwd_port, nv)) return put_str(out, "SET ERROR: write");   /* persist first */
        g_fwd_udp = nv; g_fwd_req = 1;                        /* then apply + rebuild the socket */
        int n = put_str(out, "SETOK:FWDUDP="); out[n++] = (char)('0' + g_fwd_udp);
        return n;
    }
    if (prefix_eq(cmd, "SZCS#USBRST")) {                     /* re-arm the resident USB RX pipeline */
        g_usbrst_req = 1;                                    /* performed by the main loop, not here */
        return put_str(out, "SETOK:USBRST");
    }
    if (prefix_eq(cmd, "SZCS#FWDTX")) {                      /* diagnostic: TCP->USB (down) direction on/off */
        const char *pv = skip_to_val(cmd, "FWDTX");
        if (!pv || (pv[0] != '0' && pv[0] != '1')) return put_str(out, "SET ERROR: FWDTX");
        g_fwd_txen = (unsigned char)(pv[0] - '0');
        int n = put_str(out, "SETOK:FWDTX=");
        out[n++] = (char)('0' + g_fwd_txen);
        return n;
    }
    if (prefix_eq(cmd, "CXCS#FWDSERVER") || prefix_eq(cmd, "CXCS#FWDPORT")) {
        int n = put_str(out, "READOK:FWDSERVER=");
        if (g_fwd_ip[0] | g_fwd_ip[1] | g_fwd_ip[2] | g_fwd_ip[3]) {   /* show a staged IP even if port=0 */
            char ipb[16]; int il = 0;
            for (int k = 0; k < 4; k++) { il += put_uint(ipb + il, g_fwd_ip[k]); if (k < 3) ipb[il++] = '.'; }
            ipb[il] = 0;
            n += put_str(out + n, ipb);
        } else n += put_str(out + n, "0");
        n += put_str(out + n, "#FWDPORT="); n += put_uint(out + n, g_fwd_port);
        return n;
    }
    if (prefix_eq(cmd, "SZCS#FWDSERVER") || prefix_eq(cmd, "SZCS#FWDPORT")) {
        const char *pi = skip_to_val(cmd, "FWDSERVER");
        const char *pp = skip_to_val(cmd, "FWDPORT");
        unsigned char ip[4]; for (int k = 0; k < 4; k++) ip[k] = g_fwd_ip[k];   /* default: keep current */
        int port = (int)g_fwd_port;
        if (pi && !parse_ip(pi, ip)) return put_str(out, "SET ERROR: FWDSERVER");
        int explicit_off = 0;
        if (pp) { const char *q = pp; port = parse_int(&q);
                  if (port < 0 || (*q != '#' && *q != 0 && *q != ' ')) return put_str(out, "SET ERROR: FWDPORT");
                  explicit_off = (port <= 0); }                                /* FWDPORT=0 => disable */
        if (port > 65535) return put_str(out, "SET ERROR: FWDPORT");
        if (explicit_off) { port = 0; ip[0] = ip[1] = ip[2] = ip[3] = 0; }     /* explicit disable: clear both */
        else if (port <= 0) port = 0;                                          /* still disabled (IP may be staged) */
        else if (!(ip[0] | ip[1] | ip[2] | ip[3])) return put_str(out, "SET ERROR: FWDSERVER");
        if (!fwd_store(ip, (unsigned)port, g_fwd_udp)) return put_str(out, "SET ERROR: write");
        for (int k = 0; k < 4; k++) g_fwd_ip[k] = ip[k];
        g_fwd_port = (unsigned)port;
        g_fwd_en = (port > 0) ? 1 : 0;
        g_fwd_req = 1;                                        /* loop drops + reconnects / tears down */
        int n = put_str(out, "SETOK: FWDSERVER =");
        if (ip[0] | ip[1] | ip[2] | ip[3]) {           /* reflect a staged IP even when port=0 (disabled) */
            char ipb[16]; int il = 0;
            for (int k = 0; k < 4; k++) { il += put_uint(ipb + il, ip[k]); if (k < 3) ipb[il++] = '.'; }
            ipb[il] = 0; n += put_str(out + n, ipb);
        } else n += put_str(out + n, "0");
        n += put_str(out + n, "#FWDPORT ="); n += put_uint(out + n, (unsigned)port);
        return n;
    }
    return 0;
}

/* item 16 rung pacing (SITL uses short waits so the scenario runs in seconds — same logic/code) */
#ifdef SITL
#define REC_OFFLINE_SEC 30
#define REC_STEP_SEC    20
#define REC_STABLE_SEC  15
#define REC_HOLDOFF_SEC 120
#else
#define REC_OFFLINE_SEC 300      /* offline + unregistered this long -> start the ladder */
#define REC_STEP_SEC    120      /* settle time after each rung */
#define REC_STABLE_SEC  120      /* connected this long after a manual COPS -> restore auto */
#define REC_HOLDOFF_SEC 3600     /* pause after an exhausted ladder (no attach storms) */
#endif

/* Cellular auto-recovery driver (item 16) — called every main-loop pass; all action is timer-gated.
 * Only escalates while the server is unreachable AND the modem is NOT registered (CEREG stat not 1/5):
 * if we're registered but can't connect, the PLMN isn't the problem and the ladder stays out of it. */
static void cell_recovery(unsigned now, int connected)
{
    static unsigned off_since, rung_ts, hold_until, poll_ts, stable_since;
    static unsigned char rung;          /* 0 idle; 1 cfun; 2 cops-auto; 3 scan; 4 settle/exhausted */
    static unsigned char two_phase;     /* rung 1/2 second half pending (CFUN=1 / COPS=0) */
    if (connected) {
        off_since = 0;
        if (rung) { rung = 0; two_phase = 0; vputs("REC: recovered\r\n"); }
        /* Registered via manual COPS: once the server link has proven out, restore automatic
         * selection (manual would strand a unit that travels). If auto re-camps on the bad PLMN
         * we simply run the ladder again, holdoff-limited. */
        if (g_cops_manual) {
            if (!stable_since) stable_since = now ? now : 1;
            if ((now - stable_since) >= REC_STABLE_SEC) {
                g_cops_manual = 0; stable_since = 0;
                at_cmd("AT+COPS=0\r");
                vputs("REC: restore-auto\r\n");
            }
        } else stable_since = 0;
        return;
    }
    stable_since = 0;
    if (!off_since) { off_since = now ? now : 1; return; }
    /* poll registration (direct resident getter — no AT text near the NMEA/blind-buffer stream) and
     * the camped PLMN (AT — no getter found; rare, offline-only, and the NMEA gates cover it) */
    if ((now - poll_ts) >= 30) {
        poll_ts = now;
        get_reg_fn rf = *(get_reg_fn *)SLOT_GET_REG;
        unsigned rv;
        if (rf && rf(&rv) == 0) g_cereg_stat = (signed char)(rv & 0x7f);
        at_cmd("AT+COPS?\r");
    }
    if (g_cereg_stat == 1 || g_cereg_stat == 5) { rung = 0; two_phase = 0; return; }
    if (now < hold_until) return;
    switch (rung) {
    case 0:
        if ((now - off_since) < REC_OFFLINE_SEC) return;
        at_cmd("AT+CFUN=0\r"); two_phase = 1; rung = 1; rung_ts = now;
        vputs("REC: cfun\r\n");
        break;
    case 1:
        if (two_phase && (now - rung_ts) >= 5) { at_cmd("AT+CFUN=1\r"); two_phase = 0; }
        if ((now - rung_ts) >= REC_STEP_SEC) {
            at_cmd("AT+COPS=2\r"); two_phase = 1; rung = 2; rung_ts = now;
            vputs("REC: cops-auto\r\n");
        }
        break;
    case 2:
        if (two_phase && (now - rung_ts) >= 5) { at_cmd("AT+COPS=0\r"); two_phase = 0; }
        if ((now - rung_ts) >= REC_STEP_SEC) {
            g_scan_len = 0; g_scan_acc[0] = 0; g_plmn_alt[0] = 0; g_scan_pend = 1;
            at_cmd("AT+COPS=?\r"); rung = 3; rung_ts = now;
            vputs("REC: scan\r\n");
        }
        break;
    case 3:
        if (g_plmn_alt[0]) {                       /* scan found an alternate -> manual-select it */
            char b[32]; int n = put_str(b, "AT+COPS=1,2,\"");
            n += put_str(b + n, g_plmn_alt);
            b[n++] = '"'; b[n++] = '\r'; b[n] = 0;
            at_cmd(b);
            g_cops_manual = 1; g_plmn_alt[0] = 0; g_scan_pend = 0;
            rung = 4; rung_ts = now;
            vputs("REC: manual\r\n");
        } else if ((now - rung_ts) >= REC_STEP_SEC) {   /* scan gave nothing usable */
            g_scan_pend = 0; rung = 4; rung_ts = now;
        }
        break;
    default:                                        /* settle; then holdoff + ladder again */
        if ((now - rung_ts) >= REC_STEP_SEC) {
            hold_until = now + REC_HOLDOFF_SEC;
            rung = 0; two_phase = 0;
            off_since = now ? now : 1;              /* restart the offline clock */
        }
        break;
    }
}

/* Drain server->device bytes: accumulate across TCP reads, extract whole GT06 frames, ACK every 0x80
 * command (0x15) and apply TIMER. Sets *changed=1 when the track/idle state flips. All frame fields are
 * bounds-checked (a malformed/truncated frame can't read past the frame or the accumulator). */
static void handle_rx(int fd, send_fn se, unsigned char *acc, int *acclen, int cap,
                      const unsigned char *in, int inlen,
                      int *track, int *t1, int *t2, unsigned short *serial, int *changed)
{
    if (*acclen + inlen > cap) *acclen = 0;        /* resync: drop the stale partial, never the new bytes */
    for (int i = 0; i < inlen; i++) acc[(*acclen)++] = in[i];
    int i = 0;
    while (i + 5 <= *acclen) {
        if (acc[i] != 0x78 || acc[i + 1] != 0x78) { i++; continue; }
        int total = acc[i + 2] + 5;                /* 78 78 len [len bytes] 0d 0a */
        if (i + total > *acclen) break;            /* frame incomplete — wait for more */
        if (acc[i + total - 2] != 0x0d || acc[i + total - 1] != 0x0a) { i++; continue; }  /* misframed */
        if (acc[i + 3] == 0x80) {
            int dlen = acc[i + 2] - 5;             /* proto+data+serial+crc => data-block length */
            int alen = acc[i + 4] - 4;             /* content_len - server_flag(4) */
            if (alen < 0 || dlen < 5) alen = 0;    /* no room for [content_len][flag4][ascii] */
            else if (alen > dlen - 5) alen = dlen - 5;   /* clamp ascii to inside this frame */
            char cmd[200];                             /* wide enough for a YCSJ,<url>,<port># command */
            int m = alen > (int)sizeof cmd - 1 ? (int)sizeof cmd - 1 : alen;
            for (int k = 0; k < m; k++) cmd[k] = (char)acc[i + 9 + k];
            cmd[m] = 0;
            unsigned char resp[224];
            /* Must hold the LONGEST reply any handler builds, because build_text_resp's 200-char clamp
             * happens after the write — it cannot save an already-overflowed buffer. FWDSTAT is the
             * longest: ~247 chars with every counter at its 10-digit maximum. */
            char tb[288];
            int cn;
            if ((cn = handle_config_cmd(cmd, tb)) > 0) {
                se(fd, resp, build_text_resp(tb, cn, (*serial)++, resp), 0);
            } else if (m >= 4 && cmd[0]=='c' && cmd[1]=='x' && cmd[2]=='z' && cmd[3]=='t') {
                se(fd, resp, build_text_resp(tb, cxzt_str(tb, *t1, *t2), (*serial)++, resp), 0);
            } else if (g_batt_mv && m >= 6 && cmd[0]=='S' && cmd[1]=='T' && cmd[2]=='A' &&
                       cmd[3]=='T' && cmd[4]=='U' && cmd[5]=='S') {
                se(fd, resp, build_text_resp(tb, status_str(tb), (*serial)++, resp), 0);
            } else {
                se(fd, resp, build_cmd_resp(cmd, m, cmd_recognized(cmd), (*serial)++, resp), 0);
            }
            if (m >= 6 && cmd[0] == 'T' && cmd[1] == 'I' && cmd[2] == 'M' &&
                cmd[3] == 'E' && cmd[4] == 'R' && cmd[5] == ',') {
                const char *p = cmd + 6;
                int a = parse_int(&p), b = -1;
                if (*p == ',') { p++; b = parse_int(&p); }
                if (a > 0) {
                    *t1 = a; if (b > 0) *t2 = b;
                    int nt = (a <= TRACK_T1_MAX) ? 1 : 0;
                    if (nt != *track) { *track = nt; *changed = 1; }
                    vhex("ST:TIMER_t1", (unsigned)a);
                    vhex("ST:track", (unsigned)*track);
                }
            }
            /* CANCELSOS# clears a latched SOS (item 17). The operator sends it (e.g. via the manage API)
             * after handling the alarm; a plain reboot also clears it. Nothing else clears g_sos_active —
             * a real alarm must not be droppable by the tracker itself. */
            if (m >= 9 && cmd[0]=='C' && cmd[1]=='A' && cmd[2]=='N' && cmd[3]=='C' && cmd[4]=='E' &&
                cmd[5]=='L' && cmd[6]=='S' && cmd[7]=='O' && cmd[8]=='S') {
                g_sos_active = 0; g_sos_sends = 0;
                vputs("SOS: cleared by CANCELSOS\r\n");
            }
            /* RESET# => reboot (item 11, management-UI reboot). The ACK was queued above; the main loop
             * runs the reset after a short flush so the server sees the OK, then a clean relogin. */
            if (m >= 5 && cmd[0]=='R' && cmd[1]=='E' && cmd[2]=='S' && cmd[3]=='E' && cmd[4]=='T') {
                g_reset_req = 1; vputs("RESET: reboot requested\r\n");
            }
            /* HBT,<ACC-ON>,<ACC-OFF># sets the heartbeat interval; honour it (the server disconnects a unit
             * with no heartbeat, and idle must keep the connection alive) instead of using TIMER T2. */
            if (m >= 4 && cmd[0]=='H' && cmd[1]=='B' && cmd[2]=='T' && cmd[3]==',') {
                const char *p = cmd + 4;
                int a = parse_int(&p), b = -1;
                if (*p == ',') { p++; b = parse_int(&p); }
                /* Clamp to a sane range: a garbage value (parse has no overflow guard) must never
                 * disable the heartbeat and let the server drop us. Out-of-range keeps the prior good value. */
                if (a > 0 && a <= HBT_MAX) g_hbt_on = (unsigned)a;
                if (b > 0 && b <= HBT_MAX) g_hbt_off = (unsigned)b;
                vhex("ST:HBT_off", g_hbt_off);
            }
#if FOTA
            /* YCSJ,<http-url>,<port># => queue a FOTA download for the main loop (don't block RX here). */
            if (m >= 5 && cmd[0]=='Y' && cmd[1]=='C' && cmd[2]=='S' && cmd[3]=='J' && cmd[4]==',') {
                if (fota_parse(cmd + 5)) { g_fota_req = 1; vputs("FOTA: YCSJ queued\r\n"); }
            }
#endif
        }
        i += total;
    }
    int rem = *acclen - i;
    for (int k = 0; k < rem; k++) acc[k] = acc[i + k];
    *acclen = rem;
}

#if FOTA
/* Run a queued FOTA: HTTP-GET the package from g_fota_ip/port/path, stream it into the resident flash API
 * (stages in `reserved`), verify the wrapper CRC, then reboot so the resident commits reserved->customer_app.
 * Blocking; feeds the watchdog. On ANY failure it reboots WITHOUT close() -> no pending marker -> customer_app
 * untouched (field-safe). Never returns on success (reboots); returns only on an early socket failure. */
static void fota_run(void)
{
    g_fota_req = 0;
    g_wd_phase = 'O';
    sys_reset_fn reset = *(sys_reset_fn *)SLOT_SYS_RESET;
    socket_fn  so = *(socket_fn  *)SLOT_SOCKET;
    connect_fn co = *(connect_fn *)SLOT_CONNECT;
    send_fn    se = *(send_fn    *)SLOT_SEND;
    recv_fn    rv = *(recv_fn    *)SLOT_RECV;
    close_fn   cl = *(close_fn   *)SLOT_CLOSE;
    flash_prepare_fn fprep = *(flash_prepare_fn *)SLOT_FLASH_PREPARE;
    flash_open_fn    fopn  = *(flash_open_fn    *)SLOT_FLASH_OPEN;
    flash_write_fn   fwr   = *(flash_write_fn   *)SLOT_FLASH_WRITE;
    flash_close_fn   fcls  = *(flash_close_fn   *)SLOT_FLASH_CLOSE;
    flash_verify_fn  fver  = *(flash_verify_fn  *)SLOT_FLASH_VERIFY;
    vputs("FOTA: start\r\n");
    if (fprep) fprep();                                  /* modem coordination (fast when registered) */

    int fd = so(2, 1, 0);
    if (fd < 0) { vputs("FOTA: sock fail\r\n"); return; }
    unsigned char sa[24];
    fill_server_addr(sa, g_fota_ip, g_fota_port);
    if (co(fd, sa, 24) != 0) { vputs("FOTA: connect fail\r\n"); cl(fd); return; }

    char req[224];
    int rl = put_str(req, "GET ");
    rl += put_str(req + rl, g_fota_path);
    rl += put_str(req + rl, " HTTP/1.1\r\nHost: ");
    for (int k = 0; k < 4; k++) { rl += put_uint(req + rl, g_fota_ip[k]); if (k < 3) req[rl++] = '.'; }
    if (g_fota_port != 80) { req[rl++] = ':'; rl += put_uint(req + rl, g_fota_port); }
    rl += put_str(req + rl, "\r\nConnection: close\r\n\r\n");
    for (int sent = 0; sent < rl; ) {              /* send() may be short: loop until the whole request is out */
        int w = se(fd, req + sent, rl - sent, 0);
        if (w <= 0) { vputs("FOTA: send fail\r\n"); cl(fd); return; }
        sent += w;
    }

    unsigned char mode = 0x77;
    if (!fopn || fopn(&mode) != 0) { vputs("FOTA: flash open fail\r\n"); cl(fd); return; }

    /* Stream the response: skip HTTP headers (\r\n\r\n), then write the body to flash while CRC'ing the
     * payload (bytes after the 8-byte [clen][~crc32] wrapper). recv() on this fresh socket blocks ~5s per
     * timeout; a body byte resets the stall counter. Any write/socket fault -> reboot without close(). */
    unsigned char buf[512], wrap[8];
    int hdr_done = 0, h4 = 0, wrapn = 0;
    unsigned total = 0, crc = 0xFFFFFFFFu, idle = 0, rx_total = 0, cap = 0;
    for (;;) {
        wd_alive();
        int n = rv(fd, buf, sizeof buf, 0);
        if (n == 0) break;                               /* peer closed => body complete */
        if (n < 0) { if (++idle > 12) { vputs("FOTA: dl timeout\r\n"); reset(); } continue; }
        idle = 0;
        rx_total += (unsigned)n;                         /* bound the whole transfer (slow-loris / endless body) */
        if ((!hdr_done && rx_total > 8192u) || rx_total > 0x50000u) { vputs("FOTA: rx too big\r\n"); reset(); }
        int off = 0;
        if (!hdr_done) {
            for (; off < n; off++) {
                unsigned char c = buf[off];
                if ((h4 == 0 || h4 == 2) && c == '\r') h4++;
                else if ((h4 == 1 || h4 == 3) && c == '\n') h4++;
                else h4 = 0;
                if (h4 == 4) { hdr_done = 1; off++; break; }
            }
            if (!hdr_done) continue;
        }
        int m2 = n - off;
        for (int j = 0; j < m2; ) {
            int chunk = m2 - j; if (chunk > 1024) chunk = 1024;
            for (int c = 0; c < chunk && wrapn < 8; c++) wrap[wrapn++] = buf[off + j + c];
            /* Once the [clen] wrapper is in, the payload ends at 8+clen. Any bytes after that are the
             * vendor OTA's trailing 8-bit-XOR pad byte(s) — strip them so ONE image (the XOR-padded
             * standalone_fw.bin) feeds BOTH paths: the vendor's whole-file-XOR check AND our CRC check.
             * We write/CRC only the [wrapper][payload] (=clen+8), never the pad, so `total` lands at
             * clen+8 for a padded or a pad-free image alike and every check below still holds. */
            if (wrapn >= 8 && !cap)
                cap = 8u + (wrap[0] | (wrap[1] << 8) | (wrap[2] << 16) | ((unsigned)wrap[3] << 24));
            unsigned wchunk = (unsigned)chunk;
            if (cap && total + wchunk > cap) wchunk = (total < cap) ? (cap - total) : 0u;   /* drop pad tail */
            if (wchunk) {
                /* Never write past `reserved` (0x3c000): cap the staged image at customer_app size (0x30000). */
                if (total + wchunk > 0x30000u) { vputs("FOTA: oversize - reboot, no commit\r\n"); reset(); }
                int pstart = (total < 8) ? (int)(8 - total) : 0;    /* CRC only over payload (skip 8B wrapper) */
                if ((unsigned)pstart < wchunk) crc = crc32_upd(crc, buf + off + j + pstart, (int)(wchunk - (unsigned)pstart));
                if (fwr(buf + off + j, wchunk) != 0) { vputs("FOTA: write fail\r\n"); reset(); }
                total += wchunk;
            }
            j += chunk;
        }
    }
    cl(fd);

    unsigned clen = 0, wantcrc = 0;
    if (wrapn >= 8) {
        clen    = wrap[0] | (wrap[1] << 8) | (wrap[2] << 16) | ((unsigned)wrap[3] << 24);
        wantcrc = wrap[4] | (wrap[5] << 8) | (wrap[6] << 16) | ((unsigned)wrap[7] << 24);
    }
    vhex("FOTA:total", total); vhex("FOTA:clen", clen); vhex("FOTA:crc", crc); vhex("FOTA:want", wantcrc);
    /* Validate BEFORE close() — close() sets the pending-update marker and there is no clean abort, so we
     * must never mark a bad image. Reject anything malformed or that the resident verifier would reject. */
    if (total < 9 || total != clen + 8 || crc != wantcrc ||
        clen == 0 || 8u + clen > 0x30000u || wantcrc == 0 || wantcrc == 0xFFFFFFFFu) {
        vputs("FOTA: BAD image - reboot, no commit\r\n");
        reset();                                         /* no close() => no marker => customer_app untouched */
    }
    if (fcls() != 0) { vputs("FOTA: close fail\r\n"); reset(); }
    /* Resident re-reads the STAGED image and re-CRCs it: require it matches the package we just downloaded
     * (so a stale or partially-written `reserved` cannot be committed). Only then reboot for the boot path to
     * commit reserved->customer_app. On any mismatch we do NOT reboot: the marker is set, but the resident's
     * boot-time re-verify rejects a bad CRC, and meanwhile we keep running the current (good) app. */
    unsigned info[3] = {0, 0, 0};
    if (fver(info) == 0 && info[0] == 1 && info[1] == clen && info[2] == wantcrc) {
        vputs("FOTA: staged OK - reboot to commit\r\n");
        for (volatile int d = 0; d < 8000000; d++) { }   /* let the VCOM prints flush before reset */
        reset();
    }
    vhex("FOTA:vfy", info[0]); vputs("FOTA: verify FAIL - staying on current app\r\n");
}
#endif /* FOTA */



/* traced send: marks phase 'S' so a WD stall capture names a blocked TCP send */
static send_fn g_send_raw;
static int send_traced(int fd, const void *buf, int len, int flags)
{
    unsigned char pp = g_wd_phase; g_wd_phase = 'S';
    int r = g_send_raw(fd, buf, len, flags);
    g_wd_phase = pp;
    return r;
}

/* ================= USB<->TCP forwarding bridge runtime (UAV LTE modem link) =================
 * FWDSERVER/FWDPORT set -> keep a SECOND TCP socket to that host connected (non-blocking, auto-reconnect)
 * and bridge raw bytes: if04 (USB) -> TCP, and TCP -> if04. Independent of the GT06 tracking socket, which
 * keeps running. On (re)connect the USB->TCP buffer is flushed so no stale data replays. */
#define FWD_RING_SZ      32768u  /* USB->TCP ring (power of 2). 8K was ~1 s of buffer at the observed rate,
                                  * so a brief uplink dip overflowed it; 32K rides out cellular stalls. */
#define FWD_PUMP_MS      20      /* loop wait cap while forwarding -> ~50 Hz bridge pump (low latency) */
#define FWD_TX_STALL_SEC 10      /* USB->TCP data stuck this long (half-open socket) -> drop + reconnect */
#define FWD_WD_MIN_UP     180    /* never fire the RX-death reboot in the first N s after boot */
#define FWD_RX_REG_SEC    10     /* retry the if04 RX registration this often while it has NEVER delivered */
#define FWD_RX_REG_TRIES  12     /* bounded: stop retrying after this many attempts */
#define FWD_UDP_MAX_DGRAM 4      /* max UDP datagrams sent UP per fwd_poll pass (rest discarded: no buildup).
                                  * Must stay >1: the no-buildup policy drops whatever is left after the cap,
                                  * so throttling this throws away uplink data outright (SITL UA proves it). */
#define FWD_USB_MAX_WR    1      /* max if04 WRITES per pass — the DOWN direction only. 1 (was sharing the
                                  * uplink cap of 4) while proving out dTD starvation: at 50 Hz that is still
                                  * 50 datagrams/s down, ~10x what the link carries, while giving the dTD
                                  * pool the longest possible recovery window between our writes. */
#define FWD_KA_SEC        3      /* uplink idle before a 1-byte keepalive (proxy forgets our tuple at ~10 s) */
#define FWD_UP_MAX_BACKLOG 16384u /* ride out a stall up to this much queued (32 K ring, ~3.5 s at 4.5 KB/s) */
#define FWD_UP_MAX_AGE_SEC 4      /* ...and no longer than this; past it the data is too stale to be useful */

static unsigned char g_fwd_ring[FWD_RING_SZ];
static volatile unsigned g_fwd_head, g_fwd_tail;   /* SPSC: head=producer (USB rx cb), tail=consumer (loop) */
/* g_fwd_drops + the rx/tx/conn/pass counters are declared up top (cxzt_str reports them). */

/* producer: copy host->device bytes into the ring. Only while the TCP link is up, so a disconnected period
 * accumulates nothing (no stale replay). Runs in the resident USB task context (SITL: a listener thread). */
static void usb_ring_put(const unsigned char *b, int n)
{
    if (!g_fwd_active) return;
    /* ALL-OR-NOTHING per record. Copying until the ring fills and then bailing would leave a MAVLink
     * frame truncated mid-message, desyncing the far-end parser for everything that follows — under
     * overload that turns a bandwidth shortfall into a corrupt stream. Each callback carries one CDC
     * record (whole host write), so dropping the record wholesale keeps what does get through parseable. */
    if (n <= 0) return;
    if ((unsigned)n > FWD_RING_SZ - (g_fwd_head - g_fwd_tail)) { g_fwd_drops++; return; }
    for (int i = 0; i < n; i++) {
        g_fwd_ring[g_fwd_head & (FWD_RING_SZ - 1)] = b[i];
        g_fwd_head++;
    }
}
/* resident RX callback (ABI: r0 = #bytes pending on if04). Drain them via the read slot into the ring. */
/* ⚠ The resident read (0x7e69177a) IGNORES its length argument: it memcpy()s the WHOLE current RX
 * descriptor into the caller's buffer and returns that length. Handing it a small buffer therefore
 * overruns it — and since this callback runs on the RESIDENT USB TASK's stack, a record >128 B smashed
 * that task's saved registers/LR and killed it permanently (recoverable only by reboot). That was the
 * "if04 RX freezes after 19-304 KB" bug: the first coalesced record over the buffer size killed it, which
 * is why the time-to-death varied. Fix: ONE read into a static buffer larger than any record the resident
 * dispatcher emits (it chunks at 0x200), never a loop, and refuse to read at all if it announces more. */
#define VCOM_RX_MAX 1024u
static unsigned char g_vcom_rx_buf[VCOM_RX_MAX];   /* static: don't consume the resident task's stack */
static void hw_usb_rx_cb(u32 pending)
{
    usb_read_fn rd = *(usb_read_fn *)SLOT_VCOM_READ;
    if (!rd) return;
    g_fwd_rx_calls++;
    if (pending > g_fwd_rx_max) g_fwd_rx_max = pending;   /* proves records exceed the old 128 B buffer */
    if (pending == 0 || pending > sizeof g_vcom_rx_buf) { g_fwd_drops++; return; }
    int got = rd(g_vcom_rx_buf, (int)pending);            /* length arg ignored by the resident */
    if (got <= 0) return;
    unsigned n = (unsigned)got;
    if (n > sizeof g_vcom_rx_buf) { g_fwd_drops++; n = sizeof g_vcom_rx_buf; }   /* never trust it past the buffer */
    g_fwd_rx_bytes += n;
    usb_ring_put(g_vcom_rx_buf, (int)n);
}
/* ---- Why if04 RX dies, and how we stop killing it (RE 2026-07-25, Codex + disassembly) ----
 * After our RX callback returns, the resident dispatcher reposts the next OUT transfer via *(0x7e45ab38)
 * -> 0x7e5c90c3 -> 0x7e60d264. That, and every USB send (0x7e60d3d6), allocate a hardware transfer
 * descriptor from the common dTD allocator 0x7e60da6a — a 64-entry pool built by 0x7e60d6ac, free-list
 * head at H+0x40, u16 free count at H+0x156, H = *(u32*)0x7e45ab70.
 * When that free list is empty the allocator prints "Error: Can't get dTD", RETURNS ZERO — which its
 * callers read as SUCCESS — posts nothing, and never retries. So a single momentary exhaustion at the
 * instant the dispatcher reposts if04 RX loses that repost PERMANENTLY: RX is dead until a full reboot.
 * Because the pool is shared with the down direction, OUR OWN writes can cause it, which is why
 * time-to-death tracked traffic rather than any fixed threshold (19 KB one boot, 12 MB another, and
 * 188 KB / 269 KB / 358 KB / 806 KB / 1.2 MB across 2026-07-25).
 * It also explains the DTR symptom: CDC SET_CONTROL_LINE_STATE (0x22, handled at 0x7e5c8914) queues its
 * ep0 status stage through the SAME allocator, so exhaustion presents to the host as EPROTO on every
 * interface while already-posted bulk-IN transfers keep completing.
 * ⚠ NOT the whole story: a device can also come up with USB dead from boot and -71 on SET_CONFIGURATION,
 * which survives reboots and is cleared by a physical re-plug — that one is host/link, not this. */
#define USB_POOL_HANDLE_P 0x7e45ab70u    /* -> USB pool handle */
#define USB_DTD_HEAD_OFF  0x40u          /* handle + 0x40 = dTD free-list head (0 = exhausted) */
#define USB_DTD_FREE_OFF  0x156u         /* handle + 0x156 = u16 free dTD count */
#define FWD_DTD_RESERVE   8              /* dTDs held back so the RX repost can always allocate */
#ifdef SITL
static unsigned usb_dtd_free(void) { return ~0u; }
#else
/* Free dTDs, or ~0u if the probe reads something implausible. Unlike the XD count this is the pool the
 * RX repost competes for, so our writes must never take it to zero. */
static unsigned usb_dtd_free(void)
{
    unsigned long h = *(volatile unsigned *)USB_POOL_HANDLE_P;
    if (h < 0x7e000000ul || h >= 0x7f000000ul || (h & 3ul)) return ~0u;
    unsigned n = *(volatile unsigned short *)(h + USB_DTD_FREE_OFF);
    return n > 64u ? ~0u : n;                          /* 64-entry pool; garbage -> "plenty" */
}
#endif
/* Write guard on the dTD pool — the one that actually starves.
 * Both the RX repost (0x7e60d264) and every USB send (0x7e60d3d6) allocate from the common dTD allocator
 * 0x7e60da6a. When its free list is empty that allocator prints "Error: Can't get dTD", RETURNS ZERO —
 * which its callers read as SUCCESS — posts nothing, and never retries, so one momentary exhaustion at the
 * instant the dispatcher reposts if04 RX loses that repost permanently (reboot-only recovery). It also
 * explains the DTR symptom: CDC SET_CONTROL_LINE_STATE queues its ep0 status stage through the SAME
 * allocator, so exhaustion shows up on the host as EPROTO on every interface.
 * So: hold FWD_DTD_RESERVE entries back from our own writes. Skipping a datagram costs one command;
 * losing the repost costs the whole uplink until reboot.
 * NEVER fails open on a valid-but-low count — failing open on a real shortage is precisely how the repost
 * gets starved. An implausible reading (~0u) means the probe is wrong, and then we do not block at all. */
static int usb_pool_ok(unsigned now)
{
    (void)now;
    unsigned dtd = usb_dtd_free();
    if (dtd == ~0u) return 1;                       /* probe not trustworthy -> never block on it */
    if (dtd > FWD_DTD_RESERVE) return 1;
    g_fwd_dtd_block++;
    return 0;
}
static void usb_rx_start(void)                      /* register our if04 RX callback (HW slot / SITL mock) */
{
    usb_regcb_fn reg = *(usb_regcb_fn *)SLOT_VCOM_REGCB;
    static const char nm[] = "wstr_fwd_rx";
    if (reg) reg(hw_usb_rx_cb, nm);
}
/* device->host TX (TCP->USB). HW: if04 via the guarded VCOM write. SITL: a dedicated client fd so the data
 * stream stays separate from the debug markers on stdout. */
static void usb_tx(const void *buf, int n)
{
    if (!g_fwd_txen) return;           /* SZCS#FWDTX=0: drop the down direction (isolates whether the
                                        * blocking if04 write is what wedges the resident RX callback) */
#ifdef SITL
    extern int sitl_usb_data_write(const void *, int);
    sitl_usb_data_write(buf, n);
    g_fwd_dn_wr += (unsigned)n;
#else
    /* count only what the guard actually wrote: dn_bytes-vs-dw is then a true statement about where the
     * down direction is being lost (per-pass cap, FWDTX, or the if04 write backoff). */
    if (vcom_write_guarded(buf, n)) g_fwd_dn_wr += (unsigned)n;
#endif
}

/* Non-blocking {0,0} select for writability on one fd — the reliable "can this socket send now?" test on
 * this resident (its send() return code is not trustworthy). Returns 1 if writable. */
static int sock_writable(int fd)
{
    if (fd < 0 || fd >= 256) return 0;
    select_fn sel = *(select_fn *)SLOT_SELECT;
    unsigned char ws[32];
    for (int i = 0; i < 32; i++) ws[i] = 0;
    ws[fd >> 3] |= (unsigned char)(1u << (fd & 7));
    int tv[2] = {0, 0};
    int nr = sel(fd + 1, 0, ws, 0, tv);
    return (nr > 0 && (ws[fd >> 3] & (1u << (fd & 7)))) ? 1 : 0;
}

/* One pass of the forwarding bridge: keep the FWD socket connected (non-blocking, auto-reconnect), drain the
 * USB->TCP ring out, pump TCP->USB in. Own fd/state (statics) — a second concurrent socket alongside GT06. */
static void fwd_poll(unsigned now, socket_fn so, connect_fn co, send_fn se, recv_fn rv, close_fn cl)
{
    static int fd = -1, conn = 0, cing = 0;
    static unsigned cstart = 0, lconn = 0u - (unsigned)CONN_RETRY_SEC, last_tx = 0, bad_since = 0;
    static unsigned char bad_active = 0;         /* UDP: a send is currently failing (bad_since = when it began) */
    static unsigned last_up;                     /* UDP: last successful uplink send (payload or keepalive) */
    static unsigned last_ka;                     /* UDP: last keepalive ATTEMPT (paces retries on a bad socket) */
    static unsigned backlog_since;               /* when the up-path ring last went non-empty (age bound) */

    g_fwd_passes++;
    {   /* latch the dTD low-water even on passes where we do not write — the starvation we care about can
         * happen while our own traffic is idle, and a single sample in FWDSTAT would always miss it. */
        unsigned d = usb_dtd_free();
        if (d != ~0u) { if (d < g_fwd_dtd_min) g_fwd_dtd_min = d; if (d == 0) g_fwd_dtd_zero++; }
    }
    if (g_fwd_req) {                            /* config changed -> drop, re-evaluate below */
        if (fd >= 0) { cl(fd); fd = -1; }
        conn = cing = 0; g_fwd_active = 0; g_fwd_req = 0;
    }
    if (!g_fwd_en) {                            /* disabled -> ensure torn down */
        if (fd >= 0) { cl(fd); fd = -1; }
        conn = cing = 0; g_fwd_active = 0;
        return;
    }
    /* if04 RX registration retry — ONLY while the callback has NEVER delivered (g_fwd_rx_calls == 0).
     * Observed live: after a run of USB re-enumerations/resets the unit booted with rx0/0 forever while
     * the host wrote 128/s, i.e. the boot-time usb_rx_start() registration silently didn't take. That is
     * a DIFFERENT failure from the dead-USB-task case (where re-registering is useless and was removed):
     * here nothing is wired up yet, so retrying is the only way back short of a reboot. Bounded attempts,
     * and it stops permanently the instant a single callback arrives. */
    if (g_fwd_active && g_fwd_rx_calls == 0) {
        static unsigned last_try; static unsigned char tries;
        if (tries < FWD_RX_REG_TRIES && (unsigned)(now - last_try) >= FWD_RX_REG_SEC) {
            last_try = now; tries++;
            usb_rx_start(); g_fwd_rereg++;
        }
    }
    /* NO automatic USB recovery here, deliberately. Both mechanisms that used to live at this point
     * triggered on "if04 RX has been silent", which cannot distinguish a wedged endpoint from a host that
     * simply is not writing — and both recovery actions GUARANTEE that silence, so they self-trigger.
     * FWDARM re-enumerated the modem (host loses the port for ~7 s and it returns with different node
     * numbers); FWDWD rebooted the whole tracker and was measured reboot-looping a unit on a ~100 s cycle,
     * taking the cell link down with it. Recovery is now operator-driven: SZCS#USBRST re-arms USB and
     * RESET# reboots, both on request. */
    if (!conn && !cing && (now - lconn) >= CONN_RETRY_SEC) {   /* (re)connect, non-blocking, paced */
        lconn = now;
        if (fd >= 0) { cl(fd); fd = -1; }
        fd = so(2, g_fwd_udp ? 2 : 1, 0);      /* SOCK_DGRAM for UDP uplink, else SOCK_STREAM */
        if (fd >= 0 && fd < 256) {
            fcntl_fn fcn = *(fcntl_fn *)SLOT_FCNTL; if (fcn) fcn(fd, F_SETFL_NB, 1);
            unsigned char sa[24]; fill_server_addr(sa, g_fwd_ip, g_fwd_port);
            int cr = co(fd, sa, 24);            /* UDP: sets the peer, returns 0 (no handshake). TCP: async */
            if (g_fwd_udp) {                    /* connected UDP is up immediately — no SYN to resolve */
                if (cr != 0) { cl(fd); fd = -1; }   /* udp_connect failed (route/bind) -> paced retry */
                else {
                    fcntl_fn fcn2 = *(fcntl_fn *)SLOT_FCNTL; if (fcn2) fcn2(fd, F_SETFL_NB, g_fwd_nb ? 1 : 0);
                    conn = 1; g_fwd_head = g_fwd_tail = 0; g_fwd_active = 1; last_tx = now; bad_active = 0; g_fwd_conns++;
                    vputs("FWD: udp bound\r\n");
                }
            } else { cing = 1; cstart = now; }
        } else if (fd >= 256) { cl(fd); fd = -1; }
    }
    if (cing && fd >= 0 && fd < 256) {         /* resolve the pending connect (never blocks) */
        select_fn sel = *(select_fn *)SLOT_SELECT;
        unsigned char ws[32], es[32];
        for (int i = 0; i < 32; i++) { ws[i] = 0; es[i] = 0; }
        ws[fd >> 3] |= (unsigned char)(1u << (fd & 7));
        es[fd >> 3] |= (unsigned char)(1u << (fd & 7));
        int tv[2] = {0, 0};
        int nr = sel(fd + 1, 0, ws, es, tv);
        int up = 0, fail = 0;
        if (nr > 0 && (es[fd >> 3] & (1u << (fd & 7)))) fail = 1;
        else if (nr > 0 && (ws[fd >> 3] & (1u << (fd & 7)))) up = 1;
        if (!up && !fail && (now - cstart) >= CONN_TIMEOUT_SEC) fail = 1;
        if (fail) { cl(fd); fd = -1; cing = 0; }
        else if (up) {
            /* Revert to BLOCKING for data unless FWDNB=1. Non-blocking looked safer (never stall the
             * loop) but on this resident send() then returned a positive count while NOTHING was ever
             * transmitted (peer saw 0 payload bytes across 100+ connections while our tx counter passed
             * 500 KB). The GT06 socket reverts to blocking and transmits fine over the same bearer. */
            { fcntl_fn fcn2 = *(fcntl_fn *)SLOT_FCNTL; if (fcn2) fcn2(fd, F_SETFL_NB, g_fwd_nb ? 1 : 0); }
            cing = 0; conn = 1;
            g_fwd_head = g_fwd_tail = 0;        /* FLUSH stale USB->TCP data on (re)connect */
            g_fwd_active = 1; last_tx = now; g_fwd_conns++;
            vputs("FWD: connected\r\n");
        }
    }
    if (!conn) return;

    if (g_fwd_udp) {
        /* ---- UP: RIDE OUT stalls, do not discard through them ----
         * This used to enforce "no buildup": anything still queued after the per-pass cap — or after a
         * single not-writable pass — was discarded outright, on the theory that stale telemetry is
         * worthless. Measured on the aircraft 2026-07-25 that was catastrophic: the uplink stalls for
         * ~2.2 s at a time (11 stalls, 54% of a 45 s window), and each stall threw away the whole backlog,
         * costing ~90% of the telemetry. At 4.5 KB/s a 2.2 s stall is only ~10 KB and the ring is 32 KB —
         * we were discarding data we had ample room to hold. 2 s late and complete beats on time and 90%
         * missing, and the discard was byte-granular so it truncated MAVLink frames as well as losing them.
         * So: let the ring absorb a stall and drain when the socket comes back. Drop only when the backlog
         * is genuinely too big or too old, oldest-first, and COUNT it — the old discard was invisible in
         * every counter, which is why dp0/dd0 looked healthy while most of the link was being thrown away. */
        int failed = 0, progressed = 0;
        if (g_fwd_head != g_fwd_tail) {
            int guard = 0;
            while (g_fwd_head != g_fwd_tail && guard < FWD_UDP_MAX_DGRAM) {
                if (!sock_writable(fd)) { failed = 1; g_fwd_nowr++; break; }
                unsigned idx = g_fwd_tail & (FWD_RING_SZ - 1);
                unsigned toend = FWD_RING_SZ - idx;
                unsigned avail = g_fwd_head - g_fwd_tail;
                unsigned n = avail < toend ? avail : toend;
                if (n > 256) n = 256;                    /* <=256 B per UDP datagram (split larger) */
                int w = se(fd, g_fwd_ring + idx, (int)n, 0);
                if (w > 0) { g_fwd_tail += (unsigned)w; g_fwd_tx_bytes += (unsigned)w; bad_active = 0; guard++;
                             last_up = now; progressed = 1; }
                else { failed = 1; g_fwd_nowr++; break; }  /* send failed */
            }
            /* backlog_since measures how long we have been STALLED — i.e. carrying data while making no
             * progress. It must be cleared by progress, not by the ring happening to hit empty: on a busy
             * link the ring is never exactly empty, so an "age since last empty" bound fires on a perfectly
             * healthy link and sheds everything every pass. (It did exactly that; the SITL burst test
             * caught it.) */
            if (progressed || g_fwd_head == g_fwd_tail) backlog_since = 0;
            else if (!backlog_since) backlog_since = now ? now : 1;
            if (g_fwd_head != g_fwd_tail) {
                unsigned back = g_fwd_head - g_fwd_tail;
                unsigned age  = backlog_since ? (unsigned)(now - backlog_since) : 0;
                if (back > FWD_UP_MAX_BACKLOG) {          /* shed OLDEST down to the bound */
                    unsigned shed = back - FWD_UP_MAX_BACKLOG;
                    g_fwd_tail += shed; g_fwd_up_drop += shed;
                } else if (age >= FWD_UP_MAX_AGE_SEC) {   /* stalled this long -> the data is stale */
                    g_fwd_up_drop += back; g_fwd_tail = g_fwd_head; backlog_since = 0;
                }
            }
        } else {
            backlog_since = 0;
        }
        /* KEEPALIVE. The ground proxy learns our source tuple from our uplink and forgets it after ~10 s of
         * silence; with no tuple it has nowhere to send the down direction, so a quiet uplink kills
         * COMMANDING, not just telemetry (measured: ~50 s dead out of every 60). One 0x00 byte per
         * FWD_KA_SEC of uplink idle keeps the mapping alive; 0x00 is not a MAVLink start byte so the far
         * end discards it without disturbing a frame. Only when nothing else went up this interval.
         * The attempt is timestamped whether or not it succeeds, so a failing socket cannot turn this into
         * a 50 Hz retry loop against a blocking send(). Its OUTCOME feeds the same recovery path as a
         * payload send: on a quiet link the keepalive is the only uplink traffic, so if it fails the socket
         * must be recreated, and if it succeeds that is proof the socket is alive and clears bad_active. */
        if (!failed && (unsigned)(now - last_ka) >= FWD_KA_SEC && (unsigned)(now - last_up) >= FWD_KA_SEC) {
            unsigned char z = 0;
            last_ka = now;
            if (sock_writable(fd) && se(fd, &z, 1, 0) > 0) { last_up = now; g_fwd_ka++; bad_active = 0; }
            else { failed = 1; g_fwd_nowr++; }
        }
        {   /* DOWN: drain the UDP recv queue so it can't build up, but do AT MOST FWD_UDP_MAX_DGRAM if04
             * writes per pass — a resident VCOM write can block ~250 ms, so 64 back-to-back would trip the
             * 12 s WD; capping at 4 bounds the worst case to ~1 s while still passing 200 datagrams/s down.
             * Datagrams past the cap are drained from the queue and dropped (no buildup). */
            select_fn sel = *(select_fn *)SLOT_SELECT;
            int guard = 0, wrote = 0;
            while (guard++ < 64) {
                /* STOP at the write cap instead of draining-and-discarding. The down direction carries
                 * COMMANDS: leaving a datagram in the socket queue costs 20 ms of latency, discarding it
                 * costs the command outright. The socket queue is the backpressure. */
                if (wrote >= FWD_USB_MAX_WR) break;
                if (!usb_pool_ok(now)) break;      /* dTD shortage: leave it queued, do not starve RX */
                unsigned char rd[32];
                for (int i = 0; i < 32; i++) rd[i] = 0;
                rd[fd >> 3] |= (unsigned char)(1u << (fd & 7));
                int tv[2] = {0, 0};
                int nr = sel(fd + 1, rd, 0, 0, tv);
                if (!(nr > 0 && (rd[fd >> 3] & (1u << (fd & 7))))) break;   /* nothing more queued */
                /* STATIC and oversized: recv() DISCARDS the remainder of a datagram that does not fit,
                 * which silently truncates the stream and corrupts a MAVLink frame. Nothing bounds how
                 * much the far end may aggregate into one datagram, so size for well beyond any plausible
                 * aggregate and count anything that still fills it rather than losing the tail unseen. */
                static unsigned char rx[4096];
                int n = rv(fd, rx, sizeof rx, 0);
                if (n > 0) {
                    g_fwd_dn_pkts++; g_fwd_dn_bytes += (unsigned)n;
                    if ((unsigned)n >= sizeof rx) g_fwd_dn_trunc++;   /* may have lost a tail */
                    usb_tx(rx, n); wrote++;
                }
                else if (n < 0) break;                                     /* EWOULDBLOCK; n==0 keep draining */
            }
        }
        /* Recreate only on a pass that ACTUALLY saw a failure (not during idle — an idle recreate would
         * mint a new source port the proxy hasn't learned yet, killing downlink until the next up-byte),
         * and only after failures have persisted FWD_TX_STALL_SEC. A successful send cleared bad_active. */
        if (failed) {
            if (!bad_active) { bad_active = 1; bad_since = now; }
            if ((unsigned)(now - bad_since) >= FWD_TX_STALL_SEC) { cl(fd); fd = -1; conn = 0; g_fwd_active = 0; }
        }
        return;
    }

    /* USB->TCP: drain the ring (non-blocking, bounded). Set the stall baseline BEFORE draining: if the ring
     * is empty now we're not stalled, and a producer that publishes mid-pass can't be judged "stuck" against
     * a stale last_tx (that race would close a live connection on the first byte). */
    if (g_fwd_head == g_fwd_tail) last_tx = now;
    /* Select for WRITABILITY before sending, exactly as we already guard recv. This resident's socket
     * return codes are not trustworthy (MSG_DONTWAIT / SO_RCVTIMEO are documented as not honored), and
     * send() was observed returning positive counts totalling >500 KB while the peer received ZERO bytes.
     * Asking select whether the stack actually considers the socket writable is the reliable test, and
     * g_fwd_nowr makes "never writable" visible instead of silently counted as sent. */
    if (g_fwd_head != g_fwd_tail) {
        select_fn selw = *(select_fn *)SLOT_SELECT;
        unsigned char ws[32];
        for (int i = 0; i < 32; i++) ws[i] = 0;
        ws[fd >> 3] |= (unsigned char)(1u << (fd & 7));
        int tvw[2] = {0, 0};
        int wr = selw(fd + 1, 0, ws, 0, tvw);
        if (!(wr > 0 && (ws[fd >> 3] & (1u << (fd & 7))))) { g_fwd_nowr++; goto fwd_rx; }
    }
    unsigned avail; int guard = 0;
    while ((avail = g_fwd_head - g_fwd_tail) != 0 && guard++ < 64) {
        unsigned idx = g_fwd_tail & (FWD_RING_SZ - 1);
        unsigned toend = FWD_RING_SZ - idx;
        unsigned n = avail < toend ? avail : toend;
        if (n > 512) n = 512;      /* small chunks: a blocking send must not stall the loop */
        int w = se(fd, g_fwd_ring + idx, (int)n, 0);
        if (w > 0) { g_fwd_tail += (unsigned)w; last_tx = now; g_fwd_tx_bytes += (unsigned)w; }
        else { g_fwd_tx_fail++; break; }        /* would-block: retry next pass */
    }
fwd_rx:
    /* Bounded no-progress recovery: data queued but NO successful TX for FWD_TX_STALL_SEC -> recreate the
     * socket. Covers a half-open TCP socket AND a broken UDP fd (failed udp_connect route/bind, or a fd
     * that can no longer send after bearer disruption). Baseline last_tx is reset above when the ring is
     * empty, so an idle up-path never trips this — only a genuine stuck backlog does. */
    if (g_fwd_head != g_fwd_tail && (now - last_tx) >= FWD_TX_STALL_SEC) {
        cl(fd); fd = -1; conn = 0; g_fwd_active = 0;
        return;
    }
    {                                          /* TCP->USB: guarded recv -> if04 */
        select_fn sel = *(select_fn *)SLOT_SELECT;
        unsigned char rd[32];
        for (int i = 0; i < 32; i++) rd[i] = 0;
        rd[fd >> 3] |= (unsigned char)(1u << (fd & 7));
        int tv[2] = {0, 0};
        int nr = sel(fd + 1, rd, 0, 0, tv);
        if (nr > 0 && (rd[fd >> 3] & (1u << (fd & 7)))) {
            static unsigned char rx[4096];
            int n = rv(fd, rx, sizeof rx, 0);
            /* TCP: select said readable, so recv >0 = data, 0 = peer closed, <0 = real error -> drop.
             * UDP: there is NO peer-close — recv 0 is a zero-length datagram and <0 is EWOULDBLOCK, so a
             * <=0 must NOT tear the socket down; only forward n>0. */
            if (n > 0) {
                g_fwd_dn_pkts++; g_fwd_dn_bytes += (unsigned)n;
                if (usb_pool_ok(now)) usb_tx(rx, n);       /* keep the RX repost's dTD free */
            }
            else if (!g_fwd_udp) { cl(fd); fd = -1; conn = 0; g_fwd_active = 0; }
        }
    }
}

void standalone_main(u32 loader_resolver)
{
    sapi_table_fill(loader_resolver);
#ifdef SITL
    { extern int sitl_vcom_write(const void *, int); WR = (vcom_write_fn)sitl_vcom_write; }
#else
    WR = (vcom_write_fn)RES_USB_VCOM_WRITE;
#endif
    /* Claim if04 for forwarding (if configured) BEFORE any debug output, so a configured UAV data link
     * never sees watchdog/GT06 debug markers - not even during boot or a FWD-socket outage. */
    if (fwd_load()) g_fwd_en = (g_fwd_port > 0) ? 1 : 0;
    vputs("S3c: GNSS -> LOC tracker\r\n");

    /* Arm the resident network-status subsystem once at boot (item 3/5): creates the IPC handle that
     * gates get_csq()/get_reg_stat() — without it both return error. Vendor does this at network init
     * (its 0x7e1b6498). No-arg; safe to call before the modem attaches (the ready flag comes later). */
    { netstat_start_fn ns = *(netstat_start_fn *)SLOT_NETSTAT_START; if (ns) ns(); }



    /* Reliability: log why we last rebooted. WD_SELFTEST arms + validates the auto-reset (see top of file);
     * a normal build passes arm=0 (proven FS reset-reason path only). */
    int wd_reason = reliability_init(WR, (WD_SELFTEST || WD_ARM || (WD_HW_ARM && !WD_HW_SELFTEST)) ? 1 : 0);
    (void)wd_reason;
#if WD_SELFTEST
    vhex("WD:selftest_reason", (unsigned)wd_reason);
    if (wd_reason == RR_ST_HUNG) {
        /* We marked ourselves about-to-hang last boot and came back -> something reset us while armed+hung;
         * the capture shows WD:FIRE just before it = the watchdog. Quiet it and run normally (no boot-loop:
         * this gate uses the FS reason the MAIN task wrote, not the untested FS-from-timer-callback path). */
        vputs("WD:SELFTEST_PASS\r\n");
        wd_arm(0);
    } else {
        vputs("WD:SELFTEST_HANG\r\n");
        wd_set_reason(RR_ST_HUNG);         /* main-task FS write (proven) -> survives the reset -> PASS next boot */
        for (int i = 0; i < 6; i++) { wd_alive(); wd_sleep(500); }   /* ~3s of healthy liveness (WD:alive) */
        vputs("WD:HANG_NOW\r\n");
        for (;;) wd_sleep(1000);           /* stop feeding -> wd_tick must fire sys_reset (~WD_STALL ticks) */
    }
#endif

#if WD_HW_SELFTEST
    /* HARDWARE-watchdog self-test: arm the MMP WDT and DON'T feed it (SW watchdog is off, arm=0). The SoC
     * should hard-reset ~timeout later. Gated by RR_ST_HUNG (main-task FS write) so it's boot-loop-safe. */
    vhex("WD:hw_selftest_reason", (unsigned)wd_reason);
    vhex("WD:hw_wsr", hwwd_status());       /* !=0 confirms the last reset was the HW WDT */
    if (wd_reason == RR_ST_HUNG) {
        vputs("WD:HWWD_SELFTEST_PASS\r\n");  /* armed HW + no feed + we came back => HW WDT reset the SoC */
    } else {
        vputs("WD:HWWD_ARM_NOFEED\r\n");
        wd_set_reason(RR_ST_HUNG);           /* survives the reset -> PASS next boot */
        hwwd_enable(4);                       /* 4s HW WDT; nothing feeds it */
        vputs("WD:HW_WAIT_RESET\r\n");
        for (;;) wd_sleep(1000);              /* wait -> HW WDT hard-resets the SoC in ~4s */
    }
#endif
#if WD_HW_ARM && !WD_HW_SELFTEST && !WD_HW_DIAG
    wd_hw_arm(WD_HW_SECS);                    /* production: enable HW WDT, wd_tick feeds it */
#endif
#if WD_HW_DIAG
    /* Diagnostic: the FIXED arm (APRR route + WDT-block, NO WDTPCR), no feed, then run normally. Watch
     * if04: does the main loop keep producing output (no freeze)? do wvr1/wvr2 differ (WDT clock on)? does
     * the SoC reset ~WD_HW_DIAG_SECS later (routed)? */
    vhex("WD:hw_wsr", hwwd_status());
    hwwd_enable(WD_HW_DIAG_SECS);
    vputs("WD:HW_DIAG_ARMED\r\n");
    vhex("WD:wvr1", hwwd_wvr());
    busy_delay(200000000);                    /* ~1s */
    vhex("WD:wvr2", hwwd_wvr());              /* wvr2 > wvr1 => WDT clock is running */
    for (int i = 0; i < 22; i++) busy_delay(200000000);   /* ~22s, past the 15s timeout */
    vhex("WD:wvr3", hwwd_wvr());              /* counter after ~23s: reached WMR(=secs*256)? wrapped? reset? */
    vhex("WD:wsr_after", hwwd_status());      /* WSR set => match happened but not routed to reset */
    vputs("WD:DIAG_DONE\r\n");                /* if we print this, no reset occurred */
#endif

    socket_fn  so = *(socket_fn  *)SLOT_SOCKET;
    connect_fn co = *(connect_fn *)SLOT_CONNECT;
    g_send_raw     = *(send_fn    *)SLOT_SEND;
    send_fn    se = send_traced;
    recv_fn    rv = *(recv_fn    *)SLOT_RECV;
    close_fn   cl = *(close_fn   *)SLOT_CLOSE;

    /* ---- URC queue (independent of the network). GNSS is powered ON/OFF by the state machine
     * (apply_gps) — NOT here — because default is IDLE (GPS off). ---- */
    unsigned qid = 0;
    static const char qname[] = "URC QUEUE";
    (*(msgqc_fn *)SLOT_MSGQ_CREATE)(&qid, qname, 16, 50, 11);
    (*(attach_fn *)SLOT_URC_ATTACH)(qid, -1);
    msgqr_fn mqr = *(msgqr_fn *)SLOT_MSGQ_RECV;
    vhex("S3c:qid", qid);


    /* ---- read the vendor config from the resident FS (c:/cwprm.bin, backup cwbank.bin) for SEAMLESS
     * migration: login with the provisioned TERIID + connect to the config's server/port. Falls back to
     * the real modem IMEI (un-provisioned/new units) and a default server if the config is unreadable. ---- */
    static unsigned char cfg[0x800];
    fopen_fn  fopen_  = *(fopen_fn  *)SLOT_FOPEN;
    fread_fn  fread_  = *(fread_fn  *)SLOT_FREAD;
    fclose_fn fclose_ = *(fclose_fn *)SLOT_FCLOSE;
    static const char f_prm[] = "c:/cwprm.bin", f_bank[] = "c:/cwbank.bin", f_rb[] = "rb";
    FILE_t fp = fopen_(f_prm, f_rb);
    if (!fp) fp = fopen_(f_bank, f_rb);
    unsigned fret = 0;
    if (fp) { fret = fread_(cfg, 0x800, 1, fp); fclose_(fp); }
    /* Validate by CONTENT, not the fread return (this FS's fread returns bytes, not element-count):
     * the config is good if the TERIID field holds ASCII digits. */
    int have = (fp != 0) && (cfg[CFG_OFF_TERIID] >= '0' && cfg[CFG_OFF_TERIID] <= '9');
    vhex("S4:cfg_fp", (unsigned)(unsigned long)fp);   /* nonzero -> resident FS works */
    vhex("S4:cfg_fret", fret);
    vhex("S4:cfg_ok", (unsigned)have);

    char login_id[16];
    if (have) {
        for (int i = 0; i < 15; i++) login_id[i] = cfg[CFG_OFF_TERIID + i];
        login_id[15] = 0;
    } else {                                          /* un-provisioned: real modem IMEI */
        (*(imei_fn *)SLOT_IMEI)(login_id);
        if (!(login_id[0] >= '0' && login_id[0] <= '9')) {
            static const char fallback[] = "356823030000001";
            for (int i = 0; i < 16; i++) login_id[i] = fallback[i];
        }
    }
    unsigned char srv_ip[4] = {103, 230, 158, 49};
    unsigned port = 8711;
    if (have) {
        parse_ip((const char *)(cfg + CFG_OFF_IP), srv_ip);   /* keep default if not dotted-quad */
        port = (cfg[CFG_OFF_PORT] << 8) | cfg[CFG_OFF_PORT + 1];
    }
    /* FORCE_TEST_SERVER: bench override — connect to the LOCAL test server (103.230.158.49:8711) instead
     * of the cwprm.bin server (:7711 = production), while still logging in with the unit's real TERIID.
     * Set 0 for a production build (honour cwprm.bin). */
#ifdef SITL
#define FORCE_TEST_SERVER 0     /* SITL honours the (mock) cwprm config: env SITL_SERVER sets the server */
#else
#define FORCE_TEST_SERVER 0
#endif
#if FORCE_TEST_SERVER
    srv_ip[0] = 103; srv_ip[1] = 230; srv_ip[2] = 158; srv_ip[3] = 49; port = 8711;
#endif
    vdump("S4:loginid", (const unsigned char *)login_id, 15);
    vhex("S4:port", port);

    /* cache identity/server/APN for the cxzt# reply builder */
    for (int i = 0; i < 16; i++) g_login_id_s[i] = login_id[i];
    for (int i = 0; i < 4; i++) g_srv_ip_s[i] = srv_ip[i];
    g_port_s = port;
    { const char *a = (const char *)(cfg + CFG_OFF_APN); int i = 0;
      for (; i < 47 && a[i] >= 0x20 && a[i] < 0x7f; i++) { g_apn_s[i] = a[i]; }
      g_apn_s[i] = 0; }

    /* USB<->TCP forwarding bridge: register the if04 RX callback (FWDSERVER/FWDPORT already loaded early,
     * before boot debug, so if04 ownership is claimed from the first byte). */
    usb_rx_start();

    unsigned char login_frame[24];
    int login_len = build_login(login_id, 1, login_frame);

    /* ---- state machine: default IDLE (GPS off, no LOC, heartbeat @T2); the server's TIMER,T1,T2
     * command flips TRACKING (T1<=5: GPS on, LOC @T1) vs IDLE (T1>5). Never uses the accelerometer. ---- */
    int track = 0;                                 /* 0 = idle (default), 1 = tracking */
    int t1 = 60, t2 = 1800;                         /* default idle cadence (server _desired_settings idle) */
    apply_gps(track);                              /* boot: ensure GPS is OFF while idle */
    leds_init();                                   /* item 14: configure the two status LEDs as outputs, off */
#if BATT_MCU
    batt_mcu_init();                               /* bring up UART1 to the supervisor MCU for battery mV */
    /* Prime the reading BEFORE the first login/cxzt. Without this g_batt_mv stays 0 until the first 5 s
     * poll, and cxzt reports "BT:0" -> the server renders a bogus 0% flat battery on every boot. The M0
     * answers over UART1 in a few ms; bail out after ~500 ms so a mute supervisor can't delay boot. */
    batt_mcu_request();
    for (int i = 0; i < 20 && !g_batt_mv; i++) { wd_sleep(25); batt_mcu_parse(); }
    vhex("BT:boot_mv", g_batt_mv);
#endif

    int fd = -1, connected = 0, connecting = 0;
    unsigned short serial = 2;
    unsigned char ld[18], loc[40];
    static char g_line[256]; static int g_llen;    /* NMEA line reassembly across URC chunks */
    static unsigned char acc[768]; static int acclen; /* server->device RX accumulator (>= max frame + one recv) */
    unsigned last_hbt = 0, last_loc = 0, last_csq = 0, last_batt = 0, last_blind = 0, last_sos = 0;
    unsigned last_gps_power = 0;
    unsigned conn_start = 0, last_conn = 0u - (unsigned)CONN_RETRY_SEC;  /* first attempt immediately */

    for (;;) {
        wd_alive();                                /* liveness heartbeat for the software watchdog */
        unsigned now = wd_uptime();
        leds_update(track, connected, g_sos_active, now);   /* item 14: status LEDs each pass */

        if (g_srv_req) {                           /* SZCS#SERVIP/SERVPORT wrote a new server -> apply live */
            for (int k = 0; k < 4; k++) srv_ip[k] = g_srv_new_ip[k];
            port = g_srv_new_port; g_srv_req = 0;
            connected = 0; connecting = 0;          /* drop + reconnect to the new server via the block below */
            vdump("SRV:new_ip", srv_ip, 4); vhex("SRV:new_port", port);
        }

#if BATT_MCU
        /* Supervisor query: pick up any pending 0x82 reply each cycle, and re-query every BATT_POLL_SEC.
         * The reply carries battery mV + the SOS button (item 17), so poll fast enough that an SOS press
         * is detected promptly (5 s, vs the old 15 s battery-only cadence). Independent of the network so
         * it works on the bench. "BT:mv" echoes the last reading on if04. */
#define BATT_POLL_SEC 5
        batt_mcu_parse();
        if ((now - last_batt) >= BATT_POLL_SEC) {
            /* item 15: charging indicator — resident charger_state() (the vendor heartbeat bit2 source;
             * SoC/PMIC side, NOT the M0). 1 while the charger/VBUS is present. */
            charger_state_fn chg = *(charger_state_fn *)SLOT_CHARGER_STATE;
            g_charging = (chg && chg() == 1) ? 1 : 0;
            vhex("BT:mv", g_batt_mv); vhex("CHG", g_charging); vhex("URC:n", g_urc_n);
            if (g_nmea_bad) vhex("NMEA:bad", g_nmea_bad);
            if (g_nmea_jump) vhex("NMEA:jump", g_nmea_jump);
            batt_mcu_request(); last_batt = now;
        }
#endif
#if FOTA
        if (g_fota_req) fota_run();     /* a YCSJ was queued -> download + stage the update (reboots on done) */
#endif
        if (g_usbrst_req) {          /* USB off/on: re-arms a stalled resident Modem1 RX pipeline. The host
                                      * must reopen the tty afterwards (the device re-enumerates). */
            g_usbrst_req = 0;
            usb_switch_fn sw = *(usb_switch_fn *)SLOT_USB_SWITCH;
            if (sw) { sw(0); wd_sleep(1500); sw(1); }
            wd_sleep(500);
            usb_rx_start();          /* re-register after re-enumeration */
            vputs("USB: switch reset\r\n");
        }
        if (g_reset_req) {              /* RESET# (item 11): let the ACK flush, then reboot via sys_reset */
            vputs("RESET: rebooting\r\n");
            wd_sleep(1500);             /* ~1.5 s for the TCP ACK to go out (well under the 12-tick WD) */
            sys_reset_fn rst = *(sys_reset_fn *)SLOT_SYS_RESET;
            if (rst) rst();
            g_reset_req = 0;            /* unreached if the reset takes; clears the flag if the slot is null */
        }
        cell_recovery(now, connected);  /* item 16: un-wedge a camped-but-denied modem (timer-gated) */

        /* (Re)connect + log in — NON-BLOCKING (item 12 follow-up). A blocking connect() stalls this
         * loop for the whole TCP timeout when the server is unreachable (radio up, route/server dead),
         * starving the 1 Hz blind-buffer capture. So: set the socket non-blocking, start the connect,
         * and poll it below with a {0,0} select each loop — capture keeps running while the SYN is out.
         * lwIP semantics: non-blocking connect returns "in progress"; completion = socket WRITABLE,
         * failure = EXCEPT set (errevent). A fast-failed connect that raises neither is caught by the
         * CONN_TIMEOUT_SEC deadline. */
        if (!connected && !connecting && (now - last_conn) >= CONN_RETRY_SEC) {
            last_conn = now;
            vhex("S4:cfg_ok", (unsigned)have);     /* echo the config summary each retry (survives the */
            vdump("S4:loginid", (const unsigned char *)login_id, 15);  /* USB-enum race that eats the  */
            vhex("S4:port", port);                 /* one-shot boot prints); stops once connected */
            g_wd_phase = 'C';
            if (fd >= 0) { cl(fd); fd = -1; }
            acclen = 0;
            fd = so(2, 1, 0);
            if (fd >= 0) {
                fcntl_fn fcn = *(fcntl_fn *)SLOT_FCNTL;
                if (fcn) fcn(fd, F_SETFL_NB, 1);   /* missing slot => blocking connect, as before */
                unsigned char sa[24];
                fill_server_addr(sa, srv_ip, port);
                co(fd, sa, 24);                    /* 0 = up already; else in-progress OR failed — the
                                                    * select() poll below resolves both the same way */
                connecting = 1; conn_start = now;
            }
            g_wd_phase = 0;
        }
        if (connecting && fd >= 0 && fd < 256) {   /* poll the pending connect (never blocks) */
            select_fn sel = *(select_fn *)SLOT_SELECT;
            unsigned char ws[32], es[32];
            for (int i = 0; i < 32; i++) { ws[i] = 0; es[i] = 0; }
            ws[fd >> 3] |= (unsigned char)(1u << (fd & 7));
            es[fd >> 3] |= (unsigned char)(1u << (fd & 7));
            int tv[2] = {0, 0};
            int nr = sel(fd + 1, 0, ws, es, tv);
            int up = 0, fail = 0;
            if (nr > 0 && (es[fd >> 3] & (1u << (fd & 7)))) fail = 1;      /* lwIP errevent */
            else if (nr > 0 && (ws[fd >> 3] & (1u << (fd & 7)))) up = 1;   /* writable = connected */
            if (!up && !fail && (now - conn_start) >= CONN_TIMEOUT_SEC) fail = 1;
            if (fail) {
                cl(fd); fd = -1; connecting = 0;   /* paced retry (CONN_RETRY_SEC) */
                vputs("S4: connect retry\r\n");
            } else if (up) {
                connecting = 0;
                fcntl_fn fcn = *(fcntl_fn *)SLOT_FCNTL;
                if (fcn) fcn(fd, F_SETFL_NB, 0);   /* back to blocking: a short send() means "drop" */
                if (se(fd, login_frame, login_len, 0) != login_len) {
                    cl(fd); fd = -1;               /* login didn't go out whole -> paced retry */
                } else {
                    connected = 1;
                    /* recv() is guarded by a select() poll in the loop below (this resident ignores
                     * SO_RCVTIMEO, so an unguarded recv blocks ~5s). No socket-option setup needed. */
                    last_hbt = now - (unsigned)t2;    /* emit one heartbeat right after connect (liveness) */
                    last_loc = now;                   /* but don't blast LOC immediately */
                    csq_refresh();                    /* refresh signal for the first heartbeat */
                    if (g_sos_active) g_sos_sends = 0;  /* re-deliver a live SOS to the (re)established session */
                    vputs("S4: connected\r\n");
                }
            }
        }

        /* drain server->device commands: select()-poll first so recv() runs ONLY when data is ready — the
         * resident ignores SO_RCVTIMEO, so an unguarded recv would block ~5s and stall the loop/commands.
         * The mqr() wait below paces the loop (~1 Hz), so a {0,0} non-blocking poll here is enough. */
        if (connected && fd >= 0 && fd < 256) {
            select_fn sel = *(select_fn *)SLOT_SELECT;
            unsigned char rdset[32];
            for (int i = 0; i < 32; i++) rdset[i] = 0;
            rdset[fd >> 3] |= (unsigned char)(1u << (fd & 7));
            int tv[2] = {0, 0};                    /* {tv_sec=0, tv_usec=0} = non-blocking poll */
            int nr = sel(fd + 1, rdset, 0, 0, tv);
            if (nr > 0 && (rdset[fd >> 3] & (1u << (fd & 7)))) {
                unsigned char rx[256];
                g_wd_phase = 'R';
                int n = rv(fd, rx, sizeof rx, 0);  /* data ready -> returns immediately, no block */
                g_wd_phase = 0;
                if (n == 0) { connected = 0; }     /* peer closed */
                else if (n > 0) {
                    int changed = 0;
                    handle_rx(fd, se, acc, &acclen, (int)sizeof acc, rx, n, &track, &t1, &t2, &serial, &changed);
                    if (changed) {
                        apply_gps(track);
                        /* Don't let a partial batch straddle the transition: on the next tracking
                         * session it would be flushed with a stale base time, dropping old fixes into
                         * the new session's log and briefly showing an old position.
                         * SEND it rather than spilling to the blind ring — the server marks the
                         * connection idle before it sends TIMER, and idle positions skip daily
                         * logging, so anything replayed after this point is acknowledged and then
                         * dropped from the track. Sending now, while the server still counts us as
                         * active, is the only way those tail fixes survive. */
                        if (g_batch_n > 0 && connected) {
                            int blen = build_batch_data(g_batch_pay);
                            int flen = gt06_frame(0xF1, g_batch_pay, blen, serial++, g_batch_frame);
                            if (se(fd, g_batch_frame, flen, 0) != flen) {
                                connected = 0;
                                batch_spill_to_blind();
                            } else {
                                g_batch_n = 0;
                            }
                        } else if (g_batch_n > 0) {
                            batch_spill_to_blind();
                        }
                        g_last_batch = now;
                        last_gps_power = now;
                        g_llen = 0;
                        last_loc = now;
                    }
                }
            }
        }

        /* A successful call is not enough evidence that the resident GNSS service stayed healthy (G388920
         * stopped producing NMEA until a full reset). Reassert power + NMEA delivery every 10 s for the
         * entire tracking period, and check every return. This also recovers automatically from a transient
         * power-on/config failure without requiring another TIMER state transition.
         *
         * Idle is retried too, but only until the OFF is confirmed: a failed power-off (including the one
         * at boot) would otherwise leave GNSS drawing current for the whole idle period, since nothing
         * re-runs apply_gps until the next state change. Unlike the tracking case there is no reason to
         * keep reasserting once the resident has acknowledged OFF. The gate is g_gps_off_confirmed, NOT
         * g_gps_power_state: a failed power-on leaves the confirmed state at OFF, which would otherwise
         * suppress the retry for a later off that genuinely failed. */
        if (track && (now - last_gps_power) >= GPS_RETRY_SEC) {
            apply_gps(1);
            last_gps_power = now;
        } else if (!track && !g_gps_off_confirmed &&
                   (now - last_gps_power) >= GPS_RETRY_SEC) {
            apply_gps(0);
            last_gps_power = now;
        }

        /* Batched LOC flush: every g_loc_batch seconds while tracking, ship whatever fixes we collected.
         * Fewer than a full batch is fine (send what we have); ZERO fixes sends nothing at all, which is
         * exactly the existing no-fix behaviour. Cleared unconditionally so a stale partial batch can
         * never be emitted with the wrong base time after an outage. */
        gps_state_from_evidence(now);
        if (g_batch_rephase) { g_last_batch = now; g_batch_rephase = 0; }
        if (g_loc_batch && track && (now - g_last_batch) >= (unsigned)g_loc_batch) {
            g_last_batch = now;
            if (g_batch_n > 0) {
                if (connected) {
                    /* Drain any backlog first. Replay runs later in the loop than this flush, so a live
                     * batch could otherwise go out ahead of OLDER buffered fixes — leaving the jsonl
                     * out of order and rewinding the connection's last_ts on the server. Batched replay
                     * is fast enough (10 fixes/frame) that this costs little. */
                    if (g_blind_count > 0 && !blind_replay_batched(fd, se, &serial))
                        connected = 0;
                }
                if (connected) {
                    int blen = build_batch_data(g_batch_pay);
                    int flen = gt06_frame(0xF1, g_batch_pay, blen, serial++, g_batch_frame);
                    int sret = se(fd, g_batch_frame, flen, 0);
                    vhex("S3c:batch_send", (unsigned)g_batch_n);
                    if (sret != flen) {                /* partial send corrupts the frame */
                        connected = 0;
                        batch_spill_to_blind();        /* keep the fixes for replay */
                    } else {
                        g_batch_n = 0;
                    }
                } else {
                    batch_spill_to_blind();            /* link went down before the flush */
                }
            }
        }

        /* USB<->TCP forwarding bridge: keep the FWD socket up + pump both directions (independent of GT06).
         * g_fwd_req in the gate guarantees a disable/teardown pass runs even once the socket is already down. */
        if (g_fwd_en || g_fwd_active || g_fwd_req) fwd_poll(now, so, co, se, rv, cl);

        /* Poll signal periodically (reply lands on the URC queue below). */
        if (connected && (now - last_csq) >= 20) { csq_refresh(); last_csq = now; }

        /* Heartbeat (0x13) at the HBT,X,Y interval (X=ACC-ON/tracking, Y=ACC-OFF/idle; default 15 s) — the
         * server disconnects a unit it hears no heartbeat from, so idle must keep sending them (was wrongly
         * gated on TIMER T2 = up to 30 min). Carries battery LEVEL + signal; mV comes via STATUS#/cxzt#. */
        unsigned hbt_iv = track ? g_hbt_on : g_hbt_off;
        if (connected && (now - last_hbt) >= hbt_iv) {
            unsigned char hb[8];
            int hl = gt06_frame(0x13, hb, build_heartbeat(hb), serial++, loc);
            if (se(fd, loc, hl, 0) != hl) connected = 0;   /* partial/failed send -> drop + reconnect */
            last_hbt = now;
            vputs("ST: heartbeat\r\n");
        }

        /* SOS alarm (item 17, SAFETY): while an SOS is latched, send a burst of 0x16 alarms so one press
         * reliably reaches the server despite packet loss (the server sets a sticky assist on the first,
         * and re-sends the burst on each reconnect via the g_sos_sends reset above). Runs in ANY state —
         * an SOS in idle still fires; the server then exits idle + starts tracking via TIMER. Paced to one
         * send per second by an explicit gate — the loop runs faster than 1 Hz while forwarding is active. */
        if (connected && g_sos_active && g_sos_sends < SOS_BURST && (now - last_sos) >= 1) {
            unsigned char ad[40];
            int fl = gt06_frame(0x16, ad, build_alarm_data(ad), serial++, loc);
            if (se(fd, loc, fl, 0) == fl) { g_sos_sends++; last_sos = now; vputs("SOS: 0x16 alarm sent\r\n"); }
            else connected = 0;
        }

        /* Idle catch-up: fixes buffered at the tail of a tracking period (or across a TIMER idle flip)
         * would otherwise wait for the next RMC — which never comes in idle (GPS off). Drain them at
         * the same replay rate, paced to once per second by the 1 s idle wait below. */
        if (connected && !track && g_blind_count > 0 && (now - last_blind) >= 1) {
            last_blind = now;
            if (!blind_replay(fd, se, &serial)) connected = 0;
        }

        /* Wait for the next event:
         *  - tracking: NMEA arrives on the URC queue -> wait there (returns ~1 Hz on each sentence).
         *  - idle + connected: no NMEA, so wait ON THE SOCKET (select, 1 s) — a command (e.g. TIMER to
         *    start tracking) wakes us immediately instead of sitting out the URC timeout. select's timeout
         *    is well-defined ms; the mqr() timeout unit is not, and idled ~5 s (slow start/stop tracking).
         *  - reconnecting: pace on the URC wait. */
        /* While forwarding is active the bridge needs a fast pump for its ~8 KB/s link, so cap the wait at
         * FWD_PUMP_MS (~50 Hz) instead of ~1 s. GT06 work is all time-gated, so a faster loop is harmless. */
        unsigned msg[4] = {0, 0, 0, 0};
        g_wd_phase = 'Q';
        int fast = g_fwd_active;
        if (track || !connected || fd < 0 || fd >= 256) {
            if (qid) mqr(qid, msg, fast ? FWD_PUMP_MS : 1000);
        } else {
            select_fn selw = *(select_fn *)SLOT_SELECT;
            unsigned char rw[32];
            for (int i = 0; i < 32; i++) rw[i] = 0;
            rw[fd >> 3] |= (unsigned char)(1u << (fd & 7));
            int tvw[2]; tvw[0] = fast ? 0 : 1; tvw[1] = fast ? (FWD_PUMP_MS * 1000) : 0;   /* fast: FWD_PUMP_MS */
            selw(fd + 1, rw, 0, 0, tvw);
            /* select() only waits on the socket, so still drain the URC queue non-blocking (timeout 0 =
             * return at once) — else the CSQ replies polled in idle go stale and fill the 50-slot queue. */
            if (qid) mqr(qid, msg, 0);
        }
        g_wd_phase = 0;
        unsigned p = msg[3];
#ifdef SITL
        if (!p) continue;                          /* host URC pointers aren't in the resident VA range */
#else
        if (p < 0x7E000000u || p >= 0x81000000u) continue;   /* untrusted pointer: never READ — and
                                                              * never FREE (resident _free_r has only a
                                                              * NULL guard; freeing garbage corrupts the
                                                              * heap). p==0 (mqr timeout) lands here. */
#endif
        g_urc_n++;                                 /* URC:n printed each battery cycle — death at a
                                                    * repeatable COUNT (not wall time) = the resident
                                                    * URC payload lifetime (free path RE'd from vendor) */
        if (parse_at_urc((const char *)(unsigned long)p))
            goto urc_done;                         /* AT/modem text: NOT NMEA — feeding it to the line
                                                    * assembler corrupted split sentences (bad LOCs) */
        if (!track) goto urc_done;                 /* idle: ignore any stray NMEA, never build a LOC */

        /* The URC queue delivers NMEA in CHUNKS that cut mid-sentence, so reassemble complete lines
         * (split on CR/LF) before parsing — otherwise a sentence split inside the lon field parses wrong. */
        const char *s = (const char *)(unsigned long)p;
        for (int k = 0; k < 220 && s[k]; k++) {
            char ch = s[k];
            if (ch == '\r' || ch == '\n') {
                if (g_llen >= 8) {
                    g_line[g_llen] = 0;
                    if (!nmea_cksum_ok(g_line)) {  /* spliced/mangled line (AT interleave) -> drop, don't parse */
                        g_nmea_bad++; g_llen = 0; continue;
                    }
                    g_nmea_lines++; g_nmea_ts = now;
                    parse_gga(g_line);             /* cache sats-in-use / HDOP from any GGA line */
                    parse_gsa(g_line);             /* cache 2D/3D fix type from any GSA line */
                    rmc_t rc;
                    if (parse_rmc(g_line, &rc)) {
                        g_rmc_seen++;
                        if (rc.valid) g_rmc_valid++;
                        if (rc.valid && !fix_plausible(&rc, now))
                            rc.valid = 0;          /* impossible jump (checksum-colliding splice) -> drop */
                        vhex("S3c:valid", (unsigned)rc.valid);
                        vhex("S3c:lat", rc.lat_raw);
                        vhex("S3c:lon", rc.lon_raw);
                        if (rc.valid) {               /* cache the freshest fix for the SOS alarm (item 17) */
                            g_fix_lat = rc.lat_raw; g_fix_lon = rc.lon_raw;
                            g_fix_flags = (unsigned char)((rc.south ? 1 : 0) | (rc.west ? 2 : 0) | 4);
                            g_fix_sats = g_numsats;
                            for (int q = 0; q < 6; q++) g_fix_dt[q] = rc.dt[q];
                            g_have_fix = 1; g_fix_ts = now;   /* GPS-lock freshness for the status LEDs */
                        }
                        /* Batched path (0xF1): collect every valid fix; the flush below ships them as one
                         * frame every g_loc_batch seconds. Out of coverage we still use the blind buffer. */
                        if (g_loc_batch && rc.valid) {
                            if (connected) {
                                batch_push(&rc);
                            } else {
                                /* Link already down. Spill FIRST: several paths (peer close, heartbeat
                                 * or replay failure) clear `connected` without touching the batch, and
                                 * the periodic flush may be seconds away. Appending this newer fix to
                                 * the blind buffer ahead of the older batch would replay them out of
                                 * order. */
                                if (g_batch_n > 0) batch_spill_to_blind();
                                if (g_blind_en)
                                    blind_push_full(rc.lat_raw, rc.lon_raw, rc.dt, rc.south, rc.west, 1,
                                                    g_numsats, rc.ds, rc.spd_dkn, rc.hdg_deg);
                            }
                        }
                        /* one fix per T1-second period (GPS runs 1 Hz; T1>1 downsamples) */
                        if (!g_loc_batch && rc.valid && (now - last_loc) >= (unsigned)t1) {
                            last_loc = now;
                            if (connected) {
                                build_loc_data(ld, 0, rc.lat_raw, rc.lon_raw, rc.south, rc.west, 1, g_numsats, 0, 0);
                                int flen = gt06_frame(0x12, ld, 18, serial++, loc);
                                int sret = se(fd, loc, flen, 0);
                                vhex("S3c:loc_send", (unsigned)sret);
                                if (sret != flen) {         /* partial or failed send corrupts the frame -> */
                                    connected = 0;
                                    if (g_blind_en)
                                        blind_push(rc.lat_raw, rc.lon_raw, rc.dt, rc.south, rc.west, 1, g_numsats);
                                }
                            } else if (g_blind_en) {        /* out of coverage: buffer for later replay */
                                blind_push(rc.lat_raw, rc.lon_raw, rc.dt, rc.south, rc.west, 1, g_numsats);
                            }
                        }
                        /* Catch-up: once (re)connected, replay buffered fixes at BLIND_REPLAY_HZ. This runs
                         * once per ~1 Hz RMC, so N sends per RMC == N Hz. Each replayed LOC carries its OWN
                         * datetime (back-fills to real time, no bunching at reconnect). */
                        if (connected && g_blind_count > 0
                            && !(g_loc_batch ? blind_replay_batched(fd, se, &serial)
                                             : blind_replay(fd, se, &serial)))
                            connected = 0;                  /* send failed -> fixes stay buffered */
                    }
                }
                g_llen = 0;
            } else if (g_llen < (int)sizeof g_line - 1) {
                g_line[g_llen++] = ch;
            } else {
                g_llen = 0;                        /* overflow guard */
            }
        }
urc_done:
        urc_free(p);                               /* payload is dead after parsing; exactly once per envelope */
        msg[3] = 0;                                /* mirror vendor consumer's double-free guard */
    }
}
