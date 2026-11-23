"""Offline persisted-readback, one-request, failure and timeout contracts."""
import copy
from contextlib import redirect_stdout
from datetime import datetime
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import postprocess_delivery_v1 as post

DAY = '2026-11-23'
NOW = datetime.fromisoformat(DAY + 'T11:30:00+09:00')


class PersistedWorkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'
        self.dayroot = self.state / 'daily-runtime' / DAY
        self.receipts = self.dayroot / 'delivery/PREDEADLINE'
        self.receipts.mkdir(parents=True)
        self.authority = {'schema': 'G11_DAILY_RUNTIME_AUTHORITY_V1',
            'operational_date_jst': DAY, 'immutable': True, 'runtime_source_sha': 'a' * 40}
        self.authority['authority_sha256'] = post.digest(post.compact(self.authority))
        self.authority_path = self.dayroot / 'authority.json'
        self.authority_path.write_bytes(post.compact(self.authority))
        self.receipt = {'schema': 'G11_DAILY_SITE_DELIVERY_RECEIPT_V1',
            'operational_date_jst': DAY, 'phase': 'PREDEADLINE',
            'authority_sha256': self.authority['authority_sha256'],
            'runtime_source_sha': self.authority['runtime_source_sha'],
            'site_payload_sha256': 'b' * 64, 'site_get_sha256': 'c' * 64,
            'http_success': True, 'site_get_readback': 'PASS'}
        self.receipt_path = self.receipts / ('b' * 64 + '.json')
        self.save_receipt()
        self.env = patch.dict(os.environ, {'G11_APP_ORIGIN': post.ORIGIN,
            'POSTPROCESS_CHECKPOINT_VERIFIED': 'true',
            'POSTPROCESS_AUTHORITY_SHA': self.authority['authority_sha256']})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.token = Mock(return_value='fixture-credential')
        self.sender = Mock(return_value={'date': DAY, 'status': 'PENDING',
            'processed': {'stage': 'PRIOR_NIGHT', 'status': 'COMPLETE'},
            'pending': ['LIFECYCLE']})

    def save_receipt(self):
        unsigned = {k: v for k, v in self.receipt.items() if k != 'receipt_sha256'}
        self.receipt['receipt_sha256'] = post.digest(post.compact(unsigned))
        self.receipt_path.write_bytes(post.compact(self.receipt))

    def bytes_before(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob('*') if p.is_file()}

    def run_resume(self, **kwargs):
        with redirect_stdout(io.StringIO()) as output:
            result = post.resume(self.state, DAY, now=NOW,
                token_provider=self.token, sender=self.sender, **kwargs)
        self.assertNotIn('fixture-credential', output.getvalue())
        return result

    def test_one_stage_call_after_retained_formal_readback_keeps_all_saved_bytes(self):
        before = self.bytes_before()
        result = self.run_resume()
        self.assertEqual(result['status'], 'PENDING')
        self.assertFalse(result['postprocessing_complete'])
        self.assertEqual(result['pending'], ['LIFECYCLE'])
        self.token.assert_called_once_with()
        self.sender.assert_called_once_with(DAY, 'fixture-credential')
        self.assertEqual(before, self.bytes_before())

    def test_idle_next_live_resumes_without_generating_or_republishing_input(self):
        # Only retained authority+delivery receipt exist; no current feed or
        # predictor/model source is present or needed by this helper.
        first = self.run_resume()
        self.sender.return_value = {'date': DAY, 'status': 'COMPLETE',
            'processed': {'stage': 'LIFECYCLE', 'status': 'COMPLETE'}, 'pending': []}
        second = self.run_resume()
        self.assertFalse(first['postprocessing_complete'])
        self.assertTrue(second['postprocessing_complete'])
        self.assertEqual(self.sender.call_count, 2)

    def test_lost_response_is_held_and_never_retried_in_same_run(self):
        self.sender.side_effect = TimeoutError('Authorization sensitive fixture')
        before = self.bytes_before()
        result = self.run_resume()
        self.assertEqual(result['status'], 'HELD')
        self.assertFalse(result['postprocessing_complete'])
        self.assertIsNone(result['pending'])
        self.sender.assert_called_once()
        self.assertEqual(before, self.bytes_before())

    def test_checkpoint_origin_authority_and_readback_gates_send_zero_requests(self):
        cases = [('POSTPROCESS_CHECKPOINT_VERIFIED', 'false'),
                 ('G11_APP_ORIGIN', 'https://other.invalid'),
                 ('POSTPROCESS_AUTHORITY_SHA', 'f' * 64)]
        for key, value in cases:
            with self.subTest(key=key), patch.dict(os.environ, {key: value}):
                result = self.run_resume()
                self.assertEqual(result['status'], 'HELD')
        for field, value in [('site_get_readback', 'FAIL'), ('http_success', False),
                ('phase', 'MORNING'), ('runtime_source_sha', 'f' * 40),
                ('authority_sha256', 'f' * 64), ('operational_date_jst', '2026-11-22')]:
            with self.subTest(field=field):
                saved = copy.deepcopy(self.receipt)
                self.receipt[field] = value
                self.save_receipt()
                self.assertEqual(self.run_resume()['status'], 'HELD')
                self.receipt = saved
                self.save_receipt()
        self.token.assert_not_called()
        self.sender.assert_not_called()

    def test_tampered_hash_wrong_filename_and_symlink_receipts_are_not_eligible(self):
        self.receipt['site_get_readback'] = 'FAIL'
        self.receipt_path.write_bytes(post.compact(self.receipt))
        self.assertEqual(self.run_resume()['status'], 'HELD')
        self.receipt['site_get_readback'] = 'PASS'
        self.save_receipt()
        wrong = self.receipts / ('d' * 64 + '.json')
        self.receipt_path.rename(wrong)
        self.assertEqual(self.run_resume()['status'], 'HELD')
        self.receipt_path.symlink_to(wrong)
        self.assertEqual(self.run_resume()['status'], 'HELD')
        self.token.assert_not_called()

    def test_prior_or_future_day_is_not_posted(self):
        for value in ['2026-11-22T11:30:00+09:00', '2026-11-24T11:30:00+09:00',
                      '2026-11-23T02:30:00+00:00']:
            with self.subTest(value=value), redirect_stdout(io.StringIO()):
                result = post.resume(self.state, DAY, now=datetime.fromisoformat(value),
                    token_provider=self.token, sender=self.sender)
                self.assertEqual(result['status'], 'HELD')
        self.token.assert_not_called()
        self.sender.assert_not_called()

    def test_busy_and_held_do_not_claim_completion(self):
        for status in ['BUSY', 'HELD']:
            self.sender.return_value = {'date': DAY, 'status': status,
                'processed': None, 'pending': ['LIFECYCLE'],
                'retry_at_jst': DAY + 'T11:31:00+09:00'}
            result = self.run_resume()
            self.assertEqual(result['status'], status)
            self.assertFalse(result['postprocessing_complete'])

    def test_wrong_day_unknown_stage_or_false_complete_ack_is_held(self):
        for fields in [{'date': '2026-11-22'}, {'status': 'COMPLETE'},
                {'pending': ['UNEXPECTED']}, {'pending': ['LIFECYCLE', 'LIFECYCLE']},
                {'processed': {'stage': 'LIFECYCLE', 'status': 'fixture secret'}}]:
            with self.subTest(fields=fields):
                self.sender.return_value = {'date': DAY, 'status': 'PENDING',
                    'processed': None, 'pending': ['LIFECYCLE'], **fields}
                result = self.run_resume()
                self.assertEqual(result['status'], 'HELD')
                self.assertFalse(result['postprocessing_complete'])


class TransportTests(unittest.TestCase):
    def test_one_empty_post_no_redirect_no_retry_and_credential_only_on_stdin(self):
        calls = []
        def runner(command, **kwargs):
            calls.append((command, kwargs))
            self.assertNotIn('fixture-credential', str(command))
            self.assertEqual(command[-1], post.ORIGIN + '/api/g11-postprocess?date=' + DAY)
            self.assertNotIn('--location', command)
            self.assertNotIn('--retry', command)
            self.assertNotIn('--data-binary', command)
            self.assertEqual(command[command.index('--request') + 1], 'POST')
            self.assertEqual(command[command.index('--max-time') + 1], '30')
            self.assertIn('fixture-credential', kwargs['input'])
            self.assertLessEqual(kwargs['timeout'], 35)
            target = Path(command[command.index('--output') + 1])
            target.write_text(json.dumps({'date': DAY, 'status': 'COMPLETE',
                'processed': None, 'pending': []}))
            self.assertNotIn('fixture-credential', target.read_text())
            return subprocess.CompletedProcess([], 0, stdout='200')
        self.assertEqual(post.post_stage(DAY, 'fixture-credential', runner)['status'], 'COMPLETE')
        self.assertEqual(len(calls), 1)

    def test_http_error_curl_error_and_oversize_are_not_retried_or_completed(self):
        for outcome in ['http', 'curl', 'large']:
            calls = []
            def runner(command, **kwargs):
                calls.append(command)
                target = Path(command[command.index('--output') + 1])
                target.write_bytes(b'x' * (post.MAX_BODY + 1) if outcome == 'large' else b'{}')
                return subprocess.CompletedProcess([], 7 if outcome == 'curl' else 0,
                    stdout='503' if outcome == 'http' else '200')
            with self.subTest(outcome=outcome), self.assertRaises(ValueError):
                post.post_stage(DAY, 'fixture-credential', runner)
            self.assertEqual(len(calls), 1)

    def test_oidc_and_post_share_total_wall_budget_and_restore_alarm_handler(self):
        old = signal.getsignal(signal.SIGALRM)
        with patch.object(post, 'NETWORK_WALL_SECONDS', .02):
            with self.assertRaises(TimeoutError):
                with post.network_deadline():
                    time.sleep(.05)
        self.assertIs(signal.getsignal(signal.SIGALRM), old)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)

    def test_workflow_runs_after_both_final_publication_and_wild_followup(self):
        workflow = (Path(__file__).resolve().parents[1] /
                    '.github/workflows/g11-free-runner.yml').read_text()
        marker = 'Resume one saved Site postprocessing stage'
        self.assertEqual(workflow.count('scripts/postprocess_delivery_v1.py'), 1)
        index = workflow.index(marker)
        self.assertGreater(index, workflow.index('Handoff additional timely odds after verified state save'))
        self.assertGreater(index, workflow.index('Publish a newly verified feed through OIDC'))
        block = workflow[index:workflow.index('P3 research update after LIVE publication', index)]
        self.assertIn("steps.publication-state-verified.outputs.ready=='true'", block)
        self.assertIn("steps.due.outputs.phase=='predeadline'", block)
        self.assertIn('continue-on-error: true', block)


if __name__ == '__main__':
    unittest.main()
