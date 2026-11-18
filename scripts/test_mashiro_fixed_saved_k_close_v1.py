"""Run with the frozen observer checkout used by the existing isolated workflow."""
import copy
from datetime import date
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import mashiro_fixed_saved_k_close_v1 as recovery


DAY = '2026-11-17'
KEY = '20261117-03-01'
STAMP = '2026-11-18T08:00:00+09:00'
HASH = 'a' * 64
OTHER = 'b' * 64
BASES = ['BOAT_NUMBER', 'EXHIBITION_COURSE']


class ArtifactTests(unittest.TestCase):
    def test_audit_uses_exact_available_date_when_prior_cache_has_expired(self):
        import isolated_research_runner_v1 as runner
        day = '2026-11-18'
        key = 'g11-state-v4-published-' + day + '-123-1-checkpoint'
        with patch.object(recovery, 'datetime') as clock, patch.object(runner, 'latest_canonical', side_effect=['', key]), \
                patch.object(runner, 'latest_research', side_effect=['', 'saved-observations']), patch.object(runner, 'outputs') as out:
            clock.now.return_value.date.return_value = date.fromisoformat(day)
            recovery.audit_source()
            out.assert_called_once_with(day=day, cache_key=key, research_key='saved-observations')

    def test_only_activated_matching_final_night_enqueues_research(self):
        import isolated_research_runner_v1 as runner
        key = 'g11-state-v4-published-' + DAY + '-123-1-checkpoint'
        env = {'GITHUB_REPOSITORY': recovery.REPO, 'GITHUB_REF': 'refs/heads/main', 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1'}
        with patch.dict(os.environ, env), patch.object(runner, 'dispatch_research') as dispatch, patch.object(Path, 'is_file', return_value=True):
            recovery.verified_night_handoff(key)
            dispatch.assert_called_once_with(DAY, key)
            with self.assertRaisesRegex(ValueError, 'NIGHT_HANDOFF'):
                recovery.verified_night_handoff(key.replace('123-', '456-'))

    def test_optional_selection_of_missing_or_older_published_day(self):
        for answer in ({'available': False}, {'available': True, 'projection': {'day': '2026-11-16'}}):
            with patch.object(recovery, 'request_json', return_value=(answer, {})):
                self.assertIsNone(recovery.saved_projection(DAY, required=False))
                self.assertFalse(recovery.select('2026-11-18', Path('.unused')))
                with self.assertRaisesRegex(ValueError, 'DAY_UNPUBLISHED'):
                    recovery.saved_projection(DAY)

    def test_only_named_live_matching_workflow_run(self):
        a = {'name': 'mashiro-p3-paired-2026-11-17-100-1', 'size_in_bytes': 200,
             'digest': 'sha256:' + HASH, 'created_at': STAMP, 'id': 1, 'expired': False,
             'workflow_run': {'id': 100, 'head_branch': 'main'}}
        later = dict(a, id=2)
        invalid = [dict(a, id=3, expired=True), dict(a, id=4, digest=''),
                   dict(a, id=5, workflow_run={'id': 107, 'head_branch': 'main'}),
                   dict(a, id=6, workflow_run={'id':100, 'head_branch': 'topic'}),
                   dict(a, id=7, name='mashiro-p3-paired-2026-11-18-100-1')]
        self.assertEqual(recovery.candidate_artifact([*invalid, later, a], DAY), later)

    def test_official_receipt_must_come_from_following_morning(self):
        receipt = {'SHA256': HASH, 'SOURCE_ID': 'result-k-20261117',
                   'URL': 'https://www1.mbrace.or.jp/od2/K/202611/k261117.lzh', 'CAPTURED_AT_JST': STAMP}
        store = Mock()
        store.response_receipt.return_value = receipt
        store.load_verified_snapshot.return_value = {'SNAPSHOT': {
            'PAYLOAD': {'OPERATIONAL_DATE_JST': '2026-11-18'}, 'SOURCE_RECEIPTS': []}}
        with self.assertRaisesRegex(ValueError, 'K_RECEIPT'):
            recovery.saved_k(store, None, '2026-11-18', DAY, {KEY})
        store.response_bytes.assert_not_called()
        store.load_verified_snapshot.return_value['SNAPSHOT']['SOURCE_RECEIPTS'] = [receipt]
        store.response_bytes.return_value = b'not-the-archived-bytes'
        with self.assertRaisesRegex(ValueError, 'K_BYTES'):
            recovery.saved_k(store, None, '2026-11-18', DAY, {KEY})


class JoinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(os.environ.get('MASHIRO_FIXED_OBSERVER_ROOT', '.g11-mashiro-fixed'))/'g11/research/mashiro_fixed_v1/observer.py'
        if not path.is_file():
            raise unittest.SkipTest('frozen Mashiro observer checkout required for original join integration')
        spec = importlib.util.spec_from_file_location('frozen_test_observer', path)
        cls.observer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.observer)
        cls.policy = cls.observer.configuration()[1]

    def setUp(self):
        inputs = [dict(boat=b, racer_number=5100+b, national_win_rate=5, motor=35) for b in range(1, 7)]
        targets = [dict(boat=b, racer_number=5100+b, position=b, grade='B1',
                        exhibition_rank=b, conditions={'test': True}) for b in (2, 3, 4)]
        obs = dict(RACE_KEY=KEY, OPERATIONAL_DATE_JST=DAY, CAPTURED_AT_JST=DAY+'T09:00:00+09:00',
                   DEADLINE_AT_JST=DAY+'T10:00:00+09:00', INPUT_ROWS=inputs,
                   COHORTS=[{'basis': b, 'targets': targets} for b in BASES],
                   MORNING_SNAPSHOT_SHA256=OTHER, PROTOCOL_SHA256=self.policy)
        self.observations = {KEY: (HASH, obs)}
        rows = [dict(race=KEY, date=DAY, venue=3, basis=b, boat=t['boat'], racer_number=t['racer_number'],
                     position=t['position'], grade=t['grade'], rank=t['exhibition_rank'], conditions=t['conditions'],
                     national=5, motor=35, observation_sha256=HASH, captured_at=obs['CAPTURED_AT_JST'],
                     deadline=obs['DEADLINE_AT_JST'], outcome='UNRESOLVED', result_sha256=None, name='保存済み')
                for b in BASES for t in targets]
        self.p = dict(day=DAY, protocol_sha256=self.policy, recorded=1, production_mutated=False,
                      official_acquisition=0, rows=rows, updated_at=DAY+'T21:00:00+09:00')
        self.result = dict(operational_date=DAY, venue_code='03', race_no=1, race_status='ESTABLISHED',
            entries=[{'boat': b, 'racer_number': 5100+b, 'finish': '01' if b==2 else 'F' if b==4 else '02'}
                     for b in range(1, 7)], trifecta_combination='2-3-1', refund_boats=[4])
        self.receipt = dict(SHA256=OTHER, CAPTURED_AT_JST=STAMP)

    def joined(self):
        return recovery.join_saved(self.observer, self.p, self.observations, {KEY:self.result}, self.receipt, HASH, STAMP)

    def test_preserves_original_projection_and_seals_actual_winner_loss_and_refund(self):
        original = copy.deepcopy(self.p)
        out, source, proofs, held = self.joined()
        self.assertEqual(self.p, original)
        self.assertEqual(source, OTHER)
        self.assertFalse(held)
        self.assertEqual([r['outcome'] for r in out['rows']], ['WIN','LOSS','REFUNDED']*2)
        digest = recovery.sha(recovery.compact(proofs[KEY]))
        for before, after in zip(self.p['rows'], out['rows']):
            self.assertEqual(after['result_sha256'], digest)
            self.assertEqual(set(k for k in after if before[k] != after[k]), {'outcome', 'result_sha256'})

    def test_six_person_roster_mismatch_holds_original_rows(self):
        self.result['entries'][0]['racer_number'] += 1
        updated, _, proofs, held = self.joined()
        self.assertEqual(updated['rows'], self.p['rows'])
        self.assertIn('ROSTER_CHANGED', held[KEY])
        self.assertFalse(proofs)

    def test_void_is_excluded_and_preexisting_verified_outcomes_are_immutable(self):
        self.result.update(race_status='TRIFECTA_NOT_ESTABLISHED', entries=[], trifecta_combination=None, refund_boats=[])
        self.p['rows'][0].update(outcome='VOID', result_sha256=OTHER)
        out, _, _, _ = self.joined()
        self.assertTrue(all(r['outcome']=='VOID' for r in out['rows']))
        self.assertEqual(out['rows'][0]['result_sha256'], OTHER)
        self.p['rows'][0]['outcome'] = 'WIN'
        with self.assertRaisesRegex(ValueError, 'EXISTING_OUTCOME_CONFLICT'):
            self.joined()

    def test_capture_and_full_target_list_must_match_prior_publication(self):
        self.p['rows'][0]['observation_sha256'] = OTHER
        with self.assertRaisesRegex(ValueError, 'CHANGED_PUBLISHED_OBSERVATION'):
            self.joined()
        self.p['rows'][0]['observation_sha256'] = HASH
        self.p['rows'].pop()
        with self.assertRaisesRegex(ValueError, 'PUBLISHED_TARGET_SET'):
            self.joined()

    def test_receipt_before_deadline_does_not_settle(self):
        self.receipt['CAPTURED_AT_JST'] = DAY+'T09:10:00+09:00'
        out, _, _, held = self.joined()
        self.assertEqual(out['rows'], self.p['rows'])
        self.assertIn('K_BEFORE_DEADLINE', held[KEY])


if __name__ == '__main__':
    unittest.main()
