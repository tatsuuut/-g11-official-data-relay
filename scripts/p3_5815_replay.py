"""Result-blind partial-identification replay of the current 5/8/15 selector.

Missing P3 ranks are enumerated, never imputed. Every compatible rank is passed
through the pinned production selector. Outcomes are joined only afterwards.
No production LOCK, calibration, prediction or accounting is written.
"""
import ast
from collections import Counter
from copy import deepcopy
from datetime import date
from hashlib import sha256
import inspect
from itertools import permutations
import json
import os
from pathlib import Path, PurePosixPath
import tarfile

from g11.growth_p3.v1_1 import canonical_v1_1 as C
from g11.growth_p3.v1_1 import confidence_context_v2 as CONTEXT
from g11.growth_p3.v1_1 import current_runtime_v1 as CURRENT
from g11.growth_p3.v1_1 import high_payout_wave_v2 as WAVE
from g11.growth_p3.v1_1 import morning_materials_v1 as MATERIALS
from g11.growth_p3.v1_1 import true_ai_head_adoption_v1 as TRUE
from g11.growth_p3.v1_1 import true_ai_ticket_expansion_v1 as EXPAND
from g11.growth_p3.v1_1.confidence_historical_backtest_v1 import manifest_archive, ledger_map, verified_outcome, require, stamp

DAYS = ['2026-10-22','2026-10-23','2026-10-24','2026-10-25','2026-10-26']
root = Path(os.environ['RUNNER_TEMP']) / 'p3-replay-inputs'
source_root = Path(inspect.getfile(C)).resolve().parents[3]
archive = Path(os.environ['RUNNER_TEMP']) / 'p3-history-source.tgz'

def safe_name(member):
    p = PurePosixPath(member.name)
    require(not p.is_absolute() and '..' not in p.parts and not member.issym() and not member.islnk(), 'UNSAFE_ARCHIVE')
    return '/'.join(p.parts)

def extract():
    with tarfile.open(archive, 'r|gz') as t:
        for m in t:
            name = safe_name(m)
            selected = name.startswith(('store/archives/','store/g11/true-ai-history/','store/g11/p3-true-ai-review/'))
            if name.startswith('daily-runtime/') and PurePosixPath(name).parts[1] in DAYS:
                selected = True
            if not m.isfile() or not selected: continue
            require(m.size <= 200_000_000, 'MEMBER_SIZE')
            p=root/name; p.parent.mkdir(parents=True,exist_ok=True)
            p.write_bytes(t.extractfile(m).read())

def seal(value, field='CONTENT_SHA256'):
    v=dict(value); digest=v.pop(field,None)
    require(C.canonical_sha(v)==digest, 'SEALED_'+field)
    return digest

def normalized_functions(text):
    out={}
    for n in ast.parse(text).body:
        if not isinstance(n, ast.FunctionDef): continue
        n=deepcopy(n)
        # The only allowed runtime difference is a read-only confidence context
        # passed AFTER production bets/scores have already been calculated.
        for node in ast.walk(n):
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='_selection_confidence':
                node.keywords=[k for k in node.keywords if k.arg!='context_inputs']
        out[n.name]=ast.dump(n,include_attributes=False)
    return out

def source_equivalence(day, authority):
    p=root/'daily-runtime'/day/authority['source_archive']
    require(sha256(p.read_bytes()).hexdigest()==authority['source_archive_sha256'], 'SOURCE_ARCHIVE_HASH')
    paths=['g11/growth_p3/v1_1/'+n for n in ('canonical_v1_1.py','high_payout_wave_v2.py','morning_materials_v1.py','current_runtime_v1.py','true_ai_head_adoption_v1.py')]
    paths+=['p3/formal_prediction_engine/v1/src/formal_prediction_engine_v1.py']
    old={}
    with tarfile.open(p,'r|gz') as t:
        for m in t:
            name=safe_name(m)
            if m.isfile() and name in paths:
                raw=t.extractfile(m).read()
                require(sha256(raw).hexdigest()==authority['source_files'][name], 'HISTORICAL_SOURCE_HASH')
                old[name]=raw.decode()
    require(set(old)==set(paths),'SOURCE_FILES_MISSING')
    current={name:(source_root/name).read_text() for name in paths}
    for name in paths:
        if name.endswith(('current_runtime_v1.py','true_ai_head_adoption_v1.py')): continue
        require(old[name]==current[name], 'SCORING_COMPONENT_CHANGED:'+name)
    runtime=paths[3]; a,b=map(normalized_functions,(old[runtime],current[runtime]))
    core=['_source_packet','_validated_course_finish_scores','_bound_course_contextual_finish_scores','_materials_apply','build_current_morning_output','build_current_live_output']
    for name in core: require(a[name]==b[name], 'SCORING_FUNCTION_CHANGED:'+name)
    head=paths[4]; a,b=map(normalized_functions,(old[head],current[head]))
    for name in ('_template_bets','_pool_arrangement'):
        require(a[name]==b[name], 'CORE_SELECTION_CHANGED:'+name)
    return {'scoring_components_identical':True,'scoring_functions_identical_except_read_only_confidence_context':True,
            'core_selection_identical':True,'source_archive_sha256':authority['source_archive_sha256']}

def possibilities(review, prediction, wave):
    n=review['POINTS']; original=review['BASELINE_PRODUCTION_BETS']; saved=review['SELECTED_PRODUCTION_BETS']
    prefix=list(map(int,original[0].split('-')))
    if n>3: prefix.append(int(original[3].split('-')[-1]))
    require(len(set(prefix))==len(prefix),'BASELINE_PREFIX')
    sets={}; matches=0
    for tail in permutations(b for b in range(1,7) if b not in prefix):
        rank=prefix+list(tail)
        if TRUE._template_bets(rank,n)!=original: continue
        stage=[prediction['AI_HEAD']]+[b for b in rank if b!=prediction['AI_HEAD']]
        if TRUE._template_bets(stage,n)!=review['HEAD_ONLY_PRODUCTION_BETS']: continue
        arranged,_=TRUE._pool_arrangement(stage,n,{'MORNING_TRUE_PREDICTION':prediction})
        if TRUE._template_bets(arranged,n)!=saved: continue
        bets,_=EXPAND.expand(wave=wave,stage_ranking=stage,core_bets=saved,boat_probabilities=prediction['BOATS'])
        matches+=1; sets[tuple(bets)]=True
    require(matches>0,'NO_COMPATIBLE_RANKING')
    return sorted(sets),matches

def scope_identity(materials=True):
    core=(CURRENT._source_packet,CURRENT._validated_course_finish_scores,CURRENT._bound_course_contextual_finish_scores,
          CURRENT._materials_apply,CURRENT.build_current_morning_output,CURRENT.build_current_live_output)
    identity={'SCHEMA':CONTEXT.CONTEXT_SCHEMA,'P3_VERSION':CONTEXT.P3_VERSION,
       'PL120_VERSION':f'{C.LOGIC_ID}:{C.VERSION}:PL120','CONFIDENCE_RULE_VERSION':CONTEXT.RULE_VERSION,
       'COMPONENT_SOURCE_SHA256':CONTEXT.component_hashes(),'RUNTIME_SCORING_SHA256':C.canonical_sha([inspect.getsource(f) for f in core]),
       'SELECTION_POLICY':EXPAND.POLICY,'SELECTION_POLICY_VERSION':EXPAND.VERSION,
       'TRUE_AI_MODEL_VERSION':CURRENT.TRUE_AI_MODEL_VERSION,'TRUE_AI_MODEL_SHA256':CURRENT.TRUE_AI_MODEL_SHA256,
       'MATERIALS_RULE':MATERIALS.RULE_VERSION if materials else None,'PRE_RACE_IMPACT_RULE':None}
    return {'identity':identity,'fingerprint':C.canonical_sha(identity)}

def holdout(day): return (date.fromisoformat(day)-date(2026,9,29)).days%5==4
def stats(rows):
    n=len(rows); low=sum(r['hit_min'] for r in rows); high=sum(r['hit_max'] for r in rows)
    return {'n':n,'hits_min':low,'hits_max':high,'hit_rate_min':low/n if n else None,'hit_rate_max':high/n if n else None,
            'exact_bet_sets':sum(r['variants']==1 for r in rows),'identified_hit_results':sum(r['hit_min']==r['hit_max'] for r in rows),
            'days':sorted({r['day'] for r in rows})}

def run():
    extract(); all_rows=[]; sources=[]; exclusions=Counter(); audits=[]
    for day in DAYS:
        daily=root/'daily-runtime'/day
        feed_raw=(daily/'night-feed.json').read_bytes(); feed=json.loads(feed_raw)
        check=json.loads((daily/'night-checkpoint.json').read_bytes())
        authority=json.loads((daily/'authority.json').read_bytes())
        require(check['feed_sha256']==sha256(feed_raw).hexdigest(),'NIGHT_CHECKPOINT_HASH')
        require(feed['status']=='PASS' and feed['stage']=='NIGHT','NIGHT_STATUS')
        audit=source_equivalence(day,authority); audits.append(dict(day=day,**audit))
        archives=list((root/'store/archives').rglob('daily-research-'+day+'*.json.gz'))
        if not archives: archives=[p for p in (root/'store/archives').rglob('*.json.gz') if day in str(p) or day.replace('-','') in str(p)]
        require(len(archives)==1,'ARCHIVE_DAY_COUNT:'+day)
        history,digest=manifest_archive(archives[0]); ledger=ledger_map(history)
        require(history['OPERATIONAL_DATE_JST']==day,'ARCHIVE_DAY_BINDING')
        records={r['RACE_ID']:r for r in history.get('P3_CONFIDENCE_CALIBRATION_DEV',{}).get('RECORDS',[]) if r.get('MORNING_OR_LIVE')=='LIVE'}
        reviews=list((root/'store/g11/p3-true-ai-review'/day).glob('review-*.json')); require(len(reviews)==1,'REVIEW_COUNT')
        review=json.loads(reviews[0].read_bytes()); review_sha=seal(review)
        require(review['OFFICIAL_FINAL_ACCOUNTING_SHA256']==C.canonical_sha(feed['final_accounting']),'FINAL_ACCOUNTING_BINDING')
        review_map={r['RACE_ID']:r for r in review['RECORDS']}
        bundle=TRUE.read_morning_source(root/'store/g11/true-ai-history',day)
        pred_meta=json.loads(bundle['PREDICTION_MANIFEST_JSON']); feat_meta=json.loads(bundle['FEATURE_MANIFEST_JSON'])
        races={r['key']:r for r in feed['races']}
        sources.append({'day':day,'night_feed_sha256':check['feed_sha256'],'archive_sha256':digest,
                        'review_sha256':review_sha,'true_sources':dict(bundle['SOURCE_HASHES'])})
        predicted=[]
        for key,rec in sorted(records.items()):
            try:
                rr=review_map[key]; race=races[key]; live=race['predeadline']; n=rec['POINTS']; wave=live['turbulence']
                require(rr['STATUS']=='PAIRED' and rr['SNAPSHOT']=='LIVE' and rr['PRIMARY_COMPARISON_ELIGIBLE'],'PAIRED_LIVE')
                require(rr['BET_SELECTION_MODE']==TRUE.POOL_SELECTION_MODE,'OLD_POOL_POLICY')
                require(n==(3 if wave<=2 else 5 if wave<=5 else 7) and type(wave) is int and 1<=wave<=8,'WAVE_CORE_POINTS')
                require(rr['SELECTED_PRODUCTION_BETS']==rec['PRODUCTION_BET_ORDER']==live['practical_bets'],'CORE_BET_BINDING')
                require(rr['FINAL_P3_SNAPSHOT_SHA256']==rec['SOURCE_SNAPSHOT_SHA256']==live['snapshot_sha256'],'SOURCE_SNAPSHOT_BINDING')
                require(rr['PREDICTION_LOCKED_AT']==rec['PREDICTION_LOCKED_AT'],'PREDICTION_TIME_BINDING')
                require(live.get('scoring_policy')=='OFFICIAL_ACTUAL_COURSE_ONLY' and not race.get('special_case_id'),'ORDINARY_SCORING_ONLY')
                pred=json.loads(bundle['PREDICTION_RECORDS'][key]); feat=json.loads(bundle['FEATURE_RECORDS'][key]); TRUE._validate_prediction(pred)
                require(rr['MODEL_SHA256']==pred['MODEL_SHA256']==CURRENT.TRUE_AI_MODEL_SHA256 and pred['MODEL_VERSION']==CURRENT.TRUE_AI_MODEL_VERSION,'TRUE_MODEL_PIN')
                require(pred['FEATURE_SNAPSHOT_SHA256']==feat['MORNING_FEATURE_SNAPSHOT_SHA256'] and pred['FEATURE_ASOF_TIME']==feat['FEATURE_ASOF_TIME'],'TRUE_FEATURE_BINDING')
                require(pred['AI_HEAD']==rr['SELECTED_HEAD'],'TRUE_HEAD_BINDING')
                cutoff=stamp(rec['RACE_DEADLINE_AT'])
                require(all(stamp(v)<cutoff for v in (feat_meta['CREATED_AT_UTC'],pred_meta['CREATED_AT_UTC'],pred['FEATURE_ASOF_TIME'],rec['PREDICTION_LOCKED_AT'])),'PRELOCK_ONLY')
                variants,matches=possibilities(rr,pred,wave)
                predicted.append({'race':key,'day':day,'wave':wave,'points':EXPAND.budget(wave),
                    'variants':len(variants),'compatible_rankings':matches,'candidate_bets':variants,
                    'candidates_sha256':C.canonical_sha(variants),'core_sha256':rec['BET_ARTIFACT_SHA']})
            except (ValueError,KeyError,TypeError) as e: exclusions[str(e)]+=1
        # Prediction possibilities are sealed before the official-result join.
        prediction_sha=C.canonical_sha(predicted)
        for p in predicted:
            key=p['race']; rec=records[key]
            try:
                verified_outcome(rec,ledger[key],history)
                require(rec['OFFICIAL_RESULT_STATUS']=='ESTABLISHED' and not rec['REFUND_BOATS'],'REFUND_OR_VOID')
                winners=set(rec['OFFICIAL_WINNING_TRIFECTA_SET'])
                flags=[int(bool(winners.intersection(bets))) for bets in p['candidate_bets']]
                p.update(hit_min=min(flags),hit_max=max(flags),holdout=holdout(day),prediction_batch_sha256=prediction_sha)
                all_rows.append(p)
            except (ValueError,KeyError,TypeError) as e: exclusions['OUTCOME:'+str(e)]+=1
    summary={'schema':'G11_P3_5815_PARTIALLY_IDENTIFIED_BACKTEST_V1','status':'COMPLETE',
        'method':'ALL_COMPATIBLE_SAVED_P3_RANKINGS_CURRENT_SELECTOR_NO_OUTCOME_SELECTION','cutoff_exclusive':'2026-10-27',
        'snapshot_reconstruction':False,'production_mutated':False,'formal_certification':False,'model_training':False,
        'source_code_ref':os.environ.get('SOURCE_REF'),'source_audits':audits,'sources':sources,'exclusions':dict(exclusions),
        'target_scope':scope_identity(),'rows_sha256':C.canonical_sha(all_rows),
        'by_points':{str(n):{'all':stats([r for r in all_rows if r['points']==n]),
            'development':stats([r for r in all_rows if r['points']==n and not r['holdout']]),
            'holdout':stats([r for r in all_rows if r['points']==n and r['holdout']])} for n in (5,8,15)}}
    print('P3_REPLAY_REPORT_JSON='+json.dumps(summary,ensure_ascii=False,separators=(',',':')))
    for day in DAYS:
        print('P3_REPLAY_ROWS_JSON='+json.dumps({'day':day,'rows':[r for r in all_rows if r['day']==day]},ensure_ascii=False,separators=(',',':')))

if __name__=='__main__': run()
