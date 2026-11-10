"""Retry independent research from verified saved MORNING inputs after publication."""
import argparse
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import sys

from deme_research_delivery_v1 import compact, request_json

JST = timezone(timedelta(hours=9))
RESEARCH = Path('store/shadow/original-display/v2/deme-discovery.json')


def recover(state, source, day, feed_path, *, request=request_json, runner=None):
    if datetime.now(JST).date().isoformat() != day:
        raise ValueError('DEME_RECOVERY_CURRENT_DAY')
    feed, headers = request(os.environ['G11_APP_ORIGIN']+'/api/g11-feed?date='+day)
    source_sha = {k.lower():v for k,v in headers.items()}.get('x-g11-source-feed-sha256','')
    if (feed.get('status') != 'PASS' or feed.get('operational_date_jst') != day
            or feed.get('stage') not in ('MORNING','PREDEADLINE')
            or not re.fullmatch('[a-f0-9]{64}', source_sha)
            or not feed.get('races') or feed.get('same_day_rescue')):
        raise ValueError('DEME_RECOVERY_ACCEPTED_FEED')
    # The feed is a display copy. The observer verifies each original RAW,
    # response receipt, morning snapshot SHA, deadline and result absence.
    feed_path.parent.mkdir(parents=True, exist_ok=True)
    feed_path.write_bytes(compact(feed)+b'\n')
    if runner is None:
        sys.path.insert(0,str(source/'g11/canonical'))
        runner = importlib.import_module('deme_discovery_thirty_entry').run
    result = runner(state/'store',feed_path,feed_path.parent/'deme-discovery-public.json',fast=True)
    if result.get('readback') != 'PASS':
        raise ValueError('DEME_RECOVERY_SAVED_READBACK')
    return result


def inventory(root):
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file() and p.relative_to(root) != RESEARCH}


def observations(state):
    saved = json.loads((state/RESEARCH).read_bytes())['thirty_focus_v1']
    return {key:copy.deepcopy((saved if key == 'legacy' else saved[key])['observations'])
            for key in ('legacy','replacement','nikoshi_a')}


def audit(state, source, day, feed_path):
    # This is only called on an extracted, disposable authenticated cache copy.
    before = inventory(state); old = observations(state)
    result = recover(state,source,day,feed_path)
    first = observations(state)
    assert inventory(state) == before, 'DEME_PROTECTED_FILES_CHANGED'
    for key, rows in old.items():
        assert all(first[key][ident] == row for ident,row in rows.items()), 'DEME_OLD_OBSERVATION_CHANGED'
    retry = recover(state,source,day,feed_path)
    assert observations(state) == first, 'DEME_DUPLICATE_RETRY'
    assert inventory(state) == before, 'DEME_RETRY_CHANGED_PROTECTED_FILES'
    assert result['capture']['status'] == retry['capture']['status'] == 'PASS'
    report = {}
    for key in ('replacement','nikoshi_a'):
        rows = [r for r in first[key].values() if r['race_key'][:8] == day.replace('-','')]
        assert rows, 'DEME_CURRENT_RECORDS_MISSING:'+key
        assert all(datetime.fromisoformat(r['saved_at']) < datetime.fromisoformat(r['deadline']) for r in rows)
        report[key] = dict(records=len(rows),matched=sum(r['condition_match'] is True for r in rows))
    print(json.dumps(dict(saved_deme_audit='PASS',protected_files=len(before),cohorts=report,
        no_predictor_invoked=True,official_acquisition=0,publication=False),sort_keys=True))
    return result


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--state-root',type=Path,required=True)
    p.add_argument('--source-root',type=Path,required=True)
    p.add_argument('--day',required=True)
    p.add_argument('--feed',type=Path,required=True)
    p.add_argument('--audit',action='store_true')
    a=p.parse_args()
    fn = audit if a.audit else recover
    result=fn(a.state_root.resolve(),a.source_root.resolve(),a.day,a.feed.resolve())
    print(json.dumps({'saved_deme_recovery':result},ensure_ascii=False,sort_keys=True))


if __name__ == '__main__':
    main()
