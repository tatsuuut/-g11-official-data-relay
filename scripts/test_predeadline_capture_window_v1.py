"""Replay reviewed acquisition functions with synthetic transport, never a forecast."""
import ast
import copy
from datetime import datetime,timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import types
import unittest
from typing import Any,Mapping
from unittest.mock import patch

import predeadline_capture_window_v1 as window
import predeadline_content_retry_v1 as retry
import test_predeadline_content_retry_v1 as old
from critical_saved_publication_v1 import capture_boundary_delay

DAY='2026-11-14'
NOW=datetime.fromisoformat(DAY+'T12:00:00+09:00')
KEY='20261114-20-06'
DEADLINE=NOW+timedelta(minutes=10)


def task(kind='BEFOREINFO'):
    return {'SOURCE_ID':kind.lower()+'-'+KEY,'RACE_KEY':KEY,'KIND':kind,
        'DUE_AT_JST':(DEADLINE-timedelta(minutes=7)).isoformat(),
        'MUST_FINISH_BEFORE_JST':DEADLINE.isoformat(),
        'URL':'https://www.boatrace.jp/fixture'}


class EarlyAcquisitionTests(unittest.TestCase):
    def setUp(self):
        fixture=old.RetryTests(methodName='runTest');fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.now=NOW;fixture.plan=[task(k) for k in ('BEFOREINFO','ODDS3T')]
        self.f=fixture;self.plan=copy.deepcopy(fixture.plan)

    def collect(self):
        return retry.collect_with_retry(self.f.original,self.f.runner,
            max_requests=8,early_wait_seconds=0,early_capture=True)

    def complete(self):
        self.f.responses={t['SOURCE_ID']:['complete'] for t in self.f.plan}

    def test_no_request_before_ten_minutes_then_real_timestamp_is_preserved(self):
        self.complete();self.f.now=NOW-timedelta(seconds=1)
        self.assertEqual([],self.collect()['ACQUIRED']);self.assertFalse(self.f.attempts)
        self.f.now=NOW
        self.assertEqual(2,len(self.collect()['ACQUIRED']))
        self.assertTrue(all(at==NOW for _,at in self.f.attempts+self.f.writes))
        self.assertEqual(self.plan,self.f.plan)

    def test_unpublished_pair_retries_after_existing_pause_without_backdating(self):
        result=self.collect()
        self.assertEqual(2,len(result['ACQUIRED']))
        self.assertEqual([30],self.f.sleeps);self.assertEqual(4,len(self.f.attempts))
        self.assertTrue(all(at==NOW+timedelta(seconds=30) for _,at in self.f.writes))
        self.assertEqual(self.plan,self.f.plan)

    def test_partial_pair_preserves_saved_source_and_only_refetches_missing_member(self):
        before,odds=(t['SOURCE_ID'] for t in self.f.plan)
        self.f.responses[before]=['complete']
        self.assertEqual([before],self.collect()['ACQUIRED'])
        path=self.f.root/'snapshots/predeadline'/f'{before}.json'
        raw=path.read_bytes();self.assertEqual([],self.f.sleeps)
        self.f.now+=timedelta(seconds=40)
        self.assertEqual([odds],self.collect()['ACQUIRED'])
        self.assertEqual(raw,path.read_bytes())
        self.assertEqual(1,sum(s==before for s,_ in self.f.attempts))

    def test_permanently_unpublished_values_are_never_saved_or_filled(self):
        self.f.responses={t['SOURCE_ID']:['reject'] for t in self.f.plan}
        self.assertEqual([],self.collect()['ACQUIRED'])
        self.assertEqual(4,len(self.f.attempts));self.assertFalse(self.f.writes)

    def test_existing_http_failure_policy_is_retained(self):
        for error in ('HTTP_403','HTTP_429','HTTP_5XX','TIMEOUT'):
            with self.subTest(error=error):
                self.f.responses={t['SOURCE_ID']:[old.PipelineError(error)] for t in self.f.plan}
                self.f.attempts.clear();self.f.counts.clear();self.f.sleeps.clear()
                result=self.collect()
                self.assertEqual([],result['ACQUIRED']);self.assertFalse(self.f.sleeps)

    def test_cutoff_blocks_even_a_complete_response(self):
        self.complete();self.f.now=DEADLINE
        self.assertEqual([],self.collect()['ACQUIRED']);self.assertFalse(self.f.attempts)
        self.f.now=NOW
        original=self.f.runner.client.fetch_validated_once
        def late(*args):
            self.f.now=DEADLINE
            return original(*args)
        self.f.runner.client.fetch_validated_once=late
        with self.assertRaisesRegex(old.PipelineError,'PREDEADLINE_CAPTURE_LATE'):self.collect()
        self.assertFalse(self.f.writes)

    def test_existing_authority_boundary_is_rechecked_for_both_attempts(self):
        self.f.install_actual_boundary();calls=[]
        daily=types.SimpleNamespace(verify_runtime=lambda *a,**k:calls.append('runtime'),
            verify_locks=lambda *a,**k:calls.append(a[2]))
        with patch.dict('sys.modules',{'g11.relay':types.SimpleNamespace(daily_runtime_authority_v1=daily)}):
            self.assertEqual(2,len(self.collect()['ACQUIRED']))
        self.assertEqual(['runtime','MORNING','PREDEADLINE']*2,calls)

    def test_actual_nowait_wrapper_and_context_restore_unchanged_bindings(self):
        self.complete();module=types.ModuleType('g11.canonical.official_pipeline')
        module.CanonicalRunner=self.f.Runner
        self.f.Runner._g11_ten_minute_odds_window=True
        original=self.f.Runner.run_due_predeadline
        with patch.dict('sys.modules',{'g11.canonical.official_pipeline':module}):
            with retry.bounded_content_retry(early_capture=True):
                result=self.f.runner.run_due_predeadline(max_requests=8,early_wait_seconds=0)
        self.assertEqual(2,len(result['ACQUIRED']))
        self.assertIs(original,self.f.Runner.run_due_predeadline)

    def test_only_due_assignment_changes_in_the_reviewed_first_attempt(self):
        import builtins,textwrap,inspect
        source=[];compiler=builtins.compile
        def record(value,*args,**kwargs):
            if isinstance(value,str):source.append(value)
            return compiler(value,*args,**kwargs)
        with patch('builtins.compile',record):
            retry._restricted_collector(self.f.original,None,None,early_capture=True)
        tree=ast.parse(source[-1]);fn=tree.body[0]
        class RemoveDue(ast.NodeTransformer):
            def visit_Assign(self,n):
                if isinstance(n.value,ast.Call) and isinstance(n.value.func,ast.Name) and n.value.func.id=='_capture_due':
                    return None
                return n
        transformed=RemoveDue().visit(tree)
        original=ast.parse(textwrap.dedent(inspect.getsource(self.f.original)))
        self.assertEqual(ast.dump(original),ast.dump(transformed))


class ScheduleTests(unittest.TestCase):
    def test_alignment_uses_ten_minute_boundary_from_unchanged_seven_minute_plan(self):
        self.assertEqual(30,capture_boundary_delay([task()],set(),NOW-timedelta(seconds=30)))
        self.assertEqual(0,capture_boundary_delay([task()],set(),NOW))
        self.assertEqual(DEADLINE-timedelta(minutes=7),datetime.fromisoformat(task()['DUE_AT_JST']))

    def test_legacy_day_keeps_seven_minute_start_and_other_windows_fail(self):
        due=datetime.fromisoformat('2026-11-13T12:03:00+09:00');end=due+timedelta(minutes=7)
        self.assertEqual(due,window.effective_due(due,end))
        for delta in (6,8,10):
            with self.assertRaises(ValueError):window.effective_due(DEADLINE-timedelta(minutes=delta),DEADLINE)


class VerifiedMarketTests(unittest.TestCase):
    def setUp(self):
        root=Path(os.environ.get('G11_RETRY_SOURCE_ROOT',Path(__file__).resolve().parents[1]/'.g11-private'))
        path=root/'g11/canonical/official_pipeline.py';code=ast.parse(path.read_text())
        cls=next(n for n in code.body if isinstance(n,ast.ClassDef) and n.name=='CanonicalRunner')
        method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='_validated_odds_only_market')
        code.body=[ast.ClassDef(name='Runner',bases=[],keywords=[],body=[method],decorator_list=[])];ast.fix_missing_locations(code)
        self.rows=[{'COMBINATION':f'{a}-{b}-{c}','NORMALIZED_TEXT':'10.0','MARKET_ODDS':10.0}
            for a in range(1,7) for b in range(1,7) for c in range(1,7) if len({a,b,c})==3]
        ns={'Any':Any,'Mapping':Mapping,'re':re,'timedelta':timedelta,
            '_require':old.require,'_parse_jst':datetime.fromisoformat,'RECEIPT_SCHEMA':'RECEIPT',
            'parse_odds3t_html':lambda data:copy.deepcopy(self.rows)}
        exec(compile(code,str(path),'exec'),ns);self.Runner=ns['Runner'];self.runner=self.Runner()
        self.runner.store=types.SimpleNamespace(response_bytes=lambda source:b'official raw fixture')
        self.official=types.SimpleNamespace(CanonicalRunner=self.Runner)
        self.url='https://www.boatrace.jp/owpc/pc/race/odds3t?hd=20261114&jcd=20&rno=6'
        self.morning={'SNAPSHOT':{'PAYLOAD':{'PREDEADLINE_PLAN':[{**task('ODDS3T'),'URL':self.url}]}}}
        receipt={'SCHEMA':'RECEIPT','SOURCE_ID':'odds3t-'+KEY,'HOST':'www.boatrace.jp',
            'URL':self.url,'FINAL_URL':self.url,'STATUS':'HTTP_200_BYTE_EXACT_READBACK','CAPTURED_AT_JST':NOW.isoformat()}
        self.odds={'SNAPSHOT':{'STAGE':'PREDEADLINE','NAME':'odds3t-'+KEY,'SOURCE_RECEIPTS':[receipt],
            'PAYLOAD':{'STATUS':'PREDEADLINE_SOURCE_COMPLETE','RACE_KEY':KEY,'KIND':'ODDS3T',
                'CAPTURED_AT_JST':NOW.isoformat(),'PARSED':{'ODDS_ROWS':copy.deepcopy(self.rows)}}}}

    def market(self):return self.runner._validated_odds_only_market(self.morning,self.odds,KEY)

    def set_time(self,at):
        self.odds['SNAPSHOT']['SOURCE_RECEIPTS'][0]['CAPTURED_AT_JST']=at.isoformat()
        self.odds['SNAPSHOT']['PAYLOAD']['CAPTURED_AT_JST']=at.isoformat()

    def test_ten_minute_receipt_passes_exact_original_raw_market_checks(self):
        before=copy.deepcopy((self.morning,self.odds));original=self.Runner._validated_odds_only_market
        with self.assertRaisesRegex(old.PipelineError,'ODDS_ONLY_CAPTURE_WINDOW'):self.market()
        with window.verified_odds_window(self.official) as admitted:
            self.assertTrue(admitted);self.assertEqual(120,len(self.market()))
        self.assertEqual(before,(self.morning,self.odds))
        self.assertIs(original,self.Runner._validated_odds_only_market)

    def test_earlier_than_ten_minutes_or_at_cutoff_is_rejected(self):
        with window.verified_odds_window(self.official):
            for at in (NOW-timedelta(seconds=1),DEADLINE):
                self.set_time(at)
                with self.assertRaisesRegex(old.PipelineError,'ODDS_ONLY_CAPTURE_WINDOW'):self.market()

    def test_wrong_url_or_changed_raw_values_still_fail(self):
        with window.verified_odds_window(self.official):
            self.odds['SNAPSHOT']['SOURCE_RECEIPTS'][0]['FINAL_URL']=self.url+'&wrong=1'
            with self.assertRaisesRegex(old.PipelineError,'SOURCE_BINDING'):self.market()
            self.odds['SNAPSHOT']['SOURCE_RECEIPTS'][0]['FINAL_URL']=self.url
            self.odds['SNAPSHOT']['PAYLOAD']['PARSED']['ODDS_ROWS'][0]['MARKET_ODDS']=99.0
            with self.assertRaisesRegex(old.PipelineError,'RAW_REPARSE_BINDING'):self.market()

    def test_window_restores_after_error_and_unknown_source_is_not_patched(self):
        original=self.Runner._validated_odds_only_market
        with self.assertRaisesRegex(RuntimeError,'fixture'):
            with window.verified_odds_window(self.official):raise RuntimeError('fixture')
        self.assertIs(original,self.Runner._validated_odds_only_market)
        with patch.object(window,'ODDS_WINDOW_SHA','0'*64):
            with window.verified_odds_window(self.official) as admitted:self.assertFalse(admitted)
        self.assertIs(original,self.Runner._validated_odds_only_market)


if __name__=='__main__':unittest.main()
