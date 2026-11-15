#!/usr/bin/env python3
"""Wind-based course time model for windsurf races (see docs/CourseTiming.md).

  course_time_model.py report  DAYDIR [DAYDIR ...]          per-race events, sailors, legs
  course_time_model.py fit     DAYDIR [DAYDIR ...] [-o params.json]
  course_time_model.py offset  DAYDIR                       wind-station offset from tack headings
  course_time_model.py predict --params params.json --courses courses.json [--name NAME] [--tws 15] [--wind-from DEG]

Each DAYDIR comes from fetch_race_data.py and has a hand-edited config.json.
"""
import argparse, json, math, os, pickle, statistics as st, sys
import numpy as np
from scipy.optimize import least_squares

# ---------------- model (keep in sync with the JS in courses.html) ----------------
BETA = 50.0      # tightest leg angle sailed without tacking
GAMMA = 165.0    # deepest leg angle sailed without gybing
W0 = 2.0         # kn: speeds scale with (TWS + W0)
C_TURN = 2.0     # s per mark rounding
C_GYBE = 6.0     # s per gybe (gybing mark, or a gybed downwind leg)
KN = 1852/3600
ANG = [BETA, 90, 135, GAMMA]

def straight_ratio(p, twa):
    return float(np.interp(twa, ANG, [1/math.cos(math.radians(BETA)), p[1], p[2], p[3]]))

def down_vmg(p):
    """best downwind VMG as a multiple of upwind VMG, and its angle"""
    best = (0, 180)
    for f in range(90, int(GAMMA)+1):
        v = straight_ratio(p, f)*-math.cos(math.radians(f))
        if v > best[0]: best = (v, f)
    return best

def leg_time(p, D, tws, twa):
    """seconds to sail D metres at true wind angle twa (deg 0..180); returns (s, mode)"""
    vmg = p[0]*(tws+W0)*KN
    if vmg <= 0: return float('inf'), 'x'
    if twa <= BETA: return D*math.cos(math.radians(twa))/vmg, 'beat'
    t = D/(vmg*straight_ratio(p, twa)) if twa <= GAMMA else float('inf')
    if twa > 90:
        dv, phi = down_vmg(p)
        if twa > phi:
            tg = D*-math.cos(math.radians(twa))/(vmg*dv) + C_GYBE
            if tg < t: return tg, 'gybe'
    return t, 'reach' if twa < 150 else 'run'

def turn_cost(twa_in, stbd_in, twa_out, stbd_out):
    return C_GYBE if twa_in >= 90 and twa_out >= 90 and stbd_in != stbd_out else C_TURN

def course_time(p, legs, tws, wfrom):
    """legs=[(metres, bearing)], wfrom=true wind-from deg. Returns (seconds, [(twa, mode, s)])"""
    tot, det, prev = 0.0, [], None
    for D, b in legs:
        d = ad(b, wfrom); twa = abs(d)
        t, mode = leg_time(p, D, tws, twa)
        if prev: t += turn_cost(prev[0], prev[1], twa, d > 0)
        det.append((twa, mode, t)); tot += t; prev = (twa, d > 0)
    return tot, det

# ---------------- geometry ----------------
def ad(a, b): return (a-b+180) % 360-180
def brg(a, b): return math.degrees(math.atan2(b[0]-a[0], b[1]-a[1])) % 360
def dist(a, b): return math.hypot(b[0]-a[0], b[1]-a[1])

class Proj:
    def __init__(self, lat, lon):
        self.lat0, self.lon0 = lat, lon; self.kx = 111320*math.cos(math.radians(lat)); self.ky = 110540
    def __call__(self, lat, lon): return ((lon-self.lon0)*self.kx, (lat-self.lat0)*self.ky)

def course_seq(c, xy):
    """port of WebUI CourseOrder.sequence/pinLine: [(key, (x,y))] with start/finish pins at
    their line middles, plus {'S'/'F': (endA, endB)} lines"""
    nm = len(c.get('marks', []))
    if c.get('order'):
        ks = [k if k in 'SF' else int(k) for k in c['order']]
    else:
        ks = (['S'] if c.get('start') else [])+list(range(1, nm+1))+(['F'] if c.get('finish') else [])
    pt = lambda k: c['start'] if k == 'S' else c['finish'] if k == 'F' else c['marks'][k-1]
    seq = [(k, xy(pt(k)['lat'], pt(k)['lon'])) for k in ks if pt(k)]
    lines = {}
    for which in 'SF':
        p = c.get('start' if which == 'S' else 'finish')
        if not p or not (p.get('pin') or {}).get('dist', 0) > 0: continue
        idx = [i for i, (k, _) in enumerate(seq) if k == which]
        if not idx: continue
        i = idx[0] if which == 'S' else idx[-1]
        j = i+1 if which == 'S' else i-1
        if not 0 <= j < len(seq): continue
        P = xy(p['lat'], p['lon']); nb = seq[j][1]
        sgn = 1 if p['pin'].get('side') == 'right' else -1
        frm = P; pin = None
        for _ in range(4):
            fx, fy = nb[0]-frm[0], nb[1]-frm[1]; L = math.hypot(fx, fy)
            if L < 0.5: break
            fe, fn = fx/L, fy/L
            pin = (P[0]+sgn*fn*p['pin']['dist'], P[1]-sgn*fe*p['pin']['dist'])
            frm = ((P[0]+pin[0])/2, (P[1]+pin[1])/2)
        if pin:
            lines[which] = (P, pin)
            seq = [(k, frm if k == which else v) for k, v in seq]
    return seq, lines

def course_legs(c, xy):
    seq, _ = course_seq(c, xy)
    return [(dist(seq[i-1][1], seq[i][1]), brg(seq[i-1][1], seq[i][1])) for i in range(1, len(seq))], [k for k, _ in seq]

def sub_courses(c):
    """the course plus any extraCourses (CourseOrder.subCourses)"""
    return [c]+[x for x in (c.get('extraCourses') or []) if isinstance(x, dict)]

def side(a, b, p): return (b[0]-a[0])*(p[1]-a[1])-(b[1]-a[1])*(p[0]-a[0])

def seg_cross(p0, p1, a, b, tol):
    s0, s1 = side(a, b, p0), side(a, b, p1)
    if s0 == 0 or (s0 > 0) == (s1 > 0): return None
    f = s0/(s0-s1); q = (p0[0]+f*(p1[0]-p0[0]), p0[1]+f*(p1[1]-p0[1]))
    L = dist(a, b)
    if L < 0.5: return None
    u = ((q[0]-a[0])*(b[0]-a[0])+(q[1]-a[1])*(b[1]-a[1]))/L
    return f if -tol <= u <= L+tol else None

# ---------------- a race day ----------------
class Day:
    START_TOL = 60.0
    FIN_TOL = 60.0

    def __init__(self, d):
        self.dir = d
        self.cfg = json.load(open(os.path.join(d, 'config.json')))
        summ = json.load(open(os.path.join(d, 'summary.json')))
        log = next(l for l in summ['logs'] if l['file'] == self.cfg['log_file'])
        self.races = []
        for s in log.get('sublogs', []):
            rc = self.cfg['races'].get(s['name'])
            if rc is None or rc.get('skip'): continue
            c = s.get('course') or json.load(open(os.path.join(d, 'course.json')))
            self.races.append(dict(name=s['name'], a=s['start_ts'], b=s['end_ts'], course=c,
                                   gun=rc.get('gun'), pursuit=rc.get('pursuit', False)))
        c0 = self.races[0]['course']['start']
        self.xy = Proj(c0['lat'], c0['lon'])
        self.woff = self.cfg.get('wind_offset_deg', 0.0)
        self._tracks()
        self._wind()
        self.names = self._names()

    def _tracks(self):
        cache = os.path.join(self.dir, 'tracks.pkl')
        src = os.path.join(self.dir, 'track.jsonl')
        if os.path.exists(cache) and os.path.getmtime(cache) >= os.path.getmtime(src):
            self.tracks, self.roles = pickle.load(open(cache, 'rb')); return
        tr, roles = {}, {}
        for line in open(src):
            r = json.loads(line)
            if 'pos' not in r: continue
            roles[r['id']] = r.get('role')
            t = tr.setdefault(r['id'], {})
            for p in r['pos']: t[p[0]] = (p[1], p[2])
        self.tracks = {i: sorted((t,)+v for t, v in pts.items()) for i, pts in tr.items()}
        self.roles = roles
        pickle.dump((self.tracks, self.roles), open(cache, 'wb'))

    def _wind(self):
        W = [json.loads(l) for l in open(os.path.join(self.dir, 'wind.jsonl'))]
        W = [w for w in W if w.get('s') is not None and w.get('d') is not None]
        self.wt = np.array([w['t'] for w in W], float); self.ws = np.array([w['s'] for w in W], float)
        self.wu = np.array([w['s']*math.sin(math.radians(w['d'])) for w in W])
        self.wv = np.array([w['s']*math.cos(math.radians(w['d'])) for w in W])

    def wind(self, t0, t1):
        """time-weighted mean speed (kn) and corrected true wind-from (deg) over [t0, t1]"""
        if t0 < self.wt[0]-300 or t1 > self.wt[-1]+300: raise ValueError('no wind data for interval')
        g = np.arange(t0, max(t1, t0+1)+1, 1.0)
        d = math.degrees(math.atan2(np.interp(g, self.wt, self.wu).mean(), np.interp(g, self.wt, self.wv).mean()))
        return float(np.interp(g, self.wt, self.ws).mean()), (d+self.woff) % 360

    def _names(self):
        n = {}
        try:
            for k, u in json.load(open(os.path.join(self.dir, 'users.json')))['users'].items():
                if u.get('_last_id') and u.get('name'): n[u['_last_id']] = u['name']
        except Exception:
            pass
        return n

    def name(self, sid): return self.names.get(sid, sid)

    def sailors(self):
        s = self.cfg.get('sailors')
        return s if s else [i for i, r in self.roles.items() if r == 'sailor']

    def mark_pos(self, course, m, t):
        tid = self.cfg.get('mark_trackers', {}).get(str(m))
        if tid and tid in self.tracks:
            if not hasattr(self, '_mt'): self._mt = {}
            if tid not in self._mt:
                p = self.tracks[tid]
                self._mt[tid] = (np.array([q[0] for q in p]), np.array([self.xy(q[1], q[2]) for q in p]))
            T, X = self._mt[tid]
            return (float(np.interp(t, T, X[:, 0])), float(np.interp(t, T, X[:, 1])))
        mk = course['marks'][m-1]; return self.xy(mk['lat'], mk['lon'])

    def point(self, spec, t):
        if isinstance(spec, dict) and 'tracker' in spec:
            p = self.tracks[spec['tracker']]; T = [q[0] for q in p]
            i = min(max(0, int(np.searchsorted(T, t))), len(p)-1); return self.xy(p[i][1], p[i][2])
        return self.xy(spec[0], spec[1])

    def finish_line(self, race, t):
        fl = self.cfg.get('finish_line', 'course')
        if fl == 'course':
            seq, lines = course_seq(race['course'], self.xy)
            if 'F' in lines: return lines['F']
            f = seq[-1][1]; return (f, (f[0]+1, f[1]))   # point finish: degenerate, will rarely detect
        return self.point(fl[0], t), self.point(fl[1], t)

    def detect(self, race, sid):
        a, b, c = race['a'], race['b'], race['course']
        P = [(q[0],)+self.xy(q[1], q[2]) for q in self.tracks.get(sid, []) if a-300 <= q[0] <= b+900]
        if len(P) < 100: return None
        seq, lines = course_seq(c, self.xy)
        marks = [k for k, _ in seq if isinstance(k, int)]

        def first_pass(m, t0, R):
            ins = []
            for q in P:
                if q[0] < t0: continue
                bx, by = self.mark_pos(c, m, q[0]); d = math.hypot(q[1]-bx, q[2]-by)
                if d < R: ins.append((q[0], d))
                elif ins and q[0]-ins[-1][0] > 20: break
            return min(ins, key=lambda z: z[1]) if ins else None

        def rounding(m, t0): return first_pass(m, t0, 40) or first_pass(m, t0, 60)
        ev = dict(sid=sid, ok=False, start=None, rounds=[], finish=None, finpt=None, why=None, st_src=None)
        r1 = rounding(marks[0], a)
        if not r1: ev['why'] = 'never reached first mark'; return ev
        # start: last start-line crossing (either direction) before the first rounding, not before gun-60s
        if 'S' in lines:
            A, Bp = lines['S']; tmin = race['gun']-60 if race['gun'] else a
            for k in range(1, len(P)):
                if P[k][0] > r1[0]: break
                if P[k][0] < tmin: continue
                f = seg_cross(P[k-1][1:], P[k][1:], A, Bp, self.START_TOL)
                if f is not None: ev['start'] = P[k-1][0]+f*(P[k][0]-P[k-1][0]); ev['st_src'] = 'crossing'
        if ev['start'] is None and race['gun'] and not race['pursuit']:
            ev['start'], ev['st_src'] = race['gun'], 'gun'
        rounds = [(marks[0], r1[0], r1[1])]; t = r1[0]+10
        for m in marks[1:]:
            r = rounding(m, t)
            if not r: break
            rounds.append((m, r[0], r[1])); t = r[0]+10
        ev['rounds'] = rounds
        if len(rounds) == len(marks):
            for k in range(1, len(P)):
                if P[k][0] < t: continue
                if P[k][0] > t+900: break
                A, Bp = self.finish_line(race, P[k][0])
                f = seg_cross(P[k-1][1:], P[k][1:], A, Bp, self.FIN_TOL)
                if f is not None:
                    ev['finish'] = P[k-1][0]+f*(P[k][0]-P[k-1][0])
                    ev['finpt'] = (P[k-1][1]+f*(P[k][1]-P[k-1][1]), P[k-1][2]+f*(P[k][2]-P[k-1][2])); break
        ev['ok'] = bool(ev['start'] and ev['finish'])
        if not ev['ok']:
            ev['why'] = 'no start crossing' if not ev['start'] else \
                f"rounded {len(rounds)}/{len(marks)} marks" if len(rounds) < len(marks) else 'no finish crossing'
        return ev

    def build(self):
        """per-race events, legs and per-sailor leg records"""
        recs, out = [], []
        excl = self.cfg.get('exclude', {})
        for race in self.races:
            seq, _ = course_seq(race['course'], self.xy)
            keys = [k for k, _ in seq]
            ev = {}
            for s in self.sailors():
                if s in excl.get(race['name'], {}): continue
                e = self.detect(race, s)
                if e and e['rounds']: ev[s] = e
            fins = [e for e in ev.values() if e['ok']]
            pts = [v for _, v in seq]
            if fins and self.cfg.get('finish_line', 'course') != 'course':
                # finish point = where the fleet actually crossed the as-laid line
                pts[-1] = (st.median(e['finpt'][0] for e in fins), st.median(e['finpt'][1] for e in fins))
            legs = [(dist(pts[i-1], pts[i]), brg(pts[i-1], pts[i])) for i in range(1, len(pts))]
            R = dict(day=self.dir, name=race['name'], race=race, keys=keys, legs=legs, events=ev,
                     course_finish=seq[-1][1], finpt=pts[-1], excluded=excl.get(race['name'], {}))
            out.append(R)
            for s, e in ev.items():
                if not e['ok']: continue
                times = [e['start']]+[t for _, t, _ in e['rounds']]+[e['finish']]
                for i in range(1, len(times)):
                    ws, wd = self.wind(times[i-1], times[i])
                    recs.append(dict(race=(self.dir, race['name']), sailor=self.name(s), leg=i-1,
                                     key=f"{keys[i-1]}-{keys[i]}", D=legs[i-1][0], brg=legs[i-1][1],
                                     dt=times[i]-times[i-1], ws=ws, wd=wd))
        return recs, out

    def race_wind(self, R):
        ok = [e for e in R['events'].values() if e['ok']]
        return self.wind(st.median(e['start'] for e in ok), st.median(e['finish'] for e in ok))

# ---------------- fitting ----------------
def rec_time(p, k, r, prev):
    d = ad(r['brg'], r['wd']); twa = abs(d)
    t, _ = leg_time(p, r['D'], (r['ws']+W0)*k-W0, twa)    # sailor factor scales speed
    if prev: t += turn_cost(abs(ad(prev['brg'], prev['wd'])), ad(prev['brg'], prev['wd']) > 0, twa, d > 0)
    return t

def fit(recs, p0=(0.165, 3.0, 2.4, 2.0)):
    """robust log-time fit with a speed factor per sailor; returns median-sailor params, factors, rms"""
    sail = sorted({r['sailor'] for r in recs}); idx = {s: i for i, s in enumerate(sail)}
    prev = {}
    for r in recs: prev[(r['race'], r['sailor'], r['leg'])] = r

    def unpack(x):
        lk = np.concatenate([x[4:], [-x[4:].sum()]]); return np.exp(x[:4]), np.exp(lk)

    def res(x):
        p, k = unpack(x)
        return [math.log(rec_time(p, k[idx[r['sailor']]], r, prev.get((r['race'], r['sailor'], r['leg']-1)))/r['dt'])
                for r in recs]
    x0 = np.concatenate([np.log(p0), np.zeros(len(sail)-1)])
    sol = least_squares(res, x0, loss='soft_l1', f_scale=0.15)
    p, k = unpack(sol.x); km = float(np.median(k))
    return np.array([p[0]*km, *p[1:]]), {s: float(v/km) for s, v in zip(sail, k)}, float(np.sqrt(np.mean(np.square(res(sol.x)))))

def race_actual(R):
    t = [e['finish']-e['start'] for e in R['events'].values() if e['ok']]
    return (st.median(t), len(t)) if t else (None, 0)

def webui_old(km, tws):
    for w, u in [(7.5, 500), (12.5, 650), (17.5, 800), (99, 1000)]:
        if tws <= w: return km*1000/(u/5)*60

# ---------------- commands ----------------
def load(dirs):
    recs, races, days = [], [], {}
    for d in dirs:
        day = Day(d); days[d] = day
        r, R = day.build(); recs += r; races += R
    return recs, races, days

def cmd_report(a):
    recs, races, days = load(a.days)
    for R in races:
        day = days[R['day']]; t0 = R['race']['a']
        print(f"== {R['day']} {R['name']}  {R['race']['course'].get('order')}  legs " +
              ' '.join(f"{a_}-{b_}:{d:.0f}m@{bb:.0f}" for a_, b_, (d, bb) in zip(R['keys'], R['keys'][1:], R['legs'])))
        if R['finpt'] != R['course_finish']:
            print(f"   finish: fleet crossed at ({R['finpt'][0]:.0f},{R['finpt'][1]:.0f}) m vs course finish "
                  f"({R['course_finish'][0]:.0f},{R['course_finish'][1]:.0f}) m from the start point")
        for s, why in R['excluded'].items(): print(f"   {day.name(s):16s} EXCLUDED: {why}")
        for s, e in R['events'].items():
            if e['ok']:
                g = R['race']['gun']
                note = ' (gun)' if e['st_src'] == 'gun' else (f" gun{e['start']-g:+4.0f}s" if g else '')
                print(f"   {day.name(s):16s} start +{(e['start']-t0)/60:5.2f}{note}"
                      f"  finish +{(e['finish']-t0)/60:5.2f}  elapsed {(e['finish']-e['start'])/60:5.2f} min")
            else:
                print(f"   {day.name(s):16s} not used: {e['why']}")
        act, n = race_actual(R)
        if n:
            tws, wd = day.race_wind(R)
            print(f"   median elapsed {act/60:.2f} min (n={n}); wind {tws:.1f} kn from {wd:.0f} (corrected); first leg {R['legs'][0][1]:.0f}")

def cmd_fit(a):
    recs, races, days = load(a.days)
    p, k, rms = fit(recs); dv, phi = down_vmg(p)
    print(f"fit on {len(recs)} sailor-legs from {len(races)} races")
    print(f"  VMG_up = {p[0]:.4f} x (TWS + {W0:g}) kn; VMC/VMG_up = {1/math.cos(math.radians(BETA)):.3f}@{BETA:g}, "
          f"{p[1]:.3f}@90, {p[2]:.3f}@135, {p[3]:.3f}@{GAMMA:g}; rms log {rms:.3f}")
    print(f"  best downwind VMG {dv:.3f} x VMG_up gybing at {phi}")
    print("  sailor speed factors: " + ', '.join(f"{s} {v:.3f}" for s, v in sorted(k.items(), key=lambda z: -z[1])))
    print(f"\n{'race':28s} {'TWS':>5s} {'km':>5s} {'actual':>7s} {'fit':>7s} {'fit1st':>7s} {'LORO':>7s} {'LORO1st':>8s} {'old':>7s}  (minutes, median sailor)")
    for R in races:
        act, n = race_actual(R)
        if not n: continue
        day = days[R['day']]; tws, wd = day.race_wind(R); km = sum(d for d, _ in R['legs'])/1000
        rest = [r for r in recs if r['race'] != (R['day'], R['name'])]
        pl = fit(rest)[0] if len({r['race'] for r in rest}) >= 2 else p
        v = [course_time(p, R['legs'], tws, wd)[0], course_time(p, R['legs'], tws, R['legs'][0][1])[0],
             course_time(pl, R['legs'], tws, wd)[0], course_time(pl, R['legs'], tws, R['legs'][0][1])[0]]
        lbl = f"{os.path.basename(os.path.normpath(R['day']))} {R['name']}"
        print(f"{lbl:28s} {tws:5.1f} {km:5.2f} {act/60:7.2f} " + ' '.join(f"{x/60:7.2f}" for x in v) + f" {webui_old(km, tws)/60:8.2f}")
        _, det = course_time(p, R['legs'], tws, wd)
        print(' '*29 + 'legs ' + ' '.join(f"{a_}-{b_}:{twa:.0f}{m[0]}" for a_, b_, (twa, m, _) in zip(R['keys'], R['keys'][1:], det)))
    if a.output:
        json.dump(dict(model='course-time v2', vmg_slope=round(p[0], 6), w0_kn=W0, beta_deg=BETA, gamma_deg=GAMMA,
                       ratio_angles=[BETA, 90, 135, GAMMA],
                       ratios=[round(1/math.cos(math.radians(BETA)), 6), round(p[1], 6), round(p[2], 6), round(p[3], 6)],
                       turn_s=C_TURN, gybe_s=C_GYBE, fitted_on=[f"{os.path.basename(os.path.normpath(R['day']))} {R['name']}" for R in races],
                       sailor_factors={s: round(v, 3) for s, v in k.items()}, rms_log=round(rms, 4)),
                  open(a.output, 'w'), indent=1)
        print(f"\nwrote {a.output}")

def cmd_offset(a):
    """station direction offset: bisector of the two tack headings on beats vs station direction"""
    day = Day(a.days[0]); day.woff = 0.0
    recs, races = day.build()
    out = []
    for R in races:
        for li, (D, b) in enumerate(R['legs']):
            H, W = [], []
            for s, e in R['events'].items():
                if not e['ok']: continue
                times = [e['start']]+[t for _, t, _ in e['rounds']]+[e['finish']]
                t0, t1 = times[li], times[li+1]
                ws, wd = day.wind(t0, t1)
                if abs(ad(b, wd)) > 40: break
                P = [day.xy(q[1], q[2]) for q in day.tracks[s] if t0+15 <= q[0] <= t1-15]
                for i in range(5, len(P), 5):
                    if dist(P[i-5], P[i]) > 8: H.append(brg(P[i-5], P[i])); W.append(wd)
            if len(H) < 50: continue
            w = math.degrees(math.atan2(sum(math.sin(math.radians(x)) for x in W), sum(math.cos(math.radians(x)) for x in W))) % 360
            L = [ad(h, w) for h in H if -90 < ad(h, w) < 0]; Rr = [ad(h, w) for h in H if 0 < ad(h, w) < 90]
            if len(L) < 10 or len(Rr) < 10: continue
            off = (st.median(L)+st.median(Rr))/2; out.append(off)
            print(f"{R['name']} leg {R['keys'][li]}-{R['keys'][li+1]}: station {w:.0f}, tacks {(w+st.median(L))%360:.0f}/{(w+st.median(Rr))%360:.0f}, offset {off:+.1f}, half-angle {(st.median(Rr)-st.median(L))/2:.0f}")
    if out: print(f"median offset {st.median(out):+.1f} deg (set wind_offset_deg in config.json)")

def cmd_predict(a):
    P = json.load(open(a.params))
    p = [P['vmg_slope'], *P['ratios'][1:]]
    courses = json.load(open(a.courses)); courses = courses.get('courses', courses)
    names = [a.name] if a.name else list(courses)
    tws_list = [a.tws] if a.tws else [5, 10, 15, 21.5]
    for n in names:
        c0 = courses[n]
        if not c0.get('start'): continue
        xy = Proj(c0['start']['lat'], c0['start']['lon'])
        for ci, c in enumerate(sub_courses(c0)):
            legs, keys = course_legs(c, xy)
            if not legs: continue
            wf = a.wind_from if a.wind_from is not None else legs[0][1]
            km = sum(d for d, _ in legs)/1000
            res = []
            for tws in tws_list:
                t, det = course_time(p, legs, tws, wf); res.append(f"{tws:g}kn {t/60:5.1f}")
            _, det = course_time(p, legs, tws_list[0], wf)
            warn = sorted({w for twa, m, _ in det for w in (['close-reach'] if BETA < twa < 80 else []) + (['dead-run'] if twa > 170 else [])})
            print(f"{n}{' ['+str(ci)+']' if len(sub_courses(c0)) > 1 else ''} ({c.get('order')}, {km:.2f} km, wind from {wf:.0f}): " + ', '.join(res) + " min")
            print('   legs ' + ' '.join(f"{a_}-{b_}:{d:.0f}m/{twa:.0f}°{m[0]}" for a_, b_, (d, _), (twa, m, _) in zip(keys, keys[1:], legs, det))
                  + (f"   ! untested angles: {', '.join(warn)}" if warn else ''))

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest='cmd', required=True)
    for c in ('report', 'fit', 'offset'):
        s = sp.add_parser(c); s.add_argument('days', nargs='+')
        if c == 'fit': s.add_argument('-o', '--output')
    s = sp.add_parser('predict'); s.add_argument('--params', required=True); s.add_argument('--courses', required=True)
    s.add_argument('--name'); s.add_argument('--tws', type=float); s.add_argument('--wind-from', type=float)
    a = ap.parse_args()
    {'report': cmd_report, 'fit': cmd_fit, 'offset': cmd_offset, 'predict': cmd_predict}[a.cmd](a)

if __name__ == '__main__':
    main()
