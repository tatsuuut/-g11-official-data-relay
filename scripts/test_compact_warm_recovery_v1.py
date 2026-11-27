"""Recover both public capture stages after cancellation before the full save.

Reuse the mandatory foreground-GPG fixture from the first-stage cancellation
suite. All cache objects, source bytes, models and LOCKs are synthetic local
files; no live race request or predictor runs here.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
from datetime import datetime
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import textwrap
import unittest
from unittest.mock import patch

import compact_publication_checkpoint_v1 as publication
import compact_runtime_recovery_v1 as recovery
import encrypt_runtime_state_v1 as checkpoint
import test_compact_cancel_recovery_v1 as first


DAY = first.DAY
BASE_KEY = first.BASE_KEY
CHECKPOINT_KEY = first.CHECKPOINT_KEY
FIRST_DELTA_KEY = first.DELTA_KEY
WARM_DELTA_KEY = FIRST_DELTA_KEY + '-warm'
PUBLICATION_KEY = 'g11-state-v4-published-' + DAY + '-43-1-checkpoint'


class WarmCacheSelectionTests(unittest.TestCase):
    cache = staticmethod(first.CancelRecoveryTests.cache)

    def test_late_first_stage_objects_cannot_overwrite_the_published_warm_target(self):
        caches = [self.cache(WARM_DELTA_KEY, 2, '12:02:00'),
            self.cache(FIRST_DELTA_KEY, 9, '12:09:00'),
            self.cache(CHECKPOINT_KEY, 10, '12:10:00'),
            self.cache(BASE_KEY, 11, '12:11:00')]
        for full_key in (BASE_KEY, CHECKPOINT_KEY):
            with self.subTest(full_key=full_key):
                self.assertEqual(recovery.choose_delta_key(DAY, caches, full_key), WARM_DELTA_KEY)

    def test_later_complete_publication_supersedes_same_run_warm_delta(self):
        caches = [self.cache(WARM_DELTA_KEY, 2, '12:02:00'),
            self.cache(PUBLICATION_KEY, 3, '12:03:00')]
        self.assertEqual(recovery.choose_delta_key(DAY, caches, PUBLICATION_KEY), '')

    def test_gori_protocol_uses_the_real_parent_with_an_exact_distinct_stage_key(self):
        self.assertEqual(checkpoint.validate_compact_keys(DAY, CHECKPOINT_KEY, FIRST_DELTA_KEY), 'critical')
        self.assertEqual(checkpoint.validate_compact_keys(DAY, CHECKPOINT_KEY, WARM_DELTA_KEY), 'warm')
        for parent, delta in ((CHECKPOINT_KEY + '-warm', WARM_DELTA_KEY),
                (CHECKPOINT_KEY, WARM_DELTA_KEY.replace('-43-', '-44-')),
                (CHECKPOINT_KEY, WARM_DELTA_KEY.replace(DAY, '2026-11-26')),
                (CHECKPOINT_KEY, WARM_DELTA_KEY + '-prefix-fallback')):
            with self.subTest(parent=parent, delta=delta), self.assertRaises(ValueError):
                checkpoint.validate_compact_keys(DAY, parent, delta)

    def test_actual_gori_child_rejects_mixed_first_and_warm_stage_requests(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / '.github/workflows/g11-wild4-shadow-lock-only.yml').read_text()
        start = source.index('          live_checkpoint=')
        end = source.index("          with open(os.environ['GITHUB_OUTPUT']", start)
        code = textwrap.dedent(source[start:end])
        for key, delta, requested_stage, valid in (
                (CHECKPOINT_KEY, FIRST_DELTA_KEY, 'critical', True),
                (CHECKPOINT_KEY, WARM_DELTA_KEY, 'warm', True),
                (CHECKPOINT_KEY, FIRST_DELTA_KEY, 'warm', False),
                (CHECKPOINT_KEY, WARM_DELTA_KEY, 'critical', False),
                (CHECKPOINT_KEY + '-warm', WARM_DELTA_KEY, 'warm', False),
                (CHECKPOINT_KEY, WARM_DELTA_KEY.replace('-43-', '-44-'), 'warm', False)):
            env = {'PREDEADLINE_CHECKPOINT_KEY': key, 'PREDEADLINE_DELTA_KEY': delta,
                'CAPTURE_STAGE': requested_stage, 'REQUESTED_MODE': 'wild4',
                'PREWARM_BASE_KEY': '', 'G11_PUBLIC_RUNTIME_HELPERS': str(root / 'scripts')}
            scope = {'os': os, 're': re, 'day': DAY, 'checkpoint': '', 'readback': ''}
            with self.subTest(delta=delta, stage=requested_stage), patch.dict(os.environ, env):
                if valid:
                    exec(compile(code, '<actual-gori-child-stage-binding>', 'exec'), scope)
                else:
                    with self.assertRaises((SystemExit, ValueError)):
                        exec(compile(code, '<actual-gori-child-stage-binding>', 'exec'), scope)


class WarmCancelRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        first.CancelRecoveryTests.setUpClass.__func__(cls)

    encrypt_delta = first.CancelRecoveryTests.encrypt_delta
    workspace = first.CancelRecoveryTests.workspace
    cache = staticmethod(first.CancelRecoveryTests.cache)
    restore_base = first.CancelRecoveryTests.restore_base
    rewrite_manifest = first.CancelRecoveryTests.rewrite_manifest

    def setUp(self):
        first.CancelRecoveryTests.setUp(self)
        # Native saved prediction snapshots created in the first pass become
        # immutable before the warm pass. Keep one outside the physical base.
        name = self.target / 'store/snapshots/predeadline/first-stage-lock.json'
        name.parent.mkdir(parents=True, exist_ok=True)
        name.write_bytes((self.target / 'store/new-formal-lock.json').read_bytes())
        self.manifest = checkpoint.delta_manifest(self.target, self.base_inventory,
            BASE_KEY, checkpoint.file_sha(self.base_cipher))
        checkpoint.write_delta(self.target, self.manifest, self.delta_archive)
        self.encrypt_delta()
        self.expected = checkpoint.snapshot_state(self.target)

    def make_warm_target(self):
        self.first_inventory = checkpoint.snapshot_state(self.target)
        self.first_lock_bytes = (self.target / 'store/new-formal-lock.json').read_bytes()
        self.warm = self.root / 'warm-target'
        shutil.copytree(self.target, self.warm)
        state = json.loads((self.warm / 'relay-state.json').read_bytes())
        state['CRITICAL']['LAST_TIMELY_SOURCE_COUNT'] = 4
        (self.warm / 'relay-state.json').write_bytes(checkpoint.canonical(state))
        (self.warm / 'store/warm-formal-lock.json').write_bytes(b'{"saved":"warm public LOCK"}')
        (self.warm / 'store/snapshots/predeadline/warm-stage-lock.json').write_bytes(
            b'{"saved":"warm public LOCK"}')
        (self.warm / 'store/warm-original-beforeinfo.html').write_bytes(b'new original warm input')
        self.warm_inventory = checkpoint.snapshot_state(self.warm)
        self.warm_manifest = checkpoint.delta_manifest(self.warm, self.base_inventory,
            BASE_KEY, checkpoint.file_sha(self.base_cipher), capture_stage='warm', publication_attempt='1',
            first_target=self.first_inventory, first_delta_cipher_sha256=checkpoint.file_sha(self.delta_cipher))
        self.warm_archive = self.root / 'warm-delta.tgz'
        checkpoint.write_delta(self.warm, self.warm_manifest, self.warm_archive)
        self.warm_cipher = self.root / 'warm-delta.tgz.gpg'
        self.encrypt_warm()

    def encrypt_warm(self):
        self.warm_cipher.unlink(missing_ok=True)
        with patch.dict(os.environ, self.env), checkpoint.secret_pipe(first.SECRET) as fd:
            checkpoint.encrypt_gzip_file(self.warm_archive, self.warm_cipher, fd)

    @contextmanager
    def warm_workspace(self, *, delta_cipher=None):
        # Cache versions include the saved path. The actual warm cache must be
        # restored to its own warm-delta path, rather than relabelled as first.
        with self.workspace(delta_cipher=delta_cipher or self.warm_cipher) as work:
            (work / '.relay-output/early-delta.tgz.gpg').rename(
                work / '.relay-output/warm-delta.tgz.gpg')
            yield work

    def rewrite_warm_manifest(self, mutate):
        with tarfile.open(self.warm_archive, 'r:gz') as source:
            items = [(member, source.extractfile(member).read()) for member in source.getmembers()]
        manifest = json.loads(items[0][1]); mutate(manifest)
        items[0] = items[0][0], checkpoint.canonical(manifest)
        with tarfile.open(self.warm_archive, 'w:gz') as output:
            for member, raw in items:
                member.size = len(raw)
                output.addfile(member, io.BytesIO(raw))
        self.encrypt_warm()

    def inspect_warm(self, *, full_key=BASE_KEY, delta_key=WARM_DELTA_KEY):
        return recovery.inspect_recovery(DAY, delta_key, 'true', full_key)

    def assert_both_locks(self, restored):
        self.assertEqual((restored / 'store/new-formal-lock.json').read_bytes(), self.first_lock_bytes)
        self.assertEqual((restored / 'store/warm-formal-lock.json').read_bytes(),
            b'{"saved":"warm public LOCK"}')
        self.assertEqual((restored / 'store/old-formal-lock.json').read_bytes(),
            (self.base / 'store/old-formal-lock.json').read_bytes())

    def test_warm_manifest_binds_actual_parent_and_distinct_publication_stage(self):
        self.make_warm_target()
        manifest = checkpoint.inspect_delta(self.warm_archive, day=DAY, checkpoint_key=CHECKPOINT_KEY)
        self.assertEqual(manifest['checkpoint_key'], CHECKPOINT_KEY)
        self.assertEqual(manifest['delta_key'], WARM_DELTA_KEY)
        self.assertEqual(manifest['capture_stage'], 'warm')
        self.assertEqual(manifest['publication_attempt'], '1')
        self.assertEqual(manifest['publication_key'], PUBLICATION_KEY)
        self.assertEqual(manifest['prior_delta_key'], FIRST_DELTA_KEY)
        self.assertEqual(manifest['prior_delta_cipher_sha256'], checkpoint.file_sha(self.delta_cipher))
        self.assertEqual(manifest['prior_protected']['store/snapshots/predeadline/first-stage-lock.json'],
            self.first_inventory['files']['store/snapshots/predeadline/first-stage-lock.json'])
        self.assertIn('store/new-formal-lock.json', manifest['changed'])
        self.assertIn('store/warm-formal-lock.json', manifest['changed'])

    def test_cancel_after_warm_publication_recovers_both_stage_locks_without_full_backup(self):
        self.make_warm_target()
        shutil.rmtree(self.target); shutil.rmtree(self.warm)
        with self.warm_workspace() as work:
            self.inspect_warm(full_key=CHECKPOINT_KEY)
            self.restore_base(work)
            recovery.replay(DAY, BASE_KEY)
            restored = work / '.relay-state'
            self.assertEqual(checkpoint.snapshot_state(restored), self.warm_inventory)
            self.assert_both_locks(restored)
            self.assertEqual(json.loads((work / '.relay-output/base-inventory.json').read_bytes())['inventory'],
                self.base_inventory)
            self.assertEqual(checkpoint.file_sha(work / '.relay-cache/state.tgz.gpg'),
                checkpoint.file_sha(self.base_cipher))

    def test_gori_exact_warm_stage_input_reconstructs_the_same_saved_target(self):
        self.make_warm_target()
        with self.warm_workspace() as work:
            self.restore_base(work)
            archive = work / 'runner-temp/gori-warm-input.tgz'
            publication.decrypt(work / '.relay-output/warm-delta.tgz.gpg', archive)
            manifest = checkpoint.inspect_delta(archive, day=DAY, checkpoint_key=CHECKPOINT_KEY)
            self.assertEqual(manifest['delta_key'], WARM_DELTA_KEY)
            self.assertEqual(checkpoint.validate_compact_keys(DAY, CHECKPOINT_KEY,
                manifest['delta_key']), 'warm')
            checkpoint.apply_delta(archive, work / '.relay-state', day=DAY,
                checkpoint_key=CHECKPOINT_KEY, base_cipher=work / '.relay-cache/state.tgz.gpg')
            self.assertEqual(checkpoint.snapshot_state(work / '.relay-state'), self.warm_inventory)
            self.assert_both_locks(work / '.relay-state')

    def test_first_cipher_read_back_under_warm_key_is_rejected_before_state_use(self):
        self.make_warm_target()
        with self.warm_workspace(delta_cipher=self.delta_cipher) as work:
            with self.assertRaises((RuntimeError, ValueError)):
                self.inspect_warm()
            self.assertFalse((work / '.relay-state').exists())

    def test_warm_cipher_read_back_under_first_key_is_rejected_before_state_use(self):
        self.make_warm_target()
        with self.workspace(delta_cipher=self.warm_cipher) as work:
            with self.assertRaises((RuntimeError, ValueError)):
                self.inspect_warm(delta_key=FIRST_DELTA_KEY)
            self.assertFalse((work / '.relay-state').exists())

    def test_wrong_stage_parent_or_publication_identity_cannot_recover(self):
        self.make_warm_target()
        original = self.warm_archive.read_bytes()
        for kind in ('stage', 'parent', 'publication-run', 'publication-attempt', 'missing-stage'):
            with self.subTest(kind=kind):
                self.warm_archive.write_bytes(original)
                def mutate(value):
                    if kind == 'stage': value['capture_stage'] = 'critical'
                    elif kind == 'parent': value['checkpoint_key'] = CHECKPOINT_KEY + '-warm'
                    elif kind == 'publication-run': value['publication_key'] = PUBLICATION_KEY.replace('-43-', '-44-')
                    elif kind == 'publication-attempt': value['publication_attempt'] = '0'
                    else: value.pop('capture_stage')
                self.rewrite_warm_manifest(mutate)
                with self.warm_workspace() as work:
                    with self.assertRaises((RuntimeError, ValueError)):
                        self.inspect_warm()
                    self.assertFalse((work / '.relay-state').exists())

    def test_warm_recovery_rejects_authenticated_change_to_an_existing_protected_lock(self):
        self.make_warm_target()
        (self.warm / 'store/old-formal-lock.json').write_bytes(b'changed saved old LOCK')
        manifest = copy.deepcopy(self.warm_manifest)
        manifest['target'] = checkpoint.snapshot_state(self.warm)
        manifest['changed'] = sorted(name for name, item in manifest['target']['files'].items()
            if manifest['base']['files'].get(name) != item)
        checkpoint.write_delta(self.warm, manifest, self.warm_archive); self.encrypt_warm()
        with self.warm_workspace() as work:
            try:
                self.inspect_warm()
            except (RuntimeError, ValueError):
                self.assertFalse((work / '.relay-state').exists())
                return
            self.restore_base(work)
            before = checkpoint.snapshot_state(work / '.relay-state')
            with self.assertRaises((RuntimeError, ValueError)):
                recovery.replay(DAY, BASE_KEY)
            self.assertEqual(checkpoint.snapshot_state(work / '.relay-state'), before)

    def test_warm_manifest_cannot_rewrite_a_lock_created_by_the_first_public_stage(self):
        self.make_warm_target()
        path = self.warm / 'store/snapshots/predeadline/first-stage-lock.json'
        path.write_bytes(b'changed first-stage saved prediction')
        with self.assertRaisesRegex(ValueError, 'PRIOR_LOCK_CHANGED'):
            checkpoint.delta_manifest(self.warm, self.base_inventory,
                BASE_KEY, checkpoint.file_sha(self.base_cipher), capture_stage='warm', publication_attempt='1',
                first_target=self.first_inventory, first_delta_cipher_sha256=checkpoint.file_sha(self.delta_cipher))
        # An authenticated but inconsistent saved payload must also be rejected
        # on cold recovery before it can expose a changed first-stage LOCK.
        manifest = copy.deepcopy(self.warm_manifest)
        manifest['target'] = checkpoint.snapshot_state(self.warm)
        manifest['changed'] = sorted(name for name, item in manifest['target']['files'].items()
            if manifest['base']['files'].get(name) != item)
        checkpoint.write_delta(self.warm, manifest, self.warm_archive); self.encrypt_warm()
        with self.warm_workspace() as work:
            with self.assertRaises((RuntimeError, ValueError)):
                self.inspect_warm()
            self.assertFalse((work / '.relay-state').exists())

    def test_second_publication_proof_survives_cancel_while_first_cache_bytes_stay_immutable(self):
        self.make_warm_target()
        first_durable = self.root / 'first-published-cache.gpg'
        warm_durable = self.root / 'warm-published-cache.gpg'
        with self.workspace() as producer, patch.dict(os.environ, {'GITHUB_RUN_ID': '43'}):
            publication.restore(DAY, BASE_KEY)
            result_path = producer / '.relay-output/result.json'
            actual_subprocess = publication.subprocess.run
            def metadata_only(command, **kwargs):
                if command[:2] == ['gh', 'api']:
                    return subprocess.CompletedProcess(command, 0,
                        stdout=json.dumps({'actions_caches': [self.cache(BASE_KEY, 1, '12:00:00')]}), stderr='')
                return actual_subprocess(command, **kwargs)
            with patch.object(publication.subprocess, 'run', side_effect=metadata_only), \
                    patch.object(publication, 'datetime', wraps=datetime) as clock:
                clock.now.return_value = datetime.fromisoformat(DAY + 'T12:02:00+09:00')
                for stage, target, key, durable in (
                    ('critical', self.target, CHECKPOINT_KEY, first_durable),
                    ('warm', self.warm, PUBLICATION_KEY, warm_durable)):
                    shutil.rmtree(producer / '.relay-state')
                    shutil.copytree(target, producer / '.relay-state')
                    state = json.loads((target / 'relay-state.json').read_bytes())
                    result_path.write_bytes(checkpoint.canonical({'OPERATIONAL_DATE_JST': DAY,
                        'CRITICAL': state['CRITICAL']}))
                    checkpoint.prepare_compact_checkpoint(producer / '.relay-state',
                        producer / '.relay-cache/state.tgz.gpg', BASE_KEY,
                        secret=first.SECRET, capture_stage=stage)
                    local = producer / '.relay-output' / (
                        'warm-delta.tgz.gpg' if stage == 'warm' else 'early-delta.tgz.gpg')
                    shutil.copyfile(local, durable)
                    local.unlink(); shutil.copyfile(durable, local)
                    publication.verify(key, 'true', capture_stage=stage)
                proof = json.loads((producer / '.relay-output/warm-publication-proof.json').read_bytes())
                self.assertEqual(proof['delta_key'], WARM_DELTA_KEY)
                self.assertEqual(proof['target_inventory_sha256'], publication.digest(self.warm_inventory))
            self.assertNotEqual(checkpoint.file_sha(first_durable), checkpoint.file_sha(warm_durable))
            self.assertEqual(checkpoint.file_sha(producer / '.relay-cache/state.tgz.gpg'),
                checkpoint.file_sha(self.base_cipher))
        shutil.rmtree(self.target); shutil.rmtree(self.warm)
        with self.warm_workspace(delta_cipher=warm_durable) as successor:
            self.inspect_warm(); self.restore_base(successor); recovery.replay(DAY, BASE_KEY)
            self.assertEqual(checkpoint.snapshot_state(successor / '.relay-state'), self.warm_inventory)
            self.assert_both_locks(successor / '.relay-state')


if __name__ == '__main__':
    unittest.main()
