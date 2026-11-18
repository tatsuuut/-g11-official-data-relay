"""No-model watchdog for the encrypted PREDEADLINE critical checkpoint."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import re
import subprocess
import sys
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from pathlib import Path

JST = timezone(timedelta(hours=9))
MAX_AGE = timedelta(minutes=6)
ACTIVE = {"queued", "in_progress", "requested", "waiting", "pending"}
RUNNER_NOT_ACQUIRED = "The job was not acquired by Runner of type hosted even after multiple attempts"
MAX_RUNNER_ATTEMPTS = 3
MORNING_ARM_LEG_SECONDS = 3 * 60 * 60
MORNING_ARM_MARGIN = timedelta(minutes=20)
REPOSITORY = "tatsuuut/-g11-official-data-relay"


def runner_retry_decision(run: dict, jobs: list[dict], annotations: list[dict],
                          now: datetime, repository: str) -> str:
    """Retry allocation failures only; never cancel a running or slow process."""
    if (run.get("head_branch") != "main" or
            run.get("head_repository", {}).get("full_name") != repository or
            run.get("path") not in {".github/workflows/g11-free-runner.yml",
                                    ".github/workflows/g11-explicit-night-dispatcher.yml"}):
        return "UNTRUSTED_RUN"
    if run.get("status") != "completed" or run.get("conclusion") != "failure":
        return "NOT_FAILED"
    failed = [job for job in jobs if job.get("conclusion") not in {"success", "skipped"}]
    if not failed or any(job.get("steps") or job.get("runner_id") not in {0, None}
                         for job in failed):
        return "NOT_RUNNER_ALLOCATION_FAILURE"
    if not any(row.get("message") == RUNNER_NOT_ACQUIRED for row in annotations):
        return "ALLOCATION_EVIDENCE_MISSING"
    if int(run.get("run_attempt", 1)) >= MAX_RUNNER_ATTEMPTS:
        return "RETRY_LIMIT"
    created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00")).astimezone(JST)
    if now - created > timedelta(hours=2) or now.date() != created.date():
        return "STALE_RUN"
    if run["path"].endswith("g11-free-runner.yml"):
        if not (2 <= created.hour < 8 and 2 <= now.hour < 8) or (now.hour == 7 and now.minute >= 45):
            return "MORNING_WINDOW_CLOSED"
    return "RETRY_ALLOCATION"


def _post(url: str, token: str, payload: dict | None = None) -> None:
    request = Request(url, data=json.dumps(payload or {}).encode(), method="POST",
                      headers={"Authorization": f"Bearer {token}",
                               "Accept": "application/vnd.github+json",
                               "X-GitHub-Api-Version": "2022-11-28",
                               "Content-Type": "application/json"})
    # Never retry a POST after an ambiguous network response.
    with urlopen(request, timeout=20) as response:
        if response.status not in {201, 204}:
            raise ValueError("WATCHDOG_DISPATCH_NOT_ACCEPTED")


def recover_runner_event(now: datetime, token: str, repository: str) -> bool:
    if os.environ.get("GITHUB_EVENT_NAME") != "workflow_run":
        return False
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    event_run = event.get("workflow_run", {})
    run_id = event_run.get("id")
    if type(run_id) is not int:
        raise ValueError("WATCHDOG_RUN_ID")
    base = f"https://api.github.com/repos/{repository}"
    run = _get(f"{base}/actions/runs/{run_id}", token)
    jobs = _get(f"{base}/actions/runs/{run_id}/jobs?per_page=100", token)["jobs"]
    annotations = []
    for job in jobs:
        if job.get("conclusion") in {"failure", "cancelled"} and not job.get("steps"):
            annotations.extend(_get(f"{base}/check-runs/{job['id']}/annotations", token))
    decision = runner_retry_decision(run, jobs, annotations, now, repository)
    if decision == "RETRY_ALLOCATION":
        _post(f"{base}/actions/runs/{run_id}/rerun-failed-jobs", token)
    print("G11_RUNNER_RECOVERY=" + decision + f" run={run_id} attempt={run.get('run_attempt')}")
    return True


def morning_watchdog_decision(feed: dict, runs: list[dict], now: datetime) -> str:
    day = now.date().isoformat()
    if not (2 <= now.hour < 8) or (now.hour == 2 and now.minute < 15) or (now.hour == 7 and now.minute >= 35):
        return "OUTSIDE_MORNING_WINDOW"
    if any(run.get("status") in ACTIVE for run in runs):
        return "ACTIVE_RUNTIME"
    if (feed.get("operational_date_jst") == day and feed.get("status") == "PASS"
            and feed.get("stage") in {"MORNING", "PREDEADLINE"}
            and isinstance(feed.get("daily_runtime"), dict)):
        return "MORNING_ALREADY_PUBLISHED"
    attempts = [run for run in runs if run.get("event") == "workflow_dispatch"
                and str(run.get("created_at", "")) >= (now.replace(hour=2, minute=0, second=0, microsecond=0)
                                                        .astimezone(timezone.utc).isoformat().replace("+00:00", "Z"))]
    if len(attempts) >= 3:
        return "MORNING_RETRY_LIMIT"
    return "DISPATCH_MORNING"


def morning_predeadline_handoff_decision(feed: dict, runs: list[dict], jobs_for_run,
                                        now: datetime) -> str:
    """Seed the existing self-chain from the already scheduled morning watchdog."""
    if now.utcoffset() != timedelta(hours=9) or not 6 <= now.hour < 8:
        return "OUTSIDE_HANDOFF_WINDOW"
    day = now.date().isoformat()
    authority = feed.get("daily_runtime")
    canonical = feed.get("canonical_authorities")
    p3 = canonical.get("growth_p3") if isinstance(canonical, dict) else None
    if (not isinstance(authority, dict) or authority.get("immutable") is not True
            or authority.get("operational_date_jst") != day
            or not isinstance(p3, dict) or p3.get("app_readback_status") != "PASS"):
        return "MORNING_NOT_VERIFIED"
    try:
        _last_deadline(feed, day)
        first = min(datetime.fromisoformat(row["deadline_jst"]) for row in feed["races"])
    except (KeyError, TypeError, ValueError):
        return "MORNING_NOT_VERIFIED"
    if now >= first:
        return "FIRST_DEADLINE_REACHED"
    # Reuse the current active-run and six-minute checkpoint guards. The
    # private critical lane retains ownership of per-race timing and locks.
    # Do not spend the three-minute supervisor budget reading yesterday's
    # completed jobs. Keep every active run, including a rollover run.
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    since = start.isoformat().replace("+00:00", "Z")
    current = [run for run in runs if run.get("status") in ACTIVE
               or str(run.get("created_at", since)) >= since]
    return decide(current, jobs_for_run, now, day)


def recover_morning(now: datetime, token: str, repository: str, origin: str) -> None:
    day = now.date().isoformat()
    base = f"https://api.github.com/repos/{repository}/actions/workflows/g11-free-runner.yml"
    try:
        feed = _get(f"{origin}/api/g11-feed?date={day}")
    except HTTPError as exc:
        if exc.code != 404:
            raise
        feed = {}
    runs = _get(base + "/runs?per_page=100", token)["workflow_runs"]
    decision = morning_watchdog_decision(feed, runs, now)
    if decision == "DISPATCH_MORNING":
        runs = _get(base + "/runs?per_page=100", token)["workflow_runs"]
        decision = morning_watchdog_decision(feed, runs, now)
        if decision == "DISPATCH_MORNING":
            _post(base + "/dispatches", token,
                  {"ref": "main", "inputs": {"phase": "morning", "operational_date": day}})
    elif decision in {"MORNING_ALREADY_PUBLISHED", "OUTSIDE_MORNING_WINDOW"}:
        def jobs(run_id):
            return _get(f"https://api.github.com/repos/{repository}/actions/runs/{run_id}/jobs?per_page=100", token)["jobs"]
        handoff = morning_predeadline_handoff_decision(feed, runs, jobs, now)
        if handoff in {"NO_CRITICAL_RUN", "STALE_CRITICAL"}:
            # Re-read both bindings immediately before the single POST.
            # Never retry a POST after an ambiguous network response.
            feed = _get(f"{origin}/api/g11-feed?date={day}")
            runs = _get(base + "/runs?per_page=100", token)["workflow_runs"]
            handoff = morning_predeadline_handoff_decision(feed, runs, jobs, now)
            if handoff in {"NO_CRITICAL_RUN", "STALE_CRITICAL"}:
                _post(base + "/dispatches", token,
                      {"ref": "main", "inputs": {"phase": "predeadline", "operational_date": day}})
                handoff = "DISPATCH_ACCEPTED reason=" + handoff
        print("G11_MORNING_PREDEADLINE_HANDOFF=" + handoff)
    print("G11_MORNING_WATCHDOG=" + decision)


def _get(url: str, token: str | None = None) -> dict:
    if url.startswith("https://g11-race-lab.higatatsunori2.chatgpt.site/api/g11-feed?"):
        # Reuse the production GET/readback transport and its payload-hash
        # validation. GitHub API headers are not a Site API request contract.
        from hot_delta_transport_v1 import fetch
        from subprocess import CalledProcessError
        Path('.relay-output').mkdir(exist_ok=True)
        try:
            return fetch(url)[0]
        except CalledProcessError as exc:
            headers = Path('.relay-output/hot-get-headers.txt')
            status = [line.split()[1] for line in headers.read_text().splitlines()
                      if line.startswith('HTTP/') and len(line.split()) >= 2] if headers.exists() else []
            if exc.returncode == 22 and status and status[-1] == '404':
                raise HTTPError(url, 404, 'DAY_FEED_NOT_YET_PUBLISHED', {}, None) from None
            raise RuntimeError('WATCHDOG_SITE_READBACK_FAILED') from None
    headers = {"Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urlopen(Request(url, headers=headers), timeout=20) as response:
        return json.load(response)


def _last_deadline(feed: dict, day: str) -> datetime:
    rescue = feed.get("same_day_rescue", {})
    rescue_ready = (feed.get("SNAPSHOT_CLASS") == "SAME_DAY_RESCUE_BOUNDARY"
        and rescue.get("operational_date_jst") == day and rescue.get("immutable") is True
        and rescue.get("canonical_morning_promoted") is False
        and rescue.get("research_eligible") is False and rescue.get("same_day_results_used") is False
        and feed.get("counts", {}).get("formal") == 0)
    if (feed.get("operational_date_jst") != day or feed.get("status") != "PASS"
            or feed.get("stage") not in {"MORNING", "PREDEADLINE"}
            or (not rescue_ready and feed.get("canonical_authorities", {}).get("growth_p3", {}).get("status") != "LOCKED")):
        raise ValueError("WATCHDOG_ACCEPTED_MORNING_BINDING")
    races = feed.get("races")
    if not isinstance(races, list) or not races:
        raise ValueError("WATCHDOG_ACCEPTED_RACES")
    if feed.get("counts", {}).get("races") != len(races):
        raise ValueError("WATCHDOG_ACCEPTED_RACE_COVERAGE")
    deadlines = [datetime.fromisoformat(row["deadline_jst"]) for row in races]
    if any(value.utcoffset() != timedelta(hours=9) or value.date().isoformat() != day
           for value in deadlines):
        raise ValueError("WATCHDOG_DEADLINE_BINDING")
    return max(deadlines)


def morning_arm_plan(feed: dict, now: datetime, expected_day: str | None = None,
                     expected_authority: str | None = None) -> dict:
    """Schedule only from a verified saved MORNING; never open official inputs."""
    if now.utcoffset() != timedelta(hours=9):
        raise ValueError("MORNING_ARM_CLOCK")
    day = now.date().isoformat()
    if expected_day is not None and expected_day != day:
        raise ValueError("MORNING_ARM_DAY")
    last = _last_deadline(feed, day)
    authority = feed.get("daily_runtime", {})
    sha = authority.get("authority_sha256")
    if (feed.get("SNAPSHOT_CLASS") is not None or authority.get("immutable") is not True
            or authority.get("operational_date_jst") != day
            or not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{64}", sha) is None
            or feed.get("canonical_authorities", {}).get("growth_p3", {}).get("app_readback_status") != "PASS"
            or (expected_authority is not None and expected_authority != sha)):
        raise ValueError("MORNING_ARM_AUTHORITY")
    first = min(datetime.fromisoformat(row["deadline_jst"]) for row in feed["races"])
    return {"day": day, "authority": sha, "first": first, "last": last,
            "wake": first - MORNING_ARM_MARGIN}


def morning_arm_event(event: dict, now: datetime) -> bool:
    run = event.get("workflow_run", {})
    try:
        created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return False
    return (run.get("head_branch") == "main"
            and run.get("head_repository", {}).get("full_name") == REPOSITORY
            and run.get("path") == ".github/workflows/g11-free-runner.yml"
            and run.get("name") == "runtime sync" and run.get("status") == "completed"
            and run.get("conclusion") == "success"
            and run.get("event") in {"schedule", "workflow_dispatch", "push"}
            and created.tzinfo is not None and created.astimezone(JST).date() == now.date())


def active_runtime_before_arm(token: str) -> bool:
    # Read status-specific metadata; an old active run must not fall off a
    # latest-runs page. An incomplete or ambiguous empty response cannot arm.
    base = f"https://api.github.com/repos/{REPOSITORY}/actions/workflows/g11-free-runner.yml"
    for status in sorted(ACTIVE):
        data = _get(base + f"/runs?branch=main&status={status}&per_page=100", token)
        rows, total = data.get("workflow_runs"), data.get("total_count")
        if (not isinstance(rows, list) or type(total) is not int or total < 0
                or len(rows) != min(total, 100)):
            raise ValueError("MORNING_ARM_RUN_LIST_INCOMPLETE")
        if any(row.get("head_branch") != "main" or row.get("status") != status for row in rows):
            raise ValueError("MORNING_ARM_RUN_LIST_BINDING")
        if total:
            return True
    return False


def arm_morning_handoff(*, clock=lambda: datetime.now(JST), sleep=time.sleep) -> str:
    """One small timed handoff, independent of cron and the private runtime.

    Long waits use bounded three-hour legs in a separate supervisor job. Only
    saved feed/Actions metadata is read, and all prediction and capture gates
    remain in the existing fresh PREDEADLINE invocation.
    """
    from predeadline_dispatch_v1 import Client, dispatch
    if (os.environ.get("GITHUB_REPOSITORY") != REPOSITORY
            or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_RUN_ATTEMPT") != "1"):
        raise ValueError("MORNING_ARM_CONTEXT")
    now = clock()
    expected_day = os.environ.get("G11_ARM_MORNING_DAY") or None
    expected_authority = os.environ.get("G11_ARM_MORNING_AUTHORITY") or None
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_run":
        if expected_day or expected_authority or not morning_arm_event(
                json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text()), now):
            return "NOT_VERIFIED_MORNING_EVENT"
    elif (os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or expected_day != now.date().isoformat()
            or not expected_authority or re.fullmatch(r"[0-9a-f]{64}", expected_authority) is None):
        raise ValueError("MORNING_ARM_CONTINUATION")
    origin = os.environ["G11_APP_ORIGIN"].rstrip("/")
    day = now.date().isoformat()
    # This cheap saved-only view avoids opening large feeds after ordinary
    # successful LIVE/NIGHT completions. The full authority is verified below.
    summary = _get(f"{origin}/api/g11-feed?view=wild4-day-binding&date={day}")
    if summary.get("operational_date_jst") != day or summary.get("status") != "PASS":
        return "MORNING_NOT_PUBLISHED"
    if summary.get("stage") != "MORNING":
        return "ALREADY_LIVE_OR_CLOSED"
    feed = _get(f"{origin}/api/g11-feed?date={day}")
    plan = morning_arm_plan(feed, now, expected_day, expected_authority)
    if now >= plan["last"]:
        return "NIGHT_HANDOFF"
    until = min(plan["wake"], now + timedelta(seconds=MORNING_ARM_LEG_SECONDS))
    print("G11_MORNING_ARM_WAIT=" + json.dumps({"day": day,
        "wake_at": plan["wake"].isoformat(), "leg_until": until.isoformat()}), flush=True)
    while True:
        remaining = (until - clock()).total_seconds()
        if remaining <= 0:
            break
        sleep(min(60, remaining))
    now = clock()
    # No old-day or changed-authority continuation; no timestamps are renewed.
    fresh = _get(f"{origin}/api/g11-feed?date={day}")
    current = morning_arm_plan(fresh, now, day, plan["authority"])
    if (current["first"], current["last"]) != (plan["first"], plan["last"]):
        raise ValueError("MORNING_ARM_DEADLINE_CHANGED")
    if now >= current["last"]:
        return "NIGHT_HANDOFF"
    if active_runtime_before_arm(os.environ["GH_TOKEN"]):
        return "ACTIVE_RUNTIME"
    if now < current["wake"]:
        # One POST only. An ambiguous response is not blindly replayed.
        _post(f"https://api.github.com/repos/{REPOSITORY}/actions/workflows/g11-predeadline-critical-watchdog.yml/dispatches",
              os.environ["GH_TOKEN"], {"ref": "main", "inputs": {
                  "morning_handoff_day": day, "morning_handoff_authority": plan["authority"]}})
        return "ARM_CONTINUATION_ACCEPTED"
    result = dispatch(Client(os.environ["GH_TOKEN"]), day,
                      int(os.environ["GITHUB_RUN_ID"]), int(os.environ["GITHUB_RUN_NUMBER"]), "bootstrap")
    return result["status"]


def daytime_recovery_decision(feed: dict, runs: list[dict], now: datetime) -> str:
    """Request the existing isolated lane; its saved-authority guards still decide eligibility."""
    day = now.date().isoformat()
    if now.utcoffset() != timedelta(hours=9) or not 8 <= now.hour < 22:
        return "OUTSIDE_RESCUE_WINDOW"
    if any(run.get("status") in ACTIVE for run in runs):
        return "ACTIVE_RUNTIME"
    if feed and (feed.get("status") != "WAITING_FOR_FIRST_VERIFIED_FEED" or feed.get("races")):
        return "FEED_REQUIRES_VERIFICATION"
    since = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if len(runs) >= 100 and str(runs[-1].get("created_at", since)) >= since:
        return "RECOVERY_HISTORY_INCOMPLETE"
    attempts = [run for run in runs if run.get("event") == "workflow_dispatch"
                and run.get("display_title") == "LIVE-same-day-rescue-" + day
                and str(run.get("created_at", "")) >= since]
    if len(attempts) >= 2:
        return "RESCUE_RETRY_LIMIT"
    if attempts:
        ended = datetime.fromisoformat(attempts[0]["updated_at"].replace("Z", "+00:00"))
        if now - ended < timedelta(minutes=15):
            return "RESCUE_RETRY_COOLDOWN"
    return "DISPATCH_SAME_DAY_RESCUE"


def day_feed(origin: str, day: str) -> dict:
    try:
        return _get(f"{origin}/api/g11-feed?date={day}")
    except HTTPError as exc:
        if exc.code != 404:
            raise
        return {}


def decide(runs: list[dict], jobs_for_run, now: datetime, day: str) -> str:
    if any(run.get("status") in ACTIVE for run in runs):
        return "ACTIVE_CAPTURE"
    for run in runs:
        if run.get("status") != "completed":
            continue
        for job in jobs_for_run(run["id"]):
            for step in job.get("steps") or []:
                if (step.get("name") == "Save early encrypted critical checkpoint"
                        and step.get("conclusion") == "success"):
                    at = datetime.fromisoformat(step["completed_at"].replace("Z", "+00:00"))
                    if at.astimezone(JST).date().isoformat() != day:
                        continue
                    return "RECENT_CRITICAL" if now - at <= MAX_AGE else "STALE_CRITICAL"
    return "NO_CRITICAL_RUN"


def failed_live_identity(run: dict, jobs: dict, now: datetime, run_id: int,
                         repository: str) -> bool:
    """Only a finished failed runtime job may release its own recovery job."""
    day = now.date().isoformat()
    if (repository != "tatsuuut/-g11-official-data-relay"
            or run.get("id") != run_id or run.get("head_branch") != "main"
            or run.get("head_repository", {}).get("full_name") != repository
            or run.get("path") != ".github/workflows/g11-free-runner.yml"
            or run.get("event") != "workflow_dispatch" or run.get("run_attempt") != 1
            or run.get("display_title") not in {"LIVE-predeadline-" + day, "LIVE-predeadline-"}
            or type(run.get("run_number")) is not int or run["run_number"] <= 0):
        return False
    created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
    if (created.tzinfo is None or created.astimezone(JST).date() != now.date()
            or not timedelta(0) <= now - created <= timedelta(hours=2)):
        return False
    rows = jobs.get("jobs")
    if not isinstance(rows, list) or type(jobs.get("total_count")) is not int or jobs["total_count"] != len(rows):
        raise ValueError("FAILED_LIVE_JOBS_INCOMPLETE")
    runtime = [job for job in rows if job.get("name") == run["display_title"]]
    if (len(runtime) != 1 or runtime[0].get("status") != "completed"
            or runtime[0].get("conclusion") != "failure"):
        return False
    steps = runtime[0].get("steps") or []
    legacy = (any(step.get("name") == "Report failed continuation"
                and step.get("conclusion") == "failure" for step in steps)
            and any(step.get("name") == "Save early encrypted critical checkpoint"
                    and step.get("conclusion") == "success" for step in steps))
    # A short-cycle collector can fail before it can save a new critical
    # checkpoint. A fresh normal invocation must restore and revalidate the
    # prior accepted checkpoint itself; never treat the failed state as saved.
    short_capture = (
        any(step.get("name") == "Report short LIVE delivery failures after continuation"
            and step.get("conclusion") == "failure" for step in steps)
        and any(step.get("name") == "Capture and lock dependency-free PREDEADLINE critical lane"
            and step.get("conclusion") == "failure" for step in steps)
        and all(any(step.get("name") == name and step.get("conclusion") == "success" for step in steps)
            for name in ("Select newest same-day persisted checkpoint",
                "Require the selected checkpoint to be restored exactly",
                "Decrypt and validate restored state", "Restore frozen daily source",
                "Resolve immutable private source identity")))
    return legacy or short_capture


def recover_failed_live(now: datetime, token: str, repository: str, origin: str) -> None:
    """Failure-driven scheduling, independent of delayed cron/workflow_run events.

    Never rerun the failed identity or restore private state here. The fresh
    normal invocation owns all capture, cutoff, LOCK and persistence checks.
    """
    from predeadline_dispatch_v1 import Client, dispatch
    run_id = int(os.environ["G11_FAILED_LIVE_RUN_ID"])
    if (run_id <= 0 or str(run_id) != os.environ.get("GITHUB_RUN_ID")
            or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or os.environ.get("GITHUB_REF") != "refs/heads/main"):
        raise ValueError("FAILED_LIVE_CONTEXT")
    if now.utcoffset() != timedelta(hours=9) or not 8 <= now.hour < 22:
        print("G11_FAILED_LIVE_RECOVERY=OUTSIDE_WINDOW")
        return
    day = now.date().isoformat()
    api = f"https://api.github.com/repos/{repository}"
    run = _get(f"{api}/actions/runs/{run_id}", token)
    jobs = _get(f"{api}/actions/runs/{run_id}/jobs?per_page=100", token)
    if not failed_live_identity(run, jobs, now, run_id, repository):
        print("G11_FAILED_LIVE_RECOVERY=FAILED_RUNTIME_NOT_VERIFIED")
        return
    for _ in range(2):
        feed = day_feed(origin, day)
        if feed.get("stage") == "NIGHT" and feed.get("operational_date_jst") == day:
            print("G11_FAILED_LIVE_RECOVERY=NIGHT_ALREADY_PUBLISHED")
            return
        if not feed:
            raise ValueError("FAILED_LIVE_FEED_MISSING")
        if now >= _last_deadline(feed, day):
            print("G11_FAILED_LIVE_RECOVERY=NIGHT_HANDOFF")
            return
        data = _get(f"{api}/actions/workflows/g11-free-runner.yml/runs?branch=main&per_page=100", token)
        rows = data.get("workflow_runs")
        if not isinstance(rows, list):
            raise ValueError("FAILED_LIVE_RUN_LIST_SHAPE")
        # The workflow itself is still in_progress while this separate job
        # runs. Exclude only its verified completed failed runtime, never a
        # different active run or a still-running runtime job.
        if any(row.get("id") != run_id and row.get("status") in ACTIVE for row in rows):
            print("G11_FAILED_LIVE_RECOVERY=ACTIVE_RUNTIME")
            return
    result = dispatch(Client(token), day, run_id, run["run_number"], "continuation")
    print("G11_FAILED_LIVE_RECOVERY=" + json.dumps(result, sort_keys=True))


def published_official_input_state(receipt: dict, day: str, now: datetime) -> str:
    def timestamp(value):
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+09:00", value):
            raise ValueError("OFFICIAL_INPUT_TIME")
        return datetime.fromisoformat(value)

    def observed(row):
        start = timestamp(row["observed_at_jst"])
        if row["date"] != day or start.date().isoformat() != day or start > now:
            raise ValueError("OFFICIAL_INPUT_DAY")
        return start

    if (now.utcoffset() != timedelta(hours=9) or now.date().isoformat() != day or receipt["date"] != day
            or not isinstance(receipt["pauses"], list) or len(receipt["pauses"]) > 48):
        raise ValueError("OFFICIAL_INPUT_RECEIPT")
    stop = receipt["stop"]
    if stop is not None:
        observed(stop)
        if stop["reason"] != "PIPELINE_STOPPED" or "retry_at_jst" in stop:
            raise ValueError("OFFICIAL_INPUT_STOP")
        return "PIPELINE_STOPPED"
    active = False
    for pause in receipt["pauses"]:
        start, until = observed(pause), timestamp(pause["retry_at_jst"])
        if not timedelta(0) < until - start <= timedelta(days=1):
            raise ValueError("OFFICIAL_INPUT_PAUSE")
        active |= now < until
    return "HOST_PAUSED" if active else "NO_RECORDED_ACTIVE_HOLD"


def check_published_official_inputs(day, now=None):
    from daily_runtime_delivery_v1 import ORIGIN
    now = now or datetime.now(JST)
    if day != now.date().isoformat() or os.environ.get("G11_APP_ORIGIN", "").rstrip("/") != ORIGIN:
        raise ValueError("WATCHDOG_INPUT_ORIGIN_OR_DAY")
    try:
        result = subprocess.run(["curl", "--fail", "--silent", "--show-error", "--max-time", "8",
            "--max-filesize", "16384", "--proto", "=https", ORIGIN + "/api/g11-input-pause?date=" + day],
            check=True, capture_output=True, timeout=10)
        status = published_official_input_state(json.loads(result.stdout), day, now)
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError, AttributeError):
        status = "UNVERIFIED"
    print("G11_OFFICIAL_INPUT_WATCHDOG=" + status, flush=True)
    if status != "NO_RECORDED_ACTIVE_HOLD":
        print("::error title=G11 official input availability::" + status, flush=True)
        return 1
    return 0


def main(now: datetime | None = None) -> None:
    if os.environ.get("G11_ARM_MORNING") == "true":
        print("G11_MORNING_ARM=" + arm_morning_handoff())
        return
    now = now or datetime.now(JST)
    day = now.date().isoformat()
    token = os.environ["GH_TOKEN"]
    repository = os.environ["GITHUB_REPOSITORY"]
    if os.environ.get("G11_FAILED_LIVE_RUN_ID"):
        recover_failed_live(now, token, repository, os.environ["G11_APP_ORIGIN"].rstrip("/"))
        return
    if recover_runner_event(now, token, repository):
        return
    origin = os.environ["G11_APP_ORIGIN"].rstrip("/")
    if 2 <= now.hour < 8:
        recover_morning(now, token, repository, origin)
        return
    if now.hour < 8 or now.hour > 22:
        print("G11_CRITICAL_WATCHDOG=OUTSIDE_WINDOW")
        return
    feed = day_feed(origin, day)
    base = f"https://api.github.com/repos/{repository}/actions/workflows/g11-free-runner.yml"
    if not feed or feed.get("status") == "WAITING_FOR_FIRST_VERIFIED_FEED":
        url = base + "/runs?branch=main&per_page=100"
        status = daytime_recovery_decision(feed, _get(url, token)["workflow_runs"], now)
        if status == "DISPATCH_SAME_DAY_RESCUE":
            status = daytime_recovery_decision(day_feed(origin, day), _get(url, token)["workflow_runs"], now)
            if status == "DISPATCH_SAME_DAY_RESCUE":
                _post(base + "/dispatches", token,
                      {"ref": "main", "inputs": {"phase": "same-day-rescue", "operational_date": day}})
        print("G11_DAYTIME_RECOVERY=" + status)
        return
    if feed.get("stage") == "NIGHT" and feed.get("operational_date_jst") == day:
        print("G11_CRITICAL_WATCHDOG=NIGHT_ALREADY_PUBLISHED")
        return
    if now >= _last_deadline(feed, day):
        print("G11_CRITICAL_WATCHDOG=NIGHT_HANDOFF")
        return
    if day >= "2026-11-18" and os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            print("official_input_day=" + day, file=output)
    base = f"https://api.github.com/repos/{repository}/actions/workflows/g11-free-runner.yml"
    runs = _get(base + "/runs?per_page=30", token)["workflow_runs"]
    def jobs(run_id):
        return _get(f"https://api.github.com/repos/{repository}/actions/runs/{run_id}/jobs?per_page=100", token)["jobs"]
    status = decide(runs, jobs, now, day)
    if status not in {"STALE_CRITICAL", "NO_CRITICAL_RUN"}:
        print("G11_CRITICAL_WATCHDOG=" + status)
        return
    # Recheck active runs immediately before dispatch. A parallel self-chain
    # may have accepted a new invocation since the first read.
    if decide(_get(base + "/runs?per_page=30", token)["workflow_runs"], jobs, now, day) not in {"STALE_CRITICAL", "NO_CRITICAL_RUN"}:
        print("G11_CRITICAL_WATCHDOG=DEFER_RACE_WITH_SELF_CHAIN")
        return
    payload = json.dumps({"ref": "main", "inputs": {"phase": "predeadline", "operational_date": day}}).encode()
    request = Request(base + "/dispatches", data=payload, method="POST",
                      headers={"Authorization": f"Bearer {token}",
                               "Accept": "application/vnd.github+json",
                               "X-GitHub-Api-Version": "2022-11-28",
                               "Content-Type": "application/json"})
    with urlopen(request, timeout=20) as response:
        if response.status != 204:
            raise ValueError("WATCHDOG_DISPATCH_NOT_ACCEPTED")
    print("G11_CRITICAL_WATCHDOG=DISPATCH_ACCEPTED reason=" + status)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--official-input-day":
        sys.exit(check_published_official_inputs(sys.argv[2]))
    main()


