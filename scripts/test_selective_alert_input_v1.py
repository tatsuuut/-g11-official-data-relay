import copy
import json
import os
import sys
import tempfile
import types
from pathlib import Path
import unittest
from unittest.mock import patch
import selective_alert_input_v1 as m

class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.row={"race_key":"20261013-13-01","bets":["1-2-3","2-1-3"],"bet_count":2,"lock_sha256":"a"*64,
                  "deadline_at":"2026-10-13T10:45:00+09:00","locked_at":"2026-10-13T10:42:00+09:00",
                  "odds_snapshot_sha256":"b"*64}
        self.receipt={"SOURCE_ID":"odds3t-20261013-13-01","SHA256":"c"*64,"CAPTURED_AT_JST":"2026-10-13T10:39:00+09:00","HOST":"www.boatrace.jp"}
        self.saved={"SNAPSHOT_SHA256":"b"*64};self.odds={"1-2-3":6.1,"2-1-3":22.4}
        self.packet={"STANDARD_RACE_KEY":self.row['race_key'],"RESULT_DATA_PRESENT":False,"ODDS_CAPTURED_AT":self.receipt['CAPTURED_AT_JST'],
                     "ODDS_RESPONSE_SHA256":"c"*64,"ODDS_SNAPSHOT_ID":"b"*64,"ODDS_120":self.odds}
        self.packet['BET_PACKET_HASH']=m.sha(self.packet);self.row['upstream_packet_sha256']=self.packet['BET_PACKET_HASH']
        self.day={"RESULT_DATA_PRESENT":False,"RECORDS":[self.packet]};self.day['CONTENT_SHA256']=m.sha(self.day)
    def test_exact_odds_and_purpose(self):
        p=m.make_proof('WILD4',self.row,self.saved,self.receipt,self.odds,self.day,self.packet)
        self.assertEqual(p['purpose'],'BET_DECISION_INPUT');self.assertEqual(p['values'][0]['market_odds'],6.1)
        a=copy.deepcopy(self.row);a['locked_at']='2026-10-13T10:38:00+09:00'
        self.assertEqual(m.make_proof('ABEKEN_V4',a,self.saved,self.receipt,self.odds)['purpose'],'ALERT_EVALUATION_ONLY')
    def test_no_partial_or_wrong_packet(self):
        with self.assertRaises(ValueError):m.make_proof('ABEKEN_V4',self.row,self.saved,self.receipt,{"1-2-3":6.1})
        p=copy.deepcopy(self.packet);p['ODDS_120']['1-2-3']=999
        with self.assertRaises(ValueError):m.make_proof('WILD4',self.row,self.saved,self.receipt,self.odds,self.day,p)
    def test_missing_is_held_not_filled(self):
        row={**self.row,'classification':'FORMAL'}
        source={'schema':'G11_WILD4_PUBLIC_PROJECTION_V1','date':'2026-10-13','races':[row]}
        payload,held=m.collect(Path('/unused'),source,odds_day=self.day,reader=lambda *_:(_ for _ in ()).throw(FileNotFoundError()))
        self.assertEqual(payload['records'],[]);self.assertEqual(len(held),1)
    def test_no_morning_or_zero_fallback(self):
        source={'operational_date_jst':'2026-10-13','races':[{'key':self.row['race_key'],'abeken_shadow':{'immutable_prediction_snapshots':True,'v53':{'formal_use':True,'legacy_45_fallback':False,'morning':{'practical_bets':['1-2-3']}}}}]}
        self.assertEqual(m.collect(Path('/unused'),source)[0]['records'],[])
    def test_errors_do_not_fail_normal_delivery(self):
        with patch.object(m,'collect',side_effect=RuntimeError('input failure')):
            result=m.deliver_optional(Path('/unused'),{},sender=lambda *_:self.fail('must not send'))
        self.assertEqual(result['status'],'HELD');self.assertEqual(result['notification_send_count'],0)
    def test_only_metadata_sender_no_notifications(self):
        with patch.object(m,'collect',return_value=({'schema':m.SCHEMA,'date':'2026-10-13','records':[{}]},[])):
            calls=[];result=m.deliver_optional(Path('/unused'),{},token='mock-only',sender=lambda *a:(calls.append(a) or {'status':'SAVED'}))
        self.assertEqual(len(calls),1);self.assertFalse(result['selection_enabled']);self.assertFalse(result['delivery_enabled'])
    def test_wire_budget_prevents_network(self):
        with patch.object(m.subprocess,'run',side_effect=AssertionError('network forbidden')):
            with self.assertRaises(ValueError):m.post_batch(m.ORIGIN,'mock',{'date':'2026-10-13','records':['x'*m.WIRE_LIMIT]})

    def test_rescue_lineage_uses_same_saved_packet_and_official_byte_verifier(self):
        row={**self.row,'classification':'LINEAGE'}
        source={'schema':'G11_WILD4_PUBLIC_PROJECTION_V1','date':'2026-10-22',
                'rescue_boundary_sha256':'d'*64,'races':[row]}
        before=copy.deepcopy(source);calls=[]
        def reader(store,key):
            calls.append((store,key));return self.saved,self.receipt,self.odds
        payload,held=m.collect(Path('/saved/rescue/store'),source,odds_day=self.day,reader=reader)
        self.assertEqual(len(payload['records']),1);self.assertEqual(held,[])
        self.assertEqual(calls,[(Path('/saved/rescue/store'),row['race_key'])]);self.assertEqual(source,before)
        self.assertEqual(payload['records'][0]['packet_sha256'],row['upstream_packet_sha256'])
        for edit in [lambda s:s.pop('rescue_boundary_sha256'),lambda s:s.update(date='2026-10-20')]:
            bad=copy.deepcopy(source);edit(bad)
            self.assertEqual(m.collect(Path('/unused'),bad,odds_day=self.day,reader=reader)[0]['records'],[])
        broken=copy.deepcopy(self.packet);broken['ODDS_120']['1-2-3']=999
        day={'RESULT_DATA_PRESENT':False,'RECORDS':[broken]};day['CONTENT_SHA256']=m.sha(day)
        payload,held=m.collect(Path('/unused'),source,odds_day=day,reader=reader)
        self.assertEqual(payload['records'],[]);self.assertEqual(len(held),1)

    def test_rescue_abeken_requires_saved_live_and_exclusion_boundary_without_formal_promotion(self):
        source={'operational_date_jst':'2026-10-22','SNAPSHOT_CLASS':'SAME_DAY_RESCUE_BOUNDARY',
                'ACCOUNTING_CLASS':'TODAY_RESCUE','CANONICAL_MORNING_PROMOTED':False,'RESEARCH_ELIGIBLE_AS_MORNING':False,
                'same_day_rescue':{'immutable':True,'operational_date_jst':'2026-10-22','canonical_morning_promoted':False,
                                  'research_eligible':False,'same_day_results_used':False,'authority_sha256':'d'*64},
                'races':[{'key':self.row['race_key'],'deadline_jst':self.row['deadline_at'],'formal_status':'EXCLUDED',
                          'research_eligible':False,'same_day_rescue_lock':{'v4_snapshot_sha256':'e'*64},
                          'abeken_shadow':{'immutable_prediction_snapshots':True,'v53':{'formal_use':False,'legacy_45_fallback':False,
                          'live':{'practical_bets':self.row['bets'],'buy_count':2,'snapshot_sha256':self.row['lock_sha256'],
                                  'locked_at_jst':self.row['locked_at']}}}}]}
        reader=lambda *_:(self.saved,self.receipt,self.odds)
        before=copy.deepcopy(source);payload,held=m.collect(Path('/saved/rescue/store'),source,reader=reader)
        self.assertEqual(len(payload['records']),1);self.assertEqual(held,[]);self.assertEqual(source,before)
        for edit in [lambda s:s['same_day_rescue'].update(same_day_results_used=True),
                     lambda s:s['races'][0].pop('same_day_rescue_lock'),
                     lambda s:s['races'][0]['abeken_shadow']['v53'].pop('live'),
                     lambda s:s['races'][0].update(research_eligible=True)]:
            bad=copy.deepcopy(source);edit(bad)
            self.assertEqual(m.collect(Path('/unused'),bad,reader=reader)[0]['records'],[])

    def test_relative_saved_store_and_symlink_boundary(self):
        # Real files and the existing verifier's canonical-path requirement.
        class Store:
            def __init__(self, root): self.root=root
            def load_verified_snapshot(self, stage, name):
                path=self.root/'snapshots'/stage/(name+'.json')
                if not path.is_file() or path.is_symlink() or path.resolve(strict=True)!=path:
                    raise ValueError('VERIFIED_SNAPSHOT_FILE')
                return json.loads(path.read_text())
        class Runner:
            @staticmethod
            def _validated_odds_only_market(owner, morning, saved, key):
                return saved['ODDS']
        module=types.ModuleType('g11.canonical.official_pipeline')
        module.OfficialArtifactStore=Store;module.CanonicalRunner=Runner
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'store'
            for stage,name,value in [('morning','official-day-input',{}),('predeadline','odds3t-'+self.row['race_key'],{'SNAPSHOT':{'SOURCE_RECEIPTS':[self.receipt]},'ODDS':self.odds})]:
                path=root/'snapshots'/stage/(name+'.json');path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value))
            relative=Path(os.path.relpath(root))
            with self.assertRaisesRegex(ValueError,'VERIFIED_SNAPSHOT_FILE'):
                Store(relative).load_verified_snapshot('morning','official-day-input')
            with patch.dict(sys.modules,{'g11.canonical.official_pipeline':module}):
                saved,receipt,odds=m.read_official(relative,self.row['race_key'])
                self.assertEqual(receipt,self.receipt);self.assertEqual(odds,self.odds)
                link=Path(temp)/'linked-store';link.symlink_to(root,target_is_directory=True)
                with self.assertRaisesRegex(ValueError,'VERIFIED_SNAPSHOT_FILE'):
                    m.read_official(link,self.row['race_key'])

if __name__=='__main__':unittest.main()

