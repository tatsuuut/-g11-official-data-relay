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


def actual_formal_binding():
    root=Path(os.environ.get('G11_RETRY_SOURCE_ROOT',Path(__file__).resolve().parents[1]/'.g11-private'))
    path=root/'p3/formal_invocation_adapter/v1/src/formal_p3_invocation_adapter_v1.py'
    code=ast.parse(path.read_text())
    names={'FormalP3InvocationError','_require','_jst','_bind_predeadline_plan'}
    code.body=[n for n in code.body if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name in names]
    module=types.ModuleType('actual_formal_invocation_binding')
    module.__dict__.update(Any=Any,Mapping=Mapping,re=re,datetime=datetime,timedelta=timedelta,
        FORMAL_GATE=types.SimpleNamespace(INDEX={'標準レースキー':0,'締切':1}))
    exec(compile(code,str(path),'exec'),module.__dict__)
    return module


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
        self.official=types.SimpleNamespace(CanonicalRunner=self.Runner,FORMAL_ADAPTER=actual_formal_binding())
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
        original_plan=self.official.FORMAL_ADAPTER._bind_predeadline_plan
        with self.assertRaisesRegex(RuntimeError,'fixture'):
            with window.verified_odds_window(self.official):raise RuntimeError('fixture')
        self.assertIs(original,self.Runner._validated_odds_only_market)
        self.assertIs(original_plan,self.official.FORMAL_ADAPTER._bind_predeadline_plan)
        with patch.object(window,'ODDS_WINDOW_SHA','0'*64):
            with window.verified_odds_window(self.official) as admitted:self.assertFalse(admitted)
        self.assertIs(original,self.Runner._validated_odds_only_market)
        self.assertIs(original_plan,self.official.FORMAL_ADAPTER._bind_predeadline_plan)
        self.assertFalse(getattr(self.Runner,'_g11_ten_minute_odds_window',False))


class FormalPlanTests(unittest.TestCase):
    def setUp(self):
        self.market=VerifiedMarketTests(methodName='runTest');self.market.setUp()
        self.official=self.market.official;self.invocation=self.official.FORMAL_ADAPTER
        self.bridge={'PREDEADLINE_PLAN':[task(k) for k in ('BEFOREINFO','ODDS3T')],
            'RAW_DAY':{'rows':[[KEY,DEADLINE.strftime('%H:%M')]]*6}}
        self.context={'BEFOREINFO_CAPTURED_AT_JST':NOW.isoformat(),'ODDS_CAPTURED_AT_JST':NOW.isoformat()}
        self.sources={t['KIND']:{'SNAPSHOT':{'SOURCE_RECEIPTS':[
            {'SOURCE_ID':t['SOURCE_ID'],'URL':t['URL']}]}} for t in self.bridge['PREDEADLINE_PLAN']}
        clock=patch.object(window,'datetime',wraps=datetime);self.clock=clock.start();self.addCleanup(clock.stop)
        self.clock.now.return_value=DEADLINE-timedelta(seconds=1)

    def bind(self):
        return self.invocation._bind_predeadline_plan(self.bridge,self.context,
            self.sources['BEFOREINFO'],self.sources['ODDS3T'],KEY)

    def test_actual_formal_gate_reproduces_t10_rejection_then_accepts_unchanged_evidence(self):
        before=copy.deepcopy((self.bridge,self.context,self.sources))
        with self.assertRaisesRegex(self.invocation.FormalP3InvocationError,'P07_PREDEADLINE_FRESHNESS_BINDING:BEFOREINFO'):
            self.bind()
        with window.verified_odds_window(self.official) as admitted:
            self.assertTrue(admitted)
            self.assertEqual(self.bridge['PREDEADLINE_PLAN'],self.bind()['TASKS'])
            self.assertEqual(120,len(self.market.market()))
            self.assertTrue(self.official.CanonicalRunner._g11_ten_minute_odds_window)
        self.assertEqual(before,(self.bridge,self.context,self.sources))

    def test_each_real_source_accepts_t10_t7_and_last_second_only(self):
        with window.verified_odds_window(self.official):
            for name in self.context:
                for at in (NOW,NOW+timedelta(minutes=3),DEADLINE-timedelta(seconds=1)):
                    self.context[name]=at.isoformat();self.bind()
                self.context[name]=NOW.isoformat()

    def test_both_sources_reject_old_other_day_future_and_cutoff_receipts(self):
        with window.verified_odds_window(self.official):
            for name in self.context:
                for at in (NOW-timedelta(seconds=1),NOW-timedelta(days=1),DEADLINE,DEADLINE+timedelta(seconds=1)):
                    with self.subTest(source=name,at=at):
                        self.context[name]=at.isoformat()
                        with self.assertRaisesRegex(self.invocation.FormalP3InvocationError,'P07_PREDEADLINE_FRESHNESS_BINDING'):
                            self.bind()
                self.context[name]=NOW.isoformat()
            self.clock.now.return_value=NOW
            for name in self.context:
                self.context[name]=(NOW+timedelta(seconds=1)).isoformat()
                with self.assertRaisesRegex(self.invocation.FormalP3InvocationError,'P07_PREDEADLINE_FRESHNESS_BINDING'):
                    self.bind()
                self.context[name]=NOW.isoformat()

    def test_original_seven_minute_plan_source_kind_and_url_checks_stay_active(self):
        with window.verified_odds_window(self.official):
            for field,value in (('SOURCE_ID','beforeinfo-wrong'),('URL','https://www.boatrace.jp/wrong'),
                    ('KIND','ODDS3T'),('DUE_AT_JST',NOW.isoformat())):
                saved=self.bridge['PREDEADLINE_PLAN'][0][field]
                self.bridge['PREDEADLINE_PLAN'][0][field]=value
                with self.assertRaises(self.invocation.FormalP3InvocationError):self.bind()
                self.bridge['PREDEADLINE_PLAN'][0][field]=saved

    def test_unknown_formal_source_keeps_both_gates_and_collector_at_seven_minutes(self):
        market=self.official.CanonicalRunner._validated_odds_only_market
        formal=self.invocation._bind_predeadline_plan
        with patch.object(window,'FORMAL_PLAN_SHA','0'*64):
            with window.verified_odds_window(self.official) as admitted:
                self.assertFalse(admitted)
                self.assertIs(market,self.official.CanonicalRunner._validated_odds_only_market)
                self.assertIs(formal,self.invocation._bind_predeadline_plan)
                self.assertFalse(getattr(self.official.CanonicalRunner,'_g11_ten_minute_odds_window',False))
                with self.assertRaises(self.invocation.FormalP3InvocationError):self.bind()
                with self.assertRaisesRegex(old.PipelineError,'ODDS_ONLY_CAPTURE_WINDOW'):self.market.market()


class AvailabilityReceiptTests(unittest.TestCase):
    def setUp(self):
        self.market=VerifiedMarketTests(methodName='runTest');self.market.setUp()
        self.official=self.market.official
        root=Path(os.environ.get('G11_RETRY_SOURCE_ROOT',Path(__file__).resolve().parents[1]/'.g11-private'))
        path=root/'g11/canonical/predeadline_availability_v1.py'
        if not path.is_file():self.skipTest('legacy frozen source predates official availability receipts')
        self.helper=types.ModuleType('actual_availability_receipt')
        exec(compile(path.read_text(),str(path),'exec'),self.helper.__dict__)
        self.helper._pipeline=lambda:types.SimpleNamespace(_require=old.require,_parse_jst=datetime.fromisoformat)
        self.official.PREDEADLINE_AVAILABILITY=self.helper
        self.task={**task('ODDS3T'),'URL':self.market.url}
        self.receipt={**self.market.odds['SNAPSHOT']['SOURCE_RECEIPTS'][0],'SHA256':'a'*64}
        self.withdrawal={'WITHDRAWN_BOATS':[3],'ACTIVE_BOATS':[1,2,4,5,6]}

    def payload(self):return self.helper._payload(self.task,self.receipt,self.withdrawal)

    def test_actual_t10_withdrawal_receipt_no_longer_stops_the_collector(self):
        before=copy.deepcopy((self.task,self.receipt,self.withdrawal));original=self.helper._payload
        with self.assertRaisesRegex(old.PipelineError,'AVAILABILITY_OFFICIAL_RECEIPT_BINDING'):self.payload()
        with window.verified_odds_window(self.official) as admitted:
            self.assertTrue(admitted)
            result=self.payload()
            self.assertEqual(NOW.isoformat(),result['CAPTURED_AT_JST'])
            self.assertEqual([3],result['WITHDRAWN_BOATS'])
            self.assertFalse(result['MODEL_INFERENCE_PERFORMED'])
        self.assertIs(original,self.helper._payload)
        self.assertEqual(before,(self.task,self.receipt,self.withdrawal))

    def test_early_cutoff_future_and_unbound_receipts_remain_rejected(self):
        with window.verified_odds_window(self.official):
            for at in (NOW-timedelta(seconds=1),DEADLINE,NOW-timedelta(days=1)):
                self.receipt['CAPTURED_AT_JST']=at.isoformat()
                with self.assertRaisesRegex(old.PipelineError,'AVAILABILITY_OFFICIAL_RECEIPT_BINDING'):self.payload()
            self.receipt['CAPTURED_AT_JST']=NOW.isoformat()
            for key,value in (('SOURCE_ID','odds3t-wrong'),('URL',self.market.url+'&wrong=1'),
                    ('FINAL_URL','https://example.invalid/'),('SHA256','not-a-sha')):
                original=self.receipt[key];self.receipt[key]=value
                with self.assertRaisesRegex(old.PipelineError,'AVAILABILITY_OFFICIAL_RECEIPT_BINDING'):self.payload()
                self.receipt[key]=original
            with patch.object(window,'datetime',wraps=datetime) as clock:
                clock.now.return_value=NOW
                self.receipt['CAPTURED_AT_JST']=(NOW+timedelta(seconds=1)).isoformat()
                with self.assertRaisesRegex(old.PipelineError,'AVAILABILITY_OFFICIAL_RECEIPT_BINDING'):self.payload()

    def test_unknown_availability_source_keeps_all_three_original_gates(self):
        original=self.helper._payload
        market=self.official.CanonicalRunner._validated_odds_only_market
        formal=self.official.FORMAL_ADAPTER._bind_predeadline_plan
        with patch.object(window,'AVAILABILITY_WINDOW_SHA','0'*64):
            with window.verified_odds_window(self.official) as admitted:
                self.assertFalse(admitted)
                self.assertIs(original,self.helper._payload)
                self.assertIs(market,self.official.CanonicalRunner._validated_odds_only_market)
                self.assertIs(formal,self.official.FORMAL_ADAPTER._bind_predeadline_plan)
                self.assertFalse(getattr(self.official.CanonicalRunner,'_g11_ten_minute_odds_window',False))
                with self.assertRaisesRegex(old.PipelineError,'AVAILABILITY_OFFICIAL_RECEIPT_BINDING'):self.payload()

    def test_saved_t10_withdrawal_reopens_during_night_without_rewriting_evidence(self):
        import tempfile
        raw=b'synthetic official withdrawal response'
        self.receipt['SHA256']=hashlib.sha256(raw).hexdigest()
        morning={'SNAPSHOT':{'PAYLOAD':{'PREDEADLINE_PLAN':[self.task]}}}
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);writes=[]
            def load(stage,name):
                if stage=='morning':return morning
                data=(root/'snapshots'/stage/(name+'.json')).read_bytes()
                return {'SNAPSHOT':json.loads(data),'SNAPSHOT_SHA256':hashlib.sha256(data).hexdigest()}
            def commit(stage,name,payload,receipts):
                path=root/'snapshots'/stage/(name+'.json');path.parent.mkdir(parents=True,exist_ok=True)
                with path.open('x') as output:
                    json.dump({'STAGE':'PREDEADLINE','NAME':name,'PAYLOAD':payload,'SOURCE_RECEIPTS':receipts},output)
                writes.append(path)
            store=types.SimpleNamespace(root=root,response_bytes=lambda sid:raw,
                commit_snapshot=commit,load_verified_snapshot=load)
            with patch.object(self.helper,'parse_official_withdrawal',return_value=self.withdrawal), \
                    patch.object(window,'datetime',wraps=datetime) as clock:
                clock.now.return_value=NOW+timedelta(seconds=1)
                with window.verified_odds_window(self.official):
                    saved=self.helper.save(store,self.task,self.receipt,self.withdrawal,NOW+timedelta(seconds=1))
                original_bytes=writes[0].read_bytes()
                clock.now.return_value=DEADLINE+timedelta(hours=1)
                with window.verified_odds_window(self.official):
                    self.assertEqual(saved,self.helper.load_verified(store,KEY))
                self.assertEqual(original_bytes,writes[0].read_bytes())
                self.assertEqual(1,len(writes))


    def test_saved_publication_reopens_t10_availability_without_regenerating_or_rewriting(self):
        import tempfile
        from unittest.mock import Mock
        import critical_saved_publication_v1 as early
        raw=b'synthetic retained official withdrawal bytes'
        self.receipt['SHA256']=hashlib.sha256(raw).hexdigest()
        morning={'SNAPSHOT':{'PAYLOAD':{'PREDEADLINE_PLAN':[self.task]}}}
        with tempfile.TemporaryDirectory() as d:
            state=Path(d);root=state/'store';writes=[]
            def load(stage,name):
                if stage=='morning':return morning
                data=(root/'snapshots'/stage/(name+'.json')).read_bytes()
                return {'SNAPSHOT':json.loads(data),'SNAPSHOT_SHA256':hashlib.sha256(data).hexdigest()}
            def commit(stage,name,payload,receipts):
                path=root/'snapshots'/stage/(name+'.json');path.parent.mkdir(parents=True,exist_ok=True)
                with path.open('x') as output:
                    json.dump({'STAGE':'PREDEADLINE','NAME':name,'PAYLOAD':payload,'SOURCE_RECEIPTS':receipts},output)
                writes.append(path)
            store=types.SimpleNamespace(root=root,response_bytes=lambda sid:raw,
                commit_snapshot=commit,load_verified_snapshot=load)
            authority={'authority_sha256':'a'*64,'runtime_source_sha':'b'*40}
            (state/'relay-state.json').write_text(json.dumps({'OPERATIONAL_DATE_JST':DAY,'CRITICAL':{
                'OPERATIONAL_DATE':DAY,'CRITICAL_CHECKPOINT_STATUS':'PASS','LAST_CRITICAL_RUN_ID':'123',
                'AUTHORITY_SHA256':'a'*64,'RUNTIME_SOURCE_SHA':'b'*40,
                'LAST_TIMELY_P3_LIVE_COUNT':1,'LAST_TIMELY_V53_COUNT':1}}))
            runtime=types.SimpleNamespace(verify_runtime=Mock(return_value=authority),verify_locks=Mock(),
                feed_binding=Mock(return_value=authority))
            publisher=types.SimpleNamespace(load_locked_publication=Mock(return_value={'READ_ONLY':True}))
            saved_live={'practical_bets':['1-2-3'],'captured_at_jst':NOW.isoformat(),'snapshot_sha256':'c'*64}
            def build(*args,**kwargs):
                availability=self.helper.public_by_race(store,[KEY])
                return {'stage':'PREDEADLINE','status':'PASS','operational_date_jst':DAY,
                    'races':[{'key':KEY,'predeadline':saved_live,'predeadline_availability':availability[KEY]}]}
            relay=types.SimpleNamespace(_morning_projection=Mock(return_value={}),build_feed=Mock(side_effect=build),
                predeadline_availability=types.SimpleNamespace(_pipeline=lambda:self.official))
            with patch.object(self.helper,'parse_official_withdrawal',return_value=self.withdrawal), \
                    patch.object(window,'datetime',wraps=datetime) as clock:
                clock.now.return_value=NOW+timedelta(seconds=1)
                with window.verified_odds_window(self.official):
                    self.helper.save(store,self.task,self.receipt,self.withdrawal,NOW+timedelta(seconds=1))
                original_bytes=writes[0].read_bytes();original_gate=self.helper._payload
                with self.assertRaisesRegex(old.PipelineError,'AVAILABILITY_OFFICIAL_RECEIPT_BINDING'):
                    self.helper.public_by_race(store,[KEY])
                clock.now.return_value=DEADLINE+timedelta(hours=1)
                with patch.object(store,'commit_snapshot',side_effect=AssertionError('publication is read only')):
                    for attempt in range(2):
                        self.assertTrue(early.project_saved(state,DAY,'123',state/'frozen',state/'feed.json',
                            authority_module=runtime,relay=relay,publisher=publisher,now=clock.now.return_value))
                        feed=json.loads((state/'feed.json').read_text())
                        self.assertEqual(saved_live,feed['races'][0]['predeadline'])
                        self.assertEqual([3],feed['races'][0]['predeadline_availability']['withdrawn_boats'])
                self.assertEqual(original_bytes,writes[0].read_bytes());self.assertEqual(1,len(writes))
                self.assertIs(original_gate,self.helper._payload)
                with patch.object(self.helper,'parse_official_withdrawal',return_value=None):
                    with self.assertRaisesRegex(old.PipelineError,'AVAILABILITY_WITHDRAWAL_EVIDENCE_MISSING'):
                        early.project_saved(state,DAY,'123',state/'frozen',state/'feed.json',
                            authority_module=runtime,relay=relay,publisher=publisher,now=clock.now.return_value)
                self.assertIs(original_gate,self.helper._payload)


if __name__=='__main__':unittest.main()
