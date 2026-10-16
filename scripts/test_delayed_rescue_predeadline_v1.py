import unittest
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import delayed_rescue_predeadline_v1 as live

NOW = datetime.fromisoformat("2026-10-16T10:00:00+09:00")
KEY = "20261016-04-02"
DEADLINE = datetime.fromisoformat("2026-10-16T10:05:00+09:00")

def feeds():
    old = {"same_day_rescue": {"authority_sha256":"a"*64}, "source": {},
        "generated_at_jst":NOW.isoformat(), "races":[{"key":KEY,"deadline_jst":DEADLINE.isoformat(),
        "same_day_rescue_lock":{"p3_snapshot_sha256":"b"*64},"research_eligible":False,
        "practical_bets":["1-2-3"],"predeadline":None,
        "abeken_shadow":{"v53":{"morning":{"snapshot_sha256":"c"*64},"live":None}}}]}
    new=deepcopy(old); row=new["races"][0]
    row["predeadline"]={"status":"READY","snapshot_sha256":"d"*64,"captured_at_jst":NOW.isoformat(),
        "best_ev":None,"best_ev_bet":None,"value_bets":None,"odds_captured_at_jst":NOW.isoformat(),"legacy_odds_merit":"－"}
    row["p3_morning_snapshot_sha256"]="b"*64
    row["pre_race_v2"]={"final":{"locked_at_jst":NOW.isoformat()},"early":None}
    row["abeken_shadow"]["v53"]["live"]={"snapshot_sha256":"e"*64,
        "captured_at_jst":NOW.isoformat(),"locked_at_jst":NOW.isoformat()}
    return old,new

class Boundaries(unittest.TestCase):
    def test_before_deadline(self):
        self.assertEqual(live.validate_write("predeadline","beforeinfo-"+KEY,{"RACE_KEY":KEY},{KEY:DEADLINE},NOW),KEY)
    def test_deadline_equal_and_after_rejected(self):
        for at in (DEADLINE,DEADLINE.replace(minute=6)):
            with self.assertRaisesRegex(RuntimeError,"SAVE_AFTER_DEADLINE"):
                live.validate_write("predeadline","beforeinfo-"+KEY,{"RACE_KEY":KEY},{KEY:DEADLINE},at)
    def test_no_morning_or_night_write(self):
        for stage in ("morning","night"):
            with self.assertRaises(RuntimeError): live.validate_write(stage,"prediction-"+KEY,{}, {KEY:DEADLINE},NOW)
    def test_nonrescued_and_formal_rejected(self):
        for value, deadlines in (({},{}),({"FORMAL_USE":True},{KEY:DEADLINE}),({"PURCHASE_EXECUTED":True},{KEY:DEADLINE})):
            with self.assertRaises(RuntimeError): live.validate_write("predeadline","prediction-"+KEY,value,deadlines,NOW)
    def test_append_preserves_rescue(self):
        old,new=feeds(); frozen=deepcopy(old); out,adds=live.append_projection(old,new,NOW)
        self.assertEqual(old,frozen);self.assertEqual(adds['p3'],[KEY]);self.assertEqual(adds['v4'],[KEY])
        self.assertEqual(out['races'][0]['practical_bets'],old['races'][0]['practical_bets'])
    def test_independent_v4_not_waiting_for_p3(self):
        old,new=feeds();new['races'][0]['predeadline']=None
        out,adds=live.append_projection(old,new,NOW);self.assertEqual(adds['p3'],[]);self.assertEqual(adds['v4'],[KEY])
    def test_no_late_publication(self):
        old,new=feeds();out,adds=live.append_projection(old,new,DEADLINE)
        self.assertIsNone(out['races'][0]['predeadline']);self.assertEqual(len(adds['expired']),2)
    def test_idempotent_retry(self):
        old,new=feeds();once,_=live.append_projection(old,new,NOW);again,adds=live.append_projection(once,new,NOW)
        self.assertEqual(once,again);self.assertEqual(adds['p3'],[]);self.assertEqual(adds['v4'],[])
    def test_morning_and_existing_live_mutation_rejected(self):
        old,new=feeds();new['races'][0]['practical_bets']=['6-5-4']
        with self.assertRaises(RuntimeError):live.append_projection(old,new,NOW)
        old,new=feeds();old['races'][0]['predeadline']={'snapshot_sha256':'0'*64}
        with self.assertRaises(RuntimeError):live.append_projection(old,new,NOW)
    def test_timestamp_must_be_before_deadline(self):
        old,new=feeds();new['races'][0]['pre_race_v2']['final']['locked_at_jst']=DEADLINE.isoformat()
        with self.assertRaises(RuntimeError):live.append_projection(old,new,NOW)
    def test_future_selection_omits_excluded(self):
        old,_=feeds(); rows=old['races']+[dict(old['races'][0],key='20261016-04-01',same_day_rescue_lock=None)]
        self.assertEqual(live.timely_keys(rows,NOW),[KEY]);self.assertEqual(live.timely_keys(rows,DEADLINE),[])
    def test_certificate_scope_parent_and_normal_lock(self):
        with TemporaryDirectory() as d:
            state=Path(d);(state/'rescue').mkdir();(state/'daily-runtime'/live.DAY).mkdir(parents=True)
            parent={'immutable':True,'operational_date_jst':live.DAY}
            parent['authority_sha256']=live.sha(live.canonical(parent))
            (state/'daily-runtime'/live.DAY/'authority.json').write_bytes(live.canonical(parent))
            cert={'schema':live.SCHEMA,'operational_date_jst':live.DAY,'immutable':True,
                'canonical_morning_promoted':False,'research_eligible':False,'same_day_results_used':False,
                'parent_authority_sha256':parent['authority_sha256'],'runtime_source_sha':'a'*40,
                'source_files':{'x':'b'*64},'model_files':{'x':'c'*64}}
            cert['authority_sha256']=live.sha(live.canonical(cert))
            (state/'rescue/boundary.json').write_bytes(live.canonical(cert))
            (state/'relay-state.json').write_bytes(live.canonical({'OPERATIONAL_DATE_JST':live.DAY,'MORNING_COMPLETE':False}))
            self.assertEqual(live.certificate(state,live.DAY,NOW),cert)
            with self.assertRaises(RuntimeError):live.certificate(state,'2026-10-17',NOW)
            (state/'store/snapshots/morning').mkdir(parents=True);(state/'store/snapshots/morning/lock.json').write_text('{}')
            with self.assertRaises(RuntimeError):live.certificate(state,live.DAY,NOW)

if __name__ == '__main__': unittest.main()
