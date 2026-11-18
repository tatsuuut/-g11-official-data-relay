"""Check yesterday's date-specific public state after a morning attempt. Never run a model here."""
from datetime import datetime, timedelta
import json
import os
import subprocess
from urllib.parse import urlencode

from audit_three_phase_runtime_contract import night_feed_ready
from predeadline_dispatch_v1 import ACTIVE, Client, JST, WORKFLOW


def reconcile(now, today, current_run, site, client):
    if today != now.date().isoformat() or not (2 <= now.hour < 6):
        return 'OUTSIDE_MORNING_WINDOW'
    day = (now.date() - timedelta(days=1)).isoformat()
    feed = site(day)
    if feed is None:
        return 'NO_SAVED_DAY'
    races = feed.get('races')
    if (feed.get('operational_date_jst') != day or feed.get('status') != 'PASS'
            or not isinstance(races, list) or not races or feed.get('counts', {}).get('races') != len(races)):
        raise ValueError('INVALID_DAY_METADATA')
    if feed.get('stage') == 'NIGHT' and night_feed_ready(feed, day)['ready']:
        return 'ALREADY_PUBLISHED'
    if feed.get('stage') not in ('MORNING','PREDEADLINE','NIGHT'):
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
    prior = [run for run in recent['workflow_runs'] if run['display_title'] == 'LIVE-night-'+day]
    if len(prior) >= 2:
        return 'MORNING_RECOVERY_LIMIT'
    if prior:
        attempt = prior[0]
        if attempt.get('status') != 'completed' or not attempt.get('updated_at'):
            return 'RECOVERY_ALREADY_REQUESTED'
        finished = datetime.fromisoformat(attempt['updated_at'].replace('Z', '+00:00'))
        if finished.tzinfo is None or now < finished + timedelta(hours=1):
            return 'RECOVERY_COOLDOWN'
        # The date-bound feed is still incomplete. Allow one normal retry
        # with all previously recorded per-source refusals and caps intact.
    client.request('POST',path+'/dispatches',{'ref':'main','inputs':{'phase':'night','operational_date':day}})
    return 'DISPATCHED_'+day


def read_feed(day):
    # Use the relay's date-bound publication endpoint and the same transport as its NIGHT handoff.
    url = os.environ['G11_APP_ORIGIN'].rstrip('/') + '/api/g11-feed?' + urlencode({'date': day})
    reply = subprocess.run(['curl', '--silent', '--show-error', '--location',
                            '--connect-timeout', '10', '--max-time', '30',
                            '--write-out', '\n%{http_code}', url], check=True, stdout=subprocess.PIPE)
    body, status = reply.stdout.rsplit(b'\n', 1)
    if status == b'404':
        return None
    if status != b'200':
        raise RuntimeError('NIGHT_FEED_HTTP_' + status.decode('ascii'))
    return json.loads(body)


if __name__ == '__main__':
    result = reconcile(datetime.now(JST), os.environ['OPERATIONAL_DATE'], os.environ['GITHUB_RUN_ID'],
                       read_feed, Client(os.environ['GH_TOKEN']))
    print('G11_PREVIOUS_NIGHT=' + result, flush=True)
