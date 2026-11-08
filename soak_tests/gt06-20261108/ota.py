import json, os, subprocess, sys, time, urllib.request
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
CFG = json.loads((ROOT / 'events.json').read_text())
BASE = 'https://wstracker.org'
H = os.environ.get('FW_HASH') or sys.exit('usage: FW_HASH=<hash> ota.py [--keep-latest] SID...')
FW = f'WSTR-STD1_{H}'
FOTA = f'YCSJ,http://103.230.158.49/W07_FW/W07_MG133_WSTR_B53_{H}.bin,80#'

FW_DIR = '/srv/w07_fw'
IMG = f'W07_MG133_WSTR_B53_{H}.bin'

def set_latest():
    """Point the admin UI's "upgrade to latest" symlink at the image being pushed (it must exist)."""
    r = subprocess.run(['ssh', 'tracker@wstracker.org',
                        f'cd {FW_DIR} && test -s {IMG} && ln -sfn {IMG} firmware_latest_B53.bin.new && '
                        f'mv -T firmware_latest_B53.bin.new firmware_latest_B53.bin && readlink firmware_latest_B53.bin'],
                       capture_output=True, text=True)
    if r.returncode != 0 or r.stdout.strip() != IMG:
        sys.exit(f'{IMG} not on the server or symlink update failed: {r.stderr.strip()}')
    log(event='latest_set', image=IMG)

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
    args = sys.argv[1:]
    keep_latest = '--keep-latest' in args          # one-off/diagnostic push: leave "latest" alone
    sids = [a for a in args if a != '--keep-latest']
    if not keep_latest:
        set_latest()
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
