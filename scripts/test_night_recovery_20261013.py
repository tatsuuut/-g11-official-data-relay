"""Saved incident fixtures are local evidence, never committed to the public repo."""
from copy import deepcopy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from night_recovery_20261013 import correct_feed, fetch_accepted, KEY
import daily_runtime_delivery_v1 as delivery
import hot_delta_transport_v1 as hot
import tempfile
from types import SimpleNamespace


class SavedTransportTimestampTests(unittest.TestCase):
    def plan(self):
        top = {"generated_at_jst": delivery.N13_CREATED, "source": {"projection_sha256": "a"*64},
               "final_accounting": {"bets": ["1-2-3"], "profit": -900}}
        delta = {"TOP_LEVEL": top, "RACES": [{"RACE_KEY": KEY, "EXPECTED_RACE_VERSION": "b"*64,
                 "CHANGED_FIELDS": {"result_trifecta": "2-1-3"}}], "OPERATIONAL_DATE": delivery.N13_DAY,
                 "BASE_FEED_SHA": "c"*64, "PATCH_CREATED_AT": delivery.N13_CREATED,
                 "PATCH_SEQUENCE": 1, "PATCH_SHA256": "d"*64}
        feed = {"source": top["source"], "races": [{"key": KEY}], "counts": {"races": 1, "results": 1, "pending": 0},
                "daily_runtime": {"authority_sha256": "e"*64}}
        checkpoint = {"operational_date_jst": delivery.N13_DAY, "pending": 0, "authority_sha256": "e"*64,
                      "feed_sha256": delivery.N13_FEED_SHA, "checkpoint_sha256": delivery.N13_CHECKPOINT_SHA}
        m, c = hot.night_chunks(delta, feed, checkpoint)
        return {"checkpoint_sha256": delivery.N13_CHECKPOINT_SHA, "manifest": m, "chunks": c}

    def test_only_wire_time_and_derived_hashes_change(self):
        original = self.plan(); before = deepcopy(original)
        fixed = delivery.seconds_only_n13_plan(original)
        self.assertEqual(original, before)
        self.assertEqual(fixed["manifest"]["PATCH_CREATED_AT"], "2026-10-14T02:20:52+09:00")
        self.assertNotEqual(fixed["manifest"]["NIGHT_TRANSACTION_ID"], original["manifest"]["NIGHT_TRANSACTION_ID"])
        for old, new in zip(original["chunks"], fixed["chunks"]):
            self.assertEqual(old["RACES"], new["RACES"])
            top = deepcopy(old["TOP_LEVEL"])
            if "generated_at_jst" in top: top["generated_at_jst"] = "2026-10-14T02:20:52+09:00"
            self.assertEqual(new["TOP_LEVEL"], top)
        self.assertEqual(fixed["manifest"]["CANONICAL_FEED_SHA256"], delivery.N13_FEED_SHA)
        self.assertEqual(fixed["manifest"]["EXPECTED_FINAL_ACCOUNTING_SHA"], original["manifest"]["EXPECTED_FINAL_ACCOUNTING_SHA"])
        self.assertEqual(delivery.seconds_only_n13_plan(original), fixed)

    def test_wrong_canonical_or_corrupt_chunk_stops_before_send(self):
        bad = self.plan(); bad["manifest"]["CANONICAL_FEED_SHA256"] = "0"*64
        with self.assertRaisesRegex(RuntimeError, "N13_PLAN_SCOPE"):
            delivery.seconds_only_n13_plan(bad)
        bad = self.plan(); bad["chunks"][0]["TOP_LEVEL"]["generated_at_jst"] = "tampered"
        with self.assertRaisesRegex(RuntimeError, "N13_PRIOR_CHUNK_HASH"):
            delivery.seconds_only_n13_plan(bad)

    def test_http_rejection_is_never_retried(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp); feed = state / "feed.json"
            feed.write_text(json.dumps({"operational_date_jst": delivery.N13_DAY, "stage": "NIGHT"}))
            implementation = SimpleNamespace(load=lambda *a: {}, json_file=lambda p: json.loads(p.read_bytes()), verify_locks=lambda *a, **k: None,
                read_checkpoint=lambda *a: {"feed_sha256": hot.hash_bytes(feed.read_bytes())}, day_root=lambda *a: state)
            with patch.dict(os.environ, {"G11_N13T": "true", "G11_SYNC_OIDC": "mock"}), \
                 patch.object(delivery, "retained_module", return_value=implementation), \
                 patch.object(delivery, "identity"), patch.object(delivery, "check_saved_n13"), \
                 patch.object(delivery, "saved_n13_plan"), \
                 patch.object(delivery, "send_night", side_effect=RuntimeError("NIGHT_TRANSACTION_REJECTED:HTTP409")) as send, \
                 patch.object(delivery.time, "sleep") as wait:
                with self.assertRaisesRegex(RuntimeError, "HTTP409"):
                    delivery.deliver(state, feed, delivery.ORIGIN, pause=wait)
                self.assertEqual(send.call_count, 1)
                wait.assert_not_called()

    def test_emit_requires_scope_and_exact_saved_hash(self):
        with patch.dict(os.environ, {"G11_N13T": "false"}):
            with self.assertRaisesRegex(RuntimeError, "N13_TRANSPORT_SCOPE"):
                delivery.emit_saved_n13(Path("unused"), Path("unused"))
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / "feed.json"; p.write_text("tampered")
            impl = SimpleNamespace(read_checkpoint=lambda *a: {"checkpoint_sha256": delivery.N13_CHECKPOINT_SHA,
                "feed_sha256": delivery.N13_FEED_SHA})
            with self.assertRaisesRegex(RuntimeError, "N13_SAVED_CANONICAL_HASH"):
                delivery.check_saved_n13(impl, Path(temp), p)


class TransportScratchTests(unittest.TestCase):
    def test_relative_transport_output_exists_and_cwd_is_restored(self):
        original = Path.cwd()
        visited = []
        def read(url):
            visited.append(Path.cwd())
            self.assertNotEqual(Path.cwd(), original)
            Path(".relay-output/hot-get-body.json").write_text("{}")
            return {}, {}, b"{}"
        with patch("night_recovery_20261013.hot.fetch", read):
            self.assertEqual(fetch_accepted("https://example.invalid"), ({}, {}, b"{}"))
        self.assertEqual(Path.cwd(), original)
        self.assertFalse(visited[0].exists())

    def test_failed_read_restores_cwd_without_production_write(self):
        original = Path.cwd()
        with patch("night_recovery_20261013.hot.fetch", side_effect=RuntimeError("FETCH_FAILED")):
            with self.assertRaisesRegex(RuntimeError, "FETCH_FAILED"):
                fetch_accepted("https://example.invalid")
        self.assertEqual(Path.cwd(), original)


@unittest.skipUnless(os.environ.get("G11_NIGHT_RECOVERY_FIXTURE_ROOT"), "saved incident evidence required")
class SavedEvidenceTests(unittest.TestCase):
    def setUp(self):
        root = Path(os.environ["G11_NIGHT_RECOVERY_FIXTURE_ROOT"])
        self.source = root / "night-recovery-20261013/source"
        self.accepted = json.loads((root / "night-reflection-20261013/canonical-after.json").read_bytes())
        self.feed = json.loads((root / "night-recovery-20261013/saved-night-projection.json").read_bytes())

    def test_only_affected_race_and_exact_saved_nine_bets(self):
        original = deepcopy(self.feed)
        fixed = correct_feed(self.feed, self.accepted, self.source)
        self.assertEqual(self.feed, original)
        changed = [a["key"] for a, b in zip(original["races"], fixed["races"]) if a != b]
        self.assertEqual(changed, [KEY])
        row = fixed["final_accounting"]["by_race"][KEY]["engines"]["abeken"]
        lock = next(r for r in self.accepted["races"] if r["key"] == KEY)["abeken_shadow"]["v53"]["live"]
        self.assertEqual(row["final_bets"], lock["practical_bets"])
        self.assertEqual(row["final_bet_snapshot_id"], lock["snapshot_sha256"])
        self.assertEqual(row["score"]["profit"], -900)
        self.assertEqual(fixed["final_accounting"]["engines"]["abeken"]["profit"] - original["final_accounting"]["engines"]["abeken"]["profit"], 600)
        for k, v in original["final_accounting"]["by_race"].items():
            if k != KEY:
                self.assertEqual(v, fixed["final_accounting"]["by_race"][k])
        for field in ("deme_discovery", "research_db", "research", "wild_pack", "daily_runtime", "true_ai", "abeken"):
            self.assertEqual(original.get(field), fixed.get(field))

    def test_wrong_identity_and_late_lock_fail_closed(self):
        for field, value in (("snapshot_sha256", "0" * 64), ("locked_at_jst", "2026-10-13T20:00:00+09:00")):
            accepted = deepcopy(self.accepted)
            lock = next(r for r in accepted["races"] if r["key"] == KEY)["abeken_shadow"]["v53"]["live"]
            lock[field] = value
            with self.assertRaisesRegex(RuntimeError, "ACCEPTED_FORMAL_LOCK"):
                correct_feed(self.feed, accepted, self.source)

    def test_other_day_result_and_changed_base_fail_closed(self):
        for mutation in ("date", "result", "base"):
            feed = deepcopy(self.feed)
            row = next(r for r in feed["races"] if r["key"] == KEY)
            if mutation == "date": feed["operational_date_jst"] = "2026-10-14"
            elif mutation == "result": row["payout"] = 9999
            else: row["abeken_shadow"]["v53"]["live"] = {"unexpected": True}
            with self.assertRaises(RuntimeError): correct_feed(feed, self.accepted, self.source)

    def test_correction_cannot_be_applied_twice(self):
        fixed = correct_feed(self.feed, self.accepted, self.source)
        with self.assertRaisesRegex(RuntimeError, "EXPECTED_LOST_LOCK"):
            correct_feed(fixed, self.accepted, self.source)


if __name__ == "__main__":
    unittest.main()
