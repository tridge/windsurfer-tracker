# G306161 USB API mapping and proof

Tested on G306161's V1.11.2 resident firmware using the WDBG customer app on 2026-09-04. The live
resolver exported 460 `{hash, function}` pairs. A generated SIMCom `api_map` from a public SDK mirror
identifies 329 of them; every name independently known before finding that map agrees exactly.
Additional names from a newer SIMCom library were accepted by this resident's resolver too. SIMCom's
[official OpenSDK network API summary](https://github.com/SIMComWireless/SIMCom-Smart-Docs/blob/main/docs/LTE/OpenSDK/demo_network.md)
confirms the prototypes and required `sAPI_NetworkInit()` ordering used here; the exact hashes and
addresses are confirmed by the tracker itself.

An address below is a resident implementation address observed on this build. Production code should
use its resolver-filled slot where one exists, not hard-code that implementation address.

## GNSS

| API | Hash | Function | Live proof |
|---|---:|---:|---|
| `sAPI_GnssPowerStatusGet(void)` | `e6a5ffad` | `7e6db0fb` | Returned 0 off, 1 on, then 0 after shutdown. |
| `sAPI_GnssPowerStatusSet(power)` | `fd8af451` | `7e6db0c9` | Powered the receiver on and off; setter returned 0. |
| `sAPI_GnssNmeaDataGet(enable, mode)` | `ca11a2fa` | `7e6db255` | Enabled if03 NMEA output; later disabled it. |
| `sAPI_GnssNmeaSentenceSet(mask)` | `4fd63755` | `7e6db3f3` | Mask `0x11` emitted only GGA and RMC with a valid fix. Original `0x1d` restored. |
| `sAPI_GnssNmeaSentenceGet(void)` | `54f93ca9` | `7e6db755` | Returned the sentence-array pointer; bytes confirmed each mask. |
| `sAPI_GnssStartMode(mode)` | `0d86568c` | `7e6db2d5` | Hot-start mode 0 returned 0. |
| `sAPI_GnssApFlashGet/Set` | `b78672ae` / `aca9794a` | `7e6db24b` / `7e6db229` | Reversible 0 -> 1 -> 0 test succeeded. |
| `sAPI_GnssStateBindCB(cb)` | `03f107ae` | `7e6db0bd` | Binding NULL (safe unbind) returned 0. |

The live NMEA fix was `35 19.88657 S, 149 07.90916 E`, fix quality 1, 23 satellites, HDOP 0.64.
This proves that reducing the sentence set does not prevent fixes. It saves parsing/queue work, though
the GNSS RF power saving still comes from powering the receiver off.

## Sleep, PSM, and watchdog

| API | Hash | Function | Status |
|---|---:|---:|---|
| `sAPI_SystemSleepSet(flag)` | `fd0619d9` | `7e6f3a0d` | Called: 0 -> 1 -> 0; getter tracked both transitions. |
| `sAPI_SystemSleepGet(void)` | `e629127d` | `7e6f3b35` | Called and proved. |
| `sAPI_SystemAlarmClock2Wakeup(ms)` | `b197aca7` | `7e6f3b3f` | Called in production firmware: a 60,000 ms alarm woke the sleeping module and TCP reconnected. |
| `sAPI_SystemSleepExSet(flag, time)` | `c6f8b2e1` | `7e6f3a4f` | Resolved only; the time unit/limits are not documented in the available header. |
| `sAPI_PSMSetT3324Timer(mode, text)` | `a777f0ef` | `7e6d6f2f` | Resolved. Mode 0 with `"0"` is the SDK's PSM-disable operation. |
| `sAPI_PSMSetT3412Timer(minutes)` | `4a030058` | `7e6d6ed7` | Resolved only. SDK range is 1..255 minutes. |
| `sAPI_EnableHardWarePSM(void)` | `4f5f0b77` | `7e6d6e8f` | Resolved only; deliberately not activated during exploration. |
| `sAPI_SoftWtdEnable(enable)` | `c9fe6afb` | `7e6f5e6b` | Disable call returned 0. |
| `sAPI_FalutWakeEnable(enable)` | `7a5fb2d5` | `7e6f5e4f` | Disable call returned 0. (Spelling is the exported ABI.) |
| `sAPI_SetWtdTimeOutPeriod(value)` | `22655d4c` | `7e6f5e41` | Resolved only. |
| `sAPI_FeedWtd(void)` | `7510198c` | `7e6f5e91` | Resolved only. |

The first overnight-idle implementation should use ordinary system sleep with TCP deliberately closed
and an alarm wake, not hardware LTE PSM. Before sleeping, confirm GNSS is off with the getter. On wake,
retain the idle state and do not run a generic reinitialisation path that powers GNSS. Hardware PSM can
be evaluated separately after its network/operator behaviour and recovery path are instrumented.

That path has now been exercised twice on G306161. A clean trace reported `SLEEP:seconds=0x3C`, remained
dormant for 60 seconds, then reported `SLEEP: awake, GPS remains off`, reconnected TCP, and accepted the
command disabling further test sleeps. Starting the tracker afterwards immediately resumed valid fixes
(24 satellites, HDOP 0.6), so the ordinary active path still re-enables GNSS as intended.

The first live attempt also exposed an independent timing-unit bug: resident `MsgQRecv` timeouts are in
5 ms ThreadX ticks, while `wd_sleep()` had passed its millisecond argument through unchanged. This made a
requested 61 seconds last about 305 seconds. `wd_sleep()` now rounds milliseconds up to 5 ms ticks; the
second image woke at the requested interval. The stalled first test was recovered using the USB download
agent's software reset, without a physical power cycle.

## LTE status

Network getters return failure/uninitialised data until `sAPI_NetworkInit()` has run. After calling it,
these APIs provided stable direct data without placing AT replies into the shared URC/NMEA queue:

| API | Hash | Function | Useful output |
|---|---:|---:|---|
| `sAPI_NetworkGetCpsi(SCcpsiParm *)` | `eacb3566` | `7e6d5ab5` | Online mode, PLMN, band, cell ID, TAC, RSRP, RSRQ, RSSI, SINR and physical-cell ID. |
| `sAPI_NetworkGetCops(char *)` | `2bfe7bba` | `7e6d5ebb` | `+COPS: 0,2,"50502",7` (numeric PLMN, LTE access technology). |
| `sAPI_NetworkGetCreg(int *)` | `126f7a00` | `7e6d5973` | 5: registered while roaming. |
| `sAPI_NetworkGetCgreg(int *)` | `41e775b1` | `7e6d5a2b` | 5: packet service registered while roaming. |
| `sAPI_NetworkGetCsq(uint8_t *)` | `db515814` | `7e6d5901` | Returned success but value 99 (unknown) during both snapshots. |
| `sAPI_NetworkGetCgatt(int *)` | `88e00a8e` | `7e6d6723` | 1: packet domain attached. |
| `sAPI_NetworkGetCgact(SCAPNact *)` | `8d638f00` | `7e6d624f` | CID 1 defined and active. |

Two snapshots proved that this is useful handover telemetry:

| PLMN | Band | TAC | Cell ID | Physical cell ID |
|---|---|---:|---:|---:|
| `505-02` | `EUTRAN-BAND3` | `0xCBE8` | 36519691 | 101 |
| `505-02` | `EUTRAN-BAND8` | `0xCBE8` | 36519754 | 448 |

The direct CPSI values matched read-only `AT+CPSI?` snapshots. Signal fields can change between calls,
so one structure should be sampled atomically and sent together. CSQ is inadequate on this network;
server logging should preserve CPSI's four raw signal indicators and perform presentation/conversion
server-side. PLMN `505-02` identifies the carrier without baking a carrier-name database into firmware.

`sAPI_NetworkGetCnetci` (`03c7d079`, `7e6d6395`) is also present and exposes neighbouring cells, but its
output is larger than WDBG's 256-byte scratch buffer. It was not called to avoid an output-buffer overrun.

## Runtime health

`sAPI_GetSystemInfo(&cpu, &heap)` (`1deee758`, `7e6f3b9b`) reported 99% CPU while WDBG was actively
servicing USB and 1,420,376 bytes free heap. `sAPI_GetTaskStackInfo` (`2c6189fd`, `7e6d2cb5`) was proved
against WDBG's own task handle: name `wstrkMai`, stack size 10,236 bytes, current use 220 bytes, peak
812 bytes. These are useful low-rate diagnostics for detecting leaks and eroding stack margin.
