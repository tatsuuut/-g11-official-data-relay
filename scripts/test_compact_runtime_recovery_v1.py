"""Automatic exact-base recovery admission and complete-inventory regressions."""
import copy
from contextlib import chdir
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import compact_runtime_recovery_v1 as recovery
import critical_saved_publication_v1 as publication
import encrypt_runtime_state_v1 as checkpoint
import test_predeadline_latency_v1 as fixtures

DAY = '2026-11-27'


def cache(lane, run, created='2026-11-27T07:00:00Z', *, attempt=None, **extra):
    key = f'g11-state-v4-{lane}-{DAY}-{run}'
    if attempt is not None:
        key += f'-{attempt}-checkpoint'
    return {'id':int(run), 'key':key, 'ref':'refs/heads/main', 'created_at':created, **extra}


class SelectionTests(unittest.TestCase):
    def test_new_logical_target_beats_an_older_runs_later_full_transfer(self):
        full = cache('published', 42, '2026-11-27T07:05:00Z', attempt=1)
        delta = cache('delta', 43, '2026-11-27T07:00:00Z')
        self.assertEqual(recovery.choose_delta_key(DAY, [full, delta], full['key']), delta['key'])
        fresh = cache('critical', 44, '2026-11-27T06:59:00Z')
        self.assertEqual(publication.newest_checkpoint_key(DAY, [full, fresh]), fresh['key'])

    def test_same_run_full_and_warm_published_full_supersede_first_delta(self):
        delta = cache('delta', 43)
        for full in [cache('critical', 43), cache('published', 43, attempt=1), cache('critical', 44)]:
            self.assertEqual(recovery.choose_delta_key(DAY, [full, delta], full['key']), '')
        first, retry = cache('published', 43, attempt=1), cache('published', 43, attempt=2)
        self.assertEqual(publication.newest_checkpoint_key(DAY, [retry, first]), retry['key'])

    def test_wrong_branch_day_or_nonexact_delta_key_is_not_selected(self):
        rows = [cache('delta', 43, ref='refs/heads/dev'),
                cache('delta', 44, key='g11-state-v4-delta-2026-11-26-44'),
                cache('delta', 45, key=f'g11-state-v4-delta-{DAY}-45-bad')]
        self.assertEqual(recovery.choose_delta_key(DAY, rows, ''), '')

    def test_selector_exports_physical_full_and_newer_delta_separately(self):
        full, delta = cache('published', 42, attempt=1), cache('delta', 43)
        responses = [SimpleNamespace(stdout=json.dumps({'actions_caches':rows}))
                     for rows in ([], [full], [delta])]
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)/'output'
            with patch.dict(os.environ, {'GITHUB_REPOSITORY':'owner/relay', 'GITHUB_OUTPUT':str(output)}), \
                    patch('subprocess.run', side_effect=responses):
                publication.select_restore(DAY)
            self.assertEqual(output.read_text(), f'cache_key={full["key"]}\ndelta_key={delta["key"]}\n')


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(fixtures, 'DAY', DAY))
        self.fixture = fixtures.CompleteDeltaTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.enterContext(chdir(self.fixture.root))
        recovery.OUT.mkdir()
        recovery.CIPHER.parent.mkdir()
        shutil.copyfile(self.fixture.cipher, recovery.CIPHER)
        recovery.DELTA.write_bytes(b'exact remote encrypted delta fixture')
        shutil.copytree(self.fixture.base, recovery.ROOT)
        checkpoint.save_base_inventory(recovery.ROOT, recovery.OUT/'base-inventory.json')
        self.enterContext(patch.object(recovery, '_decrypt', side_effect=lambda target:shutil.copyfile(self.fixture.delta, target)))

    def inspect(self):
        return recovery.inspect_recovery(DAY, self.fixture.manifest['delta_key'], 'true', self.fixture.base_key)

    def test_exact_full_and_delta_reconstruct_every_file_and_keep_physical_base_index(self):
        saved_index = (recovery.OUT/'base-inventory.json').read_bytes()
        saved_cipher = recovery.CIPHER.read_bytes()
        proof = self.inspect()
        self.assertEqual(proof['base_key'], self.fixture.base_key)
        recovery.replay(DAY, self.fixture.base_key)
        self.assertEqual(checkpoint.snapshot_state(recovery.ROOT), checkpoint.snapshot_state(self.fixture.target))
        self.assertEqual((recovery.OUT/'base-inventory.json').read_bytes(), saved_index)
        self.assertEqual(recovery.CIPHER.read_bytes(), saved_cipher)

    def test_missing_exact_remote_hit_never_selects_an_older_full(self):
        before = checkpoint.snapshot_state(recovery.ROOT)
        with self.assertRaisesRegex(ValueError, 'EXACT_DELTA_READBACK'):
            recovery.inspect_recovery(DAY, self.fixture.manifest['delta_key'], 'false', self.fixture.base_key)
        self.assertFalse(recovery.PROOF.exists())
        self.assertEqual(checkpoint.snapshot_state(recovery.ROOT), before)

    def test_wrong_key_or_later_same_run_full_rejects_replay_admission(self):
        for day, key, full in [(DAY, f'g11-state-v4-delta-{DAY}-99', self.fixture.base_key),
                (DAY, self.fixture.manifest['delta_key'], self.fixture.key),
                ('2026-11-28', self.fixture.manifest['delta_key'], self.fixture.base_key)]:
            with self.subTest(day=day, key=key):
                with self.assertRaises(ValueError):
                    recovery.inspect_recovery(day, key, 'true', full)
                self.assertFalse(recovery.PROOF.exists())

    def test_changed_cipher_or_base_inventory_fails_before_any_delta_is_applied(self):
        self.inspect()
        original = recovery.DELTA.read_bytes()
        recovery.DELTA.write_bytes(b'corrupt remote delta')
        before = checkpoint.snapshot_state(recovery.ROOT)
        with self.assertRaisesRegex(ValueError, 'REPLAY_BINDING'):
            recovery.replay(DAY, self.fixture.base_key)
        recovery.DELTA.write_bytes(original)
        (recovery.ROOT/'store/old-lock').write_bytes(b'changed restored old LOCK')
        with self.assertRaisesRegex(ValueError, 'REPLAY_INVENTORY'):
            recovery.replay(DAY, self.fixture.base_key)
        self.assertNotEqual(checkpoint.snapshot_state(recovery.ROOT), before)
        self.assertFalse((recovery.ROOT/'store/new-lock').exists())

    def rewrite_target(self, mutate):
        mutate(self.fixture.target)
        manifest = copy.deepcopy(self.fixture.manifest)
        manifest['target'] = checkpoint.snapshot_state(self.fixture.target)
        manifest['changed'] = sorted(name for name, item in manifest['target']['files'].items()
                                     if manifest['base']['files'].get(name) != item)
        checkpoint.write_delta(self.fixture.target, manifest, self.fixture.delta)

    def test_target_cannot_replace_the_daily_authority_even_with_a_valid_manifest(self):
        def mutate(root):
            path = root/'daily-runtime'/DAY/'authority.json'
            value = json.loads(path.read_bytes()); value['runtime_source_sha'] = 'c'*40
            path.write_text(json.dumps(value))
        self.rewrite_target(mutate)
        with self.assertRaisesRegex(ValueError, 'AUTHORITY_CHANGED'):
            self.inspect()
        self.assertFalse((recovery.ROOT/'store/new-lock').exists())

    def test_target_wrong_runtime_rejects_before_atomic_reconstruction(self):
        def mutate(root):
            path = root/'relay-state.json'
            value = json.loads(path.read_bytes()); value['CRITICAL']['RUNTIME_SOURCE_SHA'] = 'c'*40
            path.write_text(json.dumps(value))
        self.rewrite_target(mutate)
        self.inspect()
        before = checkpoint.snapshot_state(recovery.ROOT)
        with self.assertRaisesRegex(ValueError, 'DAILY_AUTHORITY'):
            recovery.replay(DAY, self.fixture.base_key)
        self.assertEqual(checkpoint.snapshot_state(recovery.ROOT), before)

    def test_preexisting_prediction_snapshot_cannot_change_during_recovery(self):
        name = 'store/snapshots/predeadline/existing-locked-race.json'
        for root in (self.fixture.base, self.fixture.target, recovery.ROOT):
            path = root/name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"immutable":"original LOCK"}')
        base = checkpoint.snapshot_state(self.fixture.base)
        self.fixture.manifest['base'] = base
        checkpoint.save_base_inventory(recovery.ROOT, recovery.OUT/'base-inventory.json')
        self.rewrite_target(lambda root:(root/name).write_text('{"immutable":"changed LOCK"}'))
        self.inspect()
        before = checkpoint.snapshot_state(recovery.ROOT)
        with self.assertRaisesRegex(ValueError, 'PROTECTED_BASE_CHANGED'):
            recovery.replay(DAY, self.fixture.base_key)
        self.assertEqual(checkpoint.snapshot_state(recovery.ROOT), before)

    def test_actual_frozen_predeadline_seal_appends_binding_without_replacing_old_binding(self):
        source = Path(os.environ.get('G11_RETRY_SOURCE_ROOT',
            Path(__file__).resolve().parents[1]/'.g11-private'))
        path = source/'g11/relay/daily_runtime_authority_v1.py'
        if not path.is_file():
            self.skipTest('actual frozen source is required in the runtime contract gate')
        ns = {'__name__':'actual_frozen_daily_authority'}
        exec(compile(path.read_text(), str(path), 'exec'), ns)
        owner = {'authority_sha256':'a'*64, 'runtime_source_sha':'b'*40}
        locks = {'old-lock':checkpoint.file_sha(recovery.ROOT/'store/old-lock')}
        with patch.dict(ns, {'load':lambda *_:owner, 'lock_inventory':lambda *_:dict(locks),
                           '_verify_predeadline_append':lambda *_:None}):
            first = ns['seal'](recovery.ROOT, DAY, 'PREDEADLINE')
            binding = recovery.ROOT/'daily-runtime'/DAY/'bindings/PREDEADLINE'/(first['binding_sha256']+'.json')
            saved = binding.read_bytes()
            base = checkpoint.snapshot_state(recovery.ROOT)
            (recovery.ROOT/'store/new-native-lock').write_bytes(b'new immutable native LOCK')
            locks['new-native-lock'] = checkpoint.file_sha(recovery.ROOT/'store/new-native-lock')
            second = ns['seal'](recovery.ROOT, DAY, 'PREDEADLINE')
        self.assertNotEqual(first['binding_sha256'], second['binding_sha256'])
        self.assertEqual(binding.read_bytes(), saved)
        self.assertEqual(len(list(binding.parent.glob('*.json'))), 2)
        # The recovery guard permits the native appended binding/new LOCK while
        # keeping every previously bound byte unchanged.
        recovery._protected_base({'day':DAY, 'base':base,
                                   'target':checkpoint.snapshot_state(recovery.ROOT)}, owner)


if __name__ == '__main__':
    unittest.main()
