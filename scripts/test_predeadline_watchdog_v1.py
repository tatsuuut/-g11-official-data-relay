"""Fault injection for missing scheduler and failed self-chain."""
from datetime import datetime
import unittest
from unittest import mock

from predeadline_watchdog_v1 import (_last_deadline, decide, main,
    runner_retry_decision, morning_watchdog_decision, RUNNER_NOT_ACQUIRED)

DAY = "2026-10-01"
NOW = datetime.fromisoformat("2026-10-01T18:20:00+09:00")


def feed():
    return {"operational_date_jst": DAY, "stage": "PREDEADLINE", "status": "PASS",
            "counts": {"races": 1},
            "canonical_authorities": {"growth_p3": {"status": "LOCKED"}},
            "races": [{"deadline_jst": "2026-10-01T22:41:00+09:00"}]}


class WatchdogTests(unittest.TestCase):
    def allocation_case(self, path="g11-free-runner.yml"):
        return ({"id": 37370139190, "head_branch": "main",
                 "head_repository": {"full_name": "tatsuuut/-g11-official-data-relay"},
                 "path": ".github/workflows/" + path, "status": "completed",
                 "conclusion": "failure", "created_at": "2026-10-05T20:30:16Z", "run_attempt": 1},
                [{"conclusion": "cancelled", "runner_id": 0, "steps": []}],
                [{"message": RUNNER_NOT_ACQUIRED}])

    def retry(self, run, jobs, annotations, at="2026-10-06T05:46:00+09:00"):
        return runner_retry_decision(run, jobs, annotations, datetime.fromisoformat(at),
                                     "tatsuuut/-g11-official-data-relay")

    def test_observed_no_runner_failure_and_completion_dispatch(self):
        for path in ("g11-free-runner.yml", "g11-explicit-night-dispatcher.yml"):
            self.assertEqual(self.retry(*self.allocation_case(path)), "RETRY_ALLOCATION")

    def test_slow_morning_source_timeout_restore_and_decrypt_not_cancelled(self):
        for step in ("Restore only encrypted runtime state", "Decrypt and validate restored state",
                     "Run the private fail-closed relay"):
            run, jobs, annotations = self.allocation_case()
            jobs[0].update(runner_id=1, steps=[{"name": step, "conclusion": "failure"}])
            self.assertEqual(self.retry(run, jobs, annotations), "NOT_RUNNER_ALLOCATION_FAILURE")
        run, jobs, annotations = self.allocation_case()
        run.update(status="in_progress", conclusion=None)
        self.assertEqual(self.retry(run, jobs, annotations), "NOT_FAILED")

    def test_retry_requires_annotation_and_is_bounded(self):
        run, jobs, annotations = self.allocation_case()
        self.assertEqual(self.retry(run, jobs, []), "ALLOCATION_EVIDENCE_MISSING")
        run["run_attempt"] = 3
        self.assertEqual(self.retry(run, jobs, annotations), "RETRY_LIMIT")

    def test_wild4_failure_isolation_and_wrong_repo(self):
        self.assertEqual(self.retry(*self.allocation_case("g11-morning-odds-footprint-4bet-shadow-v1.yml")), "UNTRUSTED_RUN")
        run, jobs, annotations = self.allocation_case()
        run["head_repository"]["full_name"] = "someone/fork"
        self.assertEqual(self.retry(run, jobs, annotations), "UNTRUSTED_RUN")

    def test_no_late_or_previous_day_replay(self):
        self.assertEqual(self.retry(*self.allocation_case(), at="2026-10-06T07:45:00+09:00"), "STALE_RUN")
        self.assertEqual(self.retry(*self.allocation_case(), at="2026-10-07T05:46:00+09:00"), "STALE_RUN")
        run, jobs, annotations = self.allocation_case()
        run["created_at"] = "2026-10-05T22:30:16Z"
        self.assertEqual(self.retry(run, jobs, annotations, at="2026-10-06T07:46:00+09:00"), "MORNING_WINDOW_CLOSED")

    def test_normal_morning_is_idempotent_previous_night_is_not_today(self):
        now = datetime.fromisoformat("2026-10-06T05:46:00+09:00")
        today = {"operational_date_jst": "2026-10-06", "status": "PASS", "stage": "MORNING", "daily_runtime": {"immutable": True}}
        self.assertEqual(morning_watchdog_decision(today, [], now), "MORNING_ALREADY_PUBLISHED")
        today.update(operational_date_jst="2026-10-05", stage="NIGHT")
        self.assertEqual(morning_watchdog_decision(today, [], now), "DISPATCH_MORNING")
        self.assertEqual(morning_watchdog_decision({}, [{"status": "in_progress"}], now), "ACTIVE_RUNTIME")

    def test_morning_retry_limit_and_closed_window(self):
        now = datetime.fromisoformat("2026-10-06T05:46:00+09:00")
        runs = [{"event": "workflow_dispatch", "created_at": "2026-10-05T20:00:00Z", "status": "completed"}] * 3
        self.assertEqual(morning_watchdog_decision({}, runs, now), "MORNING_RETRY_LIMIT")
        self.assertEqual(morning_watchdog_decision({}, [], datetime.fromisoformat("2026-10-06T08:00:00+09:00")), "OUTSIDE_MORNING_WINDOW")

    def test_verified_last_race_and_missing_scheduler(self):
        self.assertEqual(_last_deadline(feed(), DAY).hour, 22)
        self.assertEqual(decide([], lambda _: [], NOW, DAY), "NO_CRITICAL_RUN")

    def test_active_capture_suppresses_duplicate(self):
        self.assertEqual(decide([{"status": "queued"}], lambda _: [], NOW, DAY), "ACTIVE_CAPTURE")

    def test_stale_checkpoint_or_self_chain_failure_recovers(self):
        runs = [{"id": 1, "status": "completed", "display_title": "LIVE-predeadline-2026-10-01"}]
        jobs = [{"steps": [{"name": "Save early encrypted critical checkpoint",
                            "conclusion": "success", "completed_at": "2026-10-01T09:00:00Z"}]}]
        self.assertEqual(decide(runs, lambda _: jobs, NOW, DAY), "STALE_CRITICAL")

    def test_dispatch_after_missing_schedule_and_no_active_capture(self):
        def get(url, token=None):
            if "/api/g11-feed" in url:
                return feed()
            if url.endswith("/jobs?per_page=100"):
                return {"jobs": []}
            return {"workflow_runs": []}

        class Accepted:
            status = 204
            def __enter__(self): return self
            def __exit__(self, *args): return None

        with mock.patch.dict("os.environ", {"GH_TOKEN": "test", "GITHUB_REPOSITORY": "tatsuuut/-g11-official-data-relay",
                                          "G11_APP_ORIGIN": "https://example.invalid"}), \
             mock.patch("predeadline_watchdog_v1._get", side_effect=get), \
             mock.patch("predeadline_watchdog_v1.urlopen", return_value=Accepted()) as post:
            main(NOW)
            self.assertEqual(post.call_count, 1)
            self.assertIn("/dispatches", post.call_args.args[0].full_url)


if __name__ == "__main__":
    unittest.main()
