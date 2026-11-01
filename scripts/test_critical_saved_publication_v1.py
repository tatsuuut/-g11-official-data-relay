"""Failure tests for the saved-only early publication boundary."""
import copy
from contextlib import redirect_stdout
from datetime import datetime
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import critical_saved_publication_v1 as early
import predeadline_race_isolation_v1 as handoff
import hot_delta_transport_v1 as hot
import daily_runtime_delivery_v1 as delivery

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

    def test_real_projector_import_needs_no_optional_dependencies(self):
        source = Path(__file__).resolve().parents[1] / '.g11-private'
        if not (source / 'g11/relay/v1.py').is_file():
            self.skipTest('private source is available in the required contract gate')
        statement = (
            'import sys; from g11.relay import v1; '
            'from g11.growth_p3.v1_1 import relay_publish_v1; '
            "bad={'catboost','numpy','pandas','scipy','matplotlib','plotly','lxml'}; "
            'assert not bad.intersection(sys.modules), bad.intersection(sys.modules)'
        )
        result = subprocess.run([sys.executable, '-S', '-c', statement], cwd=source,
                                capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, result.stderr)

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

    def test_early_home_evidence_contains_only_new_unexpired_races(self):
        rows = [{'key': key, 'deadline_jst': DAY + at} for key, at in
                [('new', 'T20:36:00+09:00'), ('old', 'T20:36:00+09:00'),
                 ('expired', 'T20:30:00+09:00')]]
        feed = {'races': rows, 'operational_date_jst': DAY}
        result = early.fresh_input_source(feed, {'V53_LOCKED_BEFORE_DEADLINE': ['new', 'expired']}, NOW)
        self.assertEqual(['new'], [r['key'] for r in result['races']])
        self.assertEqual(3, len(feed['races']))

    def test_optional_research_wire_mismatch_does_not_retry_or_rewrite_source(self):
        old = Path.cwd()
        try:
            os.chdir(self.root); Path('.relay-output').mkdir()
            path = Path('.relay-output/deme-discovery-public.json')
            payload = {'schema': 'G11_P3_DEME_DISCOVERY_V1', 'production_mutated': False,
                'available': True, 'counts': {}, 'candidates': [],
                'updated_at': DAY+'T20:31:10.123456+09:00'}
            path.write_text(json.dumps(payload)); before = path.read_bytes()
            self.assertIsNone(delivery.optional_deme_projection(DAY))
            self.assertEqual(before, path.read_bytes())
            payload['updated_at'] = DAY+'T20:31:10+09:00'
            path.write_text(json.dumps(payload))
            self.assertEqual(payload, delivery.optional_deme_projection(DAY))
        finally:
            os.chdir(old)


class ExcludedNoBodyTransportTests(unittest.TestCase):
    """Nov 1 accepted-feed identity/version projection; pending changes are synthetic.

    The public feed at 10:39:37 had no P3 or live version for 福岡1R/2R.
    The SG exclusion and formal row below both had valid saved versions.
    Only `wild_pack.changed` is varied here; no prediction is generated.
    """
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        cwd = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)
        Path('.relay-output').mkdir()
        self.feed_path = Path('canonical-feed.json')
        self.accepted = {
            'operational_date_jst': '2026-11-01', 'stage': 'PREDEADLINE',
            'source': {'run_id': 41261629519},
            'generated_at_jst': '2026-11-01T10:39:37+09:00',
            'races': [
                {'key': '20261101-02-01', 'formal_status': 'FORMAL',
                 'exclusion_reason': None, 'research_eligible': True,
                 'p3_snapshot': 'MORNING', 'p3_status': 'PASS',
                 'p3_snapshot_sha256': 'd2ba4f6af6c1eb3f22d40d82fa8d7e50dd4a36eab553bc14b866dc746dd55047',
                 'deadline_jst': '2026-11-01T10:45:00+09:00',
                 'predeadline': {'snapshot_sha256': 'c26c49bb6367d1203b2b2cc58eea8279f9750ff2f9d2c9ba07dfcac455c5c4b2',
                                 'captured_at_jst': '2026-11-01T10:38:45+09:00'},
                 'wild_pack': {'changed': None}},
                {'key': '20261101-13-01', 'formal_status': 'EXCLUDED',
                 'exclusion_reason': 'SG開催｜研究・通知対象外', 'research_eligible': False,
                 'p3_snapshot': 'MORNING', 'p3_status': 'PASS',
                 'p3_snapshot_sha256': 'b5dae98062f6ce3abfbace50fb087f822e287e7b05bcbd2c95c528e36ec07079',
                 'deadline_jst': '2026-11-01T10:37:00+09:00',
                 'predeadline': {'snapshot_sha256': '9825f319ea3794c7acfe33e9b589831075de48f00d17abd7d3a8365fee82158c',
                                 'captured_at_jst': '2026-11-01T10:32:11+09:00'},
                 'wild_pack': {'changed': False}},
                *[{'key': key, 'formal_status': 'EXCLUDED',
                   'exclusion_reason': 'BODY_NOT_GENERATED', 'research_eligible': False,
                   'deadline_jst': '2026-11-01T' + deadline + '+09:00',
                   'predeadline': None, 'wild_pack': {'changed': None}}
                  for key, deadline in (('20261101-22-01', '10:49:00'),
                                        ('20261101-22-02', '11:16:00'))],
            ],
        }
        for row in self.accepted['races']:
            row.update(date_jst='2026-11-01', venue_code=row['key'].split('-')[1],
                       race=int(row['key'].split('-')[2]))
            row['venue'] = {'02': '戸田', '13': '尼崎', '22': '福岡'}[row['venue_code']]
        self.incoming = copy.deepcopy(self.accepted)
        self.incoming.update(source={'run_id': 41262073496},
                             generated_at_jst='2026-11-01T10:46:00+09:00')
        for row in self.incoming['races']:
            row['wild_pack']['changed'] = True
        self.site = copy.deepcopy(self.accepted)
        self.posts = []
        self.corrupt_readback = False

    def delta(self, incoming=None, accepted=None):
        with redirect_stdout(io.StringIO()) as output:
            result = hot.create_delta(incoming or self.incoming, accepted or self.accepted, 'a'*64)
        return result, output.getvalue()

    def test_mixed_actual_projection_holds_only_the_two_unversioned_exclusions(self):
        before = copy.deepcopy((self.incoming, self.accepted))
        delta, output = self.delta()
        self.assertEqual([r['RACE_KEY'] for r in delta['RACES']],
                         ['20261101-02-01', '20261101-13-01'])
        self.assertEqual([r['EXPECTED_RACE_VERSION'] for r in delta['RACES']],
                         [r['predeadline']['snapshot_sha256'] for r in self.accepted['races'][:2]])
        held = [json.loads(line.split('=', 1)[1]) for line in output.splitlines()
                if line.startswith('G11_HOT_DELTA_HELD_RACE=')]
        self.assertEqual([r['race_key'] for r in held], ['20261101-22-01', '20261101-22-02'])
        self.assertTrue(all(r['reason'] == 'EXCLUDED_BODY_NOT_GENERATED_WITHOUT_FORMAL_VERSION'
                            and r['changed_fields'] == ['wild_pack'] for r in held))
        self.assertEqual((self.incoming, self.accepted), before)

    def test_guard_requires_explicit_matching_exclusion_and_absence_on_both_sides(self):
        cases = [('formal_status', 'FORMAL'), ('formal_status', 'UNKNOWN'),
                 ('formal_status', None), ('exclusion_reason', None),
                 ('exclusion_reason', 'SG開催｜研究・通知対象外'),
                 ('research_eligible', True), ('research_eligible', 0),
                 ('research_eligible', None), ('p3_snapshot_sha256', ''),
                 ('p3_snapshot_sha256', 'malformed'), ('p3_snapshot_sha256', 'a'*64),
                 ('p3_morning_snapshot_sha256', 'a'*64), ('p3_snapshot', 'MORNING'),
                 ('p3_status', 'PASS'), ('predeadline', {}),
                 ('deadline_jst', None), ('deadline_jst', ''),
                 ('deadline_jst', '2026-11-01T10:50:00+09:00'),
                 ('date_jst', None), ('date_jst', '2026-11-02'), ('venue_code', '01')]
        for side in ('incoming', 'accepted'):
            for field, value in cases:
                with self.subTest(side=side, field=field, value=value):
                    incoming, accepted = copy.deepcopy((self.incoming, self.accepted))
                    selected = incoming if side == 'incoming' else accepted
                    selected['races'][2][field] = value
                    delta, _ = self.delta(incoming, accepted)
                    self.assertIn('20261101-22-01', [r['RACE_KEY'] for r in delta['RACES']])
        # A claimed formal live object is still validated, never silently held.
        self.incoming['races'][2]['predeadline'] = {
            'snapshot_sha256': None, 'captured_at_jst': '2026-11-01T10:42:00+09:00'}
        with self.assertRaisesRegex(RuntimeError, 'HOT_POST_DEADLINE_FORECAST'):
            self.delta()

    def test_unknown_race_still_fails_closed(self):
        self.incoming['races'][2]['key'] = '20261101-22-03'
        with self.assertRaisesRegex(RuntimeError, 'HOT_UNKNOWN_RACE'):
            self.delta()

    def test_night_uses_stored_base_cas_for_only_the_two_no_body_rows(self):
        self.incoming.update(stage='NIGHT', counts={'races': 4, 'results': 4, 'pending': 0})
        for row in self.incoming['races']:
            row.update(result_trifecta='1-2-3', payout=100)
        before = copy.deepcopy((self.incoming, self.accepted))
        delta, output = self.delta()
        self.assertEqual(len(delta['RACES']), 4)
        self.assertEqual([r['EXPECTED_RACE_VERSION'] for r in delta['RACES'][:2]],
                         [r['predeadline']['snapshot_sha256'] for r in self.accepted['races'][:2]])
        self.assertEqual([r['EXPECTED_RACE_VERSION'] for r in delta['RACES'][2:]], ['a'*64, 'a'*64])
        self.assertTrue(all(r['CHANGED_FIELDS']['result_trifecta'] == '1-2-3'
                            and r['CHANGED_FIELDS']['payout'] == 100 for r in delta['RACES']))
        self.assertEqual((self.incoming, self.accepted), before)
        self.assertNotIn('G11_HOT_DELTA_HELD_RACE=', output)
        self.incoming['counts']['pending'] = 1
        with self.assertRaisesRegex(RuntimeError, 'HOT_NIGHT_INCOMPLETE_SOURCE'):
            self.delta()

    def test_night_base_cas_rejects_unverified_exclusions_and_identity_changes(self):
        self.incoming.update(stage='NIGHT', counts={'races': 4, 'results': 4, 'pending': 0})
        cases = [('formal_status', 'FORMAL'), ('exclusion_reason', 'UNKNOWN'),
                 ('exclusion_reason', 'SG開催｜研究・通知対象外'),
                 ('research_eligible', True), ('research_eligible', 0),
                 ('p3_snapshot_sha256', ''), ('p3_snapshot_sha256', 'malformed'),
                 ('p3_morning_snapshot_sha256', 'b'*64),
                 ('p3_snapshot', 'MORNING'), ('p3_status', 'PASS'), ('predeadline', {}),
                 ('date_jst', None), ('date_jst', '2026-11-02'),
                 ('venue', None), ('venue', ''), ('venue', ' '),
                 ('venue_code', '00'), ('venue_code', '25'), ('venue_code', '03'),
                 ('race', 0), ('race', 13), ('race', True), ('race', '1'), ('race', 2),
                 ('deadline_jst', None), ('deadline_jst', ''),
                 ('deadline_jst', '2026-11-02T10:49:00+09:00'),
                 ('deadline_jst', '2026-11-01T24:49:00+09:00'),
                 ('deadline_jst', '2026-11-01T10:49:00Z'),
                 ('deadline_jst', '2026-11-01T10:49:00.000+09:00')]
        for side in ('incoming', 'accepted', 'both'):
            for field, value in cases:
                with self.subTest(side=side, field=field, value=value):
                    incoming, accepted = copy.deepcopy((self.incoming, self.accepted))
                    if side in ('incoming', 'both'):
                        incoming['races'][2][field] = value
                    if side in ('accepted', 'both'):
                        accepted['races'][2][field] = value
                    with self.assertRaisesRegex(RuntimeError, 'HOT_NIGHT_RACE_VERSION'):
                        self.delta(incoming, accepted)
        # Even a valid replacement identity cannot reuse the prior row's CAS.
        incoming = copy.deepcopy(self.incoming)
        incoming['races'][2]['venue'] = '別会場'
        with self.assertRaisesRegex(RuntimeError, 'HOT_NIGHT_RACE_VERSION'):
            self.delta(incoming, self.accepted)

    def test_night_base_cas_requires_real_base_digest_and_missing_formal_versions_fail(self):
        self.incoming.update(stage='NIGHT', counts={'races': 4, 'results': 4, 'pending': 0})
        for invalid in (None, '', 'a'*63, 'A'*64, 'z'*64):
            with self.subTest(base=invalid), self.assertRaisesRegex(RuntimeError, 'HOT_NIGHT_BASE_HASH'):
                with redirect_stdout(io.StringIO()):
                    hot.create_delta(self.incoming, self.accepted, invalid)
        for invalid in (None, '', 'invalid', 'a'*63, 'A'*64):
            with self.subTest(version=invalid):
                incoming, accepted = copy.deepcopy((self.incoming, self.accepted))
                for feed in (incoming, accepted):
                    feed['races'][0]['predeadline'] = None
                    feed['races'][0]['p3_snapshot_sha256'] = invalid
                with self.assertRaisesRegex(RuntimeError, 'HOT_NIGHT_RACE_VERSION'):
                    self.delta(incoming, accepted)

    def test_night_preserves_all_166_rows_and_refuses_partial_coverage(self):
        # Nov 1 admitted key set; result values are transport-only fixtures.
        venues = ('02', '03', '04', '06', '07', '09', '13', '14', '17', '18', '19', '21', '22', '24')
        original = copy.deepcopy(self.accepted['races'])
        rows = []
        for venue in venues:
            for number in range(1, 13):
                if venue == '17' and number in (6, 12):
                    continue
                source = original[number + 1] if venue == '22' and number <= 2 else original[0]
                row = copy.deepcopy(source)
                row.update(key=f'20261101-{venue}-{number:02d}', venue_code=venue,
                           venue='福岡' if venue == '22' else '検証会場', race=number)
                rows.append(row)
        self.accepted['races'] = rows
        self.incoming = copy.deepcopy(self.accepted)
        self.incoming.update(stage='NIGHT', counts={'races': 166, 'results': 166, 'pending': 0})
        for row in self.incoming['races']:
            row.update(result_trifecta='1-2-3', payout=100)
        before = copy.deepcopy((self.incoming, self.accepted))
        delta, _ = self.delta()
        self.assertEqual(166, len(delta['RACES']))
        self.assertEqual({r['key'] for r in rows}, {r['RACE_KEY'] for r in delta['RACES']})
        self.assertEqual(['20261101-22-01', '20261101-22-02'],
                         [r['RACE_KEY'] for r in delta['RACES'] if r['EXPECTED_RACE_VERSION'] == 'a'*64])
        self.assertEqual((self.incoming, self.accepted), before)
        self.incoming['races'].pop()
        self.incoming['counts'].update(races=165, results=165)
        with self.assertRaisesRegex(RuntimeError, 'HOT_NIGHT_INCOMPLETE_SOURCE'):
            self.delta()

    def fetch(self, url):
        value = copy.deepcopy(self.site)
        source_sha = hot.hash_bytes(hot.compact(self.site))
        if self.posts and self.corrupt_readback:
            value['races'][0]['wild_pack']['changed'] = None
        return value, {'x-g11-source-feed-sha256': source_sha}, hot.compact(value)

    def send(self, args, **kwargs):
        # Substitute only the remote Site; reject missing/invalid CAS versions.
        delta = json.loads(Path(args[args.index('--data-binary')+1][1:]).read_bytes())
        self.posts.append(delta)
        if any(not isinstance(row['EXPECTED_RACE_VERSION'], str)
               or len(row['EXPECTED_RACE_VERSION']) != 64 for row in delta['RACES']):
            status, response = 409, {'status': 'FAIL', 'error': 'HOT_DELTA_RACE'}
        else:
            self.site.update(copy.deepcopy(delta['TOP_LEVEL']))
            rows = {r['key']: r for r in self.site['races']}
            for change in delta['RACES']:
                rows[change['RACE_KEY']]['wild_pack'].update(change['CHANGED_FIELDS']['wild_pack'])
            status, response = 200, {'status': 'PASS',
                                    'payload_sha256': hot.hash_bytes(hot.compact(self.site))}
        Path(args[args.index('--output')+1]).write_text(json.dumps(response))
        return subprocess.CompletedProcess(args, 0, stdout=str(status), stderr='')

    def publish(self):
        original = hot.compact(self.incoming) + b'\n'
        self.feed_path.write_bytes(original)
        before = copy.deepcopy((self.incoming, self.accepted))
        with patch.object(hot, 'fetch', side_effect=self.fetch), \
             patch.object(hot.subprocess, 'run', side_effect=self.send), \
             patch.dict(os.environ, {'G11_SYNC_OIDC': 'fixture-only', 'GITHUB_OUTPUT': 'output'}), \
             redirect_stdout(io.StringIO()) as output:
            try:
                hot.publish(self.feed_path, 'https://example.invalid')
            finally:
                self.assertEqual(self.feed_path.read_bytes(), original)
                self.assertEqual((self.incoming, self.accepted), before)
        return output.getvalue()

    def test_mixed_publish_verifies_sent_rows_and_retains_held_rows_unchanged(self):
        output = self.publish()
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.site['races'][2:], self.accepted['races'][2:])
        self.assertTrue(all(r['wild_pack']['changed'] for r in self.site['races'][:2]))
        self.assertIn('G11_HOT_DELTA_READBACK=PASS', output)
        self.assertEqual(Path('.relay-output/hot-delta-accepted').read_text().strip(),
                         hot.hash_bytes(hot.compact(self.site)))
        self.assertEqual(json.loads(Path('.relay-output/hot-delta-readback.json').read_bytes()), self.site)

    def test_all_held_is_no_post_and_does_not_advance_public_timestamps(self):
        self.incoming['races'][:2] = copy.deepcopy(self.accepted['races'][:2])
        output = self.publish()
        self.assertEqual(self.posts, [])
        self.assertEqual(self.site, self.accepted)
        self.assertIn('G11_HOT_DELTA_NO_CHANGE=PASS', output)
        self.assertEqual(Path('.relay-output/hot-delta-accepted').read_text().strip(),
                         hot.hash_bytes(hot.compact(self.accepted)))

    def test_held_rows_do_not_weaken_formal_readback_or_invalid_version_rejection(self):
        self.corrupt_readback = True
        with self.assertRaisesRegex(RuntimeError, 'HOT_DELTA_READBACK_FIELD:20261101-02-01:wild_pack'):
            self.publish()
        self.assertFalse(Path('.relay-output/hot-delta-accepted').exists())
        self.corrupt_readback = False
        self.site = copy.deepcopy(self.accepted)
        for feed in (self.incoming, self.accepted, self.site):
            feed['races'][2]['formal_status'] = 'FORMAL'
        with self.assertRaisesRegex(RuntimeError, 'HOT_DELTA_REJECTED:HOT_DELTA_RACE'):
            self.publish()
        self.assertFalse(Path('.relay-output/hot-delta-accepted').exists())


class CaptureBoundaryTests(unittest.TestCase):
    def task(self, due, deadline, key='a'):
        return {'DUE_AT_JST': DAY+'T'+due+'+09:00',
                'MUST_FINISH_BEFORE_JST': DAY+'T'+deadline+'+09:00',
                'SOURCE_ID': key, 'RACE_KEY': key}

    def test_waits_once_for_nearest_allowed_capture_without_advancing_it(self):
        plan = [self.task('20:34:00', '20:41:00'), self.task('20:35:00', '20:42:00', 'b')]
        self.assertEqual(170, early.capture_boundary_delay(plan, set(), NOW))
        self.assertEqual(0, early.capture_boundary_delay(
            [self.task('20:37:00', '20:44:00')], set(), NOW))
        self.assertEqual(0, early.capture_boundary_delay(plan, {'a', 'b'}, NOW))

    def test_due_active_or_partially_saved_race_is_never_delayed(self):
        plan = [self.task('20:31:00', '20:38:00'), self.task('20:34:00', '20:41:00', 'b')]
        for saved in (set(), {'a'}):
            self.assertEqual(0, early.capture_boundary_delay(plan, saved, NOW))
        self.assertEqual(0, early.capture_boundary_delay(
            [self.task('20:20:00', '20:27:00')], set(), NOW))

    def test_invalid_window_or_day_cannot_create_a_wait(self):
        with self.assertRaisesRegex(RuntimeError, 'CAPTURE_BOUNDARY_PLAN'):
            early.capture_boundary_delay([self.task('20:34:00', '20:44:00')], set(), NOW)
        task = self.task('20:34:00', '20:41:00')
        task['DUE_AT_JST'] = '2026-10-25T20:34:00+09:00'
        with self.assertRaisesRegex(RuntimeError, 'CAPTURE_BOUNDARY_PLAN'):
            early.capture_boundary_delay([task], set(), NOW)

    def test_alignment_remains_before_unchanged_frozen_critical_call(self):
        source = (Path(__file__).resolve().parents[1] / 'scripts/daily_predeadline_authority_recovery_v1.py').read_text()
        block = source.split('if mode == "critical":', 1)[1].split('elif mode == "predeadline":', 1)[0]
        self.assertLess(block.index('align_empty_capture('), block.index('predeadline_critical_v1.main()'))

    def test_verified_scope_wait_has_one_target_and_bounded_sleep_segments(self):
        plan = [self.task('20:32:00', '20:39:00', 'excluded'),
                self.task('20:34:00', '20:41:00'), self.task('20:35:00', '20:42:00', 'b')]
        artifact = Mock()
        artifact.load_verified_snapshot.return_value = {'SNAPSHOT': {'PAYLOAD': {'PREDEADLINE_PLAN': plan}}}
        pipeline = SimpleNamespace(OfficialArtifactStore=Mock(return_value=artifact),
            CanonicalRunner=SimpleNamespace(_formal_prediction_scope=Mock(return_value=({}, ['a', 'b'], []))))
        sleeper, clock = Mock(), Mock(return_value=NOW)
        with tempfile.TemporaryDirectory() as temp, patch.dict(sys.modules, {
            'g11': ModuleType('g11'), 'g11.canonical': ModuleType('g11.canonical'),
            'g11.canonical.official_pipeline': pipeline}):
            early.align_empty_capture(Path(temp), DAY, clock=clock, sleeper=sleeper)
        intervals = [call.args[0] for call in sleeper.call_args_list]
        self.assertEqual(170, sum(intervals))
        self.assertTrue(all(0 < part <= 30 for part in intervals))
        clock.assert_called_once()


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

    def test_http_readback_requires_exact_final_response_hash(self):
        import hashlib
        raw = json.dumps(self.feed).encode()
        def response(command, **kwargs):
            Path(command[command.index('--output')+1]).write_bytes(raw)
            Path(command[command.index('--dump-header')+1]).write_text(
                'HTTP/1.1 200 Connection established\nX-G11-Feed-Sha256: invalid\n\n'
                'HTTP/2 200\nX-G11-Feed-Sha256: ' + hashlib.sha256(raw).hexdigest() + '\n')
        with patch.object(handoff.subprocess, 'run', side_effect=response):
            self.assertEqual([], handoff.without_published_goripan([self.row], DAY))
        def wrong_hash(command, **kwargs):
            response(command, **kwargs)
            Path(command[command.index('--dump-header')+1]).write_text('HTTP/2 200\n')
        with patch.object(handoff.subprocess, 'run', side_effect=wrong_hash):
            self.assertEqual([self.row], handoff.without_published_goripan([self.row], DAY))
        with patch.object(handoff.subprocess, 'run', side_effect=subprocess.TimeoutExpired('curl', 8)):
            self.assertEqual([self.row], handoff.without_published_goripan([self.row], DAY))


if __name__ == '__main__':
    unittest.main()

