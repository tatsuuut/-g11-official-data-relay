"""Complete yesterday's immutable Mashiro observations using K bytes in today's authenticated MORNING."""
import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime, timedelta
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

import mashiro_fixed_display_v1 as display
import research_state_isolation_v1 as isolation
from deme_research_delivery_v1 import compact, request_json, sha

REPO = 'tatsuuut/-g11-official-data-relay'
OPEN = {'WAITING', 'UNRESOLVED'}
FINAL = {'WIN', 'LOSS', 'REFUNDED', 'VOID'}
LIMIT = 256 * 1024 * 1024


def check(ok, reason):
    if not ok:
        raise ValueError('MASHIRO_SAVED_K_' + reason)


def previous_day(day):
    return (date.fromisoformat(day) - timedelta(days=1)).isoformat()


def saved_projection(day, *, required=True):
    view, _ = request_json(display.ORIGIN + '/api/g11-mashiro-research?through=' + day)
    if view.get('available') is True and view['projection']['day'] == day:
        return view['projection']
    check(not required, 'DAY_UNPUBLISHED')
    return None


def candidate_artifact(items, day):
    candidates = []
    for a in items:
        run = a.get('workflow_run') or {}
        match = re.fullmatch(r'mashiro-p3-paired-' + re.escape(day) + r'-(\d+)-(\d+)', a['name'])
        if (match and not a['expired'] and run.get('head_branch') == 'main'
                and int(match.group(1)) == run.get('id') and 0 < a['size_in_bytes'] < LIMIT
                and re.fullmatch('sha256:[0-9a-f]{64}', str(a.get('digest')))):
            candidates.append(a)
    return max(candidates, key=lambda a: (a['created_at'], a['id']), default=None)


def select(day, directory):
    target = previous_day(day)
    if not '2026-11-17' <= target <= '2026-12-14':
        return False
    p = saved_projection(target, required=False)
    if p is None or p['state'] != 'RECORDED' or not any(r['outcome'] in OPEN for r in p['rows']):
        return False
    artifact = None
    for page in range(1, 12):
        endpoint = f'repos/{REPO}/actions/artifacts?per_page=100&page={page}'
        records = json.loads(subprocess.check_output(['gh', 'api', endpoint], timeout=35))['artifacts']
        artifact = candidate_artifact(records, target)
        if artifact or len(records) < 100 or records[-1]['created_at'][:10] < target:
            break
    check(artifact, 'CLOSED_ARTIFACT_UNAVAILABLE')
    run_id = artifact['workflow_run']['id']
    run = json.loads(subprocess.check_output(['gh', 'api', f'repos/{REPO}/actions/runs/{run_id}'], timeout=35))
    check(run['head_branch'] == 'main' and run['head_repository']['full_name'] == REPO
          and run['path'] == '.github/workflows/g11-isolated-live-research.yml'
          and run['status'] == 'completed', 'CLOSED_ARTIFACT_PROVENANCE')
    directory.mkdir(parents=True, exist_ok=False)
    zipped = directory/'bundle.zip'
    with zipped.open('xb') as target_file:
        subprocess.run(['gh', 'api', f"repos/{REPO}/actions/artifacts/{artifact['id']}/zip"],
                       stdout=target_file, check=True, timeout=120)
    check(0 < zipped.stat().st_size < LIMIT
          and 'sha256:' + sha(zipped.read_bytes()) == artifact['digest'], 'ARTIFACT_DIGEST')
    with zipfile.ZipFile(zipped) as archive:
        files = [f for f in archive.infolist() if not f.is_dir()]
        check(len(files) == 1 and files[0].filename == 'state.tgz.gpg' and 0 < files[0].file_size < LIMIT,
              'ARTIFACT_MEMBER')
        with archive.open(files[0]) as member, (directory/'bundle.gpg').open('xb') as out:
            shutil.copyfileobj(member, out)
    zipped.unlink()
    (directory/'artifact.json').write_bytes(compact({'id': artifact['id'], 'sha256': artifact['digest'], 'run_id': run_id}))
    return True


def saved_k(store, official, source_day, target, accepted_keys):
    from rescue_night_result_parser_v1 import parse_night_results
    morning = store.load_verified_snapshot('morning', 'official-day-input')
    snapshot = morning['SNAPSHOT']
    check(snapshot['PAYLOAD']['OPERATIONAL_DATE_JST'] == source_day and previous_day(source_day) == target, 'K_MORNING_DAY')
    source = 'result-k-' + target.replace('-', '')
    receipt = store.response_receipt(source)
    source_url = 'https://www1.mbrace.or.jp/od2/K/' + target[:7].replace('-', '') + '/k' + target[2:].replace('-', '') + '.lzh'
    check(receipt and receipt in snapshot['SOURCE_RECEIPTS'] and receipt.get('SOURCE_ID') == source
          and receipt.get('URL') == source_url, 'K_RECEIPT')
    raw = store.response_bytes(source)
    check(sha(raw) == receipt['SHA256'], 'K_BYTES')
    name = 'K' + target[2:].replace('-', '') + '.TXT'
    parsed = parse_night_results(official.decode_cp932(official.LzhExtractor().extract_one(raw, name)),
                                 date.fromisoformat(target), official, official.parse_result_text, require_full_races=True)
    results = {r.race_key: json.loads(json.dumps(asdict(r))) for r in parsed}
    check(len(parsed) == len(results) and accepted_keys <= set(results), 'K_RACE_SET')
    return results, receipt, morning['SNAPSHOT_SHA256'], raw


def join_saved(observer, p, observations, results, receipt, source_sha, checked_at):
    target = p['day']
    _, policy, _ = observer.configuration()
    check(p['protocol_sha256'] == policy and p['recorded'] == len(observations) and p['recorded'] > 0
          and p['official_acquisition'] == 0 and p['production_mutated'] is False, 'PUBLISHED_CAPTURE_COUNT')
    out = deepcopy(p)
    index = {(r['race'], r['basis'], r['boat']): r for r in out['rows']}
    check(len(index) == len(out['rows']), 'DUPLICATE_PUBLISHED_TARGET')
    seen = set()
    morning_hashes = set()
    proofs = {}
    held = {}
    for key, (obs_hash, obs) in sorted(observations.items()):
        morning_hashes.add(obs['MORNING_SNAPSHOT_SHA256'])
        check(obs['OPERATIONAL_DATE_JST'] == target and obs['RACE_KEY'] == key, 'CLOSED_DAY')
        inputs = {r['boat']: r for r in obs['INPUT_ROWS']}
        check(set(inputs) == set(range(1, 7)), 'SIX_INPUT_BOATS')
        rows = {}
        for cohort in obs['COHORTS']:
            for t in cohort['targets']:
                boat = t['boat']
                id_ = (key, cohort['basis'], boat)
                check(id_ in index and id_ not in seen, 'PUBLISHED_TARGET_SET')
                row = index[id_]
                fixed = {'date': target, 'venue': int(key[9:11]), 'position': t['position'], 'grade': t['grade'],
                         'racer_number': t['racer_number'], 'conditions': t['conditions'], 'rank': t['exhibition_rank'],
                         'national': inputs[boat]['national_win_rate'], 'motor': inputs[boat]['motor'],
                         'captured_at': obs['CAPTURED_AT_JST'], 'deadline': obs['DEADLINE_AT_JST'],
                         'observation_sha256': obs_hash}
                check(all(row.get(k) == v for k, v in fixed.items()), 'CHANGED_PUBLISHED_OBSERVATION')
                seen.add(id_)
                rows[cohort['basis'], boat] = row
        try:
            check(observer.moment(receipt['CAPTURED_AT_JST']) >= observer.moment(obs['DEADLINE_AT_JST']), 'K_BEFORE_DEADLINE')
            joined = observer.join_result(obs, results[key], receipt['SHA256'], checked_at)
        except ValueError as exc:
            held[key] = str(exc)[:120]
            continue
        proof = dict(schema='G11_MASHIRO_FIXED_SAVED_K_RESULT_V1', source_morning_sha256=source_sha,
                     observation_sha256=obs_hash, official_archive_sha256=receipt['SHA256'], result=joined)
        proof_sha = sha(compact(proof))
        for result in joined['RESULTS']:
            row = rows[result['basis'], result['boat']]
            if row['outcome'] in FINAL:
                check(row['outcome'] == result['outcome'] and row['result_sha256'], 'EXISTING_OUTCOME_CONFLICT')
            else:
                check(row['outcome'] in OPEN and row['result_sha256'] is None, 'ORIGINAL_OUTCOME')
                row.update(outcome=result['outcome'], result_sha256=proof_sha)
        proofs[key] = proof
    check(seen == set(index) and len(morning_hashes) == 1, 'PUBLISHED_SET_COVERAGE')
    out['updated_at'] = checked_at
    return out, morning_hashes.pop(), proofs, held


def verify_capture_files(observer, folder, bundle, day):
    observations = {}
    for name, info in bundle['members'].items():
        if isolation.fixed_mashiro_member(name, day) != 'capture':
            continue
        file = folder/'files'/name
        key = file.stem.removeprefix('mashiro-fixed-v1-')
        envelope = json.loads(file.read_bytes())
        check(envelope.get('SOURCE_SHA256') == [r['SHA256'] for r in envelope['SOURCE_RECEIPTS']], 'IMMUTABLE_RECEIPTS')
        obs = observer.validate_observation({'SNAPSHOT_SHA256': info['sha256'], 'SNAPSHOT': envelope}, key, path=file)
        check(info['mtime_ns'] < int(observer.moment(obs['DEADLINE_AT_JST']).timestamp() * 1_000_000_000), 'OBSERVATION_AFTER_CLOSE')
        check(bundle['source_snapshots'].get('store/snapshots/morning/official-day-input.json') == obs['MORNING_SNAPSHOT_SHA256']
              and bundle['source_snapshots'].get('store/snapshots/predeadline/beforeinfo-' + key + '.json') == obs['BEFOREINFO_SNAPSHOT_SHA256'],
              'CLOSED_ORIGINAL_BINDINGS')
        observations[key] = (info['sha256'], obs)
    return observations


def build(args):
    day = previous_day(args.day)
    check('2026-11-17' <= day <= '2026-12-14' and re.fullmatch(
          'g11-state-v4-(?:critical|published)-' + re.escape(args.day) + r'-\d+(?:-\d+-checkpoint)?', args.source_key), 'SOURCE_CHECKPOINT')
    sys.path.insert(0, str(args.source_root.resolve()))
    from g11.canonical import official_pipeline as official
    spec = importlib.util.spec_from_file_location('fixed_mashiro_observer', args.observer_root.resolve()/'g11/research/mashiro_fixed_v1/observer.py')
    observer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(observer)
    directory = args.directory.resolve()
    folder = directory/'original'
    bundle = isolation.read_bundle(folder, day)
    check(bundle['closed'] is True, 'ORIGINAL_NOT_CLOSED')
    observations = verify_capture_files(observer, folder, bundle, day)
    prior = saved_projection(day)
    official_rows, receipt, source_sha, raw = saved_k(official.OfficialArtifactStore(args.state_root.resolve()/'store'),
                                                     official, args.day, day, set(observations))
    now = datetime.now(display.JST).isoformat()
    check(observer.moment(receipt['CAPTURED_AT_JST']) <= observer.moment(now), 'FUTURE_K')
    updated, morning_sha, proofs, held = join_saved(observer, prior, observations, official_rows, receipt, source_sha, now)
    changed = sum(a['outcome'] in OPEN and b['outcome'] in FINAL for a, b in zip(prior['rows'], updated['rows']))
    if changed == 0 and any(r['outcome'] in FINAL for r in prior['rows']):
        print('G11_MASHIRO_SAVED_K_ALREADY_RECONCILED=' + json.dumps({'day': day, 'held_races': held}, sort_keys=True))
        return False
    check(changed > 0, 'NO_VERIFIED_OUTCOMES')
    evidence = directory/'evidence'
    evidence.mkdir(exist_ok=False)
    artifact = json.loads((directory/'artifact.json').read_text())
    provenance = {'method': 'NEXT_MORNING_SAVED_OFFICIAL_K', 'result_source_day': args.day, 'result_source_checkpoint': args.source_key,
                  'result_source_morning_sha256': source_sha, 'official_archive_sha256': receipt['SHA256'],
                  'observation_bundle_sha256': bundle['bundle_sha256'], 'observation_artifact': artifact}
    delivery = dict(schema='G11_MASHIRO_FIXED_DISPLAY_DELIVERY_V1', operational_date=day, updated_at=now,
                    source_checkpoint=bundle['source_checkpoint'], source_morning_sha256=morning_sha, projection=updated,
                    settlement_provenance=provenance)
    (evidence/'official-result.lzh').write_bytes(raw)
    (evidence/'official-receipt.json').write_bytes(compact(receipt))
    (evidence/'outcomes.json').write_bytes(compact(proofs))
    if held:
        (evidence/'held-races.json').write_bytes(compact(held))
    (evidence/'delivery.json').write_bytes(compact(delivery))
    print('G11_MASHIRO_SAVED_K_PREPARED=' + json.dumps(dict(day=day, recorded=len(observations),
          changed_rows=changed, target_rows=len(updated['rows']), held_races=held,
          outcomes=dict(Counter(r['outcome'] for r in updated['rows'])), acquisition=0), sort_keys=True))
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['select', 'build', 'publish'])
    parser.add_argument('--day', required=True)
    parser.add_argument('--directory', type=Path, default=Path('.mashiro-saved-k'))
    parser.add_argument('--state-root', type=Path, default=Path('.research-state'))
    parser.add_argument('--source-root', type=Path, default=Path('.g11-private'))
    parser.add_argument('--observer-root', type=Path, default=Path('.g11-mashiro-fixed'))
    parser.add_argument('--source-key')
    args = parser.parse_args()
    if args.mode == 'select':
        selected = select(args.day, args.directory)
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            print('needed=' + str(selected).lower(), file=output)
    elif args.mode == 'build':
        prepared = build(args)
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            print('prepared=' + str(prepared).lower(), file=output)
    else:
        value = json.loads((args.directory/'evidence/delivery.json').read_bytes())
        check(value['operational_date'] == previous_day(args.day), 'PUBLISH_DATE')
        ack = display.publish(value)
        print('G11_MASHIRO_SAVED_K_READBACK=' + json.dumps(ack, sort_keys=True))


if __name__ == '__main__':
    main()

