"""Non-production differences: pure contract, immutable append, and isolation."""
from copy import deepcopy
from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import patch
import wild4_night_recovery_20261013 as r

ROOT = Path(__file__).resolve().parents[1]

class RecoveryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not all(os.environ.get(k) for k in ("RECOVERY_TEST_PRIVATE", "RECOVERY_TEST_SETTLEMENT", "RECOVERY_TEST_APP")):
            raise unittest.SkipTest("Requires pinned source checkouts and the approved saved app fixture")
        cls.account = r.accounting(Path(os.environ["RECOVERY_TEST_PRIVATE"]), Path(os.environ["RECOVERY_TEST_SETTLEMENT"]))
        cls.app = json.loads(Path(os.environ["RECOVERY_TEST_APP"]).read_bytes())

    def fixture(self, ledger):
        projection = r.projection_from_app(deepcopy(self.app))
        source = {x["key"]: x for x in self.app["races"]}
        fields = list(self.account.FIELDS); rows, direct = [], []
        captured = "2026-10-13T22:00:00+09:00"
        for row in projection["races"]:
            key = row["race_key"]
            # Fixture LOCKs copy historical tickets for contract testing only.
            # They are never passed to the production manifest gate or saved.
            lock = {"STANDARD_RACE_KEY":key,"STATUS":"LOCKED_BEFORE_RESULT",
                "RACE_DEADLINE_AT":row["deadline_at"],"SELECTED_BETS":row["bets"]}
            lock["CONTENT_SHA256"] = self.account.bootstrap.sha256_value(lock)
            (ledger/(key+".json")).write_bytes(self.account.bootstrap._canonical_bytes(lock))
            row["lock_sha256"] = lock["CONTENT_SHA256"]
            race = source[key]
            rows.append([key,race["result_trifecta"],race["payout"],False,"成立",captured,"https://www.boatrace.jp/"])
            direct.append({"operational_date":r.DAY,"venue_code":key[9:11],"race_no":int(key[-2:]),
                "trifecta_combination":race["result_trifecta"],"trifecta_payout":race["payout"],
                "refund_boats":[],"race_status":"ESTABLISHED"})
        night = {"SNAPSHOT_SHA256":"a"*64,"SNAPSHOT":{"STAGE":"NIGHT","NAME":"workbook-ledger-projection","PAYLOAD":{
            "SCHEMA":"G11_CANONICAL_NIGHT_WORKBOOK_PROJECTION_V2","STATUS":"NIGHT_WORKBOOK_PROJECTION_COMPLETE_LEDGER_TRANSFER_PENDING",
            "AGGREGATION_SCOPE":"ALL_RESULTS_FINAL","PENDING_RACE_KEYS":[],"OPERATIONAL_DATE_JST":r.DAY,
            "SOURCE_BINDINGS":{"NIGHT_RESULTS_SNAPSHOT_SHA256":"b"*64,"OFFICIAL_RESULT_RESPONSE_SHA256":"c"*64},
            "RESULT_RAW16_FIELDS":fields,"RESULT_RAW16_ROWS":rows,"RESEARCH_EXCLUSION_POLICY":{"SG_CONFIRMED_RACE_KEYS":[]},
            "OFFICIAL_TRIFECTA_SETTLEMENTS":{"MULTI_SETTLEMENT_RACE_KEYS":[]}}}}
        official = {"SNAPSHOT_SHA256":"b"*64,"SNAPSHOT":{"STAGE":"NIGHT","NAME":"official-day-results",
            "SOURCE_RECEIPTS":[{"SHA256":"c"*64,"CAPTURED_AT_JST":captured}],"PAYLOAD":{
                "OPERATIONAL_DATE_JST":r.DAY,"STATUS":"NIGHT_RESULTS_COMPLETE","PENDING_RACE_KEYS":[],"RESULTS":direct}}}
        return projection, night, official

    def test_existing_projection_reconstructed_byte_exact(self):
        p = r.projection_from_app(self.app)
        self.assertEqual(r.digest(r.canonical(p)),r.PROJECTION)
        self.assertEqual(len(p["races"]),96)
        self.assertEqual(sum(x["settlement"] is not None for x in p["races"]),0)

    def test_96_contract_results_and_idempotency(self):
        with tempfile.TemporaryDirectory() as d:
            ledger=Path(d); p,n,o=self.fixture(ledger)
            before=r.inventory(ledger)
            self.assertEqual(self.account.settle_day(ledger,n,r.DAY,o),{"new":96,"total":96,"excluded":0})
            after=r.inventory(ledger)
            self.assertTrue(all(after[k]==v for k,v in before.items()))
            records=self.account.verify_prior(ledger)
            proposed=r.result_projection(p,records)
            total=r.totals_and_join(proposed,self.app)
            self.assertEqual(total["races"],96)
            self.assertEqual(self.account.settle_day(ledger,n,r.DAY,o)["new"],0)
            self.assertEqual(after,r.inventory(ledger))
            print("FIXTURE_96_TOTALS="+json.dumps(total,sort_keys=True))

    def test_source_identity_mismatch_stops_before_append(self):
        with tempfile.TemporaryDirectory() as d:
            ledger=Path(d);_,n,o=self.fixture(ledger);before=r.inventory(ledger)
            o["SNAPSHOT_SHA256"]="d"*64
            with self.assertRaisesRegex(ValueError,"NIGHT_DIRECT_RESULT_BINDING"):
                self.account.settle_day(ledger,n,r.DAY,o)
            self.assertEqual(before,r.inventory(ledger))

    def test_existing_settlement_conflict_not_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            ledger=Path(d);_,n,o=self.fixture(ledger)
            self.account.settle_day(ledger,n,r.DAY,o);before=r.inventory(ledger)
            n["SNAPSHOT_SHA256"]="d"*64
            with self.assertRaisesRegex(ValueError,"IMMUTABLE_SETTLEMENT_CONFLICT"):
                self.account.settle_day(ledger,n,r.DAY,o)
            self.assertEqual(before,r.inventory(ledger))

    def test_p3_abeken_and_missing36_protected(self):
        changed=deepcopy(self.app)
        key=next(iter(changed["final_accounting"]["by_race"]))
        changed["final_accounting"]["by_race"][key]["engines"]["p3"]["final_point_count"]+=1
        self.assertNotEqual(r.protected_app(changed),r.protected_app(self.app))
        changed=deepcopy(self.app)
        changed["final_accounting"]["by_race"][key]["engines"]["abeken"]["final_point_count"]+=1
        self.assertNotEqual(r.protected_app(changed),r.protected_app(self.app))
        self.assertEqual(len(self.app["races"])-len(r.projection_from_app(self.app)["races"]),36)

    def test_other_date_or_mode_has_no_execution(self):
        import sys
        with patch.dict(r.os.environ,{"RECOVERY_MODE":r.MODE,"RECOVERY_DATE":"2026-10-14",
            "GITHUB_EVENT_NAME":"workflow_dispatch","GITHUB_REF":"refs/heads/main","GITHUB_REPOSITORY":r.REPO}),\
            patch.object(sys,"argv",["recovery","prepare","--work","unused"]),patch.object(r,"prepare") as prepare:
            with self.assertRaisesRegex(RuntimeError,"EXPLICIT_RECOVERY_SCOPE"):r.main()
            prepare.assert_not_called()

if __name__ == "__main__":unittest.main()
