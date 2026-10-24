"""Failure tests for the saved-only early publication boundary."""
import copy
from datetime import datetime
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import critical_saved_publication_v1 as early
import predeadline_race_isolation_v1 as handoff
import hot_delta_transport_v1 as hot

DAY = '2026-10-24'
NOW = datetime.fromisoformat(DAY + 'T20:31:10+09:00')


class EarlyPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.out = self.root / 'critical-feed.json'
        self.authority = {'authority_sha256': 'a'*64, 'runtime_source_sha': 'b'*40}
        self.meta = {'OPERATIONAL_DATE_JST': DAY, 'CRITICAL': {
            'OPERATIONAL_DATE': DAY, 'CRITICAL_CHECKPOINT_STATUS': 'PASS',
            'LAST_CRITICAL_RUN_ID': '123', 'LAST_TIMELY_P3_LIVE_COUNT': 1,
            'LAST_TIMELY_V53_COUNT': 1, 'AUTHORITY_SHA256': 'a'*64,
            'RUNTIME_SOURCE_SHA': 'b'*40}}
        self.saved = {'key': '20261024-12-12', 'predeadline': {
            'practical_bets': ['1-2-3'], 'snapshot_sha256': 'c'*64,
            'captured_at_jst': DAY+'T20:31:03+09:00'}}
        self.module = SimpleNamespace(verify_runtime=Mock(return_value=self.authority),
            verify_locks=Mock(), feed_binding=Mock(return_value={'authority': 'a'*64}))
        self.publisher = SimpleNamespace(load_locked_publication=Mock(return_value={'READ_ONLY': True}))
        self.relay = SimpleNamespace(_morning_projection=Mock(return_value={}),
            build_feed=Mock(return_value={'stage': 'PREDEADLINE', 'status': 'PASS',
                'operational_date_jst': DAY, 'races': [self.saved]}))

    def project(self, now=NOW):
        (self.root/'relay-state.json').write_text(json.dumps(self.meta))
        return early.project_saved(self.root, DAY, '123', self.root/'frozen', self.out,
            authority_module=self.module, relay=self.relay, publisher=self.publisher, now=now)

    def test_only_saved_projection_and_same_authority_are_published(self):
        before = copy.deepcopy(self.saved)
        self.assertTrue(self.project())
        self.assertEqual(before, json.loads(self.out.read_text())['races'][0])
        self.assertEqual(4, self.module.verify_locks.call_count)
        self.assertEqual('b'*40, self.relay.build_feed.call_args.kwargs['github_sha'])
        self.assertEqual('predeadline', self.relay.build_feed.call_args.kwargs['relay_phase'])

    def test_no_new_locks_does_not_build_or_send(self):
        self.meta['CRITICAL'].update(LAST_TIMELY_P3_LIVE_COUNT=0, LAST_TIMELY_V53_COUNT=0)
        self.assertFalse(self.project())
        self.relay.build_feed.assert_not_called()
        self.assertFalse(self.out.exists())

    def test_wrong_day_run_or_authority_cannot_publish(self):
        for key, value in [('LAST_CRITICAL_RUN_ID', '122'), ('AUTHORITY_SHA256', 'd'*64),
                           ('CRITICAL_CHECKPOINT_STATUS', 'PARTIAL')]:
            old = self.meta['CRITICAL'][key]; self.meta['CRITICAL'][key] = value
            with self.assertRaises(RuntimeError): self.project()
            self.meta['CRITICAL'][key] = old
        with self.assertRaisesRegex(RuntimeError, 'CURRENT_DAY'):
            self.project(datetime.fromisoformat('2026-10-25T00:01:00+09:00'))
        self.assertFalse(self.out.exists())

    def test_changed_lock_or_unexpected_night_projection_stops_before_output(self):
        self.module.verify_locks.side_effect = [None, None, RuntimeError('LOCK_CHANGED')]
        with self.assertRaisesRegex(RuntimeError, 'LOCK_CHANGED'): self.project()
        self.assertFalse(self.out.exists())
        self.module.verify_locks.side_effect = None
        self.relay.build_feed.return_value['stage'] = 'NIGHT'
        with self.assertRaisesRegex(RuntimeError, 'FEED_BINDING'): self.project()
        self.assertFalse(self.out.exists())

    def test_workflow_keeps_persistence_and_handoff_before_fast_publication(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root/'.github/workflows/g11-free-runner.yml').read_text()
        names = ['id: critical-save', 'id: wild4-early', 'name: Publish persisted critical LOCKs',
                 'id: predeadline-chain', 'name: Install pinned private runtime dependencies']
        self.assertEqual(sorted(workflow.index(n) for n in names), [workflow.index(n) for n in names])
        save = workflow.split('id: critical-save', 1)[1].split('      - name:', 1)[0]
        self.assertIn('uses: ./.github/actions/g11-publication-checkpoint', save)
        publish = workflow.split('name: Publish persisted critical LOCKs', 1)[1].split('      - name:', 1)[0]
        self.assertIn("steps.critical-save.outputs.ready=='true'", publish)
        self.assertIn('continue-on-error: true', publish)

    def test_early_and_regular_publication_have_distinct_ordered_sequences(self):
        incoming = {'operational_date_jst': DAY, 'stage': 'PREDEADLINE',
                    'source': {'run_id': 123}, 'generated_at_jst': NOW.isoformat(), 'races': []}
        accepted = {'operational_date_jst': DAY, 'races': []}
        early_delta = hot.create_delta(incoming, accepted, 'a'*64, sequence_offset=0)
        regular_delta = hot.create_delta(incoming, accepted, 'a'*64)
        self.assertEqual(123000, early_delta['PATCH_SEQUENCE'])
        self.assertEqual(123001, regular_delta['PATCH_SEQUENCE'])
        with self.assertRaisesRegex(RuntimeError, 'SEQUENCE_SCOPE'):
            hot.create_delta({**incoming, 'stage': 'NIGHT'}, accepted, 'a'*64, sequence_offset=0)


class PublishedGoripanTests(unittest.TestCase):
    def setUp(self):
        self.key = '20261024-12-12'
        self.row = {'RACE_KEY': self.key, 'DEADLINE_AT_JST': DAY+'T20:36:00+09:00'}
        self.lock = {'race_key': self.key, 'machine_identity': 'WILD4',
            'status': 'VIRTUAL_PREDEADLINE_LOCK', 'locked_at': DAY+'T20:33:53+09:00',
            'deadline_at': self.row['DEADLINE_AT_JST'], 'lock_sha256': 'a'*64,
            'model_sha256': 'b'*64, 'odds_snapshot_sha256': 'c'*64}
        self.feed = {'schema': 'G11_PUBLIC_APP_FEED_V2', 'status': 'PASS',
            'operational_date_jst': DAY, 'races': [{'key': self.key, 'date_jst': DAY,
            'deadline_jst': self.row['DEADLINE_AT_JST'], 'wild_pack': {'wild4': {'goripan': self.lock}}}]}

    def test_already_public_lock_is_not_queued_again(self):
        self.assertEqual([], handoff.exclude_published_goripan([self.row], self.feed, DAY))

    def test_absent_or_invalid_lock_keeps_existing_retry(self):
        for field, bad in [('locked_at', DAY+'T20:36:00+09:00'), ('lock_sha256', 'bad'),
                           ('race_key', '20261024-24-12'), ('status', 'WAITING')]:
            old = self.lock[field]; self.lock[field] = bad
            self.assertEqual([self.row], handoff.exclude_published_goripan([self.row], self.feed, DAY))
            self.lock[field] = old
        self.feed['races'][0]['wild_pack'] = {}
        self.assertEqual([self.row], handoff.exclude_published_goripan([self.row], self.feed, DAY))

    def test_wrong_date_and_duplicate_races_cannot_suppress_retry(self):
        with self.assertRaisesRegex(ValueError, 'PUBLIC_DAY'):
            handoff.exclude_published_goripan([self.row], self.feed, '2026-10-25')
        self.feed['races'] *= 2
        with self.assertRaisesRegex(ValueError, 'PUBLIC_DUPLICATE'):
            handoff.exclude_published_goripan([self.row], self.feed, DAY)


if __name__ == '__main__':
    unittest.main()
