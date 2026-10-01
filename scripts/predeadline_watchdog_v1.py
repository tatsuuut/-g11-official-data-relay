"""No-model watchdog for the encrypted PREDEADLINE critical checkpoint."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import sys
from urllib.request import Request, urlopen

JST = timezone(timedelta(hours=9))
MAX_AGE = timedelta(minutes=6)
ACTIVE = {"queued", "in_progress", "requested", "waiting", "pending"}


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
    if now.hour < 8 or now.hour > 22:
        print("G11_CRITICAL_WATCHDOG=OUTSIDE_WINDOW")
        return
    token = os.environ["GH_TOKEN"]
    repository = os.environ["GITHUB_REPOSITORY"]
    origin = os.environ["G11_APP_ORIGIN"].rstrip("/")
    feed = _get(f"{origin}/api/g11-feed?date={day}")
    if now >= _last_deadline(feed, day):
        print("G11_CRITICAL_WATCHDOG=NIGHT_HANDOFF")
        return
    base = f"https://api.github.com/repos/{repository}/actions/workflows/g11-predeadline-critical.yml"
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
