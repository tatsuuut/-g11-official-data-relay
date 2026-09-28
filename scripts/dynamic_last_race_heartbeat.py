#!/usr/bin/env python3
"""Decide whether the existing PREDEADLINE self-chain should continue.

The deadline is issued by the private runtime only after verifying the
immutable MORNING B binding. No app/feed clock or inferred venue time is used.
"""

import argparse
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path

JST = timezone(timedelta(hours=9))
EFFECTIVE_FROM = date(2026, 9, 29)


def decision(result: dict, operational_date: date, now: datetime) -> str:
    if now.utcoffset() != timedelta(hours=9):
        raise ValueError("HEARTBEAT_CLOCK_NOT_JST")
    if operational_date >= EFFECTIVE_FROM and now >= datetime.combine(
        operational_date + timedelta(days=1), time(2), JST
    ):
        return "STOP_ROLLOVER"
    status = result.get("STATUS")
    if status == "NIGHT_HANDOFF_READY" and result.get("NIGHT_HANDOFF") is True:
        return "HANDOFF"
    if status in {"NIGHT_WAITING_OFFICIAL_RESULTS", "NIGHT_RESULTS_COMPLETE"}:
        return "STOP_NIGHT"
    if operational_date < EFFECTIVE_FROM:
        return "STOP_LEGACY" if now.time() >= time(20, 50) else "CONTINUE"
    if now.date() not in {operational_date, operational_date + timedelta(days=1)}:
        return "STOP_WRONG_DAY"
    if status != "RELAY_PHASE_COMPLETE" or result.get("PHASE") != "PREDEADLINE":
        raise ValueError("HEARTBEAT_RUNTIME_NOT_READY")
    if result.get("OPERATIONAL_DATE_JST") != operational_date.isoformat():
        raise ValueError("HEARTBEAT_BINDING_DAY_MISMATCH")
    raw = result.get("LAST_RACE_DEADLINE_JST")
    if not isinstance(raw, str):
        raise ValueError("HEARTBEAT_VERIFIED_MORNING_DEADLINE_MISSING")
    deadline = datetime.fromisoformat(raw)
    if deadline.utcoffset() != timedelta(hours=9) or deadline.date() != operational_date:
        raise ValueError("HEARTBEAT_VERIFIED_MORNING_DEADLINE_INVALID")
    # After the deadline, one final bounded PREDEADLINE run is still needed
    # if this run began earlier; the private runtime then returns HANDOFF.
    return "CONTINUE"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--operational-date", type=date.fromisoformat, required=True)
    args = parser.parse_args()
    result = json.loads(args.result.read_text(encoding="utf-8"))
    print(decision(result, args.operational_date, datetime.now(JST)))


if __name__ == "__main__":
    main()
