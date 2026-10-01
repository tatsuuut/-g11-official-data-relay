"""Fault injection for missing scheduler and failed self-chain."""
from datetime import datetime
import unittest
from unittest import mock

from predeadline_watchdog_v1 import _last_deadline, decide, main

DAY = "2026-10-01"
NOW = datetime.fromisoformat("2026-10-01T18:20:00+09:00")


def feed():
    return {"operational_date_jst": DAY, "stage": "PREDEADLINE", "status": "PASS",
            "counts": {"races": 1},
            "canonical_authorities": {"growth_p3": {"status": "LOCKED"}},
            "races": [{"deadline_jst": "2026-10-01T22:41:00+09:00"}]}


class WatchdogTests(unittest.TestCase):
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
