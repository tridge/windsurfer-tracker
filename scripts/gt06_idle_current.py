#!/usr/bin/env python3
"""Estimate per-unit / per-class idle (or sleep) current from the GT06 packet log.

No new capture needed: gt06.log already records every frame with a timestamp, so
the battery-voltage trajectory is on disk. This walks that log, maps
conn_id -> sailor_id via login frames, pulls every voltage reading from STATUS#
(`Battery:X.XXV`, 10 mV) and cxzt# (`*BT:<mV>`, 1 mV), windows them, converts each
to remaining-SoC via the empirical discharge curve (per-unit offset + cap-class
offset, same maths as WebUI/js/battery_cal.js), least-squares-fits SoC vs time, and
reports current = capacity_mah * (dSoC%/h) / 100.

Usage (run where gt06.log lives, e.g. on the server):
  gt06_idle_current.py --log gt06.log --cal gt06_calibration.json \
      --start 1782001144 [--end EPOCH] [--settle-min 10] [--min-points 5]

--settle-min skips the first N minutes after --start (surface-charge relaxation
after coming off charge). For a SLEEP run use the same tool over the overnight
window; readings are just sparser (one per wake).
"""
import argparse, json, re, struct
from collections import defaultdict

DIR_OUT = 0x80000000          # high bit of conn_id = server->device frame
_BATT = re.compile(rb'Battery:(\d+\.\d+)V')
_BT = re.compile(rb'\*BT:(\d+)')


def imei_to_sailor(imei_bytes, prefix):
    h = imei_bytes.hex().lstrip('0')
    if len(h) == 16:
        h = h[1:]
    return prefix + h[-6:] if len(h) >= 6 else None


def walk(path):
    """Yield (ts, conn_id_no_dir, is_out, proto, data) per framed record.

    gt06.log v2: 8-byte magic 'GT06LOG2', then 14-byte records (<dIH ts,conn_id,len)
    + frame. v1 (no magic, 10-byte header, no conn_id) can't be attributed per-unit.
    """
    with open(path, 'rb') as f:
        blob = f.read()
    n, HDR = len(blob), 14
    if blob[:8] == b'GT06LOG2':
        i = 8
    else:
        raise SystemExit('gt06.log is not v2 (no GT06LOG2 magic) — need conn_id to attribute units')
    while i + HDR <= n:
        ts, conn_id, ln = struct.unpack_from('<dIH', blob, i)
        i += HDR
        if ln == 0 or i + ln > n:
            break
        frame = blob[i:i + ln]
        i += ln
        if len(frame) < 6 or frame[0] != 0x78 or frame[1] != 0x78:
            continue
        length = frame[2]
        serial_off = 3 + length - 4
        data = frame[4:serial_off] if serial_off > 4 else b''
        yield ts, conn_id & ~DIR_OUT, bool(conn_id & DIR_OUT), frame[3], data


def load_cal(path):
    c = json.load(open(path))
    return (c.get('discharge_curve') or [], c.get('offsets') or {},
            c.get('class_curve_offset_mv') or {}, c.get('units') or {})


def remaining_pct(curve, v):
    if not curve:
        return None
    n = len(curve)
    if v >= curve[0]:
        return 100.0
    if v <= curve[-1]:
        return 0.0
    for k in range(1, n):
        if v > curve[k]:
            span = curve[k - 1] - curve[k]
            frac = (curve[k - 1] - v) / span if span > 0 else 0
            return (100 - (k - 1)) - frac
    return 0.0


def soc(curve, offsets, class_off_mv, units, sid, rawv):
    off = offsets.get(sid, 0.0)
    cls = units.get(sid, {}).get('cap_class')
    coff = (class_off_mv.get(cls, 0) or 0) / 1000.0
    return remaining_pct(curve, rawv + off - coff)


def linfit(xs, ys):
    """Least-squares slope (y per x) and count."""
    m = len(xs)
    sx = sum(xs); sy = sum(ys)
    sxx = sum(x * x for x in xs); sxy = sum(x * y for x, y in zip(xs, ys))
    d = m * sxx - sx * sx
    if d == 0:
        return None
    return (m * sxy - sx * sy) / d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--log', required=True)
    ap.add_argument('--cal', required=True)
    ap.add_argument('--start', type=float, required=True, help='off-charge epoch')
    ap.add_argument('--end', type=float, default=None)
    ap.add_argument('--settle-min', type=float, default=10.0)
    ap.add_argument('--min-points', type=int, default=5)
    ap.add_argument('--prefix', default='G')
    args = ap.parse_args()

    curve, offsets, class_off_mv, units = load_cal(args.cal)
    conn_sid = {}                          # conn_id -> sailor_id (latest login)
    series = defaultdict(list)             # sailor_id -> [(ts, rawv, src)]
    wstart = args.start + args.settle_min * 60

    for ts, cid, is_out, proto, data in walk(args.log):
        if is_out:
            continue
        if proto == 0x01:                  # login: refresh conn_id -> sailor_id
            sid = imei_to_sailor(data, args.prefix)
            if sid:
                conn_sid[cid] = sid
            continue
        if proto != 0x15:                  # voltages ride on 0x15 string replies
            continue
        sid = conn_sid.get(cid)
        if not sid or ts < wstart or (args.end and ts > args.end):
            continue
        m = _BT.search(data)
        if m:
            series[sid].append((ts, int(m.group(1)) / 1000.0, 'cxzt'))
            continue
        m = _BATT.search(data)
        if m:
            series[sid].append((ts, float(m.group(1)), 'status'))

    per_class = defaultdict(list)
    rows = []
    for sid, pts in series.items():
        pts.sort()
        if len(pts) < args.min_points:
            continue
        t0 = pts[0][0]
        xs = [(t - t0) / 3600.0 for t, _, _ in pts]   # hours
        socs = [soc(curve, offsets, class_off_mv, units, sid, v) for _, v, _ in pts]
        if any(s is None for s in socs):
            continue
        slope = linfit(xs, socs)                       # %/h (negative = draining)
        if slope is None:
            continue
        cap = units.get(sid, {}).get('capacity_mah')
        cls = units.get(sid, {}).get('cap_class', '?')
        ma = (-slope) / 100.0 * cap if cap else None
        span_h = xs[-1] - xs[0]
        dv = pts[-1][1] - pts[0][1]
        rows.append((sid, cls, len(pts), span_h, dv, slope, cap, ma))
        if ma is not None:
            per_class[cls].append(ma)

    rows.sort(key=lambda r: (r[1], r[0]))
    print(f"{'unit':8} {'cls':4} {'n':>3} {'hrs':>5} {'dV':>7} {'%/h':>7} {'cap':>5} {'mA':>7}")
    for sid, cls, n, h, dv, sl, cap, ma in rows:
        print(f"{sid:8} {cls:4} {n:>3} {h:>5.1f} {dv:>+7.3f} {sl:>+7.2f} "
              f"{cap if cap else '?':>5} {ma if ma is not None else float('nan'):>7.1f}")

    import statistics as St
    print("\nPer-class idle current (median across units):")
    for cls, vals in sorted(per_class.items()):
        if vals:
            print(f"  {cls}: n={len(vals)}  median={St.median(vals):.1f} mA  "
                  f"mean={St.mean(vals):.1f} mA  range {min(vals):.1f}-{max(vals):.1f}")


if __name__ == '__main__':
    main()
