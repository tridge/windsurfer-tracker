# W07 / W07C firmware — reverse-engineering notes

Analysis of the vendor OTA firmware image `W07_NT19D_MG133_B53_V665.bin` for the Tutrack
W07B/W07C GPS tracker (SIMCom **A7670SA** module, **ASR1603** SoC). Goal: understand the
image well enough to build, repack, and OTA our own firmware — starting with a trivial,
observable string patch to prove the load path end to end.

## TL;DR — how it ticks

- The `.bin` the vendor ships over FOTA is **not encrypted**. It is **LZMA-compressed**
  with an 8-byte wrapper: `[u32 len][u32 ~crc32(payload)]` + a legacy `.lzma` stream. It
  decompresses to a **204,560-byte ARM Thumb-2 application image**. (The "encrypted" claim
  earlier was wrong — it just looked random because it was compressed.)
- The decompressed image is a **SIMCom OpenSDK application** (the customer app only — not
  the modem stack). It links to **`0x7E1B0000`** in the ASR1603's RAM, starts with a
  32-byte **application descriptor** (name ptr → `userSpaceMain`, entry point, build tag
  → `J16_10F8G`), then ~180 KB of Thumb-2 code and ~19 KB of rodata/strings.
- **We can build and load our own firmware over the vendor's FOTA channel — proven on
  hardware (2026-07-09).** A repacked, string-patched image booted on unit G375356, which now
  reports `NT19D_MG133_WSTRK_B53_V665`. No USB, no SDK, no signing key.
- There is **no cryptographic signature.** The FOTA integrity check is an **8-bit XOR over
  the whole `.bin`**, compared against the hex value in the `YCSJ` command's trailing field
  (`,80#` = port 80 **and** expected XOR `0x80`); the vendor pads every image to XOR `0x80`.
  `fwtool.py pack` reproduces this by appending one pad byte. The filename must contain `B53`
  or the device rejects it before downloading. Details in `ota_and_patch_test.md`.
- `fwtool.py` unpacks, repacks (identical LZMA header + correct XOR pad), and same-length
  string-patches these images. Daily limit: 3 FOTA sessions/day (GMT reset).

## Files in this folder

| File | What it is |
|---|---|
| `fwtool.py` | The tool: `info` / `unpack` / `pack` / `patch` / `verify`. **Repacks device-loadable OTA images.** |
| `customer_app_ota_mapping.md` | Where the OTA image lands in flash: the `customer_app` partition holds `[wrapper][LZMA payload]` verbatim (minus the XOR pad); app decompresses to RAM `0x7E1B0000` at boot. Bridges the OTA format to the golden dump (on the fwdev branch). |
| `ota_and_patch_test.md` | The `YCSJ` FOTA mechanism, the no-signature finding, and the **step-by-step patch-load test plan**. |
| `memory_map.txt` | Generated: header fields + section boundaries. |
| `strings_full.txt` | Generated: every string ≥4 chars with file offset + virtual address. |
| `tokens.txt` | Generated: `SZCS#`/`CXCS#` config tokens + reply/status format strings (marker candidates). |
| `disasm_entry.txt` | Generated: disassembly of the copy stub and the entry/init routine. |
| `firmware_patched_demo.bin` | Demonstration: the vendor image with `10F8G`→`WSTRK` (visible in `cxzt`/`VERSION#`). |

Source image: `../../devices/W07C/W07_NT19D_MG133_B53_V665.bin` (and the pre-decompressed
`../../devices/W07C/unpacked/…​.raw`).

⚠ The vendor reverse-engineering material — `extract.py`, `container_format.md`,
`image_layout.md`, `simcom_da/`, `m0_dump/`, `fromsrc/`, `fw-src/` and the `re_*.md`
analysis notes — is NOT on this branch. It lives on the **fwdev** branch; check that
out if you need to regenerate any of it.

## What this does NOT cover

- The **modem/RF protocol stack** and **ThreadX** — they ship as prebuilt SIMCom/ASR blobs,
  are not in this app image, and are not ours to change.
- The **companion supervisor MCU** (SOS button, LEDs, vibration, deep-sleep power-gating).
  It is a separate chip on its own UART; its firmware is not in this image. Our app talks to
  it over UART1 (`Send Sleep Cmd 0x04 sos,Vib,GG,Timing`, `Mcu Goto Sleep/Wakeup`, …).
- Building an *actual* custom app needs the A7670SA OpenSDK target from a SIMCom FAE
  (see the feasibility plan). This folder is about the **image format and load path**, which
  we can now manipulate without the SDK.
