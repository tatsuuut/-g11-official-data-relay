"""Fault injection for missing scheduler and failed self-chain."""
from datetime import datetime
import unittest
from subprocess import CalledProcessError
from urllib.error import HTTPError
from unittest import mock

from predeadline_watchdog_v1 import (_last_deadline, decide, main,
    runner_retry_decision, morning_watchdog_decision,
    morning_predeadline_handoff_decision, RUNNER_NOT_ACQUIRED)

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


class MorningPredeadlineHandoffTests(unittest.TestCase):
    """No runtime execution: all reads and dispatches are isolated test doubles."""
    def setUp(self):
        self.now = datetime.fromisoformat("2026-10-11T06:31:47+09:00")
        self.feed = {
            "operational_date_jst": "2026-10-11", "stage": "MORNING", "status": "PASS",
            "daily_runtime": {"immutable": True, "operational_date_jst": "2026-10-11"},
            "counts": {"races": 1},
            "canonical_authorities": {"growth_p3": {"status": "LOCKED", "app_readback_status": "PASS"}},
            "races": [{"deadline_jst": "2026-10-11T08:35:00+09:00"}],
        }

    def decision(self, runs=None, jobs=None, at=None):
        return morning_predeadline_handoff_decision(
            self.feed, runs or [], lambda _: jobs or [], at or self.now)

    def exercise(self, feeds=None, run_reads=None, post_error=None, at=None):
        import copy
        feeds = list(feeds or [self.feed, self.feed])
        run_reads = list(run_reads or [[], []])
        original = copy.deepcopy(feeds)
        def get(url, token=None):
            if "/api/g11-feed?date=" in url:
                return feeds.pop(0)
            if "/jobs?" in url:
                return {"jobs": []}
            return {"workflow_runs": run_reads.pop(0)}
        with mock.patch.dict("os.environ", {
            "GH_TOKEN": "test", "GITHUB_REPOSITORY": "tatsuuut/-g11-official-data-relay",
            "G11_APP_ORIGIN": "https://example.invalid", "GITHUB_EVENT_NAME": "schedule"
        }), mock.patch("predeadline_watchdog_v1._get", side_effect=get), \
             mock.patch("predeadline_watchdog_v1._post", side_effect=post_error) as post:
            if post_error:
                with self.assertRaises(type(post_error)):
                    main(at or self.now)
            else:
                main(at or self.now)
            calls = post.call_args_list
        self.assertEqual(self.feed, original[0])
        return calls

    def test_observed_0631_gap_seeds_existing_predeadline_once(self):
        self.assertEqual(self.decision(), "NO_CRITICAL_RUN")
        calls = self.exercise()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args, (
            "https://api.github.com/repos/tatsuuut/-g11-official-data-relay/actions/workflows/g11-free-runner.yml/dispatches",
            "test", {"ref": "main", "inputs": {"phase": "predeadline", "operational_date": "2026-10-11"}}))

    def test_handoff_window_preserves_morning_cutoff_and_night(self):
        for at in ("05:59:59", "08:00:00", "23:34:00"):
            with self.subTest(at=at):
                self.assertEqual(self.decision(at=datetime.fromisoformat("2026-10-11T"+at+"+09:00")), "OUTSIDE_HANDOFF_WINDOW")
        for at in ("06:00:00", "07:35:00", "07:59:59"):
            with self.subTest(at=at):
                now = datetime.fromisoformat("2026-10-11T"+at+"+09:00")
                self.assertEqual(self.decision(at=now), "NO_CRITICAL_RUN")
                self.assertEqual(len(self.exercise(at=now)), 1)
        self.assertEqual(self.decision(at=datetime.fromisoformat("2026-10-11T06:31:47+00:00")), "OUTSIDE_HANDOFF_WINDOW")

    def test_every_active_state_suppresses_duplicate(self):
        for status in ("queued", "in_progress", "requested", "waiting", "pending"):
            with self.subTest(status=status):
                self.assertEqual(self.decision(runs=[{"status": status}]), "ACTIVE_CAPTURE")
                self.assertEqual(self.exercise(run_reads=[[{"status": status}]]), [])

    def test_recheck_observes_parallel_chain_and_does_not_post(self):
        self.assertEqual(self.exercise(run_reads=[[], [{"status": "pending"}]]), [])

    def test_previous_day_history_does_not_consume_watchdog_budget(self):
        jobs = mock.Mock(side_effect=AssertionError("historical jobs must not be fetched"))
        runs = [{"id": i, "status": "completed", "created_at": "2026-10-10T10:00:00Z"}
                for i in range(100)]
        self.assertEqual(morning_predeadline_handoff_decision(self.feed, runs, jobs, self.now), "NO_CRITICAL_RUN")
        jobs.assert_not_called()
        runs.append({"id": 101, "status": "pending", "created_at": "2026-10-10T10:00:00Z"})
        self.assertEqual(morning_predeadline_handoff_decision(self.feed, runs, jobs, self.now), "ACTIVE_CAPTURE")

    def test_second_watchdog_run_is_idempotent_with_pending_chain(self):
        self.assertEqual(len(self.exercise()), 1)
        self.assertEqual(self.exercise(run_reads=[[{"status": "pending"}]]), [])

    def test_recent_checkpoint_defers_and_stale_checkpoint_recovers(self):
        runs = [{"id": 1, "status": "completed"}]
        def jobs(at):
            return [{"steps": [{"name": "Save early encrypted critical checkpoint",
                                "conclusion": "success", "completed_at": at}]}]
        self.assertEqual(self.decision(runs, jobs("2026-10-10T21:25:47Z")), "RECENT_CRITICAL")
        self.assertEqual(self.decision(runs, jobs("2026-10-10T21:25:46Z")), "STALE_CRITICAL")
        self.assertEqual(self.decision(runs, jobs("2026-10-09T21:31:00Z")), "NO_CRITICAL_RUN")

    def test_wrong_date_or_missing_authority_is_not_accepted(self):
        import copy
        original = copy.deepcopy(self.feed)
        for mutate in (
            lambda f: f.update(operational_date_jst="2026-10-10"),
            lambda f: f.update(stage="NIGHT"),
            lambda f: f.update(status="FAIL"),
            lambda f: f.update(daily_runtime=None),
            lambda f: f["daily_runtime"].update(immutable=False),
            lambda f: f["daily_runtime"].update(operational_date_jst="2026-10-10"),
            lambda f: f.update(canonical_authorities=None),
            lambda f: f["canonical_authorities"]["growth_p3"].update(status="UNLOCKED"),
            lambda f: f["canonical_authorities"]["growth_p3"].update(app_readback_status="FAIL"),
            lambda f: f["counts"].update(races=2),
            lambda f: f.update(races=[]),
            lambda f: f["races"][0].update(deadline_jst="2026-10-10T08:35:00+09:00"),
            lambda f: f["races"][0].update(deadline_jst="2026-10-11T08:35:00+00:00"),
            lambda f: f["races"][0].update(deadline_jst="invalid"),
        ):
            self.feed = copy.deepcopy(original)
            mutate(self.feed)
            self.assertEqual(self.decision(), "MORNING_NOT_VERIFIED")

    def test_first_deadline_is_exclusive(self):
        self.feed["races"][0]["deadline_jst"] = self.now.isoformat()
        self.assertEqual(self.decision(), "FIRST_DEADLINE_REACHED")
        self.assertEqual(self.exercise(), [])

    def test_recheck_feed_change_prevents_dispatch(self):
        import copy
        changed = copy.deepcopy(self.feed)
        changed["stage"] = "NIGHT"
        self.assertEqual(self.exercise(feeds=[self.feed, changed]), [])

    def test_ambiguous_post_failure_is_never_retried(self):
        self.assertEqual(len(self.exercise(post_error=TimeoutError("dispatch response unavailable"))), 1)

    def test_transport_access_failure_never_dispatches(self):
        for code in (403, 429):
            with mock.patch.dict("os.environ", {
                "GH_TOKEN": "test", "GITHUB_REPOSITORY": "tatsuuut/-g11-official-data-relay",
                "G11_APP_ORIGIN": "https://example.invalid", "GITHUB_EVENT_NAME": "schedule"
            }), mock.patch("predeadline_watchdog_v1._get", side_effect=HTTPError("url", code, "blocked", {}, None)), \
                 mock.patch("predeadline_watchdog_v1._post") as post:
                with self.assertRaises(HTTPError):
                    main(self.now)
                post.assert_not_called()

    def test_absent_morning_uses_original_recovery_not_predeadline(self):
        with mock.patch.dict("os.environ", {
            "GH_TOKEN": "test", "GITHUB_REPOSITORY": "tatsuuut/-g11-official-data-relay",
            "G11_APP_ORIGIN": "https://example.invalid", "GITHUB_EVENT_NAME": "schedule"
        }), mock.patch("predeadline_watchdog_v1._get", side_effect=[{}, {"workflow_runs": []}, {"workflow_runs": []}]), \
             mock.patch("predeadline_watchdog_v1._post") as post:
            main(self.now)
            self.assertEqual(post.call_count, 1)
            self.assertEqual(post.call_args.args[2]["inputs"]["phase"], "morning")


class SiteTransportTests(unittest.TestCase):
    def test_site_uses_existing_hash_verified_readback(self):
        import predeadline_watchdog_v1 as watchdog
        from unittest.mock import patch
        with patch('hot_delta_transport_v1.fetch', return_value=({'status': 'PASS'}, {}, b'{}')) as fetch:
            self.assertEqual(watchdog._get('https://g11-race-lab.higatatsunori2.chatgpt.site/api/g11-feed?date=2026-10-07'), {'status': 'PASS'})
            fetch.assert_called_once()

    def test_only_missing_day_is_recoverable_not_access_failure(self):
        import predeadline_watchdog_v1 as watchdog
        from unittest.mock import patch
        with patch('hot_delta_transport_v1.fetch', side_effect=CalledProcessError(22, 'curl')), patch.object(watchdog.Path, 'exists', return_value=True):
            for status in (403, 404):
                with patch.object(watchdog.Path, 'read_text', return_value=f'HTTP/2 {status}\n'):
                    with self.assertRaises(HTTPError if status == 404 else RuntimeError):
                        watchdog._get('https://g11-race-lab.higatatsunori2.chatgpt.site/api/g11-feed?date=2026-10-07')

    def test_network_timeout_never_reuses_stale_404_headers(self):
        import predeadline_watchdog_v1 as watchdog
        from unittest.mock import patch
        with patch('hot_delta_transport_v1.fetch', side_effect=CalledProcessError(28, 'curl')), \
             patch.object(watchdog.Path, 'exists', return_value=True), \
             patch.object(watchdog.Path, 'read_text', return_value='HTTP/2 404\n'):
            with self.assertRaisesRegex(RuntimeError, 'WATCHDOG_SITE_READBACK_FAILED'):
                watchdog._get('https://g11-race-lab.higatatsunori2.chatgpt.site/api/g11-feed?date=2026-10-07')

if __name__ == "__main__":
    unittest.main()



class DaytimeMissingMorningTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.fromisoformat('2026-10-22T09:00:00+09:00')
        self.env = {'GH_TOKEN':'test','GITHUB_REPOSITORY':'tatsuuut/-g11-official-data-relay',
                    'G11_APP_ORIGIN':'https://example.invalid','GITHUB_EVENT_NAME':'schedule'}
    def run_missing(self, feeds, runs):
        reads=iter(feeds)
        def get(url, token=None):
            if '/api/g11-feed' in url:
                value=next(reads)
                if isinstance(value, Exception): raise value
                return value
            return {'workflow_runs':runs}
        with mock.patch.dict('os.environ',self.env), mock.patch('predeadline_watchdog_v1._get',side_effect=get), mock.patch('predeadline_watchdog_v1._post') as post:
            main(self.now)
        return post
    def test_missing_feed_404_recovers_once_with_current_day_and_main(self):
        error=HTTPError('url',404,'missing',{},None)
        post=self.run_missing([error,error],[])
        self.assertEqual(post.call_count,1)
        self.assertEqual(post.call_args.args[2],{'ref':'main','inputs':{'phase':'same-day-rescue','operational_date':'2026-10-22'}})
    def test_active_or_feed_appearing_on_recheck_never_dispatches(self):
        self.assertEqual(self.run_missing([{}],[{'status':'queued'}]).call_count,0)
        self.assertEqual(self.run_missing([{},feed()],[]).call_count,0)
    def test_non404_errors_never_become_missing_morning(self):
        with self.assertRaises(HTTPError): self.run_missing([HTTPError('url',503,'unavailable',{},None)],[])
    def test_two_attempts_cooldown_and_history_truncation(self):
        from predeadline_watchdog_v1 import daytime_recovery_decision as decide_recovery
        row={'status':'completed','event':'workflow_dispatch','display_title':'LIVE-same-day-rescue-2026-10-22',
             'created_at':'2026-10-21T23:00:00Z','updated_at':'2026-10-21T23:59:00Z'}
        self.assertEqual(decide_recovery({},[row],self.now),'RESCUE_RETRY_COOLDOWN')
        self.assertEqual(decide_recovery({},[row,row],self.now),'RESCUE_RETRY_LIMIT')
        self.assertEqual(decide_recovery({},[row]*100,self.now),'RECOVERY_HISTORY_INCOMPLETE')
        row['updated_at']='2026-10-21T23:40:00Z'
        self.assertEqual(decide_recovery({},[row],self.now),'DISPATCH_SAME_DAY_RESCUE')
        self.assertEqual(decide_recovery({},[],self.now.replace(hour=22)),'OUTSIDE_RESCUE_WINDOW')
    def test_saved_rescue_is_monitored_without_canonical_promotion(self):
        f=feed();f.pop('canonical_authorities');f['counts']['formal']=0
        f['SNAPSHOT_CLASS']='SAME_DAY_RESCUE_BOUNDARY'
        f['same_day_rescue']={'operational_date_jst':DAY,'immutable':True,'canonical_morning_promoted':False,
                             'research_eligible':False,'same_day_results_used':False}
        self.assertEqual(_last_deadline(f,DAY).hour,22)
        f['same_day_rescue']['canonical_morning_promoted']=True
        with self.assertRaises(ValueError): _last_deadline(f,DAY)
