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


if __name__ == '__main__':
    unittest.main()
