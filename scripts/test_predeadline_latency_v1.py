"""Regression boundaries for faster publication without dropping saved work."""
import copy
from datetime import datetime, timedelta
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import critical_saved_publication_v1 as critical
import deme_research_delivery_v1 as research
import encrypt_runtime_state_v1 as archive

ROOT = Path(__file__).resolve().parents[1]
DAY = '2026-11-06'
NOW = datetime.fromisoformat(DAY + 'T12:00:00+09:00')


class ContinuationTests(unittest.TestCase):
    def result(self, elapsed):
        return {'CRITICAL': {'OPERATIONAL_DATE': DAY, 'CRITICAL_CHECKPOINT_STATUS': 'PASS',
            'LAST_CRITICAL_RUN_ID': '42', 'CRITICAL_CAPTURE_AT': (NOW-timedelta(seconds=elapsed)).isoformat()}}

    def test_persistence_time_counts_toward_spacing(self):
        for elapsed, expected in [(0, 45), (15, 30), (44.5, .5), (45, 0), (73, 0), (300, 0)]:
            self.assertEqual(expected, critical.continuation_delay(self.result(elapsed), DAY, '42', NOW))

    def test_future_clock_and_other_run_or_day_cannot_skip_wait(self):
        for result in [self.result(-1), {}, {'CRITICAL': {**self.result(60)['CRITICAL'], 'LAST_CRITICAL_RUN_ID': '41'}},
                       {'CRITICAL': {**self.result(60)['CRITICAL'], 'OPERATIONAL_DATE': '2026-11-05'}},
                       {'CRITICAL': {**self.result(60)['CRITICAL'], 'CRITICAL_CHECKPOINT_STATUS': 'FAIL'}}]:
            with self.assertRaises(RuntimeError):
                critical.continuation_delay(result, DAY, '42', NOW)

    def test_timing_records_missed_target_without_calling_it_device_delivery(self):
        feed = {'operational_date_jst': DAY, 'races': [
            {'key': 'early', 'deadline_jst': (NOW+timedelta(minutes=5)).isoformat()},
            {'key': 'late', 'deadline_jst': (NOW-timedelta(seconds=1)).isoformat()},
            {'key': 'old', 'deadline_jst': (NOW+timedelta(minutes=6)).isoformat()}]}
        before = copy.deepcopy(feed)
        result = critical.publication_timing(feed, {'PREDICTION_LOCKED_BEFORE_DEADLINE': ['early'],
            'V53_LOCKED_BEFORE_DEADLINE': ['early', 'late']}, NOW)
        self.assertEqual(before, feed)
        self.assertEqual([True, False], [r['five_minute_target_met'] for r in result['races']])
        self.assertFalse(result['races'][1]['before_deadline'])
        self.assertEqual('SERVER_READBACK_NOT_DEVICE_ARRIVAL', result['scope'])


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.feed = self.root/'feed.json'
        self.payload = {'stage': 'PREDEADLINE', 'status': 'PASS'}
        self.feed.write_text(json.dumps(self.payload))
        self.env = {'RESEARCH_CHECKPOINT_VERIFIED': 'true',
            'G11_APP_ORIGIN': 'https://g11-race-lab.higatatsunori2.chatgpt.site',
            'ACTIONS_ID_TOKEN_REQUEST_URL': 'https://oidc.invalid/?test=1',
            'ACTIONS_ID_TOKEN_REQUEST_TOKEN': 'synthetic', 'G11_OIDC_AUDIENCE': 'synthetic'}
        self.opener = Mock(side_effect=lambda *a, **kw: io.BytesIO(b'{"value":"synthetic-oidc"}'))

    def test_verified_live_reuses_existing_research_sender_and_preserves_feed(self):
        before = self.feed.read_bytes()
        def publish(*args):
            self.assertEqual('synthetic-oidc', os.environ['G11_SYNC_OIDC'])
            return {'status': 'PASS'}
        with patch.dict(os.environ, {**self.env, 'G11_SYNC_OIDC': 'prior'}):
            result = research.publish_persisted_live(self.root, self.feed, opener=self.opener, publisher=publish)
            self.assertEqual('prior', os.environ['G11_SYNC_OIDC'])
        self.assertEqual('PASS', result['status']); self.assertEqual(before, self.feed.read_bytes())

    def test_unverified_night_rescue_or_wrong_origin_never_requests_token(self):
        for environment, payload in [({'RESEARCH_CHECKPOINT_VERIFIED': 'false'}, self.payload),
                ({'G11_APP_ORIGIN': 'https://elsewhere.invalid'}, self.payload),
                ({}, {'stage': 'NIGHT', 'status': 'PASS'}),
                ({}, {**self.payload, 'same_day_rescue': {'anything': True}}),
                ({}, {**self.payload, 'SNAPSHOT_CLASS': 'RESCUE'})]:
            self.feed.write_text(json.dumps(payload))
            with patch.dict(os.environ, {**self.env, **environment}):
                with self.assertRaises(ValueError):
                    research.publish_persisted_live(self.root, self.feed, opener=self.opener)
        self.opener.assert_not_called()

    def test_sender_failure_restores_token(self):
        with patch.dict(os.environ, {**self.env, 'G11_SYNC_OIDC': 'prior'}):
            with self.assertRaises(RuntimeError):
                research.publish_persisted_live(self.root, self.feed, opener=self.opener,
                    publisher=Mock(side_effect=RuntimeError('synthetic')))
            self.assertEqual('prior', os.environ['G11_SYNC_OIDC'])


class ArchiveTests(unittest.TestCase):
    def test_failed_archive_keeps_prior_checkpoint_and_removes_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); state = root/'state'; state.mkdir(); output = root/'state.gpg'
            output.write_bytes(b'prior checkpoint')
            tar = Mock(stdout=Mock()); tar.wait.return_value = 1; tar.poll.return_value = 1
            def fail(*a, **kw):
                output.with_name('state.gpg.tmp').write_bytes(b'incomplete')
                return Mock(returncode=2)
            with patch.object(archive.os, 'fstat'), patch.object(archive.subprocess, 'Popen', return_value=tar), \
                    patch.object(archive.subprocess, 'run', side_effect=fail):
                with self.assertRaisesRegex(RuntimeError, 'ENCRYPTION_FAILED'):
                    archive.encrypt(state, output)
            self.assertEqual(b'prior checkpoint', output.read_bytes())
            self.assertFalse(output.with_name('state.gpg.tmp').exists())

    def test_output_inside_source_is_rejected_before_processes(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(archive.subprocess, 'Popen') as child:
            with self.assertRaises(ValueError): archive.encrypt(tmp, Path(tmp)/'recursive.gpg')
            child.assert_not_called()

    def test_real_aes_gzip_roundtrip_keeps_every_file_and_original_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); home = root/'gnupg'; home.mkdir(mode=0o700)
            state = root/'state'; state.mkdir()
            files = {'.hidden': b'preserved', 'daily-runtime/authority.json': b'{"fixed":true}',
                'store/snapshots/predeadline/lock.json': b'{"bets":["1-2-3"]}',
                'store/model.gz': os.urandom(1024*1024)}
            for name, value in files.items():
                path = state/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(value)
            probe = subprocess.run(['gpg', '--homedir', str(home), '--batch', '--yes', '--pinentry-mode', 'loopback',
                '--passphrase-fd', '0', '--symmetric', '--output', str(root/'probe.gpg'), str(state/'.hidden')],
                input=b'synthetic-test-only\n', capture_output=True)
            if probe.returncode:
                if os.environ.get('G11_REQUIRE_ENCRYPTION_TESTS') == '1':
                    self.fail('Real GPG roundtrip required: ' + probe.stderr.decode(errors='replace'))
                self.skipTest('local gpg-agent unavailable; mandatory in contract CI')
            try:
                for compressor in ['gzip', 'pigz']:
                    if not shutil.which(compressor): continue
                    output = root/(compressor+'.gpg')
                    with tempfile.TemporaryFile() as secret:
                        secret.write(b'synthetic-test-only\n'); secret.seek(0)
                        with patch.dict(os.environ, {'GNUPGHOME': str(home)}), \
                                patch.object(archive.shutil, 'which', return_value=None if compressor=='gzip' else '/usr/bin/pigz'):
                            archive.encrypt(state, output, passphrase_fd=secret.fileno())
                    plaintext = subprocess.run(['gpg', '--homedir', str(home), '--batch', '--yes', '--pinentry-mode', 'loopback',
                        '--passphrase-fd', '0', '--decrypt', str(output)], input=b'synthetic-test-only\n', capture_output=True, check=True).stdout
                    restored = root/('restored-'+compressor); restored.mkdir()
                    subprocess.run(['tar', '-xzf', '-', '-C', str(restored)], input=plaintext, check=True)
                    self.assertEqual(files, {p.relative_to(restored).as_posix(): p.read_bytes() for p in restored.rglob('*') if p.is_file()})
                    self.assertEqual(files, {p.relative_to(state).as_posix(): p.read_bytes() for p in state.rglob('*') if p.is_file()})
            finally:
                subprocess.run(['gpgconf', '--homedir', str(home), '--kill', 'gpg-agent'], capture_output=True)


class WorkflowTests(unittest.TestCase):
    def test_changed_archive_shell_blocks_parse(self):
        import textwrap
        text = (ROOT/'.github/workflows/g11-free-runner.yml').read_text()
        for name in ['Encrypt critical state before optional work', 'Encrypt changed state before persistence',
                     'Persist delivery receipts', 'Keep the predeadline relay alive between delayed cron starts']:
            step = text.split('      - name: '+name+'\n', 1)[1].split('      - name:', 1)[0]
            script = textwrap.dedent(step.split('        run: |\n', 1)[1])
            result = subprocess.run(['bash', '-n'], input=script, text=True, capture_output=True)
            self.assertEqual(0, result.returncode, name + ': ' + result.stderr)

    def test_live_research_cannot_block_prediction_publication_or_skip_persistence(self):
        text = (ROOT/'.github/workflows/g11-free-runner.yml').read_text()
        step = lambda name: text.split('      - name: '+name+'\n', 1)[1].split('      - name:', 1)[0]
        self.assertIn("steps.due.outputs.phase!='predeadline'", step('P3 research update'))
        self.assertIn("steps.due.outputs.phase=='predeadline'", step('P3 research update after LIVE publication'))
        order = ['Publish a newly verified feed through OIDC', 'P3 research update after LIVE publication',
                 'Persist delivery receipts', 'Save state', 'Publish persisted LIVE research after predictions']
        positions = [text.index('      - name: '+name+'\n') for name in order]
        self.assertEqual(sorted(positions), positions)
        self.assertIn("steps.deme-live.outcome == 'success'", step('Persist delivery receipts'))
        self.assertIn('uses: ./.github/actions/g11-publication-checkpoint', step('Save state'))
        self.assertIn("steps.delivery-checkpoint.outputs.ready=='true'", step(order[-1]))
        self.assertEqual(3, text.count('python3 scripts/encrypt_runtime_state_v1.py'))


if __name__ == '__main__': unittest.main()
