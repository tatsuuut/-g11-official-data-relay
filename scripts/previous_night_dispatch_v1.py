"""Check yesterday's date-specific public state after a morning attempt. Never run a model here."""
from datetime import datetime, timedelta
import json
import os
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from audit_three_phase_runtime_contract import night_feed_ready
from predeadline_dispatch_v1 import ACTIVE, Client, JST, WORKFLOW


def reconcile(now, today, current_run, site, client, verified_night):
    if today != now.date().isoformat() or not (2 <= now.hour < 6):
        return 'OUTSIDE_MORNING_WINDOW'
    day = (now.date() - timedelta(days=1)).isoformat()
    meta = site(day)
    if not meta:
        return 'NO_SAVED_DAY'
    if meta.get('date') != day or not isinstance(meta.get('races'), int) or meta['races'] <= 0:
        raise ValueError('INVALID_DAY_METADATA')
    if meta.get('stage') == 'NIGHT' and verified_night(day):
        return 'ALREADY_PUBLISHED'
    if meta.get('stage') not in ('MORNING','PREDEADLINE','NIGHT'):
        raise ValueError('UNEXPECTED_STAGE')
    path = '/actions/workflows/' + WORKFLOW
    # Check pending runs before adding to the existing serial mutation queue.
    for status in ACTIVE:
        active = client.request('GET', path+'/runs?'+urlencode({'status':status,'per_page':100}))
        if any(str(r['id']) != current_run for r in active['workflow_runs']) or active['total_count'] > 100:
            return 'OTHER_RUN_ACTIVE'
    cutoff = now.replace(hour=2, minute=0, second=0, microsecond=0)
    recent = client.request('GET',path+'/runs?'+urlencode({'created':'>='+cutoff.isoformat(), 'per_page':100}))
    if recent['total_count'] > len(recent['workflow_runs']):
        return 'INCOMPLETE_RUN_INDEX'
    if any(r['display_title'] == 'LIVE-night-'+day for r in recent['workflow_runs']):
        return 'RECOVERY_ALREADY_REQUESTED'
    client.request('POST',path+'/dispatches',{'ref':'main','inputs':{'phase':'night','operational_date':day}})
    return 'DISPATCHED_'+day


def overview(day):
    url=os.environ['G11_APP_ORIGIN'].rstrip('/')+'/api/g11-overview?'+urlencode({'date':day,'view':'home'})
    try:
        with urlopen(Request(url,headers={'Accept':'application/json'}),timeout=20) as r:
            return json.load(r)['meta']
    except HTTPError as error:
        if error.code==404:
            return None
        raise


def verified_night(day):
    url = os.environ['G11_APP_ORIGIN'].rstrip('/') + '/api/g11-feed?' + urlencode({'date': day})
    with urlopen(Request(url, headers={'Accept': 'application/json'}), timeout=30) as response:
        return night_feed_ready(json.load(response), day)['ready']


if __name__=='__main__':
    result=reconcile(datetime.now(JST),os.environ['OPERATIONAL_DATE'],os.environ['GITHUB_RUN_ID'],overview,Client(os.environ['GH_TOKEN']),verified_night)
    print('G11_PREVIOUS_NIGHT='+result,flush=True)
