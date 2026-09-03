/* usb_debug.c — dedicated W07C USB memory/API exploration firmware.
 *
 * This is deliberately a separate customer_app image from main.c.  It claims if04 and speaks the
 * fixed-size WDBG protocol documented in usb_debug.md.  Invalid memory reads and bad function
 * signatures can still fault the CPU; the host must set an explicit danger flag for either operation.
 */
#include "sapi.h"

#ifdef SITL
#include <sched.h>
extern int sitl_usb_data_write(const void *buf, int len);
#endif

typedef u32 (*resolver_fn)(u32 hash, u32 zero);
#ifdef SITL
#define IMAGE_END_ADDR 0x7E1B4000u
#define BSS_END_ADDR   0x7E1B8000u
#else
extern unsigned char __image_end, __bss_end;
#define IMAGE_END_ADDR ((u32)(unsigned long)&__image_end)
#define BSS_END_ADDR   ((u32)(unsigned long)&__bss_end)
#endif

#define DBG_VERSION       1u
#define DBG_REQ_SIZE      32u
#define DBG_MAX_PAYLOAD   512u
#define DBG_RX_RING       2048u

#define DBG_F_UNSAFE_READ 0x0001u
#define DBG_F_CALL_ACK    0x0002u

enum dbg_op {
    DBG_PING = 1,
    DBG_INFO = 2,
    DBG_READ = 3,
    DBG_RESOLVE = 4,
    DBG_CALL = 5,
    DBG_RESET = 6,
    DBG_SCRATCH_WRITE = 7,
    DBG_CALL8 = 8,
};

enum dbg_status {
    DBG_OK = 0,
    DBG_BAD_VERSION = 1,
    DBG_BAD_CRC = 2,
    DBG_BAD_OP = 3,
    DBG_BAD_ARG = 4,
    DBG_DENIED = 5,
    DBG_NO_SYMBOL = 6,
    DBG_UNSUPPORTED = 7,
};

static vcom_write_fn g_write;
static volatile unsigned g_rx_head, g_rx_tail;
static unsigned char g_rx_ring[DBG_RX_RING];
static unsigned char g_usb_record[1024];
static unsigned char g_reply[16 + DBG_MAX_PAYLOAD + 4];
static unsigned char g_read_data[DBG_MAX_PAYLOAD];
static unsigned char g_call_scratch[256];
static unsigned char g_call_result[4 + sizeof g_call_scratch];

static u32 get_le32(const unsigned char *p)
{
    return (u32)p[0] | ((u32)p[1] << 8) | ((u32)p[2] << 16) | ((u32)p[3] << 24);
}

static unsigned get_le16(const unsigned char *p)
{
    return (unsigned)p[0] | ((unsigned)p[1] << 8);
}

static void put_le32(unsigned char *p, u32 v)
{
    p[0] = (unsigned char)v; p[1] = (unsigned char)(v >> 8);
    p[2] = (unsigned char)(v >> 16); p[3] = (unsigned char)(v >> 24);
}

static void put_le16(unsigned char *p, unsigned v)
{
    p[0] = (unsigned char)v; p[1] = (unsigned char)(v >> 8);
}

static u32 crc32_update(u32 crc, const unsigned char *p, unsigned len)
{
    for (unsigned i = 0; i < len; i++) {
        crc ^= p[i];
        for (unsigned b = 0; b < 8; b++)
            crc = (crc & 1u) ? ((crc >> 1) ^ 0xEDB88320u) : (crc >> 1);
    }
    return crc;
}

static u32 crc32(const unsigned char *p, unsigned len)
{
    return crc32_update(0xFFFFFFFFu, p, len) ^ 0xFFFFFFFFu;
}

static void reply(unsigned op, unsigned status, u32 seq, const void *payload, unsigned len)
{
    if (len > DBG_MAX_PAYLOAD) { status = DBG_BAD_ARG; len = 0; }
    g_reply[0] = 'W'; g_reply[1] = 'D'; g_reply[2] = 'B'; g_reply[3] = 'R';
    g_reply[4] = DBG_VERSION; g_reply[5] = (unsigned char)op;
    put_le16(g_reply + 6, status);
    put_le32(g_reply + 8, seq);
    put_le32(g_reply + 12, len);
    if (payload && len) {
        const unsigned char *s = (const unsigned char *)payload;
        for (unsigned i = 0; i < len; i++) g_reply[16 + i] = s[i];
    }
    u32 c = crc32(g_reply, 16 + len);
    put_le32(g_reply + 16 + len, c);
    g_write(g_reply, (int)(20 + len));
}

/* The resident read ignores its length argument, hence one read into a 1 KiB static record exactly as
 * in the production firmware.  The callback only enqueues; potentially blocking USB replies happen in
 * the app's main context, never on the resident USB task's stack. */
static void usb_rx_cb(u32 pending)
{
    usb_read_fn rd = *(usb_read_fn *)SLOT_VCOM_READ;
    if (!rd || pending == 0 || pending > sizeof g_usb_record) return;
    int got = rd(g_usb_record, (int)pending);
    if (got <= 0 || (unsigned)got > sizeof g_usb_record) return;
    unsigned free = DBG_RX_RING - (g_rx_head - g_rx_tail);
    if ((unsigned)got > free) return;               /* discard the complete CDC record on overflow */
    for (int i = 0; i < got; i++)
        g_rx_ring[(g_rx_head + (unsigned)i) & (DBG_RX_RING - 1u)] = g_usb_record[i];
    __asm volatile ("" ::: "memory");
    g_rx_head += (unsigned)got;
}

static unsigned rx_peek(unsigned off)
{
    return g_rx_ring[(g_rx_tail + off) & (DBG_RX_RING - 1u)];
}

static void rx_drop(unsigned n)
{
    g_rx_tail += n;
}

static int range_inside(u32 addr, unsigned len, u32 lo, u32 hi)
{
    return addr >= lo && addr < hi && len <= hi - addr;
}

static int read_is_safe(u32 addr, unsigned len)
{
    u32 app_hi = BSS_END_ADDR;
    if (range_inside(addr, len, 0x7E1B0000u, app_hi)) return 1;
    /* Include the low slots as well as the nominal table.  This is the resolver-filled data table,
     * not resident code, and is the most useful crash-free API-discovery read. */
    return range_inside(addr, len, 0x7E1E1C00u, 0x7E1E2868u);
}

static u32 call_arg(u32 value)
{
    /* 0xffff0000..0xffff00ff denote scratch+offset. Keep the v1 0xffffffff token as scratch+0. */
    if (value == 0xFFFFFFFFu) return (u32)(unsigned long)g_call_scratch;
    if ((value & 0xFFFFFF00u) == 0xFFFF0000u)
        return (u32)(unsigned long)(g_call_scratch + (value & 0xFFu));
    return value;
}

static void finish_call(unsigned op, u32 seq, u32 ret)
{
    put_le32(g_call_result, ret);
    for (unsigned i = 0; i < sizeof g_call_scratch; i++)
        g_call_result[4 + i] = g_call_scratch[i];
    reply(op, DBG_OK, seq, g_call_result, sizeof g_call_result);
}

static void handle_request(const unsigned char req[DBG_REQ_SIZE])
{
    unsigned op = req[5], flags = get_le16(req + 6);
    u32 seq = get_le32(req + 8);
    u32 a0 = get_le32(req + 12), a1 = get_le32(req + 16);
    u32 a2 = get_le32(req + 20), a3 = get_le32(req + 24);

    if (req[4] != DBG_VERSION) { reply(op, DBG_BAD_VERSION, seq, 0, 0); return; }
    if (crc32(req, 28) != get_le32(req + 28)) { reply(op, DBG_BAD_CRC, seq, 0, 0); return; }

    if (op == DBG_PING) {
        static const char s[] = "W07C usbdbg/1 " GIT_HASH;
        reply(op, DBG_OK, seq, s, sizeof s - 1u);
        return;
    }
    if (op == DBG_INFO) {
        u32 info[14];
        info[0] = DBG_VERSION;
        info[1] = 0x7E1B0000u;
        info[2] = IMAGE_END_ADDR;
        info[3] = BSS_END_ADDR;
        info[4] = 0x7E1E1C00u;
        info[5] = 0x7E1E2868u;
        info[6] = (u32)(unsigned long)*(resolver_fn *)SL(RESOLVER_SLOT);
        info[7] = (u32)(unsigned long)*(vcom_write_fn *)SLOT_VCOM_WRITE;
        info[8] = (u32)(unsigned long)*(usb_read_fn *)SLOT_VCOM_READ;
        info[9] = (u32)(unsigned long)*(usb_regcb_fn *)SLOT_VCOM_REGCB;
        info[10] = (u32)(unsigned long)*(gnss1_fn *)SLOT_GNSS_POWER;
        info[11] = (u32)(unsigned long)g_call_scratch;
        info[12] = sizeof g_call_scratch;
        info[13] = DBG_MAX_PAYLOAD;
        reply(op, DBG_OK, seq, info, sizeof info);
        return;
    }
    if (op == DBG_READ) {
        unsigned len = a1;
        if (!len || len > DBG_MAX_PAYLOAD) { reply(op, DBG_BAD_ARG, seq, 0, 0); return; }
        if (!(flags & DBG_F_UNSAFE_READ) && !read_is_safe(a0, len)) {
            reply(op, DBG_DENIED, seq, 0, 0); return;
        }
#ifdef SITL
        for (unsigned i = 0; i < len; i++) g_read_data[i] = (unsigned char)(a0 + i);
#else
        const volatile unsigned char *src = (const volatile unsigned char *)(unsigned long)a0;
        for (unsigned i = 0; i < len; i++) g_read_data[i] = src[i];
#endif
        reply(op, DBG_OK, seq, g_read_data, len);
        return;
    }
    if (op == DBG_RESOLVE) {
        resolver_fn rv = *(resolver_fn *)SL(RESOLVER_SLOT);
        u32 ptr = rv ? rv(a0, 0) : 0;
        reply(op, ptr ? DBG_OK : DBG_NO_SYMBOL, seq, &ptr, sizeof ptr);
        return;
    }
    if (op == DBG_CALL) {
        if (!(flags & DBG_F_CALL_ACK) || !a0) { reply(op, DBG_DENIED, seq, 0, 0); return; }
        for (unsigned i = 0; i < sizeof g_call_scratch; i++) g_call_scratch[i] = 0;
        a1 = call_arg(a1); a2 = call_arg(a2); a3 = call_arg(a3);
#ifdef SITL
        /* Never jump to a 32-bit simulated resident address in a 64-bit host process.  Model the one
         * getter used by the end-to-end test; all real probing still happens in the ARM build. */
        u32 ret = (a0 == 0x7E600004u) ? 1u : 0x12345678u;
        u32 scratch_addr = (u32)(unsigned long)g_call_scratch;
        if (a1 >= scratch_addr && a1 < scratch_addr + sizeof g_call_scratch)
            g_call_scratch[a1 - scratch_addr] = 1;
        (void)a2; (void)a3;
#else
        typedef u32 (*call3_fn)(u32, u32, u32);
        u32 ret = ((call3_fn)(unsigned long)a0)(a1, a2, a3);
#endif
        finish_call(op, seq, ret);
        return;
    }
    if (op == DBG_SCRATCH_WRITE) {
        if (a1 > 8u || a0 > sizeof g_call_scratch || a1 > sizeof g_call_scratch - a0) {
            reply(op, DBG_BAD_ARG, seq, 0, 0); return;
        }
        for (unsigned i = 0; i < a1; i++)
            g_call_scratch[a0 + i] = (unsigned char)((i < 4 ? a2 : a3) >> ((i & 3u) * 8u));
        reply(op, DBG_OK, seq, 0, 0);
        return;
    }
    if (op == DBG_CALL8) {
        if (!(flags & DBG_F_CALL_ACK) || !a0 || a1 > 8u) {
            reply(op, DBG_DENIED, seq, 0, 0); return;
        }
        u32 av[8];
        for (unsigned i = 0; i < 8; i++) av[i] = call_arg(get_le32(g_call_scratch + i * 4u));
#ifdef SITL
        u32 ret = (a0 == 0x7E600004u) ? 1u : 0x12345678u;
        u32 scratch_addr = (u32)(unsigned long)g_call_scratch;
        if (a1 && av[0] >= scratch_addr && av[0] < scratch_addr + sizeof g_call_scratch)
            g_call_scratch[av[0] - scratch_addr] = 1;
#else
        typedef u32 (*call8_fn)(u32, u32, u32, u32, u32, u32, u32, u32);
        u32 ret = ((call8_fn)(unsigned long)a0)(av[0], av[1], av[2], av[3],
                                               av[4], av[5], av[6], av[7]);
#endif
        finish_call(op, seq, ret);
        return;
    }
    if (op == DBG_RESET) {
        reply(op, DBG_OK, seq, 0, 0);
        (*(sys_reset_fn *)SLOT_SYS_RESET)();
        return;
    }
    reply(op, DBG_BAD_OP, seq, 0, 0);
}

static void protocol_poll(void)
{
    while (g_rx_head - g_rx_tail >= DBG_REQ_SIZE) {
        if (rx_peek(0) != 'W' || rx_peek(1) != 'D' || rx_peek(2) != 'B' || rx_peek(3) != 'G') {
            rx_drop(1);                            /* resynchronise after console/noise bytes */
            continue;
        }
        unsigned char req[DBG_REQ_SIZE];
        for (unsigned i = 0; i < DBG_REQ_SIZE; i++) req[i] = (unsigned char)rx_peek(i);
        rx_drop(DBG_REQ_SIZE);
        handle_request(req);
    }
}

void standalone_main(u32 loader_resolver)
{
    sapi_table_fill(loader_resolver);
#ifdef SITL
    g_write = sitl_usb_data_write;
#else
    g_write = (vcom_write_fn)RES_USB_VCOM_WRITE;
#endif
    usb_regcb_fn reg = *(usb_regcb_fn *)SLOT_VCOM_REGCB;
    static const char name[] = "wstr_usb_debug";
    if (reg) reg(usb_rx_cb, name);

    for (;;) {
        protocol_poll();
#ifdef SITL
        sched_yield();
#else
        __asm volatile ("nop");
#endif
    }
}
