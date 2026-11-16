"""The fixed study never enters canonical state, acquisition, or inference."""
from datetime import datetime, timedelta
import importlib.util
import json
import os
import re
from pathlib import Path
import shutil
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
import subprocess
from unittest.mock import patch

import isolated_research_runner_v1 as runner
import research_state_isolation_v1 as isolation
import mashiro_fixed_display_v1 as display

DAY='2026-11-17'
KEY='20261117-01-01'
NOW=datetime.fromisoformat(DAY+'T12:01:00+09:00')
DEADLINE=NOW+timedelta(minutes=6)
CAPTURE=f'store/snapshots/predeadline/mashiro-fixed-v1-{KEY}.json'
RESULT=f'store/snapshots/night/mashiro-fixed-v1-result-{KEY}.json'
SOURCE='g11-state-v4-critical-'+DAY+'-42'
BINDING={'authority_sha256':'a'*64,'runtime_source_sha':'b'*40}


@unittest.skipUnless((Path(__file__).resolve().parents[1]/'.g11-mashiro-fixed/g11/research/mashiro_fixed_v1/test_observer.py').is_file(), 'Pinned observer is supplied by the fixed-study contract CI')
class FixedDisplayTests(unittest.TestCase):
    def fixture(self):
        path=Path(__file__).resolve().parents[1]/'.g11-mashiro-fixed/g11/research/mashiro_fixed_v1/test_observer.py'
        spec=importlib.util.spec_from_file_location('fixed_display_fixture',path)
        fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        store=fixture.Store(Path(tmp.name));m,p,mr,pr=fixture.source_packets()
        store.commit_snapshot('morning','official-day-input',m,mr)
        store.commit_snapshot('predeadline','beforeinfo-'+KEY,p,pr)
        return fixture,store

    def test_saved_observations_are_the_only_display_targets_and_pending_is_not_a_loss(self):
        f,store=self.fixture()
        value=display.projection(store,f.R,DAY,SOURCE,now=NOW)
        self.assertEqual('AWAITING_CAPTURE',value['projection']['state']);self.assertIsNone(value['projection']['recorded'])
        f.R.collect(store,DAY,clock=lambda:NOW)
        report=f.R.collect(store,DAY,clock=lambda:NOW)
        f.R.save_coverage(store,DAY,'capture',report)
        before={p:(p.stat().st_size,p.stat().st_mtime_ns) for p in store.root.rglob('*') if p.is_file()}
        value=display.projection(store,f.R,DAY,SOURCE,now=NOW)
        self.assertEqual(1,value['projection']['recorded']);self.assertEqual(6,len(value['projection']['rows']))
        self.assertTrue(all(r['outcome']=='WAITING' for r in value['projection']['rows']))
        self.assertEqual(before,{p:(p.stat().st_size,p.stat().st_mtime_ns) for p in store.root.rglob('*') if p.is_file()})
        store.time=f.DEADLINE+timedelta(minutes=2)
        receipt=f.receipt('result-k-20261117',store.time,'https://www1.mbrace.or.jp/od2/K/202611/k261117.lzh')
        store.commit_snapshot('night','official-day-results',{'OPERATIONAL_DATE_JST':DAY,'STATUS':'NIGHT_RESULTS_COMPLETE',
            'PENDING_RESULT_COUNT':0,'RESULTS':[f.ObserverTests().result()]},[receipt])
        f.R.settle(store,DAY,clock=lambda:store.time)
        value=display.projection(store,f.R,DAY,SOURCE,now=store.time)
        self.assertEqual(2,sum(r['outcome']=='WIN' for r in value['projection']['rows']))
        self.assertTrue(all(r['result_sha256'] for r in value['projection']['rows']))

    def test_tampered_coverage_and_wrong_checkpoint_fail_closed(self):
        f,store=self.fixture();report=f.R.collect(store,DAY,clock=lambda:NOW)
        f.R.save_coverage(store,DAY,'capture',report)
        path=store.root/'research/mashiro-fixed-v1'/DAY/'capture-coverage.json'
        value=json.loads(path.read_bytes());value['REPORT']['RECORDED']=0;path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'COVERAGE_COUNT'):display.projection(store,f.R,DAY,SOURCE,now=NOW)
        with self.assertRaisesRegex(ValueError,'CHECKPOINT'):display.projection(store,f.R,DAY,'unverified',now=NOW)

    def test_publication_requires_verified_persistence_before_requesting_a_token(self):
        f,store=self.fixture();value=display.projection(store,f.R,DAY,SOURCE,now=NOW)
        with patch.dict(os.environ,{'RESEARCH_CHECKPOINT_VERIFIED':'false'}):
            with self.assertRaisesRegex(ValueError,'PERSISTENCE_REQUIRED'):
                display.publish(value,opener=lambda *a,**k:self.fail('credential requested before persistence'))


class FixedResearchIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.state=self.root/'state';self.state.mkdir()
        self.write('store/snapshots/morning/official-day-input.json',{'STAGE':'MORNING','NAME':'official-day-input',
            'PAYLOAD':{'PREDEADLINE_PLAN':[{'RACE_KEY':KEY,'DUE_AT_JST':NOW.isoformat(),
                                         'MUST_FINISH_BEFORE_JST':DEADLINE.isoformat()}]}})
        self.write(f'store/snapshots/predeadline/current-growth-p3-live-{KEY}.json',{'bets':['1-2-3']})
    def write(self,name,value,at=NOW):
        p=self.state/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(isolation.canonical(value))
        os.utime(p,(at.timestamp(),at.timestamp()));return p
    def capture(self):
        return self.write(CAPTURE,{'PAYLOAD':{'SCHEMA':'G11_MASHIRO_FIXED_OBSERVATION_V1',
            'OPERATIONAL_DATE_JST':DAY,'RACE_KEY':KEY,'CAPTURED_AT_JST':NOW.isoformat(),
            'DEADLINE_AT_JST':DEADLINE.isoformat(),'RESULT_INPUT_USED':False,
            'P3_OUTPUTS_USED':False,'PRODUCTION_MUTATED':False}})
    def result(self):
        at=DEADLINE+timedelta(minutes=1)
        return self.write(RESULT,{'PAYLOAD':{'SCHEMA':'G11_MASHIRO_FIXED_FIRST_PLACE_RESULT_V1',
            'RACE_KEY':KEY,'JOINED_AT_JST':at.isoformat(),'DEADLINE_AT_JST':DEADLINE.isoformat(),
            'PROTOCOL_SHA256':'c'*64,'RESULT_SNAPSHOT_SHA256':'d'*64,
            'OBSERVATION_SNAPSHOT_SHA256':'e'*64,'PRODUCTION_MUTATED':False}},at)

    def test_allowlist_is_day_scoped_and_never_imports_new_study_to_canonical_night(self):
        coverage=f'store/research/mashiro-fixed-v1/{DAY}/capture-coverage.json'
        for name in (CAPTURE,RESULT,coverage):
            self.assertTrue(isolation.allowed(name,DAY))
            self.assertFalse(isolation.allowed(name,DAY,night=True))
            self.assertFalse(isolation.allowed(name,'2026-11-18'))
        self.assertFalse(isolation.allowed('store/research/mashiro-fixed-v1/2026-11-17/../../model.json',DAY))
        self.assertFalse(isolation.allowed('store/snapshots/night/official-day-results.json',DAY))

    def test_bundle_exports_later_results_but_holds_a_late_first_observation(self):
        before=isolation.core_sources(self.state,DAY);p=self.capture();self.result()
        os.utime(p,(DEADLINE.timestamp(),DEADLINE.timestamp()))
        out=self.root/'out'
        value=isolation.export_bundle(self.state,out,DAY,BINDING,SOURCE,{},before,
            closed=True,now=DEADLINE+timedelta(minutes=2))
        self.assertNotIn(CAPTURE,value['members']);self.assertIn(RESULT,value['members'])
        self.assertEqual(1,value['held_late_count']);isolation.read_bundle(out,DAY)
        self.assertEqual(before,isolation.core_sources(self.state,DAY))

    def test_canonical_import_excludes_capture_result_and_coverage_and_preserves_bundle_payload(self):
        self.capture();self.result()
        self.write(f'store/research/mashiro-fixed-v1/{DAY}/result-coverage.json',{'status':'fixture'})
        before=isolation.core_sources(self.state,DAY);out=self.root/'out'
        value=isolation.export_bundle(self.state,out,DAY,BINDING,SOURCE,{},before,
            closed=True,now=DEADLINE+timedelta(minutes=2))
        value['_root']=out;target=self.root/'empty-canonical';target.mkdir()
        self.assertEqual(0,isolation.apply_files(value,target,DAY,night=True))
        self.assertFalse(list(target.rglob('*.json')))
        self.assertEqual(isolation.bundle_payload_sha({'members':{}},DAY),isolation.bundle_payload_sha(value,DAY))

    def test_old_days_skip_without_importing_models_or_requiring_new_source(self):
        with patch.object(runner.importlib.util,'spec_from_file_location',side_effect=AssertionError('unexpected import')):
            r=runner.fixed_mashiro_research(self.state,self.root,self.root/'missing','2026-11-16',False)
        self.assertEqual('OUTSIDE_FIXED_WINDOW',r['STATUS']);self.assertFalse(r['CHANGED'])

    def test_independent_collector_uses_only_a_store_and_continues_if_optional_source_is_missing(self):
        before=isolation.protected_metadata(self.state,DAY)
        r=runner.fixed_mashiro_research(self.state,self.root,self.root/'missing',DAY,False)
        self.assertEqual('UNAVAILABLE',r['STATUS']);self.assertFalse(r['CHANGED'])
        self.assertEqual(before,isolation.protected_metadata(self.state,DAY))

    def test_closed_worker_can_score_new_results_without_reopening_forecasts(self):
        self.capture();first=self.root/'first';second=self.root/'second'
        before=isolation.core_sources(self.state,DAY)
        isolation.export_bundle(self.state,first,DAY,BINDING,SOURCE,{},before,
            closed=True,now=DEADLINE+timedelta(minutes=1))
        for name,path in isolation.research_files(self.state,DAY).items():path.unlink()
        class Clock(datetime):
            @staticmethod
            def now(tz):return DEADLINE+timedelta(minutes=2)
        auth=SimpleNamespace(verify_runtime=lambda *a:None)
        def score(*args):
            self.result();return {'STATUS':'FIXED_RESEARCH_ONLY','CHANGED':True}
        with patch.object(runner,'authority_binding',return_value=(auth,BINDING)), \
             patch.object(runner,'fixed_mashiro_research',side_effect=score), \
             patch.object(runner,'datetime',Clock),patch.object(isolation,'datetime',Clock),patch.object(runner,'outputs'), \
             patch.object(runner,'capture_only_research',side_effect=AssertionError('forecast reopened')):
            runner.run_research(self.state,self.root,self.root,first,second,DAY,SOURCE,self.root/'feed')
        saved=isolation.read_bundle(second,DAY)
        self.assertTrue(saved['closed']);self.assertIn(CAPTURE,saved['members']);self.assertIn(RESULT,saved['members'])
        self.assertEqual(before,isolation.core_sources(self.state,DAY))


@unittest.skipUnless(os.environ.get('G11_MASHIRO_FIXED_ENCRYPTION_TEST')=='true',
                     'Encrypted fixture roundtrip runs in the pinned-source contract CI')
class EncryptedEvidenceIntegrationTests(unittest.TestCase):
    def test_actual_workflow_readback_authenticates_the_small_daily_archive(self):
        root=Path(__file__).resolve().parents[1]
        path=root/'.g11-mashiro-fixed/g11/research/mashiro_fixed_v1/test_observer.py'
        spec=importlib.util.spec_from_file_location('fixed_archive_fixture',path)
        fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
        import encrypt_runtime_state_v1 as encryption
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory);home=work/'gnupg';home.mkdir(mode=0o700)
            store=fixture.Store(work/'state');m,p,mr,pr=fixture.source_packets()
            store.commit_snapshot('morning','official-day-input',m,mr)
            store.commit_snapshot('predeadline','beforeinfo-'+fixture.KEY,p,pr)
            fixture.R.save_observation(store,store.load_verified_snapshot('morning','official-day-input'),
                store.load_verified_snapshot('predeadline','beforeinfo-'+fixture.KEY),fixture.KEY,clock=lambda:fixture.NOW)
            store.time=fixture.DEADLINE+timedelta(minutes=2)
            receipt=fixture.receipt('result-k-20261117',store.time,'https://www1.mbrace.or.jp/od2/K/202611/k261117.lzh')
            store.commit_snapshot('night','official-day-results',{'OPERATIONAL_DATE_JST':fixture.DAY,
                'STATUS':'NIGHT_RESULTS_COMPLETE','PENDING_RESULT_COUNT':0,'RESULTS':[fixture.ObserverTests().result()]},[receipt])
            fixture.R.settle(store,fixture.DAY,clock=lambda:store.time)
            fixture.R.export_evidence(store,fixture.DAY,work/'evidence',{'source_checkpoint':'SYNTHETIC_FIXTURE_ONLY'})
            secret=work/'fixture-password';secret.write_text('synthetic-fixture-only\n');secret.chmod(0o600)
            with patch.dict(os.environ,{'GNUPGHOME':str(home)}):
                with secret.open('rb') as fd:
                    encryption.encrypt(work/'evidence',work/'encrypted.gpg',passphrase_fd=fd.fileno())
                with secret.open('rb') as fd:
                    subprocess.run(['gpg','--batch','--yes','--pinentry-mode','loopback','--passphrase-fd',str(fd.fileno()),
                        '--decrypt','--output',str(work/'.mashiro-fixed-readback.tgz'),str(work/'encrypted.gpg')],
                        pass_fds=(fd.fileno(),),check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            text=(root/'.github/workflows/g11-isolated-live-research.yml').read_text()
            section=text.split('      - name: Encrypt and verify only the fixed-study daily evidence',1)[1]
            match=re.search("python3 - <<'PY'\\n(.*?)\\n          PY",section,re.S)
            self.assertIsNotNone(match)
            import textwrap
            body=textwrap.dedent(match[1]);previous=Path.cwd()
            try:
                os.chdir(work);exec(compile(body,'actual-encrypted-evidence-readback','exec'),{})
            finally:os.chdir(previous)


if __name__=='__main__':unittest.main()
