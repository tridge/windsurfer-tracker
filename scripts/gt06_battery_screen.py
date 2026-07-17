#!/usr/bin/env python3
"""Active post-charge battery screen for the GT06 fleet.

Answers "which of the trackers I just charged do NOT really hold the charge
their voltage claims?" without waiting for one to collapse mid-race
(G375430/G347082, Yeppoon 2026: healthy 4.05 V at rest, 550-620 mV step the
moment tracking load came on, dead by early afternoon).

For each selected ONLINE idle unit, in paced batches:

  1. baseline  -- cxzt# probe -> resting *BT: millivolts (unit idle, GPS off)
  2. load on   -- TIMER,10,10# (10 s upload interval -> GPS + reporting on;
                  same ~115 mA regime as the calibration bench data; NOT 1 Hz,
                  whose TX duty depresses readings on healthy cells too)
  3. loaded    -- cxzt# probes under load -> loaded millivolts
  4. restore   -- TIMER,T1,T2# with the unit's own pre-test intervals
                  (captured from its cxzt F: field), unconditionally
  5. recovery  -- after a rest, cxzt# again -> recovered millivolts

Per-unit metrics and verdict:
  sag      = rest - min(loaded)   : >= --sag-mv (150) -> SUSPECT.  Healthy
             cells sag 40-90 mV at this load (June 2026 fleet data); the
             Yeppoon collapse cells sat at 550-620 mV.
  residual = rest - recovered     : >= --resid-mv (40) -> SUSPECT. A healthy
             cell rebounds to its resting voltage within minutes; a hollow
             top stays down (G375430 rested 630 mV below pre-race level
             through a 2 h break with GPS off).
  appR     = sag / 0.109 A        : reported for context (healthy ~0.4-0.8).

Run it the MORNING AFTER charging, not right off the charger: a weak cell can
read genuinely full immediately after a fresh full charge (G375430 passed
that state and delivered 3.4 Ah); the hollow reading develops with rest and
partial charges. Surface charge right off the charger also inflates rest V.

The test leaves each unit exactly as found (same TIMER intervals); ~3 min of
GPS per unit costs ~0.2 % battery. Never run during racing.

Usage:
  scripts/gt06_battery_screen.py --list                      # inventory + eligibility
  scripts/gt06_battery_screen.py --units G375356             # screen one unit
  scripts/gt06_battery_screen.py --all-idle --batch 10       # screen the fleet
  scripts/gt06_battery_screen.py --all-idle --dry-run        # show plan only
Password: --password, $WSTRACKER_MANAGER_PASSWORD, or prompt.
"""
import os
import sys
import re
import json
import time
import argparse
import getpass
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

_BT = re.compile(r'\*BT:(\d+)')
_F = re.compile(r'\*F:(\d+)\|(\d+)')
DELTA_A = 0.109              # track load minus idle load, for apparent R


class Api:
    def __init__(self, host, port, password):
        self.base = f"http://{host}:{port}"
        self.password = password

    def _req(self, path, method='GET', body=None):
        req = Request(self.base + path, method=method,
                      data=json.dumps(body).encode() if body is not None else None)
        req.add_header('X-Manager-Password', self.password)
        if body is not None:
            req.add_header('Content-Type', 'application/json')
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    def trackers(self):
        return self._req('/api/manage/gt06/trackers').get('trackers', [])

    def refresh(self, imei):
        return self._req(f'/api/manage/gt06/tracker/{imei}/refresh', 'POST', {})

    def command(self, imei, cmd):
        return self._req(f'/api/manage/gt06/tracker/{imei}/command', 'POST',
                         {'cmd': cmd})


class UnitTest:
    def __init__(self, t):
        self.imei = t['imei']
        self.gid = 'G' + self.imei[-6:]
        self.name = t.get('name') or ''
        self.fw = t.get('firmware') or ''
        self.cxzt0 = t.get('raw_cxzt') or ''
        m = _F.search(self.cxzt0)
        self.t1, self.t2 = (m.group(1), m.group(2)) if m else ('60', '1800')
        self.rest_mv = None
        self.load_mv = []
        self.rec_mv = None
        self.skip = None
        self.restored = False


def fresh_bt(api, ut, prev_raw, tries, wait):
    """Queue a cxzt# probe and wait for a NEW raw_cxzt; return (mv, raw)."""
    api.refresh(ut.imei)
    for _ in range(tries):
        time.sleep(wait)
        for t in api.trackers():
            if t['imei'] != ut.imei:
                continue
            raw = t.get('raw_cxzt') or ''
            if raw and raw != prev_raw:
                m = _BT.search(raw)
                if m:
                    return int(m.group(1)), raw
            break
    return None, prev_raw


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--host', default='wstracker.org')
    ap.add_argument('--port', type=int, default=41234)
    ap.add_argument('--password', default=None)
    ap.add_argument('--units', nargs='*', default=None,
                    help='unit ids/IMEIs to screen (G-labels or trailing digits)')
    ap.add_argument('--all-idle', action='store_true',
                    help='screen every online idle unit')
    ap.add_argument('--list', action='store_true',
                    help='just show inventory + eligibility')
    ap.add_argument('--batch', type=int, default=10, help='units per batch (default 10)')
    ap.add_argument('--sag-mv', type=float, default=150.0)
    ap.add_argument('--resid-mv', type=float, default=40.0)
    ap.add_argument('--warm-s', type=float, default=45.0,
                    help='wait after load-on before first loaded probe (default 45)')
    ap.add_argument('--load-probes', type=int, default=2,
                    help='cxzt probes under load (default 2)')
    ap.add_argument('--rest-s', type=float, default=300.0,
                    help='rest before the recovery probe (default 300)')
    ap.add_argument('--probe-tries', type=int, default=4,
                    help='polls per probe before giving up (default 4)')
    ap.add_argument('--probe-wait', type=float, default=15.0,
                    help='seconds between polls (default 15)')
    ap.add_argument('--dry-run', action='store_true',
                    help='show which units would be tested; send nothing')
    ap.add_argument('--json', default=None)
    args = ap.parse_args()

    password = (args.password or os.environ.get('WSTRACKER_MANAGER_PASSWORD')
                or getpass.getpass('Manager password: '))
    api = Api(args.host, args.port, password)

    try:
        inv = api.trackers()
    except (URLError, HTTPError) as e:
        raise SystemExit(f"can't reach server: {e}")

    def eligible(t):
        if not t.get('online'):
            return 'offline'
        if not t.get('idle'):
            return 'not idle (racing?)'
        if not t.get('raw_cxzt'):
            return 'no cxzt yet'
        return None

    if args.list or (not args.units and not args.all_idle):
        print(f"{'unit':8s} {'volts':>6s} {'idle':>5s} {'online':>6s}  eligibility")
        for t in sorted(inv, key=lambda t: t['imei'][-6:]):
            why = eligible(t)
            v = t.get('battery_voltage')
            print(f"G{t['imei'][-6:]:7s} {(f'{v:.2f}' if v else '-'):>6s} "
                  f"{str(bool(t.get('idle'))):>5s} {str(bool(t.get('online'))):>6s}  "
                  f"{why or 'OK'}")
        if not args.list:
            print("\nselect units with --units/--all-idle (see usage)")
        return

    picked = []
    for t in sorted(inv, key=lambda t: t['imei'][-6:]):
        if args.units is not None:
            digits = [re.sub(r'\D', '', u) for u in args.units]
            if not any(t['imei'].endswith(d) for d in digits if d):
                continue
        why = eligible(t)
        if why:
            print(f"skip G{t['imei'][-6:]}: {why}")
            continue
        picked.append(UnitTest(t))
    if not picked:
        raise SystemExit('no eligible units')

    print(f"screening {len(picked)} unit(s) in batches of {args.batch}"
          + (' [DRY RUN]' if args.dry_run else ''))
    if args.dry_run:
        for ut in picked:
            print(f"  {ut.gid}  restore=TIMER,{ut.t1},{ut.t2}#  ({ut.fw})")
        return

    results = []
    for i in range(0, len(picked), args.batch):
        batch = picked[i:i + args.batch]
        print(f"\n-- batch {i//args.batch + 1}: {', '.join(u.gid for u in batch)}")
        try:
            # 1. baseline resting voltage
            for ut in batch:
                mv, raw = fresh_bt(api, ut, ut.cxzt0, args.probe_tries, args.probe_wait)
                ut.cxzt0 = raw
                ut.rest_mv = mv
                if mv is None:
                    ut.skip = 'no baseline cxzt'
                    print(f"  {ut.gid}: no baseline reply, skipping")
                else:
                    print(f"  {ut.gid}: rest {mv} mV")
            # 2. load on
            for ut in batch:
                if not ut.skip:
                    api.command(ut.imei, 'TIMER,10,10#')
            time.sleep(args.warm_s)
            # 3. loaded probes
            for n in range(args.load_probes):
                for ut in batch:
                    if ut.skip:
                        continue
                    mv, raw = fresh_bt(api, ut, ut.cxzt0, args.probe_tries,
                                       args.probe_wait)
                    ut.cxzt0 = raw
                    if mv is not None:
                        ut.load_mv.append(mv)
                        print(f"  {ut.gid}: loaded {mv} mV")
        finally:
            # 4. restore original intervals NO MATTER WHAT
            for ut in batch:
                if ut.rest_mv is None:
                    continue
                try:
                    api.command(ut.imei, f'TIMER,{ut.t1},{ut.t2}#')
                    ut.restored = True
                except Exception as e:
                    print(f"  {ut.gid}: RESTORE FAILED ({e}) — "
                          f"send TIMER,{ut.t1},{ut.t2}# manually!")
        # 5. recovery
        time.sleep(args.rest_s)
        for ut in batch:
            if ut.skip or not ut.load_mv:
                continue
            mv, raw = fresh_bt(api, ut, ut.cxzt0, args.probe_tries, args.probe_wait)
            ut.cxzt0 = raw
            ut.rec_mv = mv
        results.extend(batch)

    print(f"\n{'unit':8s} {'rest':>6s} {'load':>6s} {'sag':>7s} {'appR':>5s} "
          f"{'recov':>6s} {'resid':>7s}  verdict")
    out = {}
    for ut in results:
        if ut.skip or not ut.load_mv or ut.rest_mv is None:
            print(f"{ut.gid:8s} {'-':>6s} {'-':>6s} {'-':>7s} {'-':>5s} {'-':>6s} "
                  f"{'-':>7s}  SKIP ({ut.skip or 'no loaded reading'})")
            continue
        sag = ut.rest_mv - min(ut.load_mv)
        resid = (ut.rest_mv - ut.rec_mv) if ut.rec_mv is not None else None
        appr = sag / 1000.0 / DELTA_A
        bad = []
        if sag >= args.sag_mv:
            bad.append(f'sag {sag} mV')
        if resid is not None and resid >= args.resid_mv:
            bad.append(f'residual {resid} mV')
        verdict = 'SUSPECT: ' + ', '.join(bad) if bad else 'PASS'
        print(f"{ut.gid:8s} {ut.rest_mv:6d} {min(ut.load_mv):6d} {sag:6d}mV "
              f"{appr:4.1f}R {(str(ut.rec_mv) if ut.rec_mv else '-'):>6s} "
              f"{(f'{resid}mV' if resid is not None else '-'):>7s}  {verdict}")
        out[ut.gid] = {'imei': ut.imei, 'rest_mv': ut.rest_mv,
                       'load_mv': ut.load_mv, 'rec_mv': ut.rec_mv,
                       'sag_mv': sag, 'resid_mv': resid, 'app_r': appr,
                       'restored': ut.restored, 'verdict': verdict}
    if args.json:
        with open(args.json, 'w') as f:
            json.dump(out, f, indent=1)
        print(f"wrote {args.json}")


if __name__ == '__main__':
    main()
