import copy
import unittest

from verify_v53_readback import audit_c1, verify_c1_record, C1_SOURCE, C1_VERSION, C1_MARKER, E1_VERSION

DAY = "2026-10-30"
KEY = "20261030-14-01"


def fixture():
    decision = {"BUY_SET": ["1-2-3"], "BUY_COUNT": 1, "MAIN_HEAD": 1}
    morning_result = {"STATUS": "READY", "SNAPSHOT_TYPE": "MORNING", "RACE_ID": KEY,
        "RACE_DATE": DAY, "LOCKED_AT": DAY + "T04:00:00+09:00", "DECISION": decision,
        "CANDIDATE_INPUT": {C1_MARKER: C1_VERSION, "IMITATION_BASELINE_VERSION": E1_VERSION}}
    morning = {"SNAPSHOT_SHA256": "a" * 64, "SNAPSHOT": {"PAYLOAD": {
        "FORMAL_USE": True, "RACE_KEY": KEY, "RESULT": morning_result}}}
    beforeinfo = {"SNAPSHOT_SHA256": "b" * 64, "SNAPSHOT": {"PAYLOAD": {
        "RACE_KEY": KEY, "KIND": "BEFOREINFO", "STATUS": "PREDEADLINE_SOURCE_COMPLETE",
        "CAPTURED_AT_JST": DAY + "T08:28:00+09:00"}}}
    live_result = {"STATUS": "READY", "SNAPSHOT_TYPE": "PRE_RACE", "RACE_ID": KEY,
        "RACE_DATE": DAY, "LOCKED_AT": DAY + "T08:28:01+09:00", "DECISION": decision,
        "PRE_RACE_DATA_CAPTURED_AT": DAY + "T08:28:00+09:00", "RIGHT_TRANSITION_AUDIT": {
            C1_MARKER: C1_VERSION, "COMBINATION_AUDIT": {
                "CONFIG_ID": "INSIDE_CLAIM|SEMANTIC4|E|ONE|E", "LABELS_USED": False,
                "OFFICIAL_RESULTS_USED": False}, "E1_BASELINE_AUDIT": {
                    "REFERENCE_ONLY_NOT_FORMAL_BET": True,
                    "IMITATION_BASELINE_VERSION": E1_VERSION, "BUY_SET": ["2-1-3"]}}}
    live = {"SNAPSHOT_SHA256": "c" * 64, "SNAPSHOT": {"PAYLOAD": {
        "FORMAL_USE": True, "RACE_KEY": KEY, "RESULT": live_result,
        "MORNING_V53_SNAPSHOT_SHA256": "a" * 64, "BEFOREINFO_SNAPSHOT_SHA256": "b" * 64,
        "P3_INPUT_USED": False, "WILD_INPUT_USED": False, "RESULT_FEEDBACK_TO_MODEL": False}}}
    race = {"key": KEY, "date_jst": DAY, "deadline_jst": DAY + "T08:35:00+09:00",
        "formal_status": "FORMAL", "research_eligible": True,
        "abeken_shadow": {"v53": {"formal_use": True,
            "morning": {"snapshot_sha256": "a" * 64, "practical_bets": ["1-2-3"], "buy_count": 1},
            "live": {"snapshot_sha256": "c" * 64, "practical_bets": ["1-2-3"], "buy_count": 1,
                "main_head": 1, "locked_at_jst": DAY + "T08:28:01+09:00"}}}}
    return race, morning, live, beforeinfo


class SavedReadbackTest(unittest.TestCase):
    def test_valid_pair_and_no_input_mutation(self):
        values = fixture()
        before = copy.deepcopy(values)
        proof = verify_c1_record(DAY, *values)
        self.assertEqual(proof["live_status"], "PASS")
        self.assertFalse(proof["same_bets"])
        self.assertEqual(len(proof["e1_reference_sha256"]), 64)
        self.assertEqual(values, before)

    def test_source_version_alone_does_not_prove_c1(self):
        values = fixture()
        del values[1]["SNAPSHOT"]["PAYLOAD"]["RESULT"]["CANDIDATE_INPUT"][C1_MARKER]
        with self.assertRaisesRegex(ValueError, "MORNING_MARKER"):
            verify_c1_record(DAY, *values)

    def test_wrong_live_marker_is_rejected(self):
        values = fixture()
        values[2]["SNAPSHOT"]["PAYLOAD"]["RESULT"]["RIGHT_TRANSITION_AUDIT"][C1_MARKER] = "OLD"
        with self.assertRaisesRegex(ValueError, "LIVE_MARKER"):
            verify_c1_record(DAY, *values)

    def test_e1_reference_must_be_saved(self):
        values = fixture()
        del values[2]["SNAPSHOT"]["PAYLOAD"]["RESULT"]["RIGHT_TRANSITION_AUDIT"]["E1_BASELINE_AUDIT"]
        with self.assertRaisesRegex(ValueError, "E1_REFERENCE"):
            verify_c1_record(DAY, *values)

    def test_result_dependent_comparison_is_rejected(self):
        values = fixture()
        values[2]["SNAPSHOT"]["PAYLOAD"]["RESULT"]["RIGHT_TRANSITION_AUDIT"]["COMBINATION_AUDIT"]["OFFICIAL_RESULTS_USED"] = True
        with self.assertRaisesRegex(ValueError, "E1_REFERENCE"):
            verify_c1_record(DAY, *values)

    def test_public_bets_must_match_saved_source(self):
        values = fixture()
        values[0]["abeken_shadow"]["v53"]["live"]["practical_bets"] = ["1-3-2"]
        with self.assertRaisesRegex(ValueError, "LIVE_READBACK"):
            verify_c1_record(DAY, *values)

    def test_post_deadline_lock_is_rejected(self):
        values = fixture()
        values[2]["SNAPSHOT"]["PAYLOAD"]["RESULT"]["LOCKED_AT"] = DAY + "T08:35:00+09:00"
        with self.assertRaisesRegex(ValueError, "PREDEADLINE_BINDING"):
            verify_c1_record(DAY, *values)

    def test_different_morning_cannot_be_joined(self):
        values = fixture()
        values[2]["SNAPSHOT"]["PAYLOAD"]["MORNING_V53_SNAPSHOT_SHA256"] = "d" * 64
        with self.assertRaisesRegex(ValueError, "LIVE_MARKER"):
            verify_c1_record(DAY, *values)

    def test_pending_live_is_not_zero_or_success(self):
        race, morning, _, _ = fixture()
        race["abeken_shadow"]["v53"]["live"] = None
        proof = verify_c1_record(DAY, race, morning)
        self.assertEqual(proof["live_status"], "NOT_YET_OBSERVED")
        self.assertNotIn("c1_points", proof)

    def test_other_source_does_not_claim_c1(self):
        proof = audit_c1(DAY, {"daily_runtime": {"runtime_source_sha": "old"}},
            lambda *_: self.fail("Older source must not be loaded as C1"))
        self.assertEqual(proof["status"], "OUTSIDE_PINNED_C1_SOURCE")

    def test_snapshot_failure_is_reported_without_interrupting_runtime(self):
        race, _, _, _ = fixture()
        feed = {"operational_date_jst": DAY, "daily_runtime": {"runtime_source_sha": C1_SOURCE},
            "races": [race]}
        def broken(*_):
            raise ValueError("SNAPSHOT_HASH_MISMATCH")
        proof = audit_c1(DAY, feed, broken)
        self.assertEqual(proof["status"], "FAIL")
        self.assertEqual(proof["live_verified"], 0)
        self.assertEqual(proof["source_writes"], 0)
        self.assertEqual(proof["failures"][0]["race_key"], KEY)

    def test_reference_records_are_not_counted_as_formal_eligible(self):
        race, _, _, _ = fixture()
        reference = copy.deepcopy(race)
        reference.update(key="20261030-14-02", formal_status="EXCLUDED", research_eligible=False)
        feed = {"operational_date_jst": DAY, "daily_runtime": {"runtime_source_sha": C1_SOURCE},
            "races": [race, reference]}
        def unavailable(*_):
            raise FileNotFoundError("No source in this count-only fixture")
        proof = audit_c1(DAY, feed, unavailable)
        self.assertEqual(proof["v53_source_records"], 2)
        self.assertEqual(proof["formal_eligible_records"], 1)
        self.assertEqual(len(proof["failures"]), 2)


if __name__ == "__main__":
    unittest.main()
