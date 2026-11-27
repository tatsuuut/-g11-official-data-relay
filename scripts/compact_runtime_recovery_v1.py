"""Recover a saved compact target from its exact authenticated full base.

The physical full ciphertext and its inventory remain the delta base after
replay. Later compact saves therefore include every change since that full.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile

import encrypt_runtime_state_v1 as checkpoint

EFFECTIVE_FROM = '2026-11-27'
SCHEMA = 'G11_COMPACT_RUNTIME_RECOVERY_V1'
OUT = Path('.relay-output')
ROOT = Path('.relay-state')
CIPHER = Path('.relay-cache/state.tgz.gpg')
DELTA = OUT / 'early-delta.tgz.gpg'
PROOF = OUT / 'compact-recovery.json'


def require(ok, reason):
    if not ok:
        raise ValueError('COMPACT_RECOVERY_' + reason)


def digest(value):
    import hashlib
    return hashlib.sha256(checkpoint.canonical(value)).hexdigest()


def checkpoint_rank(key):
    """Logical target first; an older run's late full cannot hide newer LOCKs."""
    match = re.fullmatch(r'g11-state-v4-(delta|critical|published)-(20\d{2}-\d{2}-\d{2})-'
                         r'([0-9]+)(?:(-warm)|-([0-9]+)-checkpoint)?', key)
    require(match is not None, 'KEY')
    lane, _day, run, warm, attempt = match.groups()
    require((lane == 'published' or attempt is None) and (not warm or lane == 'delta'), 'KEY_STAGE')
    return int(run), 2 if warm else {'delta': 0, 'critical': 1, 'published': 3}[lane], int(attempt or 0)


def cache_order(row):
    return (checkpoint_rank(row['key']),
            datetime.fromisoformat(row['created_at'].replace('Z', '+00:00')), row['id'])


def choose_delta_key(day, caches, full_key):
    datetime.strptime(day, '%Y-%m-%d')
    pattern = re.compile(r'g11-state-v4-delta-' + re.escape(day) + r'-[0-9]+(?:-warm)?')
    candidates = [row for row in caches if isinstance(row, dict)
                  and row.get('ref') == 'refs/heads/main'
                  and pattern.fullmatch(str(row.get('key', '')))]
    if not candidates:
        return ''
    selected = max(candidates, key=cache_order)['key']
    return selected if not full_key or checkpoint_rank(selected) > checkpoint_rank(full_key) else ''


def _secret():
    token = os.environ['G11_PRIVATE_REPO_TOKEN']
    value = (token + '\n').encode()
    require(bool(token) and 0 < len(value) <= 4096 and value.count(b'\n') == 1, 'SECRET_INPUT')
    return value


def _decrypt(destination, *, cipher=None):
    cipher = cipher or DELTA
    require(cipher.is_file() and not cipher.is_symlink(), 'DELTA_MISSING')
    with checkpoint.secret_pipe(_secret()) as fd:
        result = subprocess.run(['gpg', '--batch', '--yes', '--pinentry-mode', 'loopback',
            '--passphrase-fd', str(fd), '--decrypt', '--output', str(destination), str(cipher)],
            pass_fds=(fd,), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False)
    require(result.returncode == 0, 'AUTHENTICATION')


def _metadata(archive, manifest):
    # Every new run changes relay-state.json. Read its exact replacement before
    # touching the extracted base, so a wrong target fails without partial use.
    require('relay-state.json' in manifest['changed'], 'METADATA_REPLACEMENT')
    item = manifest['target']['files']['relay-state.json']
    require(item['size'] <= 1024 * 1024, 'METADATA_SIZE')
    with tarfile.open(archive, 'r:gz') as source:
        raw = source.extractfile('changes/relay-state.json').read()
    import hashlib
    require(hashlib.sha256(raw).hexdigest() == item['sha256'], 'METADATA_BYTES')
    state = json.loads(raw)
    gate = state.get('CRITICAL', {})
    day, run = manifest['day'], manifest['run_id']
    require(state.get('OPERATIONAL_DATE_JST') == day and state.get('MORNING_COMPLETE') is True
            and gate.get('SCHEMA') == 'G11_PREDEADLINE_CRITICAL_CHECKPOINT_V1'
            and gate.get('CRITICAL_CHECKPOINT_STATUS') == 'PASS'
            and gate.get('OPERATIONAL_DATE') == day and gate.get('LAST_CRITICAL_RUN_ID') == run
            and gate.get('POST_DEADLINE_GENERATION') == 0
            and type(gate.get('LAST_TIMELY_SOURCE_COUNT')) is int and gate['LAST_TIMELY_SOURCE_COUNT'] > 0
            and re.fullmatch('[0-9a-f]{64}', str(gate.get('AUTHORITY_SHA256', '')))
            and re.fullmatch('[0-9a-f]{40}', str(gate.get('RUNTIME_SOURCE_SHA', ''))), 'TARGET_STATE')
    return state


def _inspect(archive, day, delta_key):
    require(re.fullmatch(r'g11-state-v4-delta-' + re.escape(day) + r'-[0-9]+(?:-warm)?', delta_key), 'DELTA_KEY')
    key = delta_key.removesuffix('-warm').replace('g11-state-v4-delta-', 'g11-state-v4-critical-', 1)
    manifest = checkpoint.inspect_delta(archive, day=day, checkpoint_key=key)
    require(isinstance(manifest.get('run_id'), str) and re.fullmatch('[0-9]+', manifest['run_id'])
            and manifest['delta_key'] == delta_key
            and checkpoint_rank(manifest['base_key'])[0] < int(manifest['run_id']), 'BASE_RUN')
    name = 'daily-runtime/' + day + '/authority.json'
    require(name in manifest['base']['files']
            and manifest['base']['files'][name] == manifest['target']['files'].get(name), 'AUTHORITY_CHANGED')
    state = _metadata(archive, manifest)
    return manifest, state


def _delta_cipher(delta_key):
    return checkpoint.compact_paths('warm')[0] if delta_key.endswith('-warm') else DELTA


def _decrypt_selected(archive, delta_key):
    if delta_key.endswith('-warm'):
        _decrypt(archive, cipher=_delta_cipher(delta_key))
    else:
        _decrypt(archive)


def inspect_recovery(day, delta_key, cache_hit, full_key):
    """Authenticate the exact remote delta before selecting its physical base."""
    PROOF.unlink(missing_ok=True)
    datetime.strptime(day, '%Y-%m-%d')
    require(day >= EFFECTIVE_FROM and cache_hit == 'true', 'EXACT_DELTA_READBACK')
    if full_key:
        require(checkpoint.checkpoint_day(full_key) == day
                and checkpoint_rank(delta_key) > checkpoint_rank(full_key), 'STALE_DELTA')
    with tempfile.TemporaryDirectory(prefix='g11-compact-recovery-', dir=os.environ.get('RUNNER_TEMP')) as folder:
        archive = Path(folder) / 'delta.tgz'
        _decrypt_selected(archive, delta_key)
        manifest, state = _inspect(archive, day, delta_key)
    gate = state['CRITICAL']
    value = {'schema': SCHEMA, 'day': day, 'run_id': manifest['run_id'],
        'checkpoint_key': manifest['checkpoint_key'], 'delta_key': delta_key,
        'base_key': manifest['base_key'], 'base_cipher_sha256': manifest['base_cipher_sha256'],
        'delta_cipher_sha256': checkpoint.file_sha(_delta_cipher(delta_key)), 'manifest_sha256': digest(manifest),
        'base_inventory_sha256': digest(manifest['base']),
        'target_inventory_sha256': digest(manifest['target']),
        'authority_sha256': gate['AUTHORITY_SHA256'], 'runtime_source_sha': gate['RUNTIME_SOURCE_SHA']}
    if checkpoint.compact_manifest_stage(manifest) == 'warm':
        value.update(capture_stage='warm', publication_key=manifest['publication_key'])
    checkpoint.atomic_json(PROOF, value)
    if output := os.environ.get('GITHUB_OUTPUT'):
        with open(output, 'a') as handle:
            handle.write('base_key=' + value['base_key'] + '\ncheckpoint_key=' + value['checkpoint_key'] + '\n')
    print('G11_COMPACT_RECOVERY_MANIFEST=AUTHENTICATED_EXACT_BASE', flush=True)
    return value


def _protected_base(manifest, owner):
    day = manifest['day']
    names = {name for name in manifest['base']['files'] if
             name.startswith(('store/snapshots/morning/', 'store/snapshots/predeadline/',
                              'daily-runtime/' + day + '/bindings/'))}
    names.add('daily-runtime/' + day + '/authority.json')
    if owner.get('source_archive'):
        names.add('daily-runtime/' + day + '/' + owner['source_archive'])
    names.update('store/' + name for name in owner.get('model_files', {}))
    # Existing native bindings enumerate non-snapshot WILD/TRUE prediction LOCKs.
    for name in tuple(names):
        if name.startswith('daily-runtime/' + day + '/bindings/'):
            binding = json.loads((ROOT / name).read_bytes())
            names.update('store/' + path for path in binding.get('lock_files', {}))
    require(all(name in manifest['base']['files']
                and manifest['target']['files'].get(name) == manifest['base']['files'][name]
                for name in names), 'PROTECTED_BASE_CHANGED')


def replay(day, base_key):
    """Apply one cumulative delta; retain the real full key/cipher/base index."""
    proof = json.loads(PROOF.read_bytes())
    require(proof.get('schema') == SCHEMA and proof.get('day') == day
            and proof.get('base_key') == base_key and checkpoint.checkpoint_day(base_key) == day
            and checkpoint.file_sha(_delta_cipher(proof['delta_key'])) == proof.get('delta_cipher_sha256')
            and checkpoint.file_sha(CIPHER) == proof.get('base_cipher_sha256'), 'REPLAY_BINDING')
    saved = json.loads((OUT / 'base-inventory.json').read_bytes())
    require(saved.get('schema') == checkpoint.DELTA_SCHEMA, 'BASE_INDEX')
    with tempfile.TemporaryDirectory(prefix='g11-compact-replay-', dir=os.environ.get('RUNNER_TEMP')) as folder:
        archive = Path(folder) / 'delta.tgz'
        _decrypt_selected(archive, proof['delta_key'])
        manifest, state = _inspect(archive, day, proof['delta_key'])
        require(manifest['base_key'] == base_key and manifest['run_id'] == proof['run_id']
                and manifest['checkpoint_key'] == proof['checkpoint_key']
                and manifest['base_cipher_sha256'] == proof['base_cipher_sha256']
                and digest(manifest) == proof['manifest_sha256']
                and digest(manifest['base']) == proof['base_inventory_sha256']
                and digest(manifest['target']) == proof['target_inventory_sha256']
                and saved['inventory'] == manifest['base']
                and checkpoint.snapshot_state(ROOT) == manifest['base'], 'REPLAY_INVENTORY')
        owner = json.loads((ROOT / 'daily-runtime' / day / 'authority.json').read_bytes())
        require(owner.get('immutable') is True and owner.get('operational_date_jst') == day
                and owner.get('authority_sha256') == state['CRITICAL']['AUTHORITY_SHA256'] == proof['authority_sha256']
                and owner.get('runtime_source_sha') == state['CRITICAL']['RUNTIME_SOURCE_SHA'] == proof['runtime_source_sha'],
                'DAILY_AUTHORITY')
        _protected_base(manifest, owner)
        checkpoint.apply_delta(archive, ROOT, day=day, checkpoint_key=proof['checkpoint_key'], base_cipher=CIPHER)
    # apply_delta commits only after an exact complete target inventory check.
    # Do not replace the authenticated full base index with the replayed target.
    require(json.loads((ROOT / 'relay-state.json').read_bytes()) == state, 'TARGET_READBACK')
    print('G11_COMPACT_RUNTIME_RECOVERY=PASS PHYSICAL_BASE_RETAINED', flush=True)
    return proof


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('inspect-recovery', 'replay'))
    parser.add_argument('--day', required=True)
    parser.add_argument('--delta-key', default='')
    parser.add_argument('--cache-hit', default='')
    parser.add_argument('--full-key', default='')
    parser.add_argument('--base-key', default='')
    args = parser.parse_args()
    if args.mode == 'inspect-recovery':
        inspect_recovery(args.day, args.delta_key, args.cache_hit, args.full_key)
    else:
        replay(args.day, args.base_key)


if __name__ == '__main__':
    main()
