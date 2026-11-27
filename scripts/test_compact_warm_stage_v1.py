"""Second immutable transport slot, retaining the first saved LOCK evidence."""
import copy
from contextlib import chdir
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import compact_runtime_recovery_v1 as recovery
import encrypt_runtime_state_v1 as checkpoint
import test_predeadline_latency_v1 as fixtures

DAY = '2026-11-27'


class WarmStageTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(fixtures, 'DAY', DAY))
        self.fixture = fixtures.CompleteDeltaTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.enterContext(chdir(self.fixture.root))
        self.first_lock = self.fixture.target/'store/snapshots/predeadline/first-lock.json'
        self.first_lock.parent.mkdir(parents=True); self.first_lock.write_bytes(b'first published immutable LOCK')
        self.first = checkpoint.delta_manifest(self.fixture.target, self.fixture.before,
            self.fixture.base_key, checkpoint.file_sha(self.fixture.cipher))
        self.warm = self.fixture.root/'warm'; shutil.copytree(self.fixture.target, self.warm)
        (self.warm/'store/snapshots/predeadline/second-lock.json').write_bytes(b'second immutable LOCK')
        state = json.loads((self.warm/'relay-state.json').read_bytes())
        state['CRITICAL']['LAST_TIMELY_SOURCE_COUNT'] = 2
        (self.warm/'relay-state.json').write_text(json.dumps(state))

    def manifest(self):
        return checkpoint.delta_manifest(self.warm, self.fixture.before, self.fixture.base_key,
            checkpoint.file_sha(self.fixture.cipher), capture_stage='warm', publication_attempt='1',
            first_target=self.first['target'], first_delta_cipher_sha256='d'*64)

    def test_second_slot_has_real_parent_run_and_distinct_key_and_preserves_first(self):
        first = copy.deepcopy(self.first)
        warm = self.manifest()
        self.assertEqual(warm['checkpoint_key'], first['checkpoint_key'])
        self.assertEqual(warm['delta_key'], first['delta_key']+'-warm')
        self.assertEqual(warm['publication_key'], f'g11-state-v4-published-{DAY}-43-1-checkpoint')
        self.assertEqual(warm['prior_target'], first['target'])
        self.assertEqual(self.first, first)
        self.assertEqual(checkpoint.compact_manifest_stage(warm), 'warm')

    def test_warm_creation_rejects_missing_prior_or_changed_first_published_lock(self):
        with self.assertRaises(ValueError):
            checkpoint.delta_manifest(self.warm, self.fixture.before, self.fixture.base_key,
                checkpoint.file_sha(self.fixture.cipher), capture_stage='warm')
        (self.warm/'store/snapshots/predeadline/first-lock.json').write_bytes(b'changed LOCK')
        with self.assertRaisesRegex(ValueError, 'PRIOR_LOCK_CHANGED'):
            self.manifest()

    def test_second_key_cannot_be_passed_as_first_or_from_another_parent(self):
        warm = self.manifest()
        for key in (self.first['delta_key'], warm['delta_key'].replace('-43-', '-44-')):
            invalid = copy.deepcopy(warm); invalid['delta_key'] = key
            with self.assertRaises(ValueError):
                checkpoint.compact_manifest_stage(invalid)
        with self.assertRaises(ValueError):
            checkpoint.validate_compact_keys('2026-11-26', 'g11-state-v4-critical-2026-11-26-43',
                                             'g11-state-v4-delta-2026-11-26-43-warm')

    def test_recovery_prefers_warm_over_same_run_critical_full_but_not_published_full(self):
        warm = self.manifest()['delta_key']
        rows = [{'key':warm, 'ref':'refs/heads/main', 'id':43, 'created_at':'2026-11-27T08:00:00Z'}]
        self.assertEqual(recovery.choose_delta_key(DAY, rows, self.first['checkpoint_key']), warm)
        self.assertEqual(recovery.choose_delta_key(DAY, rows,
            f'g11-state-v4-published-{DAY}-43-1-checkpoint'), '')

    def test_stage_ready_cannot_reuse_first_receipt_or_wrong_publication_key(self):
        warm = self.manifest(); cipher, ready = checkpoint.compact_paths('warm')
        cipher.parent.mkdir(); cipher.write_bytes(b'warm opaque cipher')
        value = {key:warm[key] for key in ('day','run_id','checkpoint_key','delta_key','base_key',
            'capture_stage','publication_attempt','publication_key')}
        value['cipher_sha256'] = checkpoint.file_sha(cipher)
        checkpoint.atomic_json(ready, value)
        with patch.dict(os.environ, {'GITHUB_RUN_ID':'43'}):
            self.assertEqual(checkpoint.compact_ready(warm['publication_key'], capture_stage='warm'), value)
            self.assertIsNone(checkpoint.compact_ready(warm['checkpoint_key'], capture_stage='warm'))
            self.assertIsNone(checkpoint.compact_ready(warm['publication_key']))

    def test_exact_warm_delta_replay_keeps_both_published_locks_and_physical_full(self):
        warm = self.manifest(); archive = self.fixture.root/'warm.tgz'
        checkpoint.write_delta(self.warm, warm, archive)
        cipher, _ = checkpoint.compact_paths('warm'); cipher.parent.mkdir(); cipher.write_bytes(b'warm cipher')
        recovery.CIPHER.parent.mkdir(); shutil.copyfile(self.fixture.cipher, recovery.CIPHER)
        shutil.copytree(self.fixture.base, recovery.ROOT)
        checkpoint.save_base_inventory(recovery.ROOT, recovery.OUT/'base-inventory.json')
        base_index = (recovery.OUT/'base-inventory.json').read_bytes()
        with patch.object(recovery, '_decrypt', side_effect=lambda destination, **_kwargs:shutil.copyfile(archive,destination)):
            recovery.inspect_recovery(DAY, warm['delta_key'], 'true', self.fixture.key)
            recovery.replay(DAY, self.fixture.base_key)
        self.assertEqual(checkpoint.snapshot_state(recovery.ROOT), warm['target'])
        self.assertEqual((recovery.OUT/'base-inventory.json').read_bytes(), base_index)
        self.assertEqual(recovery.CIPHER.read_bytes(), self.fixture.cipher.read_bytes())

    def test_authenticated_warm_cannot_remove_first_lock_by_changing_both_target_and_metadata(self):
        warm = self.manifest()
        (self.warm/'store/snapshots/predeadline/first-lock.json').unlink()
        warm['target'] = checkpoint.snapshot_state(self.warm)
        warm['changed'] = sorted(name for name,item in warm['target']['files'].items()
                                 if warm['base']['files'].get(name) != item)
        archive = self.fixture.root/'invalid-warm.tgz'; checkpoint.write_delta(self.warm,warm,archive)
        with self.assertRaisesRegex(ValueError, 'PRIOR_LOCK_CHANGED'):
            checkpoint.inspect_delta(archive, day=DAY, checkpoint_key=warm['checkpoint_key'])


if __name__ == '__main__':
    unittest.main()
