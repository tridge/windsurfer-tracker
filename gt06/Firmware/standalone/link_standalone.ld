/* link_standalone.ld — a FROM-SCRATCH customer_app image (our C only, no vendor app).
 *
 * Distinct from ../fw-src/link.ld (which injects into the vendor image's zero-holes). Here we own the
 * whole image: it loads at 0x7E1B0000 exactly like the vendor app, the 32-byte descriptor is the first
 * bytes (start.S .descriptor), and the loader jumps to descriptor +0x14 = _start with r0 = the resident
 * symbol-resolver (see ../standalone_sapi_test.md RESULT 4/5/6).
 *
 * The sAPI dispatch table lives at 0x7E1E2000..0x7E1E2864 — BSS the loader/app zeroes, NOT loaded bytes
 * (RESULT 6: it sits past the vendor image end). Our image must stay well below it; the ASSERT enforces
 * that so a growing image can never overlap the table we are about to fill.
 *
 * .bss is NOLOAD (kept out of the flat binary); _start zeroes it (the loader does not zero our globals).
 */
ENTRY(_start)

SAPI_TABLE_BASE = 0x7E1E2000;   /* first sAPI slot; nothing we load may reach here */

SECTIONS
{
  . = 0x7E1B0000;

  /* 32-byte boot descriptor MUST be first (loader reads +0x14 for the entry). */
  .descriptor : { KEEP(*(.descriptor)) }

  .text   : { *(.text .text.*) }
  .rodata : { *(.rodata .rodata.*) }
  .data   : { *(.data .data.*) }

  __image_end = .;              /* end of loaded bytes (compressed into the OTA payload) */

  .bss (NOLOAD) : {
    . = ALIGN(4);
    __bss_start = .;
    *(.bss .bss.* COMMON)
    . = ALIGN(4);
    __bss_end = .;
  }

  /DISCARD/ : { *(.ARM.exidx*) *(.ARM.attributes) *(.comment) *(.note*) }

  /* reliability.c keeps its RAM reset trace at 0x7E1E1B00 */
  ASSERT(__bss_end <= 0x7E1E1B00, "BSS overlaps the reset trace at 0x7E1E1B00")
  ASSERT(__image_end < SAPI_TABLE_BASE,
         "standalone image overlaps the sAPI table BSS at 0x7E1E2000")
}
