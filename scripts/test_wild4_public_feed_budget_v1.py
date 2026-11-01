"""Exercise the deployed prior-public-feed guard; only curl is substituted.

WILD4_WORKFLOW_PATH points to the pinned reusable workflow checked out by CI.
An optional WILD4_REAL_PUBLIC_FEED_PATH exercises an incident's exact raw GET
without adding that large public feed to the repository.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

import hot_delta_transport_v1 as hot


ROOT = Path(__file__).resolve().parents[1]
DAY = '2026-11-01'
LIMIT = 16 * 1024 * 1024
REAL_FEED = os.environ.get('WILD4_REAL_PUBLIC_FEED_PATH')


class PriorPublicFeedBudgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(os.environ.get('WILD4_WORKFLOW_PATH',
                                   ROOT / '.github/workflows/g11-wild4-shadow-lock-only.yml'))
        source = path.read_text()
        match = re.search(r"          python3 - <<'PYPUBLISHEDLOCK'\n(.*?)\n          PYPUBLISHEDLOCK",
                          source, re.S)
        if match is None:
            raise AssertionError('Pinned public-feed guard not found')
        cls.script = compile(textwrap.dedent(match.group(1)), str(path), 'exec')

    def feed(self):
        # Only the read boundary is under test, not prediction admission.
        return {'schema': 'G11_PUBLIC_APP_FEED_V2', 'status': 'PASS', 'stage': 'NIGHT',
                'operational_date_jst': DAY, 'races': [{
                    'key': '20261101-14-01', 'venue': '鳴門',
                    'p3_snapshot_sha256': 'a' * 64,
                    'predeadline': {'snapshot_sha256': 'b' * 64},
                    'wild_pack': {'wild4': {'goripan': {
                        'lock_sha256': 'c' * 64, 'bets': ['1-2-3']}}}}]}

    def raw(self, size=None, **changes):
        value = self.feed()
        value.update(changes)
        # Noncanonical whitespace and UTF-8 make a reserialization observable.
        raw = json.dumps(value, ensure_ascii=False, indent=2).encode() + b'\n'
        if size is not None:
            self.assertLessEqual(len(raw), size)
            raw += b' ' * (size - len(raw))
        return raw

    def exercise(self, raw, header_sha=None):
        original_hash = hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / 'goripan-prior-public-feed.json'
            working = root / 'projection-source'
            working.mkdir()
            previous_cwd, previous_path = Path.cwd(), sys.path[:]
            calls = []

            def curl(args, *, check):
                self.assertTrue(check)
                self.assertEqual(args[0], 'curl')
                self.assertEqual(args[-1],
                    'https://g11-race-lab.higatatsunori2.chatgpt.site/api/g11-feed?view=v53-final&date=' + DAY)
                calls.append(args)
                Path(args[args.index('--output') + 1]).write_bytes(raw)
                Path(args[args.index('--dump-header') + 1]).write_text(
                    'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'
                    'x-g11-feed-sha256: ' + (header_sha or original_hash) + '\r\n\r\n')
                return subprocess.CompletedProcess(args, 0)

            scope, error = {}, None
            try:
                os.chdir(working)
                with mock.patch.dict(os.environ, {'GITHUB_WORKSPACE': str(ROOT),
                                                  'RUNNER_TEMP': str(root),
                                                  'OPERATIONAL_DATE': DAY}), \
                     mock.patch.object(hot.subprocess, 'run', side_effect=curl):
                    try:
                        exec(self.script, scope)
                    except (Exception, SystemExit) as caught:
                        error = caught
            finally:
                os.chdir(previous_cwd)
                sys.path[:] = previous_path
            saved = output.read_bytes() if output.is_file() else None
            self.assertEqual(len(calls), 1)
            self.assertEqual(hashlib.sha256(raw).hexdigest(), original_hash)
            return saved, scope.get('feed'), error

    def assertAcceptedUnchanged(self, raw):
        original = json.loads(raw)
        saved, parsed, error = self.exercise(raw)
        self.assertIsNone(error, repr(error))
        self.assertEqual(saved, raw)
        self.assertEqual(parsed, original)
        self.assertEqual(hashlib.sha256(saved).hexdigest(), hashlib.sha256(raw).hexdigest())

    def test_night_over_old_eight_mib_limit_preserves_raw_bytes_and_predictions(self):
        self.assertAcceptedUnchanged(self.raw(9 * 1024 * 1024))

    def test_sixteen_mib_boundary_is_still_bounded(self):
        self.assertAcceptedUnchanged(self.raw(LIMIT))
        saved, _, error = self.exercise(self.raw(LIMIT + 1))
        self.assertIsNone(saved)
        self.assertIsInstance(error, SystemExit)
        self.assertEqual(str(error), 'GORIPAN_PRIOR_PUBLIC_FEED_BINDING')

    def test_predeadline_keeps_the_original_eight_mib_budget(self):
        self.assertAcceptedUnchanged(self.raw(8 * 1024 * 1024, stage='PREDEADLINE'))
        saved, _, error = self.exercise(self.raw(8 * 1024 * 1024 + 1, stage='PREDEADLINE'))
        self.assertIsNone(saved)
        self.assertIsInstance(error, SystemExit)
        self.assertEqual(str(error), 'GORIPAN_PRIOR_PUBLIC_FEED_BINDING')

    def test_wrong_day_and_nonpass_status_remain_rejected(self):
        for changes in ({'operational_date_jst': '2026-11-02'},
                        {'operational_date_jst': None}, {'status': 'FAIL'}, {'status': None}):
            with self.subTest(changes=changes):
                saved, _, error = self.exercise(self.raw(**changes))
                self.assertIsNone(saved)
                self.assertIsInstance(error, SystemExit)
                self.assertEqual(str(error), 'GORIPAN_PRIOR_PUBLIC_FEED_BINDING')

    def test_invalid_json_is_rejected_before_any_projection_input_is_saved(self):
        saved, _, error = self.exercise(b'{"status":"PASS","operational_date_jst":')
        self.assertIsNone(saved)
        self.assertIsInstance(error, json.JSONDecodeError)

    def test_exact_get_sha_is_required_before_any_projection_input_is_saved(self):
        saved, _, error = self.exercise(self.raw(), header_sha='f' * 64)
        self.assertIsNone(saved)
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(str(error), 'HOT_READBACK_SHA')

    @unittest.skipUnless(REAL_FEED, 'Set WILD4_REAL_PUBLIC_FEED_PATH for exact incident bytes')
    def test_exact_incident_night_feed_is_accepted_without_reserialization(self):
        raw = Path(REAL_FEED).read_bytes()
        self.assertGreater(len(raw), 8 * 1024 * 1024)
        self.assertLessEqual(len(raw), LIMIT)
        self.assertEqual(json.loads(raw)['stage'], 'NIGHT')
        self.assertAcceptedUnchanged(raw)


if __name__ == '__main__':
    unittest.main()
