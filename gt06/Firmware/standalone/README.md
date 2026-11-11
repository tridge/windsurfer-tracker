# standalone — from-scratch `customer_app` (our C only, no vendor app)

A full-image firmware that replaces the vendor application, over the resident RTOS + modem + GNSS +
loader HAL. Distinct from the earlier `fw-src` approach (injecting our C into the vendor image's
zero-holes), which is on the fwdev branch. Feasibility
and the boot ABI are established in `../standalone_sapi_test.md` (RESULT 4/5/6): the loader jumps to
descriptor `+0x14` with `r0` = a resident symbol-resolver, the sAPI dispatch table is app-filled BSS at
`0x7E1E2000`, and the kernel/byte-pool are already resident.

## Layout
- `link_standalone.ld` — image links at `0x7E1B0000`; 32-byte descriptor first; reserves the table BSS
  (ASSERT keeps loaded bytes below `0x7E1E2000`).
- `start.S` — the decoded 32-byte descriptor + `_start`: stash `r0`→`0x7E1E2444`, zero bss, call
  `standalone_main(resolver)`.
- `sapi.h` / `sapi_table.c` — fill the dispatch table by baking the captured constant pointers
  (`gen_table.py` → `sapi_table_baked.h`; its vendor input lives on the fwdev branch —
  the generated header is committed here so this tree builds standalone).
- `main.c` — `standalone_main`: STAGE 0 fills the table and parks.
- `usb_debug.c` / `usb_debug.py` / `usb_debug.md` — a separate if04 binary-debug firmware and host
  client for guarded memory reads, resolver lookups, and explicitly dangerous API calls.

## Build
```
make            # standalone_fw.bin           (OTA-loadable, XOR-padded)
make flashimg   # customer_app_standalone.bin (partition body, padded to 0x30000)
make usb-debug  # dedicated USB-debug flash zip + B53-named FOTA image
make test-usb-debug
```

## Status
- **STAGE 0 (done):** descriptor + entry + table bake. Builds; descriptor verified.
- **STAGE 1 (DONE — PROVEN LIVE on G375356, RESULT 7):** USB-VCOM marker probe. Flashed; if04 streamed
  both "DIRECT" (resident write VA `0x7e6916b9`) and "SLOT" (baked slot `0x7e1e2580`) markers in a steady
  heartbeat, device stable. Proves: loader boots our from-scratch descriptor, resident USB enumerates
  without app help, resident sAPI calls work by VA and via the baked table. RE (init2) showed the write is
  a single synchronous resident call — no bring-up needed.
- **STAGE 2 (DONE — PROVEN LIVE, RESULT 8):** the standalone opened a real TCP socket to the production
  GT06 server (wstracker.org:7711) via resident lwIP slots and completed a login handshake — server sent a
  login-ACK (`78780501`) and a 0x80 downlink command back. Data path is FREE: the resident firmware brings
  up cellular+PDP (CGACT=1,1, IP assigned) with no app code, so no init5/PPP replay was needed. Socket
  slots: socket idx214, connect idx153, send idx168, recv idx111, close idx185; sockaddr_in 16B BSD.
- **STAGE 3a (DONE — PROVEN LIVE, RESULT 9):** built a GT06 LOC frame in C (crc_itu/build_loc_data/
  gt06_frame, validated byte-identical to server/gt06_frames.py) and streamed proto-0x12 LOC over the
  logged-in socket. Server decoded it exactly: `G375356 IDLE LOC -35.33145,149.13182 sats=8`. (IDLE = server
  doesn't map-plot idle devices; the position is received + parsed — a server policy, not a firmware issue.)
- **STAGE 3b (PROVEN LIVE, RESULT 11):** resolver-replay table-fill (gen_pairs.py → sapi_pairs.h, 456 pairs)
  fills the COMPLETE table incl. below-0x7e1e2000 slots. GNSS pipeline works: power via sAPI → subscribe
  "URC QUEUE" (MsgQCreate 0x7e1e25d0) → attach (0x7e1e1f88) → MsgQRecv (0x7e1e1f18) → live NMEA
  RMC/GGA/GSA/GSV on if04. RMC void indoors (no lock without sky); GNSS gives UTC time. set_freq errors but
  isn't needed.
- **STAGE 3c (PROVEN LIVE indoors, RESULT 12):** full integrated tracker — socket connect+login (with
  reconnect for boot-time PDP-not-ready) + GNSS + "URC QUEUE" NMEA + RMC->deg parser, all from zero vendor
  code. Boots stable; server confirms a fresh G375356 login; RMC parsed live (void indoors). The only
  untested link is valid-fix → LOC → map, which needs sky view for a GPS lock.
- **NEXT:** outdoor GPS lock → real LOC on the map; then heartbeat frames, TERIID login, 0x80 command
  handling, blind buffer, OTA; refine sats (GGA), speed/course (RMC), real UTC datetime.

## Reliability behaviour (2026-11-08 review fixes)

- **Watchdog**: the resident-timer tick does no I/O at all; a boot that never reaches the main loop resets
  after 90 s; night sleep uses a bounded grace instead of disarming. Uptime starts at 1 (0 = "unset").
- **Reset reasons** in `c:/wdstat.bin`: 0 unexpected (power/watchdog/crash), 2 RESET#, 3 first boot,
  5 FOTA commit, 6 FOTA failure, 7 no-data, 8 GPS dead, 9 timer failure. `CXCS#BOOTSTAT` reports
  `rr bc up m0b m0 sm sp nd vh ff`; the server queries it once per connection, so reboots land in
  gt06.log. An M0 uptime (`m0b`) far above our uptime at boot means a SoC-only reset, not power loss.
- **Link**: the server ACKs every heartbeat, so no frame for `3*HBT+15` s drops and reconnects. Sends
  only happen when select says writable (never block); stuck for 20 s drops the link. Fixes sent since
  the last ACKed heartbeat go back into the blind buffer if the link dies (the server may see a few
  duplicates). SOS keeps resending (1 Hz x10, then every 10 s) until a 0x16 ACK.
- **Commands**: server frames must pass the GT06 CRC; TIMER/HBT/RESET/CANCELSOS must be exactly formed.
- **GPS**: tracking with no NMEA for 45 s power-cycles the receiver; after 4 cycles one reboot
  (not repeated for the same reason, never with an SOS or undelivered fixes).
- **Offline idle**: connect retries back off 5 s -> 300 s (reset when the modem re-registers); night
  sleep also works while offline (each wake makes one real attempt); no server link for 20 min awake
  (registered or not, incl. no SIM) -> radio off/on, 40 min -> reboot (doubling per consecutive no-data
  reboot, idle only). "Link" here means a login that went out, not a confirmed server reply.
- **Offline tracking/SOS**: no server link for 3 min -> radio off/on, then every 10 min while still
  offline; never a no-data reboot. The blind buffer holds 30 min at 1 Hz (RAM, lost on reboot).
- **Reset trace** (`wt` in BOOTSTAT): a RAM record at 0x7E1E1B00 written before every reset we cause,
  incl. the watchdog. Tested on G371074 2026-11-08: NOT retained across the resident warm reset (`wt-`
  after RESET#), so on HW a SoC-only reset (`m0b` >> `up`) stays "watchdog or resident crash". Works in SITL.
- **Diagnostics**: `CXCS#LINKSTAT` = heartbeat intervals, un-ACKed/requeued fixes, blind backlog, dropped
  links, busy sends, bad-CRC frames, connect failures, SOS state.
- **PLMN**: idle: 5 min grace, 2 min per allowed operator, a failed round ends with one CFUN 0/1, then a
  15 min holdoff. Tracking/SOS: 60 s for each of these. `CXCS#CELLSTAT` `ps` = operator selections,
  `nc` = no-data radio cycles.
- **Config**: primary then backup cwprm, each fully validated; default server port is 7711.
- **FOTA**: non-blocking connect, 10 min / 60 s-stall deadlines, HTTP 200/206 required, LZMA header and
  size checked before commit (a sanity check only: there is no LZMA decoder in the app, so a truncated
  stream with a valid CRC is not detected); failures before the flash is opened return to service.

## Flashing (software, no window-timing needed)
`AT+CFUN=1,1` to if05 opens arom on demand (as /dev/ttyACMn, n varies). Watch **any** raw `/dev/ttyACM*`
(strip `ls` color codes) and fire `sudo ./adownload -p ACMn -q <zip>` — flashes reliably (SIGABRTs after
"all finished", normal). The reboot DA is flaky → after flashing, **power-cycle once** to boot; don't rely
on `adownload -r`. Recover a stuck/off-bus device by physical replug.

## Flash / recovery (target: G375356)
Replaces the vendor app → the unit won't track during the test. Recover by reflashing the golden dump or
the reporter build over arom USB (`USBDEVFS_RESET` + `asrflash.py flash-app … --reboot`, long timeout).
