# standalone — build the from-scratch customer_app image (our C only, no vendor app).
#   make            -> standalone_fw.bin       (LZMA+wrapped, XOR-padded, OTA-loadable)
#   make flashimg   -> customer_app_standalone.bin  (partition body padded to 0x30000 for the USB flasher)
#   make clean
#
# See ../standalone_sapi_test.md. Loads at 0x7E1B0000; descriptor first; entry = _start (r0 = resolver).
CC      := arm-none-eabi-gcc
OBJCOPY := arm-none-eabi-objcopy
GIT_HASH := $(shell git -C . rev-parse --short=7 HEAD 2>/dev/null || echo nogit)
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
standalone_fota.bin: standalone_fw.bin
	python3 -c "import struct; \
d=open('standalone_fw.bin','rb').read(); clen=struct.unpack('<I',d[:4])[0]; body=d[:8+clen]; \
open('$@','wb').write(body); \
print('standalone_fota.bin', len(body),'bytes (clen+8, pad-free)')"

fotaimg: standalone_fota.bin   ## build the pad-free image to host for YCSJ FOTA

# ---- SITL: host build of the SAME main.c against sitl_hal.c mocks (talks to a real server) ----
# Built 64-bit; the one 32-bit-pointer dependency (the URC message word the fw casts to a pointer) is met by
# allocating those buffers low via mmap(MAP_32BIT) in sitl_hal.c. Run e.g.:  SITL_SERVER=127.0.0.1:8711 ./sitl
HOSTCC   := gcc
SITLSRC  := main.c sitl_hal.c
SITLCFLAGS := -DSITL -std=gnu11 -O1 -g -Wall -Wextra -Werror -Wno-unused-parameter -pthread \
              -DGIT_HASH='"$(GIT_HASH)-sitl"'

sitl: $(SITLSRC) sapi.h              ## build the SITL host binary (plain)
	$(HOSTCC) $(SITLCFLAGS) -o $@ $(SITLSRC) -pthread

sitl-asan: $(SITLSRC) sapi.h         ## SITL under AddressSanitizer
	$(HOSTCC) $(SITLCFLAGS) -fsanitize=address -fno-omit-frame-pointer -o $@ $(SITLSRC) -pthread

sitl-ubsan: $(SITLSRC) sapi.h        ## SITL under UndefinedBehaviorSanitizer
	$(HOSTCC) $(SITLCFLAGS) -fsanitize=undefined -fno-sanitize-recover=all -o $@ $(SITLSRC) -pthread

sitl-all: sitl sitl-asan sitl-ubsan  ## build all SITL variants

SITL_TEST_BIN ?= ./sitl
test-gps-retry: sitl                 ## exercise truthful GPS STATUS + 10-second power-on retry
	SITL_BIN=$(SITL_TEST_BIN) python3 test_gps_retry.py

test-loc-batch: sitl                 ## exercise the batched LOC message (protocol 0xF1)
	SITL_BIN=$(SITL_TEST_BIN) python3 test_loc_batch.py

test: test-gps-retry test-loc-batch  ## all SITL regression tests

clean:
	rm -f stub_standalone.elf standalone.raw standalone_fw.bin customer_app_standalone.bin standalone_fota.bin \
	      sitl sitl-asan sitl-ubsan

.PHONY: all flashimg fotaimg sitl-all test-gps-retry test-loc-batch test clean
