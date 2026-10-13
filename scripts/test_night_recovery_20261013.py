"""Saved incident fixtures are local evidence, never committed to the public repo."""
from copy import deepcopy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from night_recovery_20261013 import correct_feed, fetch_accepted, KEY


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
