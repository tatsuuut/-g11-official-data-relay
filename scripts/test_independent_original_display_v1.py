"""Exercise the actual reviewed original-display acquisition without network."""
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import independent_original_display_v1 as adapter


class OriginalDisplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root=Path(os.environ.get('G11_EXHIBITION_CANDIDATE_DIR',
            Path(__file__).resolve().parents[1]/'.g11-exhibition-candidate')).resolve()
        if not (root/'g11/canonical/shared_original_display_v1.py').is_file():
            if os.environ.get('G11_REQUIRE_INDEPENDENT_ORIGINAL_TESTS')=='1':
                raise AssertionError('exact candidate input checkout required')
            raise unittest.SkipTest('exact candidate input checkout required')
        cls.candidate=root
        sys.path.insert(0,str(root))
        cls.addClassCleanup(sys.path.remove,str(root))
        canonical=importlib.import_module('g11.canonical')
        old=list(canonical.__path__)
        canonical.__path__[:]=[str(root/'g11/canonical'),*old]
        cls.addClassCleanup(setattr,canonical,'__path__',old)
        spec=importlib.util.spec_from_file_location('g11.canonical._independent_original_fixture',
            root/'g11/canonical/shared_original_display_v1.py')
 
        cls.shared=importlib.util.module_from_spec(spec); spec.loader.exec_module(cls.shared)
        cls.preview=cls.shared.preview
        cls.official=importlib.import_module('g11.canonical.official_pipeline')

    def setUp(self):
        from test_independent_fallback_v1 import fixture
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.key='20261127-20-12'; self.day='2026-11-27'
        self.deadline=datetime(2026,11,27,20,45,tzinfo=timezone(timedelta(hours=9)))
        self.now=self.deadline-timedelta(minutes=4)
        self.store,self.state,self.events,self.morning,_=fixture()
        self.store.root=self.root
        self.morning['PREDEADLINE_PLAN'].append({'SOURCE_ID':'beforeinfo-'+self.key,
            'RACE_KEY':self.key,'KIND':'BEFOREINFO','MUST_FINISH_BEFORE_JST':self.deadline.isoformat()})
        raw=(self.candidate/'g11/canonical/fixtures/fun_original_20261126_20_12.html').read_bytes()
        self.raw=raw.replace(b'2026-11-26',b'2026-11-27').replace(b'20261126',b'20261127').replace(b'2026/11/26',b'2026/11/27')
        parsed=self.shared.fun.FUN.parse(self.raw,self.key,self.deadline,self.now)
        self.roster={row['BOAT']:(9000+row['BOAT'],row['BOAT'],row['RACER_NAME']) for row in parsed['RACERS']}
        for name in ('ledger/manifest.json','snapshots/morning/official-day-input.json'):
            path=self.root/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_text('{}')
        self.ledger_bytes=(self.root/'ledger/manifest.json').read_bytes()
        self.stack=ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict('sys.modules',{
            'g11.canonical.shared_original_display_v1':self.shared}))
        self.stack.enter_context(patch.object(self.official,'OfficialArtifactStore',return_value=self.store))
        self.stack.enter_context(patch.object(self.official,'DailyRequestLedger',return_value=self.store.ledger))
        self.stack.enter_context(patch.object(self.preview,'race_roster',return_value=self.roster))
        self.packet=importlib.import_module('g11.canonical.boatcast_preview_v1')
        self.priority=self.stack.enter_context(patch.object(self.packet,'odds_priority',return_value={'STATUS':'ODDS_PRIORITY_CLEAR'}))
        self.critical=SimpleNamespace(_tree_sha=lambda root:'original tree')
        self.stack.enter_context(patch.dict('sys.modules',{'g11.relay.predeadline_critical_v1':self.critical}))
        self.calls=[]

    def response(self,request,**kwargs):
        self.assertTrue(self.store.ledger.state()['STOPPED'])
        url=request.full_url; self.calls.append(url)
        if url!=self.shared.fun.FUN.url_for(self.key):
            raise HTTPError(url,403,'fixture provider hold',{'Retry-After':'3600'},None)
        value=io.BytesIO(self.raw); value.status=200; value.geturl=lambda:url; value.headers={}
        return value

    def test_actual_native_acquire_reaches_fun_after_two_independent_refusals_and_keeps_stop(self):
        original=self.shared.acquire; blocked=self.shared._blocked; before=deepcopy((self.state,self.events))
        with adapter.context(self.day):
            self.assertIs(self.shared._blocked,blocked)
            result=self.shared.acquire(self.root,self.key,self.deadline,clock=lambda:self.now,opener=self.response)
            self.assertEqual(result['PROVIDER'],'BOATRACE_FUN_ORIGINAL')
            self.assertEqual(self.calls,[p.url_for(self.key) for p in self.shared.providers_for(self.key)]) 
            self.assertEqual(self.shared.load(self.root,self.key,self.deadline,asof=self.now),result)
        self.assertIs(self.shared.acquire,original); self.assertIs(self.shared._blocked,blocked)
        self.assertEqual((self.state,self.events),before)
        self.assertEqual((self.root/'ledger/manifest.json').read_bytes(),self.ledger_bytes)

    def test_unknown_incident_and_race_unavailability_keep_native_block(self):
        with adapter.context(self.day):
            self.state['REQUEST_POLICY']='legacy'
            self.assertEqual(self.shared.acquire(self.root,self.key,self.deadline,clock=lambda:self.now,opener=self.response)['STATUS'],'PIPELINE_STOPPED')
            self.state['REQUEST_POLICY']='G11_LIVE_REFUSAL_RECOVERY_V1'
            path=self.root/'snapshots/predeadline'/('availability-'+self.key+'.json')
            path.parent.mkdir(parents=True,exist_ok=True); path.write_text('{}')
            self.assertEqual(self.shared.acquire(self.root,self.key,self.deadline,clock=lambda:self.now,opener=self.response)['STATUS'],'RACE_UNAVAILABLE')
        self.assertEqual(self.calls,[])

    def test_odds_priority_defers_before_any_provider_hold_but_saved_proof_remains_reusable(self):
        with adapter.context(self.day):
            self.priority.return_value={'STATUS':'DEFERRED_ODDS_PRIORITY'}
            result=self.shared.acquire(self.root,self.key,self.deadline,clock=lambda:self.now,opener=self.response)
            self.assertEqual(result['STATUS'],'DEFERRED_ODDS_PRIORITY')
            self.assertFalse((self.root/'g11/published-preview-state').exists())
            self.assertEqual(self.calls,[])
            self.priority.return_value={'STATUS':'ODDS_PRIORITY_CLEAR'}
            proof=self.shared.acquire(self.root,self.key,self.deadline,clock=lambda:self.now,opener=self.response)
            self.priority.return_value={'STATUS':'DEFERRED_ODDS_PRIORITY'}
            self.assertEqual(self.shared.acquire(self.root,self.key,self.deadline,clock=lambda:self.now,
                opener=lambda *_a,**_k:self.fail('saved proof must not request')),proof)

    def strict(self,explicit):
        with patch.object(self.shared,'urlopen',side_effect=self.response) as reader, \
                patch.object(self.shared,'_read_bounded',return_value=self.raw) as bounded:
            with adapter.context(self.day):
                kwargs={'opener':reader} if explicit else {}
                result=self.shared.acquire(self.root,self.key,self.deadline,clock=lambda:self.now,**kwargs)
        self.assertEqual(result['STATUS'],'CAPTURED')
        self.assertTrue(bounded.call_args.kwargs['strict'])

    def test_default_native_reader_retains_strict_socket_bounds(self): self.strict(False)
    def test_explicit_cached_native_reader_retains_strict_socket_bounds(self): self.strict(True)

    def test_disallowed_host_is_rejected_before_reader_or_provider_state(self):
        provider=SimpleNamespace(NAME='forbidden',HOST='www.boatrace.jp',url_for=lambda key:'https://www.boatrace.jp/owpc/pc/race/beforeinfo')
        with patch.object(self.shared,'providers_for',return_value=[provider]), adapter.context(self.day):
            with self.assertRaisesRegex(ValueError,'PROVIDER_ALLOWLIST'):
                self.shared.acquire(self.root,self.key,self.deadline,clock=lambda:self.now,opener=self.response)
        self.assertEqual(self.calls,[])

    def test_production_summary_never_prints_original_bytes_or_unaccepted_provider(self):
        output=io.StringIO()
        with redirect_stdout(output):
            adapter.summary(self.key,{'STATUS':'CAPTURED','PROVIDER':'BOATRACE_FUN_ORIGINAL',
                'HOST':'boatrace-fun.net','SHA256':'a'*64,'CAPTURED_AT':self.now.isoformat(),
                'BYTES':'private-original-bytes','MODEL':'private-model','RACER_NAME':'private-name'})
            adapter.summary(self.key,{'STATUS':'DEFERRED_ODDS_PRIORITY','HOST':'unaccepted.invalid'})
        records=[json.loads(line.split(' ',1)[1]) for line in output.getvalue().splitlines()]
        self.assertEqual(set(records[0]),{'race_key','status','provider','host','sha256','captured_at'})
        self.assertEqual(set(records[1]),{'race_key','status'})
        self.assertNotIn('private-',output.getvalue())

    def test_unreviewed_interface_is_left_unchanged(self):
        with patch.object(self.shared,'acquire',return_value={'STATUS':'native'}) as native:
            with adapter.context(self.day):
                self.assertIs(self.shared.acquire,native)
            self.assertIs(self.shared.acquire,native)

    def test_exception_restores_acquire_and_checkpoint_tree_after_quieting(self):
        original,tree=self.shared.acquire,self.critical._tree_sha
        order=[]
        with patch('exhibition_input_compatibility_v1._quiet_original_futures',side_effect=lambda:order.append('quiet')):
            with self.assertRaises(KeyboardInterrupt):
                with adapter.context(self.day):
                    self.assertEqual(self.critical._tree_sha(self.root),'original tree')
                    self.shared.acquire(self.root,self.key,self.deadline,clock=lambda:self.now,
                        opener=lambda *_a,**_k:(_ for _ in ()).throw(KeyboardInterrupt()))
        self.assertIs(self.shared.acquire,original); self.assertIs(self.critical._tree_sha,tree)
        self.assertEqual(order,['quiet','quiet','quiet'])


if __name__=='__main__': unittest.main()
