# standalone — build the from-scratch customer_app image (our C only, no vendor app).
#   make            -> standalone_fw.bin       (LZMA+wrapped, XOR-padded, OTA-loadable)
#   make flashimg   -> customer_app_standalone.bin  (partition body padded to 0x30000 for the USB flasher)
#   make clean
#
# See ../standalone_sapi_test.md. Loads at 0x7E1B0000; descriptor first; entry = _start (r0 = resolver).
CC      := arm-none-eabi-gcc
OBJCOPY := arm-none-eabi-objcopy
# last commit that touched this firmware (not the repo HEAD), "-dirty" if it has uncommitted changes
GIT_HASH := $(shell git log -1 --format=%h --abbrev=7 -- . 2>/dev/null || echo nogit)$(shell git diff --quiet HEAD -- . 2>/dev/null || echo -dirty)
CFLAGS  := -mcpu=cortex-r5 -mthumb -mfloat-abi=soft -Os -ffreestanding -nostdlib \
           -fno-exceptions -fno-unwind-tables -ffunction-sections -fdata-sections -Wall -Werror \
           -DGIT_HASH='"$(GIT_HASH)"'

SRCS := start.S main.c sapi_table.c reliability.c hwwd.c
GEN  := sapi_pairs.h

all: standalone_fw.bin

# resolver-replay pairs (init1's hash->slot map) + supplements. sapi_pairs.h is COMMITTED so this tree
# builds without any vendor image present; regenerate it with gen_pairs.py if the slot map ever changes.

stub_standalone.elf: $(SRCS) $(GEN) sapi.h link_standalone.ld
	$(CC) $(CFLAGS) -Wl,-T,link_standalone.ld -Wl,--gc-sections -Wl,--build-id=none -o $@ $(SRCS)

standalone.raw: stub_standalone.elf
	$(OBJCOPY) -O binary $< $@

# Dedicated USB debugger variant.  It replaces the tracker app while installed and speaks WDBG on if04;
# keep its artifacts distinct so it cannot be mistaken for a production tracker image.
usb_debug.elf: start.S usb_debug.c sapi_table.c $(GEN) sapi.h link_standalone.ld
	$(CC) $(CFLAGS) -Wl,-T,link_standalone.ld -Wl,--gc-sections -Wl,--build-id=none \
		-o $@ start.S usb_debug.c sapi_table.c

usb_debug.raw: usb_debug.elf
	$(OBJCOPY) -O binary $< $@

usb_debug_fw.bin: usb_debug.raw
	python3 -c "import sys; sys.path.insert(0,'..'); import fwtool; \
open('$@','wb').write(fwtool.pack(open('$<','rb').read())); \
print('$@', __import__('os').path.getsize('$@'),'bytes')"

customer_app_usb_debug.bin: usb_debug_fw.bin
	python3 -c "import struct; d=open('$<','rb').read(); clen=struct.unpack('<I',d[:4])[0]; \
body=d[:8+clen]; assert len(body)<=0x30000; \
open('$@','wb').write(body+b'\0'*(0x30000-len(body))); print('$@',len(body),'->',0x30000)"

customer_app_usb_debug.bin.flash.zip: customer_app_usb_debug.bin customer_app_standalone.bin.flash.zip
	python3 -c "import zipfile; src=zipfile.ZipFile('customer_app_standalone.bin.flash.zip'); \
dst=zipfile.ZipFile('$@','w',zipfile.ZIP_DEFLATED); \
[dst.writestr(i,open('$<','rb').read() if i.filename=='fw_image.bin' else src.read(i.filename)) for i in src.infolist()]; \
dst.close(); src.close(); print('$@')"

W07_USBDBG_B53.bin: usb_debug_fw.bin
	python3 -c "import functools,operator; d=bytearray(open('$<','rb').read()); \
assert len(d)<65536; d+=b'\0'*(65536-len(d)); \
assert functools.reduce(operator.xor,d,0)==0x80; open('$@','wb').write(d); \
print('$@',len(d),'bytes (XOR 0x80)')"

usb-debug: customer_app_usb_debug.bin.flash.zip W07_USBDBG_B53.bin

# LZMA + [len][~crc32] wrapper + XOR pad (device-loadable), via the repo fwtool.
standalone_fw.bin: standalone.raw
	python3 -c "import sys; sys.path.insert(0,'..'); import fwtool; \
open('$@','wb').write(fwtool.pack(open('standalone.raw','rb').read())); \
print('standalone_fw.bin', __import__('os').path.getsize('$@'),'bytes')"

# customer_app partition body for the USB flash path: [wrapper][LZMA] (no XOR pad) padded to 0x30000.
customer_app_standalone.bin: standalone_fw.bin
	python3 -c "import sys,struct; \
d=open('standalone_fw.bin','rb').read(); clen=struct.unpack('<I',d[:4])[0]; body=d[:8+clen]; \
assert len(body) <= 0x30000, 'body %d exceeds partition 0x30000' % len(body); \
open('$@','wb').write(body + b'\x00'*(0x30000-len(body))); \
print('customer_app_standalone.bin', len(body),'->', 0x30000)"

flashimg: customer_app_standalone.bin   ## build the USB-flashable partition image

# FOTA image for HTTP hosting (YCSJ). As of the pad-tolerant fota_run, standalone_fw.bin (XOR-padded)
# is the UNIVERSAL image: fota_run now CRC-checks only the clen payload bytes and strips a trailing XOR
# pad, so the ONE padded image feeds BOTH our CRC path AND the vendor OTA's 8-bit-XOR path (XOR==0x80).
# Host standalone_fw.bin for either firmware. This pad-free variant is kept for reference/older paths;
# it also works with fota_run. (0x30000 zero-fill still breaks it — never host customer_app_standalone.bin.
# Filename must contain "B53" for the vendor FOTA verifier.)
# ONE image for both consumers. The VENDOR downloader requires 40960..245760 bytes and a whole-file
# 8-bit XOR matching the YCSJ trailing field (0x80); OUR fota_run caps its write/CRC at the wrapper's
# clen+8 and discards the pad, so the same padded file satisfies both (proved by test_fota_image.py).
# Zero padding leaves the XOR untouched, so the pack step's existing pad byte still lands it on 0x80.
# Do NOT ship a pad-free variant as well: two forms is how the wrong one gets flashed.
standalone_fota.bin: standalone_fw.bin
	python3 -c "import functools,operator; \
d=bytearray(open('standalone_fw.bin','rb').read()); \
assert len(d) < 65536, 'image already exceeds the pad target'; \
d += b'\0' * (65536 - len(d)); \
x=functools.reduce(operator.xor, d, 0); \
assert x == 0x80, 'padded image XOR is 0x%02X, not 0x80' % x; \
assert 40960 <= len(d) <= 245760, 'outside the vendor size window'; \
open('$@','wb').write(bytes(d)); \
print('standalone_fota.bin', len(d), 'bytes (padded, XOR 0x80 — vendor AND our own fota_run)')"

fotaimg: standalone_fota.bin   ## build the ONE OTA image (vendor + our firmware both accept it)

# ---- SITL: host build of the SAME main.c against sitl_hal.c mocks (talks to a real server) ----
# Built 64-bit; the one 32-bit-pointer dependency (the URC message word the fw casts to a pointer) is met by
# allocating those buffers low via mmap(MAP_32BIT) in sitl_hal.c. Run e.g.:  SITL_SERVER=127.0.0.1:8711 ./sitl
HOSTCC   := gcc
SITLSRC  := main.c sitl_hal.c reliability.c
SITLCFLAGS := -DSITL -std=gnu11 -O1 -g -Wall -Wextra -Werror -Wno-unused-parameter -pthread \
              -DGIT_HASH='"$(GIT_HASH)-sitl"'

sitl: $(SITLSRC) sapi.h reliability.h              ## build the SITL host binary (plain)
	$(HOSTCC) $(SITLCFLAGS) -o $@ $(SITLSRC) -pthread

usb-debug-sitl: usb_debug.c sitl_hal.c sapi.h  ## WDBG firmware with if04 simulated by --usb-listen
	$(HOSTCC) $(SITLCFLAGS) -o $@ usb_debug.c sitl_hal.c -pthread

sitl-asan: $(SITLSRC) sapi.h reliability.h         ## SITL under AddressSanitizer
	$(HOSTCC) $(SITLCFLAGS) -fsanitize=address -fno-omit-frame-pointer -o $@ $(SITLSRC) -pthread

sitl-ubsan: $(SITLSRC) sapi.h reliability.h        ## SITL under UndefinedBehaviorSanitizer
	$(HOSTCC) $(SITLCFLAGS) -fsanitize=undefined -fno-sanitize-recover=all -o $@ $(SITLSRC) -pthread

sitl-all: sitl sitl-asan sitl-ubsan  ## build all SITL variants

# Tests run against the AddressSanitizer build by default: the batched-LOC work shipped a stack
# buffer overflow that the plain build ran straight through, and only ASan caught it.
SITL_TEST_BIN ?= ./sitl-asan
test-gps-retry: sitl-asan            ## exercise truthful GPS STATUS + 10-second power-on retry
	SITL_BIN=$(SITL_TEST_BIN) python3 test_gps_retry.py

test-loc-batch: sitl-asan            ## exercise the batched LOC message (protocol 0xF1)
	SITL_BIN=$(SITL_TEST_BIN) python3 test_loc_batch.py

test-fota-image: sitl-asan standalone_fw.bin  ## one image must satisfy vendor AND our own FOTA
	SITL_BIN=$(SITL_TEST_BIN) python3 test_fota_image.py

test-battery-mv: sitl-asan           ## supervisor battery mV, including a unit on charge
	SITL_BIN=$(SITL_TEST_BIN) python3 test_battery_mv.py

test-gpsstat: sitl-asan              ## CXCS#GPSSTAT tells a dead receiver from an unlocked one
	SITL_BIN=$(SITL_TEST_BIN) python3 test_gpsstat.py

test-sapifind: sitl-asan             ## CXCS#SAPIFIND identifies the resolver's hash scheme
	SITL_BIN=$(SITL_TEST_BIN) python3 test_sapifind.py

test-usb-debug: usb-debug-sitl       ## binary WDBG framing, reads, resolver lookup, scratch call
	python3 test_usb_debug.py

test-lte-sleep: sitl-asan            ## direct CPSI telemetry + GPS-safe disconnected alarm sleep
	SITL_BIN=$(SITL_TEST_BIN) python3 test_lte_sleep.py

test-plmn-policy: sitl-asan          ## persistent carrier restrictions and offline recovery
	SITL_BIN=$(SITL_TEST_BIN) python3 test_plmn_policy.py

test-track-recovery: sitl-asan     ## tracking: early, repeated no-data radio cycles
	SITL_BIN=$(SITL_TEST_BIN) python3 test_track_recovery.py

test-watchdog: sitl-asan            ## real reliability.c: hung send + boot hang reset, reset reasons
	SITL_BIN=$(SITL_TEST_BIN) python3 test_watchdog.py

test-link: sitl-asan                 ## dead-link detection, in-flight replay, SOS until ACK, bad commands
	SITL_BIN=$(SITL_TEST_BIN) python3 test_link.py

test-gps-stall: sitl-asan            ## silent GNSS: power-cycle, one reboot, recovery
	SITL_BIN=$(SITL_TEST_BIN) python3 test_gps_stall.py

test-offline: sitl-asan              ## idle offline: connect backoff, sleep, no-data reboot
	SITL_BIN=$(SITL_TEST_BIN) python3 test_offline.py

test-fota-fail: sitl-asan standalone_fw.bin  ## FOTA failures are bounded, never commit a bad image
	SITL_BIN=$(SITL_TEST_BIN) python3 test_fota_fail.py

test: test-watchdog test-link test-gps-stall test-offline test-fota-fail test-gps-retry test-loc-batch test-fota-image test-battery-mv test-gpsstat test-sapifind test-usb-debug test-lte-sleep test-plmn-policy test-track-recovery  ## all SITL regression tests

clean:
	rm -f stub_standalone.elf standalone.raw standalone_fw.bin customer_app_standalone.bin standalone_fota.bin \
	      sitl sitl-asan sitl-ubsan usb_debug.elf usb_debug.raw usb_debug_fw.bin \
	      customer_app_usb_debug.bin customer_app_usb_debug.bin.flash.zip W07_USBDBG_B53.bin usb-debug-sitl

.PHONY: all flashimg fotaimg usb-debug sitl-all test-gps-retry test-loc-batch test-fota-image \
	test-battery-mv test-gpsstat test-sapifind test-usb-debug test-lte-sleep test-plmn-policy test-watchdog test-link test-gps-stall test-offline test-fota-fail test clean
