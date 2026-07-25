#!/usr/bin/env python3
"""Analyze tracker.log to estimate battery life per tracker.

Usage: battery_analysis.py <logfile> [logfile...] [-p pattern]

Parses log lines with bat=NN%[/V.VVVV][+].  Charging periods are excluded.

Drain is measured from the voltage field where available (mV resolution,
converted to SoC%% through the fitted OCV curve in gt06_soc) and only falls
back to the coarse 1%%-quantised bat=NN%% field otherwise.  Rates come from a
least-squares fit over each segment, so 1-unit dither does not masquerade as
repeated full-step discharges.
"""

import argparse
import re
import sys
from datetime import datetime, timedelta
from fnmatch import fnmatch
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gt06_soc import SocModel, gid_of

# bat=NN%[/V.VVVV V][+]  -- note the charging '+' trails the voltage
LINE_RE = re.compile(
    r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) '
    r'\[([^\]]+)\] '
    r'(.+?)'
    r'bat=(\d+)%(?:/(\d+\.\d+)V)?(\+?)'
)

# Above this the OCV curve is flat and post-charge surface charge is still
# decaying, so a voltage-derived drain rate reads far too low.
FLAT_TOP_V = 4.10

# A lower bound beyond this is just "no measurable drain", not a service life.
LIFE_CAP_H = 1000.0

# Plausible single-cell terminal voltage; a failed supervisor read logs 0.000V.
MIN_VALID_V = 2.5
MAX_VALID_V = 4.5


def natural_sort_key(s):
    parts = re.split(r'(\d+)', s)
    return [int(p) if p.isdigit() else p.lower() for p in parts]


def classify_state(text):
    """Determine state from the log line text between tracker ID and bat=."""
    if 'Idle heartbeat' in text:
        return 'idle'
    if 'GPS-wait heartbeat' in text or 'pos=' in text:
        return 'tracking'
    return None


def parse_start_time(text, now=None):
    """Parse a --start-time argument; the day defaults to today.

    Accepts 'HH:MM', 'HH:MM:SS', 'MM-DD HH:MM[:SS]', 'YYYY-MM-DD HH:MM[:SS]'
    and 'YYYY-MM-DD' (midnight).  'T' may be used in place of the space.
    """
    s = text.strip().replace('T', ' ')
    now = now or datetime.now()
    # Fill missing leading fields into the string rather than parsing a
    # yearless format (strptime's yearless default is deprecated in 3.15).
    for cand in (s, f'{now.year}-{s}', f'{now:%Y-%m-%d} {s}'):
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
            try:
                return datetime.strptime(cand, fmt)
            except ValueError:
                pass
    raise argparse.ArgumentTypeError(
        f'unrecognised time {text!r}; use HH:MM, "MM-DD HH:MM" or '
        f'"YYYY-MM-DD HH:MM"')


def parse_logs(logfiles, pattern, start=None):
    """Parse log files into per-tracker chronological sample lists.

    Each sample is (ts, bat_pct, volt_or_None, charging, state).
    Samples before `start` are discarded.
    """
    trackers = {}
    for logfile in logfiles:
        with open(logfile, errors='replace') as f:
            for line in f:
                m = LINE_RE.match(line)
                if not m:
                    continue
                ts_str, tracker_id, mid_text, bat_str, v_str, chg_str = m.groups()
                if not fnmatch(tracker_id, pattern):
                    continue
                state = classify_state(mid_text)
                if state is None:
                    continue
                ts = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
                if start is not None and ts < start:
                    continue
                volt = float(v_str) if v_str else None
                if volt is not None and not MIN_VALID_V <= volt <= MAX_VALID_V:
                    volt = None     # failed supervisor read reports 0.000V
                trackers.setdefault(tracker_id, []).append(
                    (ts, int(bat_str), volt, chg_str == '+', state))
    # Rotated logs are not clean daily slices -- they overlap heavily, so the
    # same reading arrives from several files.  Sort, then drop exact repeats.
    for tid, samples in trackers.items():
        samples.sort(key=lambda s: s[0])
        seen = set()
        deduped = []
        for s in samples:
            if s in seen:
                continue
            seen.add(s)
            deduped.append(s)
        trackers[tid] = deduped
    return trackers


def build_segments(samples, v_hyst=0.040, p_hyst=2, max_gap=timedelta(minutes=30),
                   smooth=timedelta(minutes=5)):
    """Split samples into contiguous non-charging, single-state runs.

    A run breaks on an explicit charging flag, a sustained rise above the run's
    baseline, a state change, or a long data gap.

    The rise test runs on a median over `smooth`, not on raw samples: under
    load the pack sags 30-80 mV on modem TX bursts, so every recovery from a
    sag looks like a charge to an instantaneous comparison -- which shreds a
    continuous race into fragments too short to fit.  Median-filtering leaves
    real charging (a sustained lift) clearly separated from sag rebound.

    Returns {'idle': [[sample, ...], ...], 'tracking': [...]}.
    """
    segments = {'idle': [], 'tracking': []}
    cur = []
    cur_state = None
    win = []            # (ts, value) inside the smoothing window
    win_is_v = False
    baseline = None     # running minimum of the smoothed signal

    def flush():
        nonlocal cur, cur_state, win, baseline
        if cur and cur_state:
            segments[cur_state].append(cur)
        cur, cur_state, win, baseline = [], None, [], None

    for s in samples:
        ts, bat, volt, charging, state = s
        val = volt if volt is not None else bat
        is_v = volt is not None
        hyst = v_hyst if is_v else p_hyst

        if win and is_v != win_is_v:     # signal changed under us
            win, baseline = [], None
        win_is_v = is_v
        win.append((ts, val))
        while win and ts - win[0][0] > smooth:
            win.pop(0)

        rising = False
        if len(win) >= 3 and ts - win[0][0] >= smooth * 0.5:
            level = median(v for _, v in win)
            if baseline is not None and level > baseline + hyst:
                rising = True
            baseline = level if baseline is None else min(baseline, level)

        gapped = cur and (ts - cur[-1][0]) > max_gap

        if charging or rising:
            flush()
            win_is_v = is_v
            continue        # drop the charging sample itself
        if gapped or (cur_state is not None and state != cur_state):
            flush()
            win, baseline = [(ts, val)], None

        if not cur:
            cur_state = state
        cur.append(s)

    flush()
    return segments


def bin_series(xs, ys, width):
    """Average (x, y) into fixed-width x buckets.

    Heartbeats arrive every ~15s and are strongly autocorrelated; binning
    first removes quantisation dither and makes the slope's standard error
    meaningful rather than wildly optimistic.
    """
    buckets = {}
    for x, y in zip(xs, ys):
        buckets.setdefault(int(x / width), []).append((x, y))
    out = []
    for k in sorted(buckets):
        pts = buckets[k]
        out.append((sum(p[0] for p in pts) / len(pts),
                    sum(p[1] for p in pts) / len(pts)))
    return [p[0] for p in out], [p[1] for p in out]


def lsq_slope(xs, ys, min_sigma=0.0):
    """Least-squares slope of ys vs xs and its standard error.

    min_sigma floors the residual scatter: a signal pinned to one quantisation
    step has zero residual and would otherwise imply infinite confidence in an
    arbitrarily small slope.  Uniform quantisation of step q has sd q/sqrt(12).

    Returns (slope, stderr) or None if the fit is degenerate.
    """
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    icept = my - slope * mx
    ssr = sum((y - (icept + slope * x)) ** 2 for x, y in zip(xs, ys))
    sigma = max((ssr / (n - 2)) ** 0.5, min_sigma)
    return slope, sigma / sxx ** 0.5


_SOC_CACHE = {}


def _soc_cached(model, gid, mode):
    """Memoised soc_of for one (unit, mode); voltages are 1 mV quantised."""
    key = (gid, mode)
    memo = _SOC_CACHE.setdefault(key, {})

    def soc(v):
        r = memo.get(v)
        if r is None:
            r = memo[v] = model.soc_of(gid, v, mode)
        return r
    return soc


def segment_rate(seg, model, gid, mode, force_source=None, bin_hours=0.25):
    """Fit a drain rate to one segment.

    Returns (rate_pct_per_hr, hours, source, mean_v, stderr) or None.
    'source' is 'V' for the voltage/SoC path or '%' for the raw percent field.
    """
    if (seg[-1][0] - seg[0][0]).total_seconds() <= 0:
        return None

    # Prefer voltage whenever its samples alone still cover most of the
    # segment; a unit that drops the field intermittently should not be
    # demoted to the coarse percent signal.
    have_v = [s for s in seg if s[2] is not None]
    v_span = ((have_v[-1][0] - have_v[0][0]).total_seconds() / 3600.0
              if len(have_v) >= 2 else 0.0)
    full_span = (seg[-1][0] - seg[0][0]).total_seconds() / 3600.0
    use_v = (force_source != 'pct' and model is not None
             and len(have_v) >= 10 and v_span >= 0.5 * full_span)
    if force_source == 'volt' and not use_v:
        return None

    pts = have_v if use_v else seg
    hours = (pts[-1][0] - pts[0][0]).total_seconds() / 3600.0
    if hours <= 0:
        return None
    t0 = pts[0][0]
    xs = [(s[0] - t0).total_seconds() / 3600.0 for s in pts]
    if use_v:
        soc = _soc_cached(model, gid, mode)
        ys = [soc(s[2]) for s in pts]
        mean_v = sum(s[2] for s in pts) / len(pts)
        # quantisation step = 1 mV, expressed in SoC% at this working voltage
        step = abs(model.soc_of(gid, mean_v + 0.001, mode) -
                   model.soc_of(gid, mean_v, mode))
        source = 'V'
    else:
        ys = [s[1] for s in pts]
        mean_v = None
        step = 1.0
        source = '%'

    bx, by = bin_series(xs, ys, bin_hours)
    fit = lsq_slope(bx, by, min_sigma=step / 12 ** 0.5)
    if fit is None:
        return None
    slope, stderr = fit
    return (-slope, hours, source, mean_v, stderr)


def compute_estimate(segs, model, gid, mode, min_hours, force_source):
    """Weighted-average drain across a state's qualifying segments.

    Returns a dict describing the estimate, or None if nothing qualified.
    """
    fits = []
    for seg in segs:
        r = segment_rate(seg, model, gid, mode, force_source)
        if r and r[1] >= min_hours:
            fits.append(r)
    if not fits:
        return None

    # Keep the estimate on one signal: if the voltage-based segments already
    # cover most of the observation, drop the coarse percent-only stragglers
    # rather than let them demote the whole aggregate.
    total = sum(f[1] for f in fits)
    v_fits = [f for f in fits if f[2] == 'V']
    if v_fits and sum(f[1] for f in v_fits) >= 0.8 * total:
        fits = v_fits

    obs = sum(f[1] for f in fits)
    rate = sum(f[0] * f[1] for f in fits) / obs
    source = 'V' if all(f[2] == 'V' for f in fits) else '%'
    vs = [f[3] for f in fits if f[3] is not None]
    mean_v = sum(v * f[1] for v, f in zip(vs, fits)) / obs if len(vs) == len(fits) else None
    # hours-weighted average shares each segment's SE the same way as its rate
    stderr = (sum((f[4] * f[1]) ** 2 for f in fits) ** 0.5) / obs

    # A slope that is not two sigma clear of zero is indistinguishable from
    # flat: report the detection limit as a bound, not a point estimate.
    floor = 2.0 * stderr
    bound = rate <= floor
    if bound:
        rate = floor
    # Near full charge the OCV curve is flat and surface charge is still
    # decaying, so a voltage fit understates the true drain.
    soft = source == 'V' and mean_v is not None and mean_v > FLAT_TOP_V

    return {'rate': rate, 'obs': obs, 'source': source, 'bound': bound,
            'soft': soft, 'mean_v': mean_v, 'stderr': stderr}


def format_cell(est):
    """Format an estimate as (life, rate, src, obs) display strings."""
    if est is None:
        return 'N/A', 'N/A', '-', 'N/A'
    life = 100.0 / est['rate']
    if est['bound']:
        # a bound past a few weeks says "flat", not a credible service life
        life_s = 'flat' if life > LIFE_CAP_H else f'>{life:.0f}h'
        rate_s = f'<{est["rate"]:.2f}%/hr'
    else:
        pre = '~' if est['soft'] else ''
        life_s = f'{pre}{life:.0f}h'
        rate_s = f'{pre}{est["rate"]:.2f}%/hr'
    return life_s, rate_s, est['source'], f'{est["obs"]:.1f}h'


def main():
    parser = argparse.ArgumentParser(description='Analyze tracker battery life from log files')
    parser.add_argument('logfiles', nargs='+', help='Path(s) to tracker.log (oldest first)')
    parser.add_argument('-p', '--pattern', default='*',
                        help='fnmatch pattern for tracker IDs (default: *)')
    parser.add_argument('--start-time', type=parse_start_time, metavar='TIME',
                        help='ignore samples before TIME: "HH:MM" (today), '
                             '"MM-DD HH:MM" (this year) or "YYYY-MM-DD HH:MM"')
    parser.add_argument('--min-hours', type=float, default=1.0,
                        help='minimum segment length to use (default: 1.0)')
    parser.add_argument('--max-gap', type=float, default=30.0,
                        help='minutes of silence that ends a segment (default: 30)')
    parser.add_argument('--source', choices=['auto', 'volt', 'pct'], default='auto',
                        help='drain signal to use (default: auto = volt when present)')
    args = parser.parse_args()

    force = None if args.source == 'auto' else args.source
    try:
        model = SocModel()      # still wanted for the expected-drain column
    except Exception as e:                                  # noqa: BLE001
        print(f'warning: SoC model unavailable ({e}); using bat%% only', file=sys.stderr)
        model = None

    trackers = parse_logs(args.logfiles, args.pattern, args.start_time)
    if not trackers:
        since = f' since {args.start_time}' if args.start_time else ''
        print(f'No matching trackers found for pattern "{args.pattern}"{since}')
        sys.exit(1)

    max_gap = timedelta(minutes=args.max_gap)
    results = []
    for tid in sorted(trackers.keys(), key=natural_sort_key):
        samples = trackers[tid]
        gid = gid_of(tid) or tid
        segments = build_segments(samples, max_gap=max_gap)
        idle_est = compute_estimate(segments['idle'], model, gid, 'idle',
                                    args.min_hours, force)
        track_est = compute_estimate(segments['tracking'], model, gid, 'track',
                                     args.min_hours, force)
        last = samples[-1]
        expect = ''
        if model:
            expect = (f'{model.expected_drain_pct_h(gid, "idle"):.2f}/'
                      f'{model.expected_drain_pct_h(gid, "track"):.2f}')
        results.append((tid, last[1], last[2], last[3], idle_est, track_est, expect))

    name_w = max(max(len(r[0]) for r in results), len('Tracker'))
    hdr = (f'{"Tracker":<{name_w}}  Bat%    Volt  {"Idle Life":>9} {"Rate":>10} {"Src":>3}'
           f' {"Obs":>5}  {"Track Life":>10} {"Rate":>10} {"Src":>3} {"Obs":>5}'
           f'  {"Model i/t":>11}')
    first = min(s[0][0] for s in trackers.values())
    last_ts = max(s[-1][0] for s in trackers.values())
    print(f'Window: {first:%Y-%m-%d %H:%M} -> {last_ts:%Y-%m-%d %H:%M} '
          f'({(last_ts - first).total_seconds() / 3600:.1f}h)'
          + (f'  [--start-time {args.start_time:%Y-%m-%d %H:%M}]'
             if args.start_time else ''))
    print(hdr)
    print('-' * len(hdr))

    for tid, bat, volt, chg, idle_est, track_est, expect in results:
        il, ir, isrc, io = format_cell(idle_est)
        tl, tr, tsrc, to = format_cell(track_est)
        bat_str = f'{bat}%' + ('+' if chg else '')
        v_str = f'{volt:.3f}V' if volt is not None else '-'
        print(f'{tid:<{name_w}}  {bat_str:>5}  {v_str:>6}  {il:>9} {ir:>10} {isrc:>3}'
              f' {io:>5}  {tl:>10} {tr:>10} {tsrc:>3} {to:>5}  {expect:>11}')

    print()
    print('Life = estimated hours from 100% to 0% (extrapolated from observed drain)')
    print('Src  = signal the rate came from: V = voltage via the OCV->SoC curve')
    print('       (~1mV steps), % = the coarse 1%-quantised bat% field')
    print('>/< = drain not 2-sigma clear of zero over this window (bound; "flat" = unmeasurable)')
    print('~   = pack above %.2fV: OCV curve is flat and surface charge still decaying,'
          % FLAT_TOP_V)
    print('      so the measured rate understates the true drain')
    print('Model i/t = SoC-model expected idle/track drain %/hr for this unit')
    print('Obs = hours of observation used for the estimate')
    print('+ after Bat% = currently charging')


if __name__ == '__main__':
    main()
