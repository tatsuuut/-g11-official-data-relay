"""Collect/publish an isolated saved-packet audit, without acquiring inputs."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys

ORIGIN='https://g11-race-lab.higatatsunori2.chatgpt.site'

def load_file(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module);return module

def modules(source,study,fixed):
    sys.path.insert(0,str(source.resolve()))
    from g11.canonical.official_pipeline import OfficialArtifactStore
    legacy=load_file('g11.growth_p3.v1_1.mashiro_tactical_research_v1',study/'g11/growth_p3/v1_1/mashiro_tactical_research_v1.py')
    observer=load_file('g11_p3_paired_mashiro',study/'g11/research/mashiro_p3_bridge_v1/observer.py')
    frozen=load_file('g11_p3_paired_fixed_input',fixed/'g11/research/mashiro_fixed_v1/observer.py')
    return OfficialArtifactStore,legacy,observer,frozen

def report_path(state,day):return state/'store/research/mashiro-p3-bridge-v1'/day/'report.json'

def collect(state,source,study,fixed,day,checkpoint,*,save=True):
    import research_state_isolation_v1 as isolation
    Store,legacy,observer,frozen=modules(source,study,fixed)
    report=observer.collect(Store(state/'store'),day,legacy,frozen)
    report.pop('content_sha256');report['source_checkpoint']=checkpoint;report['content_sha256']=observer.digest(report)
    path=report_path(state,day);changed=True
    if path.is_file():
        prior=json.loads(path.read_bytes());claimed=prior.pop('content_sha256',None)
        isolation.require(claimed==observer.digest(prior),'MASHIRO_BRIDGE_PRIOR_HASH')
        known={r['race']:r for r in prior['records'] if r['status']=='READY'}
        for i,row in enumerate(report['records']):
            old=known.get(row['race'])
            if old is None:continue
            if row['status']!='READY':report['records'][i]=old;continue
            isolation.require(all(row['sources'][k]==old['sources'][k] for k in ('p3','legacy','packet','proof','pin')),'MASHIRO_BRIDGE_FROZEN_SOURCE_CHANGED')
            if old['sources']['fixed'] is not None:isolation.require(row['sources']['fixed']==old['sources']['fixed'],'MASHIRO_BRIDGE_FIXED_SOURCE_CHANGED')
            if old['finance'] is not None:
                isolation.require(row['finance'] is None or row['finance']==old['finance'],'MASHIRO_BRIDGE_SETTLED_FINANCE_CHANGED')
                row['finance']=old['finance'];row['result_status']='SETTLED';row['result_sha']=old['result_sha'];row['result_heads']=old.get('result_heads')
        report['coverage']=dict(__import__('collections').Counter(r['status'] for r in report['records']))
        report['ready']=sum(r['status']=='READY' for r in report['records'])
        report.pop('content_sha256');report['content_sha256']=observer.digest(report)
        a={k:v for k,v in report.items() if k not in ('content_sha256','updated_at','source_checkpoint')}
        b={k:v for k,v in prior.items() if k not in ('updated_at','source_checkpoint')}
        changed=a!=b
        if not changed:report=json.loads(path.read_bytes())
    if save and changed:isolation.atomic(path,observer.canonical(report)+b'\n')
    from collections import Counter
    return dict(STATUS='PAIRED_AUDIT_RECORDED',CHANGED=changed if save else False,PLANNED=report['planned'],READY=report['ready'],
        COVERAGE=dict(Counter(r.get('reason','READY') for r in report['records'])),
        SETTLED=sum(r.get('result_status')=='SETTLED' for r in report['records']),
        PROTOCOL_SHA256=report['protocol_sha256'],OFFICIAL_ACQUISITION=0,PREDICTION_GENERATED=False,PRODUCTION_MUTATED=False)

def publish(state,study,day):
    from urllib.request import Request,urlopen
    from deme_research_delivery_v1 import compact,sha,request_json
    import research_state_isolation_v1 as isolation
    isolation.require(os.environ.get('RESEARCH_CHECKPOINT_VERIFIED')=='true' and os.environ.get('G11_APP_ORIGIN')==ORIGIN,'MASHIRO_BRIDGE_PERSISTENCE_REQUIRED')
    observer=load_file('g11_p3_paired_public',study/'g11/research/mashiro_p3_bridge_v1/observer.py')
    report=json.loads(report_path(state,day).read_bytes());claimed=report.pop('content_sha256')
    isolation.require(claimed==observer.digest(report) and report['protocol_sha256']==observer.configuration()[1],'MASHIRO_BRIDGE_SAVED_HASH')
    projection=observer.public_report(report)
    value=dict(schema='G11_MASHIRO_P3_BRIDGE_DELIVERY_V1',day=day,source_checkpoint=report['source_checkpoint'],
        private_report_sha256=claimed,projection=projection)
    isolation.require(os.environ.get('G11_OIDC_AUDIENCE')=='g11-race-lab','MASHIRO_BRIDGE_AUDIENCE')
    req=Request(os.environ['ACTIONS_ID_TOKEN_REQUEST_URL']+'&audience=g11-race-lab',headers={
        'Authorization':'bearer '+os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN'],'Accept':'application/json'})
    with urlopen(req,timeout=20) as response:token=json.loads(response.read())['value']
    raw=compact(value);isolation.require(len(raw)<=2500000,'MASHIRO_BRIDGE_BUDGET')
    ack,_=request_json(ORIGIN+'/api/g11-mashiro-p3',raw,token)
    isolation.require(ack.get('status')=='PASS' and ack.get('payload_sha256')==sha(raw),'MASHIRO_BRIDGE_SAVE_READBACK')
    visible,_=request_json(ORIGIN+'/api/g11-mashiro-p3?through='+day)
    isolation.require(visible.get('projection')==projection and visible.get('publication',{}).get('payload_sha256')==sha(raw),'MASHIRO_BRIDGE_APP_READBACK')
    return dict(STATUS='PASS',DAY=day,READY=projection['ready'],PLANNED=projection['planned'],PAYLOAD_SHA256=sha(raw),OFFICIAL_ACQUISITION=0)

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--mode',choices=('audit','collect','publish'),required=True)
    p.add_argument('--state-root',type=Path,required=True);p.add_argument('--source-root',type=Path,default=Path('.g11-private'))
    p.add_argument('--study-root',type=Path,default=Path('.g11-mashiro-bridge'));p.add_argument('--fixed-root',type=Path,default=Path('.g11-mashiro-fixed'))
    p.add_argument('--day',required=True);p.add_argument('--source-checkpoint',default='')
    a=p.parse_args();state=a.state_root.resolve()
    try:
        before={p.relative_to(state).as_posix():(p.stat().st_size,p.stat().st_mtime_ns) for p in state.rglob('*') if p.is_file()}
        result=publish(state,a.study_root,a.day) if a.mode=='publish' else collect(state,a.source_root,a.study_root,a.fixed_root,a.day,a.source_checkpoint,save=a.mode=='collect')
        if a.mode=='audit':
            after={p.relative_to(state).as_posix():(p.stat().st_size,p.stat().st_mtime_ns) for p in state.rglob('*') if p.is_file()}
            if before!=after:raise ValueError('MASHIRO_BRIDGE_AUDIT_MUTATED_INPUT')
        print('G11_MASHIRO_P3_BRIDGE='+json.dumps(result,sort_keys=True),flush=True)
    except Exception as exc:
        reason=str(exc) if isinstance(exc,ValueError) and str(exc).startswith(('MASHIRO_','RESEARCH_')) else type(exc).__name__
        print('G11_MASHIRO_P3_BRIDGE='+json.dumps(dict(STATUS='UNAVAILABLE',REASON=reason[:150],PRODUCTION_MUTATED=False)),flush=True)
        if a.mode=='audit':raise SystemExit(1)

if __name__=='__main__':main()
