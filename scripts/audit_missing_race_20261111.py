"""Read-only diagnosis of Miyajima 3R in the authenticated November 11 state.

Only official entry/result text and saved-proof identities enter the report.
No acquisition, prediction, publication, checkpoint or financial write is made.
"""
import argparse
from dataclasses import asdict
from datetime import date
import hashlib
import json
from pathlib import Path
import re
from unittest.mock import patch

DAY = '2026-11-11'
KEY = '20261111-17-03'


def hashes(state):
    roots = [state / 'store/snapshots', state / 'daily-runtime', state / 'relay-state.json']
    paths = [p for root in roots for p in ([root] if root.is_file() else root.rglob('*')) if p.is_file()]
    return {str(p.relative_to(state)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def section(text, venue, race_no, marker, race_re):
    markers = list(re.finditer(r'(?m)^(\d{2})' + marker + r'\s*$', text))
    for i, m in enumerate(markers):
        if m[1] != venue:
            continue
        block = text[m.end():markers[i+1].start() if i+1 < len(markers) else len(text)]
        races = list(race_re.finditer(block))
        for j, r in enumerate(races):
            if int(r[1]) == race_no:
                return block[r.start():races[j+1].start() if j+1 < len(races) else len(block)]
    raise ValueError('OFFICIAL_TARGET_SECTION_MISSING')


def audit(state, source):
    from g11.canonical import official_pipeline as official
    from g11.relay import daily_runtime_authority_v1 as daily
    from morning_race_isolation_v1 import isolated_results
    before = hashes(state)
    authority = daily.verify_runtime(state, DAY, source)
    for phase in ('MORNING', 'PREDEADLINE'):
        daily.verify_locks(state, DAY, phase, exact=True)
    artifact = official.OfficialArtifactStore(state / 'store')
    with patch.object(official.OfficialHttpClient, 'fetch_once', side_effect=AssertionError('NO_ACQUISITION')), \
         patch.object(official.OfficialHttpClient, 'fetch_validated_once', side_effect=AssertionError('NO_ACQUISITION')):
        morning = artifact.load_verified_snapshot('morning', 'official-day-input')
        projection = artifact.load_verified_snapshot('morning', 'workbook-projection')
        night = artifact.load_verified_snapshot('night', 'official-day-results')
        p = morning['SNAPSHOT']['PAYLOAD']
        n = night['SNAPSHOT']['PAYLOAD']
        bound = {r['SOURCE_ID'] for r in morning['SNAPSHOT']['SOURCE_RECEIPTS']} | {r['SOURCE_ID'] for r in night['SNAPSHOT']['SOURCE_RECEIPTS']}
        b_id, k_id = 'schedule-b-20261111', 'result-k-20261111'
        if not {b_id,k_id} <= bound:
            raise ValueError('OFFICIAL_RECEIPT_NOT_BOUND')
        extractor = official.LzhExtractor()
        texts = {}
        for sid, url in ((b_id,official.schedule_url(date.fromisoformat(DAY))), (k_id,official.result_url(date.fromisoformat(DAY)))):
            receipt = artifact.response_receipt(sid)
            if receipt['URL'] != url:
                raise ValueError('OFFICIAL_RECEIPT_URL')
            member = official.CanonicalRunner._archive_member(url)
            texts[sid] = official._normalize(official.decode_cp932(extractor.extract_one(artifact.response_bytes(sid),member)))
        q = []
        b_races = official.parse_schedule_text(texts[b_id], date.fromisoformat(DAY), quarantined_races=q)
        results, rejected = isolated_results(texts[k_id], date.fromisoformat(DAY), official, official.parse_result_text)
        raw = section(texts[b_id], '17', 3, 'BBGN', official._B_RACE_RE)
        entry_lines = [l for l in raw.splitlines() if re.match(r'^\s*[1-6]\s+\d{4}',l)]
        saved = []
        for path in sorted((state/'store/snapshots').rglob('*.json')):
            if KEY.lower() not in path.name.lower():
                continue
            stage = path.parent.name
            verified = artifact.load_verified_snapshot(stage,path.stem)
            saved.append({'path':str(path.relative_to(state/'store')), 'sha256':verified['SNAPSHOT_SHA256']})
        lock = projection['SNAPSHOT']['PAYLOAD']
        index = lock['LOCK49_FIELDS'].index('標準レースキー')
        keys = [row[index] for row in lock['LOCK49_ROWS']]
        report = {'schema':'G11_MISSING_RACE_AUDIT_20261111_V1','day':DAY,'target':KEY,
            'runtime_source_sha':authority['runtime_source_sha'],'authority_sha256':authority['authority_sha256'],
            'morning_input_sha256':morning['SNAPSHOT_SHA256'],'night_results_sha256':night['SNAPSHOT_SHA256'],
            'morning_admitted_count':len(p['RACES']),'lock49_count':len(keys),
            'target_in_lock49':KEY in keys,'saved_target_snapshots':saved,
            'morning_quarantine':p.get('QUARANTINED_RACES',[]),'prior_result_quarantine':p.get('RESULT_QUARANTINE',[]),
            'official_b':{'receipt':artifact.response_receipt(b_id),'admitted_count':len(b_races),'quarantine':q,
                'target_section_sha256':hashlib.sha256(raw.encode()).hexdigest(),
                'entry_lines':[{'text':l,'parsed':bool(official._B_ENTRY_RE.match(l))} for l in entry_lines]},
            'official_k':{'receipt':artifact.response_receipt(k_id),'parsed_count':len(results),'rejected':rejected,
                'target_results':[asdict(r) for r in results if r.race_key==KEY]},
            'night_scope':{'status':n['STATUS'],'expected_count':n['EXPECTED_RESULT_COUNT'],'result_count':len(n['RESULTS'])},
            'predictions_recalculated':0,'network_acquisition':0,'publication':False}
    if hashes(state) != before:
        raise ValueError('SAVED_STATE_MUTATED')
    report['saved_state_unchanged'] = True
    print('G11_MISSING_RACE_AUDIT=' + json.dumps(report,ensure_ascii=False,sort_keys=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--state',required=True,type=Path)
    parser.add_argument('--source',required=True,type=Path)
    args = parser.parse_args()
    audit(args.state.resolve(),args.source.resolve())
