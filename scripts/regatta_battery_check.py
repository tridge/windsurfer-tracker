#!/usr/bin/env python3
"""Regatta-test battery consistency check: EXPECTED battery usage (from the GT06
Battery-tab Max-track / Max-idle hours and the track/idle time the regatta
controller logged) vs ACTUAL usage (from the device voltage drop over the same
window).

  Expected drain%  = (T_track/MaxTrackH + T_idle/MaxIdleH) * 100
  MaxTrackH        = (capacity_mAh/1000 * nominal_V) / mode_power_w.track
  MaxIdleH         = (capacity_mAh/1000 * nominal_V) / mode_power_w.idle
                     (identical to WebUI/manage.html Battery tab)

  Actual drain%    = SoC(V_start) - SoC(V_end)
                     SoC = the parametric OCV fit (gt06/battery_data/soc_fit.json,
                     33-unit full-discharge) — each track-phase terminal voltage is
                     reconstructed to cell OCV (per-unit divider offset + class IR
                     add-back at I_track) and run through the fitted curve.
                     V_start/V_end are the median of the first/last few TRACK-phase
                     voltages, so both are same-load — no idle-vs-track sag mixing.

Inputs: the calibration JSON, the regatta controller log (the track/idle
schedule), and tracker.log (the voltage time series — its '<id> battery voltage:
X.XXV' lines, attributed by sailor_id and covering BOTH idle and track phases, so
the overnight idle is included; pass the full log or a grep extract of those
lines). The window is the current regatta-test session start .. each unit's last
reading.
"""
import os
import re
import json
import argparse
import datetime
import statistics
from zoneinfo import ZoneInfo

# tracker.log timestamps are server-local AEST (UTC+10), == the Brisbane schedule
# offset in June, so no conversion is needed between the two.
TZ = ZoneInfo("Australia/Brisbane")


class Cal:
    """Port of BatteryCal (WebUI/js/battery_cal.js) + the Battery-tab runtime math.

    SoC uses the parametric OCV fit (gt06/battery_data/soc_fit.json, 33-unit
    full-discharge); the legacy single-cell (G226122) discharge_curve is retired."""
    def __init__(self, doc, fit):
        self.doc = doc
        self.units = doc.get("units", {})
        self.defaults = doc.get("defaults", {})
        self.nomV = doc.get("nominal_voltage", 3.7)
        self.mp = doc.get("mode_power_w", {})
        self.i_track = (doc.get("track_current_ma") or 0) / 1000.0
        self.fit = fit
        self.fc = fit["coeffs"]
        self.foff = fit.get("offsets_mv", {})   # fit's gauge-fixed divider offsets
        self.fr = fit.get("class_r_ohm", {})

    def _u(self, uid):
        return self.units.get(uid) or self.defaults

    def cap_mah(self, uid):
        return self._u(uid).get("capacity_mah") or self.defaults.get("capacity_mah")

    def cap_class(self, uid):
        return self._u(uid).get("cap_class") or self.defaults.get("cap_class", "?")

    def soc(self, uid, v_term):
        """SoC% from a TRACK-phase terminal voltage via the parametric OCV fit:
        reconstruct cell OCV (per-unit divider offset + class IR add-back at
        I_track), then SoC = c1*(1 - 1/(1+(OCV/c2)^c4)^c3), clamp [0,100]."""
        if v_term is None:
            return None
        c = self.fc
        ocv = (v_term + self.foff.get(uid, 0.0) / 1000.0
               + self.i_track * self.fr.get(self.cap_class(uid), 0.0))
        s = c["c1"] * (1.0 - 1.0 / (1.0 + (ocv / c["c2"]) ** c["c4"]) ** c["c3"])
        return max(0.0, min(100.0, s))

    def wh(self, uid):
        cap = self.cap_mah(uid)
        return cap / 1000.0 * self.nomV if cap else None

    def max_track_h(self, uid):
        wh, p = self.wh(uid), self.mp.get("track")
        return wh / p if wh and p else None

    def max_idle_h(self, uid):
        wh, p = self.wh(uid), self.mp.get("idle")
        return wh / p if wh and p else None


def _line_ts(line):
    try:
        return datetime.datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ).timestamp()
    except ValueError:
        return None


def parse_schedule(path):
    """[(epoch, mode)] from the regatta controller log. CMD TRACK->track;
    CMD IDLE / NIGHT idle / STOP -> idle."""
    events = []
    for line in open(path):
        if " AEST" not in line and " AEDT" not in line:
            continue
        ts = _line_ts(line)
        if ts is None:
            continue
        if "CMD TRACK" in line:
            events.append((ts, "track"))
        elif "CMD IDLE" in line or "NIGHT idle" in line or "STOP" in line:
            events.append((ts, "idle"))
    return sorted(events)


def session_start(path, gap=3600):
    """Epoch of the current regatta-test session start: the latest 'START' that
    is NOT a quick restart — i.e. preceded by >gap seconds of no controller STOP
    (a STOP+START within `gap` is treated as one continuous session)."""
    starts, stops = [], []
    for line in open(path):
        ts = _line_ts(line)
        if ts is None:
            continue
        if "regatta test START" in line:
            starts.append(ts)
        elif "regatta test STOP" in line:
            stops.append(ts)
    if not starts:
        return None
    starts.sort()
    stops.sort()
    for s in reversed(starts):
        prev = [x for x in stops if x < s]
        if not prev or (s - prev[-1]) > gap:
            return s
    return starts[0]


def phase_at(events, t):
    cur = "idle"
    for ts, m in events:
        if ts <= t:
            cur = m
        else:
            break
    return cur


def mode_hours(events, t0, t1):
    """(track_h, idle_h) spent in [t0, t1] per the schedule."""
    cur = phase_at(events, t0)
    track = idle = 0.0
    last = t0
    for ts, m in events:
        if ts <= t0 or ts >= t1:
            continue
        d = ts - last
        track += d if cur == "track" else 0
        idle += d if cur == "idle" else 0
        last, cur = ts, m
    d = t1 - last
    track += d if cur == "track" else 0
    idle += d if cur == "idle" else 0
    return track / 3600.0, idle / 3600.0


_BV = re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) .*?\b(G\d+) battery voltage: ([0-9.]+)V')


def voltage_series(path):
    """{sailor_id: [(ts, rawv), ...]} from tracker.log '<id> battery voltage: X.XXV'
    lines. These come from STATUS#/cxzt battery parses in BOTH idle and track phases
    (idle ~every keepalive), so the series spans the overnight idle — unlike the
    event jsonl, which only records track phases. rawv is the raw device voltage."""
    series = {}
    for line in open(path, errors="replace"):
        m = _BV.match(line)
        if not m:
            continue
        ts = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ).timestamp()
        series.setdefault(m.group(2), []).append((ts, float(m.group(3))))
    return {u: sorted(v) for u, v in series.items()}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--calibration", default="WebUI/gt06_calibration.json")
    ap.add_argument("--soc-fit", default="gt06/battery_data/soc_fit.json",
                    help="parametric OCV SoC fit")
    ap.add_argument("--regatta-log", required=True, help="regatta controller log")
    ap.add_argument("--tracker-log", required=True,
                    help="tracker.log (or a grep extract of its 'battery voltage:' lines)")
    ap.add_argument("--since", default=None,
                    help="window start 'YYYY-MM-DD HH:MM' (AEST); default = current session START")
    ap.add_argument("--units", default=None,
                    help="restrict to a comma-separated list of sailor ids (e.g. the 17 firm 6Ah)")
    ap.add_argument("--smooth", type=int, default=5,
                    help="median over first/last N track-phase readings (~90s each)")
    ap.add_argument("--flag", type=float, default=2.0, help="flag |actual-expected| over this %%")
    args = ap.parse_args()

    if not os.path.exists(args.soc_fit):
        raise SystemExit(f"soc fit not found: {args.soc_fit}")
    fit = json.load(open(args.soc_fit))
    cal = Cal(json.load(open(args.calibration)), fit)
    events = parse_schedule(args.regatta_log)
    series = voltage_series(args.tracker_log)
    if args.since:
        since = datetime.datetime.strptime(args.since, "%Y-%m-%d %H:%M").replace(tzinfo=TZ).timestamp()
    else:
        since = session_start(args.regatta_log) or 0

    only = set(args.units.replace(",", " ").split()) if args.units else None
    rows = []
    for u, sv0 in series.items():
        if only and u not in only:
            continue
        # endpoint voltages from TRACK-phase samples only: both ends are then in
        # the discharge curve's (tracking-voltage) domain, so no phase mixing and
        # no IR-sag normalisation is needed.
        tsv = [(t, v) for t, v in sv0 if t >= since and phase_at(events, t) == "track"]
        if len(tsv) < 2 * args.smooth:
            continue
        uid = u
        head, tail = tsv[:args.smooth], tsv[-args.smooth:]
        t0 = statistics.median(t for t, _ in head)
        t1 = statistics.median(t for t, _ in tail)
        v0 = statistics.median(v for _, v in head)
        v1 = statistics.median(v for _, v in tail)
        p0 = cal.soc(uid, v0)
        p1 = cal.soc(uid, v1)
        mt, mi = cal.max_track_h(uid), cal.max_idle_h(uid)
        if p0 is None or p1 is None or not mt or not mi:
            continue
        tt, ti = mode_hours(events, t0, t1)
        actual = p0 - p1
        expected = (tt / mt + ti / mi) * 100
        rows.append((uid, cal.cap_class(uid), (t1 - t0) / 3600.0, tt, ti,
                     v0, v1, actual, expected, actual - expected))

    if not rows:
        print("No units with enough voltage readings in the window.")
        return
    rows.sort(key=lambda r: -abs(r[9]))
    win = statistics.median(r[2] for r in rows)
    tt0, ti0 = rows[0][3], rows[0][4]
    since_s = datetime.datetime.fromtimestamp(since, TZ).strftime("%Y-%m-%d %H:%M")
    print(f"Regatta battery check — since {since_s}, window ~{win:.1f} h  "
          f"(track {tt0:.2f} h + idle {ti0:.2f} h)  [SoC: parametric OCV fit]")
    print(f"Expected drain% = T_track/MaxTrack + T_idle/MaxIdle ; Actual from voltage drop\n")
    print(f"{'unit':8s}{'cls':5s}{'win_h':>6s}{'trk_h':>6s}{'idl_h':>6s}"
          f"{'Vstart':>7s}{'Vend':>6s}{'act%':>6s}{'exp%':>6s}{'diff':>7s}")
    for uid, cls, wh, tt, ti, v0, v1, a, e, d in rows:
        flag = "  <==" if abs(d) > args.flag else ""
        print(f"{uid:8s}{cls:5s}{wh:6.1f}{tt:6.2f}{ti:6.2f}"
              f"{v0:7.2f}{v1:6.2f}{a:6.1f}{e:6.1f}{d:+7.1f}{flag}")
    diffs = [r[9] for r in rows]
    acts = [r[7] for r in rows]
    exps = [r[8] for r in rows]
    print(f"\n{len(rows)} units | actual mean {statistics.mean(acts):.2f}% "
          f"vs expected mean {statistics.mean(exps):.2f}% "
          f"| diff mean {statistics.mean(diffs):+.2f}% median {statistics.median(diffs):+.2f}%")
    print(f"flagged |diff|>{args.flag}%: {sum(1 for d in diffs if abs(d) > args.flag)} units")
    for cls in sorted({r[1] for r in rows}):
        cd = [r[9] for r in rows if r[1] == cls]
        ca = [r[7] for r in rows if r[1] == cls]
        print(f"  {cls:5s} {len(cd):2d} units | actual mean {statistics.mean(ca):5.2f}% "
              f"| diff mean {statistics.mean(cd):+.2f}% median {statistics.median(cd):+.2f}%")
    print("\nNote: positive diff = drained MORE than the power model predicts; "
          "negative = less. Short windows + load-sag make small diffs noisy.")


if __name__ == "__main__":
    main()
