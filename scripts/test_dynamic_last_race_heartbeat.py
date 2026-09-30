"""Boundary checks for the MORNING-bound PREDEADLINE continuation."""

from datetime import date, datetime
import unittest

from dynamic_last_race_heartbeat import decision


DAY = date(2026, 9, 29)


def runtime(deadline):
    return {"STATUS": "RELAY_PHASE_COMPLETE", "PHASE": "PREDEADLINE",
            "OPERATIONAL_DATE_JST": DAY.isoformat(),
            "LAST_RACE_DEADLINE_JST": f"2026-09-29T{deadline}:00+09:00"}


class HeartbeatPolicyTests(unittest.TestCase):
    def test_last_race_2045_stays_alive_until_private_handoff(self):
        for minute in ("20:44:59", "20:45:00"):
            expected = "CHECK_HANDOFF" if minute == "20:45:00" else "CONTINUE"
            self.assertEqual(decision(runtime("20:45"), DAY,
                                      datetime.fromisoformat(f"2026-09-29T{minute}+09:00")), expected)

    def test_2241_and_2245_never_stop_at_2110(self):
        for deadline in ("22:41", "22:45"):
            for minute in ("21:10:00", "22:40:59", f"{deadline}:00"):
                expected = "CHECK_HANDOFF" if minute == f"{deadline}:00" else "CONTINUE"
                self.assertEqual(decision(runtime(deadline), DAY,
                                          datetime.fromisoformat(f"2026-09-29T{minute}+09:00")), expected)

    def test_handoff_and_bounded_night_wait_stop_predeadline(self):
        now = datetime.fromisoformat("2026-09-29T22:41:00+09:00")
        self.assertEqual(decision({"STATUS": "NIGHT_HANDOFF_READY", "NIGHT_HANDOFF": True}, DAY, now), "HANDOFF")
        self.assertEqual(decision({"STATUS": "NIGHT_WAITING_OFFICIAL_RESULTS"}, DAY, now), "STOP_NIGHT")
        self.assertEqual(decision({"STATUS": "NIGHT_RESULTS_COMPLETE"}, DAY, now), "STOP_NIGHT")

    def test_rescue_day_stays_alive_until_its_verified_last_deadline(self):
        rescue_day = date(2026, 9, 30)
        rescue = {
            "STATUS": "SAME_DAY_RESCUE_COMPLETE",
            "PHASE": "PREDEADLINE_RESCUE",
            "OPERATIONAL_DATE_JST": rescue_day.isoformat(),
            "LAST_RACE_DEADLINE_JST": "2026-09-30T22:41:00+09:00",
            "CANONICAL_MORNING_PROMOTED": False,
            "NIGHT_RESEARCH_CONNECTED": False,
        }
        self.assertEqual(
            decision(rescue, rescue_day, datetime.fromisoformat("2026-09-30T22:40:59+09:00")),
            "CONTINUE",
        )
        self.assertEqual(
            decision(rescue, rescue_day, datetime.fromisoformat("2026-09-30T22:41:00+09:00")),
            "RESCUE_HANDOFF",
        )

    def test_rollover_hard_stop(self):
        for minute in ("01:59:59", "02:00:00"):
            expected = "CHECK_HANDOFF" if minute < "02:00:00" else "STOP_ROLLOVER"
            self.assertEqual(decision(runtime("22:41"), DAY,
                                      datetime.fromisoformat(f"2026-09-30T{minute}+09:00")), expected)
        self.assertEqual(decision({"STATUS": "NIGHT_HANDOFF_READY", "NIGHT_HANDOFF": True}, DAY,
                                  datetime.fromisoformat("2026-09-30T02:00:00+09:00")), "STOP_ROLLOVER")

    def test_no_unverified_deadline_and_no_wrong_date(self):
        now = datetime.fromisoformat("2026-09-29T21:10:00+09:00")
        with self.assertRaisesRegex(ValueError, "DEADLINE_MISSING"):
            decision({"STATUS": "RELAY_PHASE_COMPLETE", "PHASE": "PREDEADLINE",
                      "OPERATIONAL_DATE_JST": DAY.isoformat()}, DAY, now)
        with self.assertRaisesRegex(ValueError, "BINDING_DAY_MISMATCH"):
            decision(dict(runtime("22:41"), OPERATIONAL_DATE_JST="2026-09-28"), DAY, now)

    def test_legacy_day_keeps_2050_cutoff(self):
        old = date(2026, 9, 28)
        self.assertEqual(decision({}, old, datetime.fromisoformat("2026-09-28T20:49:59+09:00")), "CONTINUE")
        self.assertEqual(decision({}, old, datetime.fromisoformat("2026-09-28T20:50:00+09:00")), "STOP_LEGACY")
        self.assertEqual(decision({}, old, datetime.fromisoformat("2026-09-29T00:10:00+09:00")), "STOP_LEGACY")


if __name__ == "__main__":
    unittest.main()
