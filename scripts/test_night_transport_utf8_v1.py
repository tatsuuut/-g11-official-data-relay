"""Cross-language hashing and immutable retry-plan regression."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import hot_delta_transport_v1 as hot
import daily_runtime_delivery_v1 as delivery


class PublishedLockResultJoinTests(unittest.TestCase):
    def fixture(self):
        key = '20261031-09-03'
        live = {'snapshot_sha256': '8ec81787e5f75441efcb5fc85bb66412ef548f69277f03580c32c90d5c5027fe',
                'captured_at_jst': '2026-10-31T11:18:45+09:00',
                'locked_at_jst': '2026-10-31T11:18:45+09:00',
                'practical_bets': ['6-1-2', '6-2-1'], 'main_head': 6}
        shadow = {'v53': {'live': live, 'formal_use': True},
                  'live': {'feature_asof_time_jst': live['captured_at_jst'],
                           'prediction_lock_time_jst': live['locked_at_jst'],
                           'practical_bets': ['6-1-2'], 'main_head': 6}}
        row = {'key': key, 'deadline_jst': '2026-10-31T11:25:00+09:00',
               'p3_snapshot_sha256': 'b' * 64, 'abeken_shadow': shadow,
               'result_trifecta': '6-1-2', 'payout': 1200,
               'research_finance': {'profit': 1000}}
        checkpoint = {'feed_sha256': '18447a8b1489d99594d0e7ee37667e0b930db64992d34a5e6e5651fbabc08d6b',
            'authority_sha256': '48aae10804d6906d6834d11ec09ce59bca05e6b43a132887dfe0a8eeb3b7f66c',
            'checkpoint_sha256': 'c' * 64, 'operational_date_jst': '2026-10-31', 'pending': 0}
        accepted = {'operational_date_jst': '2026-10-31', 'stage': 'PREDEADLINE', 'races': [row]}
        feed = copy.deepcopy(accepted)
        feed.update(stage='NIGHT', counts={'races': 1, 'results': 1, 'pending': 0},
                    source={'projection_sha256': 'a' * 64, 'run_id': 41198657386},
                    generated_at_jst='2026-10-31T20:57:01+09:00',
                    daily_runtime={'authority_sha256': checkpoint['authority_sha256']})
        incoming = feed['races'][0]['abeken_shadow']
        incoming['v53']['live'].update(snapshot_sha256='d' * 64,
            captured_at_jst='2026-10-31T11:19:08+09:00', locked_at_jst='2026-10-31T11:19:08+09:00')
        incoming['live'].update(feature_asof_time_jst='2026-10-31T11:19:08+09:00',
                                prediction_lock_time_jst='2026-10-31T11:19:08+09:00')
        feed['final_accounting'] = {'by_race': {key: {'engines': {'abeken': {
            'final_bet_source': 'PRE_RACE', 'final_bets': live['practical_bets'], 'final_head': 6,
            'final_bet_snapshot_id': 'd' * 64, 'final_bet_locked_at_jst': '2026-10-31T11:19:08+09:00',
            'score': {'profit': 1000}, 'final_status': 'FINAL_PRE_RACE'}}}},
            'engines': {'abeken': {'profit': 1000}}}
        for field in ('result_trifecta', 'payout', 'research_finance'):
            accepted['races'][0].pop(field)
        return feed, accepted, checkpoint

    def test_only_five_lock_references_and_two_result_references_change(self):
        feed, accepted, checkpoint = self.fixture()
        before, public_before = copy.deepcopy(feed), copy.deepcopy(accepted)
        wire, proof = delivery.published_lock_night_20261031(feed, accepted, checkpoint)
        self.assertEqual(feed, before)
        self.assertEqual(accepted, public_before)
        self.assertEqual(wire['races'][0]['abeken_shadow'], accepted['races'][0]['abeken_shadow'])
        target = wire['final_accounting']['by_race']['20261031-09-03']['engines']['abeken']
        self.assertEqual(target['final_bet_snapshot_id'],
                         accepted['races'][0]['abeken_shadow']['v53']['live']['snapshot_sha256'])
        # Undo only the reviewed references: everything else must match exactly.
        wire['races'][0]['abeken_shadow'] = copy.deepcopy(feed['races'][0]['abeken_shadow'])
        original = feed['final_accounting']['by_race']['20261031-09-03']['engines']['abeken']
        for field in ('final_bet_snapshot_id', 'final_bet_locked_at_jst'):
            target[field] = original[field]
        self.assertEqual(wire, feed)
        self.assertFalse(proof['settlement_changed'])

    def test_changed_bet_model_or_head_is_rejected(self):
        for location, field, value in [('v53', 'practical_bets', ['1-2-3']),
                                       ('v53', 'main_head', 1),
                                       ('legacy', 'practical_bets', ['1-2-3'])]:
            with self.subTest(field=field, location=location):
                feed, accepted, checkpoint = self.fixture()
                live = feed['races'][0]['abeken_shadow']
                target = live['v53']['live'] if location == 'v53' else live['live']
                target[field] = value
                with self.assertRaisesRegex(RuntimeError, 'PREDICTION_CONTENT_CONFLICT'):
                    delivery.published_lock_night_20261031(feed, accepted, checkpoint)

    def test_canonical_or_published_identity_and_final_binding_are_required(self):
        feed, accepted, checkpoint = self.fixture()
        for field in ('feed_sha256', 'authority_sha256'):
            bad = dict(checkpoint, **{field: '0' * 64})
            with self.assertRaisesRegex(RuntimeError, 'CANONICAL_SCOPE'):
                delivery.published_lock_night_20261031(feed, accepted, bad)
        accepted['races'][0]['abeken_shadow']['v53']['live']['snapshot_sha256'] = '0' * 64
        with self.assertRaisesRegex(RuntimeError, 'PUBLISHED_LOCK_IDENTITY'):
            delivery.published_lock_night_20261031(feed, accepted, checkpoint)
        feed, accepted, checkpoint = self.fixture()
        feed['final_accounting']['by_race']['20261031-09-03']['engines']['abeken']['final_bets'] = ['1-2-3']
        with self.assertRaisesRegex(RuntimeError, 'FINAL_LOCK_AUTHORITY'):
            delivery.published_lock_night_20261031(feed, accepted, checkpoint)

    def test_other_days_are_unchanged(self):
        feed, accepted, checkpoint = self.fixture()
        feed['operational_date_jst'] = '2026-11-01'
        result, proof = delivery.published_lock_night_20261031(feed, accepted, checkpoint)
        self.assertIs(result, feed)
        self.assertIsNone(proof)

    def test_separate_immutable_plan_and_lost_ack_reprojection(self):
        feed, accepted, checkpoint = self.fixture()
        wire, proof = delivery.published_lock_night_20261031(feed, accepted, checkpoint)
        delta = hot.create_delta(feed, accepted, 'e' * 64)
        manifest, chunks = hot.night_chunks(delta, feed, checkpoint)
        plan = {'checkpoint_sha256': checkpoint['checkpoint_sha256'], 'manifest': manifest, 'chunks': chunks}
        before = copy.deepcopy(plan)
        with tempfile.TemporaryDirectory() as tmp:
            def immutable(path, content):
                if path.exists():
                    self.assertEqual(path.read_bytes(), content)
                else:
                    path.write_bytes(content)
            implementation = SimpleNamespace(immutable=immutable,
                json_file=lambda p: json.loads(p.read_bytes()), digest=hot.hash_bytes,
                canonical=lambda v: json.dumps(v, ensure_ascii=False, sort_keys=True,
                                              separators=(',', ':')).encode())
            args = (Path(tmp), plan, wire, accepted, checkpoint, proof, implementation)
            first = delivery.published_lock_night_plan(*args)
            self.assertEqual(first, delivery.published_lock_night_plan(*args))
            self.assertEqual(plan, before)
            self.assertEqual(first['manifest']['CANONICAL_FEED_SHA256'], checkpoint['feed_sha256'])
        retry, retry_proof = delivery.published_lock_night_20261031(feed, wire, checkpoint)
        self.assertEqual(retry, wire)
        self.assertEqual(retry_proof, proof)


class NightUtf8Tests(unittest.TestCase):
    def make_plan(self):
        day = '2026-10-31'
        top = {'source': {'projection_sha256': 'a' * 64},
               'generated_at_jst': day + 'T21:00:00+09:00',
               'final_accounting': {'explanation': '剥きけん夜結果🧠' * 9000}}
        rows = [{'RACE_KEY': '20261031-09-03', 'EXPECTED_RACE_VERSION': 'b' * 64,
                 'CHANGED_FIELDS': {'research_finance': {'profit': 1200},
                                    'result_trifecta': '1-2-3'}}]
        delta = {'OPERATIONAL_DATE': day, 'BASE_FEED_SHA': 'c' * 64,
                 'PATCH_SEQUENCE': 123001, 'PATCH_CREATED_AT': top['generated_at_jst'],
                 'PATCH_SHA256': hot.hash_bytes(hot.compact({'stage': 'NIGHT', 'day': day,
                     'top': top, 'races': rows})), 'TOP_LEVEL': top, 'RACES': rows}
        feed = {'source': top['source'], 'races': [{'key': rows[0]['RACE_KEY']}],
                'counts': {'races': 1, 'results': 1, 'pending': 0},
                'daily_runtime': {'authority_sha256': 'd' * 64}}
        checkpoint = {'operational_date_jst': day, 'pending': 0,
                      'authority_sha256': 'd' * 64, 'feed_sha256': 'e' * 64,
                      'checkpoint_sha256': 'f' * 64}
        manifest, chunks = hot.night_chunks(delta, feed, checkpoint)
        plan = {'checkpoint_sha256': checkpoint['checkpoint_sha256'],
                'manifest': manifest, 'chunks': chunks}
        plan['plan_sha256'] = hot.hash_bytes(json.dumps(plan, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode())
        return plan

    def test_large_japanese_and_four_byte_text_matches_whole_utf8_document(self):
        for message in ('剥きけん夜結果確認' * 15000, '結果🧠🦍' * 15000):
            value = {'message': message}
            self.assertGreater(len(hot.compact(value)), 200000)
            self.assertEqual(hot.site_hash(value), hot.hash_bytes(hot.compact(value)))

    def test_correct_plan_is_unchanged(self):
        plan = self.make_plan()
        self.assertEqual(delivery.utf8_night_plan(plan), plan)

    def test_failed_hash_plan_is_repaired_without_payload_or_checkpoint_changes(self):
        plan = self.make_plan()
        plan['chunks'][0]['CHUNK_SHA256'] = '0' * 64
        plan['manifest']['CHUNK_SHA256'][0] = '0' * 64
        plan['manifest']['EXPECTED_TOP_LEVEL_SHA'] = '0' * 64
        original = copy.deepcopy(plan)
        fixed = delivery.utf8_night_plan(plan)
        self.assertEqual(plan, original)
        self.assertEqual(fixed['checkpoint_sha256'], original['checkpoint_sha256'])
        for field in ('CANONICAL_FEED_SHA256', 'CHECKPOINT_SHA256', 'PATCH_SHA256',
                      'SOURCE_NIGHT_SHA', 'PATCH_CREATED_AT', 'BASE_FEED_SHA'):
            self.assertEqual(fixed['manifest'][field], original['manifest'][field])
        for before, after in zip(original['chunks'], fixed['chunks']):
            for field in ('TOP_LEVEL', 'RACES', 'RACE_KEYS'):
                self.assertEqual(before[field], after[field])
            self.assertEqual(after['CHUNK_SHA256'], hot.site_hash({
                'TOP_LEVEL': after['TOP_LEVEL'], 'RACES': after['RACES']}))

    def test_changed_payload_cannot_be_relabelled_as_hash_repair(self):
        plan = self.make_plan()
        plan['chunks'][-1]['RACES'][0]['CHANGED_FIELDS']['result_trifecta'] = '6-5-4'
        with self.assertRaisesRegex(RuntimeError, 'PAYLOAD_HASH_MISMATCH'):
            delivery.utf8_night_plan(plan)

    def test_repaired_plan_is_written_beside_original_and_retries_are_identical(self):
        plan = self.make_plan()
        plan['chunks'][0]['CHUNK_SHA256'] = '0' * 64
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original_path = root / 'night-transport-plan.json'
            original_path.write_bytes(hot.compact(plan))
            before = original_path.read_bytes()
            def immutable(path, value):
                if path.exists():
                    self.assertEqual(path.read_bytes(), value)
                else:
                    path.write_bytes(value)
            implementation = SimpleNamespace(immutable=immutable,
                json_file=lambda path: json.loads(path.read_bytes()))
            first = delivery.verified_utf8_plan(root, plan, implementation)
            second = delivery.verified_utf8_plan(root, plan, implementation)
            self.assertEqual(first, second)
            self.assertEqual(original_path.read_bytes(), before)
            self.assertTrue((root / 'night-transport-plan-utf8-v1.json').is_file())


class NightNoBodySavedPlanTests(unittest.TestCase):
    def fixture(self):
        day = '2026-11-01'
        row = {'key': '20261101-22-01', 'date_jst': day, 'venue': '福岡',
               'venue_code': '22', 'race': 1, 'deadline_jst': day + 'T10:30:00+09:00',
               'formal_status': 'EXCLUDED', 'exclusion_reason': 'BODY_NOT_GENERATED',
               'research_eligible': False, 'p3_snapshot_sha256': None,
               'p3_morning_snapshot_sha256': None, 'p3_snapshot': None,
               'p3_status': None, 'predeadline': None,
               'abeken_shadow': {'v53': {'morning': {'snapshot_sha256': '1' * 64,
                   'practical_bets': ['1-2-3'], 'locked_at_jst': day + 'T06:42:04+09:00'}}}}
        formal = dict(row, key='20261101-22-03', race=3, formal_status='FORMAL',
                      exclusion_reason=None, research_eligible=True, p3_snapshot_sha256='b' * 64)
        accepted = {'operational_date_jst': day, 'stage': 'PREDEADLINE', 'races': [row, formal]}
        feed = copy.deepcopy(accepted)
        feed.update(stage='NIGHT', counts={'races': 2, 'results': 2, 'pending': 0},
            source={'projection_sha256': 'a' * 64, 'run_id': 41313903069},
            generated_at_jst=day + 'T23:04:23+09:00',
            final_accounting={'by_race': {formal['key']: {'engines': {'p3': {'profit': 500}}}}},
            daily_runtime={'authority_sha256': 'd' * 64})
        for result in feed['races']:
            result.update(result_trifecta='1-2-3', payout=1000,
                          research_finance={'profit': 500})
        checkpoint = {'operational_date_jst': day, 'pending': 0,
                      'authority_sha256': 'd' * 64, 'feed_sha256': 'e' * 64,
                      'checkpoint_sha256': 'f' * 64}
        delta = hot.create_delta(feed, accepted, 'c' * 64)
        # Reproduce the previously persisted sender's null version exactly.
        next(r for r in delta['RACES'] if r['RACE_KEY'] == row['key'])['EXPECTED_RACE_VERSION'] = None
        delta['PATCH_SHA256'] = hot.hash_bytes(hot.compact({'stage': 'NIGHT', 'day': day,
            'top': delta['TOP_LEVEL'], 'races': delta['RACES']}))
        manifest, chunks = hot.night_chunks(delta, feed, checkpoint)
        plan = {'checkpoint_sha256': checkpoint['checkpoint_sha256'], 'manifest': manifest, 'chunks': chunks}
        self.sign(plan)
        return plan, feed, accepted, checkpoint

    @staticmethod
    def canonical(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False).encode()

    def sign(self, plan):
        plan.pop('plan_sha256', None)
        plan['plan_sha256'] = hot.hash_bytes(self.canonical(plan))

    def implementation(self):
        def immutable(path, value):
            if path.exists():
                self.assertEqual(path.read_bytes(), value)
            else:
                path.write_bytes(value)
        return SimpleNamespace(immutable=immutable, json_file=lambda p: json.loads(p.read_bytes()),
                               digest=hot.hash_bytes, canonical=self.canonical)

    def test_old_plan_is_preserved_and_only_missing_transport_version_changes(self):
        plan, feed, accepted, checkpoint = self.fixture()
        before = copy.deepcopy((plan, feed, accepted, checkpoint))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / 'night-transport-plan.json'
            original.write_bytes(hot.compact(plan))
            original_bytes = original.read_bytes()
            args = (root, plan, feed, accepted, checkpoint, self.implementation())
            fixed = delivery.verified_no_body_night_plan(*args)
            self.assertEqual(fixed, delivery.verified_no_body_night_plan(*args))
            self.assertEqual(original.read_bytes(), original_bytes)
            self.assertTrue((root / 'night-transport-plan-no-body-cas-v1.json').is_file())
        self.assertEqual((plan, feed, accepted, checkpoint), before)
        self.assertNotEqual(fixed['manifest']['NIGHT_TRANSACTION_ID'], plan['manifest']['NIGHT_TRANSACTION_ID'])
        for old_chunk, new_chunk in zip(plan['chunks'], fixed['chunks']):
            self.assertEqual(old_chunk['TOP_LEVEL'], new_chunk['TOP_LEVEL'])
            self.assertEqual(old_chunk['RACE_KEYS'], new_chunk['RACE_KEYS'])
            for old, new in zip(old_chunk['RACES'], new_chunk['RACES']):
                expected = copy.deepcopy(old)
                if expected['EXPECTED_RACE_VERSION'] is None:
                    expected['EXPECTED_RACE_VERSION'] = plan['manifest']['BASE_FEED_SHA']
                self.assertEqual(new, expected)
        self.assertEqual(delivery.utf8_night_plan(fixed), fixed)
        self.assertIs(delivery.verified_no_body_night_plan(Path('.'), fixed, feed, accepted,
                      checkpoint, self.implementation()), fixed)

    def test_partial_or_different_checkpoint_cannot_be_repaired(self):
        for field, value in [('checkpoint_sha256', '0' * 64), ('feed_sha256', '0' * 64)]:
            plan, feed, accepted, checkpoint = self.fixture()
            checkpoint[field] = value
            with self.assertRaisesRegex(RuntimeError, 'PLAN_IDENTITY'):
                delivery.verified_no_body_night_plan(Path('.'), plan, feed, accepted, checkpoint,
                                                    self.implementation())
        plan, feed, accepted, checkpoint = self.fixture()
        accepted['races'].pop()
        with self.assertRaisesRegex(RuntimeError, 'PLAN_COVERAGE'):
            delivery.verified_no_body_night_plan(Path('.'), plan, feed, accepted, checkpoint,
                                                self.implementation())

    def test_formal_or_changed_identity_never_gets_a_fallback(self):
        for field, value in [('formal_status', 'FORMAL'), ('predeadline', {}),
                             ('p3_snapshot_sha256', '1' * 64),
                             ('deadline_jst', '2026-11-01T10:31:00+09:00')]:
            plan, feed, accepted, checkpoint = self.fixture()
            feed['races'][0][field] = value
            with self.assertRaisesRegex(RuntimeError, 'VERSION_SCOPE'):
                delivery.verified_no_body_night_plan(Path('.'), plan, feed, accepted, checkpoint,
                                                    self.implementation())

    def test_saved_result_mutation_is_not_relabelled_as_transport_repair(self):
        plan, feed, accepted, checkpoint = self.fixture()
        plan['chunks'][-1]['RACES'][0]['CHANGED_FIELDS']['result_trifecta'] = '6-5-4'
        self.sign(plan)
        with self.assertRaisesRegex(RuntimeError, 'PAYLOAD_HASH_MISMATCH'):
            delivery.verified_no_body_night_plan(Path('.'), plan, feed, accepted, checkpoint,
                                                self.implementation())

    def test_missing_version_field_is_not_an_explicit_null_repair(self):
        plan, feed, accepted, checkpoint = self.fixture()
        del plan['chunks'][-1]['RACES'][0]['EXPECTED_RACE_VERSION']
        self.sign(plan)
        with self.assertRaisesRegex(RuntimeError, 'VERSION_FIELD_MISSING'):
            delivery.verified_no_body_night_plan(Path('.'), plan, feed, accepted, checkpoint,
                                                self.implementation())


if __name__ == '__main__':
    unittest.main()

