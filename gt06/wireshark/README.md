# GT06 / W07C Wireshark dissector

A Wireshark **Lua** dissector that decodes the GT06-family GPS-tracker TCP protocol
(login, location, heartbeat, alarm/SOS, and the ASCII server-command / string-info
frames such as `STATUS#`, `cxzt#`, `Battery:3.47V`) field-by-field, with CRC-ITU
verification.

It matches the wire format implemented by this project's own server
(`server/protocol_GT06.py`); bit meanings follow the Concox GT06 v1.8.1 spec
(`gt06/GT06_protocol_v1.8.1.txt`). The protocol is plaintext — no keys involved.

Default port: **TCP 7711** (the GT06 listener). Use Wireshark's *Decode As* for any
other port.

## Files

| File | Purpose |
|------|---------|
| `gt06.lua` | The dissector. |
| `make_sample_pcap.py` | Emits one valid frame of each type for an instant demo / self-test. |
| `README.md` | This file. |

## Install

1. Find your **Personal Lua Plugins** folder: in Wireshark, *Help ▸ About Wireshark ▸
   Folders*, or run `tshark -G folders | grep -i plugin`. On Linux this is usually
   `~/.local/lib/wireshark/plugins/` (create it if missing).
2. Copy `gt06.lua` into that folder.
3. *Analyze ▸ Reload Lua Plugins* (Ctrl+Shift+L), or restart Wireshark.

No build step — it's pure Lua and works on any Wireshark with Lua enabled.

To load it ad-hoc without installing (handy for one-off captures):

```bash
wireshark -X lua_script:gt06.lua            # or
tshark    -X lua_script:gt06.lua -r capture.pcap
```

## Quick self-test

```bash
python3 make_sample_pcap.py > sample.hex
text2pcap -T 1024,7711 sample.hex sample.pcap
tshark -X lua_script:gt06.lua -r sample.pcap            # one line per frame
tshark -X lua_script:gt06.lua -r sample.pcap -V         # full field tree
```

Expected summary:

```
GT06 Login IMEI=866557081378657
GT06 Loc -35.28090,149.13000 sat=11 GPS
GT06 HB volt=5 sig=4 chg
GT06 CMD STATUS#
GT06 RESP Battery:3.47V
GT06 ALARM[SOS] -35.28090,149.13000 sat=9 GPS
```

## Capturing live traffic

The trackers connect to the **server**, so capture there, not on your laptop.

**Live, straight into Wireshark (SSH pipe):**

```bash
ssh tracker@wstracker.org "sudo tcpdump -U -s0 -w - 'tcp port 7711'" | wireshark -k -i -
```

(`-U` = unbuffered so frames appear immediately; `-s0` = full payload.)

**Capture to a file, then open locally:**

```bash
ssh tracker@wstracker.org "sudo tcpdump -s0 -w /tmp/gt06.pcap 'tcp port 7711'"   # Ctrl-C to stop
scp tracker@wstracker.org:/tmp/gt06.pcap .
wireshark gt06.pcap
```

Filter inside Wireshark with `gt06`, or e.g. `gt06.protocol == 0x12` (locations),
`gt06.imei`, `gt06.str.command`, `gt06.crc_ok == false`.

## Decoding historical / raw frames

If you have raw GT06 frame bytes as hex (e.g. from a log), wrap them in a pcap with
`text2pcap`. One packet per `text2pcap` "offset block" (offset resetting to `000000`
starts a new packet):

```bash
text2pcap -T 1024,7711 frames.hex frames.pcap
```

## Field / display-filter reference

Frame: `78 78 | LEN(1) | PROTO(1) | PAYLOAD | SERIAL(2) | CRC-ITU(2) | 0D 0A`
(the `79 79` 2-byte-length variant is also handled). A zero-payload frame is shown as
`ACK (<proto>)` — that's the server's `78 78 05 …` acknowledgement.

| Protocol | ID | Notes |
|----------|----|-------|
| Login | `0x01` | IMEI (BCD) |
| Location | `0x12` / `0x22` | datetime, lat/lon, sats, speed, course/status bits |
| Heartbeat/Status | `0x13` | terminal-info bits, voltage level (0–6), GSM (0–4) |
| Alarm/SOS | `0x16` / `0x23` | GPS block + LBS + status; W07C sends SOS as `0x16` |
| Server Command | `0x80` | ASCII command (server→device), e.g. `TIMER,60,300#` |
| String Info | `0x15` | ASCII response (device→server), e.g. `Battery:3.47V`, `cxzt#` reply |

Useful fields: `gt06.protocol`, `gt06.imei`, `gt06.loc.lat`, `gt06.loc.lon`,
`gt06.loc.sats`, `gt06.loc.fix`, `gt06.loc.speed`, `gt06.voltage_level`, `gt06.gsm`,
`gt06.term.charge`, `gt06.term.alarm`, `gt06.str.command`, `gt06.str.response`,
`gt06.serial`, `gt06.crc`, `gt06.crc_ok`.

## Notes & known quirks (W07C firmware)

- The dissector **displays** frames; it never rejects them on semantics. Some W07C
  units send a Location with `course_status = 0x0000` even on a valid fix — it will
  show as `no-fix` with hemisphere bits clear, which is the raw truth.
- Date/time and IMEI are decoded as the server does (raw binary bytes for the
  timestamp; BCD nibbles for IMEI), so they match the logs.
- Terminal-info bits are labelled per the GT06 v1.8.1 spec (bit2=charge, bits3–5=alarm,
  bit6=GPS-tracking). The project's server uses a slightly different reading in places;
  the raw byte is always shown so you can judge.
- `cxzt#` / `STATUS#` replies arrive in `0x15` string frames — read the full text in
  `gt06.str.response`.
