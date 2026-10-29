"""Publish a bounded display copy after canonical feed delivery succeeds.

The retained private research index remains authoritative. This optional step
never rewrites a feed, prediction, LOCK, model, or settlement.
"""
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

RELEASE='G11_DEME_CONTINUITY_20261021_V1'
SCHEMA='G11_DEME_RESEARCH_DELIVERY_V1'

def compact(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()

def sha(value):return hashlib.sha256(value).hexdigest()

def request_json(url, data=None, token=None):
    # Use the same curl transport as the accepted operational delivery.
    # The edge rejects urllib's default client with HTTP 403 / error 1010.
    headers={'Cache-Control':'no-cache'}
    if data is not None:
        headers.update({'Authorization':'Bearer '+token,'Content-Type':'application/json','x-g11-feed-sha256':sha(data)})
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory);body=root/'response.json';response_headers=root/'headers.txt'
        args=['curl','--silent','--show-error','--max-time','25','--max-filesize','40000000',
            '--config','-','--dump-header',str(response_headers),'--output',str(body),'--write-out','%{http_code}']
        if data is not None:
            payload=root/'payload.json';payload.write_bytes(data)
            args+=['--request','POST','--data-binary','@'+str(payload)]
        # Keep the publisher credential off argv, disk and logs.
        config=''.join('header = '+json.dumps(k+': '+v)+'\n' for k,v in headers.items())
        result=subprocess.run(args+[url],input=config,text=True,capture_output=True,timeout=30)
        if result.returncode:raise ValueError('RESEARCH_TRANSPORT_FAILED')
        raw=body.read_bytes()
        if len(raw)>40000000:raise ValueError('RESEARCH_READ_LIMIT')
        if not result.stdout.isdigit() or not 200<=int(result.stdout)<300:
            reason=''
            try:
                code=json.loads(raw).get('error')
                if isinstance(code,str) and re.fullmatch('[A-Z0-9_]{1,80}',code):reason='_'+code
            except (ValueError,AttributeError):pass
            raise ValueError('RESEARCH_HTTP_'+result.stdout+reason)
        parsed_headers={}
        for line in response_headers.read_text().splitlines():
            if line.startswith('HTTP/'):parsed_headers={}
            elif ':' in line:
                key,value=line.split(':',1);parsed_headers[key.lower()]=value.strip()
        return json.loads(raw),parsed_headers

def publish(state, feed_path, origin, request=request_json):
    projection_path=feed_path.parent/'deme-discovery-public.json'
    if not projection_path.is_file():return {'status':'NOT_AVAILABLE','reason':'CURRENT_RESEARCH_PROJECTION_MISSING'}
    if projection_path.stat().st_size>320000:raise ValueError('RESEARCH_PUBLIC_BUDGET')
    p=json.loads(projection_path.read_bytes());feed=json.loads(feed_path.read_bytes())
    if len(compact({k:v for k,v in p.items() if k!='thirty_focus_packed'}))>256000:
        raise ValueError('RESEARCH_LEGACY_PUBLIC_BUDGET')
    thirty=p.get('thirty_focus_packed')
    if thirty is not None and (not isinstance(thirty,dict) or
        thirty.get('schema')!='G11_THIRTY_VIEW_PACKED_V1' or
        not isinstance(thirty.get('data'),str) or len(thirty['data'])>48000):
        raise ValueError('RESEARCH_THIRTY_BUDGET')
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
    if thirty is not None:
        shown=visible.get('thirty_focus') or {};saved=canonical.get('thirty_focus_v1') or {}
        registration=shown.get('registration') or {};selection=saved.get('selection') or {}
        if (shown.get('transport_sha256')!=thirty.get('sha256') or
            registration.get('status')!='SAVED_READBACK_VERIFIED' or
            registration.get('registered_at')!=selection.get('activated_at') or
            registration.get('requested_start_date_jst')!=selection.get('requested_start_date_jst') or
            (shown.get('observation') or {}).get('saved_records')!=len(saved.get('observations',{}))):
            raise ValueError('RESEARCH_THIRTY_PUBLIC_READBACK')
    return dict(status='PASS',schema=SCHEMA,operational_date=day,updated_at=p['updated_at'],
        payload_sha256=sha(data),source_state_sha256=digest,site_get_readback='PASS',production_mutated=False)

def optional_publish(state, feed_path, origin):
    try:result=publish(Path(state),Path(feed_path),origin)
    except Exception as exc:
        reason=str(exc) if isinstance(exc,ValueError) and str(exc).startswith('RESEARCH_') else type(exc).__name__
        result=dict(status='HELD',reason=reason[:100],production_mutated=False)
    print(json.dumps({'deme_research_delivery':result},ensure_ascii=False,sort_keys=True))
    return result
