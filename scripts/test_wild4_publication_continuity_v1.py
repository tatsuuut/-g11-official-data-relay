from copy import deepcopy
import json
from pathlib import Path
import os
import tempfile
import unittest

import wild4_publication_continuity_v1 as c


@unittest.skipUnless(os.environ.get('WILD4_WORKFLOW_PATH'), 'Pinned workflow required')
class RecoveryPersistenceTests(unittest.TestCase):
    def test_recovery_is_persisted_even_without_future_odds_or_completed_night(self):
        source = Path(os.environ['WILD4_WORKFLOW_PATH']).read_text()
        recovery = source.split('      - name: Recover Nov 8 original saved LOCKs omitted by failed publication\n')[1].split('      - name:', 1)[0]
        self.assertIn('id: publication-recovery', recovery)
        for name in ('Encrypt immutable future locks', c.PERSISTED_STEP):
            block = source.split('      - name: ' + name + '\n')[1].split('      - name:', 1)[0]
            self.assertIn("steps.publication-recovery.outputs.changed == 'true'", block)
        helper = Path(c.__file__).read_text()
        self.assertIn("output.write('changed=' + ('true' if result['restored'] else 'false')", helper)
        for name in ('Checkout pinned official settlement source', 'Settle only immutable locks from verified final NIGHT'):
            block = source.split('      - name: ' + name + '\n')[1].split('      - name:', 1)[0]
            self.assertIn("steps.publication-recovery.outputs.changed == 'true'", block.splitlines()[0])


class PersistedFailureTests(unittest.TestCase):
    def setUp(self):
        self.run = {'id': 11, 'status': 'completed', 'conclusion': 'failure', 'path': c.CALLER,
                    'head_branch': 'main', 'event': 'schedule',
                    'repository': {'id': c.REPOSITORY_ID}, 'head_repository': {'id': c.REPOSITORY_ID}}
        self.job = {'run_id': 11, 'name': 'Goripan WILD4 prospective observation / lock-future-only',
                    'status': 'completed', 'conclusion': 'failure', 'steps': [
            {'name': n, 'conclusion': 'success'} for n in (
                'Decrypt verified state and candidate',
                'Restore and verify earlier immutable SHADOW locks',
                'Encrypt immutable future locks', c.PERSISTED_STEP)] + [
            {'name': 'Project saved WILD4 locks and official settlements without source mutation', 'conclusion': 'success'},
            {'name': 'Require successful attempted public projection and delivery', 'conclusion': 'failure'}]}

    def test_publication_failure_keeps_successfully_preserved_work(self):
        self.assertTrue(c.persisted_failure(self.run, [self.job]))

    def test_missing_failed_cancelled_old_or_foreign_persistence_is_rejected(self):
        for status in ('failure', 'skipped', 'cancelled', None):
            with self.subTest(status=status):
                job = deepcopy(self.job); job['steps'][3]['conclusion'] = status
                self.assertFalse(c.persisted_failure(self.run, [job]))
        job = deepcopy(self.job); job['steps'][3]['name'] = 'Preserve encrypted shadow locks'
        self.assertFalse(c.persisted_failure(self.run, [job]))
        for field, bad in (('event', 'pull_request'), ('head_branch', 'other'),
                           ('repository', {'id': 1}), ('head_repository', {'id': 1}),
                           ('status', 'in_progress'), ('path', 'other'), ('conclusion', 'cancelled')):
            with self.subTest(field=field):
                self.assertFalse(c.persisted_failure({**self.run, field: bad}, [self.job]))

    def test_prepublication_error_or_later_unrelated_failure_is_not_accepted(self):
        job = deepcopy(self.job); job['steps'].insert(1, {'name': 'settlement', 'conclusion': 'failure'})
        self.assertFalse(c.persisted_failure(self.run, [job]))
        job = deepcopy(self.job); job['steps'].append({'name': 'unexpected failure', 'conclusion': 'failure'})
        self.assertFalse(c.persisted_failure(self.run, [job]))
        self.assertFalse(c.persisted_failure(self.run, [self.job, self.job]))


class AtomicRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name); self.base = root/'base'; self.donor = root/'donor'
        self.common = '20261108-01-01'; self.new = '20261108-01-02'
        self.pin = {'model': {'identity': 'frozen'}}
        self.saved = {'race_key': self.new, 'buy_count': 1,
                      'locked_at': '2026-11-08T09:54:00+09:00', 'deadline_at': '2026-11-08T10:00:00+09:00'}
        self.d = {'artifact_id': 1, 'run_id': 2, 'locks': [self.saved]}
        self.spec = {'day': c.DAY, 'baseline_locks': 1, 'expected_locks': 2, 'donors': [self.d]}
        for path, keys in ((self.base, [self.common]), (self.donor, [self.common, self.new])):
            (path/'model-registry/models/frozen').mkdir(parents=True)
            (path/'model-registry/models/frozen/model.cbm').write_bytes(b'unchanged-model')
            (path/'day-model-binding.json').write_bytes(c.canonical(self.pin))
            (path/'lifecycle.json').write_bytes(c.canonical({'last_attempt': None}))
            self.write(path/'batch-manifest.json', {'RACE_KEYS': keys, 'LOCKED_BEFORE_DEADLINE': len(keys),
                                                   'NEW_LOCKED_THIS_RUN': 1, 'SOURCE_ODDS_SHA256': 'a'*64})
            for key in keys:
                self.write(path/(key+'.json'), {'STANDARD_RACE_KEY': key, 'BUY_COUNT': 1,
                    'LOCKED_AT': self.saved['locked_at'], 'RACE_DEADLINE_AT': self.saved['deadline_at'],
                    'OFFICIAL_RESULT_USED': False, 'PRODUCTION_BETS_CHANGED': False, 'SELECTED_BETS': ['1-2-3']})
        (self.base/'odds.json').write_bytes(b'current'); (self.donor/'odds.json').write_bytes(b'older')

    def write(self, path, value):
        path.write_bytes(c.canonical(c.sealed(value)))

    def verify(self, root, day):
        keys = set(c.read_sealed(root/'batch-manifest.json')['RACE_KEYS'])
        for key in keys: c.read_sealed(root/(key+'.json'))
        return keys, self.pin, json.loads((root/'lifecycle.json').read_bytes())

    def call(self):
        return c.recover_originals(self.base, self.spec, [(self.d, self.donor)], verify=self.verify)

    def reject_unchanged(self, reason):
        before, donor_before = c.inventory(self.base), c.inventory(self.donor)
        with self.assertRaisesRegex(ValueError, reason): self.call()
        self.assertEqual(c.inventory(self.base), before); self.assertEqual(c.inventory(self.donor), donor_before)

    def test_exact_copy_preserves_all_other_state_and_is_idempotent(self):
        before, original = c.inventory(self.base), c.inventory(self.donor)
        self.assertEqual(self.call(), {'restored': 1, 'locks': 2, 'generation_count': 0})
        self.assertEqual((self.base/(self.new+'.json')).read_bytes(), (self.donor/(self.new+'.json')).read_bytes())
        after = c.inventory(self.base)
        self.assertTrue(all(after[k] == v for k, v in before.items() if k != 'batch-manifest.json'))
        self.assertEqual(c.inventory(self.donor), original)
        self.assertEqual(self.call(), {'restored': 0, 'locks': 2, 'generation_count': 0})
        self.assertEqual(c.inventory(self.base), after)

    def test_changed_overlap_and_model_fail_before_any_write(self):
        path = self.donor/(self.common+'.json'); raw = path.read_bytes(); path.write_bytes(raw+b' ')
        self.reject_unchanged('OVERLAP_BYTES'); path.write_bytes(raw)
        (self.donor/'model-registry/models/frozen/model.cbm').write_bytes(b'changed')
        self.reject_unchanged('MODEL_BYTES')

    def test_later_resave_invalid_hash_or_result_used_cannot_replace_original(self):
        path = self.donor/(self.new+'.json'); raw = path.read_bytes(); lock = json.loads(raw)
        for changes in ({'LOCKED_AT': '2026-11-08T09:55:00+09:00'}, {'OFFICIAL_RESULT_USED': True},
                        {'BUY_COUNT': 2}, {'RACE_DEADLINE_AT': '2026-11-08T09:50:00+09:00'}):
            self.write(path, {**lock, **changes}); self.reject_unchanged('ORIGINAL_LOCK_IDENTITY')
        path.write_bytes(raw.replace(b'1-2-3', b'6-5-4')); self.reject_unchanged('CONTENT_HASH')

    def test_missing_duplicate_or_extra_donor_fails_without_partial_recovery(self):
        self.spec['expected_locks'] = 3; self.reject_unchanged('FINAL_COVERAGE')
        self.spec['expected_locks'] = 2; self.spec['donors'].append({'artifact_id': 2})
        self.reject_unchanged('DONOR_COVERAGE')

    def test_existing_settlement_and_night_attempt_are_not_rebased(self):
        (self.base/'settlements').mkdir(); file = self.base/'settlements/record.json'; file.write_bytes(b'saved')
        self.reject_unchanged('NIGHT_ALREADY_PROCESSED'); file.unlink()
        (self.base/'lifecycle.json').write_bytes(c.canonical({'last_attempt': {'date': c.DAY}}))
        self.reject_unchanged('NIGHT_ALREADY_PROCESSED')

    def test_exact_partial_night_hold_retains_settlement_and_retry_bytes(self):
        (self.base/'settlements').mkdir()
        lock = c.read_sealed(self.base/(self.common+'.json'))
        self.write(self.base/'settlements'/(self.common+'.json'), {
            'SCHEMA': 'G11_WILD_BET_FUTURE_OOS_SHADOW_RESULT_V1', 'STANDARD_RACE_KEY': self.common,
            'SHADOW_LOCK_SHA256': lock['CONTENT_SHA256'], 'ACTUAL_PURCHASE': False,
            'PRODUCTION_PROMOTION_ELIGIBLE': False})
        state = {'last_attempt': {'date': c.DAY, 'state': 'TECHNICAL_HOLD',
            'reason': 'OFFICIAL_DAY_PREDICTION_COVERAGE', 'coverage': {'official_races': 2,
                'settled_current_locks': 1, 'missing_current_keys': [self.new]}},
            'night_coverage_retry': {'binding': {'date': c.DAY, 'cohort_sha256': 'f'*64}}}
        (self.base/'lifecycle.json').write_bytes(c.canonical(state))
        before = c.inventory(self.base)
        self.assertEqual(self.call()['restored'], 1)
        after = c.inventory(self.base)
        self.assertEqual(before['lifecycle.json'], after['lifecycle.json'])
        self.assertEqual(before['settlements/'+self.common+'.json'], after['settlements/'+self.common+'.json'])
        self.assertEqual(c.read_sealed(self.base/c.RECEIPT)['prior_settlements_preserved'], 1)

    def test_damaged_recovery_receipt_cannot_skip_verification(self):
        self.call(); path = self.base/c.RECEIPT; value = json.loads(path.read_bytes())
        value['inventory_spec_sha256'] = 'a'*64; self.write(path, value)
        self.reject_unchanged('RECOVERY_RECEIPT')


try:
    import test_wild4_saved_ledger_union_v1 as previous
    PRIVATE_READY = previous.PRIVATE_READY
except ImportError:
    PRIVATE_READY = False


@unittest.skipUnless(PRIVATE_READY, 'Pinned private verifier is required by CI')
class RealPrivateContractTests(unittest.TestCase):
    def test_saved_union_is_accepted_by_the_actual_frozen_projection(self):
        # Reuse the existing sealed-model fixture; only its synthetic dates and
        # small race set change. The real model and LOCK validators run below.
        previous.SavedLedgerUnionTests.setUp(self)
        for root in (self.base, self.donor):
            for p in list(root.glob('20261101-*.json')):
                p.unlink()
            for p in (root/'day-model-binding.json', root/'model-registry/lifecycle.json'):
                value = json.loads(p.read_bytes().replace(b'2026-11-01', b'2026-11-08'))
                p.write_bytes(c.canonical(c.sealed(value)))
        common, new = '20261108-01-01', '20261108-01-02'
        saved = {'race_key': new, 'buy_count': 1, 'locked_at': c.DAY+'T09:54:00+09:00',
                 'deadline_at': c.DAY+'T10:00:00+09:00'}
        pin = json.loads((self.base/'day-model-binding.json').read_bytes())
        for root, keys in ((self.base, [common]), (self.donor, [common, new])):
            manifest = c.read_sealed(root/'batch-manifest.json')
            manifest.update(DATE=c.DAY, RACE_KEYS=keys, LOCKED_BEFORE_DEADLINE=len(keys))
            (root/'batch-manifest.json').write_bytes(c.canonical(c.sealed(manifest)))
            for key in keys:
                lock = {'SCHEMA': 'G11_WILD_BET_FUTURE_OOS_SHADOW_LOCK_V1', 'STATUS': 'LOCKED_BEFORE_RESULT',
                    'STANDARD_RACE_KEY': key, 'CANDIDATE_MODEL_SHA256': self.model_sha,
                    'CANDIDATE_REGISTRY_SHA256': pin['model']['registry_sha256'],
                    'PRODUCTION_BETS_CHANGED': False, 'OFFICIAL_RESULT_USED': False,
                    'PRODUCTION_PROMOTION_ELIGIBLE': False, 'PRODUCTION_MONETARY_ACTION': 'PROHIBITED',
                    'TOP1_FORCED_AS_BET': False, 'SELECTED_BETS': ['1-2-3'], 'BUY_COUNT': 1,
                    'TOP1_DISPLAY': '1-2-3', 'ODDS_SNAPSHOT_SHA256': 'a'*64, 'INPUT_PACKET_SHA256': 'b'*64,
                    'LOCKED_AT': saved['locked_at'], 'RACE_DEADLINE_AT': saved['deadline_at']}
                (root/(key+'.json')).write_bytes(c.canonical(c.sealed(lock)))
        donor = {'artifact_id': 1, 'locks': [saved]}
        spec = {'day': c.DAY, 'baseline_locks': 1, 'expected_locks': 2, 'donors': [donor]}
        result = c.recover_originals(self.base, spec, [(donor, self.donor)])
        self.assertEqual(result, {'restored': 1, 'locks': 2, 'generation_count': 0})
        self.assertEqual(len(c.verify_ledger(self.base, c.DAY)[0]), 2)


if __name__ == '__main__':
    unittest.main()
