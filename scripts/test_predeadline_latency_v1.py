"""Regression boundaries for faster publication without dropping saved work."""
import copy
import hashlib
from datetime import datetime, timedelta
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tarfile
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
                for compressor, workers in [('gzip', 1), ('pigz', 2), ('igzip', 1)]:
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


class SplitEncryptionTests(unittest.TestCase):
    def exercise(self, *, delta_only, base_key):
        secret=b'synthetic-memory-only\n'
        duplicate=os.dup
        with tempfile.TemporaryDirectory() as tmp, archive.secret_pipe(secret) as fd:
            root=Path(tmp)/'state';root.mkdir()
            output=Path(tmp)/'state.gpg';output.write_bytes(b'unchanged exact base ciphertext')
            args=['checkpoint','--state-root',str(root),'--output',str(output),'--delta-base-key',base_key]
            if delta_only:args.append('--delta-only')
            with patch.object(sys,'argv',args),patch.object(archive.os,'dup',side_effect=lambda _:duplicate(fd)), \
                    patch.object(archive,'prepare_compact_checkpoint') as prepare,patch.object(archive,'encrypt') as full:
                archive.main()
                prepare.assert_called_once_with(root,output,base_key,secret=secret)
                self.assertEqual(full.call_count,0 if delta_only else 1)
                self.assertEqual(output.read_bytes(),b'unchanged exact base ciphertext')

    def test_delta_preparation_never_overwrites_base_or_runs_full_encryption_even_without_a_base_key(self):
        for key in ('','g11-state-v4-published-'+DAY+'-41'):
            with self.subTest(key=key):self.exercise(delta_only=True,base_key=key)

    def test_existing_combined_writer_still_performs_full_encryption(self):
        self.exercise(delta_only=False,base_key='g11-state-v4-published-'+DAY+'-41')


class WorkflowTests(unittest.TestCase):
    def test_compact_dispatch_precedes_full_encryption_but_canonical_publication_still_requires_full_readback(self):
        source=(ROOT/'.github/actions/g11-publication-checkpoint/action.yml').read_text()
        names=['Start WILD4 from a verified full reconstruction before large backup transfer',
               'Encrypt the complete state before full backup and canonical publication',
               'Record encrypted publication checkpoint digest',
               'Save complete encrypted state before publication',
               'Read back complete encrypted publication checkpoint',
               'Verify encrypted publication checkpoint bytes']
        indexes=[source.index('    - name: '+name+'\n') for name in names]
        self.assertEqual(indexes,sorted(indexes))
        full=source.split('    - name: '+names[1]+'\n',1)[1].split('    - name:',1)[0]
        self.assertIn("if: inputs.encrypt-full == 'true'",full)
        self.assertIn('G11_PRIVATE_REPO_TOKEN: ${{ inputs.encryption-secret }}',full)
        self.assertIn('--passphrase', (ROOT/'scripts/encrypt_runtime_state_v1.py').read_text())
        self.assertIn('set -euo pipefail',full)
        self.assertNotIn('continue-on-error',full)
        self.assertIn('value: ${{ steps.verified.outputs.ready }}',source)
        workflow=(ROOT/'.github/workflows/g11-free-runner.yml').read_text()
        critical=workflow.split('      - name: Encrypt critical state before optional work\n',1)[1].split('      - name:',1)[0]
        self.assertIn('--delta-only',critical)
        save=workflow.split('      - name: Save early encrypted critical checkpoint\n',1)[1].split('      - name:',1)[0]
        self.assertIn("encrypt-full: 'true'",save)

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


class CompleteDeltaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.base = self.root/'base'; self.base.mkdir()
        self.cipher = self.root/'base.gpg'; self.cipher.write_bytes(b'authenticated original cipher')
        data = {'relay-state.json': b'old metadata', 'store/old-lock': b'immutable old LOCK',
                'store/odds': b'old captured odds', 'store/removed/file': b'old transient',
                'daily-runtime/'+DAY+'/authority.json': json.dumps({'immutable':True,'operational_date_jst':DAY,
                    'authority_sha256':'a'*64,'runtime_source_sha':'b'*40}).encode(),
                'models/static.bin': os.urandom(2*1024*1024), '.hidden': b'preserved'}
        for name, raw in data.items():
            path = self.base/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(raw)
        (self.base/'empty-history-directory').mkdir()
        self.target = self.root/'target'; shutil.copytree(self.base,self.target)
        (self.target/'store/odds').write_bytes(b'new original predeadline odds')
        (self.target/'store/removed/file').unlink(); (self.target/'store/removed').rmdir()
        (self.target/'new-empty-directory').mkdir()
        (self.target/'store/new-lock').write_bytes(b'new immutable LOCK')
        (self.target/'.hidden').chmod(0o600)
        self.key = 'g11-state-v4-critical-'+DAY+'-43'
        self.base_key = 'g11-state-v4-published-'+DAY+'-42-1-checkpoint'
        self.state = {'OPERATIONAL_DATE_JST':DAY,'MORNING_COMPLETE':True,'CRITICAL':{
            'SCHEMA':'G11_PREDEADLINE_CRITICAL_CHECKPOINT_V1','CRITICAL_CHECKPOINT_STATUS':'PASS',
            'OPERATIONAL_DATE':DAY,'LAST_CRITICAL_RUN_ID':'43','POST_DEADLINE_GENERATION':0,
            'LAST_TIMELY_SOURCE_COUNT':1,'AUTHORITY_SHA256':'a'*64,'RUNTIME_SOURCE_SHA':'b'*40}}
        (self.target/'relay-state.json').write_text(json.dumps(self.state))
        self.before = archive.snapshot_state(self.base)
        self.manifest = archive.delta_manifest(self.target,self.before,self.base_key,archive.file_sha(self.cipher))
        self.delta = self.root/'delta.tgz'; archive.write_delta(self.target,self.manifest,self.delta)

    def restored(self):
        path=self.root/'restored';shutil.copytree(self.base,path);return path

    def rewrite(self, mutate):
        with tarfile.open(self.delta,'r:gz') as source:
            items=[(m,source.extractfile(m).read()) for m in source.getmembers()]
        items=mutate(items)
        with tarfile.open(self.delta,'w:gz') as output:
            for member,raw in items:
                member.size=len(raw);output.addfile(member,io.BytesIO(raw))

    def test_exact_reconstruction_preserves_all_history_models_empty_directories_modes_and_sources(self):
        source_before=archive.snapshot_state(self.target);dest=self.restored()
        self.assertNotIn('models/static.bin',self.manifest['changed'])
        self.assertLess(self.delta.stat().st_size,100000)
        archive.apply_delta(self.delta,dest,day=DAY,checkpoint_key=self.key,base_cipher=self.cipher)
        self.assertEqual(archive.snapshot_state(dest),source_before)
        self.assertEqual(archive.snapshot_state(self.base),self.before)
        self.assertEqual(archive.snapshot_state(self.target),source_before)
        self.assertEqual(self.before['files']['store/old-lock'],archive.snapshot_state(dest)['files']['store/old-lock'])

    def test_wrong_day_run_base_cipher_and_modified_base_are_rejected_without_mutation(self):
        for kind in ('day','run','cipher','base'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as tmp:
                dest=Path(tmp)/'restored';shutil.copytree(self.base,dest)
                cipher=self.cipher
                if kind=='cipher':cipher=Path(tmp)/'other.gpg';cipher.write_bytes(b'wrong base')
                if kind=='base':(dest/'store/old-lock').write_bytes(b'modified')
                before=archive.snapshot_state(dest)
                with self.assertRaises(ValueError):
                    archive.apply_delta(self.delta,dest,day='2026-11-05' if kind=='day' else DAY,
                        checkpoint_key=self.key+'4' if kind=='run' else self.key,base_cipher=cipher)
                self.assertEqual(archive.snapshot_state(dest),before)

    def test_corrupt_replacement_rolls_back_all_changes(self):
        def corrupt(items):
            member,raw=items[-1];items[-1]=(member,b'x'*len(raw));return items
        self.rewrite(corrupt);dest=self.restored()
        with self.assertRaisesRegex(ValueError,'TARGET_INVENTORY'):
            archive.apply_delta(self.delta,dest,day=DAY,checkpoint_key=self.key,base_cipher=self.cipher)
        self.assertEqual(archive.snapshot_state(dest),self.before)
        self.assertFalse(list(dest.parent.glob('.g11-delta-verified-*')))

    def test_duplicate_extra_and_link_members_cannot_enter_reconstruction(self):
        original=self.delta.read_bytes()
        for kind in ('duplicate','traversal','link','missing'):
            self.delta.write_bytes(original)
            def mutate(items):
                if kind=='missing':return items[:-1]
                item=copy.copy(items[-1][0]);raw=items[-1][1]
                if kind=='traversal':item.name='../escape'
                if kind=='link':item.type=tarfile.SYMTYPE;item.linkname='/tmp/escape';items[-1]=(item,b'');return items
                return items+[(item,raw)]
            self.rewrite(mutate)
            with self.subTest(kind=kind),self.assertRaises(ValueError):
                archive.inspect_delta(self.delta,day=DAY,checkpoint_key=self.key)

    def test_frozen_authority_change_and_wrong_source_date_cannot_be_transported(self):
        path=self.target/'daily-runtime'/DAY/'authority.json';raw=path.read_bytes()
        path.write_bytes(raw+b' ')
        with self.assertRaisesRegex(ValueError,'BASE_AUTHORITY_CHANGED'):
            archive.delta_manifest(self.target,self.before,self.base_key,archive.file_sha(self.cipher))
        path.write_bytes(raw)
        with self.assertRaisesRegex(ValueError,'TARGET_BINDING'):
            archive.delta_manifest(self.target,self.before,self.base_key.replace(DAY,'2026-11-05'),archive.file_sha(self.cipher))

    def test_file_directory_replacements_preserve_the_exact_complete_tree(self):
        (self.target/'store/new-lock').unlink();(self.target/'store/new-lock').mkdir()
        (self.target/'store/new-lock/child').write_bytes(b'new')
        (self.target/'models/static.bin').unlink();(self.target/'models').rmdir();(self.target/'models').write_bytes(b'file replacing directory')
        manifest=archive.delta_manifest(self.target,self.before,self.base_key,archive.file_sha(self.cipher))
        archive.write_delta(self.target,manifest,self.delta);dest=self.restored()
        archive.apply_delta(self.delta,dest,day=DAY,checkpoint_key=self.key,base_cipher=self.cipher)
        self.assertEqual(archive.snapshot_state(dest),archive.snapshot_state(self.target))

    def test_failed_atomic_exposure_keeps_the_original_state(self):
        dest=self.restored();replace=archive.os.replace
        def fail(source,target):
            if Path(target)==dest:raise OSError('synthetic atomic move failure')
            return replace(source,target)
        with patch.object(archive.os,'replace',side_effect=fail),self.assertRaises(OSError):
            archive.apply_delta(self.delta,dest,day=DAY,checkpoint_key=self.key,base_cipher=self.cipher)
        self.assertEqual(archive.snapshot_state(dest),self.before)

    def test_real_authenticated_delta_and_full_checkpoint_share_no_partial_state_or_secret_file(self):
        import extract_runtime_state_v1 as restore
        home=self.root/'gnupg';home.mkdir(mode=0o700)
        probe=subprocess.run(['gpg','--homedir',str(home),'--batch','--yes','--pinentry-mode','loopback',
            '--passphrase-fd','0','--symmetric','--output',str(self.root/'probe.gpg'),str(self.base/'.hidden')],
            input=b'synthetic-test-only\n',capture_output=True)
        if probe.returncode:
            if os.environ.get('G11_REQUIRE_ENCRYPTION_TESTS')=='1':self.fail('Real GPG required in CI')
            self.skipTest('local gpg-agent unavailable; mandatory in contract CI')
        workspace=self.root/'work';workspace.mkdir();output=workspace/'.relay-cache/state.tgz.gpg';output.parent.mkdir()
        env={**os.environ,'GNUPGHOME':str(home),'GITHUB_RUN_ID':'43'}
        (self.base/'relay-state.json').write_text(json.dumps({'OPERATIONAL_DATE_JST':DAY,'MORNING_COMPLETE':True}))
        try:
            with patch.dict(os.environ,env),archive.secret_pipe(b'synthetic-test-only\n') as fd:
                archive.encrypt(self.base,output,passphrase_fd=fd,codec='gzip')
            original=workspace/'base-retained.gpg';shutil.copyfile(output,original)
            archive.save_base_inventory(self.base,workspace/'.relay-output/base-inventory.json')
            result=subprocess.run(['bash','-c','exec 3<<<"synthetic-test-only"; exec python3 "$1" --state-root "$2" --output "$3" --delta-base-key "$4" --delta-only',
                'checkpoint-test',str(ROOT/'scripts/encrypt_runtime_state_v1.py'),str(self.target),str(output),self.base_key],
                env=env,cwd=workspace,text=True,capture_output=True,timeout=60)
            self.assertEqual(result.returncode,0,result.stderr);self.assertIn('G11_COMPACT_CHECKPOINT=READY',result.stdout)
            # Dispatch can restore the complete authenticated target while the
            # exact base ciphertext is still retained, before full encryption.
            self.assertEqual(output.read_bytes(),original.read_bytes())
            def decrypt(source,target):
                subprocess.run(['gpg','--batch','--yes','--pinentry-mode','loopback','--passphrase-fd','0',
                    '--decrypt','--output',str(target),str(source)],input=b'synthetic-test-only\n',env=env,capture_output=True,check=True)
            delta=workspace/'delta.tgz';decrypt(workspace/'.relay-output/early-delta.tgz.gpg',delta)
            baseline=workspace/'base.tgz';decrypt(original,baseline)
            complete=workspace/'complete.tgz'
            old=workspace/'restored-base';restore.extract_complete_state(baseline,old)
            archive.apply_delta(delta,old,day=DAY,checkpoint_key=self.key,base_cipher=original)
            # Prewarming changes only when the exact same base is restored.
            # Retain its authenticated ciphertext across checkout cleanup, then
            # require the unchanged complete reconstruction and inventories.
            warm=workspace/'warm';warm.mkdir();warm_cipher=workspace/'warm.gpg';shutil.copyfile(original,warm_cipher)
            with patch.dict(os.environ,env):
                self.assertEqual(self.base_key,archive.prepare_warm_base(self.base_key,self.key,
                    secret=b'synthetic-test-only\n',folder=warm,cipher=warm_cipher))
            self.assertFalse(warm_cipher.exists());self.assertFalse((warm/'g11-prewarm-base.tgz').exists())
            self.assertEqual(archive.file_sha(original),archive.file_sha(warm/'g11-prewarm-cipher.tgz.gpg'))
            archive.apply_delta(delta,warm/'g11-prewarm-base',day=DAY,checkpoint_key=self.key,
                                base_cipher=warm/'g11-prewarm-cipher.tgz.gpg')
            self.assertEqual(archive.snapshot_state(old),archive.snapshot_state(warm/'g11-prewarm-base'))
            bad=workspace/'bad-warm';bad.mkdir();damaged=workspace/'damaged.gpg'
            damaged.write_bytes(original.read_bytes()[:-10])
            with patch.dict(os.environ,env),self.assertRaises(ValueError):
                archive.prepare_warm_base(self.base_key,self.key,secret=b'synthetic-test-only\n',folder=bad,cipher=damaged)
            self.assertFalse((bad/'g11-prewarm-base').exists());self.assertFalse((bad/'g11-prewarm-base.tgz').exists())
            result=subprocess.run(['bash','-c','exec 3<<<"synthetic-test-only"; exec python3 "$1" --state-root "$2" --output "$3"',
                'full-checkpoint-test',str(ROOT/'scripts/encrypt_runtime_state_v1.py'),str(self.target),str(output)],
                env=env,cwd=workspace,text=True,capture_output=True,timeout=60)
            self.assertEqual(result.returncode,0,result.stderr)
            decrypt(output,complete)
            full=workspace/'restored-full';restore.extract_complete_state(complete,full)
            self.assertEqual(archive.snapshot_state(old),archive.snapshot_state(full))
            self.assertEqual(archive.snapshot_state(full),archive.snapshot_state(self.target))
            self.assertFalse((workspace/'passphrase').exists())
        finally:
            subprocess.run(['gpgconf','--homedir',str(home),'--kill','gpg-agent'],capture_output=True)


class PrewarmTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.output=Path(self.tmp.name)/'outputs'
        self.base='g11-state-v4-published-'+DAY+'-42-1-checkpoint'
        self.key='g11-state-v4-critical-'+DAY+'-43'
        self.delta='g11-state-v4-delta-'+DAY+'-43'
        self.env={'SELECTED_CHECKPOINT':self.base,'RESTORED_CHECKPOINT':self.base,
            'GITHUB_RUN_ID':'43','GITHUB_RUN_ATTEMPT':'1','GITHUB_OUTPUT':str(self.output),
            'GITHUB_REPOSITORY':'tatsuuut/-g11-official-data-relay'}

    def test_current_exact_restored_base_starts_only_preparation_before_capture(self):
        with patch.dict(os.environ,self.env),patch.object(subprocess,'run',return_value=Mock(returncode=0)) as dispatch:
            critical.prewarm_worker(DAY,'predeadline',now=NOW)
        self.assertEqual(self.output.read_text(),'status=attempted\nstatus=sent\n')
        args=dispatch.call_args.args[0]
        for field in ('prewarm_base_key='+self.base,'predeadline_checkpoint_key='+self.key,'predeadline_delta_key='+self.delta):
            self.assertIn(field,args)
        self.assertEqual(dispatch.call_args.kwargs['timeout'],12)

    def test_wrong_restore_fails_closed_and_non_current_night_or_rerun_never_dispatches(self):
        with patch.dict(os.environ,{**self.env,'RESTORED_CHECKPOINT':self.base+'4'}), \
                patch.object(subprocess,'run') as dispatch,self.assertRaises(RuntimeError):
            critical.prewarm_worker(DAY,'predeadline',now=NOW)
        dispatch.assert_not_called()
        for phase,day,extra in [('night',DAY,{}),('predeadline','2026-11-05',{}),
                              ('predeadline',DAY,{'GITHUB_RUN_ATTEMPT':'2'}),
                              ('predeadline',DAY,{'GITHUB_RUN_ID':'42'})]:
            with self.subTest(phase=phase,day=day,extra=extra),patch.dict(os.environ,{**self.env,**extra}), \
                    patch.object(subprocess,'run') as dispatch:
                critical.prewarm_worker(day,phase,now=NOW)
                dispatch.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_ambiguous_dispatch_is_retained_without_claiming_acceptance(self):
        for result in (Mock(returncode=1),subprocess.TimeoutExpired('gh',12)):
            self.output.unlink(missing_ok=True)
            with patch.dict(os.environ,self.env),patch.object(subprocess,'run',
                    side_effect=result if isinstance(result,Exception) else None,return_value=result):
                critical.prewarm_worker(DAY,'predeadline',now=NOW)
            self.assertEqual(self.output.read_text(),'status=attempted\n')

    def test_saved_metadata_wait_requires_exact_day_run_and_ref_and_is_bounded(self):
        for hit in ('exact','prefix','wrong-ref','missing'):
            elapsed=[0];queries=[]
            def query(path):
                queries.append(path)
                if '/actions/runs/' in path:return {'id':43,'status':'in_progress'}
                return {'actions_caches':[] if hit=='missing' else [{'key':self.delta+('4' if hit=='prefix' else ''),
                    'ref':'refs/heads/elsewhere' if hit=='wrong-ref' else 'refs/heads/main'}]}
            def sleep(seconds):elapsed[0]+=seconds
            with self.subTest(hit=hit),patch.dict(os.environ,self.env):
                available=archive.wait_for_compact(self.base,self.key,self.delta,maximum=25,
                    clock=lambda:elapsed[0],sleeper=sleep,query=query)
            self.assertEqual(available,hit=='exact')
            self.assertEqual(elapsed[0],0 if hit=='exact' else 25)
            self.assertTrue(all('/actions/caches?' in p or '/actions/runs/43' in p for p in queries))
        with patch.dict(os.environ,self.env):
            query=Mock(side_effect=[{'actions_caches':[]},{'id':43,'status':'completed'}])
            sleeper=Mock()
            self.assertFalse(archive.wait_for_compact(self.base,self.key,self.delta,query=query,sleeper=sleeper))
            sleeper.assert_not_called()
            for base,key,delta in [(self.base,self.key+'4',self.delta),(self.base,self.key,self.delta+'4'),
                    (self.base.replace(DAY,'2026-11-05'),self.key,self.delta),
                    (self.base.replace('-42-', '-44-'),self.key,self.delta)]:
                with self.assertRaises(ValueError):archive.wait_for_compact(base,key,delta,query=query)


class CompressedSavedReadbackTests(unittest.TestCase):
    def test_real_compressed_http_retains_exact_decoded_bytes_and_hash_gate(self):
        import gzip
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        import threading
        import hot_delta_transport_v1 as hot
        raw=json.dumps({'captured_at':'2026-11-17T19:48:12+09:00','bets':['1-2-3'],
                        'saved_history':'同じ保存済みデータ'*5000},ensure_ascii=False).encode()
        compressed=gzip.compress(raw);calls=[]
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append(self.headers.get('Accept-Encoding',''))
                body=compressed[:-8] if self.path=='/truncated' else compressed
                self.send_response(200)
                self.send_header('Content-Type','application/json');self.send_header('Content-Encoding','gzip')
                self.send_header('Content-Length',str(len(compressed)))
                self.send_header('X-G11-Feed-Sha256','0'*64 if self.path=='/wrong-hash' else hashlib.sha256(raw).hexdigest())
                self.end_headers();self.wfile.write(body)
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cwd=Path.cwd();os.chdir(tmp);Path('.relay-output').mkdir()
                try:
                    url='http://127.0.0.1:'+str(server.server_port)
                    feed,headers,body=hot.fetch(url+'/saved')
                    self.assertEqual(body,raw);self.assertEqual(feed,json.loads(raw))
                    self.assertEqual(headers['x-g11-feed-sha256'],hashlib.sha256(raw).hexdigest())
                    self.assertIn('gzip',calls[0]);self.assertLess(len(compressed),len(raw)//10)
                    with self.assertRaisesRegex(RuntimeError,'HOT_READBACK_SHA'):hot.fetch(url+'/wrong-hash')
                    with self.assertRaises(subprocess.CalledProcessError):hot.fetch(url+'/truncated')
                finally:os.chdir(cwd)
        finally:server.shutdown();server.server_close();thread.join(timeout=5)


if __name__ == '__main__': unittest.main()

