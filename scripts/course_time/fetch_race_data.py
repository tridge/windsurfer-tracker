#!/usr/bin/env python3
"""Download one race day's data from a tracker server for course-time modelling.

  fetch_race_data.py --event 11 --date 2026-11-15 --out DAYDIR

Writes DAYDIR/{summary.json, track.jsonl, wind.jsonl, users.json, course.json, courses.json}
and a starter DAYDIR/config.json (edit it: buoy trackers, finish line, guns, exclusions).
"""
import argparse, gzip, json, os, sys, urllib.request

def get(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'course-time-model', 'Cache-Control': 'no-cache'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--event', type=int, required=True)
    ap.add_argument('--date', required=True, help='YYYY-MM-DD')
    ap.add_argument('--out', required=True)
    ap.add_argument('--base', default='https://wstracker.org')
    ap.add_argument('--log', help='log file name if not the one with sublogs, e.g. 2026_11_15.jsonl')
    a = ap.parse_args()
    d = a.date.replace('-', '_'); base = f"{a.base}/{a.event}/"
    os.makedirs(a.out, exist_ok=True)
    summary = json.loads(get(f"{base}logs/{d}_summary.json"))
    logs = summary['logs']
    log = next((l for l in logs if l['file'] == a.log), None) if a.log else \
        next((l for l in logs if l.get('sublogs')), None)
    if not log:
        sys.exit('no log with sublogs (races) found; mark the races as sublogs in the review page or pass --log')
    json.dump(summary, open(os.path.join(a.out, 'summary.json'), 'w'), indent=1)
    try:
        raw = gzip.decompress(get(f"{base}logs/{log['file']}.gz"))
    except Exception:
        raw = get(f"{base}logs/{log['file']}")
    open(os.path.join(a.out, 'track.jsonl'), 'wb').write(raw)
    for name, url in [('wind.jsonl', f"{base}logs/{d}_wind.jsonl"), ('users.json', f"{base}users.json"),
                      ('course.json', f"{base}course.json"), ('courses.json', f"{a.base}/api/event/{a.event}/courses")]:
        try:
            open(os.path.join(a.out, name), 'wb').write(get(url))
        except Exception as e:
            print(f"warning: {name}: {e}", file=sys.stderr)
    cfg = os.path.join(a.out, 'config.json')
    if not os.path.exists(cfg):
        json.dump({
            "event": a.event, "date": a.date, "log_file": log['file'],
            "races": {s['name']: {"gun": None, "pursuit": False} for s in log['sublogs']},
            "mark_trackers": {}, "finish_line": "course",
            "wind_offset_deg": 0, "sailors": None, "exclude": {}, "notes": ""
        }, open(cfg, 'w'), indent=1)
    print(f"{log['file']}: {len(log['sublogs'])} races -> {a.out}")

if __name__ == '__main__':
    main()
