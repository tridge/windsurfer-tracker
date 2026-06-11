#!/usr/bin/env python3
'''Bulk enable/disable SimBase SIMs via API v2.

Usage:
  python scripts/simbase_manage.py status                    # list all SIMs
  python scripts/simbase_manage.py disable                   # disable all SIMs
  python scripts/simbase_manage.py enable                    # enable all SIMs
  python scripts/simbase_manage.py disable --tag W07C        # only SIMs tagged "W07C"
  python scripts/simbase_manage.py disable --dry-run         # preview without changes
  python scripts/simbase_manage.py enable --iccid 8961...    # enable a single SIM

API key: ~/.simbase.key file, SIMBASE_API_KEY env var, or --api-key flag.
'''

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import sys
import time

import requests

BASE_URL = 'https://api.simbase.com/v2'
RATE_LIMIT_DELAY = 0.25  # seconds between API calls
MAX_WORKERS = 4  # parallel API requests (10 trips the API rate limit)
MAX_RETRIES = 5


def request_with_retry(session, method, url, **kwargs):
    '''Request with backoff on 429/5xx (honours Retry-After).'''
    for attempt in range(MAX_RETRIES):
        resp = session.request(method, url, **kwargs)
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == MAX_RETRIES - 1:
                break
            retry_after = resp.headers.get('Retry-After')
            try:
                delay = float(retry_after)
            except (TypeError, ValueError):
                delay = 2 ** attempt
            time.sleep(delay)
            continue
        break
    resp.raise_for_status()
    return resp


def get_session(api_key):
    s = requests.Session()
    s.headers.update({
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    })
    return s


def fetch_all_sims(session):
    '''Paginate through all SIMs and return a list.'''
    sims = []
    cursor = None
    while True:
        params = {'limit': 100}
        if cursor:
            params['cursor'] = cursor
        resp = request_with_retry(session, 'GET', f'{BASE_URL}/simcards', params=params)
        data = resp.json()
        page = data.get('data', data.get('simcards', []))
        if isinstance(page, list):
            sims.extend(page)
        else:
            # handle unexpected shape
            print(f'Warning: unexpected response shape: {list(data.keys())}', file=sys.stderr)
            break
        # check for more pages
        has_more = data.get('has_more', data.get('hasMore', False))
        if not has_more:
            break
        cursor = data.get('cursor', data.get('nextCursor'))
        if not cursor:
            break
        time.sleep(RATE_LIMIT_DELAY)
    return sims


def filter_sims(sims, tag=None, iccid=None):
    '''Filter SIMs by tag and/or ICCID.'''
    if iccid:
        return [s for s in sims if s.get('iccid') == iccid]
    if tag:
        result = []
        for s in sims:
            sim_tags = s.get('tags', []) or []
            sim_name = s.get('name', '') or ''
            if tag in sim_tags or tag.lower() in sim_name.lower():
                result.append(s)
        return result
    return sims


def get_sim_state(sim):
    '''Extract state from a SIM record (handles different API field names).'''
    return sim.get('state', sim.get('status', 'unknown'))


def get_sim_label(sim):
    '''Human-readable label for a SIM.'''
    name = sim.get('name', '')
    iccid = sim.get('iccid', '?')
    if name:
        return f'{name} ({iccid})'
    return iccid


def fetch_sim_detail(session, iccid):
    '''Fetch individual SIM details (has session_status and connection info).'''
    resp = request_with_retry(session, 'GET', f'{BASE_URL}/simcards/{iccid}')
    return resp.json()


def cmd_status(session, sims):
    '''Print status of all SIMs with connection state.'''
    total = len(sims)
    print(f'Fetching details for {total} SIMs...')
    # Fetch all SIM details in parallel
    details = [None] * total
    done = [0]

    def fetch_one(idx, iccid):
        detail = fetch_sim_detail(session, iccid)
        details[idx] = detail
        done[0] += 1
        print(f'\r  {done[0]}/{total}', end='', flush=True)
        return detail

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(fetch_one, i, s['iccid']): i for i, s in enumerate(sims)}
        for f in as_completed(futures):
            f.result()  # raise any exceptions

    print('\r' + ' ' * 20 + '\r', end='')
    counts = {}
    connected_count = 0
    for detail in details:
        state = get_sim_state(detail)
        counts[state] = counts.get(state, 0) + 1
        tags = ', '.join(detail.get('tags', []) or [])
        name = detail.get('name', '') or ''
        iccid = detail.get('iccid', '')
        msisdn = detail.get('msisdn', '') or ''
        session_status = detail.get('session_status', 'unavailable')
        connected = session_status == 'in_session'
        if connected:
            connected_count += 1
        conn_str = 'online' if connected else 'offline'
        conn_info = detail.get('connection') or {}
        carrier = conn_info.get('carrier', '') or ''
        print(f'{iccid}  {state:10s}  {conn_str:7s}  {name:20s}  {msisdn:15s}  {carrier:15s}  {tags}')
    print()
    parts = [f'{v} {k}' for k, v in sorted(counts.items())]
    print(f'Total: {total} SIMs ({", ".join(parts)}, {connected_count} online)')


def cmd_enable(session, sims, dry_run=False):
    '''Activate all inactive/suspended SIMs.'''
    targets = [s for s in sims if get_sim_state(s) not in ('enabled', 'enabling')]
    already = len(sims) - len(targets)

    if not targets:
        print(f'All {len(sims)} SIMs are already enabled.')
        return

    print(f'Will enable {len(targets)} SIMs ({already} already active)')
    if dry_run:
        for s in targets:
            print(f'  [dry-run] would activate {get_sim_label(s)} (currently {get_sim_state(s)})')
        return

    ok = [0]
    errors = [0]

    def activate_one(s):
        try:
            request_with_retry(session, 'POST', f'{BASE_URL}/simcards/{s["iccid"]}/state',
                               json={'state': 'enabled'})
            print(f'  enabled {get_sim_label(s)}')
            ok[0] += 1
        except requests.RequestException as e:
            print(f'  ERROR activating {get_sim_label(s)}: {e}', file=sys.stderr)
            errors[0] += 1

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        list(pool.map(activate_one, targets))

    print(f'\nEnabled {ok[0]}/{len(targets)} SIMs ({already} already enabled, {errors[0]} errors)')


def cmd_disable(session, sims, dry_run=False):
    '''Deactivate all active SIMs.'''
    targets = [s for s in sims if get_sim_state(s) in ('enabled', 'enabling')]
    already = len(sims) - len(targets)

    if not targets:
        print(f'All {len(sims)} SIMs are already disabled.')
        return

    print(f'Will disable {len(targets)} SIMs ({already} already inactive)')
    if dry_run:
        for s in targets:
            print(f'  [dry-run] would deactivate {get_sim_label(s)}')
        return

    ok = [0]
    errors = [0]

    def deactivate_one(s):
        try:
            request_with_retry(session, 'POST', f'{BASE_URL}/simcards/{s["iccid"]}/state',
                               json={'state': 'disabled'})
            print(f'  disabled {get_sim_label(s)}')
            ok[0] += 1
        except requests.RequestException as e:
            print(f'  ERROR deactivating {get_sim_label(s)}: {e}', file=sys.stderr)
            errors[0] += 1

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        list(pool.map(deactivate_one, targets))

    print(f'\nDisabled {ok[0]}/{len(targets)} SIMs ({already} already disabled, {errors[0]} errors)')


def cmd_sms(session, iccid, text):
    '''Send an SMS to a SIM via the SimBase API.'''
    resp = request_with_retry(session, 'POST', f'{BASE_URL}/simcards/{iccid}/sms',
                              json={'message': text})
    print(f'Sent to {iccid}: {resp.status_code} {resp.text}')


def cmd_inbox(session, iccid):
    '''Show SMS received from a SIM (device replies).'''
    resp = request_with_retry(session, 'GET', f'{BASE_URL}/simcards/{iccid}/sms')
    data = resp.json()
    msgs = data.get('sms', [])
    if not msgs:
        print(f'No SMS for {iccid}.')
        return
    for m in msgs:
        print(m)


def fetch_server_inventory(server_url, manager_password):
    '''GT06 device inventory from the tracker server, or None without a password.'''
    if not manager_password:
        return None
    resp = requests.get(f'{server_url}/api/manage/gt06/trackers',
                        headers={'X-Manager-Password': manager_password}, timeout=30)
    resp.raise_for_status()
    return resp.json().get('trackers', [])


def cmd_diagnose(session, sims, server_url, manager_password):
    '''Join SimBase SIM detail with the tracker-server inventory and bucket the fleet.'''
    total = len(sims)
    print(f'Fetching details for {total} SIMs...')
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        details = list(pool.map(lambda s: fetch_sim_detail(session, s['iccid']), sims))

    inventory = fetch_server_inventory(server_url, manager_password)
    if inventory is None:
        print('(no manager password: server-side columns unavailable; '
              'pass --manager-password or set WSTRACKER_MANAGER_PASSWORD)')
    by_imei = {t['imei']: t for t in inventory or []}

    now = time.time()

    def iso_age_h(iso):
        if not iso:
            return None
        t = time.mktime(time.strptime(iso, '%Y-%m-%dT%H:%M:%SZ')) - time.timezone
        return (now - t) / 3600

    rows = []
    for d in details:
        imei = d.get('imei') or ''
        loc = d.get('location') or {}
        usage = d.get('current_month_usage') or {}
        t = by_imei.get(imei)
        rows.append({
            'iccid': d['iccid'],
            'imei6': imei[-6:],
            'sess': d.get('session_status') == 'in_session',
            'loc_age_h': iso_age_h(loc.get('last_update')),
            'radio': loc.get('radio') or '',
            'mb': (usage.get('data') or 0) / 1e6,
            'srv': t,
            'seen_h': ((now - t['last_seen']) / 3600
                       if t and t.get('last_seen') else None),
        })

    rows.sort(key=lambda r: (r['sess'], r['srv'] is not None,
                             r['loc_age_h'] if r['loc_age_h'] is not None else 9e9))
    hdr = f'{"ICCID":>20} {"IMEI6":>6} {"sess":>4} {"net_seen":>8} {"radio":>5} {"MB_mo":>6}'
    if inventory is not None:
        hdr += f' {"sailor":>8} {"online":>6} {"srv_seen":>8}'
    print(hdr)
    for r in rows:
        la = f'{r["loc_age_h"]:.1f}h' if r['loc_age_h'] is not None else 'never'
        line = (f'{r["iccid"]:>20} {r["imei6"] or "—":>6} '
                f'{"IN" if r["sess"] else "off":>4} {la:>8} {r["radio"]:>5} {r["mb"]:>6.1f}')
        if inventory is not None:
            t = r['srv']
            sh = f'{r["seen_h"]:.1f}h' if r['seen_h'] is not None else '—'
            line += (f' {(t or {}).get("sailor_id") or "—":>8} '
                     f'{str((t or {}).get("online", "—")):>6} {sh:>8}')
        print(line)

    buckets = {}
    for r in rows:
        if r['sess'] and r['srv'] and r['srv'].get('online'):
            k = 'healthy: in session + connected to server'
        elif r['sess']:
            k = 'in session but NOT connected to server'
        elif r['loc_age_h'] is not None:
            k = 'offline: was on the network, now dropped'
        else:
            k = 'NEVER attached to any network'
        buckets.setdefault(k, []).append(r['imei6'] or r['iccid'][-6:])
    print('\nBuckets:')
    for k, v in sorted(buckets.items()):
        print(f'  {len(v):>3}  {k}: {", ".join(v)}')


def main():
    parser = argparse.ArgumentParser(
        description='Manage SimBase SIMs - bulk enable/disable to save costs between regattas')
    parser.add_argument('action',
                        choices=['status', 'enable', 'disable', 'diagnose', 'sms', 'inbox'],
                        help='Action to perform')
    parser.add_argument('text', nargs='?',
                        help='SMS text (for the sms action)')
    default_key = os.environ.get('SIMBASE_API_KEY')
    if not default_key:
        keyfile = os.path.expanduser('~/.simbase.key')
        if os.path.exists(keyfile):
            with open(keyfile) as f:
                default_key = f.read().strip()
    parser.add_argument('--api-key', default=default_key,
                        help='SimBase API key (default: ~/.simbase.key, SIMBASE_API_KEY env var)')
    parser.add_argument('--tag', help='Only manage SIMs with this tag or name substring')
    parser.add_argument('--iccid', help='Only manage a single SIM by ICCID')
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview changes without making them')
    parser.add_argument('--server-url', default='https://wstracker.org',
                        help='Tracker server URL for diagnose (default: %(default)s)')
    parser.add_argument('--manager-password',
                        default=os.environ.get('WSTRACKER_MANAGER_PASSWORD'),
                        help='Tracker-server manager password for diagnose '
                             '(default: WSTRACKER_MANAGER_PASSWORD env var)')
    args = parser.parse_args()

    if not args.api_key:
        print('Error: set SIMBASE_API_KEY env var or pass --api-key', file=sys.stderr)
        sys.exit(1)

    session = get_session(args.api_key)

    # sms/inbox act on one SIM and don't need the full fleet fetch
    if args.action in ('sms', 'inbox'):
        if not args.iccid:
            print('Error: --iccid required for sms/inbox', file=sys.stderr)
            sys.exit(1)
        if args.action == 'sms':
            if not args.text:
                print('Error: sms needs message text', file=sys.stderr)
                sys.exit(1)
            cmd_sms(session, args.iccid, args.text)
        else:
            cmd_inbox(session, args.iccid)
        return

    print('Fetching SIM list...')
    all_sims = fetch_all_sims(session)
    sims = filter_sims(all_sims, tag=args.tag, iccid=args.iccid)

    if not sims:
        filter_desc = ''
        if args.tag:
            filter_desc = f' matching tag "{args.tag}"'
        if args.iccid:
            filter_desc = f' with ICCID {args.iccid}'
        print(f'No SIMs found{filter_desc}.')
        sys.exit(1)

    if args.tag or args.iccid:
        print(f'Filtered to {len(sims)}/{len(all_sims)} SIMs')

    if args.action == 'status':
        cmd_status(session, sims)
    elif args.action == 'enable':
        cmd_enable(session, sims, dry_run=args.dry_run)
    elif args.action == 'disable':
        cmd_disable(session, sims, dry_run=args.dry_run)
    elif args.action == 'diagnose':
        cmd_diagnose(session, sims, args.server_url, args.manager_password)


if __name__ == '__main__':
    main()
