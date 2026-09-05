# W07C USB debug firmware

This dedicated `customer_app` replaces the tracker application while installed and exposes a small
binary debugger on USB VCOM **if04**. It is for a recoverable bench unit only.

Build and test:

```sh
make usb-debug test-usb-debug
```

Artifacts:

- `customer_app_usb_debug.bin.flash.zip` — ready for the existing `adownload` USB flash path.
- `W07_USBDBG_B53.bin` — 64 KiB vendor/standalone FOTA image, whole-file XOR `0x80`.

Flash with the ASR helper's `--reboot` option so its signed CRANEGM download agent performs the reset
in software. A physical power-cycle is only a fallback if that reset agent fails. Reinstall the current
production `customer_app_standalone.bin` (or the golden vendor app) when exploration is finished.

## Host tool

`usb_debug.py` auto-detects the option-driver tty belonging to USB interface 04. A port can also be
specified explicitly.

```sh
python3 usb_debug.py ping
python3 usb_debug.py info
python3 usb_debug.py read 0x7e1e1f00 0x100
python3 usb_debug.py resolve 0x9fc0f60b
python3 usb_debug.py resolve sAPI_GnssPowerStatusGet
python3 usb_debug.py resolve sAPI_NetworkGetCpsi
```

Ordinary reads are restricted to this app's image/BSS and `0x7e1e1c00..0x7e1e2868` (the complete sAPI
slot table). Resident RAM/code reads require `read --unsafe`; an unmapped address can fault the CPU.

Function calls require the conspicuous `--i-accept-crash-risk` option. The basic call supports three
32-bit arguments. Writing `scratch` as an argument substitutes a pointer to a zeroed 256-byte firmware
buffer and returns that buffer after the call, which covers getters with an output-pointer ABI:

```sh
python3 usb_debug.py call 0x7e600001 --i-accept-crash-risk
python3 usb_debug.py call 0x7e600001 scratch --i-accept-crash-risk
python3 usb_debug.py gps-power-status --i-accept-crash-risk
```

`callx` supports up to eight AAPCS arguments (four registers plus four stack arguments). It can preload
binary or NUL-terminated text at `scratch+32`; arguments named `scratch+N` pass a pointer into that
buffer. The first 32 scratch bytes hold the eight argument words.

```sh
python3 usb_debug.py callx 0x7e600001 1 2 3 4 5 --i-accept-crash-risk
python3 usb_debug.py callx 0x7e600001 scratch+32 --data-ascii 'c:/cwprm.bin' --i-accept-crash-risk
```

The resident resolver is a linear `{hash,function}` table. On the tested V1.11.2 resident its table is
at `0x7e4643f0`; enumerate all 460 callable exports without knowing any name hashes with:

```sh
python3 usb_debug.py exports --output exports.csv
```

Disassembly and live off/on/off probing identified export 106 as `sAPI_GnssPowerStatusGet`: hash
`0xe6a5ffad`, function `0x7e6db0fb`, resolved standalone slot `0x7e1e25dc`. It returns 0 when GNSS is
off and 1 when it is on; power transitions are asynchronous and may take a few seconds to appear.

`usb_debug.py` also contains an exact, live-resolver-confirmed name map for the GNSS, sleep, LTE/PSM,
task-health, and watchdog APIs investigated for the tracker. Named resolution consults this map before
falling back to speculative hash algorithms. See `usb_api_findings.md` for ABIs, live results, and the
distinction between functions that were called and functions that were only resolved.

Always do resolver-only queries before calling a candidate. A timeout after a read/call is printed as
`CRASH/REBOOT NOTICE`; at that point the CPU or USB task may need a physical power-cycle. A responsive
device can be rebooted with `python3 usb_debug.py reset`.

## Wire protocol (version 1)

All integers are little-endian. A request is exactly 32 bytes:

```text
"WDBG" | version:u8 | op:u8 | flags:u16 | seq:u32 |
arg0:u32 | arg1:u32 | arg2:u32 | arg3:u32 | crc32(first 28 bytes):u32
```

A response is variable length:

```text
"WDBR" | version:u8 | op:u8 | status:u16 | seq:u32 | length:u32 |
payload[length] | crc32(header + payload):u32
```

Operations are `PING=1`, `INFO=2`, `READ=3`, `RESOLVE=4`, `CALL=5`, `RESET=6`, `SCRATCH_WRITE=7`, and
`CALL8=8`. Reads are capped at 512 bytes per request. `RESOLVE` takes the loader's precomputed 32-bit
symbol hash and never invokes the result. Calls take the function address in `arg0`; the explicit call
flag is mandatory.
