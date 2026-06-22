#!/usr/bin/env python3
"""Extract the full-discharge battery dataset from the raw daily position logs
into gt06/battery_data/ — a compact, checked-in, reproducible dataset that the
SoC-curve fitter (gt06_fit_soc_curve.py) operates on.

The raw event logs are 1 Hz and huge; bat_v only moves in ~10 mV steps so 1 Hz
massively oversamples the voltage trajectory. We bin per (id, BIN seconds) and
keep the median bat_v plus enough state (charging, speed, sats, count) to know
the unit was tracking off-charge. Output is one gzipped CSV + a meta.json.

  python3 gt06_extract_battery_data.py \
      --logs 2026_06_18.jsonl.gz 2026_06_19.jsonl.gz 2026_06_20.jsonl.gz 2026_06_21.jsonl.gz \
      --unplug '2026-06-18 17:30' --tz 10 --power-w 0.381 --track-ma 115 \
      --out-dir gt06/battery_data
"""
import argparse, gzip, json, os, statistics, time, calendar
from collections import defaultdict


def openf(p):
    return gzip.open(p, 'rt', errors='replace') if p.endswith('.gz') else open(p, errors='replace')


def _ep(s, tzoff):
    return calendar.timegm(time.strptime(s, '%Y-%m-%d %H:%M')) - tzoff


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--logs', nargs='+', required=True)
    ap.add_argument('--unplug', required=True, metavar='YYYY-MM-DD HH:MM')
    ap.add_argument('--tz', type=float, default=10.0, help='log local tz offset hours')
    ap.add_argument('--power-w', type=float, default=0.381)
    ap.add_argument('--track-ma', type=float, default=115.0)
    ap.add_argument('--bin', type=int, default=60, help='downsample bin seconds')
    ap.add_argument('--out-dir', default='gt06/battery_data')
    a = ap.parse_args()

    tzoff = a.tz * 3600
    unplug = _ep(a.unplug, tzoff)
    BIN = a.bin
    # (id, bin) -> lists
    bins = defaultdict(lambda: {'v': [], 'chg': 0, 'spd': [], 'sat': [], 'n': 0})
    for fn in a.logs:
        for line in openf(fn):
            try:
                d = json.loads(line)
            except Exception:
                continue
            g, ts, bv = d.get('id'), d.get('ts'), d.get('bat_v')
            if not (g and ts and bv):
                continue
            b = int(ts // BIN)
            rec = bins[(g, b)]
            rec['v'].append(bv)
            rec['n'] += 1
            if d.get('chg'):
                rec['chg'] = 1
            if d.get('spd') is not None:
                rec['spd'].append(d['spd'])
            if d.get('nsats') is not None:
                rec['sat'].append(d['nsats'])

    os.makedirs(a.out_dir, exist_ok=True)
    rows = []
    for (g, b), rec in bins.items():
        rows.append((g, b * BIN, round(statistics.median(rec['v']), 4), rec['chg'],
                     round(statistics.median(rec['spd']), 1) if rec['spd'] else '',
                     int(statistics.median(rec['sat'])) if rec['sat'] else '',
                     rec['n']))
    rows.sort(key=lambda r: (r[0], r[1]))

    out_csv = os.path.join(a.out_dir, 'tracking.csv.gz')
    with gzip.open(out_csv, 'wt') as f:
        f.write('id,ts,bat_v,chg,spd,nsats,n\n')
        for r in rows:
            f.write(','.join(str(x) for x in r) + '\n')

    ids = sorted(set(r[0] for r in rows))
    meta = {
        'source_logs': a.logs,
        'unplug_epoch': int(unplug),
        'unplug_local': a.unplug,
        'tz_offset_h': a.tz,
        'power_w': a.power_w,
        'track_current_ma': a.track_ma,
        'bin_seconds': BIN,
        'n_units': len(ids),
        'n_rows': len(rows),
        'note': ('Full-discharge run: unplugged at unplug_epoch, tracked ~1 Hz at '
                 'constant power_w until each cell hit low-voltage cutoff. bat_v is the '
                 'per-bin median terminal voltage; chg=1 if any sample in the bin was '
                 'charging. SoC is reconstructed by the fitter (constant-power charge '
                 'integration). Units that reached cutoff are the firm anchors.'),
    }
    with open(os.path.join(a.out_dir, 'meta.json'), 'w') as f:
        json.dump(meta, f, indent=1)
    print(f"wrote {out_csv}: {len(rows)} rows, {len(ids)} units")
    print(f"wrote {os.path.join(a.out_dir, 'meta.json')}")


if __name__ == '__main__':
    main()
