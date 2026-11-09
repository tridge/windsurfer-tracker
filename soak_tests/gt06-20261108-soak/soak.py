#!/usr/bin/env python3
"""24h+ tracking / idle / night-sleep soak of the event-2 GT06 fleet on our firmware.

Each ~2h cycle: TRACK 25 min -> IDLE 25 min -> SLEEP 60 min (event night-idle window, disconnected
sleep) -> IDLE 10 min (day idle restored). Injected: RESET# during track/idle/sleep, server-side
socket drops, two server restarts, one same-image OTA, and a start-all while asleep on even cycles.
Every check failure is an anomaly in anomalies.jsonl (and a line on stdout prefixed ANOMALY).
"""
import argparse
import datetime
import json
import random
import re
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUN = Path(__file__).resolve().parent
BASE = 'https://wstracker.org'
EID = 2
FW = 'WSTR-STD1_6be7199'
FOTA = 'YCSJ,http://103.230.158.49/W07_FW/W07_MG133_WSTR_B53_6be7199.bin,80#'
NIGHT_HBT = 600
DAY_HBT = 15
MGR = json.loads((ROOT / 'events.json').read_text())['manager_password']
ADMIN = subprocess.run(
    ['ssh', 'tracker@wstracker.org',
     f'python3 -c "import json;print(json.load(open(\'tracker/events.json\'))[\'events\'][\'{EID}\'][\'admin_password\'])"'],
    capture_output=True, text=True, check=True).stdout.strip()

lock = threading.Lock()
stop = threading.Event()
state = {}          # sid -> live info from the server log
expected = {}       # sid -> list of (time, kind) planned disruptions
phase = {'name': 'init', 'begin': time.time(), 'cycle': 0}
counts = {'anomalies': 0, 'resets': 0, 'disconnects': 0, 'restarts': 0, 'ota': 0, 'phases': 0}


def now():
    return time.time()


def iso(t=None):
    return datetime.datetime.fromtimestamp(t or now()).strftime('%Y-%m-%d %H:%M:%S')


def record(name, data):
    with lock, (RUN / f'{name}.jsonl').open('a') as f:
        f.write(json.dumps({'time': now(), 'iso': iso(), **data}) + '\n')


def anomaly(sid, kind, detail):
    counts['anomalies'] += 1
    record('anomalies', {'sid': sid, 'kind': kind, 'detail': detail, 'phase': phase['name'],
                         'cycle': phase['cycle']})
    print(f'ANOMALY {iso()} cycle {phase["cycle"]} {phase["name"]} {sid}: {kind}: {detail}', flush=True)


def api(path, data=None, admin=False, method=None):
    h = {'X-Admin-Password': ADMIN} if admin else {'X-Manager-Password': MGR}
    if data is not None:
        h['Content-Type'] = 'application/json'
    req = urllib.request.Request(BASE + path, headers=h, method=method,
                                 data=json.dumps(data).encode() if data is not None else None)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except Exception as e:
            err = e
            stop.wait(5)
    record('errors', {'path': path, 'error': str(err)})
    return {}


def trackers():
    rows = api('/api/manage/gt06/trackers').get('trackers', [])
    return {r['sailor_id']: r for r in rows if r.get('sailor_id') and r.get('eid') == EID}


def positions():
    return api(f'/{EID}/current_positions.json').get('sailors', {})


def command(sid, cmd):
    imei = state.get(sid, {}).get('imei')
    if imei:
        return api(f'/api/manage/gt06/tracker/{imei}/command', {'cmd': cmd})


def expect(sid, kind, window=900):
    expected.setdefault(sid, []).append((now(), kind, window))


def is_expected(sid, kinds, since=None):
    t = now()
    for (ts, kind, window) in expected.get(sid, []):
        if kind in kinds and t - ts < window and (since is None or ts >= since - 5):
            return True
    return False


def fleet_expected(kinds):
    return any(is_expected('*', kinds) for _ in [0])


# ---------------- server log monitor ----------------
def log_monitor():
    while not stop.is_set():
        proc = subprocess.Popen(
            ['ssh', '-o', 'BatchMode=yes', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
             'tracker@wstracker.org', 'tail -n 0 -F /home/tracker/tracker/tracker.log'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors='replace', bufsize=1)
        with (RUN / 'server-evidence.log').open('a', buffering=1) as ev:
            for line in proc.stdout:
                if stop.is_set():
                    break
                m = re.search(r'\b(G\d{6})\b', line)
                if not m or m.group(1) not in state:
                    if 'Traceback' in line or 'Error' in line and 'GT06' in line:
                        ev.write(line)
                        record('server_errors', {'line': line.strip()})
                    continue
                sid = m.group(1)
                ev.write(line)
                handle_line(sid, line.strip())
        proc.terminate()
        if not stop.is_set():
            record('errors', {'error': 'log monitor restarted'})
            stop.wait(5)


def handle_line(sid, line):
    t = now()
    st = state[sid]
    if re.search(r'Login: id .*-> ' + sid, line):
        with lock:
            st['logins'].append(t)
            st['connected'] = True
            st['last_login'] = t
    elif re.search(r'Disconnected: ' + sid + r' ', line):
        with lock:
            if t - st.get('last_login', 0) > 3:      # not the stale side closed after a new login
                st['connected'] = False
                st['disconnects'].append(t)
    m = re.search(r'Command ACK from ' + sid + r': (.*)', line)
    if m:
        val = m.group(1)
        if 'READOK:BOOTSTAT' in val:
            f = dict(re.findall(r'\b(rr|bc|up|m0b|m0|sm|nd|ff)(\d+)', val))
            bootstat(sid, {k: int(v) for k, v in f.items()}, val)
        elif 'READOK:GPSSTAT' in val:
            st['gps'] = {'time': t, **{k: int(v) for k, v in re.findall(r'\b([a-z]+)(-?\d+)\b', val)}}
        elif 'READOK:LINKSTAT' in val:
            linkstat(sid, val)
        elif val.startswith('Battery:'):
            g = re.search(r'GPS:(\w+)', val)
            st['status'] = {'time': t, 'gps': g.group(1) if g else None}
        elif 'ERROR' in val and not re.match(r'(MODE|SENDS|SENALM|MOVING|VIBCHK|ACCLINE|GPS_RST|GPSCODE)', val):
            if not val.startswith(('CXCS#GPS_RST', 'CXCS#GPSCODE', 'CXCS#VIBCHK', 'CXCS#ACCLINE')):
                record('cmd_errors', {'sid': sid, 'reply': val})
    if 'CRC mismatch' in line:
        anomaly(sid, 'crc_mismatch_at_server', line)
    if re.search(r'SOS|[Aa]larm', line) and 'SENALM' not in line:
        anomaly(sid, 'alarm_seen', line)


def bootstat(sid, f, raw):
    st = state[sid]
    prev = st.get('boot')
    st['boot'] = {'time': now(), **f}
    if prev is None:
        record('boots', {'sid': sid, 'first': True, 'raw': raw})
        return
    if f.get('bc', 0) == prev.get('bc', 0):
        return                                    # same boot, new connection
    rr = f.get('rr')
    power_loss = f.get('m0b', 0) < 2              # M0 restarted too -> power was lost
    kind = {2: 'reset', 5: 'ota'}.get(rr)
    ok = kind is not None and is_expected(sid, [kind])
    if ok:
        st['pending'] = None
    record('boots', {'sid': sid, 'rr': rr, 'bc': f.get('bc'), 'expected': ok,
                     'power_loss': power_loss, 'raw': raw})
    if not ok:
        anomaly(sid, 'unexpected_reboot',
                f'rr{rr} bc{prev.get("bc")}->{f.get("bc")} power_loss={power_loss} {raw}')
    if f.get('bc', 0) > prev.get('bc', 0) + 1:
        anomaly(sid, 'missed_reboots', f'bc{prev.get("bc")}->{f.get("bc")} (more than one boot)')


def linkstat(sid, val):
    st = state[sid]
    f = {k: int(v) for k, v in re.findall(r'\b(ua|rq|bl|ld|tb|crc|cf)(\d+)', val)}
    prev = st.get('link')
    st['link'] = {'time': now(), **f, 'raw': val}
    record('linkstat', {'sid': sid, **f, 'raw': val})
    if prev and st['boot'] and prev.get('time', 0) > st['boot']['time'] - 1:
        for k in ('ld', 'crc'):
            if f.get(k, 0) > prev.get(k, 0):
                anomaly(sid, f'linkstat_{k}_increased', f'{prev.get(k)}->{f.get(k)} {val}')


# ---------------- actions ----------------
def reset(sid, why):
    imei = state[sid]['imei']
    expect(sid, 'reset', 1800)
    state[sid]['pending'] = now()
    counts['resets'] += 1
    record('actions', {'action': 'reset', 'sid': sid, 'why': why})
    api(f'/api/manage/gt06/tracker/{imei}/reboot', {})


def reset_on_wake(sid, why, timeout=900):
    """RESET# is only accepted on a live link: wait for the sleeping unit's next wake login."""
    t0 = now()
    def run():
        while now() - t0 < timeout and not stop.is_set():
            st = state[sid]
            if st.get('connected') and st.get('last_login', 0) > t0:
                stop.wait(3)
                reset(sid, why)
                return
            stop.wait(1)
        record('errors', {'error': f'{sid} never woke for the queued reset'})
    threading.Thread(target=run, daemon=True).start()


def drop(sid):
    imei = state[sid]['imei']
    expect(sid, 'drop', 300)
    counts['disconnects'] += 1
    record('actions', {'action': 'server_disconnect', 'sid': sid})
    api(f'/api/manage/gt06/tracker/{imei}/disconnect', {})


def restart_server():
    for sid in state:
        expect(sid, 'restart', 300)
    counts['restarts'] += 1
    record('actions', {'action': 'server_restart'})
    r = subprocess.run(['sh', str(ROOT / 'restart_server.sh')], cwd=ROOT, capture_output=True, text=True)
    if r.returncode:
        record('errors', {'error': f'server restart failed: {r.returncode} {r.stderr.strip()}'})


def ota(sid):
    expect(sid, 'ota', 900)
    state[sid]['pending'] = now()
    counts['ota'] += 1
    record('actions', {'action': 'ota', 'sid': sid})
    command(sid, FOTA)


def set_sleep_window(on, minutes=60):
    if on:
        tz = datetime.datetime.now()
        start = tz.strftime('%H:%M')
        end = (tz + datetime.timedelta(minutes=minutes)).strftime('%H:%M')
        sched = {'enabled': True, 'night_idle': True, 'start': start, 'end': end}
    else:
        sched = {'enabled': False, 'night_idle': True, 'start': '22:00', 'end': '07:00'}
    record('actions', {'action': 'sleep_window', 'sched': sched})
    return api(f'/api/manage/event/{EID}', {'sleep_schedule': sched}, method='PATCH')


def all_state(active):
    record('actions', {'action': 'start-all' if active else 'stop-all'})
    api(f'/api/event/{EID}/admin/{"start-all" if active else "stop-all"}', {}, admin=True)


# ---------------- checks ----------------
def query_all(cmds, sids=None):
    for sid in (sids or list(state)):
        if (state[sid].get('row') or {}).get('online'):
            for c in cmds:
                command(sid, c)


def disrupted(sid, window=240):
    """A planned disruption in the last `window` s, or a reset/OTA queued but not yet seen."""
    st = state[sid]
    if st.get('pending'):
        return True
    t = now()
    return any(t - ts < window for (ts, kind, _) in expected.get(sid, []))


def check_online(rows, grace=90):
    t = now()
    for sid, row in rows.items():
        seen = row.get('last_seen') or 0
        if t - seen > grace and not disrupted(sid):
            key = ('offline', sid)
            st = state[sid]
            if st.get('offline_flag') is None:
                st['offline_flag'] = t
                anomaly(sid, 'offline', f'no traffic for {int(t - seen)}s')
        elif t - seen <= grace:
            state[sid]['offline_flag'] = None


def run_phase(name, seconds, cycle, active, events):
    """events: list of (offset_s, fn). Periodic checks run throughout."""
    phase.update(name=name, begin=now(), cycle=cycle)
    counts['phases'] += 1
    record('phases', {'cycle': cycle, 'phase': name, 'event': 'begin'})
    print(f'{iso()} cycle {cycle}: {name} ({seconds // 60} min)', flush=True)
    begin = now()
    events = sorted(events, key=lambda e: e[0])
    gps_first = {}
    last_q = last_snap = last_link = 0
    pos_ts = {}
    while now() - begin < seconds and not stop.is_set():
        el = now() - begin
        while events and events[0][0] <= el:
            _, fn = events.pop(0)
            try:
                fn()
            except Exception as e:
                record('errors', {'event_fn': str(e)})
        if now() - last_snap >= 30:
            last_snap = now()
            rows = trackers()
            rows = {s_: r_ for s_, r_ in rows.items() if s_ in state}
            for sid, row in rows.items():
                state[sid]['row'] = row
            if name != 'sleep' and not (name == 'idle-settle' and el < NIGHT_HBT + 120):
                check_online(rows)
            pos = positions()
            record('samples', {'cycle': cycle, 'phase': name, 'elapsed': int(el),
                               'devices': {s: {k: r.get(k) for k in ('online', 'last_seen', 'firmware', 'idle',
                                                                    'battery_voltage')}
                                           for s, r in rows.items()},
                               'pos': {s: {k: p.get(k) for k in ('ts', 'nsats', 'chg', 'bat_v', 'lat', 'lon')}
                                       for s, p in pos.items() if s in state}})
            for sid in state:
                p = pos.get(sid, {})
                ll = (p.get('lat'), p.get('lon'))
                woken = phase.get('wake_cmd', 0) >= begin   # start-all sent mid-sleep
                if name in ('idle', 'idle-settle', 'sleep') and el > 240 and not active and not woken:
                    prev = pos_ts.get(sid)
                    if prev and ll != prev and None not in ll and ll != (0, 0) and not disrupted(sid):
                        anomaly(sid, 'position_while_idle', f'{prev}->{ll}')
                pos_ts[sid] = ll
        if name != 'sleep' and el > 60 and now() - last_q >= 120:
            last_q = now()
            query_all(['CXCS#GPSSTAT'])
        if name != 'sleep' and now() - last_link >= 1800 and el > 120:
            last_link = now()
            query_all(['CXCS#LINKSTAT'])
        for sid in state:
            g = state[sid].get('gps')
            if g and g['time'] > begin + 60 and sid not in gps_first:
                gps_first[sid] = g
        stop.wait(1)
    # phase-end evaluation
    t = now()
    for sid, st in state.items():
        g = st.get('gps')
        if name in ('sleep', 'idle-settle'):
            continue
        if disrupted(sid):
            continue
        if not g or g['time'] < begin + 60:
            anomaly(sid, 'no_gpsstat', f'no GPSSTAT reply during {name}')
            continue
        want = 2 if active else 1
        if g.get('pwr') != want or g.get('req') != want:
            anomaly(sid, 'gps_state', f'{name}: pwr{g.get("pwr")} req{g.get("req")} (want {want})')
        first = gps_first.get(sid)
        if first and first is not g and g.get('nmea', 0) >= first.get('nmea', 0) and \
                g.get('vrmc', 0) >= first.get('vrmc', 0):
            if active and g.get('nmea', 0) <= first.get('nmea', 0):
                anomaly(sid, 'nmea_stalled', f'nmea {first.get("nmea")}->{g.get("nmea")} while tracking')
            if not active and g.get('nmea', 0) != first.get('nmea', 0) and g['time'] - first['time'] > 120:
                anomaly(sid, 'nmea_while_idle', f'nmea {first.get("nmea")}->{g.get("nmea")}')
    if active:
        pos = positions()
        stale = [s for s in state if t - (pos.get(s, {}).get('ts') or 0) > 120 and not disrupted(s)]
        record('fixes', {'cycle': cycle, 'stale_positions': stale,
                         'vrmc': {s: (state[s].get('gps') or {}).get('vrmc') for s in state}})
        for s in stale:
            v = (state[s].get('gps') or {}).get('vrmc', 0)
            if v and v > (gps_first.get(s) or {}).get('vrmc', 0):
                anomaly(s, 'fixes_not_published', f'vrmc advancing ({v}) but position stale')
    if name == 'idle-settle':
        rows = {s_: r_ for s_, r_ in trackers().items() if s_ in state}
        for s, r in rows.items():
            if t - (r.get('last_seen') or 0) > 90 and not disrupted(s):
                anomaly(s, 'not_back_after_sleep', f'no traffic {int(t - (r.get("last_seen") or 0))}s at settle end')
    record('phases', {'cycle': cycle, 'phase': name, 'event': 'end'})
    write_summary()


def sleep_phase(cycle, minutes, start_while_asleep):
    """Night-idle window: units must disconnect, wake ~every NIGHT_HBT, keep GPS off."""
    phase.update(name='sleep', begin=now(), cycle=cycle)
    begin = now()
    base_logins = {s: len(st['logins']) for s, st in state.items()}
    base_disc = {s: len(st['disconnects']) for s, st in state.items()}
    set_sleep_window(True, minutes)
    victim = random.choice(list(state))
    events = [(20 * 60, lambda: reset_on_wake(victim, 'at a sleep wake'))]
    if start_while_asleep:
        events.append(((minutes - 6) * 60, lambda: (all_state(True), phase.update(wake_cmd=now()))))
    run_phase('sleep', (minutes - 1) * 60, cycle, False, events)
    # evaluate sleep behaviour from the connection history
    for sid, st in state.items():
        logins = [t for t in st['logins'] if t >= begin + 180]
        discs = [t for t in st['disconnects'] if t >= begin]
        if len(discs) == 0:
            anomaly(sid, 'never_slept', 'no disconnect during the night-idle window')
            continue
        gaps = [b - a for a, b in zip([begin + 180] + logins, logins + [now()])]
        if gaps and max(gaps) > NIGHT_HBT + 300 and not (start_while_asleep and gaps.index(max(gaps)) == len(gaps) - 1):
            anomaly(sid, 'missed_wake', f'longest gap between check-ins {int(max(gaps))}s (HBT {NIGHT_HBT})')
        record('sleep_stats', {'cycle': cycle, 'sid': sid, 'wakes': len(logins), 'disconnects': len(discs),
                               'max_gap': int(max(gaps)) if gaps else None})
    set_sleep_window(False)
    if start_while_asleep:
        # every unit must be tracking within one night HBT of the start command
        t0 = phase.get('wake_cmd', begin)
        deadline = t0 + NIGHT_HBT + 180
        while now() < deadline and not stop.is_set():
            rows = {s_: r_ for s_, r_ in trackers().items() if s_ in state}
            if all(not r.get('idle') and r.get('online') for r in rows.values()):
                break
            stop.wait(30)
        rows = {s_: r_ for s_, r_ in trackers().items() if s_ in state}
        late = [s for s, r in rows.items() if r.get('idle')]
        record('wake_latency', {'cycle': cycle, 'seconds': int(now() - t0), 'late': late})
        for s in late:
            anomaly(s, 'slow_wake', f'still idle {int(now() - t0)}s after start-all')


def new_state(row):
    return {'imei': row['imei'], 'logins': [], 'disconnects': [], 'connected': row.get('online'),
            'boot': None, 'row': row}


def write_summary():
    with lock:
        summ = {'updated': iso(), 'phase': phase, 'counts': counts,
                'units': {s: {'online': (st.get('row') or {}).get('online'),
                              'fw': (st.get('row') or {}).get('firmware'),
                              'bc': (st.get('boot') or {}).get('bc'),
                              'logins': len(st['logins']), 'link': (st.get('link') or {}).get('raw')}
                          for s, st in state.items()}}
    tmp = RUN / 'summary.tmp'
    tmp.write_text(json.dumps(summ, indent=1, default=str))
    tmp.replace(RUN / 'summary.json')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cycles', type=int, default=13)
    ap.add_argument('--first-cycle', type=int, default=1)
    ap.add_argument('--restart-cycles', default='3,9')
    ap.add_argument('--min-units', type=int, default=1)
    ap.add_argument('--track-min', type=int, default=25)
    ap.add_argument('--idle-min', type=int, default=25)
    ap.add_argument('--sleep-min', type=int, default=60)
    ap.add_argument('--settle-min', type=int, default=14)
    args = ap.parse_args()
    ok_fw = (FW, 'WSTR-STD1_ca21b0f')
    for _ in range(45):                       # units may be between night wakes: wait up to 15 min
        allrows = trackers()
        if sum(1 for r in allrows.values() if r.get('online') and r.get('firmware') in ok_fw) >= args.min_units:
            break
        time.sleep(20)
    bad = [s for s, r in allrows.items() if r.get('firmware') not in ok_fw or not r.get('online')]
    rows = {s: r for s, r in allrows.items() if s not in bad}   # dead/retired units are not tested
    for sid, row in rows.items():
        state[sid] = new_state(row)
    record('setup', {'units': sorted(rows), 'excluded': bad})
    print(f'{iso()} soak start: {len(rows)} units {sorted(rows)}; excluded: {bad}', flush=True)
    threading.Thread(target=log_monitor, daemon=True).start()
    time.sleep(5)
    query_all(['CXCS#BOOTSTAT', 'CXCS#LINKSTAT'])          # baselines
    time.sleep(20)
    try:
        for cycle in range(args.first_cycle, args.cycles + 1):
            sids = list(state)
            r = random.sample(sids, 2)
            d = random.choice(sids)
            all_state(True)
            run_phase('track', args.track_min * 60, cycle, True,
                      [(8 * 60, lambda: [reset(s, 'during tracking') for s in r]),
                       (15 * 60, lambda: drop(d))])
            all_state(False)
            ri = random.choice(sids)
            di = random.choice(sids)
            ev = [(8 * 60, lambda: reset(ri, 'during idle')), (12 * 60, lambda: drop(di))]
            if cycle in [int(c) for c in args.restart_cycles.split(',')]:
                ev.append((16 * 60, restart_server))
            if cycle == 5:
                ev.append((4 * 60, lambda: ota(random.choice(sids))))
            run_phase('idle', args.idle_min * 60, cycle, False, ev)
            sleep_phase(cycle, args.sleep_min, start_while_asleep=(cycle % 2 == 0))
            if cycle % 2 == 0:
                all_state(False)                            # back to idle after the asleep start
            run_phase('idle-settle', args.settle_min * 60, cycle, False, [])
            if stop.is_set():
                break
    finally:
        set_sleep_window(False)
        all_state(False)
        write_summary()
        record('setup', {'event': 'finished', 'counts': counts})
        print(f'{iso()} soak finished: {json.dumps(counts)}', flush=True)
        stop.set()


if __name__ == '__main__':
    main()
