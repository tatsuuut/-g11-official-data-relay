"""Replay the actual reviewed collector, without network or prediction models."""
import ast
import copy
import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import tempfile
import textwrap
import types
import unittest
from typing import Any
from unittest.mock import Mock, patch

import predeadline_content_retry_v1 as retry

JST = timezone(timedelta(hours=9))
BASE = datetime(2026, 10, 30, 9, 22, tzinfo=JST)
KEY = '20261030-14-03'

# Public timing evidence only; no official response bodies or model data.
WARM_HANDOFF_TIMING = {'schema': 'G11_SAVED_WARM_HANDOFF_TIMING_FIXTURE_V1',
 'source_url': 'https://github.com/tatsuuut/-g11-official-data-relay/actions/runs/44980310556/job/137079364892',
 'source_head': '9ddbefe5f3b8906832b80be5707c5e7e9f85e9e4',
 'day': '2026-11-22',
 'warm_started_at_jst': '2026-11-22T12:27:26.774475+09:00',
 'previous_request_started_at_jst': '2026-11-22T12:25:04.155+09:00',
 'warm_first_request_started_at_jst': '2026-11-22T12:27:29.690+09:00',
 'warm_slots': 7,
 'responses': [{'source_id': 'beforeinfo-20261122-13-05',
                'headers_at_jst': '2026-11-22T12:27:30.013+09:00',
                'header_wait_ms': 322.661},
               {'source_id': 'odds3t-20261122-13-05',
                'headers_at_jst': '2026-11-22T12:27:59.347+09:00',
                'header_wait_ms': 289.253},
               {'source_id': 'beforeinfo-20261122-02-05', 'headers_at_jst': '2026-11-22T12:28:29.323+09:00'},
               {'source_id': 'odds3t-20261122-02-05', 'headers_at_jst': '2026-11-22T12:28:59.352+09:00'},
               {'source_id': 'beforeinfo-20261122-21-09', 'headers_at_jst': '2026-11-22T12:29:29.252+09:00'},
               {'source_id': 'odds3t-20261122-21-09', 'headers_at_jst': '2026-11-22T12:29:59.243+09:00'}],
 'deadlines': {'20261122-13-05': '2026-11-22T12:31:00+09:00',
               '20261122-02-05': '2026-11-22T12:33:00+09:00',
               '20261122-21-09': '2026-11-22T12:37:00+09:00'},
 'original_readback_at_jst': '2026-11-22T12:32:04.960917+09:00',
 'original_followup_started_at_jst': '2026-11-22T12:31:42.590560+09:00',
 'original_publication_started_at_jst': '2026-11-22T12:31:45.349691+09:00',
 'notes': ['Timing evidence only; source bodies in tests remain deterministic complete-input stubs for the '
           'actual reviewed collector.',
           'A replay projects the avoided serial acquisition and dispatch time; it does not certify live '
           'publication or GitHub scheduling latency.']}


class PipelineError(ValueError):
    pass


class Rejected(Exception):
    def __init__(self, at, reason):
        self.retry_at, self.reason, self.evidence = at, reason, {'SHA256': 'a'*64}


class Deferred(Exception):
    def __init__(self, at, reason):
        self.retry_at, self.reason = at, reason


def require(ok, reason):
    if not ok:
        raise PipelineError(reason)


def load_collector():
    root = Path(os.environ.get('G11_RETRY_SOURCE_ROOT',
        Path(__file__).resolve().parents[1] / '.g11-private'))
    path = root / 'g11/canonical/official_pipeline.py'
    code = path.read_text()
    tree = ast.parse(code)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'CanonicalRunner')
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'run_due_predeadline')
    tree.body = [ast.ClassDef(name='Runner', bases=[], keywords=[], body=[method], decorator_list=[])]
    ast.fix_missing_locations(tree)
    def parse(data):
        if data != b'complete':
            raise PipelineError('HTML_NUMBER_PARSE')
        return {'verified': True}
    ns = {'Any': Any, '_require': require, '_parse_jst': datetime.fromisoformat,
          '_format_jst': lambda at: at.isoformat(), 'PipelineError': PipelineError,
          'AcquisitionRejected': Rejected, 'AcquisitionDeferred': Deferred,
          'PREDEADLINE_EARLY_WAIT_SECONDS': 300, 'DEGRADED_INTERVAL_SECONDS': 20,
          '_morning_distance_by_key': lambda day: {row['RACE_KEY']: 1800 for row in day['PREDEADLINE_PLAN']},
          'parse_beforeinfo_html': parse, 'parse_odds3t_html': parse,
          'PREDEADLINE_AVAILABILITY': types.SimpleNamespace(
              load_verified=lambda *_: None,
              parse_odds_source=lambda data, _key: {'ODDS_ROWS': parse(data)},
              REASON='OFFICIAL_WITHDRAWAL_UNSUPPORTED_FIELD_SIZE')}
    exec(compile(tree, str(path), 'exec'), ns)
    return ns['Runner']


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root/'snapshots/predeadline').mkdir(parents=True)
        self.now = BASE
        self.sleeps, self.attempts, self.writes = [], [], []
        self.responses, self.counts, self.pauses = {}, {}, {}
        self.plan = [self.task(kind, KEY) for kind in ('BEFOREINFO', 'ODDS3T')]
        self.log = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.log)
        self.redirect.__enter__(); self.addCleanup(self.redirect.__exit__, None, None, None)
        owner = self
        self.Runner = load_collector()
        self.original = self.Runner.run_due_predeadline
        self.runner = self.Runner()
        def load(stage, name):
            if stage == 'morning':
                return {'SNAPSHOT': {'PAYLOAD': {'PREDEADLINE_PLAN': owner.plan,
                        'SCOPE': {'EXPECTED_RACE_COUNT': len({r['RACE_KEY'] for r in owner.plan})}}}}
            return json.loads((owner.root/'snapshots/predeadline'/f'{name}.json').read_text())
        def commit(stage, name, payload, receipts):
            path = owner.root/'snapshots/predeadline'/f'{name}.json'
            with path.open('x') as out:
                json.dump({'SNAPSHOT': {'PAYLOAD': payload}, 'SOURCE_RECEIPTS': receipts}, out)
            owner.writes.append((name, owner.now))
        self.runner.store = types.SimpleNamespace(root=self.root, load_verified_snapshot=load, commit_snapshot=commit)
        self.runner.client = types.SimpleNamespace(clock=lambda: self.now, fetch_validated_once=self.fetch)
        self.runner.sleeper = self.sleep
        self.runner._formal_prediction_scope = lambda *_args, **_kwargs: (
            {}, {r['RACE_KEY'] for r in self.plan}, [])

    def task(self, kind, key):
        return {'SOURCE_ID': kind.lower()+'-'+key, 'RACE_KEY': key, 'KIND': kind,
            'URL': 'https://www.boatrace.jp/fixture',
            'DUE_AT_JST': (BASE-timedelta(minutes=2)).isoformat(),
            'MUST_FINISH_BEFORE_JST': (BASE+timedelta(minutes=5)).isoformat()}

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)

    def fetch(self, source, url, validator):
        if self.now < self.pauses.get(source, BASE):
            raise Deferred(self.pauses[source], 'SOURCE_CONTENT_PAUSED')
        count = self.counts.get(source, 0)
        self.counts[source] = count + 1
        self.attempts.append((source, self.now))
        behavior = self.responses.get(source, ['reject', 'complete'])
        action = behavior[min(count, len(behavior)-1)]
        if isinstance(action, Exception):
            raise action
        if action == 'late':
            self.now += timedelta(minutes=5)
            action = 'complete'
        try:
            parsed = validator(b'complete' if action == 'complete' else b'')
        except PipelineError as exc:
            self.pauses[source] = self.now + timedelta(seconds=30)
            raise Rejected(self.pauses[source], str(exc))
        return {'CAPTURED_AT_JST': self.now.isoformat()}, parsed

    def run_capture(self, wait=0):
        return retry.collect_with_retry(self.original, self.runner,
            max_requests=8, early_wait_seconds=wait)

    def setup_paced_day(self, *, degraded=False):
        self.now = BASE.replace(month=11, day=19)
        for task in self.plan:
            key = '20261119-14-03'
            task.update(RACE_KEY=key, SOURCE_ID=task['KIND'].lower()+'-'+key,
                        DUE_AT_JST=(self.now-timedelta(minutes=2)).isoformat(),
                        MUST_FINISH_BEFORE_JST=(self.now+timedelta(minutes=5)).isoformat())
        self.runner.store.response_receipt = lambda _: None
        self.prior_start = self.now - timedelta(seconds=4)
        self.state = {'STOPPED': False, 'DEGRADED': degraded, 'PAUSE_UNTIL': {},
                      'ODDS_STOPPED': False, 'ACTIVE_REQUEST': None,
                      'CONTENT_INVALID_COUNTS': {}, 'SOURCE_PAUSE_UNTIL': {}}

        def ledger_state():
            last = self.attempts[-1][1] if self.attempts else self.prior_start
            return dict(self.state, LAST_REQUEST_AT=last)

        self.runner.store.ledger = types.SimpleNamespace(state=ledger_state)
        def fetch(source, url, validator):
            try:
                return self.fetch(source, url, validator)
            except Rejected as exc:
                counts = self.state['CONTENT_INVALID_COUNTS']
                counts[source] = counts.get(source, 0) + 1
                self.state['SOURCE_PAUSE_UNTIL'][source] = exc.retry_at
                raise
        self.runner.client.fetch_validated_once = fetch
        self.responses = {r['SOURCE_ID']: ['complete'] for r in self.plan}
        return [task['SOURCE_ID'] for task in self.plan]

    def test_cancelled_venue_skips_new_gets_but_processes_other_venues_and_saved_receipts(self):
        before, odds = self.setup_paced_day()
        other_tasks = [dict(task, RACE_KEY=task['RACE_KEY'].replace('-14-', '-18-'),
                            SOURCE_ID=task['SOURCE_ID'].replace('-14-', '-18-')) for task in self.plan]
        self.plan.extend(other_tasks)
        for task in other_tasks:
            self.responses[task['SOURCE_ID']] = ['complete']
        self.runner.store.response_receipt = lambda sid: {'saved':True} if sid == odds else None
        with patch('predeadline_input_health_v1.verified_cancelled_venue_days',
                   return_value={'20261119-14':'b'*64}):
            result = self.run_capture()
        self.assertEqual(result['SKIPPED'], [{'SOURCE_ID':before, 'RACE_KEY':'20261119-14-03',
                         'REASON':'OFFICIAL_VENUE_DAY_CANCELLED','EVIDENCE_SHA256':'b'*64}])
        self.assertEqual(result['ACQUIRED'], [odds] + [r['SOURCE_ID'] for r in other_tasks])
        self.assertFalse(result['MISSED'])

    def november20_congestion(self, next_minute):
        self.setup_paced_day(degraded=True)
        self.now = datetime.fromisoformat('2026-11-20T10:59:44+09:00')
        self.prior_start = self.now - timedelta(seconds=30)
        self.plan = []
        for key, minute in [('20261120-21-06', 3), ('20261120-17-03', next_minute),
                            ('20261120-22-02', 9), ('20261120-02-02', 12)]:
            end = self.now.replace(hour=11, minute=minute, second=0)
            for kind in ('BEFOREINFO', 'ODDS3T'):
                row = self.task(kind, key)
                row.update(DUE_AT_JST=(end-timedelta(minutes=7)).isoformat(),
                           MUST_FINISH_BEFORE_JST=end.isoformat())
                self.plan.append(row)
                self.responses[row['SOURCE_ID']] = ['complete']

    def normal_partial_capture(self):
        self.setup_paced_day(degraded=True)
        self.now = datetime.fromisoformat('2026-11-27T16:23:45+09:00')
        self.prior_start = self.now - timedelta(seconds=30)
        self.plan, self.responses = [], {}
        self.runner._formal_prediction_scope = lambda *_a, **_k: (
            {}, {task['RACE_KEY'] for task in self.plan}, [])
        for key, minute in [('20261127-24-03', 30), ('20261127-20-04', 35),
                            ('20261127-07-04', 35), ('20261127-01-04', 35)]:
            end = self.now.replace(minute=minute, second=0)
            for kind in ('BEFOREINFO', 'ODDS3T'):
                row = self.task(kind, key)
                row.update(DUE_AT_JST=(end-timedelta(minutes=7)).isoformat(),
                           MUST_FINISH_BEFORE_JST=end.isoformat())
                self.plan.append(row)
                self.responses[row['SOURCE_ID']] = ['complete']

    def seed_public_receipt(self, first):
        out = self.root / '.relay-output'
        out.mkdir(exist_ok=True)
        day = first['OPERATIONAL_DATE_JST']
        runtime = first['CRITICAL'].get('RUNTIME_SOURCE_SHA', 'd'*40)
        raw = json.dumps({'operational_date_jst':day, 'stage':'PREDEADLINE', 'status':'PASS',
            'source':{'run_id':int(first['CRITICAL']['LAST_CRITICAL_RUN_ID']), 'github_sha':runtime},
            'daily_runtime':{'schema':'G11_DAILY_RUNTIME_AUTHORITY_V1',
                'operational_date_jst':day, 'phase':'PREDEADLINE', 'immutable':True,
                'authority_sha256':first['CRITICAL']['AUTHORITY_SHA256'], 'runtime_source_sha':runtime}}).encode()
        accepted = 'b'*64
        (out / 'hot-delta-readback.json').write_bytes(raw)
        (out / 'hot-delta-accepted').write_text(accepted + '\n')
        receipt = {'schema':'G11_DAILY_SITE_DELIVERY_RECEIPT_V1',
            'operational_date_jst':day, 'phase':'PREDEADLINE',
            'authority_sha256':first['CRITICAL']['AUTHORITY_SHA256'],
            'runtime_source_sha':runtime,
            'site_payload_sha256':accepted, 'site_get_sha256':hashlib.sha256(raw).hexdigest(),
            'http_success':True, 'site_get_readback':'PASS'}
        path = self.root / '.relay-state' / 'daily-runtime' / day / 'delivery' / 'PREDEADLINE' / (accepted+'.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(receipt))
        return path

    def test_normal_partial_capture_reuses_same_worker_only_after_public_readback(self):
        self.normal_partial_capture()
        expected = [task['SOURCE_ID'] for task in self.plan]
        output = self.root / 'github-output'
        with patch.dict(os.environ, {'G11_URGENT_WARM_HANDOFF':'true', 'GITHUB_OUTPUT':str(output)}):
            first = retry.collect_with_retry(self.original, self.runner, max_requests=8,
                early_wait_seconds=0, early_capture=True)
        self.assertEqual(first['ACQUIRED'], expected[:2])
        self.assertEqual(first['WARM_REMAINING'], 6)
        self.assertEqual(output.read_text(), 'warm=true\n')
        self.assertEqual(self.now.isoformat(), '2026-11-27T16:24:15+09:00')
        original = {sid:(self.root/'snapshots/predeadline'/f'{sid}.json').read_bytes()
                    for sid in expected[:2]}
        # The first native LOCK, verified saved checkpoint and public readback
        # complete before the same worker can enter this remaining-slots pass.
        self.sleep(30)
        with patch.dict(os.environ, {'G11_URGENT_WARM_HANDOFF':'false',
                'G11_WARM_CAPTURE_REMAINING':'6', 'GITHUB_RUN_ID':'41',
                'G11_WARM_FIRST_PUBLIC_READBACK':'41'}):
            second = retry.collect_with_retry(self.original, self.runner, max_requests=6,
                early_wait_seconds=0, early_capture=True)
        self.assertEqual(second['ACQUIRED'], expected[2:])
        self.assertNotIn('WARM_REMAINING', second)
        self.assertEqual(self.sleeps, [30, 30, 15, 30, 30, 30, 30, 30])
        self.assertEqual([sid for sid, _ in self.attempts], expected)
        self.assertEqual(len(self.attempts), 8)
        self.assertTrue(all(at >= datetime.fromisoformat('2026-11-27T16:25:00+09:00')
                            for _, at in self.attempts[2:]))
        self.assertTrue(all((end-start).total_seconds() >= 30
                            for (_, start), (_, end) in zip(self.attempts, self.attempts[1:])))
        self.assertEqual(original, {sid:(self.root/'snapshots/predeadline'/f'{sid}.json').read_bytes()
                                    for sid in expected[:2]})
        self.assertEqual(self.plan[2]['DUE_AT_JST'], '2026-11-27T16:28:00+09:00')

    def test_normal_partial_warm_retains_actual_frozen_authority_boundary(self):
        self.normal_partial_capture()
        self.install_actual_boundary()
        (self.root/'manifest.json').write_text('{"OPERATIONAL_DATE_JST":"2026-11-27"}')
        calls = []
        daily = types.SimpleNamespace(
            verify_runtime=lambda *a, **k: calls.append('runtime'),
            verify_locks=lambda state, day, phase, **k: calls.append(phase))
        with patch.dict('sys.modules', {'g11.relay':types.SimpleNamespace(daily_runtime_authority_v1=daily)}):
            with patch.dict(os.environ, {'G11_URGENT_WARM_HANDOFF':'true'}):
                first = retry.collect_with_retry(self.original, self.runner, max_requests=8,
                    early_wait_seconds=0, early_capture=True)
            self.sleep(30)
            with patch.dict(os.environ, {'G11_URGENT_WARM_HANDOFF':'false',
                    'G11_WARM_CAPTURE_REMAINING':str(first['WARM_REMAINING']), 'GITHUB_RUN_ID':'41',
                    'G11_WARM_FIRST_PUBLIC_READBACK':'41'}):
                second = retry.collect_with_retry(self.original, self.runner, max_requests=6,
                    early_wait_seconds=0, early_capture=True)
        self.assertEqual(len(first['ACQUIRED'])+len(second['ACQUIRED']), 8)
        self.assertEqual(calls, ['runtime', 'MORNING', 'PREDEADLINE']*3)
        self.assertEqual(self.original, self.Runner.run_due_predeadline)

    def test_normal_warm_contexts_restore_after_future_due_sleeper_raises(self):
        import predeadline_capture_window_v1 as window
        from test_predeadline_capture_window_v1 import VerifiedMarketTests
        self.normal_partial_capture()
        # Reuse the test's actual frozen odds/formal-plan function extraction;
        # verified_odds_window still checks their real pinned source hashes.
        market = VerifiedMarketTests()
        market.setUp()
        module = types.ModuleType('g11.canonical.official_pipeline')
        module.CanonicalRunner = self.Runner
        module.FORMAL_ADAPTER = market.official.FORMAL_ADAPTER
        self.Runner._validated_odds_only_market = market.Runner._validated_odds_only_market
        original_market = self.Runner._validated_odds_only_market
        original_plan = module.FORMAL_ADAPTER._bind_predeadline_plan
        opener = Mock()
        self.runner.client.opener = opener
        def sleep_failure(_seconds):
            raise RuntimeError('warm due sleep fixture')
        with patch.dict('sys.modules', {'g11.canonical.official_pipeline':module}), \
                patch.dict(os.environ, {'G11_URGENT_WARM_HANDOFF':'true', 'GITHUB_RUN_ID':'41'}):
            with self.assertRaisesRegex(RuntimeError, 'warm due sleep fixture'):
                with window.verified_odds_window(module) as admitted, retry.bounded_content_retry(early_capture=True):
                    self.assertTrue(admitted)
                    first = self.runner.run_due_predeadline(max_requests=8, early_wait_seconds=0)
                    self.assertEqual(first['WARM_REMAINING'], 6)
                    self.now = self.now.replace(minute=24, second=45)
                    self.runner.sleeper = sleep_failure
                    with patch.dict(os.environ, {'G11_URGENT_WARM_HANDOFF':'false',
                            'G11_WARM_CAPTURE_REMAINING':'6', 'G11_WARM_FIRST_PUBLIC_READBACK':'41'}):
                        self.runner.run_due_predeadline(max_requests=8, early_wait_seconds=0)
        self.assertIs(self.Runner.run_due_predeadline, self.original)
        self.assertIs(self.Runner._validated_odds_only_market, original_market)
        self.assertIs(module.FORMAL_ADAPTER._bind_predeadline_plan, original_plan)
        self.assertIs(self.runner.client.opener, opener)
        self.assertFalse(hasattr(self.Runner, '_g11_ten_minute_odds_window'))
        self.assertEqual(len(self.attempts), 2)

    def test_normal_warm_keeps_native_failure_cooldown_and_never_replays_first_sources(self):
        self.normal_partial_capture()
        with patch.dict(os.environ, {'G11_URGENT_WARM_HANDOFF':'true'}):
            first = retry.collect_with_retry(self.original, self.runner, max_requests=8,
                early_wait_seconds=0, early_capture=True)
        self.now = self.now.replace(minute=25, second=0)
        failed = self.plan[2]['SOURCE_ID']
        self.responses[failed] = ['reject']
        with patch.dict(os.environ, {'G11_URGENT_WARM_HANDOFF':'false',
                'G11_WARM_CAPTURE_REMAINING':'6', 'GITHUB_RUN_ID':'41',
                'G11_WARM_FIRST_PUBLIC_READBACK':'41'}):
            second = retry.collect_with_retry(self.original, self.runner, max_requests=6,
                early_wait_seconds=0, early_capture=True)
        self.assertEqual(len(second['ACQUIRED']), 5)
        self.assertEqual([row['SOURCE_ID'] for row in second['REJECTED']], [failed])
        self.assertEqual(self.counts[failed], 1)
        self.assertTrue(all(self.counts[sid] == 1 for sid in first['ACQUIRED']))
        self.assertEqual(self.state['CONTENT_INVALID_COUNTS'][failed], 1)
        self.assertEqual(datetime.fromisoformat(second['REJECTED'][0]['RETRY_AT_JST']),
                         self.state['SOURCE_PAUSE_UNTIL'][failed]+timedelta(seconds=60))
        self.assertTrue(all((end-start).total_seconds() >= 30
                            for (_, start), (_, end) in zip(self.attempts, self.attempts[1:])))
        self.assertNotIn('WARM_REMAINING', second)

    def test_normal_warm_never_bypasses_native_daily_request_cap(self):
        self.normal_partial_capture()
        with patch.dict(os.environ, {'G11_URGENT_WARM_HANDOFF':'true'}):
            first = retry.collect_with_retry(self.original, self.runner, max_requests=8,
                early_wait_seconds=0, early_capture=True)
        before = list(self.writes)
        self.now = self.now.replace(minute=25, second=0)
        self.responses[self.plan[2]['SOURCE_ID']] = [PipelineError('DAILY_REQUEST_CAP')]
        with patch.dict(os.environ, {'G11_URGENT_WARM_HANDOFF':'false',
                'G11_WARM_CAPTURE_REMAINING':str(first['WARM_REMAINING']), 'GITHUB_RUN_ID':'41',
                'G11_WARM_FIRST_PUBLIC_READBACK':'41'}):
            with self.assertRaisesRegex(PipelineError, 'DAILY_REQUEST_CAP'):
                retry.collect_with_retry(self.original, self.runner, max_requests=6,
                    early_wait_seconds=0, early_capture=True)
        self.assertEqual(self.writes, before)
        self.assertEqual(len(self.attempts), 3)

    def test_verified_alternate_pair_does_not_hide_next_warm_due_or_get_downloaded_again(self):
        if hashlib.sha256(inspect.getsource(self.original).encode()).hexdigest() != retry.ALTERNATE_COLLECTOR_SHA:
            self.skipTest('retained collector predates shared alternate reuse')
        self.normal_partial_capture()
        self.now = self.now.replace(minute=24, second=45)
        self.prior_start = self.now - timedelta(seconds=30)
        key = self.plan[0]['RACE_KEY']
        for prefix in ('published-exhibition-', 'venue-market-'):
            (self.root/'snapshots/predeadline'/f'{prefix}{key}.json').write_text('retained alternate')
        calls = []
        def recheck(store, race, *_args):
            self.assertEqual(race, key)
            calls.append(race)
        with patch.dict(self.original.__globals__, {
                'PUBLISHED_PREVIEW':types.SimpleNamespace(race_roster=lambda *_: object(), read_observation=recheck),
                'VENUE_MARKET':types.SimpleNamespace(recheck=recheck)}), \
                patch.dict(os.environ, {'G11_URGENT_WARM_HANDOFF':'false',
                    'G11_WARM_CAPTURE_REMAINING':'6', 'GITHUB_RUN_ID':'41',
                    'G11_WARM_FIRST_PUBLIC_READBACK':'41'}):
            result = retry.collect_with_retry(self.original, self.runner, max_requests=6,
                early_wait_seconds=0, early_capture=True)
        self.assertEqual(result['ACQUIRED'], [task['SOURCE_ID'] for task in self.plan[2:]])
        self.assertEqual(self.sleeps[0], 15)
        self.assertEqual(len(self.attempts), 6)
        self.assertFalse(any(sid.endswith(key) for sid, _ in self.attempts))
        self.assertEqual(calls, [key]*4)  # Both native passes verify the saved alternate pair.

    def test_normal_partial_capture_does_not_arm_failed_or_closed_budget(self):
        for failed, budget in [(True, 8), (False, 2)]:
            with self.subTest(failed=failed, budget=budget):
                self.normal_partial_capture()
                self.attempts, self.counts = [], {}
                # Fresh saved input paths for each independent first pass.
                for path in (self.root/'snapshots/predeadline').glob('*.json'):
                    path.unlink()
                if failed:
                    self.responses[self.plan[0]['SOURCE_ID']] = [PipelineError('TIMEOUT')]
                with patch.dict(os.environ, {'G11_URGENT_WARM_HANDOFF':'true'}):
                    first = retry.collect_with_retry(self.original, self.runner, max_requests=budget,
                        early_wait_seconds=0, early_capture=True)
                self.assertNotIn('WARM_REMAINING', first)
                self.assertLessEqual(len(self.attempts), 2)

    def test_normal_full_eight_capture_slots_do_not_open_another_pass(self):
        self.normal_partial_capture()
        self.now = self.now.replace(minute=25, second=0)
        self.prior_start = self.now - timedelta(seconds=30)
        with patch.dict(os.environ, {'G11_URGENT_WARM_HANDOFF':'true'}):
            result = retry.collect_with_retry(self.original, self.runner, max_requests=8,
                early_wait_seconds=0, early_capture=True)
        self.assertEqual(len(result['ACQUIRED']), 8)
        self.assertNotIn('WARM_REMAINING', result)
        self.assertEqual(len(self.attempts), 8)

    def test_normal_partial_capture_needs_enabled_same_day_eligible_upcoming_task(self):
        for mode in ('disabled', 'distant', 'excluded', 'cancelled', 'saved'):
            with self.subTest(mode=mode):
                self.normal_partial_capture()
                self.attempts, self.counts = [], {}
                for path in (self.root/'snapshots/predeadline').glob('*.json'):
                    path.unlink()
                if mode == 'distant':
                    for task in self.plan[2:]:
                        end = self.now.replace(minute=45, second=0)
                        task.update(DUE_AT_JST=(end-timedelta(minutes=7)).isoformat(),
                                    MUST_FINISH_BEFORE_JST=end.isoformat())
                if mode == 'excluded':
                    self.runner._formal_prediction_scope = lambda *_a, **_k: ({}, {self.plan[0]['RACE_KEY']}, [])
                if mode == 'saved':
                    for task in self.plan[2:]:
                        (self.root/'snapshots/predeadline'/f'{task["SOURCE_ID"]}.json').write_text('{}')
                cancelled = {task['RACE_KEY'][:-3]:'b'*64 for task in self.plan[2:]} if mode == 'cancelled' else {}
                with patch.dict(os.environ, {'G11_URGENT_WARM_HANDOFF':'false' if mode == 'disabled' else 'true'}), \
                        patch('predeadline_input_health_v1.verified_cancelled_venue_days', return_value=cancelled):
                    first = retry.collect_with_retry(self.original, self.runner, max_requests=8,
                        early_wait_seconds=0, early_capture=True)
                self.assertNotIn('WARM_REMAINING', first)

    def test_warm_due_wait_requires_public_readback_and_respects_sixty_second_limit(self):
        for marker, delay in [('', 15), ('other-run', 15), ('41', 61)]:
            with self.subTest(marker=marker, delay=delay):
                self.normal_partial_capture()
                self.plan = self.plan[2:]
                due = self.now + timedelta(seconds=delay)
                for task in self.plan:
                    task.update(DUE_AT_JST=(due+timedelta(minutes=3)).isoformat(),
                                MUST_FINISH_BEFORE_JST=(due+timedelta(minutes=10)).isoformat())
                self.attempts, self.sleeps = [], []
                with patch.dict(os.environ, {'G11_URGENT_WARM_HANDOFF':'false',
                        'G11_WARM_CAPTURE_REMAINING':'6', 'GITHUB_RUN_ID':'41',
                        'G11_WARM_FIRST_PUBLIC_READBACK':marker}):
                    result = retry.collect_with_retry(self.original, self.runner, max_requests=6,
                        early_wait_seconds=0, early_capture=True)
                self.assertFalse(result['ACQUIRED'])
                self.assertFalse(self.attempts)
                self.assertFalse(self.sleeps)

    def test_warm_due_wait_rechecks_host_pause_and_late_wakeup(self):
        for changed in ('host-paused', 'stopped', 'late'):
            with self.subTest(changed=changed):
                self.normal_partial_capture()
                self.plan = self.plan[2:]
                self.attempts, self.sleeps = [], []
                self.now = self.now.replace(minute=24, second=45)
                def wake(seconds):
                    self.sleep(seconds)
                    if changed == 'host-paused':
                        self.state['PAUSE_UNTIL']['www.boatrace.jp'] = self.now + timedelta(minutes=5)
                    elif changed == 'stopped':
                        self.state['STOPPED'] = True
                    else:
                        self.now += timedelta(minutes=11)
                self.runner.sleeper = wake
                with patch.dict(os.environ, {'G11_URGENT_WARM_HANDOFF':'false',
                        'G11_WARM_CAPTURE_REMAINING':'6', 'GITHUB_RUN_ID':'41',
                        'G11_WARM_FIRST_PUBLIC_READBACK':'41'}):
                    result = retry.collect_with_retry(self.original, self.runner, max_requests=6,
                        early_wait_seconds=0, early_capture=True)
                self.assertFalse(result['ACQUIRED'])
                self.assertEqual(self.sleeps, [15])
                self.assertFalse(self.attempts)

    def test_urgent_saved_pair_reaches_publication_before_six_remaining_gets(self):
        self.november20_congestion(next_minute=6)
        expected = [r['SOURCE_ID'] for r in self.plan]
        with patch.dict(os.environ, {'G11_URGENT_WARM_HANDOFF':'true'}):
            first = retry.collect_with_retry(self.original, self.runner, max_requests=8,
                early_wait_seconds=0, early_capture=True)
        self.assertEqual(first['ACQUIRED'], expected[:2])
        self.assertEqual(first['WARM_REMAINING'], 6)
        self.assertEqual(self.now.isoformat(), '2026-11-20T11:00:14+09:00')
        # Replay representative native lock, verified checkpoint and public
        # readback durations. The second collection can begin only after this.
        self.sleep(12 + 102)
        self.assertLess(self.now, datetime.fromisoformat(self.plan[0]['MUST_FINISH_BEFORE_JST']))
        second = retry.collect_with_retry(self.original, self.runner, max_requests=first['WARM_REMAINING'],
            early_wait_seconds=0, early_capture=True)
        self.assertEqual(second['ACQUIRED'], expected[2:])
        self.assertNotIn('WARM_REMAINING', second)
        self.assertEqual([sid for sid, _ in self.attempts], expected)
        self.assertEqual([sid for sid, _ in self.writes], expected)
        self.sleep(14 + 52 + 10)
        self.assertLess(self.now, datetime.fromisoformat(self.plan[2]['MUST_FINISH_BEFORE_JST']))

    def test_urgent_handoff_keeps_another_near_cutoff_in_the_first_capture(self):
        self.november20_congestion(next_minute=4)
        with patch.dict(os.environ, {'G11_URGENT_WARM_HANDOFF':'true'}):
            result = retry.collect_with_retry(self.original, self.runner, max_requests=8,
                early_wait_seconds=0, early_capture=True)
        self.assertEqual(result['ACQUIRED'][:4], [r['SOURCE_ID'] for r in self.plan[:4]])

    def test_official_host_release_preserves_urgent_remainder_from_full_second_attempt(self):
        self.november20_congestion(next_minute=6)
        release = self.now + timedelta(seconds=1)
        first = {name:[] for name in ('ACQUIRED','MISSED','SKIPPED','REJECTED',
            'RETRYABLE_FAILURES','UNAVAILABLE','TASK_DEFERRED')}
        first.update(STATUS='DUE_TASKS_PROCESSED', DEFERRED={'REASON':'HOST_PAUSED',
            'RETRY_AT_JST':release.isoformat()})
        full = retry._restricted_collector(self.original, None, None, early_capture=True, isolate_host=True)
        with patch.dict(os.environ, {'G11_URGENT_WARM_HANDOFF':'true'}):
            recovered = retry.resume_at_host_release(first, self.plan, self.runner, full, 8, self.now)
        self.assertEqual(recovered['ACQUIRED'], [r['SOURCE_ID'] for r in self.plan[:2]])
        self.assertEqual(recovered['WARM_REMAINING'], 6)
        self.assertIsNone(recovered['DEFERRED'])

    def test_warm_workflow_requires_both_checkpoints_and_distinct_publication_slots(self):
        source = (Path(__file__).resolve().parents[1] / '.github/workflows/g11-free-runner.yml').read_text()
        steps = ['critical-save', 'critical-publish', 'predeadline-chain', 'warm',
                 'publication-state-verified', 'critical-retry', 'wild4-followup']
        offsets = [source.index('id: ' + name + '\n') for name in steps]
        self.assertEqual(offsets, sorted(offsets))
        warm = source[offsets[3]:].split('      - name:', 1)[0]
        self.assertIn("steps.critical-publish.outcome=='success'", warm)
        self.assertIn('python3 -S scripts/predeadline_content_retry_v1.py', warm)
        self.assertIn("steps.critical.outputs.warm=='true'", warm)
        self.assertIn('G11_WARM_CONTINUATION_READY: ${{steps.predeadline-chain.outputs.ready}}', warm)
        chain = source[offsets[2]:].split('      - name:', 1)[0]
        self.assertGreater(chain.index("echo 'ready=true'"), chain.index('predeadline_dispatch_v1.py'))
        publish = source[offsets[-2]:].split('      - name:', 1)[0]
        self.assertIn("steps.publication-state-verified.outputs.ready=='true'", publish)
        self.assertIn("G11_WARM_PUBLICATION:", publish)
        followup = source[offsets[-1]:].split('      - name:', 1)[0]
        self.assertIn('if: always()&&!cancelled()&&', followup)
        self.assertIn("steps.publication-state-verified.outputs.ready=='true'", followup)
        self.assertIn("steps.warm.outcome == 'success' && steps.critical-retry.outcome == 'success'", source)

    def test_warm_start_checks_receipt_preserves_first_cutoff_and_enforces_residual_budget(self):
        root = self.root
        out = root / '.relay-output'
        out.mkdir()
        first = {'OPERATIONAL_DATE_JST':'2026-11-20', 'CRITICAL':{'OPERATIONAL_DATE':'2026-11-20',
            'CRITICAL_CHECKPOINT_STATUS':'PASS','LAST_CRITICAL_RUN_ID':'41','AUTHORITY_SHA256':'a'*64},
            'ACQUISITION':{'ACQUIRED':['a','b'], 'WARM_REMAINING':6}}
        (out / 'result.json').write_text(json.dumps(first))
        self.seed_public_receipt(first)
        def complete(_command, *, env, **kwargs):
            self.assertEqual((env['G11_WARM_CAPTURE_REMAINING'], env['G11_URGENT_WARM_HANDOFF']), ('6','false'))
            second = dict(first, ACQUISITION={'ACQUIRED':['c','d']})
            (out / 'warm-result.json').write_text(json.dumps(second))
        with patch.dict(os.environ, {'GITHUB_WORKSPACE':str(root), 'GITHUB_RUN_ID':'41'}), patch('subprocess.run',side_effect=complete) as call:
            retry.resume_warm()
            self.assertEqual(json.loads((out/'first-critical-result.json').read_bytes()), first)
            self.assertEqual(json.loads((out/'result.json').read_bytes())['ACQUISITION'], {'ACQUIRED':['c','d']})
            first['ACQUISITION']['WARM_REMAINING'] = 7
            (out / 'result.json').write_text(json.dumps(first))
            with self.assertRaisesRegex(ValueError, 'FIRST_RECEIPT_OR_BUDGET'):
                retry.resume_warm()
            self.assertEqual(call.call_count, 1)

    def test_warm_start_rejects_wrong_day_authority_or_unverified_public_readback(self):
        out = self.root / '.relay-output'
        out.mkdir()
        first = {'OPERATIONAL_DATE_JST':'2026-11-27', 'CRITICAL':{'OPERATIONAL_DATE':'2026-11-27',
            'CRITICAL_CHECKPOINT_STATUS':'PASS','LAST_CRITICAL_RUN_ID':'41','AUTHORITY_SHA256':'a'*64,
            'RUNTIME_SOURCE_SHA':'d'*40},
            'ACQUISITION':{'ACQUIRED':['a','b'], 'WARM_REMAINING':6}}
        (out / 'result.json').write_text(json.dumps(first))
        for field, value in [('operational_date_jst','2026-11-26'), ('authority_sha256','c'*64),
                             ('runtime_source_sha','c'*40),
                             ('site_get_sha256','c'*64), ('site_payload_sha256','c'*64),
                             ('site_get_readback','PENDING'), ('http_success',False)]:
            with self.subTest(field=field):
                path = self.seed_public_receipt(first)
                receipt = json.loads(path.read_bytes())
                receipt[field] = value
                path.write_text(json.dumps(receipt))
                with patch.dict(os.environ, {'GITHUB_WORKSPACE':str(self.root), 'GITHUB_RUN_ID':'41'}), \
                        patch('subprocess.run') as call:
                    with self.assertRaisesRegex(ValueError, 'WARM_FIRST_PUBLIC_READBACK'):
                        retry.resume_warm()
                call.assert_not_called()
                self.assertFalse((out/'first-critical-result.json').exists())
                self.assertEqual(json.loads((out/'result.json').read_bytes()), first)

        for section, field, value in [(None,'operational_date_jst','2026-11-26'),
                (None,'stage','MORNING'), (None,'status','PENDING'),
                ('source','run_id',40), ('source','github_sha','c'*40),
                ('daily_runtime','authority_sha256','c'*64),
                ('daily_runtime','runtime_source_sha','c'*40)]:
            with self.subTest(section=section, field=field):
                path = self.seed_public_receipt(first)
                public = json.loads((out/'hot-delta-readback.json').read_bytes())
                (public if section is None else public[section])[field] = value
                raw = json.dumps(public).encode()
                (out/'hot-delta-readback.json').write_bytes(raw)
                # Even a matching old receipt/raw pair cannot authorize a
                # different run, day, source or publication phase.
                receipt = json.loads(path.read_bytes())
                receipt['site_get_sha256'] = hashlib.sha256(raw).hexdigest()
                path.write_text(json.dumps(receipt))
                with patch.dict(os.environ, {'GITHUB_WORKSPACE':str(self.root), 'GITHUB_RUN_ID':'41'}), \
                        patch('subprocess.run') as call:
                    with self.assertRaisesRegex(ValueError, 'WARM_FIRST_PUBLIC_READBACK'):
                        retry.resume_warm()
                call.assert_not_called()

    def warm_congestion_replay(self, *, following_deadline=None):
        fixture = copy.deepcopy(WARM_HANDOFF_TIMING)
        self.setup_paced_day(degraded=True)
        self.now = datetime.fromisoformat(fixture['warm_first_request_started_at_jst'])
        self.prior_start = datetime.fromisoformat(fixture['previous_request_started_at_jst'])
        self.plan = []
        self.responses = {}
        for key, deadline in fixture['deadlines'].items():
            end = datetime.fromisoformat(deadline)
            if following_deadline is not None and key.endswith('-21-09'):
                end = following_deadline
            for kind in ('BEFOREINFO', 'ODDS3T'):
                row = self.task(kind, key)
                row.update(DUE_AT_JST=(end-timedelta(minutes=7)).isoformat(),
                           MUST_FINISH_BEFORE_JST=end.isoformat())
                self.plan.append(row)
                self.responses[row['SOURCE_ID']] = ['complete']
        return fixture

    def test_warm_handoff_keeps_near_pair_then_publishes_before_distant_pair(self):
        fixture = self.warm_congestion_replay()
        expected = [row['source_id'] for row in fixture['responses']]
        with patch.dict(os.environ, {'G11_URGENT_WARM_HANDOFF': 'true',
                                   'G11_WARM_CAPTURE_REMAINING': '7'}):
            result = retry.collect_with_retry(self.original, self.runner, max_requests=7,
                early_wait_seconds=0, early_capture=True)
        # Amagasaki alone cannot hand off Toda safely. Once Toda's pair is
        # complete, its later cutoff must not obscure the earlier saved pair.
        self.assertEqual(result['ACQUIRED'], expected[:4])
        self.assertEqual(result['WARM_REMAINING'], 3)
        self.assertEqual([sid for sid, _ in self.attempts], expected[:4])
        report = next(json.loads(line.split('=', 1)[1]) for line in self.log.getvalue().splitlines()
                      if 'WARM_PAIR_PUBLICATION_HANDOFF' in line)
        self.assertEqual(report['race_key'], '20261122-13-05')
        self.assertEqual(report['publication_reserve_seconds'], 145)
        self.assertEqual(report['successor_capture_reserve_seconds'], 380)
        # Preserve the observed limit: removing the two distant GETs and
        # dispatch wait alone still projects a little past Amagasaki's cutoff.
        at = datetime.fromisoformat
        avoided = at(fixture['responses'][-1]['headers_at_jst']) - at(fixture['responses'][3]['headers_at_jst'])
        dispatch = at(fixture['original_publication_started_at_jst']) - at(fixture['original_followup_started_at_jst'])
        projected = at(fixture['original_readback_at_jst']) - avoided - dispatch
        remaining = (at(fixture['deadlines']['20261122-13-05']) - projected).total_seconds()
        self.assertAlmostEqual(avoided.total_seconds(), 59.891)
        self.assertAlmostEqual(remaining, -2.310786, places=5)

    def test_warm_handoff_does_not_push_another_near_cutoff_into_next_worker(self):
        fixture = self.warm_congestion_replay(
            following_deadline=datetime(2026, 11, 22, 12, 35, tzinfo=JST))
        with patch.dict(os.environ, {'G11_URGENT_WARM_HANDOFF': 'true',
                                   'G11_WARM_CAPTURE_REMAINING': '7'}):
            result = retry.collect_with_retry(self.original, self.runner, max_requests=7,
                early_wait_seconds=0, early_capture=True)
        self.assertEqual(result['ACQUIRED'], [row['source_id'] for row in fixture['responses']])
        self.assertNotIn('WARM_REMAINING', result)

    def test_expired_completed_pair_does_not_hide_a_future_warm_publication(self):
        self.warm_congestion_replay()
        now = datetime(2026, 11, 22, 12, 1, tzinfo=JST)
        ends = [now-timedelta(minutes=1), now+timedelta(minutes=2), now+timedelta(minutes=7)]
        for index, row in enumerate(self.plan):
            end = ends[index//2]
            row.update(DUE_AT_JST=(end-timedelta(minutes=7)).isoformat(),
                       MUST_FINISH_BEFORE_JST=end.isoformat())
        saved = {}
        for row in self.plan[:4]:
            path = self.root/'snapshots/predeadline'/f'{row["SOURCE_ID"]}.json'
            data = json.dumps({'saved_before_deadline': row['SOURCE_ID']}).encode()
            path.write_bytes(data)
            saved[path] = data
        with patch.dict(os.environ, {'G11_URGENT_WARM_HANDOFF': 'true',
                                   'G11_WARM_CAPTURE_REMAINING': '6'}):
            handoff = retry.urgent_pair_handoff(self.runner.store, self.plan[3], ends[1], now,
                self.plan[4:], {row['RACE_KEY'] for row in self.plan}, {}, 2,
                acquired=[row['SOURCE_ID'] for row in self.plan[:4]], plan=self.plan)
        self.assertTrue(handoff)
        report = next(json.loads(line.split('=', 1)[1]) for line in self.log.getvalue().splitlines()
                      if 'WARM_PAIR_PUBLICATION_HANDOFF' in line)
        self.assertEqual(report['race_key'], '20261122-02-05')
        for path, data in saved.items():
            self.assertEqual(path.read_bytes(), data)

    def test_saved_warm_handoff_restores_pending_pair_without_reacquiring_saved_sources(self):
        import shutil
        fixture = self.warm_congestion_replay()
        expected = [row['source_id'] for row in fixture['responses']]
        with patch.dict(os.environ, {'G11_URGENT_WARM_HANDOFF': 'true',
                                   'G11_WARM_CAPTURE_REMAINING': '7'}):
            first = retry.collect_with_retry(self.original, self.runner, max_requests=7,
                early_wait_seconds=0, early_capture=True)
        saved = {path.name: path.read_bytes() for path in (self.root/'snapshots/predeadline').iterdir()}
        original_root = self.root
        self.root = original_root / 'restored'
        shutil.copytree(original_root / 'snapshots', self.root / 'snapshots')
        self.runner.store.root = self.root
        self.sleep(retry.WARM_PUBLICATION_RESERVE_SECONDS + retry.WARM_SUCCESSOR_SETUP_SECONDS)
        with patch.dict(os.environ, {'G11_URGENT_WARM_HANDOFF': 'false',
                                   'G11_WARM_CAPTURE_REMAINING': ''}):
            second = retry.collect_with_retry(self.original, self.runner, max_requests=8,
                early_wait_seconds=0, early_capture=True)
        self.assertEqual(first['ACQUIRED'], expected[:4])
        self.assertEqual(second['ACQUIRED'], expected[4:])
        self.assertEqual([sid for sid, _ in self.attempts], expected)
        self.assertEqual([sid for sid, _ in self.writes], expected)
        for name, data in saved.items():
            self.assertEqual((self.root/'snapshots/predeadline'/name).read_bytes(), data)
        for (_, previous), (_, current) in zip(self.attempts, self.attempts[1:]):
            self.assertGreaterEqual((current - previous).total_seconds(), retry.DEGRADED_SPACING_SECONDS)
        self.assertLess(self.now, datetime.fromisoformat(fixture['deadlines']['20261122-21-09']))

    def test_warm_continuation_keeps_stop_and_cutoff_after_delayed_start(self):
        fixture = self.warm_congestion_replay()
        with patch.dict(os.environ, {'G11_URGENT_WARM_HANDOFF': 'true',
                                   'G11_WARM_CAPTURE_REMAINING': '7'}):
            retry.collect_with_retry(self.original, self.runner, max_requests=7,
                early_wait_seconds=0, early_capture=True)
        attempts = list(self.attempts)
        self.state['STOPPED'] = True
        with patch.dict(os.environ, {'G11_URGENT_WARM_HANDOFF': 'false',
                                   'G11_WARM_CAPTURE_REMAINING': ''}):
            stopped = retry.collect_with_retry(self.original, self.runner, max_requests=8,
                early_wait_seconds=0, early_capture=True)
            self.assertEqual(stopped['ACQUIRED'], [])
            self.assertEqual(stopped['DEFERRED'], {'REASON': 'PIPELINE_STOPPED'})
            self.now = datetime.fromisoformat(fixture['deadlines']['20261122-21-09'])
            expired = retry.collect_with_retry(self.original, self.runner, max_requests=8,
                early_wait_seconds=0, early_capture=True)
        self.assertEqual(expired['ACQUIRED'], [])
        self.assertTrue(set(row['source_id'] for row in fixture['responses'][-2:]).issubset(expired['MISSED']))
        self.assertEqual(self.attempts, attempts)

    def test_warm_start_preserves_handoff_budget_for_existing_continuation(self):
        root = self.root
        out = root / '.relay-output'
        out.mkdir()
        first = {'OPERATIONAL_DATE_JST': '2026-11-22', 'CRITICAL': {'OPERATIONAL_DATE': '2026-11-22',
            'CRITICAL_CHECKPOINT_STATUS': 'PASS', 'LAST_CRITICAL_RUN_ID': '41', 'AUTHORITY_SHA256': 'a'*64},
            'ACQUISITION': {'ACQUIRED': ['a'], 'WARM_REMAINING': 7}}
        (out / 'result.json').write_text(json.dumps(first))
        self.seed_public_receipt(first)
        def complete(_command, *, env, **kwargs):
            self.assertEqual((env['G11_WARM_CAPTURE_REMAINING'], env['G11_URGENT_WARM_HANDOFF']), ('7', 'true'))
            second = dict(first, ACQUISITION={'ACQUIRED': ['b', 'c', 'd', 'e'], 'WARM_REMAINING': 3})
            (out / 'warm-result.json').write_text(json.dumps(second))
        with patch.dict(os.environ, {'GITHUB_WORKSPACE': str(root), 'GITHUB_RUN_ID': '41',
                                   'G11_WARM_CONTINUATION_READY': 'true'}), patch('subprocess.run', side_effect=complete):
            retry.resume_warm()
        self.assertEqual(json.loads((out/'first-critical-result.json').read_bytes()), first)
        self.assertEqual(json.loads((out/'result.json').read_bytes())['ACQUISITION'],
                         {'ACQUIRED': ['b', 'c', 'd', 'e'], 'WARM_REMAINING': 3})

    def test_warm_start_rejects_inflated_or_unconsumed_handoff_budget(self):
        out = self.root / '.relay-output'
        out.mkdir()
        first = {'OPERATIONAL_DATE_JST': '2026-11-22', 'CRITICAL': {'OPERATIONAL_DATE': '2026-11-22',
            'CRITICAL_CHECKPOINT_STATUS': 'PASS', 'LAST_CRITICAL_RUN_ID': '41', 'AUTHORITY_SHA256': 'a'*64},
            'ACQUISITION': {'ACQUIRED': ['a'], 'WARM_REMAINING': 7}}
        (out / 'result.json').write_text(json.dumps(first))
        self.seed_public_receipt(first)
        for acquired, remaining in [(['b', 'c'], 6), ([], 7), (['b'], True), (['b'], -1),
                                    (['a'], 0), (['b', 'b'], 0)]:
            with self.subTest(acquired=acquired, remaining=remaining):
                def complete(_command, **kwargs):
                    second = dict(first, ACQUISITION={'ACQUIRED': acquired, 'WARM_REMAINING': remaining})
                    (out / 'warm-result.json').write_text(json.dumps(second))
                with patch.dict(os.environ, {'GITHUB_WORKSPACE': str(self.root), 'GITHUB_RUN_ID': '41',
                                           'G11_WARM_CONTINUATION_READY': 'true'}), patch('subprocess.run', side_effect=complete):
                    with self.assertRaisesRegex(ValueError, 'WARM_CHECKPOINT_OR_BUDGET'):
                        retry.resume_warm()
                self.assertEqual(json.loads((out/'result.json').read_bytes()), first)

    def test_unconfirmed_continuation_keeps_warm_pass_in_existing_worker(self):
        out = self.root / '.relay-output'
        out.mkdir()
        first = {'OPERATIONAL_DATE_JST': '2026-11-22', 'CRITICAL': {'OPERATIONAL_DATE': '2026-11-22',
            'CRITICAL_CHECKPOINT_STATUS': 'PASS', 'LAST_CRITICAL_RUN_ID': '41', 'AUTHORITY_SHA256': 'a'*64},
            'ACQUISITION': {'ACQUIRED': ['a'], 'WARM_REMAINING': 7}}
        (out / 'result.json').write_text(json.dumps(first))
        self.seed_public_receipt(first)
        def complete(_command, *, env, **kwargs):
            self.assertEqual(env['G11_URGENT_WARM_HANDOFF'], 'false')
            second = dict(first, ACQUISITION={'ACQUIRED': ['b', 'c', 'd', 'e', 'f', 'g']})
            (out / 'warm-result.json').write_text(json.dumps(second))
        with patch.dict(os.environ, {'GITHUB_WORKSPACE': str(self.root), 'GITHUB_RUN_ID': '41',
                                   'G11_WARM_CONTINUATION_READY': ''}), patch('subprocess.run', side_effect=complete):
            retry.resume_warm()
        self.assertNotIn('WARM_REMAINING', json.loads((out/'result.json').read_bytes())['ACQUISITION'])

    def test_shared_spacing_handles_prior_worker_and_both_inputs(self):
        before, odds = self.setup_paced_day()
        start = self.now
        self.assertEqual(self.run_capture()['ACQUIRED'], [before, odds])
        self.assertEqual(self.attempts, [(before, start+timedelta(seconds=11)),
                                        (odds, start+timedelta(seconds=26))])

    def test_degraded_spacing_rechecks_deadline_after_delayed_wake(self):
        before, odds = self.setup_paced_day(degraded=True)
        def late_wakeup(seconds):
            self.sleep(seconds)
            self.now += timedelta(minutes=5)
        self.runner.sleeper = late_wakeup
        self.assertEqual(self.run_capture()['MISSED'], [before, odds])
        self.assertEqual(self.sleeps, [26])
        self.assertFalse(self.attempts)

    def test_incomplete_source_waits_across_workers_while_other_input_proceeds(self):
        before, odds = self.setup_paced_day()
        first = self.now
        self.state['CONTENT_INVALID_COUNTS'][before] = 2
        self.state['SOURCE_PAUSE_UNTIL'][before] = first+timedelta(seconds=30)
        result = self.run_capture()
        self.assertEqual(result['ACQUIRED'], [odds])
        self.assertEqual(result['TASK_DEFERRED'][0]['RETRY_AT_JST'],
                         (first+timedelta(seconds=180)).isoformat())
        self.now = first+timedelta(seconds=180)
        self.assertEqual(self.run_capture()['ACQUIRED'], [before])

    def test_first_incomplete_response_waits_without_optional_second_get(self):
        before, odds = self.setup_paced_day()
        self.responses = {before: ['reject'], odds: ['reject']}
        rejected = self.run_capture()['REJECTED']
        self.assertEqual(len(rejected), 2)
        self.assertEqual([sid for sid, _ in self.attempts], [before, odds])
        self.assertEqual(rejected[0]['RETRY_AT_JST'],
                         (self.pauses[before]+timedelta(seconds=60)).isoformat())
        self.assertEqual(retry.capture_pause(self.runner.store, before, self.now),
                         ('SOURCE_CONTENT_PAUSED', self.pauses[before]+timedelta(seconds=60)))

    def test_stopped_host_admits_only_previously_saved_receipt(self):
        before, odds = self.setup_paced_day()
        self.state['STOPPED'] = True
        self.runner.store.response_receipt = lambda sid: {'saved': True} if sid == odds else None
        calls = []
        def cached(sid, url, validator):
            self.assertEqual(sid, odds)
            calls.append(sid)
            return {'CAPTURED_AT_JST': self.now.isoformat()}, validator(b'complete')
        self.runner.client.fetch_validated_once = cached
        result = self.run_capture()
        self.assertEqual(result['ACQUIRED'], [odds])
        self.assertEqual(result['TASK_DEFERRED'][0]['SOURCE_ID'], before)
        self.assertEqual(result['DEFERRED']['REASON'], 'PIPELINE_STOPPED')
        self.assertEqual(calls, [odds])
        self.assertFalse(self.sleeps)

    def test_official_denial_is_observed_once_without_an_extra_fetch(self):
        from urllib.error import HTTPError
        failure = PipelineError('HTTP_403')
        failure.__cause__ = HTTPError('https://www.boatrace.jp/',403,'Forbidden',{},io.BytesIO(b'Forbidden'))
        before, odds = [task['SOURCE_ID'] for task in self.plan]
        self.responses[before] = [failure]
        self.runner.store.response_receipt = lambda _: None
        def ledger_state():
            return {'STOPPED': bool(self.attempts), 'PAUSE_UNTIL': {}}
        self.runner.store.ledger = types.SimpleNamespace(state=ledger_state)
        collect = retry._restricted_collector(self.original, None, None, isolate_host=True)
        with patch('predeadline_refusal_evidence_v1.Thread') as thread:
            result = collect(self.runner, max_requests=8, early_wait_seconds=0)
            thread.return_value.start.assert_called_once()
        self.assertIn('G11_OFFICIAL_REFUSAL=',self.log.getvalue())
        self.assertEqual(self.attempts,[(before,BASE)])
        self.assertEqual(result['RETRYABLE_FAILURES'][0]['REASON'],'HTTP_403')
        self.assertTrue(any(row['SOURCE_ID']==odds and row['REASON']=='PIPELINE_STOPPED'
                            for row in result['TASK_DEFERRED']))
        self.assertEqual(self.writes,[])

    def test_short_recovery_window_defers_only_that_race_without_starving_later_inputs(self):
        self.setup_paced_day()
        self.now = datetime.fromisoformat('2026-11-27T11:26:13+09:00')
        self.prior_start = self.now - timedelta(minutes=1)
        first_key, later_key = '20261127-14-06', '20261127-18-07'
        self.plan = []
        for key, remaining in ((first_key, 40), (later_key, 300)):
            cutoff = self.now + timedelta(seconds=remaining)
            for kind in ('BEFOREINFO', 'ODDS3T'):
                self.plan.append(dict(SOURCE_ID=kind.lower()+'-'+key, RACE_KEY=key, KIND=kind,
                    URL='https://www.boatrace.jp/fixture/'+kind+'/'+key,
                    DUE_AT_JST=(cutoff-timedelta(minutes=7)).isoformat(),
                    MUST_FINISH_BEFORE_JST=cutoff.isoformat()))
        considered, fetched = [], []
        def fetch(sid, url, validator):
            considered.append(sid)
            if sid.endswith(first_key):
                raise Deferred(self.now+timedelta(seconds=40), 'LIVE_RECOVERY_DEADLINE_TOO_CLOSE')
            fetched.append(sid)
            self.attempts.append((sid, self.now))
            return {'CAPTURED_AT_JST': self.now.isoformat()}, validator(b'complete')
        self.runner.client.fetch_validated_once = fetch
        result = self.run_capture()
        expected_later = [row['SOURCE_ID'] for row in self.plan if row['RACE_KEY'] == later_key]
        self.assertEqual(result['ACQUIRED'], expected_later)
        self.assertEqual(fetched, expected_later)
        self.assertEqual({row['SOURCE_ID'] for row in result['TASK_DEFERRED']},
                         {row['SOURCE_ID'] for row in self.plan if row['RACE_KEY'] == first_key})
        self.assertTrue(all(row['REASON'] == 'LIVE_RECOVERY_DEADLINE_TOO_CLOSE'
                            for row in result['TASK_DEFERRED']))
        self.assertIsNone(result['DEFERRED'])
        self.assertEqual(considered, [row['SOURCE_ID'] for row in self.plan])
        self.assertTrue(all(seconds <= retry.DEGRADED_SPACING_SECONDS for seconds in self.sleeps))

    def test_recovery_claim_deferrals_reach_existing_fallback_without_official_http(self):
        # Replay the reviewed frozen collector and unchanged fallback selector.
        # The recovery service is refused before any official request starts.
        source_root = Path(os.environ.get('G11_RETRY_SOURCE_ROOT',
            Path(__file__).resolve().parents[1] / '.g11-private'))
        failover_path = source_root / 'g11/canonical/live_source_failover_v1.py'
        tree = ast.parse(failover_path.read_text())
        tree.body = [node for node in tree.body
                     if not isinstance(node, ast.ImportFrom) or node.level == 0]
        for reason in ('LIVE_RECOVERY_ADMISSION_REQUIRED', 'LIVE_RECOVERY_CLAIM_DEFERRED',
                       'LIVE_RECOVERY_CLAIM_SPENT'):
            with self.subTest(reason=reason):
                self.attempts.clear(); self.sleeps.clear(); self.writes.clear()
                self.setup_paced_day()
                self.now = datetime.fromisoformat('2026-11-27T11:26:13+09:00')
                self.prior_start = self.now - timedelta(minutes=1)
                self.state['PAUSE_UNTIL'] = {'www.boatrace.jp':
                    datetime.fromisoformat('2026-11-27T11:22:21+09:00')}
                open_keys = {'20261127-14-06', '20261127-18-07'}
                future, closed, excluded = ('20261127-13-04', '20261127-02-02', '20261127-11-04')
                self.plan = []
                for key, delta in [(key, 5) for key in sorted(open_keys)] + [
                        (future, 9), (closed, -1), (excluded, 5)]:
                    cutoff = self.now + timedelta(minutes=delta)
                    for kind in ('BEFOREINFO', 'ODDS3T'):
                        self.plan.append(dict(SOURCE_ID=kind.lower()+'-'+key,
                            RACE_KEY=key, KIND=kind,
                            URL='https://www.boatrace.jp/fixture/'+kind+'/'+key,
                            DUE_AT_JST=(cutoff-timedelta(minutes=7)).isoformat(),
                            MUST_FINISH_BEFORE_JST=cutoff.isoformat()))
                self.runner._formal_prediction_scope = lambda *_a, **_k: (
                    {}, open_keys | {future, closed}, [])
                def load(stage, name):
                    self.assertEqual((stage, name), ('morning', 'official-day-input'))
                    return {'SNAPSHOT': {'PAYLOAD': {
                        'OPERATIONAL_DATE_JST': '2026-11-27', 'PREDEADLINE_PLAN': self.plan,
                        'SCOPE': {'EXPECTED_RACE_COUNT': len({r['RACE_KEY'] for r in self.plan})}}}}
                self.runner.store.load_verified_snapshot = load
                admission_calls = []
                def denied(source_id, _url, _validator):
                    admission_calls.append(source_id)
                    raise Deferred(self.now+timedelta(seconds=60), reason)
                self.runner.client.fetch_validated_once = denied
                capture = retry._restricted_collector(self.original, None, None,
                    early_capture=True, isolate_host=True)
                result = capture(self.runner, max_requests=8, early_wait_seconds=0)
                self.assertEqual(admission_calls, [self.plan[0]['SOURCE_ID']])
                self.assertEqual(result['DEFERRED']['REASON'], reason)
                self.assertEqual({row['RACE_KEY'] for row in result['TASK_DEFERRED']},
                                 open_keys | {future})
                self.assertTrue(all(row['REASON'] == reason for row in result['TASK_DEFERRED']))
                self.assertFalse(self.writes); self.assertFalse(self.sleeps)

                preview, market = Mock(return_value=[]), Mock(return_value=[])
                namespace = dict(preview=types.SimpleNamespace(capture_missing=preview),
                    markets=types.SimpleNamespace(capture_due=market),
                    availability=types.SimpleNamespace(load_verified=lambda *_: None))
                exec(compile(tree, str(failover_path), 'exec'), namespace)
                fallback = namespace['capture'](self.runner, result)['LIVE_SOURCE_FAILOVER']
                self.assertEqual(set(fallback['ELIGIBLE_BEFOREINFO']), open_keys)
                self.assertEqual(set(fallback['ELIGIBLE_ODDS3T']), open_keys)
                self.assertEqual({row['PRIMARY_SOURCE_ID'] for row in fallback['FAILED_INPUTS']},
                    {kind+'-'+key for kind in ('beforeinfo', 'odds3t') for key in open_keys})
                preview.assert_called_once_with(self.runner, race_keys=open_keys, allow_missing=True)
                market.assert_called_once_with(self.runner, max_requests=1,
                    race_keys=open_keys, allow_missing=True)
                preview.reset_mock(); market.reset_mock()
                self.state['STOPPED'] = True
                self.assertEqual(namespace['capture'](self.runner, result)['LIVE_SOURCE_FAILOVER']['STATUS'],
                                 'GLOBAL_STOPPED')
                preview.assert_not_called(); market.assert_not_called()
                self.state['STOPPED'] = False
                missing_alone = {**result, 'TASK_DEFERRED': [], 'DEFERRED': None}
                self.assertEqual(namespace['capture'](self.runner, missing_alone)['LIVE_SOURCE_FAILOVER']['STATUS'],
                                 'NO_FAILED_OPEN_INPUTS')
                preview.assert_not_called(); market.assert_not_called()

    def test_unknown_recovery_deferral_keeps_original_global_failure_policy(self):
        self.setup_paced_day()
        self.prior_start = self.now - timedelta(minutes=1)
        def denied(*_args):
            raise Deferred(self.now+timedelta(seconds=60), 'LIVE_RECOVERY_UNKNOWN_POLICY')
        self.runner.client.fetch_validated_once = denied
        capture = retry._restricted_collector(self.original, None, None, isolate_host=True)
        result = capture(self.runner, max_requests=8, early_wait_seconds=0)
        self.assertEqual(result['DEFERRED']['REASON'], 'LIVE_RECOVERY_UNKNOWN_POLICY')
        self.assertEqual(result['TASK_DEFERRED'], [])
        self.assertFalse(self.writes); self.assertFalse(self.sleeps)

    def test_transient_empty_pair_retries_in_process_and_saves_before_cutoff(self):
        result = self.run_capture()
        self.assertEqual(len(result['ACQUIRED']), 2)
        self.assertEqual(result['REJECTED'], [])
        self.assertEqual(self.sleeps, [30])
        self.assertEqual(len(self.attempts), 4)
        self.assertTrue(all(at == BASE+timedelta(seconds=30) for _, at in self.writes))
        self.assertIn('ATTEMPTED_ONCE', self.log.getvalue())

    def install_actual_boundary(self):
        root = Path(os.environ.get('G11_RETRY_SOURCE_ROOT',
            Path(__file__).resolve().parents[1] / '.g11-private'))
        ns = {}
        path = root / 'g11/canonical/daily_runtime_boundary_v1.py'
        exec(compile(path.read_text(), str(path), 'exec'), ns)
        for name in ('run_morning', 'run_morning_predictions',
                     'run_due_predeadline_predictions', 'run_predeadline_cycle', 'run_night'):
            setattr(self.Runner, name, lambda *a, **k: None)
        (self.root / 'manifest.json').write_text('{"OPERATIONAL_DATE_JST":"2026-10-30"}')
        ns['install_daily_boundaries'](self.Runner, root)
        self.original = self.Runner.run_due_predeadline

    def test_actual_authority_wrapper_keeps_correct_globals_and_rechecks_retry(self):
        self.install_actual_boundary()
        calls = []
        daily = types.SimpleNamespace(
            verify_runtime=lambda *a, **k: calls.append('runtime'),
            verify_locks=lambda state, day, phase, **k: calls.append(phase))
        with patch.dict('sys.modules', {'g11.relay': types.SimpleNamespace(daily_runtime_authority_v1=daily)}):
            result = self.run_capture()
        self.assertEqual(len(result['ACQUIRED']), 2)
        self.assertEqual(calls, ['runtime', 'MORNING', 'PREDEADLINE'] * 2)
        self.assertNotIn('PREDEADLINE_EARLY_WAIT_SECONDS', self.original.__globals__)

    def test_authority_failure_on_second_call_prevents_retry_writes(self):
        self.install_actual_boundary()
        calls = []
        def verify(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError('DAILY_RUNTIME_MISMATCH')
        daily = types.SimpleNamespace(verify_runtime=verify, verify_locks=lambda *a, **k: None)
        with patch.dict('sys.modules', {'g11.relay': types.SimpleNamespace(daily_runtime_authority_v1=daily)}):
            with self.assertRaisesRegex(RuntimeError, 'DAILY_RUNTIME_MISMATCH'):
                self.run_capture()
        self.assertEqual(len(self.attempts), 2)
        self.assertEqual(self.writes, [])

    def test_permanent_invalid_values_are_never_admitted_and_not_looped(self):
        self.responses = {r['SOURCE_ID']: ['reject'] for r in self.plan}
        result = self.run_capture()
        self.assertEqual(result['ACQUIRED'], [])
        self.assertEqual(len(result['REJECTED']), 2)
        self.assertEqual(len(self.attempts), 4)
        self.assertEqual(self.writes, [])

    def test_any_new_success_returns_without_delaying_publication(self):
        self.responses[self.plan[1]['SOURCE_ID']] = ['complete']
        result = self.run_capture()
        self.assertEqual(len(result['ACQUIRED']), 1)
        self.assertEqual(len(self.attempts), 2)
        self.assertEqual(self.sleeps, [])
        self.assertIn('PUBLICATION_READY', self.log.getvalue())

    def test_healthy_capture_is_identical_to_existing_return(self):
        self.responses = {r['SOURCE_ID']: ['complete'] for r in self.plan}
        result = self.run_capture()
        self.assertEqual(len(result['ACQUIRED']), 2)
        self.assertEqual(len(self.attempts), 2)
        self.assertEqual(self.sleeps, [])

    def test_already_saved_snapshot_is_not_replaced(self):
        sid = self.plan[0]['SOURCE_ID']
        path = self.root/'snapshots/predeadline'/f'{sid}.json'
        path.write_text('{"existing":true}')
        original = path.read_bytes()
        result = self.run_capture()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(result['ACQUIRED'], [self.plan[1]['SOURCE_ID']])
        self.assertTrue(all(source != sid for source, _ in self.attempts))

    def test_newly_due_other_race_is_not_pulled_into_retry(self):
        future = [self.task(kind, '20261030-18-03') for kind in ('BEFOREINFO', 'ODDS3T')]
        for row in future:
            row['DUE_AT_JST'] = (BASE+timedelta(seconds=20)).isoformat()
            row['MUST_FINISH_BEFORE_JST'] = (BASE+timedelta(minutes=7, seconds=20)).isoformat()
        self.plan.extend(future)
        result = self.run_capture()
        self.assertEqual(len(result['ACQUIRED']), 2)
        self.assertTrue(all(source.endswith(KEY) for source, _ in self.attempts))

    def test_more_than_two_rejections_returns_to_normal_scheduler(self):
        self.plan.extend(self.task(kind, '20261030-18-03') for kind in ('BEFOREINFO', 'ODDS3T'))
        result = self.run_capture()
        self.assertEqual(len(result['REJECTED']), 4)
        self.assertEqual(len(self.attempts), 4)
        self.assertEqual(self.sleeps, [])

    def test_http_failure_keeps_existing_policy(self):
        self.responses[self.plan[0]['SOURCE_ID']] = [PipelineError('HTTP_429')]
        self.run_capture()
        self.assertEqual(len(self.attempts), 2)
        self.assertEqual(self.sleeps, [])

    def test_exhausted_request_budget_is_not_bypassed(self):
        self.responses[self.plan[0]['SOURCE_ID']] = ['reject', PipelineError('DAILY_REQUEST_CAP')]
        with self.assertRaisesRegex(PipelineError, 'DAILY_REQUEST_CAP'):
            self.run_capture()
        self.assertEqual(self.writes, [])

    def test_exhausted_content_budget_remains_unavailable(self):
        self.responses = {r['SOURCE_ID']: ['reject', PipelineError('SOURCE_CONTENT_RETRY_EXHAUSTED')] for r in self.plan}
        result = self.run_capture()
        self.assertEqual(len(result['UNAVAILABLE']), 2)
        self.assertEqual(self.writes, [])

    def test_reviewed_retry_retains_terminal_evidence_without_acquiring_forecast_inputs(self):
        if 'PREDEADLINE_AVAILABILITY' not in self.original.__code__.co_names:
            self.skipTest('legacy frozen collector has no withdrawal boundary')
        key = '20261111-01-01'
        self.now = datetime(2026, 11, 11, 12, 4, tzinfo=JST)
        self.plan = [self.task(kind, key) for kind in ('BEFOREINFO', 'ODDS3T')]
        for task in self.plan:
            task.update(DUE_AT_JST=(self.now-timedelta(minutes=2)).isoformat(),
                        MUST_FINISH_BEFORE_JST=(self.now+timedelta(minutes=5)).isoformat())
        self.responses['beforeinfo-'+key] = ['reject']
        helper = self.original.__globals__['PREDEADLINE_AVAILABILITY']
        normal_parse = helper.parse_odds_source
        helper.parse_odds_source = lambda data, race: (
            {'AVAILABILITY': {'WITHDRAWN_BOATS': [3], 'ACTIVE_BOATS': [1, 2, 4, 5, 6]}}
            if self.counts.get('odds3t-'+race, 0) >= 2 else normal_parse(data, race))
        evidence_path = self.root/'snapshots/predeadline'/('availability-'+key+'.json')
        def saved_evidence(_store, _task, _receipt, _withdrawal, _now):
            verified = {'SNAPSHOT_SHA256': 'd'*64}
            evidence_path.write_text(json.dumps(verified))
            return verified
        helper.save = saved_evidence
        helper.load_verified = lambda *_: json.loads(evidence_path.read_text()) if evidence_path.exists() else None
        helper.unavailable_item = lambda task, proof: {
            'SOURCE_ID': task['SOURCE_ID'], 'RACE_KEY': task['RACE_KEY'], 'REASON': helper.REASON,
            'AVAILABILITY_SNAPSHOT_SHA256': proof['SNAPSHOT_SHA256']}
        result = self.run_capture()
        self.assertEqual([], result['ACQUIRED'])
        self.assertEqual([], result['REJECTED'])
        self.assertEqual([], result['TASK_DEFERRED'])
        self.assertEqual(1, len(result['UNAVAILABLE']))
        self.assertEqual([], self.writes)
        self.assertEqual(4, len(self.attempts))
        evidence = evidence_path.read_bytes()
        repeated = self.run_capture()
        self.assertEqual(2, len(repeated['UNAVAILABLE']))
        self.assertEqual(4, len(self.attempts))
        self.assertEqual(evidence, evidence_path.read_bytes())

    def test_close_to_deadline_is_not_retried(self):
        self.now = BASE + timedelta(minutes=3)
        self.run_capture()
        self.assertEqual(len(self.attempts), 2)
        self.assertEqual(self.sleeps, [])

    def test_delayed_wakeup_skips_retry(self):
        self.runner.sleeper = lambda seconds: self.sleep(seconds+180)
        self.run_capture()
        self.assertEqual(len(self.attempts), 2)
        self.assertEqual(self.writes, [])

    def test_response_after_deadline_is_never_committed(self):
        self.responses[self.plan[0]['SOURCE_ID']] = ['reject', 'late']
        with self.assertRaisesRegex(PipelineError, 'PREDEADLINE_CAPTURE_LATE'):
            self.run_capture()
        self.assertEqual(self.writes, [])

    def test_non_nowait_caller_retains_original_behavior(self):
        self.run_capture(wait=1)
        self.assertEqual(len(self.attempts), 2)
        self.assertEqual(self.sleeps, [])

    def test_retry_start_window_stops_later_requests(self):
        restricted = retry._restricted_collector(self.original,
            [r['SOURCE_ID'] for r in self.plan], BASE)
        result = restricted(self.runner, max_requests=2, early_wait_seconds=0)
        self.assertEqual(result['ACQUIRED'], [])
        self.assertEqual(self.attempts, [])

    def test_actual_source_identity_matches_reviewed_hash(self):
        self.assertIn(hashlib.sha256(inspect.getsource(self.original).encode()).hexdigest(), retry.COLLECTOR_SHAS)
        self.assertIn(retry.COLLECTOR_SHA, retry.COLLECTOR_SHAS)

    def test_restricted_collector_preserves_verified_alternate_readback_before_skipping_primary(self):
        if hashlib.sha256(inspect.getsource(self.original).encode()).hexdigest() != retry.ALTERNATE_COLLECTOR_SHA:
            self.skipTest('retained collector predates shared alternate reuse')
        roster = object()
        for prefix in ('published-exhibition-', 'venue-market-'):
            (self.root/'snapshots/predeadline'/f'{prefix}{KEY}.json').write_text('retained alternate')
        self.responses = {task['SOURCE_ID']: ['complete'] for task in self.plan}
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt):
                calls = []
                def recheck(store, key, deadline, actual_roster):
                    self.assertIs(store, self.runner.store)
                    self.assertEqual(KEY, key)
                    self.assertEqual(BASE+timedelta(minutes=5), deadline)
                    self.assertIs(roster, actual_roster)
                    calls.append(key)
                    if corrupt:
                        raise ValueError('RAW_READBACK_BINDING')
                with patch.dict(self.original.__globals__, {
                    'PUBLISHED_PREVIEW': types.SimpleNamespace(race_roster=lambda *_: roster, read_observation=recheck),
                    'VENUE_MARKET': types.SimpleNamespace(recheck=recheck),
                }):
                    capture = retry._restricted_collector(self.original, None, None, early_capture=True)
                    result = capture(self.runner, max_requests=2, early_wait_seconds=0)
                self.assertEqual([KEY, KEY], calls)
                self.assertEqual(2 if corrupt else 0, len(self.attempts))
                self.assertEqual(2 if corrupt else 0, len(result['ACQUIRED']))
                if not corrupt:
                    self.assertEqual(['VERIFIED_ALTERNATE_ALREADY_SAVED']*2,
                        [item['REASON'] for item in result['SKIPPED']])
                    self.assertEqual([], self.writes)

    def test_shared_input_collector_changes_only_the_exact_reviewed_readback_reuse_block(self):
        source = inspect.getsource(self.original)
        if hashlib.sha256(source.encode()).hexdigest() != retry.ALTERNATE_COLLECTOR_SHA:
            self.skipTest('retained collector predates shared alternate reuse')
        function = ast.parse(textwrap.dedent(source)).body[0]
        matches = []
        for node in ast.walk(function):
            for _field, value in ast.iter_fields(node):
                if not isinstance(value, list):
                    continue
                for index in range(len(value)-1):
                    assignment = value[index]
                    if (isinstance(assignment, ast.Assign)
                            and any(isinstance(target, ast.Name) and target.id == 'alternate_name'
                                    for target in assignment.targets)):
                        matches.append((value, index))
        self.assertEqual(1, len(matches))
        statements, index = matches[0]
        reuse = ast.Module(body=statements[index:index+2], type_ignores=[])
        self.assertEqual('770a82d96a5d56d641056d0980d3ad1e86c90405f84df43c0ebaaeb6d718b6cf',
            hashlib.sha256(ast.dump(reuse).encode()).hexdigest())
        del statements[index:index+2]
        # Exact run_due_predeadline AST from approved private 2b988a726cebe7c2923212bb97e44b6f2196cfcf.
        self.assertEqual('cf39162ae8e25f04e1ac312ee2548e7b1ec085adc24d93cbfa4730cdc47a9f18',
            hashlib.sha256(ast.dump(function).encode()).hexdigest())

    def test_unreviewed_source_is_not_compiled(self):
        with patch.object(retry, 'COLLECTOR_SHAS', frozenset({'0'*64})):
            with self.assertRaisesRegex(ValueError, 'UNREVIEWED_COLLECTOR'):
                retry._restricted_collector(self.original, [], BASE)

    def test_unreviewed_source_keeps_original_context_binding(self):
        module = types.ModuleType('g11.canonical.official_pipeline')
        module.CanonicalRunner = self.Runner
        with patch.dict('sys.modules', {'g11.canonical.official_pipeline': module}), \
             patch.object(retry, 'COLLECTOR_SHAS', frozenset({'0'*64})):
            with retry.bounded_content_retry():
                self.assertIs(self.Runner.run_due_predeadline, self.original)
        self.assertIs(self.Runner.run_due_predeadline, self.original)
        self.assertIn('UNREVIEWED_SOURCE_UNCHANGED', self.log.getvalue())

    def test_context_restores_binding_after_failure(self):
        module = types.ModuleType('g11.canonical.official_pipeline')
        module.CanonicalRunner = self.Runner
        with patch.dict('sys.modules', {'g11.canonical.official_pipeline': module}):
            with self.assertRaisesRegex(RuntimeError, 'fixture'):
                with retry.bounded_content_retry():
                    self.assertIsNot(self.Runner.run_due_predeadline, self.original)
                    raise RuntimeError('fixture')
        self.assertIs(self.Runner.run_due_predeadline, self.original)

    def test_live_adapter_observes_http_without_changing_capture_or_model_inputs(self):
        from unittest.mock import Mock
        from urllib.request import Request
        self.setup_paced_day()  # Exercises the actual compiled host-isolation path.
        module = types.ModuleType('g11.canonical.official_pipeline')
        module.CanonicalRunner = self.Runner
        response = types.SimpleNamespace(status=200, headers={'Server-Timing': 'edge;dur=9000, origin;dur=20'})
        opener = Mock(return_value=response)
        self.runner.client.opener = opener
        self.responses = {task['SOURCE_ID']: ['complete'] for task in self.plan}
        for task in self.plan:
            task['URL'] = ('https://www.boatrace.jp/owpc/pc/race/' + task['KIND'].lower()
                           + '?hd=20261119&jcd=14&rno=3')
        def fetch(source, url, validator):
            self.assertIs(self.runner.client.opener(Request(url), timeout=30), response)
            return self.fetch(source, url, validator)
        self.runner.client.fetch_validated_once = fetch
        with patch.dict('sys.modules', {'g11.canonical.official_pipeline': module}):
            with retry.bounded_content_retry():
                result = self.runner.run_due_predeadline(max_requests=8, early_wait_seconds=0)
        self.assertEqual(result['ACQUIRED'], [row['SOURCE_ID'] for row in self.plan])
        self.assertEqual((opener.call_count, len(self.writes), self.sleeps), (2, 2, [11, 15]))
        self.assertIs(self.runner.client.opener, opener)
        self.assertIs(self.Runner.run_due_predeadline, self.original)
        events = [json.loads(line.split('=', 1)[1]) for line in self.log.getvalue().splitlines()
                  if line.startswith('G11_OFFICIAL_RESPONSE=')]
        self.assertEqual([row['source'] for row in events], result['ACQUIRED'])
        self.assertTrue(all(row['status'] == 200 for row in events))
        self.assertNotIn('G11_OFFICIAL_REFUSAL=', self.log.getvalue())

    def test_live_adapter_restores_opener_when_collector_raises(self):
        from unittest.mock import Mock
        module = types.ModuleType('g11.canonical.official_pipeline')
        module.CanonicalRunner = self.Runner
        error = RuntimeError('private transport failure')
        opener = Mock(side_effect=error)
        self.runner.client.opener = opener
        self.runner.client.fetch_validated_once = lambda *_: self.runner.client.opener(
            'https://www.boatrace.jp/owpc/pc/race/beforeinfo?hd=20261030&jcd=14&rno=3')
        with patch.dict('sys.modules', {'g11.canonical.official_pipeline': module}):
            with self.assertRaises(RuntimeError) as caught:
                with retry.bounded_content_retry():
                    self.runner.run_due_predeadline(max_requests=8, early_wait_seconds=0)
        self.assertIs(caught.exception, error)
        self.assertIs(self.runner.client.opener, opener)
        self.assertIs(self.Runner.run_due_predeadline, self.original)
        self.assertEqual((opener.call_count, len(self.writes)), (1, 0))
        self.assertNotIn('private transport failure', self.log.getvalue())

    def test_real_nowait_policy_nests_and_restores_with_retry_adapter(self):
        root = Path(os.environ.get('G11_RETRY_SOURCE_ROOT',
            Path(__file__).resolve().parents[1] / '.g11-private'))
        path = root/'g11/relay/predeadline_nowait_v1.py'
        tree = ast.parse(path.read_text())
        tree.body = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                     and n.name == 'scheduling_policy']
        ns = {'contextmanager': contextlib.contextmanager, 'wraps': __import__('functools').wraps,
              'inspect': inspect, 'CanonicalRunner': self.Runner, 'CAPTURE_SUCCESS_LIMIT': 8,
              'Any': Any, 'Iterator': __import__('typing').Iterator}
        exec(compile(tree, str(path), 'exec'), ns)
        module = types.ModuleType('g11.canonical.official_pipeline')
        module.CanonicalRunner = self.Runner
        with patch.dict('sys.modules', {'g11.canonical.official_pipeline': module}):
            with retry.bounded_content_retry():
                with ns['scheduling_policy']():
                    result = self.runner.run_due_predeadline()
        self.assertEqual(len(result['ACQUIRED']), 2)
        self.assertEqual(len(self.attempts), 4)
        self.assertIs(self.Runner.run_due_predeadline, self.original)

    def test_short_existing_source_pause_can_be_revisited_once(self):
        self.pauses = {r['SOURCE_ID']: BASE+timedelta(seconds=30) for r in self.plan}
        self.responses = {r['SOURCE_ID']: ['complete'] for r in self.plan}
        result = self.run_capture()
        self.assertEqual(len(result['ACQUIRED']), 2)
        self.assertEqual(self.sleeps, [30])
        self.assertEqual(len(self.attempts), 2)

    def test_source_binding_and_long_pauses_are_not_relaxed(self):
        first = self.original(self.runner, max_requests=8, early_wait_seconds=0)
        first['REJECTED'][0]['RACE_KEY'] = '20261030-01-01'
        with self.assertRaisesRegex(ValueError, 'SOURCE_BINDING'):
            retry.select_retry(first, self.plan, self.now)
        first['REJECTED'][0]['RACE_KEY'] = KEY
        first['REJECTED'][0]['RETRY_AT_JST'] = (BASE+timedelta(minutes=20)).isoformat()
        self.assertEqual(retry.select_retry(first, self.plan, self.now)[0], [])

    def test_only_filter_and_retry_start_budget_differ_from_frozen_collector(self):
        source = inspect.getsource(self.original)
        captured = {}
        real_compile = compile
        def observe(code, *args, **kwargs):
            captured['source'] = code
            return real_compile(code, *args, **kwargs)
        with patch('builtins.compile', side_effect=observe):
            retry._restricted_collector(self.original, [], BASE)
        before = ast.parse(__import__('textwrap').dedent(source))
        after = ast.parse(captured['source'])
        loop = next(n for n in ast.walk(after) if isinstance(n, ast.While))
        self.assertEqual(len(loop.body), len(next(n for n in ast.walk(before) if isinstance(n, ast.While)).body)+2)
        self.assertIsInstance(loop.body[1], ast.If)
        self.assertIsInstance(loop.body[2], ast.If)
        del loop.body[1:3]
        self.assertEqual(ast.dump(after), ast.dump(before))


class TerminalWithdrawalMergeTests(unittest.TestCase):
    def setUp(self):
        self.key = '20261111-01-01'
        self.first = {key: [] for key in ('ACQUIRED', 'MISSED', 'SKIPPED', 'REJECTED',
            'TASK_DEFERRED', 'RETRYABLE_FAILURES', 'UNAVAILABLE')}
        self.first.update(DEFERRED=None, STATUS='PASS')
        self.second = copy.deepcopy(self.first)
        self.first['REJECTED'] = [{'SOURCE_ID': 'beforeinfo-'+self.key, 'RACE_KEY': self.key,
                                   'REASON': 'HTML_NUMBER_PARSE'}]
        self.first['TASK_DEFERRED'] = [{'SOURCE_ID': 'odds3t-'+self.key, 'RACE_KEY': self.key,
                                        'REASON': 'SOURCE_CONTENT_PAUSED'}]
        self.second['UNAVAILABLE'] = [{'SOURCE_ID': kind+'-'+self.key, 'RACE_KEY': self.key,
            'REASON': 'OFFICIAL_WITHDRAWAL_UNSUPPORTED_FIELD_SIZE',
            'AVAILABILITY_SNAPSHOT_SHA256': 'a'*64} for kind in ('beforeinfo', 'odds3t')]

    def test_proof_backed_terminal_race_resolves_only_its_content_waits(self):
        unrelated = {'SOURCE_ID': 'beforeinfo-20261111-01-02', 'RACE_KEY': '20261111-01-02',
                     'REASON': 'HTML_NUMBER_PARSE'}
        http_failure = {'SOURCE_ID': 'beforeinfo-'+self.key, 'RACE_KEY': self.key,
                        'REASON': 'HTTP_429'}
        self.first['REJECTED'].append(unrelated)
        self.first['RETRYABLE_FAILURES'].append(http_failure)
        before = copy.deepcopy((self.first, self.second))
        merged = retry.merge_attempts(self.first, self.second)
        self.assertEqual([unrelated], merged['REJECTED'])
        self.assertEqual([], merged['TASK_DEFERRED'])
        self.assertEqual([http_failure], merged['RETRYABLE_FAILURES'])
        self.assertEqual([], merged['ACQUIRED'])
        self.assertEqual(self.second['UNAVAILABLE'], merged['UNAVAILABLE'])
        self.assertEqual(before, (self.first, self.second))

    def test_unproven_or_generic_unavailability_does_not_resolve_content_waits(self):
        for field, bad in (
            ('REASON', 'SOURCE_CONTENT_RETRY_EXHAUSTED'),
            ('AVAILABILITY_SNAPSHOT_SHA256', None), ('AVAILABILITY_SNAPSHOT_SHA256', 'bad'),
            ('SOURCE_ID', 'unknown-'+self.key), ('RACE_KEY', '20261111-01-02'),
            ('RACE_KEY', '20261110-01-01'),
        ):
            with self.subTest(field=field, value=bad):
                second = copy.deepcopy(self.second)
                for row in second['UNAVAILABLE']:
                    row[field] = bad
                merged = retry.merge_attempts(self.first, second)
                self.assertEqual(self.first['REJECTED'], merged['REJECTED'])
                self.assertEqual(self.first['TASK_DEFERRED'], merged['TASK_DEFERRED'])

    def test_terminal_unavailability_does_not_trigger_another_retry(self):
        now = datetime(2026, 11, 11, 12, 4, tzinfo=JST)
        result = retry.merge_attempts(self.first, self.second)
        self.assertEqual(([], 0, 'EXISTING_FAILURE_POLICY'), retry.select_retry(result, [], now))


if __name__ == '__main__':
    unittest.main()


