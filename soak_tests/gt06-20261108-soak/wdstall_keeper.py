#!/usr/bin/env python3
"""Keep SZCS#WDSTALL=120 applied (RAM-only, lost on every reboot) on the given units by re-sending it at login."""
import json, re, subprocess, sys, time, urllib.request
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
MGR = json.loads((ROOT / 'events.json').read_text())['manager_password']
SIDS = sys.argv[1:] or ['G380521']
def cmd(sid, c):
    req = urllib.request.Request(f'https://wstracker.org/api/manage/gt06/tracker/866557081{sid[1:]}/command',
                                 data=json.dumps({'cmd': c}).encode(),
                                 headers={'X-Manager-Password': MGR, 'Content-Type': 'application/json'})
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:
        print(time.strftime('%H:%M:%S'), sid, 'cmd failed', e, flush=True)
for s in SIDS:
    cmd(s, 'SZCS#WDSTALL=120')
while True:
    p = subprocess.Popen(['ssh', '-o', 'ServerAliveInterval=15', 'tracker@wstracker.org',
                          'tail -n 0 -F /home/tracker/tracker/tracker.log'],
                         stdout=subprocess.PIPE, text=True, errors='replace', bufsize=1)
    for line in p.stdout:
        m = re.search(r'Login: id .* -> (G\d{6}) ', line)
        if m and m.group(1) in SIDS:
            time.sleep(20)                       # let the login reconcile finish first
            cmd(m.group(1), 'SZCS#WDSTALL=120')
            print(time.strftime('%H:%M:%S'), m.group(1), 'reapplied after login', flush=True)
        elif re.search(r'Command ACK from (%s): (SETOK:WDSTALL|READOK:BOOTSTAT)' % '|'.join(SIDS), line):
            print(line.strip(), flush=True)
    time.sleep(5)
