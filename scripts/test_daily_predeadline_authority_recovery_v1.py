"""Focused fail-closed regression for the Oct 4 frozen-runtime adapter."""
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import types
import unittest

import daily_predeadline_authority_recovery_v1 as recovery


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        self.store = self.state / "store"
        self.deadline = "2026-10-04T12:00:00+09:00"
        self.early = "2026-10-04T11:50:00+09:00"
        self.key = "20261004-01-01"
        self._write("snapshots/morning/official-day-input.json", {
            "STAGE": "MORNING", "NAME": "official-day-input", "PAYLOAD": {
                "PREDEADLINE_PLAN": [{"RACE_KEY": self.key,
                    "DUE_AT_JST": "2026-10-04T11:45:00+09:00",
                    "MUST_FINISH_BEFORE_JST": self.deadline}]}})
        self.locks = {}
        self.records = []

        def inventory(store, day, phase):
            return {p.relative_to(store).as_posix(): recovery.hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted((store / "snapshots" / phase.lower()).glob("*.json"))}

        def old_verify(state, day, phase, *, exact=False):
            observed = inventory(state / "store", day, phase)
            if exact and observed != self.records[-1]["lock_files"]:
                raise RuntimeError("OLD_EXACT_MISMATCH")
            return observed

        self.module = types.SimpleNamespace(lock_inventory=inventory,
            verify_locks=old_verify, bindings=lambda state, day, phase: self.records)
        recovery.apply(self.module, recovery.DAY)
        self._seal()

    def _write(self, relative, value, *, late=False):
        path = self.store / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True))
        timestamp = datetime.fromisoformat(
            "2026-10-04T12:01:00+09:00" if late else self.early).timestamp()
        os.utime(path, (timestamp, timestamp))
        return path

    def _new(self, suffix="beforeinfo"):
        name = f"snapshots/predeadline/{suffix}-{self.key}.json"
        path = self._write(name, {"STAGE": "PREDEADLINE", "NAME": Path(name).stem,
            "PAYLOAD": {"RACE_KEY": self.key, "CAPTURED_AT_JST": self.early,
                        "RESULT_USED_AS_PREDICTION_FEATURE": False},
            "SOURCE_RECEIPTS": [{"CAPTURED_AT_JST": self.early}]})
        return path

    def _seal(self):
        observed = self.module.lock_inventory(self.store, recovery.DAY, "PREDEADLINE")
        self.records.append({"lock_files": observed})

    def test_A_first_binding(self):
        self.assertEqual(self.module.verify_locks(self.state, recovery.DAY, "PREDEADLINE", exact=True), {})

    def test_B_append_future_lock(self):
        path = self._new()
        observed = self.module.verify_locks(self.state, recovery.DAY, "PREDEADLINE", exact=True)
        self.assertIn(path.relative_to(self.store).as_posix(), observed)

    def test_C_existing_lock_mutation_fails(self):
        path = self._new()
        self.module.verify_locks(self.state, recovery.DAY, "PREDEADLINE")
        self._seal()
        path.write_bytes(b"changed")
        with self.assertRaisesRegex(RuntimeError, "LOCK_MISMATCH"):
            self.module.verify_locks(self.state, recovery.DAY, "PREDEADLINE")

    def test_D_existing_lock_deletion_fails(self):
        path = self._new()
        self._seal()
        path.unlink()
        with self.assertRaisesRegex(RuntimeError, "LOCK_MISMATCH"):
            self.module.verify_locks(self.state, recovery.DAY, "PREDEADLINE")

    def test_E_postdeadline_append_fails(self):
        path = self._new()
        timestamp = datetime.fromisoformat("2026-10-04T12:01:00+09:00").timestamp()
        os.utime(path, (timestamp, timestamp))
        with self.assertRaisesRegex(RuntimeError, "AFTER_DEADLINE"):
            self.module.verify_locks(self.state, recovery.DAY, "PREDEADLINE")

    def test_F_retry_idempotent(self):
        self._new()
        first = self.module.verify_locks(self.state, recovery.DAY, "PREDEADLINE")
        self._seal()
        self.assertEqual(first, self.module.verify_locks(self.state, recovery.DAY, "PREDEADLINE", exact=True))

    def test_G_multiple_heartbeats_monotonic(self):
        self._new("beforeinfo")
        first = self.module.verify_locks(self.state, recovery.DAY, "PREDEADLINE")
        self._seal()
        self._new("odds3t")
        second = self.module.verify_locks(self.state, recovery.DAY, "PREDEADLINE", exact=True)
        self._seal()
        self.assertLess(len(first), len(second))
        self.assertEqual(second, self.module.verify_locks(self.state, recovery.DAY, "PREDEADLINE", exact=True))

    def test_H_morning_gate_unchanged(self):
        with self.assertRaisesRegex(RuntimeError, "OLD_EXACT_MISMATCH"):
            self.module.verify_locks(self.state, recovery.DAY, "MORNING", exact=True)

    def test_I_result_backflow_fails(self):
        path = self._new()
        value = json.loads(path.read_bytes())
        value["PAYLOAD"]["RESULT_USED_AS_PREDICTION_FEATURE"] = True
        self._write(path.relative_to(self.store).as_posix(), value)
        with self.assertRaisesRegex(RuntimeError, "RESULT_BACKFLOW"):
            self.module.verify_locks(self.state, recovery.DAY, "PREDEADLINE")

    def test_J_expired_optional_research_cannot_create_shadow(self):
        with self.assertRaisesRegex(RuntimeError, "SHADOW_AFTER_DEADLINE"):
            recovery.allow_optional_shadow(self.store, self.key,
                "mashiro-v4-shadow-" + self.key,
                now=datetime.fromisoformat("2026-10-04T12:01:00+09:00"))

    def test_K_future_optional_research_can_create_shadow(self):
        recovery.allow_optional_shadow(self.store, self.key,
            "mashiro-v4-shadow-" + self.key,
            now=datetime.fromisoformat(self.early))


class RescueBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.now = datetime.fromisoformat("2026-10-06T10:00:00+09:00")
        self.cert = dict(schema="G11_SAME_DAY_RESCUE_BOUNDARY_V1", operational_date_jst="2026-10-06",
                         canonical_morning_promoted=False, research_eligible=False,
                         same_day_results_used=False, immutable=True, runtime_source_sha="a"*40)
        self.write()
    def write(self):
        unsigned = dict(self.cert)
        unsigned.pop("authority_sha256", None)
        self.cert["authority_sha256"] = recovery.hashlib.sha256(json.dumps(unsigned,
            ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        p = self.state / "rescue/boundary.json"
        p.parent.mkdir(exist_ok=True)
        p.write_text(json.dumps(self.cert))
    def test_case_b_rescue_bootstrap(self):
        self.assertEqual(recovery.rescue_certificate(self.state, "2026-10-06", now=self.now), self.cert)
    def test_case_c_missing_boundary_closed(self):
        (self.state / "rescue/boundary.json").unlink()
        with self.assertRaisesRegex(RuntimeError, "MISSING"):
            recovery.rescue_certificate(self.state, "2026-10-06", now=self.now)
    def test_case_d_wrong_date_closed(self):
        self.cert["operational_date_jst"] = "2026-10-05"
        self.write()
        with self.assertRaisesRegex(RuntimeError, "INVALID"):
            recovery.rescue_certificate(self.state, "2026-10-06", now=self.now)
    def test_case_g_next_morning_not_suppressed(self):
        import daily_runtime_bootstrap_v1 as bootstrap
        self.assertEqual(bootstrap.prepare(self.state, "2026-10-07", "morning", self.state / "source"),
                         {"checkout_candidate": "true", "frozen": "false"})
    def test_case_a_normal_predeadline_no_rescue_bootstrap(self):
        import daily_runtime_bootstrap_v1 as bootstrap
        from unittest.mock import Mock, patch
        path = self.state / "daily-runtime/2026-10-06/authority.json"
        path.parent.mkdir(parents=True); path.write_text("{}")
        module = Mock()
        module.restore_source.return_value = {"runtime_source_sha": "b"*40, "authority_sha256": "c"*64}
        with patch.object(bootstrap, "retained_module", return_value=module):
            result = bootstrap.prepare(self.state, "2026-10-06", "predeadline", self.state / "source")
        self.assertEqual(result["frozen"], "true")
        self.assertNotIn("rescue_bootstrap", result)


class PredeadlineDependencyTests(unittest.TestCase):
    def test_frozen_predeadline_installs_bet_dependencies_after_critical_checkpoint(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/g11-free-runner.yml").read_text()
        names = ["Capture and lock dependency-free PREDEADLINE critical lane",
                 "Save early encrypted critical checkpoint",
                 "Keep the predeadline relay alive between delayed cron starts",
                 "Install pinned private runtime dependencies", "Run the private fail-closed relay"]
        positions = [workflow.index("      - name: " + name) for name in names]
        self.assertEqual(positions, sorted(positions))
        step = workflow.split("      - name: Install pinned private runtime dependencies\n", 1)[1].split("\n      - name:", 1)[0]
        self.assertIn("if: env.G11_NIGHT_TRANSPORT_ONLY != 'true'", step)
        self.assertIn("'catboost==1.2.8'", step)
        self.assertIn("import catboost", step)


class PredeadlineQueueIsolationTests(unittest.TestCase):
    def test_read_only_validation_cannot_displace_production_waiting_job(self):
        import types
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/g11-free-runner.yml").read_text()
        block = workflow.split("\nconcurrency:\n", 1)[1].split("\njobs:", 1)[0]
        expression = block.split("group: ", 1)[1].split("\n", 1)[0].strip()
        self.assertTrue(expression.startswith("${{") and expression.endswith("}}"))
        expression = expression[3:-2].replace("&&", " and ").replace("||", " or ")
        def group(event, phase="", message=""):
            github = types.SimpleNamespace(event_name=event,
                event=types.SimpleNamespace(head_commit=types.SimpleNamespace(message=message)))
            return eval(expression, {"__builtins__": {}},
                        {"github": github, "inputs": types.SimpleNamespace(phase=phase),
                         "contains": lambda value, part: part in value})
        production = group("workflow_dispatch", "predeadline")
        self.assertEqual(production, "g11-free-official-data-runner")
        for event, phase, message in [
            ("schedule", "", ""), ("workflow_dispatch", "night", ""),
            ("workflow_dispatch", "morning", ""), ("workflow_dispatch", "same-day-rescue", ""),
            ("push", "", "[night-transport-only]"), ("push", "", "ordinary runtime update")]:
            self.assertEqual(group(event, phase, message), production)
        for event, phase, message in [
            ("push", "", "[daily-runtime-freeze] regression check"),
            ("workflow_dispatch", "dry-run", "")]:
            self.assertNotEqual(group(event, phase, message), production)
        self.assertIn("cancel-in-progress: false", block)

if __name__ == "__main__":
    unittest.main()

import unittest
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import daily_predeadline_authority_recovery_v1 as live

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
            state=Path(d);(state/'rescue').mkdir();(state/'daily-runtime'/live.DELAYED_DAY).mkdir(parents=True)
            parent={'immutable':True,'operational_date_jst':live.DELAYED_DAY}
            parent['authority_sha256']=live.sha(live.canonical(parent))
            (state/'daily-runtime'/live.DELAYED_DAY/'authority.json').write_bytes(live.canonical(parent))
            cert={'schema':live.SCHEMA,'operational_date_jst':live.DELAYED_DAY,'immutable':True,
                'canonical_morning_promoted':False,'research_eligible':False,'same_day_results_used':False,
                'parent_authority_sha256':parent['authority_sha256'],'runtime_source_sha':'a'*40,
                'source_files':{'x':'b'*64},'model_files':{'x':'c'*64}}
            cert['authority_sha256']=live.sha(live.canonical(cert))
            (state/'rescue/boundary.json').write_bytes(live.canonical(cert))
            (state/'relay-state.json').write_bytes(live.canonical({'OPERATIONAL_DATE_JST':live.DELAYED_DAY,'MORNING_COMPLETE':False}))
            self.assertEqual(live.certificate(state,live.DELAYED_DAY,NOW),cert)
            with self.assertRaises(RuntimeError):live.certificate(state,'2026-10-17',NOW)
            (state/'store/snapshots/morning').mkdir(parents=True);(state/'store/snapshots/morning/lock.json').write_text('{}')
            with self.assertRaises(RuntimeError):live.certificate(state,live.DELAYED_DAY,NOW)

if __name__ == '__main__': unittest.main()

class Oct18ContinuationScopeTests(unittest.TestCase):
    def test_one_invalid_night_lock_keeps_other_engine_and_race(self):
        from types import SimpleNamespace
        day='2026-10-21'
        feed={'races':[]}; results=[]
        for number in (1,2):
            key=f'20261021-04-{number:02d}'
            feed['races'].append({'key':key,'deadline_jst':day+'T12:00:00+09:00',
                'practical_bets':['1-2-3'],
                'same_day_rescue_lock':{'p3_locked_at_jst':day+('T12:01:00+09:00' if number==1 else 'T08:00:00+09:00'),
                    'v4_locked_at_jst':day+'T08:00:00+09:00'},
                'abeken_shadow':{'v53':{'morning':{'practical_bets':['1-2-3']}}}})
            results.append(SimpleNamespace(race_key=key,trifecta_combination='1-2-3',trifecta_payout=1230,
                refund_boats=[],trifecta_settlements=[SimpleNamespace(combination='1-2-3',payout=1230)]))
        before=deepcopy(feed); scores=live.night_scores(feed,results)
        self.assertEqual(scores['20261021-04-01']['engines']['p3_rescue']['status'],'HELD_INVALID_FIXED_PREDICTION')
        self.assertEqual(scores['20261021-04-01']['engines']['abeken_rescue']['status'],'SETTLED')
        self.assertEqual(scores['20261021-04-02']['engines']['p3_rescue']['virtual_profit'],1130)
        self.assertEqual(scores['20261021-04-02']['engines']['goripan']['status'],'SEPARATE_SAVED_LOCK_LEDGER_NO_BACKFILL')
        self.assertEqual(feed,before)

    def test_saved_live_read_failure_is_local_and_never_mutates_public_feed(self):
        from types import SimpleNamespace
        day='2026-10-21';keys=['20261021-04-01','20261021-04-02']
        feed={'races':[{'key':key,'deadline_jst':day+'T12:00:00+09:00'} for key in keys]}
        before=deepcopy(feed)
        def loader(engine):
            def load(key):
                if engine=='p3' and key==keys[0]:raise ValueError('BAD_SAVED_LOCK')
                return {'snapshot_sha256':key+engine,'captured_at_jst':day+'T11:50:00+09:00','practical_bets':['1-2-3']},key+engine
            return load
        receipts={key+engine:{'snapshot_sha256':key+engine,'saved_at_jst':day+'T11:51:00+09:00'} for key in keys for engine in ('p3','abeken')}
        audit,unpublished,held=live.night_saved_live_audit(feed,set(keys),receipts,set(receipts),{e:loader(e) for e in ('p3','abeken')})
        results=[SimpleNamespace(race_key=key,trifecta_combination='1-2-3',trifecta_payout=1230,refund_boats=[],trifecta_settlements=[SimpleNamespace(combination='1-2-3',payout=1230)]) for key in keys]
        scores=live.night_scores(audit,results,held)
        self.assertEqual(feed,before)
        self.assertEqual(len(unpublished),3)
        self.assertEqual(scores[keys[0]]['engines']['p3_live']['status'],'HELD_INVALID_FIXED_PREDICTION')
        for key,engine in ((keys[0],'abeken_live'),(keys[1],'p3_live'),(keys[1],'abeken_live')):
            self.assertEqual(scores[key]['engines'][engine]['virtual_profit'],1130)
        receipts[keys[1]+'p3']['saved_at_jst']=day+'T12:01:00+09:00'
        _,_,held=live.night_saved_live_audit(feed,set(keys),receipts,set(receipts),{e:loader(e) for e in ('p3','abeken')})
        self.assertIn('p3_live',held[keys[1]])
        self.assertNotIn('abeken_live',held[keys[1]])

    def test_only_explicit_days_are_admitted(self):
        self.assertEqual(live.DELAYED_DAYS, {'2026-10-16','2026-10-18','2026-10-21'})
        for day in ('2026-10-17','2026-10-19'):
            with TemporaryDirectory() as td:
                with self.assertRaisesRegex(RuntimeError, 'CURRENT_DAY_ONLY'):
                    live.certificate(Path(td), day, datetime.fromisoformat(day+'T09:00:00+09:00'))

    def test_broken_snapshot_proof_holds_only_its_race_without_changing_forecasts(self):
        rows=[{'key':key,'same_day_rescue_lock':{'p3_snapshot_sha256':'p3','v4_snapshot_sha256':'v4'}}
              for key in ('20261021-04-01','20261021-04-02')]
        baseline={'races':deepcopy(rows)}
        rows[0]['predeadline']={'snapshot_sha256':'corrupt-live'}
        feed={'races':rows};before=deepcopy(feed)
        held=live.night_saved_forecast_holds(feed,baseline,{'p3','v4'},set())
        self.assertEqual(set(held),{'20261021-04-01'})
        self.assertEqual(set(held['20261021-04-01']),{'p3_rescue','abeken_rescue','p3_live','abeken_live'})
        self.assertEqual(feed,before)
    def test_oct18_requires_existing_boundary_and_current_day(self):
        with TemporaryDirectory() as td:
            for at in ('2026-10-17T09:00:00+09:00','2026-10-19T02:00:00+09:00'):
                with self.assertRaisesRegex(RuntimeError,'CURRENT_DAY_ONLY'):
                    live.certificate(Path(td),'2026-10-18',datetime.fromisoformat(at),allow_night_rollover=True)
            with self.assertRaisesRegex(RuntimeError,'BOUNDARY_MISSING'):
                live.certificate(Path(td),'2026-10-18',datetime.fromisoformat('2026-10-18T09:00:00+09:00'))
    def test_oct18_night_overlay_keeps_predictions_and_rejects_early_results(self):
        from types import SimpleNamespace
        day='2026-10-18';key='20261018-04-02'
        feed={'operational_date_jst':day,'stage':'MORNING','SNAPSHOT_CLASS':'SAME_DAY_RESCUE_BOUNDARY',
          'counts':{'formal':0,'results':0,'pending':1},'same_day_rescue':{'immutable':True},
          'races':[{'key':key,'venue_code':'04','race':2,'deadline_jst':day+'T10:05:00+09:00',
          'result_trifecta':None,'payout':None,'result_meta':{},'practical_bets':['1-2-3'],
          'same_day_rescue_lock':{'p3_snapshot_sha256':'a'*64},'research_eligible':False}]}
        result=SimpleNamespace(race_key=key,operational_date=day,venue_code='04',race_no=2,trifecta_popularity=2,
          winning_method='escape',race_status='ESTABLISHED',trifecta_settlements=[],trifecta_combination='2-1-3',trifecta_payout=1200)
        original=deepcopy(feed)
        with self.assertRaisesRegex(RuntimeError,'NIGHT_BEFORE_DEADLINE'):
            live.night_overlay(feed,[result],datetime.fromisoformat(day+'T10:05:00+09:00'))
        out=live.night_overlay(feed,[result],datetime.fromisoformat(day+'T20:00:00+09:00'))
        self.assertEqual(feed,original);self.assertEqual(out['races'][0]['practical_bets'],['1-2-3'])
        self.assertEqual(out['races'][0]['same_day_rescue_lock'],original['races'][0]['same_day_rescue_lock'])
        self.assertEqual(live.night_overlay(out,[result],datetime.fromisoformat(day+'T21:00:00+09:00')),out)
        result.operational_date='2026-10-17'
        with self.assertRaisesRegex(RuntimeError,'NIGHT_RESULT_IDENTITY'):
            live.night_overlay(feed,[result],datetime.fromisoformat(day+'T20:00:00+09:00'))


class FuturePinnedRescueTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.state=Path(self.temp.name)
        self.day='2026-10-22';self.now=datetime.fromisoformat(self.day+'T09:00:00+09:00')
        self.parent={'schema':'G11_DAILY_RUNTIME_AUTHORITY_V1','operational_date_jst':self.day,'immutable':True,
                     'runtime_source_sha':'a'*40,'source_files':{'runtime.py':'b'*64},'model_files':{'model':'c'*64}}
        for key in ('p3','abeken_v53','wild','true_ai'): self.parent[key]={'source_sha':'a'*40,'version':'unchanged'}
        self.cert={**deepcopy(self.parent),'schema':live.SCHEMA,'source_delta':[],
                   'canonical_morning_promoted':False,'research_eligible':False,'same_day_results_used':False,
                   'safety_margin_seconds':300,'model_set_sha256':live.sha(live.canonical(self.parent['model_files']))}
        self.write()
    def write(self):
        self.parent.pop('authority_sha256',None);self.parent['authority_sha256']=live.sha(live.canonical(self.parent))
        self.cert['parent_authority_sha256']=self.parent['authority_sha256']
        self.cert.pop('authority_sha256',None);self.cert['authority_sha256']=live.sha(live.canonical(self.cert))
        for name,value in [('rescue/boundary.json',self.cert),('daily-runtime/'+self.day+'/authority.json',self.parent),
                           ('relay-state.json',{'OPERATIONAL_DATE_JST':self.day,'MORNING_COMPLETE':False})]:
            p=self.state/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(live.canonical(value))
    def test_future_parent_preserving_continuation_and_rollover(self):
        self.assertEqual(live.certificate(self.state,self.day,self.now),self.cert)
        self.assertEqual(live.certificate(self.state,self.day,datetime.fromisoformat('2026-10-23T01:59:00+09:00'),allow_night_rollover=True),self.cert)
        with self.assertRaisesRegex(RuntimeError,'CURRENT_DAY_ONLY'):
            live.certificate(self.state,self.day,datetime.fromisoformat('2026-10-23T02:00:00+09:00'),allow_night_rollover=True)
    def test_signed_changed_source_models_delta_or_binding_cannot_auto_admit(self):
        original=deepcopy(self.cert)
        for field,value in [('runtime_source_sha','d'*40),('source_files',{'other':'e'*64}),('source_delta',['runtime.py']),
                            ('model_files',{'model':'d'*64}),('model_set_sha256','f'*64),('safety_margin_seconds',0),
                            ('wild',{'source_sha':'e'*40,'version':'changed'})]:
            self.cert=deepcopy(original);self.cert[field]=value;self.write()
            with self.assertRaisesRegex(RuntimeError,'FUTURE_'): live.certificate(self.state,self.day,self.now)
    def test_partial_canonical_lock_is_still_prohibited(self):
        p=self.state/'store/snapshots/morning/saved.json';p.parent.mkdir(parents=True);p.write_text('preserved')
        with self.assertRaisesRegex(RuntimeError,'NORMAL_LOCK_CONFLICT'): live.certificate(self.state,self.day,self.now)
        self.assertEqual(p.read_text(),'preserved')
    def test_future_night_uses_exact_saved_race_set_not_incident_156(self):
        base={'operational_date_jst':self.day,'stage':'MORNING','status':'PASS','generated_at_jst':self.now.isoformat(),
              'same_day_rescue':self.cert,'counts':{'formal':0,'results':0,'pending':2},
              'races':[{'key':'20261022-01-0'+str(i),'result_trifecta':None,'payout':None,'result_meta':{},'practical_bets':['1-2-3']} for i in (1,2)]}
        out=deepcopy(base);out.update(stage='NIGHT',generated_at_jst=self.day+'T23:00:00+09:00');out['counts'].update(results=2,pending=0)
        for r in out['races']: r.update(result_trifecta='1-2-3',payout=1200)
        patch=json.loads(live.rescue_night_delta(base,out,'e'*64));self.assertEqual(len(patch['RACES']),2)
        for change in (lambda f:f['races'].pop(),lambda f:f['races'][0].update(key='20261022-02-01'),lambda f:f['races'][0].update(practical_bets=['6-5-4'])):
            bad=deepcopy(out);change(bad)
            with self.assertRaises(RuntimeError): live.rescue_night_delta(base,bad,'e'*64)
    def test_future_saved_rescue_routes_cron_and_night_to_same_frozen_source(self):
        import daily_runtime_bootstrap_v1 as bootstrap
        (self.state/'rescue/feed.json').write_text('{}')
        self.assertEqual(bootstrap.recovery_route(self.state,self.day,'morning',self.now)['phase'],'predeadline')
        with patch.object(live,'clock',return_value=self.now):
            for phase in ('predeadline','night'):
                value=bootstrap.prepare(self.state,self.day,phase,self.state/'unused')
                self.assertEqual(value['source_sha'],self.cert['runtime_source_sha'])
                self.assertEqual(value['d16'],'true')



class RescueDeliveryRoutingTests(unittest.TestCase):
    def route(self, day, stage, audit, target):
        import daily_runtime_delivery_v1 as delivery
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root / "rescue").mkdir()
            if audit: (root / "rescue/predeadline-audit.json").write_text("{}")
            feed=root / "feed.json"
            feed.write_text(json.dumps({"operational_date_jst": day, "stage": stage,
                "SNAPSHOT_CLASS": "SAME_DAY_RESCUE_BOUNDARY",
                "caution": "x" * (delivery.hot.BASE_BUDGET + 100)}))
            before=feed.read_bytes()
            with patch.object(recovery,"deliver",return_value="LIVE") as live, \
                 patch.object(recovery,"deliver_delayed_night",return_value="NIGHT") as night, \
                 patch.object(delivery,"deliver_rescue",return_value="INITIAL") as initial:
                self.assertEqual(delivery.deliver(root,feed,delivery.ORIGIN),target)
                self.assertEqual(live.call_count,int(target=="LIVE"))
                self.assertEqual(night.call_count,int(target=="NIGHT"))
                self.assertEqual(initial.call_count,int(target=="INITIAL"))
            self.assertEqual(feed.read_bytes(),before)

    def test_large_live_feed_uses_delta_for_all_admitted_dates(self):
        for day in ("2026-10-16","2026-10-18","2026-10-21","2026-10-22","2026-11-01"):
            with self.subTest(day=day): self.route(day,"MORNING",True,"LIVE")

    def test_night_does_not_need_live_audit_or_morning_base(self):
        for day in ("2026-10-21","2026-10-22"):
            with self.subTest(day=day): self.route(day,"NIGHT",False,"NIGHT")

    def test_initial_boundary_and_legacy_dates_keep_existing_transport(self):
        self.route("2026-10-21","MORNING",False,"INITIAL")
        self.route("2026-10-06","MORNING",True,"INITIAL")

    def test_boundary_error_never_falls_back_to_full_republication(self):
        import daily_runtime_delivery_v1 as delivery
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root / "rescue").mkdir()
            (root / "rescue/predeadline-audit.json").write_text("{}")
            feed=root / "feed.json"
            feed.write_text(json.dumps({"operational_date_jst":"2026-10-21","stage":"MORNING",
                "SNAPSHOT_CLASS":"SAME_DAY_RESCUE_BOUNDARY"}))
            with patch.object(recovery,"deliver",side_effect=RuntimeError("CERTIFICATE")), \
                 patch.object(delivery,"deliver_rescue",side_effect=AssertionError("UNSAFE_FALLBACK")):
                with self.assertRaisesRegex(RuntimeError,"CERTIFICATE"):
                    delivery.deliver(root,feed,delivery.ORIGIN)

    def test_publication_failure_cannot_stop_saved_rescue_and_observer(self):
        from types import SimpleNamespace as S
        workflow=(Path(__file__).resolve().parents[1]/".github/workflows/g11-free-runner.yml").read_text()
        def allowed(name,persisted=True,cancelled=False,runtime="success",published="failure",early=False,followup=False,needed=False):
            block=workflow.split("      - name: "+name+"\n",1)[1]
            expr=block.split("if: ",1)[1].split("\n",1)[0]
            self.assertIn("always()",expr)
            expr=expr.replace("&&"," and ").replace("||"," or ").replace("!cancelled()","not cancelled()")
            expr=expr.replace("steps.publication-state-verified.outputs.ready",repr("true" if persisted else "false"))
            expr=expr.replace("steps.wild4-early.outputs.sent",repr("true" if early else ""))
            expr=expr.replace("steps.wild4-followup.outputs.sent",repr("true" if followup else ""))
            expr=expr.replace("steps.wild4-followup.outputs.needed",repr("true" if needed else "false"))
            steps=S(runtime=S(outcome=runtime),publish=S(outcome=published),
                authority=S(outputs=S(rescue_bootstrap="true")),due=S(outputs=S(phase="predeadline")))
            return eval(expr,{"__builtins__":{}},{"steps":steps,"always":lambda:True,"cancelled":lambda:cancelled})
        for name in ("Rescue","WILD4 shadow"):
            self.assertTrue(allowed(name))
            self.assertFalse(allowed(name,persisted=False))
            self.assertFalse(allowed(name,cancelled=True))
        self.assertFalse(allowed("WILD4 shadow",runtime="failure"))
        self.assertFalse(allowed("WILD4 shadow",early=True))
        self.assertTrue(allowed("WILD4 shadow",early=True,needed=True))
        self.assertFalse(allowed("WILD4 shadow",early=True,needed=True,followup=True))
        self.assertTrue(allowed("Rescue",early=True))


class StoredRescueTransportTests(unittest.TestCase):
    def test_exact_source_readback_uses_authenticated_route_and_rejects_display_body(self):
        from unittest.mock import patch
        import hot_delta_transport_v1 as hot
        day="2026-10-21"; origin="https://g11-race-lab.higatatsunori2.chatgpt.site"
        feed={"operational_date_jst":day,"SNAPSHOT_CLASS":"SAME_DAY_RESCUE_BOUNDARY"}
        raw=b'{ "SNAPSHOT_CLASS":"SAME_DAY_RESCUE_BOUNDARY", "operational_date_jst":"2026-10-21" }\n'
        headers={"x-g11-transport-readback":"STORED_RESCUE","x-g11-source-feed-sha256":hot.hash_bytes(raw)}
        with patch.dict(os.environ,{"G11_SYNC_OIDC":"fixture"}), patch.object(hot,"fetch",return_value=(feed,headers,raw)) as read:
            self.assertEqual(hot.fetch_stored_rescue(origin,day),(feed,headers,raw))
            read.assert_called_once_with(origin+"/api/g11-feed?date="+day+"&view=stored-rescue",token="fixture")
            headers["x-g11-source-feed-sha256"]="f"*64
            with self.assertRaisesRegex(RuntimeError,"STORED_RESCUE_READBACK"):hot.fetch_stored_rescue(origin,day)
            with self.assertRaisesRegex(RuntimeError,"ORIGIN_OR_DAY"):hot.fetch_stored_rescue("https://elsewhere.invalid",day)

    def test_night_mints_existing_workflow_oidc_without_persisting_token(self):
        from unittest.mock import patch,MagicMock
        import hot_delta_transport_v1 as hot
        import urllib.request,io
        day="2026-10-21";origin="https://g11-race-lab.higatatsunori2.chatgpt.site"
        feed={"operational_date_jst":day,"SNAPSHOT_CLASS":"SAME_DAY_RESCUE_BOUNDARY"};raw=json.dumps(feed).encode()
        headers={"x-g11-transport-readback":"STORED_RESCUE","x-g11-source-feed-sha256":hot.hash_bytes(raw)}
        response=MagicMock();response.__enter__.return_value=io.BytesIO(b'{"value":"fixture-night"}')
        with patch.dict(os.environ,{"ACTIONS_ID_TOKEN_REQUEST_URL":"https://issuer.invalid/token?x=1","ACTIONS_ID_TOKEN_REQUEST_TOKEN":"request-fixture","G11_OIDC_AUDIENCE":"g11-race-lab"},clear=True), \
             patch.object(urllib.request,"urlopen",return_value=response) as mint,patch.object(hot,"fetch",return_value=(feed,headers,raw)) as read:
            hot.fetch_stored_rescue(origin,day)
            self.assertIn("audience=g11-race-lab",mint.call_args.args[0].full_url)
            self.assertEqual(read.call_args.kwargs,{"token":"fixture-night"})
            self.assertNotIn("G11_SYNC_OIDC",os.environ)

