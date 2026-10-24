"""Exercise the reviewed, real collector with filesystem/clock/model doubles.

G11_ISOLATION_SOURCE_DIR may point to a read-only source export for local tests.
CI uses the already restored private runtime; no private source is vendored here.
"""
import ast
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import predeadline_race_isolation_v1 as adapter

JST = ZoneInfo('Asia/Tokyo')
BASE = datetime(2026, 10, 24, 13, 55, tzinfo=JST)
BAD, GOOD = '20261024-17-09', '20261024-22-07'
IDS = ('WILD_HEAD', 'WILD_SECOND', 'WILD_THIRD')


class InputError(ValueError):
    pass


class GateError(RuntimeError):
    pass


def require(value, code):
    if not value:
        raise GateError(code)


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True,
        separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def load_functions(relative, names, namespace):
    override = os.environ.get('G11_ISOLATION_SOURCE_DIR')
    path = (Path(override) / Path(relative).name if override else
            Path(__file__).resolve().parents[1] / '.g11-private' / relative)
    source = path.read_text()
    tree = ast.parse(source, filename=str(path))
    tree.body = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in names]
    namespace = dict(namespace)
    exec(compile(tree, str(path), 'exec', flags=__import__('__future__').annotations.compiler_flag), namespace)
    return namespace


class IsolationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.current = BASE
        self.calls, self.reports, self.writes = [], [], []
        self.bad = {BAD}
        self.late = set()
        self.corrupt = set()
        self.plan = []
        self.features = []
        for key in (BAD, GOOD):
            for kind in ('BEFOREINFO', 'ODDS3T'):
                sid = kind.lower() + '-' + key
                self.plan.append({'RACE_KEY': key, 'KIND': kind, 'SOURCE_ID': sid,
                    'DUE_AT_JST': (BASE-timedelta(minutes=1)).isoformat(),
                    'MUST_FINISH_BEFORE_JST': (BASE+timedelta(minutes=6)).isoformat()})
                path = self.root/'snapshots/predeadline'/f'{sid}.json'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{}')
            self.features.append({'RACE_ID': key, 'MORNING_FEATURE_SNAPSHOT_SHA256': 'a'*64,
                                  'FEATURE_ASOF_TIME': BASE.isoformat()})
        admission = self.root/'g11/wild-pack-oos/morning/2026-10-24/position-oos-admission.manifest.json'
        admission.parent.mkdir(parents=True)
        admission.write_text('{}')
        owner = self
        class Clock:
            @staticmethod
            def now(tz):
                return owner.current
            fromisoformat = staticmethod(datetime.fromisoformat)
        class Store:
            def __init__(self, root): pass
            def load_verified_snapshot(self, stage, source):
                return {'SNAPSHOT': {'PAYLOAD': {'PREDEADLINE_PLAN': owner.plan}}}
        self.official = types.ModuleType('g11.canonical.official_pipeline')
        self.official.OfficialArtifactStore = Store
        self.modules = patch.dict(sys.modules, {'g11.canonical.official_pipeline': self.official})
        self.modules.start(); self.addCleanup(self.modules.stop)
        ns = {'Path': Path, 'datetime': Clock, 'JST': JST, '_require': require,
            '_true_ai_day_root': lambda *_: self.root/'features',
            '_load_manifest_artifact': lambda _: ({'ARTIFACT_SHA256': 'b'*64}, {
                'SCHEMA': 'G11_TRUE_AI_MORNING_FEATURE_BATCH_V1',
                'OPERATIONAL_DATE_JST': '2026-10-24', 'RECORDS': self.features,
                'RACE_COUNT': len(self.features)}),
            '_verified_pre_race_source': self.verified,
            '_json': lambda p: json.loads(p.read_text()), '_file_sha': lambda p: hashlib.sha256(p.read_bytes()).hexdigest(),
            '_canonical_sha': sha, 'pre_position': types.SimpleNamespace(predict_live=self.predict),
            'prospective': types.SimpleNamespace(_write_json_atomic=self.write),
            'PRE_RACE_PACKET_SCHEMA': 'G11_WILD_PRE_RACE_COLLECTION_PACKET_V1',
            'PRE_RACE_WILD_IDS': IDS, 'STATUS_FILE': 'status.json',
            '_pre_race_status': lambda store, root, n: {'partial': n},
        }
        self.ns = load_functions('g11/wild_pack/v1/runtime_orchestrator_v1.py',
            {'_collect_pre_race_packets', 'run_predeadline'}, ns)
        self.original = self.ns['_collect_pre_race_packets']
        self.collect = adapter._compile(self.original, adapter.COLLECT_SHA,
            adapter._collect_source, {'_isolated_input_errors': (InputError, GateError),
                '_isolation_report': lambda *args: self.reports.append(args)})

    def verified(self, artifacts, source, key, kind):
        if key in self.corrupt:
            raise GateError('PRE_RACE_SOURCE_SHA:' + source)
        return {'CAPTURED_AT_JST': BASE.isoformat(),
                'PARSED': {'START_TIMING': None if key in self.bad else .13}}, 'c'*64

    def predict(self, parsed, key, captured, deadline, model, morning):
        self.calls.append((key, dict(parsed)))
        if parsed['START_TIMING'] is None:
            raise InputError('RUNTIME_START_TIMING')
        if key in self.late:
            self.current = datetime.fromisoformat(deadline)
        return {**{i: {'MODEL_VERSION': i+'-unchanged', 'CURRENT_ID': i+'-current'} for i in IDS},
                'SELECTED': {'HEAD': 1, 'SECOND': 2, 'THIRD': 3},
                'FULL_PATH_COUNTS': {'HEAD': 6, 'SECOND': 30, 'THIRD': 120}}

    def write(self, path, value):
        self.writes.append(path.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True))

    def packet(self, key):
        return self.root/'g11/wild-pack-oos/pre_race/2026-10-24'/f'collection-packet-{key}.json'

    def test_real_regression_bad_first_race_no_longer_starves_next_venue(self):
        with self.assertRaises(InputError):
            self.original(self.root, BASE.date())
        self.assertFalse(self.packet(GOOD).exists())
        self.assertTrue(self.collect(self.root, BASE.date()))
        self.assertFalse(self.packet(BAD).exists())
        self.assertTrue(self.packet(GOOD).exists())
        self.assertEqual(self.reports[-1][3], [{'RACE_KEY': BAD, 'REASON': 'RUNTIME_START_TIMING'}])
        self.assertIsNone(self.calls[-2][1]['START_TIMING'])

    def test_successful_prediction_bytes_match_original(self):
        self.bad.clear()
        self.original(self.root, BASE.date())
        expected = {key: self.packet(key).read_bytes() for key in (BAD, GOOD)}
        for key in expected: self.packet(key).unlink()
        self.collect(self.root, BASE.date())
        self.assertEqual(expected, {key: self.packet(key).read_bytes() for key in expected})

    def test_existing_lock_is_not_recalculated_or_rewritten(self):
        self.collect(self.root, BASE.date())
        raw = self.packet(GOOD).read_bytes(); self.calls.clear(); self.writes.clear()
        self.collect(self.root, BASE.date())
        self.assertEqual(raw, self.packet(GOOD).read_bytes())
        self.assertNotIn(GOOD, [key for key, _ in self.calls])
        self.assertNotIn(self.packet(GOOD).name, self.writes)

    def test_completion_at_deadline_cannot_create_a_lock(self):
        self.bad.clear(); self.late.add(BAD)
        self.collect(self.root, BASE.date())
        self.assertFalse(self.packet(BAD).exists())
        self.assertFalse(self.packet(GOOD).exists())
        self.assertIn('PRE_RACE_SAVE_TIME_SAFE', self.reports[-1][3][0]['REASON'])

    def test_already_closed_races_never_call_predictor(self):
        self.current = BASE + timedelta(minutes=7)
        self.collect(self.root, BASE.date())
        self.assertEqual([], self.calls)

    def test_slow_race_cannot_block_a_later_deadline(self):
        self.bad.clear(); self.late.add(BAD)
        for task in self.plan:
            if task['RACE_KEY'] == GOOD:
                task['MUST_FINISH_BEFORE_JST'] = (BASE+timedelta(minutes=20)).isoformat()
        self.collect(self.root, BASE.date())
        self.assertFalse(self.packet(BAD).exists())
        self.assertTrue(self.packet(GOOD).exists())
        self.assertEqual((BASE+timedelta(minutes=6)).isoformat(),
                         json.loads(self.packet(GOOD).read_text())['PREDICTION_AT'])

    def test_source_hash_failure_is_not_accepted_or_spread(self):
        self.bad.clear(); self.corrupt.add(BAD)
        self.collect(self.root, BASE.date())
        self.assertFalse(self.packet(BAD).exists())
        self.assertTrue(self.packet(GOOD).exists())
        self.assertNotIn(BAD, [key for key, _ in self.calls])

    def test_corrupt_existing_lock_is_never_overwritten(self):
        path = self.packet(BAD); path.parent.mkdir(parents=True); path.write_text('{}')
        self.collect(self.root, BASE.date())
        self.assertEqual('{}', path.read_text())
        self.assertTrue(self.packet(GOOD).exists())
        self.assertIn('PRE_RACE_PACKET_READBACK', self.reports[-1][3][0]['REASON'])

    def test_global_manifest_failure_remains_failure(self):
        self.features.append(self.features[0])
        with self.assertRaisesRegex(GateError, 'PRE_RACE_MORNING_FEATURE_DUPLICATE'):
            self.collect(self.root, BASE.date())
        self.assertEqual([], self.writes)

    def test_unexpected_programming_error_is_not_hidden(self):
        self.collect.__globals__['pre_position'] = types.SimpleNamespace(
            predict_live=lambda *_: (_ for _ in ()).throw(TypeError('bug')))
        with self.assertRaisesRegex(TypeError, 'bug'):
            self.collect(self.root, BASE.date())

    def test_independent_bet_odds_save_precedes_optional_model_failure(self):
        manifest = self.root/'g11/wild-pack-oos/morning/2026-10-24/bet-upstream.manifest.json'
        manifest.write_text('{}')
        events = []
        fn = adapter._compile(self.ns['run_predeadline'], adapter.BET_HANDOFF_SHA,
            adapter._handoff_source, {
                '_load_manifest_artifact': lambda _: ({}, {'RECORDS': []}),
                '_write_statuses': lambda _: events.append('status'),
                'bet': types.SimpleNamespace(attach_prediction_time_odds=lambda *_: {},
                    persist=lambda *_: events.append('odds_saved')),
                '_collect_pre_race_packets': lambda *_: (_ for _ in ()).throw(InputError('model')),
            })
        with self.assertRaisesRegex(InputError, 'model'):
            fn(self.root, BASE.date())
        self.assertEqual(['odds_saved', 'status'], events)

    def test_unreviewed_source_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'UNREVIEWED_SOURCE'):
            adapter._compile(self.original, '0'*64, adapter._collect_source)

    def test_display_validation_failure_is_recorded_and_next_race_continues(self):
        root = self.root/'display'; (root/'morning').mkdir(parents=True)
        (root/'manifest.json').write_text('{}')
        for key in (BAD, GOOD): (root/'morning'/f'{key}.json').write_text('{}')
        control = {'FAILURES': {}}; saved = []
        url = lambda key: 'https://example.test/' + key
        response = types.SimpleNamespace(read=lambda _: b'official', geturl=lambda: url('response'))
        def audit(store, key, *args):
            if key == BAD: raise InputError('BEFOREINFO_TIMING_FIELDS')
            return {'RACE_KEY': key}
        ns = load_functions('g11/canonical/original_display_shadow.py', {'capture_due'}, {
            '_shadow_root': lambda _: root, '_require': require, 'JST': JST,
            'datetime': datetime, 'timedelta': timedelta, 'Path': Path,
            '_load_control': lambda _: control, '_deadline_by_key': lambda _: {
                key: BASE+timedelta(minutes=6) for key in (BAD, GOOD)},
            'original_display_url': url, 'urlparse': __import__('urllib.parse', fromlist=['urlparse']).urlparse,
            'OFFICIAL_HOST': 'example.test', 'USER_AGENT': 'test',
            'Request': lambda *args, **kwargs: object(), 'urlopen': lambda *args, **kwargs: response,
            'parse_original_display_text': lambda _: {}, 'HTTPError': __import__('urllib.error', fromlist=['HTTPError']).HTTPError,
            'URLError': __import__('urllib.error', fromlist=['URLError']).URLError,
            'ShadowError': InputError, '_sha': lambda _: 'a'*64, 'CAPTURE_RECEIPT_SCHEMA': 'receipt',
            'build_capture_audit': audit, '_write_immutable': lambda path, _: saved.append(path),
            '_json_bytes': lambda x: json.dumps(x).encode(), '_append_control': lambda *_: None,
        })
        fn = adapter._compile(ns['capture_due'], adapter.DISPLAY_SHA, adapter._display_source)
        result = fn(self.root, now=BASE)
        self.assertEqual([GOOD], result['ACQUIRED'])
        self.assertEqual([{'RACE_KEY': BAD, 'REASON': 'BEFOREINFO_TIMING_FIELDS'}], result['FAILED'])
        self.assertEqual('BEFOREINFO_TIMING_FIELDS', control['FAILURES'][BAD]['REASON'])
        self.assertEqual(3, len(saved))


class FollowupTest(unittest.TestCase):
    def setUp(self):
        self.now = BASE + timedelta(minutes=2)
        self.critical = {'OPERATIONAL_DATE': '2026-10-24',
            'CRITICAL_CHECKPOINT_STATUS': 'PASS', 'CRITICAL_CAPTURE_AT': BASE.isoformat()}
        self.morning = {'OPERATIONAL_DATE_JST': '2026-10-24', 'PREDEADLINE_PLAN': []}
        self.snapshots = {}
        for key, captured in [(BAD, BASE-timedelta(seconds=15)), (GOOD, BASE+timedelta(minutes=1))]:
            name = 'odds3t-' + key
            self.morning['PREDEADLINE_PLAN'].append({'RACE_KEY': key, 'SOURCE_ID': name,
                'KIND': 'ODDS3T', 'MUST_FINISH_BEFORE_JST': (BASE+timedelta(minutes=6)).isoformat()})
            self.snapshots[name] = {'SNAPSHOT': {'PAYLOAD': {
                'STATUS': 'PREDEADLINE_SOURCE_COMPLETE', 'RACE_KEY': key, 'KIND': 'ODDS3T',
                'CAPTURED_AT_JST': captured.isoformat()}}}

    def choose(self, early=True):
        return adapter.followup_odds(self.morning, self.critical, self.snapshots.get, self.now, early)

    def test_accepted_early_dispatch_does_not_suppress_new_saved_odds(self):
        self.assertEqual([GOOD], [r['RACE_KEY'] for r in self.choose()])

    def test_failed_early_handoff_retries_all_unexpired_verified_inputs(self):
        self.assertEqual([BAD, GOOD], [r['RACE_KEY'] for r in self.choose(False)])

    def test_no_new_source_does_not_queue_duplicate_job(self):
        self.snapshots.pop('odds3t-' + GOOD)
        self.assertEqual([], self.choose())

    def test_expired_inputs_are_never_handed_off_for_prediction(self):
        self.now = BASE+timedelta(minutes=7)
        self.assertEqual([], self.choose(False))

    def test_wrong_day_and_future_capture_cannot_trigger_handoff(self):
        self.morning['OPERATIONAL_DATE_JST'] = '2026-10-23'
        with self.assertRaisesRegex(ValueError, 'DAY_BINDING'): self.choose()
        self.morning['OPERATIONAL_DATE_JST'] = '2026-10-24'
        self.snapshots['odds3t-' + GOOD]['SNAPSHOT']['PAYLOAD']['CAPTURED_AT_JST'] = (self.now+timedelta(seconds=1)).isoformat()
        with self.assertRaisesRegex(ValueError, 'SNAPSHOT_TIME'): self.choose()

    def test_wrong_source_or_unverified_critical_is_rejected(self):
        self.critical['CRITICAL_CHECKPOINT_STATUS'] = 'PARTIAL'
        with self.assertRaisesRegex(ValueError, 'CRITICAL_BINDING'): self.choose()
        self.critical['CRITICAL_CHECKPOINT_STATUS'] = 'PASS'
        self.snapshots['odds3t-' + GOOD]['SNAPSHOT']['PAYLOAD']['RACE_KEY'] = BAD
        with self.assertRaisesRegex(ValueError, 'SNAPSHOT_BINDING'): self.choose()

    def test_followup_is_after_verified_save_and_before_site_publication(self):
        source = (Path(__file__).resolve().parents[1]/'.github/workflows/g11-free-runner.yml').read_text()
        self.assertLess(source.index('id: publication-state-verified'), source.index('id: wild4-followup'))
        self.assertLess(source.index('id: wild4-followup'), source.index('id: publish\n'))
        step = source.split('id: wild4-followup\n', 1)[1].split('      - name:', 1)[0]
        self.assertIn("steps.publication-state-verified.outputs.ready=='true'", step)
        self.assertIn('predeadline_race_isolation_v1.py handoff', step)

    def test_dispatch_claims_sent_only_after_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)/'output'; calls = []
            adapter.dispatch_followup(self.choose(), '2026-10-24', output,
                run=lambda args, **kw: calls.append((args, kw)))
            self.assertEqual('needed=true\nsent=true\n', output.read_text())
            self.assertEqual('operational_date=2026-10-24', calls[0][0][-1])
            self.assertTrue(calls[0][1]['check'])

    def test_failed_dispatch_retains_retry_signal_without_false_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)/'output'
            with self.assertRaises(RuntimeError):
                adapter.dispatch_followup(self.choose(), '2026-10-24', output,
                    run=lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('dispatch failed')))
            self.assertEqual('needed=true\n', output.read_text())

    def test_empty_followup_never_calls_receiver(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)/'output'
            adapter.dispatch_followup([], '2026-10-24', output,
                run=lambda *a, **kw: self.fail('unexpected dispatch'))
            self.assertEqual('needed=false\n', output.read_text())


if __name__ == '__main__':
    unittest.main()
