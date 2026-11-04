import copy
from datetime import datetime
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wild4_republish_20261103 as r

class RepublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.ledger=Path(self.temp.name); (self.ledger/'settlements').mkdir()
        self.check=r.verifier(Path(os.environ['WILD4_SETTLEMENT_SOURCE']))
        rows=[]; public=[]; keys=[]
        for i in range(143):
            key=f'20261103-{i//12+1:02d}-{i%12+1:02d}'; keys.append(key)
            lock={'STANDARD_RACE_KEY':key,'CANDIDATE_MODEL_SHA256':r.MODEL,'SELECTED_BETS':['1-2-3'],'BUY_COUNT':1,
                'LOCKED_AT':'2026-11-03T09:00:00+09:00','RACE_DEADLINE_AT':'2026-11-03T10:00:00+09:00',
                'INPUT_PACKET_SHA256':'b'*64,'ODDS_SNAPSHOT_SHA256':'c'*64,'EFFECTIVE_MODEL_IDENTITY':r.GENERATION,'CALIBRATION_SHA256':'d'*64}
            lock=self.save(self.ledger/(key+'.json'),lock)
            row={'race_key':key,'model_sha256':r.MODEL,'lock_sha256':lock['CONTENT_SHA256'],'bets':['1-2-3'],'bet_count':1,
                'locked_at':lock['LOCKED_AT'],'deadline_at':lock['RACE_DEADLINE_AT'],'upstream_packet_sha256':'b'*64,
                'odds_snapshot_sha256':'c'*64,'effective_model_identity':r.GENERATION,'calibration_sha256':'d'*64,'abstain':False,'settlement':None}
            rows.append(row)
            self.save(self.ledger/'settlements'/(key+'.json'),{'SCHEMA':'G11_WILD_BET_FUTURE_OOS_SHADOW_RESULT_V1',
                'STANDARD_RACE_KEY':key,'SHADOW_LOCK_SHA256':lock['CONTENT_SHA256'],'OFFICIAL_SOURCE_SHA256':'e'*64,
                'OFFICIAL_CAPTURED_AT':'2026-11-03T23:00:00+09:00','OFFICIAL_RESULT':'1-2-3','EVALUATION_STATUS':'FIRST_CURRENT_VALID',
                'SHADOW_HIT':True,'SHADOW_BUY_COUNT':1,'SHADOW_UNIT_100YEN_INVESTMENT':100,'SHADOW_UNIT_100YEN_PAYOUT':200,
                'REFUND_OCCURRED':False,'ACTUAL_PURCHASE':False,'PRODUCTION_PROMOTION_ELIGIBLE':False})
            public.append({'key':key,'result_meta':{'result_status':'ESTABLISHED','trifecta_settlements':[{'trifecta':'1-2-3','payout':200}]}})
        manifest=self.save(self.ledger/'batch-manifest.json',{'DATE':r.DAY,'CANDIDATE_MODEL_SHA256':r.MODEL,'RACE_KEYS':keys})
        self.patcher=patch.object(r,'MANIFEST',manifest['CONTENT_SHA256']); self.patcher.start();self.addCleanup(self.patcher.stop)
        self.prior={'races':rows,'observation':{'unchanged':'metadata'}}; self.feed={'races':public}

    def save(self,path,value):
        value={k:v for k,v in value.items() if k!='CONTENT_SHA256'}
        value['CONTENT_SHA256']=r.digest(r.canonical(value)+b'\n');path.write_bytes(r.canonical(value)+b'\n');return value

    def attach(self):return r.attach_saved(self.prior,self.ledger,self.check,self.feed)

    def test_exact_saved_settlements_append_without_any_source_write(self):
        before=r.inventory(self.ledger); old=copy.deepcopy(self.prior); result=self.attach()
        self.assertEqual(r.inventory(self.ledger),before);self.assertEqual(self.prior,old)
        self.assertEqual(len(result['races']),143);self.assertEqual(result['observation'],old['observation'])
        for a,b in zip(old['races'],result['races']):self.assertEqual({**b,'settlement':None},a)
        self.prior=result;self.assertEqual(self.attach(),result)

    def test_missing_saved_result_is_never_regenerated(self):
        (self.ledger/'settlements'/(self.prior['races'][0]['race_key']+'.json')).unlink()
        with self.assertRaisesRegex(RuntimeError,'ALL_SAVED_SETTLEMENTS'):self.attach()

    def test_tampered_source_hash_is_rejected(self):
        p=next((self.ledger/'settlements').glob('*.json'));v=r.read(p);v['SHADOW_UNIT_100YEN_PAYOUT']=300;p.write_bytes(r.canonical(v))
        with self.assertRaisesRegex(ValueError,'SETTLEMENT_SHA'):self.attach()

    def test_hash_valid_but_wrong_official_payout_is_rejected(self):
        p=next((self.ledger/'settlements').glob('*.json'));v=r.read(p);v['SHADOW_UNIT_100YEN_PAYOUT']=300;self.save(p,v)
        with self.assertRaisesRegex(RuntimeError,'SAVED_OFFICIAL_JOIN'):self.attach()

    def test_existing_settlement_cannot_be_replaced(self):
        self.prior['races'][0]['settlement']={'different':'saved'}
        with self.assertRaisesRegex(RuntimeError,'SETTLEMENT_CONFLICT'):self.attach()

    def test_changed_original_lock_is_rejected(self):
        self.prior['races'][0]['bets']=['1-3-2']
        with self.assertRaisesRegex(RuntimeError,'LOCK_IDENTITY'):self.attach()

    def test_exact_manual_expiring_scope(self):
        env={'GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_REF':'refs/heads/main','GITHUB_REPOSITORY':r.REPO,
            'RECOVERY_MODE':r.MODE,'RECOVERY_DATE':r.DAY,'PREDEADLINE_CHECKPOINT_KEY':'','NIGHT_CHECKPOINT_KEY':''}
        now=datetime.fromisoformat('2026-11-05T00:10:00+09:00')
        with patch.dict(os.environ,env):r.scope(now)
        for key,value in [('GITHUB_EVENT_NAME','schedule'),('RECOVERY_DATE','2026-11-04'),('GITHUB_REF','refs/heads/other'),
                          ('PREDEADLINE_CHECKPOINT_KEY','x'),('NIGHT_CHECKPOINT_KEY','x')]:
            with self.subTest(key=key),patch.dict(os.environ,{**env,key:value}),self.assertRaises(RuntimeError):r.scope(now)
        with patch.dict(os.environ,env),self.assertRaises(RuntimeError):r.scope(datetime.fromisoformat('2026-11-06T00:00:00+09:00'))

if __name__=='__main__':unittest.main()
