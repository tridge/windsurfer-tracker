# GT06 allowed carriers

The WSTR standalone firmware can restrict operator selection to a per-tracker list of numeric PLMN
codes. The default is `50501,50502` (Telstra and Optus), for the Simbase SIMs used in these trackers.
The list is a device-owned setting and is not overwritten by the server's tracking/idle reconciliation.

In `manage.html`, open **GT06 Trackers**, use the tracker's **≡** button to open **Params**, then
**Fetch** its parameters. Under **Cell**, edit **Allowed carriers (PLMNOK)**. Enter comma-separated
5 or 6 digit codes, without spaces or duplicates, up to 31 characters. Blank explicitly restores
unrestricted automatic operator selection. The UI displays the known Australian carrier names.
Choose codes supported by the installed SIM and the country in which the tracker will operate.

Editing requires a live `CELLSTAT` reply advertising `policy1`. Older WSTR firmware exposes a RAM-only
scan filter under the same name; it must be upgraded before the UI enables this persistent setting.
Vendor firmware is not supported. A disconnected tracker cannot receive a new list remotely; its
existing saved list continues to govern local recovery without the server.

## Firmware behaviour

- Save validated changes in alternating `c:/wstrplmn.0` and `c:/wstrplmn.1` records with a generation
  and CRC. Verify the write before acknowledging it. On boot, use the newest valid record, falling
  back to the other after a torn write. With neither valid, use the compiled Telstra/Optus default.
- Read registration and operator state through the resident network getters every 30 seconds,
  including while TCP is connected. `sAPI_NetworkGetCops` supplies selection mode and operator;
  `sAPI_NetworkGetCpsi` supplies numeric PLMN if COPS uses an operator name. Polling does not put AT
  response text in the shared GPS URC stream.
  See the [SIMCom network API reference](https://github.com/SIMComWireless/SIMCom-Smart-Docs/blob/main/docs/LTE/OpenSDK/demo_network.md)
  and the [recorded hardware API checks](../gt06/Firmware/standalone/usb_api_findings.md).
- With a nonempty list, request manual selection only of listed PLMNs using the existing
  `AT+COPS=1,2,"<PLMN>"` path. Pin an already registered allowed operator in place. Ordinary cell
  handovers within that operator continue to work. Do not restore unrestricted automatic selection
  after reconnecting, and do not run the legacy automatic-selection recovery ladder.
- Correct a known disallowed operator or automatic selection when the policy runs. Allow 120 seconds
  after a selection request for registration to settle. After losing service on a previously working
  allowed operator, allow a further 300-second outage grace period before trying alternatives.
- Try each listed operator once per recovery round, then wait one hour before another round. The
  modem stays in manual selection during that backoff. If no allowed carrier is available, remain
  offline and retain tracking data through the existing blind buffer.
- A registered allowed modem with a broken server route does not trigger carrier cycling. Settings
  changes wait five seconds after the change so their confirming read can finish; subsequent status
  polling does not extend that delay.
  Network selection does not change GNSS power, and night sleep waits for carrier policy to settle.

This controls selection after the customer application starts. The resident modem may attempt
registration before application startup; this feature does not rewrite the SIM's preferred/forbidden
network files or claim to prevent those earlier attempts. The direct getters and AT selection path
were previously exercised on hardware; the combined restriction policy requires bench validation
before deployment. The unproven direct operator setter API is not used.

## Diagnostics and tests

`CXCS#CELLSTAT` returns the latest registration state (`reg`), numeric operator (`plmn`), requested
manual state (`man`), allowed list (`ok`, or `any`), capability (`policy1`) and observed COPS `mode`.
Mode `1` means manual selection; `-1` means the getter could not supply it. Firmware logs distinguish
blocked operators, selection requests, successful allowed registration and exhausted-list backoff.
Existing `STATUS#` LTE telemetry reports carrier, cell and signal data to the server.

Run `make test-plmn-policy` in `gt06/Firmware/standalone` for ASan SITL validation of malformed lists,
reboot persistence, torn writes, failed writes, Vodafone rejection, failed-first-carrier rotation,
offline denial recovery, bounded attempts when no allowed network is available, night sleep after
service returns during backoff, and applying settings despite continuous status polling. Run
`python3 -m pytest -q test/test_gt06_plmn.py` for the management validation and capability gate.
