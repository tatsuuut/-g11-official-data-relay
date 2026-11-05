"""One reviewed, offline continuation of the Nov 6 C04 admission incident.

Keep both failed attempts and their acquired bytes. No acquisition, source/model
replacement, clock override, canonical promotion, or repeated repair is allowed.
"""
import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

DAY = '2026-11-06'
SOURCE = '40dd169569bd0bea8049d92e1b61d05da4d38839'
ATTEMPTS = {'42129528978.json', '42130300335.json'}
REASON = 'RESCUE_FORMAL_GATE:C04_SOURCE_RECEIPTS'


def require(value, code):
    if not value:
        raise RuntimeError('SAVED_RECEIPT_REPAIR:' + code)


def inspect_attempts(state, day, source, now):
    require(day == DAY == now.date().isoformat(), 'CURRENT_DAY_ONLY')
    require(source == SOURCE, 'FROZEN_SOURCE')
    paths = list((state / 'recovery-failures' / day).glob('*.json'))
    require({p.name for p in paths} == ATTEMPTS, 'EXACT_FAILED_ATTEMPTS')
    evidence = {}
    for path in paths:
        require(not path.is_symlink(), 'FAILURE_SYMLINK')
        raw = path.read_bytes()
        record = json.loads(raw)
        require(record.get('reason') == REASON and record.get('source_sha') == source,
                'FAILED_REASON_OR_SOURCE')
        evidence[path.name] = hashlib.sha256(raw).hexdigest()
    require(not (state / 'rescue/feed.json').exists()
            and not (state / 'rescue/generation-evidence.json').exists(), 'EXISTING_PREDICTIONS')
    allowed = {'morning/official-day-input.json', 'morning/abeken-head-features.json'}
    root = state / 'rescue/store/snapshots'
    actual = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
    require(actual == allowed and not any(p.is_symlink() for p in root.rglob('*')),
            'INPUT_ONLY_SNAPSHOTS')
    require(not any((state / 'store/snapshots/morning').glob('*.json')), 'CANONICAL_LOCK_PRESENT')
    require(not (state / 'recovery-repairs' / day / 'receipt-scope-v1.json').exists(), 'ALREADY_ATTEMPTED')
    return evidence


def run_if_reviewed(argv):
    if os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch':
        return False
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', required=True)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--source-sha', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--run-id', type=int, required=True)
    args = parser.parse_args(argv)
    if args.date != DAY or len(list((args.state / 'recovery-failures' / args.date).glob('*.json'))) < 2:
        return False
    require(not os.environ.get('G11_RELAY_NOW_JST'), 'CLOCK_OVERRIDE')
    from g11.relay import delayed_morning_recovery_v1 as recovery
    from g11.canonical import official_pipeline as official
    from morning_source_receipt_limit_v1 import receipt_limit
    now = recovery.clock()
    evidence = inspect_attempts(args.state, args.date, args.source_sha, now)
    certificate = recovery.verify_boundary(args.state / 'rescue', args.date)
    require(certificate['runtime_source_sha'] == args.source_sha, 'BOUNDARY_SOURCE')
    store = official.OfficialArtifactStore(args.state / 'rescue/store')
    verified = store.load_verified_snapshot('morning', 'official-day-input')
    require(len(verified['SNAPSHOT']['SOURCE_RECEIPTS']) == 23
            and receipt_limit(verified) >= 23, 'SAVED_PROVENANCE')
    # Run the original byte identity, receipt, RAW38 and admission checks before
    # consuming the single additional repair attempt.
    bridge = official.FORMAL_GATE.build_canonical_official_morning_input(verified)
    official.FORMAL_GATE.assess_day_bundle(bridge['RAW_DAY'], bridge['FIELD_OBSERVATIONS'])
    marker = args.state / 'recovery-repairs' / args.date / 'receipt-scope-v1.json'
    marker.parent.mkdir(parents=True, exist_ok=True)
    record = {'at_jst': now.isoformat(), 'source_sha': args.source_sha,
              'run_id': args.run_id, 'preserved_failures': evidence,
              'input_sha256': verified['SNAPSHOT_SHA256'], 'network_allowed': False,
              'canonical_morning_promoted': False, 'maximum_repair_attempts': 1}
    with marker.open('x') as handle:
        json.dump(record, handle, sort_keys=True)
        handle.write('\n')
    try:
        with patch.object(official.OfficialHttpClient, 'fetch_once',
                          side_effect=RuntimeError('SAVED_RECEIPT_REPAIR:NETWORK_PROHIBITED')):
            feed = recovery.run(args.state.resolve(), args.date, args.output.resolve(), args.source_sha, args.run_id)
    except Exception as exc:
        marker.with_suffix('.result.json').write_text(json.dumps({'status':'FAILED','reason':str(exc)}) + '\n')
        raise
    marker.with_suffix('.result.json').write_text(json.dumps({'status':'SAVED','run_id':args.run_id}) + '\n')
    print(json.dumps({'STATUS':'DELAYED_RECOVERY_SAVED','PHASE':'SAME_DAY_RESCUE',
        'OPERATIONAL_DATE_JST':args.date, 'NORMAL_MORNING_LOCKS':0,
        'DELAYED_RECOVERY_LOCKS':feed['same_day_rescue']['rescued_race_count'],
        'STATE_CHANGED':True,'FEED_EMITTED':True,'RESULTS_USED':False,
        'SAVED_RECEIPT_REPAIR':'PASS'}, sort_keys=True))
    return True
