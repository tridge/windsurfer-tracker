import json, sys, time, urllib.request
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
CFG = json.loads((ROOT / 'events.json').read_text())
BASE = 'https://wstracker.org'
FW = 'WSTR-STD1_f655071'
FOTA = 'YCSJ,http://103.230.158.49/W07_FW/W07_MG133_WSTR_B53_f655071.bin,80#'

def api(path, data=None):
    h = {'X-Manager-Password': CFG['manager_password']}
    if data is not None: h['Content-Type'] = 'application/json'
    r = urllib.request.Request(BASE + path, headers=h, data=json.dumps(data).encode() if data is not None else None)
    with urllib.request.urlopen(r, timeout=15) as resp: return json.load(resp)

def trackers():
    return {t['sailor_id']: t for t in api('/api/manage/gt06/trackers')['trackers'] if t.get('sailor_id')}

def log(**k):
    k['time'] = time.time()
    with open(Path(__file__).parent / 'fota.jsonl', 'a') as f: f.write(json.dumps(k) + '\n')
    print(json.dumps(k), flush=True)

if __name__ == '__main__':
    sids = sys.argv[1:]
    t = trackers()
    for sid in sids:
        imei = t[sid]['imei']
        res = api(f'/api/manage/gt06/tracker/{imei}/command', {'cmd': FOTA})
        log(event='ycsj_sent', sid=sid, imei=imei, fw_before=t[sid].get('firmware'), result=res)
        time.sleep(15)
    deadline = time.time() + 900
    pending = set(sids)
    while pending and time.time() < deadline:
        time.sleep(20)
        t = trackers()
        for sid in sorted(pending):
            fw = t[sid].get('firmware'); on = t[sid].get('online')
            if fw and fw.startswith(FW):
                log(event='done', sid=sid, fw=fw, online=on, bv=t[sid].get('battery_voltage'))
                pending.discard(sid)
    for sid in pending: log(event='TIMEOUT', sid=sid, fw=t[sid].get('firmware'), online=t[sid].get('online'))
