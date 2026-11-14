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

    def test_rescue_publication_elapsed_time_uses_the_same_spacing(self):
        result = {'STATUS': 'SAME_DAY_RESCUE_COMPLETE', 'PHASE': 'PREDEADLINE_RESCUE',
            'OPERATIONAL_DATE_JST': DAY, 'FEED_EMITTED': True, 'MORNING_IMMUTABLE': True}
        feed = {'operational_date_jst': DAY, 'stage': 'MORNING', 'status': 'PASS',
            'SNAPSHOT_CLASS': 'SAME_DAY_RESCUE_BOUNDARY', 'source': {'run_id': 42}}
        for elapsed, expected in [(0, 45), (30, 15), (45, 0), (90, 0)]:
            feed['generated_at_jst'] = (NOW-timedelta(seconds=elapsed)).isoformat()
            self.assertEqual(expected, critical.rescue_continuation_delay(result, feed, DAY, '42', NOW))
        for target, field, value in [('feed', 'generated_at_jst', (NOW+timedelta(seconds=1)).isoformat()),
                ('feed', 'source', {'run_id': 41}), ('feed', 'stage', 'NIGHT'),
                ('result', 'MORNING_IMMUTABLE', False), ('result', 'PHASE', 'SAME_DAY_RESCUE'),
                ('result', 'OPERATIONAL_DATE_JST', '2026-11-05')]:
            bad_result, bad_feed = copy.deepcopy(result), copy.deepcopy(feed)
            (bad_feed if target == 'feed' else bad_result)[field] = value
            with self.subTest(target=target, field=field), self.assertRaises(RuntimeError):
                critical.rescue_continuation_delay(bad_result, bad_feed, DAY, '42', NOW)


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

    def test_verified_rescue_uses_existing_sender_after_delivery_and_keeps_locks(self):
        from test_rescue_transport_contract_v1 import scenario
        import daily_runtime_delivery_v1 as delivery
        with scenario('2026-10-22') as (state, feed, site, saved), patch.dict(os.environ, self.env):
            delivery.deliver(state, feed, delivery.ORIGIN)
            before = feed.read_bytes()
            sender = Mock(return_value={'status': 'PASS'})
            result = research.publish_persisted_live(state, feed, opener=self.opener, publisher=sender)
            self.assertEqual('PASS', result['status'])
            self.assertEqual(before, feed.read_bytes())
            sender.assert_called_once_with(state, feed, delivery.ORIGIN)
            for path, raw in saved.items():
                self.assertEqual(raw, path.read_bytes())

    def test_rescue_wrong_receipt_or_cohort_never_requests_credentials(self):
        from test_rescue_transport_contract_v1 import scenario
        import daily_runtime_delivery_v1 as delivery
        for change in ['authority', 'failed', 'day', 'formal']:
            with self.subTest(change=change), scenario('2026-10-22') as (state, feed, site, saved), \
                    patch.dict(os.environ, self.env):
                delivery.deliver(state, feed, delivery.ORIGIN)
                receipt_path = state/'rescue/predeadline-delivery.json'
                receipt = json.loads(receipt_path.read_bytes())
                if change == 'authority': receipt['authority_sha256'] = '0'*64
                if change == 'failed': receipt['site_get_readback'] = 'FAIL'
                if change == 'day': receipt['operational_date_jst'] = '2026-10-21'
                receipt_path.write_text(json.dumps(receipt))
                if change == 'formal':
                    value = json.loads(feed.read_bytes()); value['races'][0]['research_eligible'] = True
                    feed.write_text(json.dumps(value))
                opener = Mock()
                with self.assertRaises(ValueError):
                    research.publish_persisted_live(state, feed, opener=opener)
                opener.assert_not_called()


class ArchiveTests(unittest.TestCase):
    def test_accelerated_writer_bounds_workers_and_preserves_legacy_default(self):
        for cpus, expected in [(1,1), (2,2), (4,4), (16,4)]:
            with patch.object(archive.os, 'sched_getaffinity', return_value=set(range(cpus))), \
                    patch.object(archive.shutil, 'which', return_value='/usr/bin/pigz'):
                self.assertEqual(f'pigz -1 -p {expected}', archive.compression_command(accelerated=True, codec='pigz'))
                self.assertEqual('pigz -1 -p 2', archive.compression_command(codec='pigz'))
        with patch.object(archive.os, 'sched_getaffinity', side_effect=OSError), \
                patch.object(archive.os, 'cpu_count', return_value=2), \
                patch.object(archive.shutil, 'which', return_value='/usr/bin/pigz'):
            self.assertEqual('pigz -1 -p 2', archive.compression_command(accelerated=True, codec='pigz'))

    def test_supported_authenticated_mode_and_legacy_capability_fallback(self):
        for available, expected in [('--use-ocb-sym\n--force-aead', '--use-ocb-sym'),
                ('--force-ocb\n--force-aead', '--force-ocb'), ('--force-aead', '--force-aead')]:
            with self.subTest(available=available), patch.object(archive.subprocess, 'run',
                    return_value=Mock(returncode=0, stdout=available)):
                self.assertEqual([expected], archive.authenticated_encryption_options())
        with patch.object(archive.subprocess, 'run', return_value=Mock(returncode=0, stdout='--symmetric')):
            self.assertEqual([], archive.authenticated_encryption_options())
            with self.assertRaises(ValueError): archive.authenticated_encryption_options('ocb')
        with patch.object(archive.subprocess, 'run') as child:
            self.assertEqual([], archive.authenticated_encryption_options('legacy'))
            with self.assertRaises(ValueError): archive.authenticated_encryption_options('--disable-mdc')
            child.assert_not_called()
        with patch.object(archive.subprocess, 'run', return_value=Mock(returncode=1, stdout='--force-aead')):
            self.assertEqual([], archive.authenticated_encryption_options())

    def test_standard_gzip_fallback_and_invalid_compression_never_run_shell(self):
        with patch.object(archive.shutil, 'which', return_value=None):
            self.assertEqual('gzip -1', archive.compression_command())
            with self.assertRaises(ValueError): archive.compression_command(codec='pigz')
        for workers in (0, 5, True, '4', -1):
            with self.subTest(workers=workers), self.assertRaises(ValueError):
                archive.compression_command(workers)
        with self.assertRaises(ValueError): archive.compression_command(codec='gzip;false')

    def test_failed_archive_keeps_prior_checkpoint_and_removes_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); state = root/'state'; state.mkdir(); output = root/'state.gpg'
            output.write_bytes(b'prior checkpoint')
            tar = Mock(stdout=Mock()); tar.wait.return_value = 1; tar.poll.return_value = 1
            def fail(*a, **kw):
                output.with_name('state.gpg.tmp').write_bytes(b'incomplete')
                return Mock(returncode=2)
            with patch.object(archive.os, 'fstat'), patch.object(archive, 'authenticated_encryption_options', return_value=[]), \
                    patch.object(archive.subprocess, 'Popen', return_value=tar), \
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
                for compressor, workers in [('gzip', 1), ('pigz', 2), ('pigz', 4)]:
                    if not shutil.which(compressor): continue
                    output = root/(compressor+str(workers)+'.gpg')
                    with tempfile.TemporaryFile() as secret:
                        secret.write(b'synthetic-test-only\n'); secret.seek(0)
                        with patch.dict(os.environ, {'GNUPGHOME': str(home)}), \
                                patch.object(archive.shutil, 'which', return_value=None if compressor=='gzip' else '/usr/bin/pigz'):
                            archive.encrypt(state, output, passphrase_fd=secret.fileno(), workers=workers, codec=compressor)
                    plaintext = subprocess.run(['gpg', '--homedir', str(home), '--batch', '--yes', '--pinentry-mode', 'loopback',
                        '--passphrase-fd', '0', '--decrypt', str(output)], input=b'synthetic-test-only\n', capture_output=True, check=True).stdout
                    restored = root/('restored-'+compressor+str(workers)); restored.mkdir()
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
                     'Persist delivery receipts', 'Keep the predeadline relay alive between delayed cron starts', 'Rescue']:
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
                 'Persist delivery receipts', 'Save deferred LIVE research state', 'Publish persisted LIVE research after predictions']
        positions = [text.index('      - name: '+name+'\n') for name in order]
        self.assertEqual(sorted(positions), positions)
        self.assertIn("steps.deme-live.outcome == 'success'", step('Persist delivery receipts'))
        self.assertIn('uses: ./.github/actions/g11-publication-checkpoint', step(order[-2]))
        self.assertIn("steps.deme-live.outcome=='success'", step(order[-2]))
        self.assertIn('uses: actions/cache/save@', step('Save state'))
        self.assertIn("steps.deme-live.outcome!='success'", step('Save state'))
        self.assertIn("steps.delivery-checkpoint.outputs.ready=='true'", step(order[-1]))
        self.assertEqual(3, text.count('python3 scripts/encrypt_runtime_state_v1.py'))

    def test_normal_and_rescue_live_defer_research_and_failed_publish_keeps_it(self):
        import itertools
        import re
        text = (ROOT/'.github/workflows/g11-free-runner.yml').read_text()
        def condition(name, values):
            step = text.split('      - name: '+name+'\n', 1)[1].split('      - name:', 1)[0]
            expression = ' '.join(re.split(r'\n {8}(?=\S)', step.split('        if: >-\n', 1)[1], 1)[0].split())
            expression = re.sub(r'steps\.[\w.-]+', lambda m: repr(values[m[0]]), expression)
            expression = expression.replace('always()', 'True').replace('cancelled()', 'False')
            expression = expression.replace('&&', ' and ').replace('||', ' or ')
            expression = re.sub(r'!(?!=)', ' not ', expression)
            return eval(expression, {'__builtins__': {}}, {})
        for phase, frozen, rescue, bootstrap, coalesced in itertools.product(
                ['morning', 'predeadline', 'night'], ['true', 'false'], ['true', ''], ['true', ''],
                ['success','failure','skipped']):
            values = {'steps.deme-source.outcome': 'success', 'steps.due.outputs.phase': phase,
                'steps.authority.outputs.frozen': frozen, 'steps.critical.outputs.rescue': rescue,
                'steps.authority.outputs.rescue_bootstrap': bootstrap,
                'steps.fast-deme-live.outcome': coalesced}
            deferred = phase == 'predeadline' and (frozen == 'true' or rescue == 'true' or bootstrap == 'true')
            with self.subTest(**values):
                self.assertEqual(not deferred, condition('P3 research update', values))
                self.assertEqual(deferred and coalesced != 'success', condition('P3 research update after LIVE publication', values))

    def test_rescue_continuation_uses_verified_elapsed_time_and_safe_dispatch(self):
        text = (ROOT/'.github/workflows/g11-free-runner.yml').read_text()
        step = text.split('      - name: Rescue\n', 1)[1].split('      - name:', 1)[0]
        self.assertIn('--rescue-continuation-delay', step)
        self.assertLess(step.index('dynamic_last_race_heartbeat.py'), step.index('--rescue-continuation-delay'))
        self.assertEqual(2, step.count('dynamic_last_race_heartbeat.py'))
        self.assertIn('predeadline_dispatch_v1.py --day "$d" --mode continuation', step)
        self.assertNotIn('gh workflow run g11-free-runner.yml', step)


if __name__ == '__main__': unittest.main()

