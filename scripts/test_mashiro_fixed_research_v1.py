"""The fixed study never enters canonical state, acquisition, or inference."""
from datetime import datetime, timedelta
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

import isolated_research_runner_v1 as runner
import research_state_isolation_v1 as isolation

DAY='2026-11-17'
KEY='20261117-01-01'
NOW=datetime.fromisoformat(DAY+'T12:01:00+09:00')
DEADLINE=NOW+timedelta(minutes=6)
CAPTURE=f'store/snapshots/predeadline/mashiro-fixed-v1-{KEY}.json'
RESULT=f'store/snapshots/night/mashiro-fixed-v1-result-{KEY}.json'
SOURCE='g11-state-v4-critical-'+DAY+'-42'
BINDING={'authority_sha256':'a'*64,'runtime_source_sha':'b'*40}


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


if __name__=='__main__':unittest.main()
