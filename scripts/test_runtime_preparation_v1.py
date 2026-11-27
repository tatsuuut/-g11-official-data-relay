"""Complete byte-preserving restore and public dependency cache boundaries."""
import gzip
import ast
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import re
import textwrap
from datetime import datetime, date, timedelta, timezone
from types import SimpleNamespace
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

import extract_runtime_state_v1 as restore
import encrypt_runtime_state_v1 as archive
import public_python_runtime_v1 as runtime


class NativeRestorePreparationTests(unittest.TestCase):
    def test_existing_accelerator_is_reused_without_package_install(self):
        with patch.object(archive.shutil,'which',return_value='/usr/bin/igzip'),patch.object(archive.subprocess,'run') as command:
            self.assertTrue(archive.prepare_gzip_acceleration());command.assert_not_called()

    def test_unavailable_or_slow_optional_package_preserves_standard_gzip_fallback(self):
        for result in (Mock(returncode=100),subprocess.TimeoutExpired('apt-get',10)):
            with self.subTest(result=type(result).__name__),patch.object(archive.shutil,'which',return_value=None), \
                    patch.object(archive.subprocess,'run',side_effect=result if isinstance(result,Exception) else None,return_value=result) as command:
                self.assertFalse(archive.prepare_gzip_acceleration())
                self.assertEqual(command.call_args.kwargs['timeout'],10)

    def test_public_runtime_action_does_not_wait_for_optional_os_packages(self):
        action=(Path(__file__).resolve().parents[1]/'.github/actions/g11-public-python/action.yml').read_text()
        self.assertNotIn('--prepare-gzip',action)
        self.assertIn("if: inputs.enabled == 'true'",action)
        self.assertIn('path: .relay-cache/state.tgz.gpg',action)
        self.assertIn('key: ${{ inputs.prewarm-base-key }}',action)
        self.assertNotIn('restore-keys:',action)
        self.assertIn("steps.prewarm-cache.outputs.cache-hit == 'true'",action)


class StateRestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive = self.root / 'state.tgz'
        self.dest = self.root / 'restored'

    def archive_members(self, members):
        with tarfile.open(self.archive, 'w:gz') as handle:
            for member, data in members:
                handle.addfile(member, io.BytesIO(data))

    def member(self, name, data=b'immutable', kind=tarfile.REGTYPE):
        member = tarfile.TarInfo(name)
        member.type = kind
        member.size = len(data) if kind == tarfile.REGTYPE else 0
        return member, data

    def test_every_byte_and_directory_matches_existing_extractor(self):
        source = self.root / 'original'
        source.mkdir()
        files = {'.hidden': b'kept', 'store/lock.json': b'{"before_deadline":true}',
                 'daily-runtime/source/a.py': b'frozen source', 'model.bin': os.urandom(2 ** 20)}
        for name, value in files.items():
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        (source / 'empty').mkdir()
        with tarfile.open(self.archive, 'w:gz') as handle:
            handle.add(source, arcname='.')
        old = self.root / 'old'
        with tarfile.open(self.archive, 'r:gz') as handle:
            handle.getmembers()
            handle.extractall(old, filter='data')
        self.dest.mkdir()
        restore.extract_complete_state(self.archive, self.dest)
        snapshot = lambda p: {str(x.relative_to(p)): (x.read_bytes() if x.is_file() else None)
                              for x in p.rglob('*')}
        self.assertEqual(snapshot(old), snapshot(self.dest))
        self.assertEqual(snapshot(source), snapshot(self.dest))

    def test_late_unsafe_member_leaves_destination_empty(self):
        for name, kind in [('../escape', tarfile.REGTYPE), ('/escape', tarfile.REGTYPE),
                           ('link', tarfile.SYMTYPE), ('hard', tarfile.LNKTYPE),
                           ('device', tarfile.CHRTYPE), ('fifo', tarfile.FIFOTYPE)]:
            with self.subTest(name=name, kind=kind):
                self.archive_members([self.member('good'), self.member(name, kind=kind)])
                with self.assertRaises(ValueError):
                    restore.extract_complete_state(self.archive, self.dest)
                self.assertFalse(self.dest.exists())
                self.assertFalse(list(self.root.glob('.g11-state-extract-*')))

    def test_gzip_crc_failure_after_tar_end_never_exposes_partial_state(self):
        self.archive_members([self.member('good')])
        content = bytearray(self.archive.read_bytes())
        content[-8] ^= 1
        self.archive.write_bytes(content)
        with self.assertRaises((gzip.BadGzipFile, tarfile.ReadError)):
            restore.extract_complete_state(self.archive, self.dest)
        self.assertFalse(self.dest.exists())

    def test_native_failure_after_valid_tar_never_exposes_state(self):
        self.archive_members([self.member('good')])
        child = Mock(stdout=io.BytesIO(gzip.decompress(self.archive.read_bytes())))
        child.wait.return_value = 1; child.poll.return_value = 1
        with patch.object(restore.shutil, 'which', return_value='/synthetic/igzip'), \
                patch.object(restore.subprocess, 'Popen', return_value=child):
            with self.assertRaises(gzip.BadGzipFile):
                restore.extract_complete_state(self.archive, self.dest)
        self.assertFalse(self.dest.exists())
        self.assertFalse(list(self.root.glob('.g11-state-extract-*')))

    def test_concatenated_gzip_and_late_crc_are_checked_by_both_readers(self):
        self.archive_members([self.member('good')])
        raw = gzip.decompress(self.archive.read_bytes()); middle = len(raw)//2
        for reader in ['python'] + (['igzip'] if restore.shutil.which('igzip') else []):
            with self.subTest(reader=reader):
                self.archive.write_bytes(gzip.compress(raw[:middle])+gzip.compress(raw[middle:]))
                restore.extract_complete_state(self.archive, self.dest, decompressor=reader)
                self.assertEqual(b'immutable', (self.dest/'good').read_bytes())
                restore.shutil.rmtree(self.dest)
                damaged = bytearray(self.archive.read_bytes()); damaged[-8] ^= 1
                self.archive.write_bytes(damaged)
                with self.assertRaises((gzip.BadGzipFile, tarfile.ReadError)):
                    restore.extract_complete_state(self.archive, self.dest, decompressor=reader)
                self.assertFalse(self.dest.exists())

    def test_empty_truncated_duplicate_and_conflicting_archives_fail_closed(self):
        for members in [[], [self.member('good'), self.member('./good')],
                        [self.member('parent'), self.member('parent/child')]]:
            self.archive_members(members)
            with self.assertRaises((ValueError, OSError)):
                restore.extract_complete_state(self.archive, self.dest)
            self.assertFalse(self.dest.exists())
        self.archive_members([self.member('large', os.urandom(2 ** 18))])
        self.archive.write_bytes(self.archive.read_bytes()[:1000])
        with self.assertRaises((EOFError, tarfile.ReadError)):
            restore.extract_complete_state(self.archive, self.dest)
        self.assertFalse(self.dest.exists())

    def test_existing_state_and_symlink_are_never_replaced(self):
        self.archive_members([self.member('good')])
        self.dest.mkdir()
        lock = self.dest / 'lock'
        lock.write_bytes(b'existing LOCK')
        with self.assertRaises(ValueError):
            restore.extract_complete_state(self.archive, self.dest)
        self.assertEqual(b'existing LOCK', lock.read_bytes())
        linked = self.root / 'link'
        linked.symlink_to(self.dest)
        with self.assertRaises(ValueError):
            restore.extract_complete_state(self.archive, linked)


class PublicRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'public-env'
        self.pathfile = Path(self.tmp.name) / 'path'
        self.environment = patch.dict(os.environ, {'GITHUB_PATH': str(self.pathfile)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.distributions = [['catboost', '1.2.8'], ['lxml', '6.1.1']]

    def save(self):
        self.root.mkdir(exist_ok=True)
        (self.root / 'g11-public-runtime.json').write_text(json.dumps({
            'identity': runtime.identity(self.root, 'runtime'), 'distributions': self.distributions}))

    def test_verified_hit_does_not_install_or_recreate(self):
        self.save()
        with patch.object(runtime, 'probe', return_value=self.distributions), \
                patch.object(runtime.subprocess, 'run') as install, \
                patch.object(runtime.venv, 'EnvBuilder') as builder:
            self.assertTrue(runtime.prepare(self.root, 'runtime'))
        install.assert_not_called()
        builder.assert_not_called()
        self.assertEqual(str(self.root / 'bin') + '\n', self.pathfile.read_text())

    def test_miss_changed_identity_corruption_or_import_failure_rebuilds(self):
        for condition in ['missing', 'identity', 'corrupt', 'wrong_shape', 'import', 'distribution']:
            with self.subTest(condition=condition):
                self.save()
                manifest = self.root / 'g11-public-runtime.json'
                if condition == 'missing': manifest.unlink()
                if condition == 'identity':
                    value = json.loads(manifest.read_text()); value['identity']['image_version'] = 'wrong'
                    manifest.write_text(json.dumps(value))
                if condition == 'corrupt': manifest.write_text('bad')
                if condition == 'wrong_shape': manifest.write_text('[]')
                results = [self.distributions]
                if condition == 'import': results = [subprocess.CalledProcessError(1, 'probe'), self.distributions]
                if condition == 'distribution': results = [[['catboost', 'wrong']], self.distributions]
                builder = Mock()
                builder.create.side_effect = lambda root: root.mkdir()
                with patch.object(runtime, 'probe', side_effect=results), \
                        patch.object(runtime.venv, 'EnvBuilder', return_value=builder), \
                        patch.object(runtime.subprocess, 'run') as install:
                    self.assertFalse(runtime.prepare(self.root, 'runtime'))
                builder.create.assert_called_once()
                self.assertIn('catboost==1.2.8', install.call_args.args[0])
                self.assertIn('lxml==6.1.1', install.call_args.args[0])

    def test_bad_install_cannot_enable_environment(self):
        builder = Mock()
        builder.create.side_effect = lambda root: root.mkdir()
        with patch.object(runtime.venv, 'EnvBuilder', return_value=builder), \
                patch.object(runtime.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'pip')):
            with self.assertRaises(subprocess.CalledProcessError):
                runtime.prepare(self.root, 'runtime')
        self.assertFalse(self.pathfile.exists())
        self.assertFalse((self.root / 'g11-public-runtime.json').exists())

    def test_key_is_bound_to_profile_runner_image_interpreter_and_absolute_path(self):
        base = runtime.identity(self.root, 'runtime')
        for field in ['root', 'profile', 'requirements', 'python', 'executable_sha256', 'machine', 'image_version']:
            self.assertNotEqual(runtime.fingerprint(base), runtime.fingerprint({**base, field: 'changed'}))
        self.assertNotEqual(runtime.fingerprint(base), runtime.fingerprint(runtime.identity(self.root, 'parser')))

    def test_activating_the_environment_keeps_the_base_interpreter_identity(self):
        before = runtime.identity(self.root, 'runtime')
        with patch.object(runtime.sys, 'executable', str(self.root / 'bin/python3')):
            self.assertEqual(before, runtime.identity(self.root, 'runtime'))


@unittest.skipUnless(os.environ.get('WILD4_BOUND_DAY_SOURCE_PATH') and os.environ.get('WILD4_BOUND_WORKFLOW_PATH'),
                     'exact frozen private binder and child are provided by CI')
class BoundDayReadTests(unittest.TestCase):
    def binder(self):
        source=Path(os.environ['WILD4_BOUND_DAY_SOURCE_PATH']).read_text()
        node=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='bind_day')
        self.assertEqual(hashlib.sha256(ast.dump(node,include_attributes=False).encode()).hexdigest(),
                         '40ef2009d8247a620ea6100e29838642035a83ff88c5879a5d93c6d339caab62')
        return node

    def test_previous_night_flag_is_used_only_for_an_unbound_day_without_prior_locks(self):
        node=self.binder()
        uses=[n for n in ast.walk(node) if isinstance(n,ast.Name) and isinstance(n.ctx,ast.Load) and n.id=='previous_night_complete']
        self.assertEqual(len(uses),1)
        choice=next(n for n in ast.walk(node) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='choose_day')
        self.assertIn(uses[0],[k.value for k in choice.keywords if k.arg=='previous_night_complete'])
        pinned=next(n for n in ast.walk(node) if isinstance(n,ast.If) and isinstance(n.test,ast.Call) and
                    isinstance(n.test.func,ast.Attribute) and isinstance(n.test.func.value,ast.Name) and n.test.func.value.id=='pin')
        self.assertIn(choice,list(ast.walk(pinned.orelse[0].orelse[0])))

    def run_bound(self, prior, *, wrong_day=False, bad_model=False):
        node=self.binder();model={'identity':'a'*64,'registry_sha256':'b'*64};saved=[]
        with tempfile.TemporaryDirectory() as tmp:
            ledger=Path(tmp);(ledger/'day-model-binding.json').write_text('test-only pin')
            (ledger/'20261117-01-01.json').write_text('test-only immutable LOCK')
            choose=Mock(side_effect=AssertionError('bound day must not select a model'))
            def refresh(field):
                def run(ledger,state,evidence=None,**kwargs):
                    state[field]=copy.deepcopy(evidence);return state
                return run
            def require(ok,code):
                if not ok:raise ValueError(code)
            scope={'initialize':lambda *_:{'active':model},'adopt_update_policy':lambda state,now:state,
                   'adopt_saved_cohort_policy':lambda state,now:state,
                   'refresh_generation':refresh('generation_evidence'),'refresh_model_scope':refresh('model_scope_evidence'),
                   'refresh_update_qualification':lambda ledger,state,day,now:{**state,'update_qualification':day},
                   'KEY':re.compile(r'\d{8}-\d{2}-\d{2}'),'read':lambda p:{'date':'2026-11-16' if wrong_day else '2026-11-17','model':model},
                   'state_path':lambda p:p/'model-registry/lifecycle.json','model_info':Mock(return_value={} if bad_model else model),
                   'b':SimpleNamespace(load_json=lambda p:{'CANDIDATE_REGISTRY_SHA256':model['registry_sha256']}),
                   'save':lambda p,state:saved.append((p.name,copy.deepcopy(state))),'require':require,'choose_day':choose,'AUTHORITY':'test'}
            exec(compile(ast.Module(body=[node],type_ignores=[]),'<exact-frozen-binder>','exec'),scope)
            root=scope['bind_day'](ledger,ledger/'original',ledger/'calibrated','2026-11-17',previous_night_complete=prior,
                                   now=datetime(2026,11,17,0,tzinfo=timezone.utc),generation_evidence={'verified':'generation'},
                                   model_scope_evidence={'verified':'scope'})
            choose.assert_not_called();scope['model_info'].assert_called_once()
            self.assertEqual(root.name,model['identity']);self.assertEqual(saved[0][1]['generation_evidence'],{'verified':'generation'})
            self.assertEqual(saved[0][1]['model_scope_evidence'],{'verified':'scope'})
            self.assertEqual(saved[0][1]['update_qualification'],'2026-11-17')
            return saved

    def test_bound_model_registry_and_learning_updates_are_identical_with_or_without_the_unused_flag(self):
        self.assertEqual(self.run_bound(True),self.run_bound(False))
        for prior in (True,False):
            with self.assertRaisesRegex(ValueError,'DAY_MODEL_DATE'):self.run_bound(prior,wrong_day=True)
            with self.assertRaisesRegex(ValueError,'DAILY_MODEL_IDENTITY'):self.run_bound(prior,bad_model=True)

    def test_only_existing_day_pin_skips_the_two_reads_and_unbound_days_keep_original_evidence_checks(self):
        child=Path(os.environ['WILD4_BOUND_WORKFLOW_PATH']).read_text()
        code=textwrap.dedent(child.split('          prior_complete=False\n',1)[1].split('          generation_evidence=None\n',1)[0])
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return datetime.fromisoformat('2026-11-17T07:00:00+09:00')
        for pinned in (False,True):
            with self.subTest(pinned=pinned),tempfile.TemporaryDirectory() as tmp:
                temp=Path(tmp);ledger=temp/'g11-wild4-future-locks';ledger.mkdir()
                if pinned:(ledger/'day-model-binding.json').write_text('pin remains verified by bind_day')
                def fetch(url):
                    day='2026-11-16' if url.endswith('2026-11-16') else '2026-11-17'
                    return {'operational_date_jst':day,'stage':'NIGHT','status':'PASS','counts':{'races':1,'results':1},
                            'races':[{'deadline_jst':'2026-11-17T08:00:00+09:00'}]},None,None
                request=Mock(side_effect=fetch);scope={'temp':temp,'day':'2026-11-17','prior_complete':False,
                    'datetime':Clock,'date':date,'timedelta':timedelta,'timezone':timezone,'hot':SimpleNamespace(fetch=request)}
                exec(compile(code,'<deployed-day-read-guard>','exec'),scope)
                self.assertEqual(request.call_count,0 if pinned else 2)
                self.assertEqual(scope['prior_complete'],not pinned)


@unittest.skipUnless(os.environ.get('WILD4_WORKFLOW_PATH'), 'tested against the pinned reusable workflow in contract CI')
class CurrentInferenceBoundaryTests(unittest.TestCase):
    def test_current_deployed_model_odds_inference_learning_and_settlement_stay_unchanged(self):
        # Baseline: the actually deployed 1184c7f5 workflow, not the older
        # research branch or the earlier handoff fixture kept by its own tests.
        source = Path(os.environ['WILD4_WORKFLOW_PATH']).read_text()
        protected = {
            'Carry previous-day model registry without copying previous predictions': 'cdfe9943acc632fc699ed539c4becc85db7d140997c9ce1b356fc412e6969ab5',
            'Bind one model for the operational day': '988e96549a566fa5bcbdfce254381445386d56a5e17a02da61743b62ffb49db6',
            'Verify frozen WILD4 inference runtime': '4495877bd12adea8c5d209ed488a85122892ef0858bf8161cda0d4340c6d0202',
            'Check for a matching unexpired official odds packet': '455bd16a042ba59030bff53f69ad844e74b1e57a43352a55eb9c5e2ff117ed7a',
            'Lock future races before deadline without any production write': '4c4fa5c0dec49e4fb47d4322bee731ab7ad8c503d659ee778db5abd13bffb4a6',
            'Settle only immutable locks from verified final NIGHT': '03695c52becd2dcbd700f7dcf8d763442b280356d1ef17fd619aa574923801a3',
            'Continue existing BET learning after completed NIGHT': '797e01d03c64314e9b7a7ba313323e424ed85ffd033e623cb9fefe829534e095'}
        for name, expected in protected.items():
            block = source.split('      - name: ' + name + '\n', 1)[1].split('      - name:', 1)[0]
            if name == 'Settle only immutable locks from verified final NIGHT':
                # The one-day original-LOCK recovery may invoke the identical
                # existing settlement body; only its trigger gains this branch.
                block = block.replace(' && (((github.event_name', ' && ((github.event_name')
                block = block.replace(" || steps.publication-recovery.outputs.changed == 'true')", '')
            with self.subTest(name=name):
                self.assertEqual(expected, hashlib.sha256(block.encode()).hexdigest())


import daily_runtime_bootstrap_v1 as guard
from unittest.mock import MagicMock

MORNING_GUARD_DAY='2026-11-28'
MORNING_GUARD_BIND={'schema':'G11_DAILY_RUNTIME_AUTHORITY_V1','operational_date_jst':MORNING_GUARD_DAY,'phase':'MORNING',
      'authority_sha256':'a'*64,'runtime_source_sha':'b'*40,'immutable':True,
      'p3':{'version':'fixture'},'abeken_v53':{},'wild':{},'true_ai':{}}

class MorningAuthorityGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.state=Path(self.tmp.name)
        self.site=Mock(return_value={'status':'PASS','operational_date_jst':MORNING_GUARD_DAY,'stage':'MORNING','daily_runtime':dict(MORNING_GUARD_BIND),'source':{'github_sha':'b'*40},'races':[{'key':'fixture'}],'counts':{'races':1}})
        self.impl=Mock(); self.impl.load.return_value={'fixture':'local'}
        self.impl.feed_binding.return_value=dict(MORNING_GUARD_BIND)
        self.impl.json_file.return_value={'OPERATIONAL_DATE_JST':MORNING_GUARD_DAY,'MORNING_COMPLETE':True}
        self.load=Mock(return_value=self.impl)
    def run_guard(self,day=MORNING_GUARD_DAY,phase='morning'):
        return guard.guard_accepted_morning(self.state,day,phase,read_site=self.site,load_retained=self.load)
    def authority(self,projection=True):
        p=self.state/'daily-runtime'/MORNING_GUARD_DAY/'authority.json';p.parent.mkdir(parents=True);p.write_text('{}')
        if projection:
            p=self.state/'store/snapshots/morning/workbook-projection.json';p.parent.mkdir(parents=True);p.write_text('{}')
    def test_first_morning_404(self):
        self.site.return_value=None
        self.assertEqual(self.run_guard()['status'],'NO_ACCEPTED_DAY_FEED');self.load.assert_not_called()
    def test_incident_rejects_previous_day_restore(self):
        with self.assertRaisesRegex(RuntimeError,'CHECKPOINT_MISSING_NO_REGENERATION'):self.run_guard()
        self.load.assert_not_called()
    def test_matching_retained_state(self):
        self.authority();self.assertEqual(self.run_guard()['status'],'ACCEPTED_MORNING_CHECKPOINT_VERIFIED')
        self.assertEqual(self.impl.verify_locks.call_count,2)
    def test_different_authority(self):
        self.authority();self.impl.feed_binding.return_value={**MORNING_GUARD_BIND,'authority_sha256':'c'*64}
        with self.assertRaisesRegex(RuntimeError,'AUTHORITY_CONFLICT'):self.run_guard()
    def test_incomplete_morning(self):
        self.authority(False)
        with self.assertRaisesRegex(RuntimeError,'LOCKS_MISSING'):self.run_guard()
    def test_metadata_wrong_date(self):
        self.authority();self.impl.json_file.return_value={'OPERATIONAL_DATE_JST':'2026-11-27','MORNING_COMPLETE':True}
        with self.assertRaisesRegex(RuntimeError,'LOCKS_MISSING'):self.run_guard()
    def test_incomplete_metadata(self):
        self.authority();self.impl.json_file.return_value={'OPERATIONAL_DATE_JST':MORNING_GUARD_DAY,'MORNING_COMPLETE':False}
        with self.assertRaisesRegex(RuntimeError,'LOCKS_MISSING'):self.run_guard()
    def test_later_phase_no_regression(self):
        self.site.return_value['stage']='PREDEADLINE';self.site.return_value['daily_runtime']['phase']='PREDEADLINE'
        with self.assertRaisesRegex(RuntimeError,'ALREADY_ADVANCED'):self.run_guard()
    def test_unknown_authority(self):
        del self.site.return_value['daily_runtime']
        with self.assertRaisesRegex(RuntimeError,'AUTHORITY_UNKNOWN'):self.run_guard()
    def test_source_binding(self):
        self.site.return_value['source']['github_sha']='c'*40
        with self.assertRaisesRegex(RuntimeError,'SITE_FEED_BINDING'):self.run_guard()
    def test_count_binding(self):
        self.site.return_value['counts']['races']=2
        with self.assertRaisesRegex(RuntimeError,'SITE_FEED_BINDING'):self.run_guard()
    def test_other_day_feed(self):
        self.site.return_value['operational_date_jst']='2026-11-27'
        with self.assertRaisesRegex(RuntimeError,'DAY_IDENTITY'):self.run_guard()
    def test_transport_error(self):
        self.site.side_effect=RuntimeError('MORNING_SITE_READBACK_HTTP_500')
        with self.assertRaisesRegex(RuntimeError,'HTTP_500'):self.run_guard()
        self.load.assert_not_called()
    def test_outside_scope_untouched(self):
        for day,phase in [(MORNING_GUARD_DAY,'night'),(MORNING_GUARD_DAY,'predeadline'),('2026-11-27','morning')]:
            self.assertEqual(self.run_guard(day,phase)['status'],'OUTSIDE_MORNING_GUARD_SCOPE')
        self.site.assert_not_called()
    def test_lock_verification_failure_propagates(self):
        self.authority();self.impl.verify_locks.side_effect=RuntimeError('DAILY_MORNING_LOCK_MISMATCH')
        with self.assertRaisesRegex(RuntimeError,'DAILY_MORNING_LOCK_MISMATCH'):self.run_guard()
    def test_readonly_files(self):
        self.authority();before={str(p):p.read_bytes() for p in self.state.rglob('*') if p.is_file()}
        self.run_guard();self.assertEqual(before,{str(p):p.read_bytes() for p in self.state.rglob('*') if p.is_file()})


class MorningAuthorityReadbackTests(unittest.TestCase):
    def setUp(self):
        import sys, types
        from unittest.mock import patch
        self.origin='https://example.invalid'
        self.url=self.origin+'/api/g11-feed?date='+MORNING_GUARD_DAY
        self.reply=Mock(); self.reply.status=200; self.reply.geturl.return_value=self.url
        self.raw=b'{"status":"PASS"}'
        self.reply.read.return_value=self.raw
        self.reply.headers={'x-g11-feed-sha256':hashlib.sha256(self.raw).hexdigest()}
        self.opener=MagicMock(); self.opener.open.return_value.__enter__.return_value=self.reply
        dispatcher=types.ModuleType('predeadline_dispatch_v1'); dispatcher.NoRedirect=Mock
        delivery=types.ModuleType('daily_runtime_delivery_v1');delivery.ORIGIN=self.origin
        self.enterContext(patch.dict(sys.modules,{'predeadline_dispatch_v1':dispatcher,'daily_runtime_delivery_v1':delivery}))
        self.enterContext(patch.dict(os.environ,{'G11_APP_ORIGIN':self.origin}))
        self.enterContext(patch('urllib.request.build_opener',return_value=self.opener))
    def test_success(self): self.assertEqual(guard.read_accepted_day(MORNING_GUARD_DAY),{'status':'PASS'})
    def test_hash_mismatch(self):
        self.reply.headers={}
        with self.assertRaisesRegex(RuntimeError,'READBACK_INVALID'):guard.read_accepted_day(MORNING_GUARD_DAY)
    def test_redirect_rejected(self):
        from urllib.error import HTTPError
        self.opener.open.side_effect=HTTPError(self.url,302,'redirect',{},None)
        with self.assertRaisesRegex(RuntimeError,'HTTP_302'):guard.read_accepted_day(MORNING_GUARD_DAY)
    def test_404_absence(self):
        from urllib.error import HTTPError
        self.opener.open.side_effect=HTTPError(self.url,404,'missing',{},None)
        self.assertIsNone(guard.read_accepted_day(MORNING_GUARD_DAY))
    def test_500_not_absence(self):
        from urllib.error import HTTPError
        self.opener.open.side_effect=HTTPError(self.url,500,'fail',{},None)
        with self.assertRaisesRegex(RuntimeError,'HTTP_500'):guard.read_accepted_day(MORNING_GUARD_DAY)
    def test_wrong_origin(self):
        from unittest.mock import patch
        with patch.dict(os.environ,{'G11_APP_ORIGIN':'https://wrong.invalid'}):
            with self.assertRaisesRegex(RuntimeError,'ORIGIN_MISMATCH'):guard.read_accepted_day(MORNING_GUARD_DAY)
        self.opener.open.assert_not_called()


if __name__ == '__main__':
    unittest.main()
