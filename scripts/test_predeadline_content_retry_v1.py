"""Replay the actual reviewed collector, without network or prediction models."""
import ast
import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from typing import Any
from unittest.mock import patch

import predeadline_content_retry_v1 as retry

JST = timezone(timedelta(hours=9))
BASE = datetime(2026, 10, 30, 9, 22, tzinfo=JST)
KEY = '20261030-14-03'


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
          'parse_beforeinfo_html': parse, 'parse_odds3t_html': parse}
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
        self.assertEqual(hashlib.sha256(inspect.getsource(self.original).encode()).hexdigest(), retry.COLLECTOR_SHA)

    def test_unreviewed_source_is_not_compiled(self):
        with patch.object(retry, 'COLLECTOR_SHA', '0'*64):
            with self.assertRaisesRegex(ValueError, 'UNREVIEWED_COLLECTOR'):
                retry._restricted_collector(self.original, [], BASE)

    def test_context_restores_binding_after_failure(self):
        module = types.ModuleType('g11.canonical.official_pipeline')
        module.CanonicalRunner = self.Runner
        with patch.dict('sys.modules', {'g11.canonical.official_pipeline': module}):
            with self.assertRaisesRegex(RuntimeError, 'fixture'):
                with retry.bounded_content_retry():
                    self.assertIsNot(self.Runner.run_due_predeadline, self.original)
                    raise RuntimeError('fixture')
        self.assertIs(self.Runner.run_due_predeadline, self.original)

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


if __name__ == '__main__':
    unittest.main()

