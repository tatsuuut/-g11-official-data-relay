"""Regression for an excluded novice race blocking every critical checkpoint."""
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import inspect
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import critical_formal_lock_scope_v1 as scope

FIELDS = ['標準レースキー', '正式/除外', '除外理由']
FORMAL = '20261101-14-01'
EXCLUDED = '20261101-22-01'


class ScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Path(temporary.name)
        self.root = self.store / 'snapshots/morning'
        self.root.mkdir(parents=True)
        self.lock = self.root / ('current-growth-p3-' + FORMAL + '.json')
        self.lock.write_text('retained immutable morning prediction\n')
        self.projection = {'LOCK49_FIELDS': FIELDS, 'LOCK49_ROWS': [
            [FORMAL, 'FORMAL', None], [EXCLUDED, 'EXCLUDED', 'BODY_NOT_GENERATED']]}

    def missing(self, value=None):
        return scope.missing_formal_locks(value or self.projection, self.store,
                                         lambda key: 'current-growth-p3-' + key, FIELDS)

    def test_explicit_exclusion_does_not_require_or_create_prediction(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        self.assertEqual(self.missing(), [])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_missing_formal_lock_still_blocks(self):
        self.lock.unlink()
        self.assertEqual(self.missing(), [FORMAL])

    def test_unknown_status_missing_reason_duplicate_or_bad_row_fails_closed(self):
        changes = [
            lambda p: p['LOCK49_ROWS'][1].__setitem__(1, 'UNKNOWN'),
            lambda p: p['LOCK49_ROWS'][1].__setitem__(2, None),
            lambda p: p['LOCK49_ROWS'][1].__setitem__(0, FORMAL),
            lambda p: p['LOCK49_ROWS'][0].__setitem__(0, '../unbound'),
            lambda p: p['LOCK49_ROWS'][1].pop(),
            lambda p: p.__setitem__('LOCK49_FIELDS', list(reversed(FIELDS))),
            lambda p: p.__setitem__('LOCK49_ROWS', []),
        ]
        for change in changes:
            value = deepcopy(self.projection)
            change(value)
            with self.assertRaisesRegex(ValueError, 'CRITICAL_MORNING_'):
                self.missing(value)


class FrozenCriticalIntegrationTests(unittest.TestCase):
    def modules(self):
        try:
            from g11.relay import predeadline_critical_v1 as critical
            from g11.canonical import official_pipeline as official
        except ModuleNotFoundError:
            if os.environ.get('G11_REQUIRE_PRIVATE_CRITICAL_TESTS') == '1':
                raise
            self.skipTest('private runtime is required in CI')
        return critical, official

    def exercise(self, *, missing=False, corrupt=False):
        critical, official = self.modules()
        day = '2026-11-01'
        now = datetime.fromisoformat(day + 'T07:00:00+09:00')
        deadline = day + 'T08:35:00+09:00'
        source_id = 'beforeinfo-' + FORMAL
        fields = list(official.LOCK49_FIELDS)
        rows = []
        for key, status, reason in ((FORMAL, 'FORMAL', None),
                                    (EXCLUDED, 'EXCLUDED', 'BODY_NOT_GENERATED')):
            row = [None] * len(fields)
            for name, value in (('標準レースキー', key), ('正式/除外', status), ('除外理由', reason)):
                row[fields.index(name)] = value
            rows.append(row)
        projection = {'LOCK49_FIELDS': fields, 'LOCK49_ROWS': rows}
        order = []
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        class Store:
            def __init__(self, root): self.root = root
            def load_verified_snapshot(self, stage, name):
                if (stage, name) == ('morning', 'official-day-input'):
                    return {'SNAPSHOT': {'PAYLOAD': {'PREDEADLINE_PLAN': [
                        {'SOURCE_ID': source_id, 'RACE_KEY': FORMAL,
                         'MUST_FINISH_BEFORE_JST': deadline}]}}}
                if (stage, name) == ('morning', 'workbook-projection'):
                    return {'SNAPSHOT': {'PAYLOAD': projection}}
                if (stage, name) == ('predeadline', source_id):
                    return {'SNAPSHOT': {'PAYLOAD': {'CAPTURED_AT_JST':
                        (now - timedelta(seconds=1)).isoformat()}}}
                raise AssertionError((stage, name))
        class Runner:
            def __init__(self, store): pass
            def run_due_predeadline_predictions(self):
                order.append('P3_V53')
                return {'ABEKEN_V53_READY': [FORMAL]}
        def publish(*args, **kwargs):
            self.assertTrue(kwargs['strict_deadline'])
            self.assertFalse(kwargs['include_research'])
            self.assertIs(args[1], projection)
            order.append('CURRENT_P3')
            return {'LIVE_PUBLISH': {'COMPLETED': [
                {'RACE_ID': FORMAL, 'P3_SNAPSHOT_WRITE': 'PASS'}]}}

        original = critical.run
        original_bytes = Path(critical.__file__).read_bytes()
        self.assertEqual(hashlib.sha256(inspect.getsource(original).encode()).hexdigest(), scope.RUN_SHA256)
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            model = state / 'store/g11/wild-pack-models/current.json'
            model.parent.mkdir(parents=True)
            model.write_text('{"fixture":"critical-scope-test-only"}\n')
            authority = critical.daily_runtime.create(state, day,
                Path(critical.__file__).resolve().parents[2], 'a' * 40,
                now=datetime.fromisoformat(day + 'T02:15:00+09:00'))
            morning = state / 'store/snapshots/morning'
            morning.mkdir(parents=True)
            lock = morning / (critical.current_runtime.snapshot_name(FORMAL) + '.json')
            if not missing: lock.write_text('{}\n')
            (morning / 'fixture-input.json').write_text('{"retained":true}\n')
            (state / 'relay-state.json').write_text(json.dumps({
                'OPERATIONAL_DATE_JST': day, 'MORNING_COMPLETE': True}))
            critical.daily_runtime.seal(state, day, 'MORNING')
            critical.daily_runtime.seal(state, day, 'PREDEADLINE', executed=False)
            before = {p.name: p.read_bytes() for p in morning.iterdir()}
            authority_path = state / 'daily-runtime' / day / 'authority.json'
            authority_bytes = authority_path.read_bytes()
            if corrupt: lock.write_text('changed after sealing\n')
            with patch.object(critical, 'datetime', Clock), \
                 patch.object(critical, 'OfficialArtifactStore', Store), \
                 patch.object(critical, 'CanonicalRunner', Runner), \
                 patch.object(critical, 'capture', side_effect=lambda root:
                     order.append('OFFICIAL_CAPTURE') or {'ACQUIRED': [source_id]}), \
                 patch.object(critical.current_p3, 'publish_future', side_effect=publish), \
                 scope.formal_lock_scope(day):
                if missing or corrupt:
                    with self.assertRaises(Exception) as error:
                        critical.run(state, day, 'fixture')
                    if missing:
                        self.assertIn('CRITICAL_MORNING_P3_LOCK_MISSING', str(error.exception))
                    else:
                        self.assertNotIn('CURRENT_P3', order)
                else:
                    result = critical.run(state, day, 'fixture')
                    self.assertEqual(result['STATUS'], 'CRITICAL_CHECKPOINT_PASS')
                    self.assertEqual(result['CRITICAL']['AUTHORITY_SHA256'], authority['authority_sha256'])
                    self.assertEqual(result['CRITICAL']['POST_DEADLINE_GENERATION'], 0)
                    self.assertEqual(result['CRITICAL']['PREDICTION_LOCKED_BEFORE_DEADLINE'], [FORMAL])
                    self.assertEqual(order, ['OFFICIAL_CAPTURE', 'P3_V53', 'CURRENT_P3'])
            self.assertIs(critical.run, original)
            self.assertEqual(authority_path.read_bytes(), authority_bytes)
            if not corrupt:
                self.assertEqual(before, {p.name: p.read_bytes() for p in morning.iterdir()})
        self.assertEqual(Path(critical.__file__).read_bytes(), original_bytes)

    def test_excluded_race_allows_unchanged_critical_capture_publish_and_seal(self):
        self.exercise()

    def test_formal_race_missing_lock_still_fails(self):
        self.exercise(missing=True)

    def test_original_authority_rejects_changed_morning_lock(self):
        self.exercise(corrupt=True)

    def test_historical_runner_and_unknown_source_are_not_adapted(self):
        critical, _ = self.modules()
        original = critical.run
        with scope.formal_lock_scope('2026-10-31'):
            self.assertIs(critical.run, original)
        with patch.object(critical, 'run', self.exercise), self.assertRaisesRegex(ValueError, 'SOURCE_CHANGED'):
            with scope.formal_lock_scope('2026-11-01'):
                self.fail('unreviewed source accepted')
        self.assertIs(critical.run, original)


if __name__ == '__main__':
    unittest.main()
