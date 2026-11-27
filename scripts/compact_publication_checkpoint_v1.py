"""Verify a complete saved base and authenticated delta before early delivery.

Only transport state is read here. Every file and directory remains covered by
the existing complete inventories; ordinary full backups still follow delivery.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
from urllib.parse import urlencode

import encrypt_runtime_state_v1 as checkpoint
from extract_runtime_state_v1 import extract_complete_state

BASE_SCHEMA = 'G11_AUTHENTICATED_PUBLICATION_BASE_V1'
PROOF_SCHEMA = 'G11_COMPLETE_COMPACT_PUBLICATION_PROOF_V1'
JST = timezone(timedelta(hours=9))
OUT = Path('.relay-output')


def require(ok, reason):
    if not ok:
        raise ValueError('G11_COMPACT_PUBLICATION_' + reason)


def digest(value):
    return hashlib.sha256(checkpoint.canonical(value)).hexdigest()


def secret():
    require(bool(os.environ.get('G11_PRIVATE_REPO_TOKEN')), 'SECRET_INPUT')
    value = (os.environ['G11_PRIVATE_REPO_TOKEN'] + '\n').encode()
    require(0 < len(value) <= 4096 and value.count(b'\n') == 1, 'SECRET_INPUT')
    return value


def decrypt(cipher, archive):
    with checkpoint.secret_pipe(secret()) as fd:
        result = subprocess.run(['gpg', '--batch', '--yes', '--pinentry-mode', 'loopback',
            '--passphrase-fd', str(fd), '--decrypt', '--output', str(archive), str(cipher)],
            pass_fds=(fd,), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False)
    require(result.returncode == 0, 'AUTHENTICATION')


def authority(root, day):
    value = json.loads((root / 'daily-runtime' / day / 'authority.json').read_bytes())
    require(value.get('immutable') is True and value.get('operational_date_jst') == day,
            'AUTHORITY')
    return value


def retain_base(root, cipher, base_key, day):
    """Called only by the authenticated, complete extraction path below."""
    require(checkpoint.checkpoint_day(base_key) == day, 'BASE_DAY')
    saved = json.loads((OUT / 'base-inventory.json').read_bytes())
    require(saved.get('schema') == checkpoint.DELTA_SCHEMA, 'BASE_INDEX')
    inventory = checkpoint.validate_inventory(saved['inventory'])
    state = json.loads((root / 'relay-state.json').read_bytes())
    require(state.get('OPERATIONAL_DATE_JST') == day and state.get('MORNING_COMPLETE') is True,
            'BASE_STATE')
    owner = authority(root, day)
    name = 'daily-runtime/' + day + '/authority.json'
    require(inventory['files'][name]['sha256'] == checkpoint.file_sha(root / name),
            'BASE_AUTHORITY')
    checkpoint.atomic_json(OUT / 'authenticated-base.json', {
        'schema': BASE_SCHEMA, 'day': day, 'base_key': base_key,
        'cipher_sha256': checkpoint.file_sha(cipher), 'inventory_sha256': digest(inventory),
        'authority_sha256': owner['authority_sha256'],
        'runtime_source_sha': owner['runtime_source_sha']})


def restore(day, base_key):
    """Keep the prior full restore and cross-day reset, then bind its evidence."""
    root, cipher = Path('.relay-state'), Path('.relay-cache/state.tgz.gpg')
    (OUT / 'authenticated-base.json').unlink(missing_ok=True)
    root.mkdir(exist_ok=True)
    if not cipher.is_file():
        return
    archive = Path(os.environ['RUNNER_TEMP']) / 'g11-relay-state.tgz'
    decrypt(cipher, archive)
    extract_complete_state(archive, root, inventory_path=OUT / 'base-inventory.json')
    metadata_path = root / 'relay-state.json'
    if metadata_path.is_file():
        metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
        if metadata.get('OPERATIONAL_DATE_JST') != day:
            store = root / 'store'
            for name in ('manifest.json', 'scope.json', 'responses', 'rejected-responses',
                         'snapshots', 'ledger'):
                target = store / name
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
            rescue = root / 'rescue'
            if rescue.is_dir():
                shutil.rmtree(rescue)
            elif rescue.exists():
                rescue.unlink()
            metadata = {'SCHEMA': 'G11_PRIVATE_PUBLIC_RELAY_STATE_V1',
                'OPERATIONAL_DATE_JST': day, 'MORNING_COMPLETE': False, 'LAST_PHASE': None,
                'LAST_STORE_SHA256': None, 'LAST_PUBLISHED_FEED_SHA256': None}
            metadata_path.write_text(json.dumps(metadata, sort_keys=True, separators=(',', ':'))
                                     + '\n', encoding='utf-8')
    try:
        retain_base(root, cipher, base_key, day)
    except (OSError, ValueError, KeyError, TypeError):
        # A legacy/cross-day base still follows the original full-checkpoint path.
        (OUT / 'authenticated-base.json').unlink(missing_ok=True)
        print('G11_COMPACT_PUBLICATION_BASE=UNAVAILABLE_FULL_CHECKPOINT_RETAINED', flush=True)


def verify_manifest(manifest, archive, *, root, cipher, ready, base, result):
    """Prove complete reconstruction without rewriting any live state file."""
    day, run = ready['day'], ready['run_id']
    require(base.get('schema') == BASE_SCHEMA and base.get('day') == day
            and base.get('base_key') == ready['base_key'] == manifest['base_key']
            and base.get('cipher_sha256') == manifest['base_cipher_sha256']
            == checkpoint.file_sha(cipher), 'BASE_BINDING')
    saved = json.loads((OUT / 'base-inventory.json').read_bytes())
    require(saved.get('schema') == checkpoint.DELTA_SCHEMA
            and saved.get('inventory') == manifest['base']
            and base['inventory_sha256'] == digest(manifest['base']), 'BASE_INVENTORY')
    require(int(ready['base_key'].split(day + '-')[1].split('-')[0]) < int(run), 'BASE_RUN')
    # inspect_delta verifies the exact replacement list and full path/mode/size
    # inventories. Authenticate and hash every replacement from the remote readback.
    with tarfile.open(archive, 'r:gz') as source:
        for name in manifest['changed']:
            item = manifest['target']['files'][name]
            payload = hashlib.sha256()
            with source.extractfile('changes/' + name) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    payload.update(block)
            require(payload.hexdigest() == item['sha256'], 'REPLACEMENT_BYTES')
    require(checkpoint.snapshot_state(root) == manifest['target'], 'TARGET_INVENTORY')
    owner = authority(root, day)
    name = 'daily-runtime/' + day + '/authority.json'
    require(manifest['base']['files'][name] == manifest['target']['files'][name]
            and owner['authority_sha256'] == base['authority_sha256']
            and owner['runtime_source_sha'] == base['runtime_source_sha'], 'AUTHORITY_CHANGED')
    state = json.loads((root / 'relay-state.json').read_bytes())
    gate = state.get('CRITICAL', {})
    require(result.get('OPERATIONAL_DATE_JST') == day and result.get('CRITICAL') == gate
            and state.get('OPERATIONAL_DATE_JST') == day and state.get('MORNING_COMPLETE') is True
            and gate.get('SCHEMA') == 'G11_PREDEADLINE_CRITICAL_CHECKPOINT_V1'
            and gate.get('CRITICAL_CHECKPOINT_STATUS') == 'PASS'
            and gate.get('OPERATIONAL_DATE') == day and gate.get('LAST_CRITICAL_RUN_ID') == run
            and gate.get('POST_DEADLINE_GENERATION') == 0
            and gate.get('LAST_TIMELY_SOURCE_COUNT', 0) > 0
            and gate.get('AUTHORITY_SHA256') == owner['authority_sha256']
            and gate.get('RUNTIME_SOURCE_SHA') == owner['runtime_source_sha'], 'TARGET_BINDING')
    return {'schema': PROOF_SCHEMA, 'day': day, 'run_id': run,
        'checkpoint_key': ready['checkpoint_key'], 'base_key': ready['base_key'],
        'base_cipher_sha256': base['cipher_sha256'], 'delta_key': ready['delta_key'],
        'delta_cipher_sha256': ready['cipher_sha256'],
        'target_inventory_sha256': digest(manifest['target']),
        'authority_sha256': owner['authority_sha256'], 'runtime_source_sha': owner['runtime_source_sha']}


def require_remote_base(base_key, *, query=None):
    """Reject a known missing dependency without downloading its large bytes."""
    repository = os.environ['GITHUB_REPOSITORY']
    require(repository == 'tatsuuut/-g11-official-data-relay', 'BASE_REPOSITORY')
    url = 'repos/' + repository + '/actions/caches?' + urlencode({
        'key': base_key, 'ref': 'refs/heads/main', 'per_page': 100})
    if query is None:
        response = subprocess.run(['gh', 'api', url], capture_output=True,
            text=True, timeout=12, check=True)
        value = json.loads(response.stdout)
    else:
        value = query(url)
    require(type(value) is dict and type(value.get('actions_caches')) is list
            and any(type(row) is dict and row.get('key') == base_key
                    and row.get('ref') == 'refs/heads/main'
                    for row in value['actions_caches']), 'REMOTE_BASE_MISSING')


def verify(key, readback_hit):
    proof_path = OUT / 'compact-publication-proof.json'
    proof_path.unlink(missing_ok=True)
    require(readback_hit == 'true', 'EXACT_READBACK')
    ready = checkpoint.compact_ready(key)
    require(ready is not None and ready['day'] >= '2026-11-27'
            and ready['day'] == datetime.now(JST).date().isoformat(),
            'CURRENT_READY')
    base = json.loads((OUT / 'authenticated-base.json').read_bytes())
    with tempfile.TemporaryDirectory(prefix='g11-compact-publication-') as folder:
        archive = Path(folder) / 'readback.tgz'
        decrypt(OUT / 'early-delta.tgz.gpg', archive)
        manifest = checkpoint.inspect_delta(archive, day=ready['day'], checkpoint_key=key)
        proof = verify_manifest(manifest, archive, root=Path('.relay-state'),
            cipher=Path('.relay-cache/state.tgz.gpg'), ready=ready, base=base,
            result=json.loads((OUT / 'result.json').read_bytes()))
    require_remote_base(ready['base_key'])
    checkpoint.atomic_json(proof_path, proof)
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write('ready=true\n')
    print('G11_COMPLETE_COMPACT_PUBLICATION_READBACK=PASS', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('restore', 'verify'))
    parser.add_argument('--day')
    parser.add_argument('--base-key', default='')
    parser.add_argument('--checkpoint-key')
    parser.add_argument('--readback-hit')
    args = parser.parse_args()
    if args.mode == 'restore':
        require(args.day is not None, 'RESTORE_DAY')
        restore(args.day, args.base_key)
    else:
        verify(args.checkpoint_key, args.readback_hit)


if __name__ == '__main__':
    main()
