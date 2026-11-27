"""Deliver a verified warm capture before the ordinary complete backup.

This transport adapter only reads existing receipts and LOCK metadata. Native
capture, inference, source admission and the shared request ledger stay intact.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import encrypt_runtime_state_v1 as checkpoint

JST = timezone(timedelta(hours=9))
OUT = Path('.relay-output')
STATE = Path('.relay-state')
CIPHER = Path('.relay-cache/state.tgz.gpg')
START = OUT / 'warm-publication-start.json'
SCHEMA = 'G11_VERIFIED_WARM_PUBLICATION_START_V1'


def require(ok, reason):
    if not ok:
        raise ValueError('G11_WARM_PUBLICATION_' + reason)


def read(path):
    return json.loads(Path(path).read_bytes())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def output(**fields):
    with open(os.environ['GITHUB_OUTPUT'], 'a') as handle:
        for key, value in fields.items():
            handle.write(key + '=' + str(value) + '\n')


def identity(day, result):
    run = os.environ['GITHUB_RUN_ID']
    require(re.fullmatch('[0-9]+', run) and result.get('OPERATIONAL_DATE_JST') == day, 'IDENTITY')
    gate = result['CRITICAL']
    owner = read(STATE / 'daily-runtime' / day / 'authority.json')
    require(gate.get('SCHEMA') == 'G11_PREDEADLINE_CRITICAL_CHECKPOINT_V1'
            and gate.get('CRITICAL_CHECKPOINT_STATUS') == 'PASS'
            and gate.get('LAST_CRITICAL_RUN_ID') == run and gate.get('OPERATIONAL_DATE') == day
            and gate.get('POST_DEADLINE_GENERATION') == 0
            and re.fullmatch('[0-9a-f]{64}', str(gate.get('AUTHORITY_SHA256', '')))
            and re.fullmatch('[0-9a-f]{40}', str(gate.get('RUNTIME_SOURCE_SHA', '')))
            and owner.get('immutable') is True and owner.get('operational_date_jst') == day
            and owner.get('authority_sha256') == gate['AUTHORITY_SHA256']
            and owner.get('runtime_source_sha') == gate['RUNTIME_SOURCE_SHA'], 'AUTHORITY')
    return run, gate


def first_receipt(day, result):
    run, gate = identity(day, result)
    source = result['ACQUISITION']
    acquired, slots = source['ACQUIRED'], source['WARM_REMAINING']
    require(type(acquired) is list and all(isinstance(item, str) for item in acquired)
            and len(set(acquired)) == len(acquired) and 0 < len(acquired) < 8
            and type(slots) is int and 1 <= slots <= 7 and len(acquired) + slots == 8, 'FIRST_BUDGET')
    public = read(OUT / 'hot-delta-readback.json')
    binding = public.get('daily_runtime', {})
    require(public.get('operational_date_jst') == day and public.get('stage') == 'PREDEADLINE'
            and public.get('status') == 'PASS' and str(public.get('source', {}).get('run_id')) == run
            and public.get('source', {}).get('github_sha') == gate['RUNTIME_SOURCE_SHA']
            and binding.get('schema') == 'G11_DAILY_RUNTIME_AUTHORITY_V1'
            and binding.get('operational_date_jst') == day and binding.get('phase') == 'PREDEADLINE'
            and binding.get('immutable') is True and binding.get('authority_sha256') == gate['AUTHORITY_SHA256']
            and binding.get('runtime_source_sha') == gate['RUNTIME_SOURCE_SHA'], 'FIRST_READBACK')
    accepted = (OUT / 'hot-delta-accepted').read_text().strip()
    require(re.fullmatch('[0-9a-f]{64}', accepted), 'FIRST_READBACK')
    receipt = read(STATE / 'daily-runtime' / day / 'delivery' / 'PREDEADLINE' / (accepted + '.json'))
    require(receipt.get('schema') == 'G11_DAILY_SITE_DELIVERY_RECEIPT_V1'
            and receipt.get('operational_date_jst') == day and receipt.get('phase') == 'PREDEADLINE'
            and receipt.get('authority_sha256') == gate['AUTHORITY_SHA256']
            and receipt.get('runtime_source_sha') == gate['RUNTIME_SOURCE_SHA']
            and receipt.get('site_payload_sha256') == accepted
            and receipt.get('site_get_sha256') == sha(OUT / 'hot-delta-readback.json')
            and receipt.get('http_success') is True and receipt.get('site_get_readback') == 'PASS', 'FIRST_RECEIPT')
    return run, gate, acquired, slots


def start(day, base_key):
    START.unlink(missing_ok=True)
    require(day >= '2026-11-27' and day == datetime.now(JST).date().isoformat(), 'CURRENT_DAY')
    result = read(OUT / 'result.json')
    run, gate, acquired, slots = first_receipt(day, result)
    state = read(STATE / 'relay-state.json')
    require(state.get('MORNING_COMPLETE') is True and state.get('CRITICAL') == gate, 'FIRST_STATE')
    proof = read(OUT / 'compact-publication-proof.json')
    ready = checkpoint.compact_ready('g11-state-v4-critical-' + day + '-' + run)
    require(ready is not None and checkpoint.checkpoint_day(base_key) == day
            and proof.get('schema') == 'G11_COMPLETE_COMPACT_PUBLICATION_PROOF_V1'
            and proof.get('day') == day and proof.get('run_id') == run
            and proof.get('base_key') == ready['base_key'] == base_key
            and proof.get('authority_sha256') == gate['AUTHORITY_SHA256']
            and proof.get('runtime_source_sha') == gate['RUNTIME_SOURCE_SHA']
            and proof.get('delta_cipher_sha256') == ready['cipher_sha256']
            and proof.get('base_cipher_sha256') == checkpoint.file_sha(CIPHER), 'PHYSICAL_BASE')
    value = {'schema': SCHEMA, 'day': day, 'run_id': run, 'base_key': base_key,
        'base_cipher_sha256': proof['base_cipher_sha256'], 'first_result_sha256': sha(OUT / 'result.json'),
        'first_public_sha256': sha(OUT / 'hot-delta-readback.json'),
        'first_accepted_sha256': (OUT / 'hot-delta-accepted').read_text().strip(),
        'authority_sha256': gate['AUTHORITY_SHA256'], 'runtime_source_sha': gate['RUNTIME_SOURCE_SHA'],
        'acquired': acquired, 'remaining': slots}
    checkpoint.atomic_json(START, value)
    output(ready='true', status='attempted')
    try:
        response = subprocess.run(['gh', 'workflow', 'run', '364923281', '-f', 'mode=wild4',
            '-f', 'operational_date=' + day, '-f', 'capture_stage=warm',
            '-f', 'predeadline_checkpoint_key=g11-state-v4-critical-' + day + '-' + run,
            '-f', 'predeadline_delta_key=g11-state-v4-delta-' + day + '-' + run + '-warm',
            '-f', 'prewarm_base_key=' + base_key], capture_output=True, check=False, timeout=12)
        if response.returncode == 0:
            output(status='sent')
            print('G11_WARM_PREWARM=DISPATCH_ACCEPTED_BEFORE_CAPTURE', flush=True)
        else:
            print('G11_WARM_PREWARM=DISPATCH_UNCONFIRMED', flush=True)
    except (OSError, subprocess.SubprocessError):
        print('G11_WARM_PREWARM=DISPATCH_UNCONFIRMED', flush=True)
    return value


def warm_context(day):
    value = read(START)
    first = read(OUT / 'first-critical-result.json')
    run, gate, acquired, slots = first_receipt(day, first)
    require(value.get('schema') == SCHEMA and value.get('day') == day and value.get('run_id') == run
            and value.get('first_result_sha256') == sha(OUT / 'first-critical-result.json')
            and value.get('first_public_sha256') == sha(OUT / 'hot-delta-readback.json')
            and value.get('first_accepted_sha256') == (OUT / 'hot-delta-accepted').read_text().strip()
            and value.get('authority_sha256') == gate['AUTHORITY_SHA256']
            and value.get('runtime_source_sha') == gate['RUNTIME_SOURCE_SHA']
            and value.get('acquired') == acquired and value.get('remaining') == slots
            and value.get('base_cipher_sha256') == checkpoint.file_sha(CIPHER), 'START_BINDING')
    result = read(OUT / 'result.json')
    _, after = identity(day, result)
    source = result['ACQUISITION']
    fresh, remaining = source['ACQUIRED'], source.get('WARM_REMAINING', 0)
    require(type(fresh) is list and all(isinstance(item, str) for item in fresh)
            and len(set(fresh)) == len(fresh) and not set(fresh).intersection(acquired)
            and len(fresh) <= slots and type(remaining) is int and 0 <= remaining < slots
            and (not remaining or remaining + len(fresh) == slots)
            and after['AUTHORITY_SHA256'] == gate['AUTHORITY_SHA256']
            and after['RUNTIME_SOURCE_SHA'] == gate['RUNTIME_SOURCE_SHA']
            and read(STATE / 'relay-state.json').get('CRITICAL') == after, 'WARM_BUDGET_OR_STATE')
    return value, result


def discard_prepared():
    for name in ('critical-prepared-feed.json', 'critical-prepared-proof.json', 'critical-prepared-proof.tmp'):
        (OUT / name).unlink(missing_ok=True)


def prepare(day):
    value, result = warm_context(day)
    if not result['ACQUISITION']['ACQUIRED']:
        output(ready='false')
        return
    discard_prepared()
    from compact_publication_checkpoint_v1 import secret
    checkpoint.prepare_compact_checkpoint(STATE, CIPHER, value['base_key'], secret=secret(), capture_stage='warm')
    key = 'g11-state-v4-published-' + day + '-' + value['run_id'] + '-' + os.environ['GITHUB_RUN_ATTEMPT'] + '-checkpoint'
    output(ready='true' if checkpoint.compact_ready(key, capture_stage='warm') else 'false')


def critical_encrypt():
    require(read(OUT / 'result.json').get('CRITICAL', {}).get('CRITICAL_CHECKPOINT_STATUS') == 'PASS', 'CRITICAL_GATE')
    CIPHER.parent.mkdir(exist_ok=True)
    from compact_publication_checkpoint_v1 import secret
    # The same delta-only preparation keeps the restored full cipher intact.
    checkpoint.prepare_compact_checkpoint(STATE, CIPHER, os.environ['G11_DELTA_BASE_KEY'], secret=secret())
    output(ready='true')


def publish(day):
    require(os.environ.get('CRITICAL_CHECKPOINT_VERIFIED') == 'true', 'REMOTE_PROOF_REQUIRED')
    value, result = warm_context(day)
    proof = read(OUT / 'warm-publication-proof.json')
    key = 'g11-state-v4-published-' + day + '-' + value['run_id'] + '-' + os.environ['GITHUB_RUN_ATTEMPT'] + '-checkpoint'
    require(proof.get('schema') == 'G11_COMPLETE_COMPACT_PUBLICATION_PROOF_V1'
            and proof.get('capture_stage') == 'warm' and proof.get('day') == day
            and proof.get('run_id') == value['run_id'] and proof.get('publication_key') == key
            and str(proof.get('publication_attempt')) == os.environ['GITHUB_RUN_ATTEMPT']
            and proof.get('authority_sha256') == result['CRITICAL']['AUTHORITY_SHA256']
            and proof.get('runtime_source_sha') == result['CRITICAL']['RUNTIME_SOURCE_SHA'], 'WARM_REMOTE_PROOF')
    discard_prepared()
    subprocess.run([sys.executable, '-S', 'scripts/critical_saved_publication_v1.py', '--day', day],
        check=True, env={**os.environ, 'G11_WARM_PUBLICATION': 'true'})


def handoff(day, key, stage, prewarm):
    ready = checkpoint.compact_ready(key, capture_stage=stage)
    run = os.environ['GITHUB_RUN_ID']
    require(ready is not None and ready['day'] == day and ready['run_id'] == run, 'HANDOFF_READBACK')
    suffix = '-warm' if stage == 'warm' else ''
    require(ready['checkpoint_key'] == 'g11-state-v4-critical-' + day + '-' + run
            and ready['delta_key'] == 'g11-state-v4-delta-' + day + '-' + run + suffix, 'HANDOFF_IDENTITY')
    gate = read(OUT / 'result.json')['CRITICAL']
    alternate = gate.get('LAST_TIMELY_ALTERNATE_MARKET_COUNT', 0)
    require(gate.get('CRITICAL_CHECKPOINT_STATUS') == 'PASS'
            and (gate.get('LAST_TIMELY_SOURCE_COUNT', 0) > 0 or type(alternate) is int and alternate > 0), 'HANDOFF_INPUT')
    output(attempted='true')
    if prewarm in ('sent', 'attempted'):
        if prewarm == 'sent':
            output(sent='true')
        print('WILD4_COMPACT_HANDOFF=EXISTING_PREWARM_DISPATCH', flush=True)
        return
    arguments = ['gh', 'workflow', 'run', '364923281', '-f', 'mode=wild4', '-f', 'operational_date=' + day,
        '-f', 'predeadline_checkpoint_key=' + ready['checkpoint_key'], '-f', 'predeadline_delta_key=' + ready['delta_key']]
    if stage == 'warm':
        arguments += ['-f', 'capture_stage=warm']
    subprocess.run(arguments, check=True, timeout=12)
    output(sent='true')
    print('WILD4_COMPACT_HANDOFF=DISPATCH_ACCEPTED_BEFORE_FULL_TRANSFER', flush=True)


def chain(day):
    from dynamic_last_race_heartbeat import decision
    from critical_saved_publication_v1 import continuation_delay
    def heartbeat():
        return decision(read(OUT / 'result.json'), date.fromisoformat(day), datetime.now(JST))
    first = heartbeat()
    if first not in ('CONTINUE', 'CHECK_HANDOFF'):
        label = {'HANDOFF': 'NIGHT_HANDOFF', 'RESCUE_HANDOFF': 'RESCUE_NIGHT_HANDOFF'}.get(first, first)
        print('G11_PREDEADLINE_CHAIN=' + label)
        return
    time.sleep(continuation_delay(read(OUT / 'result.json'), day, os.environ['GITHUB_RUN_ID'], datetime.now(JST)))
    after = heartbeat()
    if after not in ('CONTINUE', 'CHECK_HANDOFF'):
        print('G11_PREDEADLINE_CHAIN=' + ('RESCUE_NIGHT_HANDOFF' if after == 'RESCUE_HANDOFF' else after))
        return
    subprocess.run([sys.executable, 'scripts/predeadline_dispatch_v1.py', '--day', day, '--mode', 'continuation'], check=True)
    output(ready='true')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('start', 'prepare', 'publish', 'handoff', 'chain', 'critical-encrypt'))
    parser.add_argument('--day')
    parser.add_argument('--base-key', default='')
    parser.add_argument('--key', default='')
    parser.add_argument('--capture-stage', choices=('critical', 'warm'), default='critical')
    parser.add_argument('--prewarm', default='')
    args = parser.parse_args()
    args.day = args.day or os.environ.get('OPERATIONAL_DATE') or read(OUT / 'result.json')['OPERATIONAL_DATE_JST']
    if args.mode == 'start':
        start(args.day, args.base_key)
    elif args.mode == 'prepare':
        prepare(args.day)
    elif args.mode == 'publish':
        publish(args.day)
    elif args.mode == 'handoff':
        handoff(args.day, args.key, args.capture_stage, args.prewarm)
    elif args.mode == 'critical-encrypt':
        critical_encrypt()
    else:
        chain(args.day)


if __name__ == '__main__':
    main()
