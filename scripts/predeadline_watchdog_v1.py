"""No-model watchdog for the encrypted PREDEADLINE critical checkpoint."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from pathlib import Path

JST = timezone(timedelta(hours=9))
MAX_AGE = timedelta(minutes=6)
ACTIVE = {"queued", "in_progress", "requested", "waiting", "pending"}
RUNNER_NOT_ACQUIRED = "The job was not acquired by Runner of type hosted even after multiple attempts"
MAX_RUNNER_ATTEMPTS = 3


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
    print("G11_MORNING_WATCHDOG=" + decision)


def _get(url: str, token: str | None = None) -> dict:
    headers = {"Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urlopen(Request(url, headers=headers), timeout=20) as response:
        return json.load(response)


def _last_deadline(feed: dict, day: str) -> datetime:
    if (feed.get("operational_date_jst") != day or feed.get("status") != "PASS"
            or feed.get("stage") not in {"MORNING", "PREDEADLINE"}
            or feed.get("canonical_authorities", {}).get("growth_p3", {}).get("status") != "LOCKED"):
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


def main(now: datetime | None = None) -> None:
    now = now or datetime.now(JST)
    day = now.date().isoformat()
    token = os.environ["GH_TOKEN"]
    repository = os.environ["GITHUB_REPOSITORY"]
    if recover_runner_event(now, token, repository):
        return
    origin = os.environ["G11_APP_ORIGIN"].rstrip("/")
    if 2 <= now.hour < 8:
        recover_morning(now, token, repository, origin)
        return
    if now.hour < 8 or now.hour > 22:
        print("G11_CRITICAL_WATCHDOG=OUTSIDE_WINDOW")
        return
    feed = _get(f"{origin}/api/g11-feed?date={day}")
    if now >= _last_deadline(feed, day):
        print("G11_CRITICAL_WATCHDOG=NIGHT_HANDOFF")
        return
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
    main()
