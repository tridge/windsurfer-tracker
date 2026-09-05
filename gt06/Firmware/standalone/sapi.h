/* sapi.h — fixed addresses for the standalone customer_app (from ../standalone_sapi_test.md + RE).
 * The sAPI dispatch table is filled at boot (bake or resolver-replay); slots are then called directly. */
#ifndef SAPI_H
#define SAPI_H

typedef unsigned int   u32;
typedef unsigned char  u8;

/* SITL seam: on the host build the fixed slot addresses can't be dereferenced, so remap every SLOT_*
 * address into a host-side pointer array (sitl_slots[]) that the SITL HAL fills with mock fn-pointers.
 * The call sites in main.c (`*(X_fn*)SLOT_X`) are UNCHANGED — on device SL(a)=a, on host SL(a)=&sitl_slots[idx].
 * Base is below the lowest slot (0x7E1E1F18); index = (addr-base)/4. */
#ifdef SITL
#define SITL_SLOT_BASE 0x7E1E1C00u
extern void *sitl_slots[1024];
#define SL(a) ((unsigned long)(void *)&sitl_slots[((unsigned)(a) - SITL_SLOT_BASE) >> 2])
#else
#define SL(a) ((unsigned long)(a))
#endif

#define SAPI_TABLE_BASE   0x7E1E2000u          /* first dispatch slot */
#define SAPI_TABLE_WORDS  537u                 /* 0x7E1E2000..0x7E1E2864 (RESULT 2) */
#define RESOLVER_SLOT     0x7E1E2444u          /* init1 stashes the loader r0 here; thunk reads it */

/* Known slots (byte VA of the fn-ptr word; deref then call). */
/* task_create (slot 0x7e1e211c -> 0x7e6d2c69): real sig is (void **h0, void **h1, u32 stk_bytes,
 * u32 prio, const char *name, void(*entry)(void), u32 arg). r0/r1 are WRITABLE out-ptrs (the resident
 * stores the created handles there) — passing the entry addr in r0 faults. Kept for reference; the
 * watchdog uses the resident periodic TIMER below instead (no thread/stack to own). */
#define SLOT_TASK_CREATE  SL(0x7E1E211Cu)
typedef int (*task_create_fn)(void **h0, void **h1, unsigned stk, unsigned prio,
                              const char *name, void (*entry)(void), unsigned arg);
#define SLOT_GPS_POWER    SL(0x7E1E21ACu)

/* Resident vendor periodic-timer API (RE'd from app call sites 0x7e1ba4d2/0x7e1ca83c). The callback runs
 * from the resident timer thread — independent of our main loop. ~200 RTOS ticks/sec. App owns only a
 * one-word handle; the resident allocates the TX_TIMER control block. */
#define SLOT_TIMER_CREATE SL(0x7E1E21B8u)   /* idx110 -> 0x7e6d2d6f: int create(void **handle) -> 0 ok */
#define SLOT_TIMER_START  SL(0x7E1E242Cu)   /* idx267 -> 0x7e6d2d75: start(h, initial, reschedule, cb, input) */
typedef int  (*timer_create_fn)(void **handle_out);
typedef void (*timer_start_fn)(void *handle, unsigned long initial, unsigned long reschedule,
                               void (*cb)(unsigned long), unsigned long input);
#define SLOT_VCOM_WRITE   SL(0x7E1E2580u)          /* -> resident RES_USB_VCOM_WRITE */

/* Resident implementations, callable DIRECTLY by fixed VA (no table needed) — captured from this
 * device's boot (fromsrc/sapi_table_boot1.bin). USB-VCOM write is synchronous and needs no bring-up:
 * the resident USB firmware enumerates if04 before the app runs (RE: init2 only registers an RX cb). */
#define RES_USB_VCOM_WRITE 0x7E6916B9u         /* (const void *buf, int len) -> bytes written (Thumb) */

typedef int (*vcom_write_fn)(const void *buf, int len);

/* USB-VCOM host->device READ path (the UAV modem-link RX direction; if04) — RE'd (gt06/Firmware/
 * re_usb_vcom_read.md), the true analogue of sAPI_UartRegisterCallbackEX. Register OUR callback and the
 * resident invokes it (in TASK context) whenever host bytes arrive; drain them with the read slot.
 * Both slots are ALREADY in sapi_pairs.h (regcb hash 0x9fc0f60b, read hash 0x0b22f3b7) => filled by the
 * resolver replay, callable directly. The vendor's own if04 RX handler (init2/YS_UsbVcomCBFuncEx) is
 * dropped in the standalone build, so nothing competes for the single callback slot. */
#define SLOT_VCOM_REGCB SL(0x7E1E2440u)   /* -> 0x7e691743: int reg(void (*cb)(u32 pending), const char *name) */
/* ⚠⚠ read(): the `len` argument is IGNORED by this resident (0x7e69177a memcpy's the whole current RX
 * descriptor into buf and returns its length). The buffer MUST be big enough for the largest record the
 * dispatcher emits (chunks at 0x200) or you overrun it — and as the callback runs on the resident USB
 * task's stack, a small buffer smashes that task and kills if04 RX permanently (reboot-only recovery).
 * Call it ONCE with a buffer >= the announced `pending`, never in a chunked loop. */
#define SLOT_VCOM_READ  SL(0x7E1E20CCu)   /* -> 0x7e69177b: int read(void *buf, int len_IGNORED) -> bytes */
/* sAPI_UsbSwitchFunction (hash 0x84441396, resolver-filled) — int fn(int enable): 0 disables the USB
 * device, 1 re-enables it, forcing a full re-enumeration. This is the ONLY exported USB recovery entry
 * point; there is no narrow "re-post a VCOM RX buffer" API. Use it to re-arm a resident Modem1 RX
 * pipeline that has stopped delivering (host must reopen the tty afterwards). */
#define SLOT_USB_SWITCH SL(0x7E1E2130u)
typedef int (*usb_switch_fn)(int enable);
typedef int (*usb_regcb_fn)(void (*cb)(u32 pending), const char *name);
typedef int (*usb_read_fn)(void *buf, int len);

/* Socket slots (resident lwIP BSD sockets at 0x80140000, reached via the baked table — RE'd STAGE 2).
 * The already-active resident PDP (CGACT=1,1, IP assigned) may already bind lwIP; if connect fails,
 * SLOT_PPP_DIAL(netid,1) dials the bearer first. Connect by IP (skip DNS). */
#define SLOT_SOCKET    SL(0x7E1E2358u)   /* idx214: socket(af=2, type=1, proto=0) -> fd */
#define SLOT_CONNECT   SL(0x7E1E2264u)   /* idx153: connect(fd, &sockaddr_in, 24) -> 0 ok */
#define SLOT_SEND      SL(0x7E1E22A0u)   /* idx168: send(fd, buf, len, 0) -> bytes */
#define SLOT_RECV      SL(0x7E1E21BCu)   /* idx111: recv(fd, buf, len, flags) -> n (<0 = EWOULDBLOCK when nonblk) */
#ifndef MSG_DONTWAIT                  /* on SITL the host <sys/socket.h> defines these; keep its values there */
#define MSG_DONTWAIT   0x08u          /* lwIP recv flag: NOT honored by this resident (blocks ~5s) — use RCVTIMEO */
#endif
/* setsockopt (RE'd: lwIP XIP run 0x80140000 = flash 0x140000; idx254 -> 0x80147153, sig below). Set a short
 * SO_RCVTIMEO so recv() returns instead of blocking ~5s (the resident's default RCVTIMEO). Level 0xFFF
 * routes to the timeout handler; wrong optval/optlen only returns EINVAL (safe to probe). */
#define SLOT_SETSOCKOPT SL(0x7E1E23F8u)
#ifndef SOL_SOCKET
#define SOL_SOCKET     0xFFFu
#endif
#ifndef SO_RCVTIMEO
#define SO_RCVTIMEO    0x1006u
#endif
typedef int (*setsockopt_fn)(int s, int level, int optname, const void *optval, unsigned optlen);
#define SLOT_CLOSE     SL(0x7E1E22E4u)   /* idx185: close(fd) */
/* select (RE'd: idx309 -> 0x8014644D). fd_set = 32-byte bitmask (fd bit = set[fd>>3] & (1<<(fd&7)),
 * FD_SETSIZE 256); timeout = {int32 tv_sec, int32 tv_usec}: NULL=block forever, {0,0}=non-blocking poll,
 * else wait tv_sec*1000 + (tv_usec+500)/1000 ms. Returns >0=#ready (sets rewritten), 0=timeout, -1=error.
 * Used to guard recv() so it never blocks (SO_RCVTIMEO is accepted but NOT honored by this resident). */
#define SLOT_SELECT    SL(0x7E1E24D4u)
typedef int (*select_fn)(int maxfdp1, void *rd, void *wr, void *ex, void *timeout);
/* fcntl (RE'd: idx347 -> 0x8014733B) — non-standard: fcntl(s,4,val) sets non-blocking, val in {0,1}
 * (bit0, NOT O_NONBLOCK 0x800); fcntl(s,3,0) gets it. Kept as an alternative to select-guarded recv. */
#define SLOT_FCNTL     SL(0x7E1E256Cu)
#define F_GETFL_NB     3
#define F_SETFL_NB     4
typedef int (*fcntl_fn)(int s, int cmd, int val);
#define SLOT_PPP_DIAL  SL(0x7E1E2578u)   /* idx350: (netid,1) data-bearer dial (fallback) */

/* GNSS (RE'd STAGE 3b): power/init are 3 resident slot calls; fixes arrive as NMEA RMC text pushed onto
 * the resident "URC QUEUE" message bus (subscribe via MsgQ slots, then MsgQRecv 16-byte messages). */
#define SLOT_GNSS_POWER  SL(0x7E1E21ACu)   /* idx107: gnss_power(on) */
#define SLOT_GNSS_POWER_STATUS SL(0x7E1E25DCu) /* hash 0xe6a5ffad: sAPI_GnssPowerStatusGet(void)
                                               * -> 0 OFF, 1 ON. Identified and transition-tested on
                                               * G306161 via the WDBG export-table debugger. */
#define SLOT_GNSS_CFG    SL(0x7E1E2418u)   /* idx262: gnss_cfg(1,1) config URC */
#define SLOT_GNSS_FREQ   SL(0x7E1E2344u)   /* idx209: gnss_set_freq(1) */
typedef int (*gnss_status_fn)(void);
#define SLOT_MSGQ_CREATE SL(0x7E1E25D0u)   /* create: (u32 *qid, name, elemsize, depth, flags) -> 0 ok */
#define SLOT_URC_ATTACH  SL(0x7E1E1F88u)   /* attach(qid, -1): register the queue as the URC sink */
#define SLOT_MSGQ_RECV   SL(0x7E1E1F18u)   /* recv: (qid, msgbuf16, timeout) blocking (was wrongly 0x7e1e20bc) */
/* newlib _free_r(reent, ptr). Vendor free() veneer 0x7e1d793c calls this slot; the resolved implementation
 * on this firmware (0x7e6f2244) ignores `reent`, NULL-checks ptr, then enters the resident heap free. */
#define SLOT_FREE_R      SL(0x7E1E2394u)
typedef void (*free_r_fn)(void *reent, void *ptr);

/* Config store (STAGE 4, offsets confirmed from the golden nvm dump): the vendor config is a file on the
 * resident FS "c:/cwprm.bin" (identical backup "c:/cwbank.bin"), 0x800 bytes, file offset == field offset.
 * On flash it is nvm+0x7000 (0x75a000) with the backup at nvm+0x8000 (0x75b000) — a redundant pair, not A/B.
 * Fields: server IP @+0x034 (ASCII), port @+0x05c (BE u16), TERIID @+0x104 (ASCII 15 digits), APN @+0x210.
 * IMEI getter for un-provisioned units. All via stdio-style resident slots. */
#define SLOT_FOPEN   SL(0x7E1E1F80u)   /* (const char *name, const char *mode) -> FILE* (0=fail) */
#define SLOT_FREAD   SL(0x7E1E227Cu)   /* (void *dst, u32 size, u32 count, FILE*) -> count */
#define SLOT_FWRITE  SL(0x7E1E2088u)   /* (const void *src, u32 size, u32 count, FILE*) -> count */
#define SLOT_FCLOSE  SL(0x7E1E25A8u)   /* (FILE*) */
#define SLOT_IMEI    SL(0x7E1E2374u)   /* int imei_get(char *out16) -> 0 ok, writes 15 ASCII digits */
#define SLOT_SYS_RESET SL(0x7E1E2544u) /* idx337: resident sys_reset (0x7e6f22fa). The app's own reboot
                                    * wrapper 0x7e1b1afc is just `bx *SLOT` -> this. No arg, no return. */
typedef void (*sys_reset_fn)(void);
#define SLOT_CHARGER_STATE SL(0x7E1E219Cu) /* hash 0xa221fdd6: int charger_state(void) -> 1 while charger/
                                    * VBUS present. Vendor heartbeat bit2 source (via its 0x7e1b1b3c stub);
                                    * SoC/PMIC-side — the M0 supervisor has NO charger state (RE 07-23). */
typedef int (*charger_state_fn)(void);
#define SLOT_NETSTAT_START SL(0x7E1E202Cu) /* hash 0x5dbeb222: void netstat_start(void) — one-shot
                                    * network-status bring-up (CP 0x7e6d7d40): creates the IPC handle
                                    * ([0x7e4652a0]) that GATES get_csq/get_reg (they return 1 without
                                    * it), then polls registration <=4s. Vendor calls it once at network
                                    * init (its 0x7e1b6498); call before the first csq_refresh(). */
typedef void (*netstat_start_fn)(void);
#define SLOT_GET_CSQ SL(0x7E1E1F54u) /* hash 0xdb515814: int get_csq(u8 *out_rssi) -> 0 ok, out 0..31.
                                    * Vendor signal source (its 0x7e1ca468 poller) — direct read, keeps
                                    * AT+CSQ text OFF the URC queue where it interleaved into NMEA. */
typedef int (*get_csq_fn)(unsigned char *out);
#define SLOT_GET_REG SL(0x7E1E23C0u) /* hash 0x41e775b1: int get_reg_stat(u32 *out) -> 0 ok;
                                    * (*out & ~4)==1 => registered (1=home, 5=roaming). CEREG-equivalent. */
typedef int (*get_reg_fn)(unsigned *out);
/* OpenSDK network/system APIs recovered by exact hash through WDBG on G306161. NetworkGetCpsi fills
 * the documented SCcpsiParm prefix; this resident also appends RSSI/EARFCN/radio-frame fields. Keep
 * the complete 152-byte object so a newer tail can never overwrite the caller's stack. */
#define SLOT_NETWORK_INIT       SL(0x7E1E25C4u) /* hash 0xefc83346: int sAPI_NetworkInit(void) */
#define SLOT_NETWORK_GET_CPSI   SL(0x7E1E2230u) /* hash 0xeacb3566: int sAPI_NetworkGetCpsi(SCcpsiParm *) */
#define SLOT_NETWORK_GET_COPS   SL(0x7E1E263Cu) /* hash 0x2bfe7bba: int sAPI_NetworkGetCops(char *) */
#define SLOT_SYSTEM_SLEEP_SET   SL(0x7E1E1F60u) /* hash 0xfd0619d9: int sAPI_SystemSleepSet(flag) */
#define SLOT_SYSTEM_SLEEP_GET   SL(0x7E1E2194u) /* hash 0xe629127d: int sAPI_SystemSleepGet(void) */
#define SLOT_SYSTEM_ALARM_WAKE  SL(0x7E1E22CCu) /* hash 0xb197aca7: int sAPI_SystemAlarmClock2Wakeup(ms) */
typedef int (*network_init_fn)(void);
typedef struct {
    char networkmode[40];
    char Mnc_Mcc[20];
    char GSMBandStr[20];
    char LTEBandStr[20];
    int LAC, CellID, TAC, Rsrp, Rsrq, RXLEV, TA, SINR, Rssi;
    int dlEuArfcn, subframeAssignment;
    /* This resident writes the physical-cell ID at +144. A newer public header calls the same tail word
     * systemFrameNumber, so name it by measured semantics and retain one spare word for buffer safety. */
    int observedPhysicalCellId, reservedTail;
} cpsi_t;
typedef int (*network_cpsi_fn)(cpsi_t *out);
typedef int (*network_cops_fn)(char *out);
typedef int (*system_sleep_set_fn)(int flag);
typedef int (*system_sleep_get_fn)(void);
typedef int (*system_alarm_wake_fn)(unsigned long ms);
/* Status-LED GPIO family (item 14, RE re_leds.md). Two app LEDs = gpio ids 7 and 0x78 driven by the
 * resident GPIO sAPI; all pairs already in sapi_pairs.h. Red charge LED is the M0's, not reachable here. */
#define SLOT_GPIO_CONFIG SL(0x7E1E254Cu) /* hash 0x3c43521f: gpio_config(id, mode) -> mode 1 = output */
#define SLOT_GPIO_SET    SL(0x7E1E2104u) /* hash 0x5255c7d8: gpio_set_level(id, level) -> 1=on 0=off */
typedef int (*gpio_cfg_fn)(unsigned id, unsigned mode);
typedef int (*gpio_set_fn)(unsigned id, unsigned level);
#define CFG_OFF_IP    0x034        /* server IP/host, ASCII NUL-term */
#define CFG_OFF_PORT  0x05C        /* server port, big-endian u16 */
#define CFG_OFF_TERIID 0x104       /* TERIID, ASCII 15 digits NUL-term */
#define CFG_OFF_USER  0x190        /* APN username (USERPPP), ASCII NUL-term, ~30B field */
#define CFG_OFF_PW    0x1AE        /* APN password (PWPPP), ASCII NUL-term, ~30B field */
#define CFG_OFF_APN   0x210        /* APN, ASCII NUL-term, ~30B field (next vendor field @0x230) */

typedef void *FILE_t;
typedef FILE_t (*fopen_fn)(const char *name, const char *mode);
typedef unsigned (*fread_fn)(void *dst, unsigned size, unsigned count, FILE_t fp);
typedef unsigned (*fwrite_fn)(const void *src, unsigned size, unsigned count, FILE_t fp);
typedef int (*fclose_fn)(FILE_t fp);
typedef int (*imei_fn)(char *out);

typedef int (*gnss1_fn)(int a);
typedef int (*gnss2_fn)(int a, int b);
typedef int (*msgqc_fn)(void *qid, const char *name, int elemsize, int depth, int flags);
typedef int (*attach_fn)(unsigned qid, int arg);
typedef int (*msgqr_fn)(unsigned qid, void *msg, int timeout);

typedef int (*socket_fn)(int af, int type, int proto);
typedef int (*connect_fn)(int fd, const void *sa, int salen);
typedef int (*send_fn)(int fd, const void *buf, int len, int flags);
typedef int (*recv_fn)(int fd, void *buf, int len, int flags);
typedef int (*close_fn)(int fd);
typedef int (*dial_fn)(int netid, int up);

/* Supervisor-MCU UART (STAGE: battery mV — RE: gt06/Firmware/re_battery_voltage.md). The M0 "MCU Power
 * Manage" coprocessor is on UART1; the R5 queries it with a `55 AA` framed packet and the `0x82` reply
 * carries battery millivolts. These four slots are already in sapi_pairs.h and filled by the resolver
 * replay (verified). Wrappers 0x7e1b19cc/dc/ec/fc just `bx *slot`, so we call the slots directly. */
#define SLOT_UART_READ    SL(0x7E1E1F3Cu)   /* sAPI_UartRead(ref, void *buf, int len) -> bytes read */
#define SLOT_UART_REGCB   SL(0x7E1E21E0u)   /* sAPI_UartRegisterCallbackEX(port, cb, reserve) */
#define SLOT_UART_SETCFG  SL(0x7E1E2478u)   /* sAPI_UartSetConfig(port, const void *cfg) */
#define SLOT_UART_WRITE   SL(0x7E1E24A0u)   /* sAPI_UartWrite(port, const void *buf, int len) -> bytes */
/* cfg is {u32 baud @+0; u16 dataBits(=3) @+4; u8 parity(=0) @+6} = 9600 8-N-1 (vendor 0x7e1d46a4). The RX
 * callback ABI is cb(uart_ref, pending_len, reserve); pass uart_ref straight to UartRead (vendor 0x7e1d4988). */
typedef int  (*uart_setcfg_fn)(int port, const void *cfg);
typedef int  (*uart_regcb_fn)(int port, void (*cb)(u32, u32, u32), void *reserve);
typedef int  (*uart_read_fn)(u32 ref, void *buf, int len);
typedef int  (*uart_write_fn)(int port, const void *buf, int len);

/* Resident app-package flash-write API (FOTA — RE: gt06/Firmware/re_flash_write_api.md). Stages a
 * [u32 clen][u32 ~crc32][LZMA] image into the `reserved` partition (chosen internally by name, no caller
 * offset); close() flushes the last page + writes the pending-update marker (0x5713452A @ fota_param+0x2008);
 * the resident boot/update path then commits reserved->customer_app. customer_app is NEVER touched by a
 * partial write => brick/field-safe. All 5 slots are already filled by the resolver replay. */
#define SLOT_FLASH_PREPARE SL(0x7E1E202Cu)   /* prepare(void): FBF-disable + wait for a safe (registered) moment */
#define SLOT_FLASH_OPEN    SL(0x7E1E2620u)   /* open(const u8 *mode) -> 0 ok; *mode 0x77='w', 0x72='r' */
#define SLOT_FLASH_WRITE   SL(0x7E1E23ECu)   /* write(const void *src, u32 len) -> 0 ok; sequential, buffers to 4KB */
#define SLOT_FLASH_CLOSE   SL(0x7E1E2144u)   /* close(void) -> 0 ok; flush final page + set pending-update marker */
#define SLOT_FLASH_VERIFY  SL(0x7E1E237Cu)   /* verify(pkg_info *out) -> 0 ok; checks staged header + reflected CRC-32 */
typedef void (*flash_prepare_fn)(void);
typedef int  (*flash_open_fn)(const unsigned char *mode);
typedef int  (*flash_write_fn)(const void *src, unsigned len);
typedef int  (*flash_close_fn)(void);
typedef int  (*flash_verify_fn)(unsigned *out);   /* out = {u32 valid; u32 payload_len; u32 payload_crc} */

/* fill the dispatch table so the slots above become callable. Returns non-zero on success. */
int sapi_table_fill(u32 loader_resolver);

#endif
