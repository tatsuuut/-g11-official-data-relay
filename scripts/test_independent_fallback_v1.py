"""Scope evidence and bounded market-to-exhibition handoff contracts."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import ast
import importlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import independent_fallback_v1 as adapter

JST = timezone(timedelta(hours=9))
DAY = '2026-11-27'
KEY = '20261127-24-02'


def fixture():
    now = datetime(2026,11,27,23,54,tzinfo=JST)
    url = 'https://www.boatrace.jp/owpc/pc/race/beforeinfo?hd=20261127&jcd=24&rno=2'
    task = {'SOURCE_ID':'beforeinfo-'+KEY,'KIND':'BEFOREINFO','RACE_KEY':KEY,'URL':url,
            'MUST_FINISH_BEFORE_JST':(now+timedelta(minutes=5)).isoformat()}
    events = []
    for rid, stamp, outcome, status, sid in [('recovered','12:00','HTTP_200',200,'odds3t-'+KEY),
                                            ('stopped','12:01','HTTP_403',403,task['SOURCE_ID'])]:
        at = DAY+'T'+stamp+':00+09:00'
        events += [{'TYPE':'REQUEST_STARTED','AT_JST':at,'PAYLOAD':{'REQUEST_ID':rid,'SOURCE_ID':sid,
                    'HOST':adapter.NATIONAL,'URL_SHA256':sha256(url.encode()).hexdigest()}},
                   {'TYPE':'OUTCOME_RECORDED','AT_JST':at,'PAYLOAD':{'REQUEST_ID':rid,
                    'OUTCOME':outcome,'HTTP_STATUS':status}}]
    state = {'REQUEST_POLICY':'G11_LIVE_REFUSAL_RECOVERY_V1','STOPPED':True,'ACTIVE_REQUEST':None,
             'STOPPED_AT_JST':DAY+'T12:01:00+09:00','REFUSAL_RECOVERY':{'ATTEMPT_COUNT':1,
             'STATUS':'EXHAUSTED','REQUEST_ID':'recovered'}}
    morning = {'OPERATIONAL_DATE_JST':DAY,'PREDEADLINE_PLAN':[task]}
    store = NS(ledger=NS(state=lambda:deepcopy(state),events=lambda:deepcopy(events)),
               load_verified_snapshot=lambda *_:{'SNAPSHOT':{'PAYLOAD':morning}})
    return store,state,events,morning,now


class ScopeTests(unittest.TestCase):
    def test_actual_incident_is_scoped_without_state_mutation(self):
        store,state,events,morning,now = fixture()
        before = deepcopy((state,events,morning))
        proof = adapter.stop_scope(store,DAY,now)
        self.assertTrue(proof['native_stopped'])
        self.assertEqual(proof['market_attempt_limit'],1)
        self.assertEqual((state,events,morning),before)

    def test_wrong_or_unknown_incident_is_closed(self):
        cases = [lambda s,e,m:s.update(STOPPED=False),
                 lambda s,e,m:s.update(REQUEST_POLICY='legacy'),
                 lambda s,e,m:s.update(ACTIVE_REQUEST={'REQUEST_ID':'busy'}),
                 lambda s,e,m:s['REFUSAL_RECOVERY'].update(ATTEMPT_COUNT=True),
                 lambda s,e,m:s['REFUSAL_RECOVERY'].update(ATTEMPT_COUNT=0),
                 lambda s,e,m:s['REFUSAL_RECOVERY'].update(STATUS='COOLDOWN'),
                 lambda s,e,m:e[-1]['PAYLOAD'].update(OUTCOME='HTTP_429',HTTP_STATUS=429),
                 lambda s,e,m:e[-2]['PAYLOAD'].update(HOST='other.example'),
                 lambda s,e,m:e[-2]['PAYLOAD'].update(URL_SHA256='0'*64),
                 lambda s,e,m:e[1]['PAYLOAD'].update(OUTCOME='HTTP_5XX',HTTP_STATUS=503),
                 lambda s,e,m:e[-1]['PAYLOAD'].update(REQUEST_ID='mismatch'),
                 lambda s,e,m:m.update(OPERATIONAL_DATE_JST='2026-11-26'),
                 lambda s,e,m:m['PREDEADLINE_PLAN'].append(deepcopy(m['PREDEADLINE_PLAN'][0])),
                 lambda s,e,m:m['PREDEADLINE_PLAN'][0].update(KIND='ODDS3T')]
        for mutate in cases:
            with self.subTest(case=cases.index(mutate)):
                store,state,events,morning,now=fixture();mutate(state,events,morning)
                with self.assertRaises((ValueError,KeyError)):
                    adapter.stop_scope(store,DAY,now)

    def test_prior_or_other_day_and_non_jst_closed(self):
        for day,now in [('2026-11-26',datetime(2026,11,26,23,54,tzinfo=JST)),
                        ('2026-11-28',datetime(2026,11,28,23,54,tzinfo=JST)),
                        (DAY,datetime(2026,11,27,14,54,tzinfo=timezone.utc))]:
            store,*_=fixture()
            with self.assertRaises(ValueError):adapter.stop_scope(store,day,now)

    def test_pre_effective_does_not_import_private_sources(self):
        with patch.object(importlib,'import_module',side_effect=AssertionError('private import')):
            with adapter.context('2026-11-26'):pass

    def test_missing_module_keeps_primary_operational(self):
        with patch.object(importlib,'import_module',side_effect=ImportError('not installed')):
            with adapter.context(DAY):pass


class TransformTests(unittest.TestCase):
    def test_unknown_future_capture_shape_is_noop(self):
        with patch.object(adapter.importlib,'import_module',return_value=NS()):
            with adapter.context(DAY):
                pass

    def test_unknown_future_reader_returns_native_without_mutating_any_reader(self):
        native = lambda *_: {'LIVE_SOURCE_FAILOVER': {'STATUS':'GLOBAL_STOPPED'}}
        modules = {short:NS(**{name:(native if short=='live_source_failover_v1' else lambda:None)})
                   for short,(name,_,_) in adapter.FUNCTIONS.items()}
        modules['venue_market_v1'].PROVIDERS = {adapter.BOATCAST:None,'www.omurakyotei.jp':None}
        def imported(name):
            return modules.get(name.rsplit('.',1)[-1],NS(read=lambda:None))
        runner = NS(store=NS(),client=NS(clock=lambda:None))
        with patch.object(adapter.importlib,'import_module',side_effect=imported), \
                patch.object(adapter,'stop_scope',return_value={'day':DAY}):
            with adapter.context(DAY):
                result = modules['live_source_failover_v1'].capture(runner,{})
            self.assertIs(modules['live_source_failover_v1'].capture,native)
        self.assertEqual(result,native())
        self.assertFalse(hasattr(modules['published_preview_v1'],'_read'))

    def test_only_stopped_subscripts_and_reviewed_boatcast_import_change(self):
        tree=ast.parse("def f():\n from . import boatcast_preview_v1 as boatcast\n return state['STOPPED'], state['PAUSE_UNTIL']\n")
        transform=adapter._Scoped();tree=transform.visit(tree)
        self.assertEqual(transform.count,1)
        text=ast.unparse(ast.fix_missing_locations(tree))
        self.assertIn('_independent_blocked()',text)
        self.assertIn("state['PAUSE_UNTIL']",text)
        self.assertIn('boatcast = _independent_boatcast',text)


class HoldTests(unittest.TestCase):
    def exercise(self, *, seconds=90, remaining=300, status=200, altered=False, saved=False):
        with TemporaryDirectory() as tmp:
            store,_,_,morning,now=fixture();store.root=Path(tmp)
            clock={'now':now};proof={'day':DAY};sleeps=[];calls=[]
            cutoff=now+timedelta(seconds=remaining)
            morning['PREDEADLINE_PLAN'][0]['MUST_FINISH_BEFORE_JST']=cutoff.isoformat()
            path=store.root/'g11/published-preview-state'/adapter.BOATCAST/'host.json'
            path.parent.mkdir(parents=True)
            host={'next':(now+timedelta(seconds=seconds)).isoformat(),'status':status,'race':KEY,'source':'raw-sha'}
            path.write_text(json.dumps(host))
            if saved:
                target=store.root/'snapshots/predeadline'/('published-exhibition-'+KEY+'.json')
                target.parent.mkdir(parents=True);target.write_text('{}')
            def sleep(value):
                sleeps.append(value);clock['now']+=timedelta(seconds=value)
                if altered:path.write_text(json.dumps({**host,'status':403}))
            runner=NS(store=store,client=NS(clock=lambda:clock['now']),sleeper=sleep)
            checked={'SNAPSHOT':{'SOURCE_RECEIPTS':[{'SOURCE_ID':'raw-sha'}]}}
            def capture(*a,**kw):calls.append(kw);return {KEY:{'sha256':'verified'}}
            modules={'venue_market_v1':NS(recheck=lambda *a:checked),
                     'published_preview_v1':NS(race_roster=lambda *a:{}),
                     'scoped_preview':NS(capture_missing=capture)}
            result={'LIVE_SOURCE_FAILOVER':{'ELIGIBLE_BEFOREINFO':[KEY]},
                    'STADIUM_MARKETS':{KEY:{'source':adapter.BOATCAST,'captured_at':now.isoformat()}}}
            with patch.object(adapter,'stop_scope',return_value=proof):
                try:adapter._wait_for_packet(runner,result,modules,proof)
                except ValueError:
                    if not altered:raise
            return sleeps,calls,result

    def test_existing_90s_hold_waits_in_bounded_chunks_then_rechecks_once(self):
        sleeps,calls,result=self.exercise()
        self.assertEqual(sleeps,[60,30]);self.assertEqual(len(calls),1)
        self.assertEqual(calls[0],{'race_keys':{KEY},'allow_missing':True})
        self.assertEqual(result['INDEPENDENT_PREVIEWS'][KEY]['sha256'],'verified')

    def test_extended_refusal_short_deadline_and_saved_preview_do_not_wait(self):
        for kwargs in [dict(seconds=91),dict(status=403),dict(remaining=269),dict(saved=True)]:
            with self.subTest(kwargs=kwargs):
                sleeps,calls,_=self.exercise(**kwargs);self.assertEqual((sleeps,calls),([],[]))

    def test_changed_provider_hold_cannot_issue_packet(self):
        sleeps,calls,_=self.exercise(altered=True)
        self.assertEqual(sleeps,[60]);self.assertEqual(calls,[])


if __name__=='__main__':unittest.main()
