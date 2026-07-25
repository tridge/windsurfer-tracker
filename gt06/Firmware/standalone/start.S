@ start.S — 32-byte boot descriptor + entry trampoline for the from-scratch customer_app.
@
@ The loader decompresses our image to 0x7E1B0000, reads the descriptor here, and calls +0x14 (_start)
@ with r0 = the resident symbol-resolver fn(hash32)->ptr (../standalone_sapi_test.md RESULT 4/5/6).
@ Descriptor layout is the decoded vendor one (RESULT 6): +0x08/+0x0c copied verbatim, +0x10/+0x18 are
@ name-string pointers, +0x14 is the only field the loader jumps to.
    .syntax unified
    .cpu    cortex-r5

    .section .descriptor,"a",%progbits
    .align  2
    .global _descriptor
_descriptor:
    .word   0                       @ +0x00
    .word   0                       @ +0x04
    .word   0x00002800              @ +0x08  main-thread stack size (verbatim from vendor)
    .word   0x0000001e              @ +0x0c  priority/version (verbatim from vendor)
    .word   _name_entry             @ +0x10  entry-name string
    .word   _start + 1              @ +0x14  ENTRY (Thumb bit set) — the loader jumps here
    .word   _name_app               @ +0x18  app-name string
    .word   0                       @ +0x1c

    .thumb
    .section .text.start,"ax",%progbits
    .global _start
    .thumb_func
_start:
    @ r0 = resident resolver. Stash it where init1 would (the thunk at 0x7e1b1cd0 reads this slot), so a
    @ resolver-replay table fill works; we also pass it to standalone_main for the bake-vs-replay choice.
    ldr     r1, =0x7E1E2444         @ RESOLVER_SLOT
    str     r0, [r1]

    @ Zero our .bss (the loader does not clear our globals).
    ldr     r1, =__bss_start
    ldr     r2, =__bss_end
    movs    r3, #0
1:  cmp     r1, r2
    bhs     2f
    str     r3, [r1]
    adds    r1, r1, #4
    b       1b
2:
    @ r0 still = resolver on entry to C.
    bl      standalone_main

    @ standalone_main should not return; if it does, park (watchdog may reset — acceptable for a probe).
3:  b       3b
    .ltorg

    .section .rodata.names,"a",%progbits
_name_entry:
    .asciz  "wstrkMain"
_name_app:
    .asciz  "WSTRK_STANDALONE"
