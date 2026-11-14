"""Independent saved-input research worker and verified NIGHT handover.

This workflow has its own concurrency group and cache namespace. It never
collects official inputs, publishes canonical forecasts or saves canonical
state. Only a closed, allowlisted bundle can enter the NIGHT settlement copy.
"""
import argparse
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import research_state_isolation_v1 as isolation

REPOSITORY = 'tatsuuut/-g11-official-data-relay'
WORKFLOW = 'g11-isolated-live-research.yml'
CACHE_PREFIX = 'g11-research-v1-'


def outputs(**values):
    with open(os.environ['GITHUB_OUTPUT'], 'a') as target:
        for key, value in values.items():
            text = str(value).lower() if isinstance(value, bool) else str(value or '')
            isolation.require('\n' not in text and '\r' not in text, 'RESEARCH_OUTPUT_VALUE')
            target.write(key+'='+text+'\n')


def gh_json(endpoint):
    result = subprocess.run(['gh', 'api', endpoint], capture_output=True,
        text=True, timeout=25, check=True)
    return json.loads(result.stdout)


def caches(prefix):
    value = gh_json('repos/'+REPOSITORY+'/actions/caches?key='+prefix+
        '&ref=refs%2Fheads%2Fmain&per_page=100&sort=created_at&direction=desc')
    return [row for row in value['actions_caches'] if row.get('ref') == 'refs/heads/main']


def latest_research(day):
    pattern = re.compile(re.escape(CACHE_PREFIX+day)+r'-\d+-\d+$')
    rows = [r for r in caches(CACHE_PREFIX+day+'-') if pattern.fullmatch(r.get('key',''))]
    return max(rows, key=lambda r:(r['created_at'],r['id']))['key'] if rows else ''


def latest_canonical(day):
    from critical_saved_publication_v1 import newest_checkpoint_key
    rows = []
    for lane in ('critical', 'published'):
        rows.extend(caches('g11-state-v4-'+lane+'-'+day+'-'))
    return newest_checkpoint_key(day, rows)


def operational_day(day, now):
    target = date.fromisoformat(day)
    return day >= isolation.EFFECTIVE_FROM and (target == now.date() or
        (target+timedelta(days=1) == now.date() and now.hour < 2))


def event_selection(event, now):
    parent = event.get('workflow_run') or {}
    if (parent.get('head_branch') != 'main'
            or (parent.get('head_repository') or {}).get('full_name') != REPOSITORY):
        return None
    title = parent.get('display_title','')
    match = re.fullmatch(r'LIVE-(predeadline|night|auto)-(\d{4}-\d{2}-\d{2})', title)
    if match:
        phase, day = match.groups()
    elif title == 'runtime sync' and 8 <= now.hour < 22:
        message = (parent.get('head_commit') or {}).get('message','')
        if any(tag in message for tag in ('[morning-', '[daily-runtime-freeze]',
                '[historical-', '[rescue-', '[delayed-recovery-')):
            return None
        phase = 'night' if '[night-' in message else 'predeadline'
        day = now.date().isoformat()
    else:
        return None
    valid_day = operational_day(day, now) or (phase == 'night'
        and isolation.EFFECTIVE_FROM <= day <= now.date().isoformat())
    if not valid_day or not isinstance(parent.get('id'), int):
        return None
    return {'day':day, 'phase':phase, 'parent':parent['id']}


def select_event():
    isolation.require(os.environ.get('GITHUB_REPOSITORY') == REPOSITORY, 'RESEARCH_REPOSITORY')
    now = datetime.now(isolation.JST)
    selected = event_selection(json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_bytes()), now)
    if not selected:
        outputs(active=False); return
    day, parent = selected['day'], selected['parent']
    source_key = ''
    if selected['phase'] == 'night':
        source_key = latest_canonical(day)
    else:
        exact = f'g11-state-v4-critical-{day}-{parent}'
        for attempt in range(76):
            if any(row.get('key') == exact for row in caches(exact)):
                # Prefer the latest ready superset when an older workflow_run
                # event arrives late. Never roll research back to an old input.
                source_key = latest_canonical(day) or exact
                break
            status = gh_json(f'repos/{REPOSITORY}/actions/runs/{parent}')
            if status.get('status') == 'completed':
                break
            time.sleep(8)
    outputs(active=bool(source_key), day=day, source_key=source_key,
            research_key=latest_research(day) if source_key else '')
    print('G11_RESEARCH_INPUT_SELECTION='+json.dumps({'day':day,'parent':parent,
        'ready':bool(source_key)},sort_keys=True),flush=True)


def prepare_input(state, day):
    marker = state/isolation.MARKER.format(day=day)
    if not marker.is_file():
        outputs(active=False); print('G11_RESEARCH_INPUT=LEGACY_NOT_ACTIVATED'); return
    value = json.loads(marker.read_bytes())
    metadata = json.loads((state/'relay-state.json').read_bytes())
    critical = metadata.get('CRITICAL',{})
    now = datetime.now(isolation.JST)
    closing = now >= max(isolation.plan_deadlines(state,day).values())
    isolation.require((operational_day(day,now) or closing) and value.get('schema') == isolation.SCHEMA
        and value.get('day') == day and metadata.get('OPERATIONAL_DATE_JST') == day
        and metadata.get('MORNING_COMPLETE') is True and critical.get('OPERATIONAL_DATE') == day
        and critical.get('CRITICAL_CHECKPOINT_STATUS') == 'PASS', 'RESEARCH_INPUT_BINDING')
    outputs(active=True, closing=closing)


def decode(encrypted, destination):
    from extract_runtime_state_v1 import extract_complete_state
    archive = encrypted.with_name(encrypted.name+'.authenticated.tgz')
    try:
        subprocess.run(['gpg','--batch','--yes','--pinentry-mode','loopback',
            '--passphrase-fd','3','--decrypt','--output',str(archive),str(encrypted)],
            pass_fds=(3,), stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
        extract_complete_state(archive,destination)
    finally:
        archive.unlink(missing_ok=True)


def authority_binding(state, day, source):
    from daily_runtime_bootstrap_v1 import retained_module
    authority = retained_module(state,day)
    binding = authority.verify_runtime(state,day,source)
    for phase in ('MORNING','PREDEADLINE'):
        authority.verify_locks(state,day,phase,exact=True)
    return authority, {'authority_sha256':binding['authority_sha256'],
                       'runtime_source_sha':binding['runtime_source_sha']}


def capture_only_research(state, source, day):
    sys.path.insert(0,str(source))
    from daily_predeadline_authority_recovery_v1 import guard_optional_mashiro_shadow
    from predeadline_race_isolation_v1 import isolate_predeadline_inputs
    from rescue_night_result_parser_v1 import official_input_compatibility
    from g11.wild_pack.v1 import runtime_orchestrator_v1 as wild
    from g11.growth_p3.v1_1 import relay_publish_v1 as publisher
    from g11.growth_p3.v1_1 import current_runtime_v1 as current
    from g11.growth_p3.v1_1 import mashiro_v4_runtime_adapter_v1 as mashiro
    from g11.canonical.official_pipeline import OfficialArtifactStore
    from g11.relay import v1 as relay
    guard_optional_mashiro_shadow()
    result = {}; failures = []
    with official_input_compatibility(), isolate_predeadline_inputs():
        try:
            result['wild_changed'] = wild.run_predeadline(state/'store',date.fromisoformat(day))
        except Exception as exc:
            failures.append('WILD:'+type(exc).__name__)
        artifact = OfficialArtifactStore(state/'store')
        morning = relay._morning_projection(state/'store')
        future = publisher._future_race_keys(state/'store',morning,datetime.now(isolation.JST))
        if future:
            try:
                result['confidence'] = current.publish_prospective_confidence_evidence(
                    artifact,future,captured_at_jst=datetime.now(isolation.JST).isoformat())['STATUS']
            except Exception as exc:
                failures.append('CONFIDENCE:'+type(exc).__name__)
        try:
            result['mashiro'] = mashiro.collect_predeadline(artifact,date.fromisoformat(day))['STATUS']
        except Exception as exc:
            failures.append('MASHIRO:'+type(exc).__name__)
    # These are the research functions only. No publish_morning,
    # publish_ready_live, new official acquisition or canonical feed publisher.
    return {'results':result,'failures':failures,'core_inference_calls':0,'official_acquisition':0}


def run_research(state, source, deme_source, previous, destination, day, source_key, feed):
    authority,binding = authority_binding(state,day,source)
    sources = isolation.core_sources(state,day)
    seeds = {name:isolation.digest(path) for name,path in isolation.research_files(state,day).items()}
    before = isolation.protected_metadata(state,day)
    if (previous/'manifest.json').is_file():
        prior = isolation.read_bundle(previous,day); prior['_root'] = previous
        isolation.validate_source(prior,state,day,binding)
        if prior['closed']:
            outputs(no_change=True,closing=True)
            print('G11_ISOLATED_RESEARCH=ALREADY_CLOSED'); return
        isolation.apply_files(prior,state,day)
    closing = datetime.now(isolation.JST) >= max(isolation.plan_deadlines(state,day).values())
    result = {'mode':'CLOSE_ONLY'} if closing else capture_only_research(state,source,day)
    if not closing:
        try:
            from saved_deme_recovery_v1 import recover
            result['deme'] = recover(state,deme_source,day,feed)['readback']
        except Exception as exc:
            result['deme'] = 'UNAVAILABLE:'+type(exc).__name__
    isolation.require(isolation.protected_metadata(state,day) == before
        and isolation.core_sources(state,day) == sources, 'RESEARCH_PROTECTED_STATE_CHANGED')
    authority.verify_runtime(state,day,source)
    value = isolation.export_bundle(state,destination,day,binding,source_key,seeds,sources,closed=closing)
    isolation.read_bundle(destination,day)
    outputs(no_change=False,closing=closing,bundle_sha=value['bundle_sha256'])
    print('G11_ISOLATED_RESEARCH='+json.dumps({'day':day,'closed':closing,
        'members':len(value['members']),'held_late_count':value['held_late_count'],
        'canonical_state_written':False,'result':result},sort_keys=True),flush=True)


def select_night(state, day):
    marker = state/isolation.MARKER.format(day=day)
    if not marker.is_file():
        outputs(required=False); return
    value = json.loads(marker.read_bytes())
    isolation.require(value.get('schema') == isolation.SCHEMA and value.get('day') == day,
                      'RESEARCH_NIGHT_MARKER')
    if value.get('night_imported_payload_sha256') or (state/'daily-runtime'/day/'night-checkpoint.json').is_file():
        outputs(required=False); return
    isolation.require(datetime.now(isolation.JST) >= max(isolation.plan_deadlines(state,day).values()),
                      'RESEARCH_NIGHT_WINDOW_OPEN')
    outputs(required=True,research_key=latest_research(day))


def import_night(state, source, bundle_root, day, *, authority=None, now=None):
    now = now or datetime.now(isolation.JST)
    isolation.require(now >= max(isolation.plan_deadlines(state,day).values()),
                      'RESEARCH_IMPORT_BEFORE_LAST_DEADLINE')
    if not (bundle_root/'manifest.json').is_file():
        outputs(waiting=True); print('G11_RESEARCH_NIGHT=WAITING_FOR_CLOSED_BUNDLE'); return
    if authority is None:
        authority,binding = authority_binding(state,day,source)
    else:
        b=authority.verify_runtime(state,day,source)
        binding={k:b[k] for k in ('authority_sha256','runtime_source_sha')}
    value = isolation.read_bundle(bundle_root,day); value['_root'] = bundle_root
    isolation.validate_source(value,state,day,binding)
    if not value['closed']:
        outputs(waiting=True); print('G11_RESEARCH_NIGHT=WAITING_FOR_CLOSE'); return
    marker = state/isolation.MARKER.format(day=day)
    original_marker = marker.read_bytes()
    before_sources = isolation.core_sources(state,day)
    binding_root = state/'daily-runtime'/day/'bindings/PREDEADLINE'
    prior_bindings = set(binding_root.glob('*.json'))
    def finalize():
        try:
            isolation.require(isolation.core_sources(state,day) == before_sources,
                              'RESEARCH_NIGHT_CORE_CHANGED')
            authority.verify_locks(state,day,'MORNING',exact=True)
            authority.verify_locks(state,day,'PREDEADLINE',exact=True,allow_append=True)
            authority.seal(state,day,'PREDEADLINE')
            authority.verify_locks(state,day,'PREDEADLINE',exact=True)
            marked=json.loads(original_marker)
            marked['night_imported_bundle_sha256']=value['bundle_sha256']
            marked['night_imported_payload_sha256']=isolation.bundle_payload_sha(value,day)
            isolation.atomic(marker,isolation.canonical(marked)+b'\n')
        except Exception:
            for path in set(binding_root.glob('*.json'))-prior_bindings:
                path.unlink()
            isolation.atomic(marker,original_marker)
            raise
    count=isolation.apply_files(value,state,day,night=True,after_copy=finalize)
    outputs(waiting=False)
    print('G11_RESEARCH_NIGHT='+json.dumps({'status':'IMPORTED_CLOSED_BUNDLE',
        'members':count,'core_predictions_changed':False},sort_keys=True),flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode',choices=['select-event','activate','prepare-input','decode','run',
                                  'select-night','import-night','retry-night','publish'])
    p.add_argument('--state-root',type=Path,default=Path('.relay-state'))
    p.add_argument('--source-root',type=Path,default=Path('.g11-private'))
    p.add_argument('--deme-source',type=Path,default=Path('.g11-deme-research'))
    p.add_argument('--previous',type=Path,default=Path('.research-previous'))
    p.add_argument('--destination',type=Path,default=Path('.research-export'))
    p.add_argument('--encrypted',type=Path,default=Path('.relay-cache/state.tgz.gpg'))
    p.add_argument('--feed',type=Path,default=Path('.research-output/feed.json'))
    p.add_argument('--day',default='')
    p.add_argument('--source-key',default='')
    a=p.parse_args();state=a.state_root.resolve();source=a.source_root.resolve()
    if a.mode=='select-event': return select_event()
    if a.mode=='decode': return decode(a.encrypted.resolve(),a.destination.resolve())
    if a.mode=='activate':
        isolation.activate(state,a.day);outputs(active=True);return
    if a.mode=='prepare-input': return prepare_input(state,a.day)
    if a.mode=='select-night': return select_night(state,a.day)
    if a.mode=='import-night': return import_night(state,source,a.previous.resolve(),a.day)
    if a.mode=='publish':
        if datetime.now(isolation.JST) >= max(isolation.plan_deadlines(state,a.day).values()):
            print('G11_ISOLATED_RESEARCH_PUBLICATION=CLOSED_WINDOW'); return
        from deme_research_delivery_v1 import publish_persisted_live
        for attempt in range(3):
            try:
                result=publish_persisted_live(state,a.feed.resolve())
                isolation.require(result.get('status') == 'PASS', 'RESEARCH_PUBLICATION_NOT_VERIFIED')
                break
            except ValueError as exc:
                if attempt == 2 or str(exc) not in (
                        'RESEARCH_HTTP_409_WAITING_MATCHING_FEED', 'RESEARCH_HTTP_409_DEME_CONCURRENT_PUBLICATION'):
                    raise
                time.sleep(2)
        print('G11_ISOLATED_RESEARCH_PUBLICATION=VERIFIED_RESEARCH_ONLY')
        return
    if a.mode=='retry-night':
        time.sleep(30)
        subprocess.run(['gh','workflow','run','g11-free-runner.yml','--ref','main',
            '-f','phase=night','-f','operational_date='+a.day],timeout=25,check=True)
        print('G11_RESEARCH_NIGHT=CONTINUATION_DISPATCHED');return
    sys.path.insert(0,str(source))
    run_research(state,source,a.deme_source.resolve(),a.previous.resolve(),a.destination.resolve(),
                 a.day,a.source_key,a.feed.resolve())


if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        code=str(exc)
        print('G11_ISOLATED_RESEARCH=HELD TYPE='+type(exc).__name__+
              (' CODE='+code if re.fullmatch('[A-Z0-9_]{1,100}',code) else ''),flush=True)
        raise SystemExit(1) from None
