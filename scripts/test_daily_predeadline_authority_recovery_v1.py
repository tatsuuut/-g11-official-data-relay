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
