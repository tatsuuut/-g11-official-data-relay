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


class RegisteredMarketHandoffTests(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.day = '2026-11-27'; self.key = '20261127-01-05'
        self.name = 'venue-market-' + self.key
        path = self.root / 'snapshots/predeadline' / (self.name + '.json')
        path.parent.mkdir(parents=True); path.write_text('verified fixture')
        self.limit = datetime.fromisoformat(self.day+'T20:30:00+09:00')
        self.now = self.limit - timedelta(minutes=8)
        self.raw = b'exact registered alternate market bytes'
        self.receipt = {'HOST':'race.boatcast.jp', 'STATUS':'HTTP_200_BYTE_EXACT_READBACK',
            'SOURCE_ID':'registered-'+self.key, 'SHA256':hashlib.sha256(self.raw).hexdigest(),
            'CAPTURED_AT_JST':(self.limit-timedelta(minutes=9)).isoformat()}
        self.packet = {'STAGE':'PREDEADLINE','NAME':self.name,'SOURCE_RECEIPTS':[self.receipt],
            'PAYLOAD':{'KIND':'ODDS3T','RACE_KEY':self.key,
                'CAPTURED_AT_JST':self.receipt['CAPTURED_AT_JST'],'PARSED':{'ODDS_ROWS':[1]*120}}}
        self.plan = [{'KIND':'ODDS3T','RACE_KEY':self.key,
            'DUE_AT_JST':(self.limit-timedelta(minutes=7)).isoformat(),
            'MUST_FINISH_BEFORE_JST':self.limit.isoformat()}]
        self.morning = {'OPERATIONAL_DATE_JST':self.day}
        def load(stage,name):
            if stage=='morning': return {'SNAPSHOT':{'PAYLOAD':self.morning}}
            self.assertEqual(name,self.name)
            return {'SNAPSHOT':deepcopy(self.packet)}
        self.ledger = {'STOPPED':True}
        self.store = SimpleNamespace(root=self.root,load_verified_snapshot=load,
            ledger=SimpleNamespace(state=lambda:dict(self.ledger)))
        def matches(receipt,key):
            return receipt['HOST'] in ('race.boatcast.jp','www.omurakyotei.jp') and receipt['SOURCE_ID']=='registered-'+key
        self.calls=[]
        def recheck(store,key,limit,roster):
            self.calls.append((key,limit,roster))
            if hashlib.sha256(self.raw).hexdigest()!=self.packet['SOURCE_RECEIPTS'][0]['SHA256']:
                raise ValueError('RAW_READBACK')
            return load('predeadline',self.name)
        self.market=SimpleNamespace(source_matches=matches,recheck=recheck)
        self.preview=SimpleNamespace(race_roster=lambda morning,key:['verified morning roster'])

    def run_handoff(self,prior=(),now=None):
        import types
        canonical=types.ModuleType('g11.canonical')
        canonical.venue_market_v1=self.market
        canonical.published_preview_v1=self.preview
        with patch.dict('sys.modules',{'g11.canonical':canonical}):
            return scope.timely_stadium_sources(self.store,self.day,self.plan,set(prior),now or self.now)

    def test_new_registered_boatcast_market_counts_while_national_stop_remains(self):
        self.assertEqual(self.run_handoff(),[self.name])
        self.assertEqual(self.calls,[(self.key,self.limit,['verified morning roster'])])
        self.assertEqual(self.ledger,{'STOPPED':True})
        self.assertEqual(self.run_handoff([self.name]),[])
        self.assertEqual(self.run_handoff(now=self.limit),[])

    def test_current_omura_registered_source_is_retained(self):
        self.packet['SOURCE_RECEIPTS'][0]['HOST']='www.omurakyotei.jp'
        self.assertEqual(self.run_handoff(),[self.name])

    def test_real_registered_boatcast_parser_and_recheck_match_native_snapshot_format(self):
        try:
            from g11.canonical import boatcast_market_v1 as market, published_preview_v1 as preview
            from g11.canonical.test_boatcast_market_v1 import MemoryStore, ROSTER, board
        except ImportError:
            self.skipTest('actual frozen input modules are supplied by contract CI')
        class Store(MemoryStore):
            def commit_response(self, *args, **kwargs):
                value = super().commit_response(*args, **kwargs)
                value['STATUS'] = 'HTTP_200_BYTE_EXACT_READBACK'
                return value
            def commit_snapshot(self, stage, name, payload, receipts):
                super().commit_snapshot(stage, name, payload, receipts)
                self.snapshots[stage, name]['SNAPSHOT'].update(STAGE=stage.upper(), NAME=name)
            def load_verified_snapshot(self, stage, name):
                if stage == 'morning':
                    return {'SNAPSHOT': {'PAYLOAD': {'OPERATIONAL_DATE_JST': '2026-11-27'}}}
                return super().load_verified_snapshot(stage, name)
        store = Store(self.root/'native-format')
        now = self.limit-timedelta(minutes=3)
        with patch.object(market, 'read', return_value=(board(), now, now-timedelta(seconds=25))):
            market.capture(store, self.key, self.limit, ROSTER, lambda: now)
        with patch.object(preview, 'race_roster', return_value=ROSTER):
            self.assertEqual(scope.timely_stadium_sources(store,self.day,self.plan,set(),now),[self.name])
            sid=next(iter(store.responses))
            raw, receipt=store.responses[sid]
            store.responses[sid]=(raw.replace(b'6.9',b'7.9',1),receipt)
            self.assertEqual(scope.timely_stadium_sources(store,self.day,self.plan,set(),now),[])

    def test_wrong_host_raw_bytes_roster_day_or_race_cannot_signal_handoff(self):
        original=deepcopy(self.packet)
        for changed in ('host','raw','day','race'):
            self.packet=deepcopy(original); self.morning={'OPERATIONAL_DATE_JST':self.day}
            if changed=='host': self.packet['SOURCE_RECEIPTS'][0]['HOST']='www.boatrace.jp'
            if changed=='raw': self.packet['SOURCE_RECEIPTS'][0]['SHA256']='0'*64
            if changed=='day': self.morning['OPERATIONAL_DATE_JST']='2026-11-26'
            if changed=='race': self.packet['PAYLOAD']['RACE_KEY']='20261127-01-06'
            with self.subTest(changed=changed): self.assertEqual(self.run_handoff(),[])

    def test_historical_inventory_does_not_expand(self):
        self.assertEqual(scope.stadium_inventory(self.root,'2026-11-26'),set())


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

    def exercise(self, *, missing=False, corrupt=False, observe=False):
        critical, official = self.modules()
        run_sha = hashlib.sha256(inspect.getsource(critical.run).encode()).hexdigest()
        day = ('2026-11-21' if run_sha == scope.DISPLAY_WARM_RUN_SHA else
               '2026-11-17' if observe else '2026-11-01')
        formal = day.replace('-', '') + '-14-01'
        excluded = day.replace('-', '') + '-22-01'
        now = datetime.fromisoformat(day + 'T07:00:00+09:00')
        deadline = day + 'T08:35:00+09:00'
        source_id = 'beforeinfo-' + formal
        fields = list(official.LOCK49_FIELDS)
        rows = []
        for key, status, reason in ((formal, 'FORMAL', None),
                                    (excluded, 'EXCLUDED', 'BODY_NOT_GENERATED')):
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
                        {'SOURCE_ID': source_id, 'RACE_KEY': formal, 'KIND': 'BEFOREINFO',
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
                return {'ABEKEN_V53_READY': [formal]}
        def publish(*args, **kwargs):
            self.assertTrue(kwargs['strict_deadline'])
            self.assertFalse(kwargs['include_research'])
            self.assertIs(args[1], projection)
            order.append('CURRENT_P3')
            return {'LIVE_PUBLISH': {'COMPLETED': [
                {'RACE_ID': formal, 'P3_SNAPSHOT_WRITE': 'PASS'}]}}

        original = critical.run
        original_bytes = Path(critical.__file__).read_bytes()
        self.assertTrue(scope.reviewed_run(day, run_sha))
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
            lock = morning / (critical.current_runtime.snapshot_name(formal) + '.json')
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
            import predeadline_input_health_v1 as health
            with patch.object(health, 'report', side_effect=lambda *args: order.append('INPUT_HEALTH')), \
                 patch.object(critical, 'datetime', Clock), \
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
                    self.assertEqual(result['CRITICAL']['PREDICTION_LOCKED_BEFORE_DEADLINE'], [formal])
                    self.assertEqual(order, ['OFFICIAL_CAPTURE'] + (['INPUT_HEALTH'] if day >= '2026-11-17' else []) + ['P3_V53', 'CURRENT_P3'])
            self.assertIs(critical.run, original)
            self.assertEqual(authority_path.read_bytes(), authority_bytes)
            if not corrupt:
                self.assertEqual(before, {p.name: p.read_bytes() for p in morning.iterdir()})
        self.assertEqual(Path(critical.__file__).read_bytes(), original_bytes)

    def test_only_the_two_reviewed_fingerprints_are_admitted_on_their_days(self):
        self.assertTrue(scope.reviewed_run('2026-11-20', scope.RUN_SHA256))
        self.assertFalse(scope.reviewed_run('2026-11-20', scope.DISPLAY_WARM_RUN_SHA))
        self.assertTrue(scope.reviewed_run('2026-11-21', scope.RUN_SHA256))
        self.assertTrue(scope.reviewed_run('2026-11-21', scope.DISPLAY_WARM_RUN_SHA))
        self.assertFalse(scope.reviewed_run('2026-11-21', '0' * 64))

    def test_excluded_race_allows_unchanged_critical_capture_publish_and_seal(self):
        self.exercise()

    def test_health_observer_is_bound_to_the_compiled_runtime_globals(self):
        self.exercise(observe=True)

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

