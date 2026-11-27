"""Warm publication ordering and receipt guards without external requests."""
import copy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import warm_publication_orchestrator_v1 as warm

DAY = '2026-11-27'
RUN = '43'
BASE = 'g11-state-v4-published-' + DAY + '-42-1-checkpoint'
KEY = 'g11-state-v4-published-' + DAY + '-43-1-checkpoint'


class WarmPublicationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        self.environ = patch.dict(os.environ, {'GITHUB_RUN_ID': RUN, 'GITHUB_RUN_ATTEMPT':'1',
            'GITHUB_OUTPUT':str(self.root/'output'), 'G11_PRIVATE_REPO_TOKEN':'synthetic'})
        self.environ.start(); self.addCleanup(self.environ.stop)
        self.out = self.root/'.relay-output'; self.out.mkdir()
        self.cipher = self.root/'.relay-cache/state.tgz.gpg'
        self.cipher.parent.mkdir(); self.cipher.write_bytes(b'unchanged physical full ciphertext')
        self.gate = {'SCHEMA':'G11_PREDEADLINE_CRITICAL_CHECKPOINT_V1', 'CRITICAL_CHECKPOINT_STATUS':'PASS',
            'LAST_CRITICAL_RUN_ID':RUN, 'OPERATIONAL_DATE':DAY, 'POST_DEADLINE_GENERATION':0,
            'AUTHORITY_SHA256':'a'*64, 'RUNTIME_SOURCE_SHA':'b'*40, 'LAST_TIMELY_SOURCE_COUNT':2}
        self.first = {'OPERATIONAL_DATE_JST':DAY, 'CRITICAL':self.gate,
            'ACQUISITION':{'ACQUIRED':['beforeinfo-first','odds3t-first'], 'WARM_REMAINING':6}}
        owner = {'immutable':True, 'operational_date_jst':DAY,
            'authority_sha256':'a'*64, 'runtime_source_sha':'b'*40}
        self.write('.relay-state/daily-runtime/'+DAY+'/authority.json', owner)
        self.write('.relay-state/relay-state.json', {'MORNING_COMPLETE':True,'CRITICAL':self.gate})
        self.write('.relay-output/result.json', self.first)
        public = {'operational_date_jst':DAY, 'stage':'PREDEADLINE', 'status':'PASS',
            'source':{'run_id':43,'github_sha':'b'*40}, 'daily_runtime':dict(owner,
                schema='G11_DAILY_RUNTIME_AUTHORITY_V1', phase='PREDEADLINE', runtime_source_sha='b'*40)}
        self.write('.relay-output/hot-delta-readback.json', public)
        (self.out/'hot-delta-accepted').write_text('c'*64+'\n')
        receipt = {'schema':'G11_DAILY_SITE_DELIVERY_RECEIPT_V1', 'operational_date_jst':DAY,
            'phase':'PREDEADLINE', 'authority_sha256':'a'*64, 'runtime_source_sha':'b'*40,
            'site_payload_sha256':'c'*64, 'site_get_sha256':warm.sha(self.out/'hot-delta-readback.json'),
            'http_success':True, 'site_get_readback':'PASS'}
        self.receipt = '.relay-state/daily-runtime/'+DAY+'/delivery/PREDEADLINE/'+'c'*64+'.json'
        self.write(self.receipt, receipt)
        self.ready = {'day':DAY,'run_id':RUN,'base_key':BASE, 'cipher_sha256':'d'*64}
        self.write('.relay-output/compact-publication-proof.json', {
            'schema':'G11_COMPLETE_COMPACT_PUBLICATION_PROOF_V1', 'day':DAY,'run_id':RUN,'base_key':BASE,
            'authority_sha256':'a'*64, 'runtime_source_sha':'b'*40,
            'delta_cipher_sha256':'d'*64, 'base_cipher_sha256':warm.checkpoint.file_sha(self.cipher)})

    def write(self, name, value):
        path = self.root/name; path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(value)); return path

    def start(self, response=None):
        with patch.object(warm.checkpoint,'compact_ready',return_value=self.ready), \
                patch.object(warm,'datetime', wraps=datetime) as clock, \
                patch.object(warm.subprocess,'run', return_value=response or subprocess.CompletedProcess([],0)) as call:
            clock.now.return_value = datetime.fromisoformat(DAY+'T17:00:00+09:00')
            value = warm.start(DAY,BASE)
        return value, call

    def captured(self, acquired=None):
        self.write('.relay-output/first-critical-result.json', self.first)
        result = copy.deepcopy(self.first)
        result['ACQUISITION'] = {'ACQUIRED': acquired if acquired is not None else ['beforeinfo-warm','odds3t-warm']}
        self.write('.relay-output/result.json',result)
        return result

    def test_prewarm_requires_actual_first_readback_and_preserves_parent_identity(self):
        _, call = self.start()
        arguments = call.call_args.args[0]
        self.assertIn('capture_stage=warm',arguments)
        self.assertIn('predeadline_checkpoint_key=g11-state-v4-critical-'+DAY+'-'+RUN,arguments)
        self.assertIn('predeadline_delta_key=g11-state-v4-delta-'+DAY+'-'+RUN+'-warm',arguments)
        self.assertIn('prewarm_base_key='+BASE,arguments)
        self.assertEqual(call.call_args.kwargs['timeout'],12)
        self.assertTrue((self.out/'warm-publication-start.json').exists())

    def test_wrong_first_receipt_run_authority_or_http_never_dispatches(self):
        original = warm.read(self.out/'hot-delta-readback.json')
        for field in ('run','authority','http'):
            with self.subTest(field=field):
                public = copy.deepcopy(original)
                receipt = warm.read(self.root/self.receipt)
                if field=='run': public['source']['run_id']=42
                if field=='authority': public['daily_runtime']['authority_sha256']='e'*64
                if field=='http': receipt['http_success']=False
                self.write('.relay-output/hot-delta-readback.json',public)
                receipt['site_get_sha256']=warm.sha(self.out/'hot-delta-readback.json')
                self.write(self.receipt,receipt)
                with self.assertRaises(ValueError), patch.object(warm.subprocess,'run') as call:
                    self.start()
                call.assert_not_called()
                self.assertFalse((self.out/'warm-publication-start.json').exists())
                self.write('.relay-output/hot-delta-readback.json',original)
                receipt['http_success']=True
                receipt['site_get_sha256']=warm.sha(self.out/'hot-delta-readback.json')
                self.write(self.receipt,receipt)

    def test_first_full_fallback_changed_cipher_cannot_enter_warm_fast_path(self):
        self.cipher.write_bytes(b'newly encrypted first full instead of physical base')
        with self.assertRaisesRegex(ValueError,'PHYSICAL_BASE'):
            self.start()

    def test_ambiguous_prewarm_is_not_reported_as_accepted(self):
        with patch.object(warm.checkpoint,'compact_ready',return_value=self.ready), \
                patch.object(warm,'datetime', wraps=datetime) as clock, \
                patch.object(warm.subprocess,'run',side_effect=subprocess.TimeoutExpired('gh',12)):
            clock.now.return_value=datetime.fromisoformat(DAY+'T17:00:00+09:00')
            warm.start(DAY,BASE)
        output=(self.root/'output').read_text()
        self.assertIn('status=attempted',output)
        self.assertNotIn('status=sent',output)

    def test_prepare_keeps_first_cipher_uses_warm_stage_and_discards_first_prepared_feed(self):
        self.start(); self.captured()
        for name in ('critical-prepared-feed.json','critical-prepared-proof.json'):
            (self.out/name).write_text('first projection must not be reused')
        before=self.cipher.read_bytes()
        with patch.object(warm.checkpoint,'prepare_compact_checkpoint') as prepare, \
                patch.object(warm.checkpoint,'compact_ready',return_value={'capture_stage':'warm'}):
            warm.prepare(DAY)
        self.assertEqual(prepare.call_args.kwargs['capture_stage'],'warm')
        self.assertEqual(self.cipher.read_bytes(),before)
        self.assertFalse((self.out/'critical-prepared-feed.json').exists())
        self.assertIn('ready=true',(self.root/'output').read_text())

    def test_duplicate_first_source_or_overdrawn_warm_slots_cannot_prepare(self):
        self.start()
        for acquired in (['beforeinfo-first'], ['new'+str(i) for i in range(7)]):
            self.captured(acquired)
            with self.assertRaisesRegex(ValueError,'WARM_BUDGET_OR_STATE'), \
                    patch.object(warm.checkpoint,'prepare_compact_checkpoint') as prepare:
                warm.prepare(DAY)
            prepare.assert_not_called()

    def test_no_warm_input_does_not_create_a_delta_or_an_extra_publication(self):
        self.start(); self.captured([])
        with patch.object(warm.checkpoint,'prepare_compact_checkpoint') as prepare:
            warm.prepare(DAY)
        prepare.assert_not_called()
        self.assertTrue((self.root/'output').read_text().endswith('ready=false\n'))

    def test_early_warm_publish_requires_warm_remote_proof_and_never_uses_first_preparation(self):
        self.start(); self.captured()
        proof={'schema':'G11_COMPLETE_COMPACT_PUBLICATION_PROOF_V1','capture_stage':'warm',
            'day':DAY,'run_id':RUN,'publication_key':KEY,'publication_attempt':'1',
            'authority_sha256':'a'*64,'runtime_source_sha':'b'*40}
        self.write('.relay-output/warm-publication-proof.json',proof)
        (self.out/'critical-prepared-feed.json').write_text('old')
        with patch.dict(os.environ,{'CRITICAL_CHECKPOINT_VERIFIED':'true'}), patch.object(warm.subprocess,'run') as call:
            warm.publish(DAY)
        self.assertNotIn('--use-prepared',call.call_args.args[0])
        self.assertEqual(call.call_args.kwargs['env']['G11_WARM_PUBLICATION'],'true')
        self.assertFalse((self.out/'critical-prepared-feed.json').exists())
        proof['capture_stage']='critical'; self.write('.relay-output/warm-publication-proof.json',proof)
        with patch.dict(os.environ,{'CRITICAL_CHECKPOINT_VERIFIED':'true'}), self.assertRaisesRegex(ValueError,'WARM_REMOTE_PROOF'):
            warm.publish(DAY)

    def test_critical_encryption_retains_existing_delta_only_fallback_contract(self):
        with patch.dict(os.environ,{'G11_DELTA_BASE_KEY':BASE}), \
                patch.object(warm.checkpoint,'prepare_compact_checkpoint') as call:
            warm.critical_encrypt()
        self.assertEqual(call.call_args.args,(warm.STATE,warm.CIPHER,BASE))
        self.assertNotIn('capture_stage',call.call_args.kwargs)
        self.assertTrue((self.root/'output').read_text().endswith('ready=true\n'))


    def test_warm_transport_cannot_skip_later_full_backup_or_use_an_unsaved_full_base(self):
        root=Path(__file__).resolve().parents[1]
        workflow=(root/'.github/workflows/g11-free-runner.yml').read_text()
        action=(root/'.github/actions/g11-publication-checkpoint/action.yml').read_text()
        names=['id: warm-start\n','id: warm\n','id: warm-encrypt\n','id: warm-save\n',
               'id: warm-publish\n','id: encrypt-state\n','id: publication-state-verified\n','id: critical-retry\n']
        positions=[workflow.index(name) for name in names]
        self.assertEqual(positions,sorted(positions))
        start=workflow.split('id: warm-start\n',1)[1].split('      - name:',1)[0]
        self.assertIn("steps.critical-save.outputs.full-ready!='true'",start)
        fallback=workflow.split('id: critical-retry\n',1)[1].split('      - name:',1)[0]
        self.assertIn("steps.warm-publish.outcome!='success'",fallback)
        self.assertIn("steps.publication-state-verified.outputs.ready=='true'",fallback)
        stop=action.split('    - name: Keep warm failure on the ordinary later full checkpoint path\n',1)[1].split('    - name:',1)[0]
        self.assertIn("inputs.capture-stage == 'warm'",stop)
        result=subprocess.run(['bash','-c',stop.split('      run: ',1)[1].strip()],capture_output=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn("endsWith(steps.checkpoint-choice.outputs.delta_key,'-warm')",workflow)
        full=workflow.split('id: publication-state-verified\n',1)[1].split('      - name:',1)[0]
        self.assertNotIn('allow-compact',full)
        self.assertNotIn('capture-stage',full)


if __name__=='__main__': unittest.main()
