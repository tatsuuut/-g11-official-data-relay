"""Publish verified official settlement/refund evidence for rescue displays.

No prediction, LOCK, formal accounting or research ledger is changed.
"""
import json
import os
import re
import sys
from dataclasses import asdict
from datetime import date,timedelta
from pathlib import Path
from deme_research_delivery_v1 import request_json,compact,sha

SCHEMA='G11_OFFICIAL_RESULT_DELIVERY_V1'

def envelope(snapshot, snapshot_sha, feed, source_sha):
    p=snapshot['PAYLOAD'];day=feed['operational_date_jst']
    if feed.get('stage')!='NIGHT' or feed.get('SNAPSHOT_CLASS')!='SAME_DAY_RESCUE_BOUNDARY':
        raise ValueError('OFFICIAL_EVIDENCE_NOT_RESCUE_NIGHT')
    if p.get('STATUS')!='NIGHT_RESULTS_COMPLETE' or p.get('OPERATIONAL_DATE_JST')!=day or p.get('PENDING_RESULT_COUNT')!=0:
        raise ValueError('OFFICIAL_EVIDENCE_INCOMPLETE')
    url='https://www1.mbrace.or.jp/od2/K/'+day[:7].replace('-','')+'/k'+day[2:].replace('-','')+'.lzh'
    receipts=[r for r in snapshot['SOURCE_RECEIPTS'] if r.get('URL')==url]
    if len(receipts)!=1 or not re.fullmatch('[a-f0-9]{64}',receipts[0]['SHA256']):
        raise ValueError('OFFICIAL_EVIDENCE_RECEIPT')
    rows={}
    for r in p['RESULTS']:
        key=r['operational_date'].replace('-','')+'-'+r['venue_code']+'-'+str(r['race_no']).zfill(2)
        if key in rows:raise ValueError('OFFICIAL_EVIDENCE_DUPLICATE')
        rows[key]={'date':r['operational_date'],'status':r['race_status'],'refund_boats':r['refund_boats'],
          'settlements':[{'trifecta':s['combination'],'payout':s['payout'],'popularity':s['popularity']} for s in r['trifecta_settlements']]}
    if set(rows)!={r['key'] for r in feed['races']}:raise ValueError('OFFICIAL_EVIDENCE_RACE_SET')
    for r in feed['races']:
        e=rows[r['key']];m=r.get('result_meta') or {}
        if e['date']!=day or e['status']!=m.get('result_status') or e['settlements']!=m.get('trifecta_settlements'):
            raise ValueError('OFFICIAL_EVIDENCE_RESULT_MISMATCH')
    return {'schema':SCHEMA,'source_feed_sha256':source_sha,'snapshot_sha256':snapshot_sha,
        'evidence':{'operational_date':day,'source_url':url,'archive_sha256':receipts[0]['SHA256'],'rows':rows}}

def publish(state, feed_path, origin, request=request_json):
    feed=json.loads(Path(feed_path).read_bytes())
    current=feed.get('stage')=='NIGHT' and feed.get('SNAPSHOT_CLASS')=='SAME_DAY_RESCUE_BOUNDARY'
    day=feed['operational_date_jst'] if current else (date.fromisoformat(feed['operational_date_jst'])-timedelta(days=1)).isoformat()
    try:accepted,headers=request(origin+'/api/g11-feed?date='+day)
    except ValueError as exc:
        if str(exc)=='RESEARCH_HTTP_404':return {'status':'NOT_APPLICABLE'}
        raise
    if accepted.get('stage')!='NIGHT' or accepted.get('SNAPSHOT_CLASS')!='SAME_DAY_RESCUE_BOUNDARY':return {'status':'NOT_APPLICABLE'}
    existing,_=request(origin+'/api/g11-feed?date='+day+'&view=v53-final')
    if existing.get('official_result_evidence'):
        return {'status':'PASS','date':day,'site_get_readback':'PASS','duplicate':True}
    sys.path.insert(0,str(Path(state).resolve().parent/'.g11-private'))
    from g11.canonical import official_pipeline as official
    store=official.OfficialArtifactStore(Path(state)/('rescue/store' if current else 'store'))
    if current:
        verified=store.load_verified_snapshot('night','official-day-results')
        snapshot=verified['SNAPSHOT']
    else:
        # Catch up the previous published rescue using K bytes already bound to
        # today's verified MORNING input. No acquisition or forecast replay.
        verified=store.load_verified_snapshot('morning','official-day-input')
        source='result-k-'+day.replace('-','');receipt=store.response_receipt(source)
        if receipt not in verified['SNAPSHOT']['SOURCE_RECEIPTS']:raise ValueError('OFFICIAL_EVIDENCE_MORNING_BINDING')
        raw=store.response_bytes(source)
        from rescue_night_result_parser_v1 import parse_night_results
        member='K'+day[2:].replace('-','')+'.TXT'
        results=parse_night_results(official.decode_cp932(official.LzhExtractor().extract_one(raw,member)),date.fromisoformat(day),official,official.parse_result_text,require_full_races=True)
        # A transient result projection, bound to the real MORNING snapshot and
        # its unchanged K receipt; this is never persisted as a NIGHT snapshot.
        snapshot={'SOURCE_RECEIPTS':[receipt],'PAYLOAD':{'STATUS':'NIGHT_RESULTS_COMPLETE','OPERATIONAL_DATE_JST':day,
            'PENDING_RESULT_COUNT':0,'RESULTS':[asdict(r) for r in results]}}
    source_sha=headers.get('x-g11-source-feed-sha256','')
    if not re.fullmatch('[a-f0-9]{64}',source_sha):raise ValueError('OFFICIAL_EVIDENCE_FEED_HASH')
    body=compact(envelope(snapshot,verified['SNAPSHOT_SHA256'],accepted,source_sha))
    ack,_=request(origin+'/api/g11-official-result-evidence',body,os.environ['G11_SYNC_OIDC'])
    if ack.get('status')!='PASS' or ack.get('payload_sha256')!=sha(body):raise ValueError('OFFICIAL_EVIDENCE_SAVE')
    view,_=request(origin+'/api/g11-feed?date='+day+'&view=v53-final')
    if view.get('official_result_evidence')!=json.loads(body)['evidence']:raise ValueError('OFFICIAL_EVIDENCE_DISPLAY_READBACK')
    return {'status':'PASS','date':day,'races':len(view['official_result_evidence']['rows']),
        'payload_sha256':sha(body),'site_get_readback':'PASS','prediction_recalculated':False}

def optional_publish(state,feed,origin):
    try:result=publish(state,feed,origin)
    except Exception as exc:
        reason=str(exc) if isinstance(exc,ValueError) and re.fullmatch('[A-Z0-9_]{1,100}',str(exc)) else type(exc).__name__
        result={'status':'HELD','reason':reason}
    print(json.dumps({'official_result_delivery':result},sort_keys=True))
    return result
