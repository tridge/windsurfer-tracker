#!/usr/bin/env python3
"""Check WebUI/js/course-time.js matches course_time_model.py: its embedded PARAMS equal
params.json, and it gives the same times for every course in a courses.json.
  test_js_parity.py params.json courses.json"""
import json, os, subprocess, sys
import course_time_model as M
HERE = os.path.dirname(os.path.abspath(__file__))
JS = os.path.join(HERE, '..', '..', 'WebUI', 'js', 'course-time.js')
P = json.load(open(sys.argv[1])); p = [P['vmg_slope'], *P['ratios'][1:]]
cs = json.load(open(sys.argv[2])); cs = cs.get('courses', cs)
cases = []
for n, c0 in cs.items():
    if not c0.get('start'): continue
    xy = M.Proj(c0['start']['lat'], c0['start']['lon'])
    for c in M.sub_courses(c0):
        legs, _ = M.course_legs(c, xy)
        for tws in (5, 10, 15, 21.5):
            for wf in (None, 0, 90, 200, 333):
                t, _ = M.course_time(p, legs, tws, legs[0][1] if wf is None else wf)
                cases.append(dict(name=n, legs=[dict(dist=d, bearing=b) for d, b in legs], tws=tws, wf=wf, py=t))
js = """const CT=require(%r);const cases=%s;
console.log(JSON.stringify({params:CT.PARAMS,t:cases.map(c=>CT.courseTime(c.legs,c.tws,c.wf).seconds)}));""" % (
    os.path.abspath(JS), json.dumps(cases))
out = json.loads(subprocess.run(['node', '-e', js], capture_output=True, text=True, check=True).stdout)
fail = 0
for k, v in out['params'].items():
    if json.dumps(v) != json.dumps(P.get(k)) and v != P.get(k):
        print(f"PARAMS mismatch {k}: js {v} vs params.json {P.get(k)}"); fail += 1
bad = [(c['name'], c['tws'], c['wf'], c['py'], j) for c, j in zip(cases, out['t']) if abs(c['py']-j) > 1e-5*max(1, c['py'])]
print(f"{len(cases)} cases, {len(bad)} mismatches, {fail} param mismatches")
for b in bad[:10]: print(b)
sys.exit(1 if bad or fail else 0)
