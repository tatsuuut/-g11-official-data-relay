"""Read saved fixed-study evidence into an optional app display; no acquisition."""
import argparse
from collections import Counter
from datetime import datetime, timezone, timedelta
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

JST=timezone(timedelta(hours=9))
ORIGIN='https://g11-race-lab.higatatsunori2.chatgpt.site'

def require(ok, code):
    if not ok:raise ValueError('MASHIRO_DISPLAY_'+code)

def projection(store, observer, day, source_checkpoint, *, now=None):
    now=now or datetime.now(JST)
    config,policy,_=observer.configuration()
    require(re.fullmatch(r'\d{4}-\d{2}-\d{2}',day) and day<=now.astimezone(JST).date().isoformat(),'DAY')
    require(re.fullmatch(r'g11-state-v4-(critical|published)-'+day+r'-\d+(?:-\d+-checkpoint)?',source_checkpoint),'CHECKPOINT')
    morning=store.load_verified_snapshot('morning','official-day-input')
    m=observer.verified(morning,'morning','official-day-input')
    require(m['OPERATIONAL_DATE_JST']==day,'MORNING_DAY')
    races={observer.race_key(r):r for r in m['RACES']}
    planned=set(races)|{r['RACE_KEY'] for r in m.get('QUARANTINED_RACES',[])}
    require(len(races)==len(m['RACES']) and all(k[:8]==day.replace('-','') for k in planned),'SCHEDULE')
    p=dict(schema='G11_MASHIRO_FIXED_DISPLAY_V1',day=day,state='AWAITING_CAPTURE',updated_at=now.isoformat(),
           protocol_sha256=policy,planned=len(planned),recorded=None,coverage={},rows=[],
           p3_outputs_used=False,production_mutated=False,official_acquisition=0)
    if day<config['study_start_jst']:
        p.update(state='NOT_STARTED',recorded=0)
    else:
        require(day<=config['study_end_jst'],'OUTSIDE_WINDOW')
        coverage=store.root/'research/mashiro-fixed-v1'/day/'capture-coverage.json'
        if coverage.is_file():
            require(not coverage.is_symlink(),'COVERAGE_SYMLINK')
            c=json.loads(coverage.read_bytes())
            require(c['SCHEMA']=='G11_MASHIRO_FIXED_COVERAGE_V1' and c['PROTOCOL_SHA256']==policy
                    and c['DAY']==day and c['PHASE']=='capture','COVERAGE_BINDING')
            report=c['REPORT'];items=report['RACES'];keys=[r['RACE_KEY'] for r in items]
            require(set(keys)==planned and len(keys)==len(planned) and report['PLANNED_RACES']==len(planned),'COVERAGE_RACES')
            recorded=[r['RACE_KEY'] for r in items if r['STATUS'] in ('RECORDED','ALREADY_RECORDED')]
            require(report['RECORDED']==len(recorded),'COVERAGE_COUNT')
            p.update(state='RECORDED',recorded=len(recorded),coverage=dict(Counter(r['STATUS'] for r in items)))
            for key in sorted(recorded):
                record=store.load_verified_snapshot('predeadline',observer.observation_name(key))
                obs=observer.validate_observation(record,key,path=observer.observation_paths(store,key))
                require(obs['OPERATIONAL_DATE_JST']==day and obs['MORNING_SNAPSHOT_SHA256']==morning['SNAPSHOT_SHA256'],'OBSERVATION_SOURCE')
                rows={r['boat']:r for r in obs['INPUT_ROWS']}
                entries={r['boat']:r for r in races[key]['entries']}
                result=None;result_sha=None
                path=store.root/'snapshots/night'/(observer.result_name(key)+'.json')
                if path.is_file():
                    rr=store.load_verified_snapshot('night',observer.result_name(key))
                    result=observer.verified(rr,'night',observer.result_name(key));result_sha=rr['SNAPSHOT_SHA256']
                    require(result['SCHEMA']=='G11_MASHIRO_FIXED_FIRST_PLACE_RESULT_V1' and result['PROTOCOL_SHA256']==policy
                            and result['RACE_KEY']==key and result['OBSERVATION_SNAPSHOT_SHA256']==record['SNAPSHOT_SHA256']
                            and result['DEADLINE_AT_JST']==obs['DEADLINE_AT_JST'] and result['PRODUCTION_MUTATED'] is False
                            and observer.moment(result['JOINED_AT_JST'])>=observer.moment(obs['DEADLINE_AT_JST']),'RESULT_BINDING')
                    expected={(c['basis'],t['boat']) for c in obs['COHORTS'] for t in c['targets']}
                    require(len(result['RESULTS'])==len(expected) and {(r['basis'],r['boat']) for r in result['RESULTS']}==expected,'RESULT_TARGETS')
                for cohort in obs['COHORTS']:
                    coverage_key=cohort['basis']+'_'+cohort['status']
                    p['coverage'][coverage_key]=p['coverage'].get(coverage_key,0)+1
                    for target in cohort['targets']:
                        b=target['boat'];source=rows[b];entry=entries[b];status='WAITING'
                        require(entry['racer_number']==target['racer_number'],'ROSTER')
                        if result:
                            match=next(r for r in result['RESULTS'] if r['basis']==cohort['basis'] and r['boat']==b)
                            require(all(match[k]==v for k,v in target.items()) and match['outcome'] in ('WIN','LOSS','REFUNDED','VOID'),'RESULT_FIXED_FIELDS')
                            status=match['outcome']
                        elif now>=observer.moment(obs['DEADLINE_AT_JST']) and (store.root/'research/mashiro-fixed-v1'/day/'result-coverage.json').is_file():
                            status='UNRESOLVED'
                        p['rows'].append(dict(race=key,date=day,venue=int(key[9:11]),boat=b,position=target['position'],basis=cohort['basis'],
                            grade=target['grade'],racer_number=target['racer_number'],name=entry.get('racer_name',''),
                            rank=target['exhibition_rank'],national=source['national_win_rate'],motor=source['motor'],
                            outcome=status,conditions=target['conditions'],captured_at=obs['CAPTURED_AT_JST'],deadline=obs['DEADLINE_AT_JST'],
                            observation_sha256=record['SNAPSHOT_SHA256'],result_sha256=result_sha))
    return dict(schema='G11_MASHIRO_FIXED_DISPLAY_DELIVERY_V1',operational_date=day,updated_at=p['updated_at'],
        source_checkpoint=source_checkpoint,source_morning_sha256=morning['SNAPSHOT_SHA256'],projection=p)

def publish(delivery, *, request=None, opener=None):
    from urllib.request import Request,urlopen
    from deme_research_delivery_v1 import compact,sha,request_json
    request=request or request_json;opener=opener or urlopen
    require(os.environ.get('RESEARCH_CHECKPOINT_VERIFIED')=='true' and os.environ.get('G11_APP_ORIGIN')==ORIGIN,'PERSISTENCE_REQUIRED')
    require(os.environ.get('G11_OIDC_AUDIENCE')=='g11-race-lab','AUDIENCE')
    auth=Request(os.environ['ACTIONS_ID_TOKEN_REQUEST_URL']+'&audience=g11-race-lab',headers={
        'Authorization':'bearer '+os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN'],'Accept':'application/json'})
    with opener(auth,timeout=20) as response:token=json.loads(response.read())['value']
    raw=compact(delivery);require(len(raw)<=1500000,'BUDGET')
    ack,_=request(ORIGIN+'/api/g11-mashiro-research',raw,token)
    require(ack.get('status')=='PASS' and ack.get('payload_sha256')==sha(raw),'SAVE_READBACK')
    readback,_=request(ORIGIN+'/api/g11-mashiro-research?through='+delivery['operational_date'])
    require(readback.get('available') is True and readback.get('projection')==delivery['projection']
            and readback.get('publication',{}).get('payload_sha256')==sha(raw),'APP_READBACK')
    return {'status':'PASS','day':delivery['operational_date'],'state':delivery['projection']['state'],
        'recorded':delivery['projection']['recorded'],'targets':len(delivery['projection']['rows']),
        'payload_sha256':sha(raw),'official_acquisition':0,'production_mutated':False}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-root',type=Path,required=True);parser.add_argument('--source-root',type=Path,required=True)
    parser.add_argument('--observer-root',type=Path,required=True);parser.add_argument('--day',required=True)
    parser.add_argument('--source-checkpoint',required=True);parser.add_argument('--audit-only',action='store_true')
    a=parser.parse_args()
    try:
        sys.path.insert(0,str(a.source_root.resolve()))
        from g11.canonical.official_pipeline import OfficialArtifactStore
        spec=importlib.util.spec_from_file_location('g11_mashiro_fixed_display',a.observer_root/'g11/research/mashiro_fixed_v1/observer.py')
        observer=importlib.util.module_from_spec(spec);spec.loader.exec_module(observer)
        state=a.state_root.resolve();before={p.relative_to(state).as_posix():(p.stat().st_size,p.stat().st_mtime_ns) for p in state.rglob('*') if p.is_file()}
        delivery=projection(OfficialArtifactStore(state/'store'),observer,a.day,a.source_checkpoint)
        after={p.relative_to(state).as_posix():(p.stat().st_size,p.stat().st_mtime_ns) for p in state.rglob('*') if p.is_file()}
        require(before==after,'MUTATED_SAVED_INPUT')
        result={'status':'PASS','audit_only':True,'state':delivery['projection']['state'],
                'day':a.day,'official_acquisition':0,'production_mutated':False} if a.audit_only else publish(delivery)
    except Exception as exc:
        reason=str(exc) if isinstance(exc,ValueError) and str(exc).startswith(('MASHIRO_DISPLAY_','RESEARCH_')) else type(exc).__name__
        result={'status':'UNAVAILABLE','reason':reason[:150],'production_mutated':False}
        print('G11_MASHIRO_APP_DISPLAY='+json.dumps(result,sort_keys=True),flush=True)
        if a.audit_only:raise SystemExit(1)
        return
    print('G11_MASHIRO_APP_DISPLAY='+json.dumps(result,sort_keys=True),flush=True)

if __name__=='__main__':main()
