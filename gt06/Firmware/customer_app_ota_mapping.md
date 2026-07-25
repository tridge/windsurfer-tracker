# Where the OTA app lives in flash: the `customer_app` partition

Bridges the OTA image format (`fwtool.py`, `container_format.md`, `image_layout.md`) to the physical
8 MB flash golden dump (`simcom_da/`). Answers: what is the exact relationship between the
`customer_app` flash partition and the WSTRK OTA image we built and flashed?

Established by read-only byte comparison of the golden dump, the `customer_app` partition, and
`firmware_wstrk_xor.bin`, on G375356 (2026-07-12).

## Answer

**The `customer_app` partition holds the OTA image verbatim (`[8-byte wrapper][LZMA payload]`), minus
only its trailing XOR-pad byte. At boot the loader LZMA-decompresses that payload to RAM at
`0x7E1B0000` and runs it; the decompressed app is never stored in flash.**

> **⚠ CORRECTION (2026-07-22): FOTA does NOT write `customer_app` directly.** The resident flash-write
> API stages the image into the **`reserved`** partition (`0x6df000`), then `close()` sets a
> pending-update marker (magic `0x5713452A` @ `fota_param+0x2008`), and the resident boot/update path
> commits `reserved → customer_app`. So the byte-identity shown below is the **post-commit** state of
> `customer_app` (and matches an image written the direct way too). A *direct* write to `customer_app`
> happens only on the **USB `adownload`** path (see Implications), never via FOTA. Full staged-write
> mechanism + the 5-slot resident API: `re_flash_write_api.md`.

```
 OTA .bin (firmware_wstrk_xor.bin, 105341 B)      customer_app @ flash 0x71b000 (size 0x30000)
 ┌─────────────────────────────────────┐          ┌─────────────────────────────────────┐
 │ [u32 clen=0x19b74][u32 ~crc32]       │  ──FOTA──▶│ [u32 clen=0x19b74][u32 ~crc32]  0x00 │
 │ LZMA payload (105332 B)              │  verbatim │ LZMA payload (105332 B)              │
 │ [1 XOR-pad byte]  ← dropped, not     │           │ ...body ends at 0x19b7c (105340 B)   │
 │                     stored in flash  │           │ 0x00 padding ... to 0x30000 (~91 KB) │
 └─────────────────────────────────────┘           └─────────────────────────────────────┘
                                                        │ boot loader: LZMA-inflate payload
                                                        ▼
                                              RAM @ 0x7E1B0000 : 204560 B Thumb-2 app (runs here)
```

## Evidence (all re-runnable, read-only)

- `customer_app[0:105340]` (= `8 + clen`) is **byte-identical** to `firmware_wstrk_xor.bin[0:105340]`:
  same 8-byte wrapper (`clen=0x00019b74`, `~crc32=0x1f953c96`) + the exact LZMA payload. First
  difference is at offset **0x19b7c** (105340), exactly where the OTA body ends. The single trailing
  XOR-pad byte — needed only for the FOTA download `,80#` XOR verify — is **not** written to flash.
- The LZMA payload occurs in the whole 8 MB golden image **only at `0x71b008`** (inside
  `customer_app`). The **decompressed** 204560-B app occurs **nowhere** in flash → it is inflated to
  RAM at boot. (The app links at `0x7E1B0000`; the flash XIP window is `0x80000000` and flash holds
  only the LZMA-compressed form, which cannot be XIP-executed. So: decompress-to-RAM, not XIP.)
- `customer_app` matches the **WSTRK** build, not stock (`repack_stock_xor.bin` differs) → the flash
  currently holds our image (consistent with `AT+SIMCOMATI` → `…250117J16_WSTRK`).
- Partition geometry from the device's on-flash BTPA ptable: `customer_app` @ `0x71b000`, size
  `0x30000` (196608 B). Compressed WSTRK uses 105340 B ⇒ **~91 KB headroom** for a larger compressed
  app (its decompressed size must still fit the RAM region at `0x7E1B0000`).

### What the WSTRK patch changed
Decompressed stock vs wstrk differ in **25 bytes**, all string edits `10F8G`→`WSTRK` at the version
tags (decompressed offset maps to RAM `0x7E1B0000 + off`):

| decompressed off | RAM addr | string |
|---|---|---|
| 0x2d256 | 0x7E1DD256 | SDK revision `…J16_10F8G` → `…J16_WSTRK` (shown by `AT+SIMCOMATI`) |
| 0x2d2a9 | 0x7E1DD2A9 | `MG133_10F8G_B53_V665` → `MG133_WSTRK_B53_V665` (shown by `cxzt`) |
| 0x2e3a9 | 0x7E1DE3A9 | another `MG133_10F8G…` copy |
| (2 more copies) | | ~5 copies total, ≈5 bytes each ⇒ 25 |

### Why the golden-image `nvm` scan showed `10F8G`, not `WSTRK`
`nvm` (0x753000) holds a **separate config copy** of the version string that still reads `10F8G`; the
**running app** in `customer_app` (compressed) carries `WSTRK`. Two independent stores.

## Implications for recovery / injection

- The tracker app partition on our device is **`customer_app`** (the SIMCom/logicrom "user_app"
  equivalent). Its content is `[8-byte wrapper][LZMA payload]`. Two write paths land it there:
  **USB `adownload`** writes `customer_app` *directly*; **FOTA** stages the same bytes in `reserved`
  and the boot/update path commits them to `customer_app` (see the correction under Answer).
- Restoring `customer_app` from the golden dump restores the exact running app.
- USB write via `adownload` to `customer_app` bypasses the OTA 3/day limit and the XOR verify, and
  works on a dead app — the write-recovery safety net for the asm→C injection experiments.

Related: `fwtool.py`, `container_format.md`, `image_layout.md`, `simcom_da/README.md` (golden dump +
partition map), and the master plan `~/.claude/plans/i-want-to-look-snoopy-lemon.md`.
