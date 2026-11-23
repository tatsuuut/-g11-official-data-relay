"""Failure tests for the saved-only early publication boundary."""
import copy
from contextlib import redirect_stdout, nullcontext, contextmanager
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
        self.enterContext(patch.object(early,'official_input_compatibility',return_value=nullcontext()))
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

    def test_saved_projection_uses_the_same_official_parser_context_as_capture(self):
        active=[]
        @contextmanager
        def compatibility():
            active.append(True)
            try:yield
            finally:active.pop()
        original=self.relay.build_feed.return_value
        def read_saved(*args,**kwargs):
            if not active:raise RuntimeError('NIGHT_SG_OFFICIAL_B_QUARANTINE_BINDING')
            return original
        self.relay.build_feed.side_effect=read_saved
        with patch.object(early,'official_input_compatibility',side_effect=compatibility):
            self.assertTrue(self.project())
        self.assertEqual(active,[])
        self.assertEqual(self.saved,json.loads(self.out.read_text())['races'][0])

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

    def test_saved_terminal_availability_publishes_without_new_prediction_locks(self):
        day = '2026-11-11'
        self.meta['OPERATIONAL_DATE_JST'] = day
        self.meta['CRITICAL'].update(OPERATIONAL_DATE=day,
            LAST_TIMELY_P3_LIVE_COUNT=0, LAST_TIMELY_V53_COUNT=0)
        row = {'key': '20261111-01-01', 'predeadline_availability': {
            'schema': 'G11_PREDEADLINE_AVAILABILITY_V1', 'status': 'UNAVAILABLE',
            'reason': 'OFFICIAL_WITHDRAWAL_UNSUPPORTED_FIELD_SIZE'}}
        self.relay.build_feed.return_value.update(operational_date_jst=day, races=[row])
        (self.root/'relay-state.json').write_text(json.dumps(self.meta))
        self.assertTrue(early.project_saved(self.root, day, '123', self.root/'frozen', self.out,
            authority_module=self.module, relay=self.relay, publisher=self.publisher,
            now=datetime.fromisoformat(day+'T12:04:00+09:00')))
        self.assertEqual([row], json.loads(self.out.read_text())['races'])
        self.assertEqual(4, self.module.verify_locks.call_count)

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

    def completed_night(self):
        root = self.root / 'daily-runtime' / DAY
        root.mkdir(parents=True)
        self.module.canonical = lambda value: json.dumps(value, ensure_ascii=False,
            sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        self.module.digest = lambda raw: early.hashlib.sha256(raw).hexdigest()
        checkpoint = {'feed_sha256': 'c'*64, 'checkpoint_sha256': 'd'*64,
                      'races': 1, 'results': 1, 'pending': 0}
        self.module.read_checkpoint = Mock(return_value=checkpoint)
        feed = {'operational_date_jst': DAY, 'stage': 'NIGHT', 'status': 'PASS',
                'daily_runtime': self.module.feed_binding(self.authority, 'NIGHT'),
                'source': {'github_sha': self.authority['runtime_source_sha']},
                'races': [{'key': '20261024-12-12', 'deadline_jst': DAY+'T20:30:00+09:00'}]}
        (root / 'night-feed.json').write_text(json.dumps(feed))
        receipt = {'schema': 'G11_DAILY_SITE_DELIVERY_RECEIPT_V1',
            'operational_date_jst': DAY, 'phase': 'NIGHT', **self.authority,
            'canonical_feed_sha256': checkpoint['feed_sha256'],
            'checkpoint_sha256': checkpoint['checkpoint_sha256'],
            'site_payload_sha256': 'e'*64, 'site_get_sha256': 'f'*64,
            'night_transaction_id': '1'*64, 'manifest_sha256': '2'*64,
            'http_success': True, 'site_get_readback': 'PASS', 'atomic_transaction': 'PASS',
            'daily_operation_complete': 'PASS', 'results': 1, 'races': 1, 'pending': 0,
            'completion_gate': {'DAILY_AUTHORITY_READBACK': 'PASS', 'MORNING_AUTHORITY_MATCH': 'PASS',
                'PREDEADLINE_AUTHORITY_MATCH': 'PASS', 'NIGHT_AUTHORITY_MATCH': 'PASS',
                'RESULTS': 1, 'RACES': 1, 'PENDING': 0, 'MORNING_LOCK_MUTATION': 0,
                'PREDEADLINE_LOCK_MUTATION': 0, 'PREDICTION_RECALCULATION': 0,
                'FINANCE_VERIFIED': 'PASS', 'IMMUTABLE_NIGHT_CHECKPOINT': 'PASS',
                'ATOMIC_TRANSACTION': 'PASS', 'SITE_GET_READBACK': 'PASS'}}
        self.write_night_receipt(receipt)
        return root, receipt

    def write_night_receipt(self, receipt):
        unsigned = {k: v for k, v in receipt.items() if k != 'receipt_sha256'}
        receipt = {**unsigned, 'receipt_sha256': self.module.digest(self.module.canonical(unsigned))}
        path = self.root / 'daily-runtime' / DAY / 'daily-operation-complete.json'
        path.write_text(json.dumps(receipt))
        return path

    def test_verified_completed_night_skips_stale_live_without_projection_or_transport(self):
        root, _ = self.completed_night()
        before = {path.name: path.read_bytes() for path in root.iterdir()}
        with redirect_stdout(io.StringIO()) as output:
            self.assertFalse(self.project())
        self.module.read_checkpoint.assert_called_once_with(self.root, DAY)
        self.relay.build_feed.assert_not_called()
        self.publisher.load_locked_publication.assert_not_called()
        self.assertFalse(self.out.exists())
        self.assertEqual(before, {path.name: path.read_bytes() for path in root.iterdir()})
        self.assertIn('SKIPPED_VERIFIED_COMPLETED_NIGHT', output.getvalue())

    def test_completed_night_uses_native_checkpoint_hash_and_lock_verifier(self):
        from daily_runtime_bootstrap_v1 import module
        source = Path(__file__).resolve().parents[1] / '.g11-private/g11/relay/daily_runtime_authority_v1.py'
        if not source.is_file():
            self.skipTest('retained authority reader is exercised in the private contract gate')
        implementation = module(source)
        root, receipt = self.completed_night()
        lock = self.root / 'original-lock.bin'; lock.write_bytes(b'original saved LOCK')
        inventory = lambda: {'original-lock.bin': implementation.digest(lock.read_bytes())}
        night = self.root / 'store/snapshots/night'; night.mkdir(parents=True)
        for name in ('official-day-results', 'workbook-ledger-projection'):
            (night / (name+'.json')).write_text('{}')
        checkpoint = {'schema': implementation.CHECKPOINT_SCHEMA, 'operational_date_jst': DAY,
            'authority_sha256': self.authority['authority_sha256'], 'immutable': True,
            'races': 1, 'results': 1, 'pending': 0, 'finance_verified': True,
            'morning_lock_mutated': False, 'predeadline_lock_mutated': False,
            'prediction_recalculated': False,
            'feed_sha256': implementation.digest((root/'night-feed.json').read_bytes()),
            'official_source_files': {'result-k': 'verified'},
            'official_results_sha256': implementation.digest((night/'official-day-results.json').read_bytes()),
            'night_projection_sha256': implementation.digest((night/'workbook-ledger-projection.json').read_bytes())}
        for phase in ('morning', 'predeadline'):
            checkpoint[phase+'_lock_sha256'] = implementation.digest(implementation.canonical(inventory()))
            checkpoint[phase+'_bindings_sha256'] = implementation.digest(implementation.canonical([]))
        checkpoint['checkpoint_sha256'] = implementation.digest(implementation.canonical(checkpoint))
        (root/'night-checkpoint.json').write_bytes(implementation.canonical(checkpoint))
        receipt.update(canonical_feed_sha256=checkpoint['feed_sha256'],
                       checkpoint_sha256=checkpoint['checkpoint_sha256'])
        self.write_night_receipt(receipt)
        with patch.object(implementation, 'load', return_value=self.authority), \
             patch.object(implementation, 'official_sources', return_value={'result-k': 'verified'}), \
             patch.object(implementation, 'verify_locks', side_effect=lambda *a, **k: inventory()), \
             patch.object(implementation, 'bindings', return_value=[]), \
             patch.object(implementation, 'feed_binding', self.module.feed_binding), \
             redirect_stdout(io.StringIO()):
            self.assertTrue(early.completed_night_publication(self.root, DAY,
                self.authority, implementation, NOW))
            lock.write_bytes(b'changed LOCK')
            with self.assertRaisesRegex(RuntimeError, 'NIGHT_CHECKPOINT_LOCK_HASH_MISMATCH'):
                early.completed_night_publication(self.root, DAY, self.authority, implementation, NOW)

    def test_completed_night_must_have_same_authority_checkpoint_and_complete_receipt(self):
        _, receipt = self.completed_night()
        for key, value in [('authority_sha256', '3'*64), ('runtime_source_sha', '4'*40),
                           ('canonical_feed_sha256', '5'*64), ('checkpoint_sha256', '6'*64),
                           ('site_get_readback', 'PARTIAL'), ('http_success', False),
                           ('phase', 'PREDEADLINE'), ('pending', 1), ('races', 2),
                           ('completion_gate', {})]:
            with self.subTest(key=key):
                self.write_night_receipt({**receipt, key: value})
                with self.assertRaisesRegex(RuntimeError, 'NIGHT_'):
                    self.project()
        self.relay.build_feed.assert_not_called()
        self.assertFalse(self.out.exists())

    def test_completed_night_corrupt_receipt_or_changed_immutable_checkpoint_cannot_skip(self):
        root, receipt = self.completed_night()
        path = root / 'daily-operation-complete.json'
        saved = json.loads(path.read_bytes()); saved['site_get_readback'] = 'PARTIAL'
        path.write_text(json.dumps(saved))
        with self.assertRaisesRegex(RuntimeError, 'NIGHT_RECEIPT_HASH'):
            self.project()
        self.write_night_receipt(receipt)
        self.module.read_checkpoint.side_effect = RuntimeError('NIGHT_CHECKPOINT_LOCK_HASH_MISMATCH')
        with self.assertRaisesRegex(RuntimeError, 'NIGHT_CHECKPOINT_LOCK_HASH_MISMATCH'):
            self.project()
        self.assertFalse(self.out.exists())

    def test_completed_night_never_suppresses_future_races_or_an_unpublished_night(self):
        root, receipt = self.completed_night()
        feed_path = root / 'night-feed.json'
        feed = json.loads(feed_path.read_bytes())
        feed['races'][0]['deadline_jst'] = DAY+'T20:32:00+09:00'
        feed_path.write_text(json.dumps(feed))
        with self.assertRaisesRegex(RuntimeError, 'NIGHT_FUTURE_RACE'):
            self.project()
        (root / 'daily-operation-complete.json').unlink()
        self.relay.build_feed.return_value['stage'] = 'NIGHT'
        with self.assertRaisesRegex(RuntimeError, 'EARLY_PUBLICATION_FEED_BINDING'):
            self.project()
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

    def test_independent_saved_abeken_lock_uses_base_cas_without_creating_p3(self):
        incoming = copy.deepcopy(self.accepted)
        incoming.update(source=self.incoming['source'],
                        generated_at_jst=self.incoming['generated_at_jst'])
        saved = {'snapshot_sha256': 'b' * 64,
                 'locked_at_jst': '2026-11-01T10:42:00+09:00',
                 'captured_at_jst': '2026-11-01T10:42:00+09:00',
                 'practical_bets': ['2-1-3']}
        incoming['races'][2]['abeken_shadow'] = {'v53': {'live': saved}}
        before = copy.deepcopy((incoming, self.accepted))
        delta, output = self.delta(incoming)
        self.assertEqual(len(delta['RACES']), 1)
        change = delta['RACES'][0]
        self.assertEqual(change['RACE_KEY'], '20261101-22-01')
        self.assertEqual(change['EXPECTED_RACE_VERSION'], 'a' * 64)
        self.assertEqual(change['CHANGED_FIELDS'], {'abeken_shadow': {'v53': {'live': saved}}})
        self.assertNotIn('G11_HOT_DELTA_HELD_RACE=', output)
        self.assertEqual((incoming, self.accepted), before)
        self.assertIsNone(incoming['races'][2]['predeadline'])
        incoming['races'][2]['wild_pack']['changed'] = True
        held, output = self.delta(incoming)
        self.assertEqual(held['RACES'], [])
        self.assertIn('G11_HOT_DELTA_HELD_RACE=', output)

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
        raw=hot.compact(value)
        return value, {'x-g11-source-feed-sha256': source_sha,
                       'x-g11-feed-sha256':hot.hash_bytes(raw)}, raw

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
                verified=hot.publish(self.feed_path, 'https://example.invalid')
                self.assertIsInstance(verified,tuple)
                self.assertEqual(verified[0],json.loads(verified[2]))
                self.assertEqual(verified[1]['x-g11-feed-sha256'],hot.hash_bytes(verified[2]))
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


class WithdrawalAvailabilityTransportTests(unittest.TestCase):
    """Synthetic evidence only: no private prediction or official response bytes."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        cwd = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)
        Path('.relay-output').mkdir()
        self.day = '2026-11-11'
        self.key = '20261111-01-01'
        self.evidence = {
            'schema': 'G11_PREDEADLINE_AVAILABILITY_V1', 'status': 'UNAVAILABLE',
            'reason': 'OFFICIAL_WITHDRAWAL_UNSUPPORTED_FIELD_SIZE',
            'withdrawn_boats': [3], 'active_boats': [1, 2, 4, 5, 6],
            'captured_at_jst': self.day+'T12:04:00+09:00',
            'source_snapshot_sha256': 'c'*64, 'official_response_sha256': 'd'*64,
            'source_url': 'https://www.boatrace.jp/owpc/pc/race/odds3t?rno=1&jcd=01&hd=20261111',
        }
        self.accepted = {'operational_date_jst': self.day, 'stage': 'MORNING',
            'source': {'run_id': 123}, 'generated_at_jst': self.day+'T06:00:00+09:00',
            'races': [{'key': self.key, 'date_jst': self.day, 'venue': '検証会場',
                'venue_code': '01', 'race': 1, 'deadline_jst': self.day+'T12:10:00+09:00',
                'formal_status': 'FORMAL', 'exclusion_reason': None, 'research_eligible': True,
                'p3_snapshot': 'MORNING', 'p3_status': 'PASS', 'p3_snapshot_sha256': 'b'*64,
                'p3_morning_snapshot_sha256': 'b'*64, 'practical_bets': ['1-2-4'],
                'predeadline': None}]}
        self.incoming = copy.deepcopy(self.accepted)
        self.incoming.update(stage='PREDEADLINE', source={'run_id': 124},
                             generated_at_jst=self.day+'T12:04:01+09:00')
        self.incoming['races'][0]['predeadline_availability'] = copy.deepcopy(self.evidence)
        self.site = copy.deepcopy(self.accepted)
        self.posts = []
        self.corrupt = None

    def delta(self):
        with redirect_stdout(io.StringIO()):
            return hot.create_delta(self.incoming, self.accepted, 'a'*64)

    def fetch(self, url):
        value = copy.deepcopy(self.site)
        if self.posts and self.corrupt == 'absent':
            value['races'][0].pop('predeadline_availability', None)
        elif self.posts and self.corrupt == 'changed':
            value['races'][0]['predeadline_availability']['source_snapshot_sha256'] = 'e'*64
        return value, {'x-g11-source-feed-sha256': hot.hash_bytes(hot.compact(self.site))}, hot.compact(value)

    def send(self, args, **kwargs):
        delta = json.loads(Path(args[args.index('--data-binary')+1][1:]).read_bytes())
        self.posts.append(delta)
        self.site.update(copy.deepcopy(delta['TOP_LEVEL']))
        self.site['stage'] = delta['STAGE']
        rows = {row['key']: row for row in self.site['races']}
        for change in delta['RACES']:
            self.assertEqual('b'*64, change['EXPECTED_RACE_VERSION'])
            rows[change['RACE_KEY']].update(copy.deepcopy(change['CHANGED_FIELDS']))
        response = {'status': 'PASS', 'payload_sha256': hot.hash_bytes(hot.compact(self.site))}
        Path(args[args.index('--output')+1]).write_text(json.dumps(response))
        return subprocess.CompletedProcess(args, 0, stdout='200', stderr='')

    def publish(self):
        path = Path('synthetic-feed.json')
        source = hot.compact(self.incoming)
        path.write_bytes(source)
        before = copy.deepcopy((self.incoming, self.accepted))
        with patch.object(hot, 'fetch', side_effect=self.fetch), \
             patch.object(hot.subprocess, 'run', side_effect=self.send), \
             patch.dict(os.environ, {'G11_SYNC_OIDC': 'fixture-only', 'GITHUB_OUTPUT': 'output'}), \
             redirect_stdout(io.StringIO()) as output:
            try:
                hot.publish(path, 'https://example.invalid')
            finally:
                self.assertEqual(source, path.read_bytes())
                self.assertEqual(before, (self.incoming, self.accepted))
        return output.getvalue()

    def test_availability_only_delta_publishes_and_preserves_existing_locks(self):
        before = copy.deepcopy(self.accepted['races'][0])
        delta = self.delta()
        self.assertEqual(1, len(delta['RACES']))
        self.assertEqual({'predeadline_availability': self.evidence}, delta['RACES'][0]['CHANGED_FIELDS'])
        self.assertEqual('b'*64, delta['RACES'][0]['EXPECTED_RACE_VERSION'])
        output = self.publish()
        self.assertEqual(1, len(self.posts))
        self.assertIn('G11_HOT_DELTA_READBACK=PASS', output)
        self.assertNotIn('G11_HOT_DELTA_NO_CHANGE=PASS', output)
        retained = {key: value for key, value in self.site['races'][0].items()
                    if key != 'predeadline_availability'}
        self.assertEqual(before, retained)
        self.assertEqual(self.evidence, self.site['races'][0]['predeadline_availability'])

    def test_repeat_evidence_is_no_change_and_does_not_advance_public_time(self):
        self.accepted['races'][0]['predeadline_availability'] = copy.deepcopy(self.evidence)
        self.site = copy.deepcopy(self.accepted)
        output = self.publish()
        self.assertEqual([], self.posts)
        self.assertEqual(self.accepted, self.site)
        self.assertIn('G11_HOT_DELTA_NO_CHANGE=PASS', output)

    def test_missing_or_changed_readback_never_records_acceptance(self):
        for corrupt in ('absent', 'changed'):
            with self.subTest(corrupt=corrupt):
                self.corrupt, self.posts, self.site = corrupt, [], copy.deepcopy(self.accepted)
                with self.assertRaisesRegex(RuntimeError, 'HOT_DELTA_READBACK_FIELD:.*:predeadline_availability'):
                    self.publish()
                self.assertFalse(Path('.relay-output/hot-delta-accepted').exists())

    def test_only_reviewed_rosters_same_race_hashes_and_capture_window_are_admitted(self):
        failures = [
            ('schema', 'unknown', 'CONTRACT'), ('status', 'WAITING', 'CONTRACT'),
            ('reason', 'INPUT_MISSING', 'CONTRACT'),
            ('withdrawn_boats', [True], 'ROSTER'), ('withdrawn_boats', [3, 3], 'ROSTER'),
            ('active_boats', [1, 2, 3, 4, 5, 6], 'ROSTER'),
            ('source_snapshot_sha256', 'bad', 'HASH'), ('official_response_sha256', 'G'*64, 'HASH'),
            ('captured_at_jst', self.day+'T12:10:00+09:00', 'CAPTURE'),
            ('captured_at_jst', self.day+'T12:02:59+09:00', 'CAPTURE'),
            ('captured_at_jst', self.day+'T12:04:00', 'CAPTURE'),
            ('captured_at_jst', '2026-11-10T12:04:00+09:00', 'CAPTURE'),
            ('source_url', self.evidence['source_url'].replace('www.boatrace.jp', 'example.invalid'), 'SOURCE'),
            ('source_url', self.evidence['source_url'].replace('rno=1', 'rno=2'), 'SOURCE'),
            ('source_url', self.evidence['source_url']+'&rno=1', 'SOURCE'),
        ]
        for field, value, reason in failures:
            with self.subTest(field=field, value=value):
                self.incoming['races'][0]['predeadline_availability'] = {**self.evidence, field: value}
                with self.assertRaisesRegex(RuntimeError, 'HOT_AVAILABILITY_'+reason):
                    self.delta()
        self.incoming['races'][0]['predeadline_availability'] = copy.deepcopy(self.evidence)
        self.accepted['operational_date_jst'] = self.incoming['operational_date_jst'] = '2026-11-10'
        with self.assertRaisesRegex(RuntimeError, 'HOT_AVAILABILITY_CONTRACT'):
            self.delta()

    def test_valid_three_to_five_active_boats_and_query_order_are_supported(self):
        for withdrawn in ([3], [2, 3], [2, 3, 6]):
            with self.subTest(withdrawn=withdrawn):
                evidence = self.incoming['races'][0]['predeadline_availability']
                evidence.update(withdrawn_boats=withdrawn,
                    active_boats=[boat for boat in range(1, 7) if boat not in withdrawn],
                    source_url='https://www.boatrace.jp/owpc/pc/race/odds3t?hd=20261111&jcd=01&rno=1')
                self.assertEqual(1, len(self.delta()['RACES']))

    def test_previously_published_evidence_is_immutable(self):
        self.accepted['races'][0]['predeadline_availability'] = copy.deepcopy(self.evidence)
        self.incoming['races'][0]['predeadline_availability']['source_snapshot_sha256'] = 'e'*64
        with self.assertRaisesRegex(RuntimeError, 'HOT_AVAILABILITY_IMMUTABLE'):
            self.delta()

    def test_no_body_exclusion_uses_exact_base_cas_only_for_bounded_fields(self):
        for feed in (self.accepted, self.incoming):
            row = feed['races'][0]
            row.update(formal_status='EXCLUDED', exclusion_reason='BODY_NOT_GENERATED',
                       research_eligible=False, practical_bets=[])
            for field in ('p3_snapshot_sha256', 'p3_morning_snapshot_sha256', 'p3_snapshot', 'p3_status'):
                row[field] = None
        delta = self.delta()
        self.assertEqual('a'*64, delta['RACES'][0]['EXPECTED_RACE_VERSION'])
        self.assertEqual({'predeadline_availability'}, set(delta['RACES'][0]['CHANGED_FIELDS']))
        self.incoming['races'][0]['abeken_shadow'] = {'v53': {'formal_use': False}}
        delta = self.delta()
        self.assertEqual('a'*64, delta['RACES'][0]['EXPECTED_RACE_VERSION'])
        self.assertEqual({'predeadline_availability', 'abeken_shadow'}, set(delta['RACES'][0]['CHANGED_FIELDS']))
        self.incoming['races'][0]['wild_pack'] = {'changed': True}
        self.assertEqual([], self.delta()['RACES'])

    def test_morning_projection_bounds_evidence_and_keeps_morning_values(self):
        feed = copy.deepcopy(self.incoming)
        feed['stage'] = 'MORNING'
        feed['races'][0]['predeadline_availability']['private_detail'] = 'not-public'
        path = self.root/'morning.json'
        path.write_text(json.dumps(feed))
        with redirect_stdout(io.StringIO()):
            hot.project_morning_base(path)
        row = json.loads(path.read_bytes())['races'][0]
        self.assertEqual(self.evidence, row['predeadline_availability'])
        for field in ('practical_bets', 'p3_snapshot_sha256', 'p3_morning_snapshot_sha256'):
            self.assertEqual(self.accepted['races'][0][field], row[field])

    def test_night_transport_and_readback_retain_terminal_evidence(self):
        self.incoming.update(stage='NIGHT', status='PASS', counts={'races': 1, 'results': 1, 'pending': 0})
        self.incoming['races'][0].update(result_trifecta='1-2-4', payout=100)
        self.assertEqual(self.evidence, self.delta()['RACES'][0]['CHANGED_FIELDS']['predeadline_availability'])
        checkpoint = {'races': 1, 'feed_sha256': 'e'*64, 'checkpoint_sha256': 'f'*64, 'authority_sha256': 'a'*64}
        readback = copy.deepcopy(self.incoming)
        readback['transport'] = {'canonical_feed_sha256': 'e'*64, 'night_checkpoint_sha256': 'f'*64,
                                 'daily_authority_sha256': 'a'*64}
        headers = {'x-g11-canonical-feed-sha256': 'e'*64, 'x-g11-daily-authority-sha256': 'a'*64}
        with patch.object(delivery, 'identity'), patch.object(delivery, 'n13_transport_only', return_value=False):
            delivery.verify_night_readback(self.incoming, readback, headers, checkpoint, {})
            del readback['races'][0]['predeadline_availability']
            with self.assertRaisesRegex(RuntimeError, 'DAILY_SITE_RACE_READBACK:.*:predeadline_availability'):
                delivery.verify_night_readback(self.incoming, readback, headers, checkpoint, {})


class OfficialScopeTests(unittest.TestCase):
    def test_successful_admitted_scope_does_not_hide_an_excluded_official_race(self):
        day='2026-11-11'
        morning={'OPERATIONAL_DATE_JST':day,'RACES':[
            {'operational_date':day,'venue_code':'17','race_no':1},
            {'operational_date':day,'venue_code':'17','race_no':2}],
            'QUARANTINED_RACES':[{'RACE_KEY':'20261111-17-03','REASON':'B_ENTRY_CARDINALITY:17:3:5'}]}
        feed={'operational_date_jst':day,'races':[{'key':'20261111-17-01'},{'key':'20261111-17-02'}]}
        report=delivery.official_scope_coverage(morning,feed,'a'*64)
        self.assertEqual((report['scheduled_races'],report['admitted_races'],report['excluded_races']),(3,2,1))
        self.assertEqual(report['status'],'PARTIAL_OFFICIAL_SCOPE')
        self.assertFalse(report['whole_official_schedule_in_scope'])
        self.assertFalse(report['accounting_mutated'])
        self.assertEqual(report['excluded'],morning['QUARANTINED_RACES'])
        feed['races'].append({'key':'20261111-17-03'})
        with self.assertRaisesRegex(RuntimeError,'OFFICIAL_SCOPE_FEED_BINDING'):
            delivery.official_scope_coverage(morning,feed,'a'*64)

    def test_missing_scope_proof_cannot_default_to_whole_day_coverage(self):
        with self.assertRaisesRegex(RuntimeError,'OFFICIAL_SCOPE_SOURCE_BINDING'):
            delivery.official_scope_coverage({}, {'operational_date_jst':'2026-11-11','races':[]},'a'*64)


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
            self.assertIn('?view=wild4-readback&date='+DAY,command[-1])
            self.assertNotIn('v53-final',command[-1])
            Path(command[command.index('--output')+1]).write_bytes(raw)
            Path(command[command.index('--dump-header')+1]).write_text(
                'HTTP/1.1 200 Connection established\nX-G11-Feed-Sha256: invalid\n\n'
                'HTTP/2 200\nX-G11-Readback-Scope: WILD4_SAVED_PROJECTION\nX-G11-Feed-Sha256: ' + hashlib.sha256(raw).hexdigest() + '\n')
        with patch.object(handoff.subprocess, 'run', side_effect=response):
            self.assertEqual([], handoff.without_published_goripan([self.row], DAY))
        def wrong_hash(command, **kwargs):
            response(command, **kwargs)
            Path(command[command.index('--dump-header')+1]).write_text('HTTP/2 200\n')
        with patch.object(handoff.subprocess, 'run', side_effect=wrong_hash):
            self.assertEqual([self.row], handoff.without_published_goripan([self.row], DAY))
        def wrong_scope(command, **kwargs):
            response(command,**kwargs)
            path=Path(command[command.index('--dump-header')+1])
            path.write_text(path.read_text().replace('WILD4_SAVED_PROJECTION','DISPLAY_ENRICHMENT'))
        with patch.object(handoff.subprocess,'run',side_effect=wrong_scope):
            self.assertEqual([self.row],handoff.without_published_goripan([self.row],DAY))
        with patch.object(handoff.subprocess, 'run', side_effect=subprocess.TimeoutExpired('curl', 8)):
            self.assertEqual([self.row], handoff.without_published_goripan([self.row], DAY))


class NativeIndependentLiveStageTests(unittest.TestCase):
    """Actual frozen projector and V53/store readers, synthetic saved inputs.

    Extract exact reader definitions from the retained private checkout. The
    unrelated SG/research/P3-model services are controlled fixtures; no model,
    official request, production checkpoint or external publication is run.
    """
    def setUp(self):
        self.enterContext(patch.object(early,'official_input_compatibility',return_value=nullcontext()))
        import ast
        import __future__
        import dataclasses
        import hashlib
        import re
        import stat
        from datetime import date, timedelta, timezone
        from typing import Any, Mapping, Sequence
        self.day = '2026-11-15'
        self.key = '20261115-23-02'
        self.now = datetime.fromisoformat(self.day + 'T09:04:00+09:00')
        self.source = Path(os.environ.get('G11_SAVED_PROJECTOR_SOURCE_ROOT',
            str(Path(__file__).resolve().parents[1] / '.g11-private'))).resolve()
        files = {
            'official': 'g11/canonical/official_pipeline.py',
            'public': 'g11/canonical/public_app_feed.py',
            'v53': 'g11/abeken/v53/abeken_shadow_v53_runtime.py',
            'relay': 'g11/relay/v1.py',
            'publisher': 'g11/growth_p3/v1_1/relay_publish_v1.py',
        }
        if not all((self.source / path).is_file() for path in files.values()):
            if os.environ.get('G11_PRIVATE_INTEGRATION_TESTS') == 'true':
                self.fail('required retained saved-projector source is missing')
            self.skipTest('native source runs in the required private contract gate')
        base = dict(Path=Path, json=json, hashlib=hashlib, os=os, re=re,
                    stat=stat, datetime=datetime, date=date, timedelta=timedelta,
                    timezone=timezone, Mapping=Mapping, Sequence=Sequence,
                    Any=Any, dataclass=dataclasses.dataclass, JST=early.JST)
        modules = {}
        for name in ('g11', 'g11.canonical', 'g11.abeken', 'g11.abeken.v1',
                     'g11.abeken.v53', 'g11.relay',
                     'g11.relay.daily_runtime_authority_v1',
                     'g11.canonical.official_pipeline',
                     'g11.canonical.official_st_provenance_v1',
                     'g11.abeken.v1.abeken_shadow_v1',
                     'g11.abeken.v53.abeken_shadow_v53_runtime'):
            modules[name] = ModuleType(name)
            modules[name].__path__ = []
        module_patch = patch.dict(sys.modules, modules)
        module_patch.start()
        self.addCleanup(module_patch.stop)
        for name, module in modules.items():
            if '.' in name:
                parent, child = name.rsplit('.', 1)
                setattr(modules[parent], child, module)

        def load(which, names, module=None):
            module = module or ModuleType('saved_' + which)
            module.__dict__.update(base)
            path = self.source / files[which]
            parsed = ast.parse(path.read_text())
            selected = []
            for node in parsed.body:
                bound = ([node.name] if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                         else [t.id for t in node.targets if isinstance(t, ast.Name)]
                         if isinstance(node, ast.Assign) else [])
                if set(bound) & set(names):
                    selected.append(node)
            self.assertEqual(set(names), {
                name for node in selected for name in
                ([node.name] if hasattr(node, 'name') else
                 [t.id for t in node.targets if isinstance(t, ast.Name)])} & set(names))
            exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), 'exec',
                         flags=__future__.annotations.compiler_flag), module.__dict__)
            return module

        self.official = load('official', ['PipelineError', '_require', '_sha',
            '_json_bytes', '_load_json', '_regular_file', '_safe_directory',
            '_write_exclusive', '_fsync_directory', 'OfficialArtifactStore',
            'SNAPSHOT_SCHEMA', 'VERIFIED_SNAPSHOT_SCHEMA', 'RECEIPT_SCHEMA',
            'RAW38_FIELDS', 'WORK53_FIELDS', 'DISPLAY14_FIELDS', 'LOCK49_FIELDS',
            'WORKBOOK_SCHEMA_SHA256', 'WORKBOOK_PROJECTION_SCHEMA',
            'PREDEADLINE_WORKBOOK_PROJECTION_SCHEMA', 'PREDEADLINE_UPDATE_FIELDS',
            'PREDEADLINE_CANDIDATE16_FIELDS', '_fixed_row_sha', '_fixed_matrix_sha',
            '_canonical_cell_text', '_validated_odds_archive_record',
            'build_predeadline_workbook_projection'],
            modules['g11.canonical.official_pipeline'])
        self.official.PREDEADLINE_AVAILABILITY = SimpleNamespace(
            public_by_race=lambda *a: {}, EFFECTIVE_FROM=date(2026, 11, 11))
        self.v53 = load('v53', ['RUNTIME_LOGIC_VERSION', 'RUNTIME_DISPLAY_VERSION',
            'RUNTIME_STATUS', 'RESEARCH_PROGRESS_PERCENT', 'SECOND_STATES', 'THIRD_STATES',
            'V53ContractError', '_boat', '_boats', 'ConditionalRoute', 'form_buy_set',
            'validate_decision', '_restore_json_graph'],
            modules['g11.abeken.v53.abeken_shadow_v53_runtime'])
        old = modules['g11.abeken.v1.abeken_shadow_v1']
        old.__dict__.update(MODEL_VERSION='SYNTHETIC', CURRENT_REPRODUCTION_ESTIMATE=45,
            PROJECT='SYNTHETIC', OBSERVATION_IDS=(), BUY_LOGIC_SHA256='f' * 64)
        modules['g11.relay.daily_runtime_authority_v1'].authority_for_store = lambda *_: {
            'abeken_v53': {'logic_version': self.v53.RUNTIME_LOGIC_VERSION,
                          'version': self.v53.RUNTIME_DISPLAY_VERSION}}
        modules['g11.canonical.official_st_provenance_v1'].reconcile = lambda *a, **k: {}
        self.feedlib = load('public', ['LOCK49_FIELDS', 'ABEKEN_RACE_SCHEMA',
            'ABEKEN_STATUS_CAPABILITY', '_abeken_public_projection', '_apply_predeadline'])
        self.feedlib._require = early.require
        self.feedlib._predeadline_prediction_invalidations = lambda: {}
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = self.root / 'store'
        (self.store / 'snapshots/morning').mkdir(parents=True)
        (self.store / 'snapshots/predeadline').mkdir()
        response = self.store / 'responses/synthetic-beforeinfo'
        response.mkdir(parents=True)
        self.raw = response / 'response.bin'
        self.raw.write_bytes(b'synthetic official response fixture; no real race input')
        self.receipt = {'SCHEMA': self.official.RECEIPT_SCHEMA,
            'SOURCE_ID': 'synthetic-beforeinfo', 'SIZE_BYTES': self.raw.stat().st_size,
            'SHA256': hashlib.sha256(self.raw.read_bytes()).hexdigest(),
            'CAPTURED_AT_JST': self.day + 'T09:03:00+09:00'}
        (response / 'receipt.json').write_bytes(self.official._json_bytes(self.receipt))
        self.artifact = self.official.OfficialArtifactStore(self.store)
        values = {'標準レースキー': self.key, '日付': self.day,
                  '締切': '09:10', '正式/除外': 'FORMAL', '場': 'Synthetic', 'R': 2}
        values['朝行SHA'] = self.official._fixed_row_sha(
            values.get(field) for field in self.feedlib.LOCK49_FIELDS if field != '朝行SHA')
        self.morning = {'SCHEMA': self.official.WORKBOOK_PROJECTION_SCHEMA,
            'STATUS': 'WORKBOOK_PROJECTION_COMPLETE', 'OPERATIONAL_DATE_JST': self.day,
            'WORKBOOK_SCHEMA_SHA256': self.official.WORKBOOK_SCHEMA_SHA256,
            'WORKBOOK_PROFILE_SHA256': 'e' * 64, 'MORNING_ODDS_USED': False,
            'CONFIDENCE_GENERATED': False, 'PURCHASE_EXECUTED': False,
            'AUTHORITY_ISSUED': 0, 'AUTHORITY_CONSUMED': 0,
            'DISPLAY14_FIELDS': list(self.official.DISPLAY14_FIELDS),
            'DISPLAY14_ROWS': [[values.get(field) for field in self.official.DISPLAY14_FIELDS]],
            'COUNTS': {'RACES': 1, 'FORMAL': 1, 'DISPLAY': 1, 'LOCK': 1},
            'LOCK49_FIELDS': list(self.feedlib.LOCK49_FIELDS),
            'LOCK49_ROWS': [[values.get(field) for field in self.feedlib.LOCK49_FIELDS]]}
        self.artifact.commit_snapshot('morning', 'workbook-projection', self.morning, [self.receipt])
        self.artifact.commit_snapshot('morning', 'official-day-input',
                                      {'OPERATIONAL_DATE_JST': self.day}, [self.receipt])
        self.artifact.commit_snapshot('morning', 'current-growth-p3-morning-' + self.key,
                                      {'RACE_ID': self.key}, [self.receipt])
        pin = self.store / 'abeken-v53-daily-version' / (self.day + '.json')
        pin.parent.mkdir()
        pin.write_text(json.dumps({'DAILY_ABEKEN_SHADOW_VERSION': self.v53.RUNTIME_LOGIC_VERSION}))
        self.publication = {'STATUS': 'PASS', 'READ_ONLY': True, 'LIVE_BY_RACE': {},
            'LIVE_READBACK': {'STATUS': 'PASS', 'READBACK_RACES': 0, 'SNAPSHOT_SHA256': {}}}
        current = SimpleNamespace(snapshot_name=lambda key: 'current-growth-p3-morning-' + key)
        def morning_reader(store, keys):
            hashes = {key: store.load_verified_snapshot('morning', current.snapshot_name(key))[
                'SNAPSHOT_SHA256'] for key in keys}
            return {key: {} for key in keys}, {'STATUS': 'PASS', 'EXPECTED_RACES': len(keys),
                'READBACK_RACES': len(keys), 'SNAPSHOT_SHA256': hashes}
        current.load_public_morning_projection = morning_reader
        current.load_public_live_projection = lambda *a: (
            copy.deepcopy(self.publication['LIVE_BY_RACE']),
            copy.deepcopy(self.publication['LIVE_READBACK']))
        self.publisher = load('publisher', ['_locked_race_keys', 'load_locked_publication'])
        self.publisher.__dict__.update(current=current, feedlib=self.feedlib,
            OfficialArtifactStore=self.official.OfficialArtifactStore,
            verification=SimpleNamespace(read_verified_records=lambda *a, **k: {}),
            _require=early.require)
        self.relay = load('relay', ['build_feed', '_morning_projection', '_json', '_write_json', '_sha'])
        self.relay.__dict__.update(_require=early.require, feedlib=self.feedlib,
            OfficialArtifactStore=self.official.OfficialArtifactStore,
            _night_sg_race_keys=lambda *a: (set(), {'OFFICIAL_B_REPARSED': True}),
            predeadline_availability=SimpleNamespace(public_by_race=lambda *a: {}),
            build_predeadline_workbook_projection=self.official.build_predeadline_workbook_projection,
            _display_research=lambda store, keys: ({}, {key: {} for key in keys}),
            _canonical_authorities=lambda *a: {'capability': 'SYNTHETIC', 'payload': {}},
            _now_jst=lambda: self.now,
            current_growth_p3=SimpleNamespace(apply_to_feed=lambda rows, pub: [
                row.update(predeadline=copy.deepcopy(pub['LIVE_BY_RACE'].get(row['key'])))
                for row in rows]))
        self.feedlib._deadline = lambda day, at: day + 'T' + at + ':00+09:00'
        self.feedlib._base_races = lambda *a: [{'key': self.key,
            'deadline_jst': self.day + 'T09:10:00+09:00', 'formal_status': 'FORMAL',
            'research_eligible': True, 'predeadline': None}]
        self.feedlib._counts = lambda *a: {'races': 1, 'formal': 1, 'excluded': 0,
                                          'results': 0, 'pending': 1}
        for name in ('_attach_three_engine_comparison', '_research', '_research_db',
                     '_conditional_finish_research', '_logic_candidate_research'):
            setattr(self.feedlib, name, lambda *a: {})
        self.auth = {'authority_sha256': 'a' * 64, 'runtime_source_sha': 'b' * 40}
        self.meta = {'OPERATIONAL_DATE_JST': self.day, 'CRITICAL': {
            'OPERATIONAL_DATE': self.day, 'CRITICAL_CHECKPOINT_STATUS': 'PASS',
            'LAST_CRITICAL_RUN_ID': '123', 'LAST_TIMELY_P3_LIVE_COUNT': 0,
            'LAST_TIMELY_V53_COUNT': 0, 'AUTHORITY_SHA256': 'a' * 64,
            'RUNTIME_SOURCE_SHA': 'b' * 40}}
        self.module = SimpleNamespace(verify_runtime=lambda *a: self.auth,
            verify_locks=self.verify_locks, feed_binding=lambda *a: {'immutable': True})
        self.out = self.root / 'critical-feed.json'
        self.seal_fixture()

    def seal_fixture(self):
        self.sealed = {p: p.read_bytes() for p in (self.store / 'snapshots').rglob('*.json')}

    def verify_locks(self, *a, **k):
        if any(not p.is_file() or p.read_bytes() != value for p, value in self.sealed.items()):
            raise RuntimeError('LOCK_CHANGED')

    def save_v53(self):
        decision = {'MAIN_HEAD': 1, 'SUB_HEAD_LIST': [], 'HEAD_POOL': [1],
            'HEAD_RIGHT': {'1': 'MAIN'}, 'SECOND_RIGHT': {'1': {'2': 'NORMAL'}},
            'THIRD_RIGHT': {'1-2': {'3': 'NORMAL'}}, 'SPECIAL_POSITION_ROUTE': [],
            'BUY_SET': ['1-2-3'], 'BUY_COUNT': 1,
            'RUNTIME_LOGIC_VERSION': self.v53.RUNTIME_LOGIC_VERSION,
            'MARK': {}, 'WIN_RIGHT': {}, 'FOLLOW_RIGHT': {}, 'ELIGIBLE_HIMO_POOL': [2, 3]}
        result = {'STATUS': 'READY', 'SNAPSHOT_TYPE': 'MORNING',
            'RUNTIME_LOGIC_VERSION': self.v53.RUNTIME_LOGIC_VERSION,
            'RACE_ID': self.key, 'RACE_DATE': self.day, 'LEGACY_45_FALLBACK': False,
            'LOCKED_AT': self.day + 'T06:00:00+09:00', 'DECISION': decision}
        payload = {'RACE_KEY': self.key, 'FORMAL_USE': True,
            'RESULT_FEEDBACK_TO_MODEL': False, 'P3_INPUT_USED': False, 'WILD_INPUT_USED': False,
            'RESULT': result}
        morning_name = 'abeken-v53-shadow-morning-' + self.key
        identity = self.artifact.commit_snapshot('morning', morning_name, payload, [self.receipt])
        result = {**result, 'SNAPSHOT_TYPE': 'PRE_RACE',
            'LOCKED_AT': self.day + 'T09:03:00+09:00',
            'PRE_RACE_DATA_CAPTURED_AT': self.day + 'T09:03:00+09:00',
            'UPDATE_MODES': ['KEEP_MORNING_RIGHT'],
            'MORNING_TO_PRE_RACE_DIFF': {'BUY_SET': {'MORNING': ['1-2-3']}}}
        payload = {**payload, 'RESULT': result, 'MORNING_V53_SNAPSHOT_SHA256': identity['SHA256']}
        self.live_name = 'abeken-v53-shadow-live-' + self.key
        self.live_path = self.store / 'snapshots/predeadline' / (self.live_name + '.json')
        self.artifact.commit_snapshot('predeadline', self.live_name, payload, [self.receipt])
        self.meta['CRITICAL']['LAST_TIMELY_V53_COUNT'] = 1
        self.seal_fixture()

    def project(self):
        (self.root / 'relay-state.json').write_text(json.dumps(self.meta))
        return early.project_saved(self.root, self.day, '123', self.source, self.out,
            authority_module=self.module, relay=self.relay, publisher=self.publisher, now=self.now)

    def test_actual_v53_only_projector_publishes_saved_live_and_preserves_all_locks(self):
        self.save_v53()
        original = self.relay.build_feed
        baseline = original(self.store, run_id=123, github_sha='b' * 40,
            current_p3_publication=self.publisher.load_locked_publication(self.store, self.morning, self.now),
            relay_phase='predeadline')
        self.assertEqual('MORNING', baseline['stage'])
        self.assertIsNone(baseline['races'][0]['abeken_shadow']['v53']['live'])
        with patch.object(self.official.OfficialArtifactStore, 'commit_snapshot',
                          side_effect=AssertionError('read-only publication cannot create a LOCK')):
            self.assertTrue(self.project())
        feed = json.loads(self.out.read_text())
        self.assertEqual('PREDEADLINE', feed['stage'])
        live = feed['races'][0]['abeken_shadow']['v53']['live']
        self.assertEqual(['1-2-3'], live['practical_bets'])
        self.assertEqual(self.day + 'T09:03:00+09:00', live['locked_at_jst'])
        self.assertIsNone(feed['races'][0]['predeadline'])
        self.assertIs(original, self.relay.build_feed)
        self.verify_locks()

    def test_actual_saved_only_retry_includes_prior_v53_without_new_lock(self):
        self.save_v53()
        self.meta['CRITICAL']['LAST_TIMELY_V53_COUNT'] = 0
        self.assertTrue(self.project())
        self.verify_locks()

    def test_actual_v53_only_projection_keeps_optional_research_isolated(self):
        self.save_v53()
        self.relay._display_research = Mock(side_effect=ValueError('DISPLAY_RACE_PARTITION'))
        self.assertTrue(self.project())
        feed = json.loads(self.out.read_text())
        self.assertEqual('PREDEADLINE', feed['stage'])
        self.assertEqual('TECHNICAL_HOLD', feed['original_display_research']['status'])
        self.assertIsNotNone(feed['races'][0]['abeken_shadow']['v53']['live'])
        self.verify_locks()

    def test_actual_core_projection_failure_is_never_isolated(self):
        self.save_v53()
        self.relay.build_feed.__globals__['build_predeadline_workbook_projection'] = Mock(
            side_effect=ValueError('CORE_PROJECTION_BINDING'))
        with self.assertRaisesRegex(ValueError, 'CORE_PROJECTION_BINDING'): self.project()
        self.assertFalse(self.out.exists())

    def test_actual_p3_only_native_stage_uses_matching_readback(self):
        self.publication['LIVE_BY_RACE'] = {self.key: {'status': 'READY',
            'snapshot_sha256': 'c' * 64, 'captured_at_jst': self.day + 'T09:03:00+09:00'}}
        self.publication['LIVE_READBACK'].update(READBACK_RACES=1,
            SNAPSHOT_SHA256={self.key: 'c' * 64})
        self.meta['CRITICAL']['LAST_TIMELY_P3_LIVE_COUNT'] = 1
        self.assertTrue(self.project())
        self.assertEqual('PREDEADLINE', json.loads(self.out.read_text())['stage'])
        self.verify_locks()

    def test_actual_no_live_stays_morning_and_does_not_publish(self):
        self.assertFalse(self.project())
        self.assertFalse(self.out.exists())

    def test_actual_v53_response_hash_corruption_fails_before_output(self):
        self.save_v53()
        self.raw.write_bytes(b'corrupted')
        with self.assertRaisesRegex(self.official.PipelineError, 'RESPONSE_RECEIPT_READBACK'):
            self.project()
        self.assertFalse(self.out.exists())

    def test_actual_wrong_v53_day_and_invalid_decision_cannot_open_stage(self):
        self.save_v53()
        original = self.live_path.read_bytes()
        for kind in ('day', 'decision', 'morning_hash', 'late'):
            value = json.loads(original)
            result = value['PAYLOAD']['RESULT']
            if kind == 'day': result['RACE_DATE'] = '2026-11-14'
            elif kind == 'decision': result['DECISION']['BUY_SET'] = ['1-2-4']
            elif kind == 'morning_hash': value['PAYLOAD']['MORNING_V53_SNAPSHOT_SHA256'] = 'd' * 64
            else: result['LOCKED_AT'] = self.day + 'T09:10:00+09:00'
            self.live_path.write_bytes(self.official._json_bytes(value))
            self.seal_fixture()
            with self.subTest(kind=kind), self.assertRaises((RuntimeError, ValueError)):
                self.project()
            self.assertFalse(self.out.exists())

    def test_actual_changed_lock_and_p3_readback_mismatch_fail_closed(self):
        self.save_v53()
        self.live_path.write_bytes(self.live_path.read_bytes() + b' ')
        with self.assertRaisesRegex(RuntimeError, 'LOCK_CHANGED'): self.project()
        self.live_path.write_bytes(self.sealed[self.live_path])
        self.publication['LIVE_BY_RACE'] = {self.key: {'status': 'READY',
            'snapshot_sha256': 'c' * 64, 'captured_at_jst': self.day + 'T09:03:00+09:00'}}
        self.publication['LIVE_READBACK'].update(READBACK_RACES=1,
            SNAPSHOT_SHA256={self.key: 'd' * 64})
        with self.assertRaisesRegex(RuntimeError, 'LIVE_P3_BINDING'): self.project()
        self.assertFalse(self.out.exists())


if __name__ == '__main__':
    unittest.main()



