"""Republish an authenticated, already-settled ledger; never calculate predictions or results."""
from __future__ import annotations
import argparse
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace
import urllib.request
import zipfile
from wild4_night_recovery_20261013 import (
    require, canonical, digest, read, inventory, functions, extract_encrypted, api, SETTLEMENT_FIELDS)

DAY = '2026-11-03'
MODE = 'wild4-republish-20261103'
ORIGIN = 'https://g11-race-lab.higatatsunori2.chatgpt.site'
REPO = 'tatsuuut/-g11-official-data-relay'
RUN = 41649800291
ARTIFACT = 13237366373
NAME = 'g11-wild4-20261103-future-oos-locks-encrypted'
ARTIFACT_SHA = 'deaa087cfa0cb8dc4027d0652d9650a198c22b6416218c1cac185ac8edee45cf'
RUN_SHA = 'fdfb52466b0511334f321193619a8f3f85fc5333'
MANIFEST = 'af9c665796062e15e47f0b1da7cc0eb0652f441f51dd7d41c298e68356abf99f'
MODEL = 'da88e71f7a5fcc8254031c8b5ad574a1bb551812217dedae8f9c21dd06a25f98'
GENERATION = '06032b26d9aabc8a9c4eda6b91ea6ed6269944b001153f523f51649b36f5a1be'
CANONICAL = '8c3c09caa28b8653ba4b1181bc982b8e65e13212eb255ad7dafdebdb856ead94'
PRIOR = 'aa475b6e050701e92151d6ebb1ce2b5a61cda93d96fab91ebec1e1e938af1c10'
KEY = re.compile(r'20261103-[0-9]{2}-(?:0[1-9]|1[0-2])\Z')

def scope(now=None):
    now = now or datetime.now(timezone(timedelta(hours=9)))
    require(os.environ.get('GITHUB_EVENT_NAME') == 'workflow_dispatch'
        and os.environ.get('GITHUB_REF') == 'refs/heads/main'
        and os.environ.get('GITHUB_REPOSITORY') == REPO
        and os.environ.get('RECOVERY_MODE') == MODE and os.environ.get('RECOVERY_DATE') == DAY
        and not os.environ.get('PREDEADLINE_CHECKPOINT_KEY') and not os.environ.get('NIGHT_CHECKPOINT_KEY')
        and now.date().isoformat() in {'2026-11-04', '2026-11-05'}, 'EXACT_REPUBLICATION_SCOPE')

def verifier(source):
    return functions(source, {'require', 'read_record', 'verify_prior'}, {
        'bootstrap': SimpleNamespace(sha256_value=lambda v: digest(canonical(v)+b'\n')),
        'KEY': KEY, 'RESULT_SCHEMA': 'G11_WILD_BET_FUTURE_OOS_SHADOW_RESULT_V1',
        'EXCLUSION_SCHEMA': 'G11_WILD4_FUTURE_OOS_EXCLUSION_V1'})

def get_feed(work, app):
    path = work / ('app-read.json' if app else 'canonical-read.json')
    head = work / 'read.headers'
    query = ('view=v53-final&' if app else '')+'date='+DAY
    done = subprocess.run(['curl','--fail','--silent','--show-error','--max-time','45',
        '-D',str(head),ORIGIN+'/api/g11-feed?'+query,'-o',str(path)],capture_output=True)
    require(done.returncode == 0, 'SITE_GET')
    raw = path.read_bytes()
    headers = dict(line.split(': ',1) for line in head.read_text().lower().splitlines() if ': ' in line)
    require(len(raw) <= 16*1024*1024 and digest(raw) == headers.get('x-g11-feed-sha256'), 'SITE_GET_HASH')
    feed = json.loads(raw)
    require(feed.get('status') == 'PASS' and feed.get('stage') == 'NIGHT'
        and feed.get('operational_date_jst') == DAY, 'PUBLISHED_NIGHT')
    if not app: require(digest(raw) == CANONICAL, 'CANONICAL_UNCHANGED')
    return feed, headers

def projection(feed):
    require(feed.get('goripan',{}).get('status') == 'VERIFIED_CANONICAL_PROJECTION', 'ORIGINAL_PROJECTION')
    obs = feed['goripan']['observation']; active = obs['lifecycle']['active']
    require(obs['model_sha256'] == active['model_sha256'] == MODEL
        and active['identity'] == GENERATION and obs['source_manifest_sha256'] == MANIFEST, 'ORIGINAL_GENERATION')
    rows = sorted((r['wild_pack']['wild4']['goripan'] for r in feed['races']
        if r.get('wild_pack',{}).get('wild4',{}).get('goripan')),key=lambda r:r['race_key'])
    return {'schema':'G11_WILD4_PUBLIC_PROJECTION_V1','machine_identity':'WILD4','display_name':'🦍 ゴリぱん',
        'date':DAY,'formal_from':'2026-10-08','promoted_at':'2026-10-08T00:12:31+09:00',
        'model_sha256':MODEL,'model_binding':active,'source_manifest_sha256':MANIFEST,'observation':obs,
        'races':rows,'purchase_executed':False,'source_locks_mutated':False,'automatic_candidate_promotion':False}

def attach_saved(prior, ledger, check, feed):
    manifest = read(ledger/'batch-manifest.json')
    require(manifest['CONTENT_SHA256'] == MANIFEST == digest(canonical({k:v for k,v in manifest.items() if k!='CONTENT_SHA256'})+b'\n')
        and manifest['DATE'] == DAY and manifest['CANDIDATE_MODEL_SHA256'] == MODEL, 'ORIGINAL_MANIFEST')
    rows = {r['race_key']:r for r in prior['races']}
    keys = sorted(p.stem for p in ledger.glob('*.json') if KEY.fullmatch(p.stem))
    require(len(rows) == len(prior['races']) == 143 and keys == manifest['RACE_KEYS'] == sorted(rows), 'EXACT_143_LOCKS')
    settled = check.verify_prior(ledger)
    require(sorted(settled) == keys, 'ALL_SAVED_SETTLEMENTS_REQUIRED')
    result = deepcopy(prior)
    public = {r['key']:r for r in feed['races']}
    for row in result['races']:
        key = row['race_key']; lock = check.read_record(ledger/(key+'.json'),key); saved=settled[key]
        bindings = {'CANDIDATE_MODEL_SHA256':'model_sha256','CONTENT_SHA256':'lock_sha256',
            'SELECTED_BETS':'bets','BUY_COUNT':'bet_count','LOCKED_AT':'locked_at','RACE_DEADLINE_AT':'deadline_at',
            'INPUT_PACKET_SHA256':'upstream_packet_sha256','ODDS_SNAPSHOT_SHA256':'odds_snapshot_sha256',
            'EFFECTIVE_MODEL_IDENTITY':'effective_model_identity','CALIBRATION_SHA256':'calibration_sha256'}
        require(all(lock.get(a)==row.get(b) for a,b in bindings.items())
            and row['effective_model_identity']==GENERATION and row['model_sha256']==MODEL
            and row['bet_count']==len(row['bets']) and row['abstain']==(row['bet_count']==0)
            and datetime.fromisoformat(row['locked_at'])<datetime.fromisoformat(row['deadline_at']), 'LOCK_IDENTITY:'+key)
        value={k:saved.get(k) for k in SETTLEMENT_FIELDS}
        require(row['settlement'] in (None,value), 'SETTLEMENT_CONFLICT')
        require(datetime.fromisoformat(value['OFFICIAL_CAPTURED_AT'])>datetime.fromisoformat(row['deadline_at']), 'RESULT_AFTER_DEADLINE')
        race=public[key]
        if value['EVALUATION_STATUS']=='FIRST_CURRENT_VALID':
            hit=value['OFFICIAL_RESULT'] in row['bets']
            require(not value['REFUND_OCCURRED'] and race['result_meta']['result_status']=='ESTABLISHED'
                and value['SHADOW_HIT']==hit and value['SHADOW_BUY_COUNT']==row['bet_count']
                and value['SHADOW_UNIT_100YEN_INVESTMENT']==row['bet_count']*100
                and any(x['trifecta']==value['OFFICIAL_RESULT'] and (not hit or x['payout']==value['SHADOW_UNIT_100YEN_PAYOUT'])
                    for x in race['result_meta']['trifecta_settlements'])
                and (hit or value['SHADOW_UNIT_100YEN_PAYOUT']==0), 'SAVED_OFFICIAL_JOIN:'+key)
        else:
            require(value['SHADOW_UNIT_100YEN_INVESTMENT'] is None and value['SHADOW_UNIT_100YEN_PAYOUT'] is None
                and (value['EVALUATION_STATUS']=='VOID_BY_PREDECLARED_CONTRACT' or value['EXCLUSION_REASON']), 'SAVED_EXCLUSION')
        row['settlement']=value
    return result

def prepare(args):
    work=args.work; work.mkdir(parents=True,exist_ok=True)
    meta=api('/actions/artifacts/'+str(ARTIFACT)); run=api('/actions/runs/'+str(RUN))
    require(meta['id']==ARTIFACT and meta['name']==NAME and not meta['expired']
        and meta['digest']=='sha256:'+ARTIFACT_SHA and meta['workflow_run']['id']==RUN
        and run['head_sha']==RUN_SHA and run['head_branch']=='main' and run['conclusion']=='failure'
        and run['path']=='.github/workflows/g11-morning-odds-footprint-4bet-shadow-v1.yml', 'EXACT_SETTLED_ARTIFACT')
    archive=work/'original.zip'
    headers='Authorization: Bearer '+os.environ['GH_TOKEN']+'\nAccept: application/vnd.github+json\n'
    download=subprocess.run(['curl','--fail','--location','--silent','--show-error','--max-time','60',
        '--header','@-','https://api.github.com/repos/'+REPO+'/actions/artifacts/'+str(ARTIFACT)+'/zip',
        '-o',str(archive)],input=headers,text=True,capture_output=True)
    require(download.returncode==0 and digest(archive.read_bytes())==ARTIFACT_SHA, 'ARTIFACT_ZIP_HASH')
    with zipfile.ZipFile(archive) as z:
        require(z.namelist()==['future-locks.tgz.gpg'], 'ARTIFACT_MEMBERS')
        (work/'future-locks.tgz.gpg').write_bytes(z.read('future-locks.tgz.gpg'))
    extract_encrypted(work/'future-locks.tgz.gpg',work/'restored',work)
    ledger=work/'restored/g11-wild4-future-locks'; before=inventory(ledger)
    get_feed(work,False); feed,h=get_feed(work,True); prior=projection(feed)
    require(digest(canonical(prior))==h.get('x-g11-wild4-projection-sha256')==PRIOR, 'EXACT_ORIGINAL_PROJECTION')
    result=attach_saved(prior,ledger,verifier(args.settlement),feed)
    require(inventory(ledger)==before, 'SOURCE_LEDGER_UNCHANGED')
    (work/'projection.json').write_bytes(canonical(result))
    (work/'ledger-inventory.json').write_bytes(canonical(before))
    print('WILD4_SAVED_143_SETTLEMENTS_VERIFIED; SOURCE_WRITES=0; NEW_PREDICTIONS=0',flush=True)

def publish(args):
    work=args.work; data=(work/'projection.json').read_bytes(); result=json.loads(data)
    require((work/'future-locks.tgz.gpg').read_bytes()==args.saved_archive.read_bytes(), 'PERSISTED_CIPHERTEXT_READBACK')
    require(inventory(work/'restored/g11-wild4-future-locks')==read(work/'ledger-inventory.json'), 'SOURCE_LEDGER_UNCHANGED')
    get_feed(work,False); feed,h=get_feed(work,True)
    require(digest(canonical(projection(feed)))==h.get('x-g11-wild4-projection-sha256')==PRIOR, 'CONCURRENT_PROJECTION_CHANGE')
    url=os.environ['ACTIONS_ID_TOKEN_REQUEST_URL']
    req=urllib.request.Request(url+('&' if '?' in url else '?')+'audience=g11-race-lab',
        headers={'Authorization':'Bearer '+os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']})
    with urllib.request.urlopen(req,timeout=30) as response: token=json.load(response)['value']
    headers='Authorization: Bearer '+token+'\nContent-Type: application/json\nX-G11-Feed-Sha256: '+digest(data)+'\n'
    sent=subprocess.run(['curl','--silent','--show-error','--max-time','45','-X','POST',ORIGIN+'/api/g11-wild4-projection',
        '--header','@-','--data-binary','@'+str(work/'projection.json'),'-o',str(work/'reply.json'),'--write-out','%{http_code}'],
        input=headers,text=True,capture_output=True)
    reply=read(work/'reply.json')
    require(sent.returncode==0 and sent.stdout=='200' and reply.get('status')=='PASS'
        and reply.get('payload_sha256')==digest(data), 'PUBLICATION_FAILED_NO_RETRY')
    after,headers=get_feed(work,True)
    require(projection(after)==result and headers.get('x-g11-wild4-projection-sha256')==digest(data), 'ALL_143_PUBLIC_READBACK')
    get_feed(work,False)
    require(inventory(work/'restored/g11-wild4-future-locks')==read(work/'ledger-inventory.json'), 'SOURCE_LEDGER_UNCHANGED')
    print('WILD4_143_SETTLEMENT_PUBLIC_GET=PASS; CANONICAL_UNCHANGED; SOURCE_WRITES=0',flush=True)
    with open(os.environ['GITHUB_STEP_SUMMARY'],'a') as out: out.write('Nov 3: 143 original saved settlements published; all original LOCKs and canonical NIGHT unchanged.\n')

def main():
    p=argparse.ArgumentParser(); p.add_argument('action',choices=['prepare','publish']); p.add_argument('--work',type=Path,required=True)
    p.add_argument('--settlement',type=Path); p.add_argument('--saved-archive',type=Path); args=p.parse_args(); scope()
    {'prepare':prepare,'publish':publish}[args.action](args)

if __name__=='__main__': main()
