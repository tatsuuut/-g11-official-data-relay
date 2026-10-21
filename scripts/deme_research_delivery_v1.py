"""Publish a bounded display copy after canonical feed delivery succeeds.

The retained private research index remains authoritative. This optional step
never rewrites a feed, prediction, LOCK, model, or settlement.
"""
import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

RELEASE='G11_DEME_CONTINUITY_20261021_V1'
SCHEMA='G11_DEME_RESEARCH_DELIVERY_V1'

def compact(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()

def sha(value):return hashlib.sha256(value).hexdigest()

def request_json(url, data=None, token=None):
    headers={'Cache-Control':'no-cache'}
    if data is not None:
        headers.update({'Authorization':'Bearer '+token,'Content-Type':'application/json','x-g11-feed-sha256':sha(data)})
    request=urllib.request.Request(url,data=data,headers=headers)
    try:
        with urllib.request.urlopen(request,timeout=25) as response:
            raw=response.read(40_000_001)
            if len(raw)>40_000_000:raise ValueError('RESEARCH_READ_LIMIT')
            return json.loads(raw),dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        raise ValueError('RESEARCH_HTTP_'+str(exc.code)) from None

def publish(state, feed_path, origin, request=request_json):
    projection_path=feed_path.parent/'deme-discovery-public.json'
    if not projection_path.is_file():return {'status':'NOT_AVAILABLE','reason':'CURRENT_RESEARCH_PROJECTION_MISSING'}
    if projection_path.stat().st_size>256000:raise ValueError('RESEARCH_PUBLIC_BUDGET')
    p=json.loads(projection_path.read_bytes());feed=json.loads(feed_path.read_bytes())
    runtime=p.get('runtime') or {};day=feed['operational_date_jst']
    if (runtime.get('implementation')!=RELEASE or runtime.get('operational_date')!=day
        or p.get('available') is not True or p.get('production_mutated') is not False
        or p.get('automatic_adoption') is not False):raise ValueError('RESEARCH_PROJECTION_CONTRACT')
    path=state/'store/shadow/original-display/v2/deme-discovery.json'
    canonical=json.loads(path.read_bytes())
    digest=sha(compact({k:v for k,v in canonical.items() if k!='state_integrity_sha256'}))
    if canonical.get('state_integrity_sha256')!=digest or p.get('source_state_sha256')!=digest:
        raise ValueError('RESEARCH_CANONICAL_BINDING')
    accepted,headers=request(origin+'/api/g11-feed?date='+day)
    source_sha={k.lower():v for k,v in headers.items()}.get('x-g11-source-feed-sha256','')
    if accepted.get('status')!='PASS' or accepted.get('operational_date_jst')!=day or not re.fullmatch('[a-f0-9]{64}',source_sha):
        raise ValueError('RESEARCH_ACCEPTED_FEED_BINDING')
    if (accepted.get('same_day_rescue') or {}).get('authority_sha256')!=(feed.get('same_day_rescue') or {}).get('authority_sha256'):
        raise ValueError('RESEARCH_RESCUE_AUTHORITY_JOIN')
    envelope=dict(schema=SCHEMA,operational_date=day,updated_at=p['updated_at'],source_feed_sha256=source_sha,projection=p)
    data=compact(envelope)
    ack,_=request(origin+'/api/g11-deme-research',data,os.environ['G11_SYNC_OIDC'])
    if ack.get('status')!='PASS' or ack.get('payload_sha256')!=sha(data):raise ValueError('RESEARCH_SAVE_READBACK')
    visible,_=request(origin+'/api/g11-feed?view=p3-deme&through='+day)
    if (visible.get('publication') or {}).get('payload_sha256')!=sha(data) or visible.get('source_state_sha256')!=digest:
        raise ValueError('RESEARCH_PUBLIC_READBACK')
    return dict(status='PASS',schema=SCHEMA,operational_date=day,updated_at=p['updated_at'],
        payload_sha256=sha(data),source_state_sha256=digest,site_get_readback='PASS',production_mutated=False)

def optional_publish(state, feed_path, origin):
    try:result=publish(Path(state),Path(feed_path),origin)
    except Exception as exc:
        reason=str(exc) if isinstance(exc,ValueError) and str(exc).startswith('RESEARCH_') else type(exc).__name__
        result=dict(status='HELD',reason=reason[:100],production_mutated=False)
    print(json.dumps({'deme_research_delivery':result},ensure_ascii=False,sort_keys=True))
    return result
