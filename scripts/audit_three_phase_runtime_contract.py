#!/usr/bin/env python3
"""Fail-closed static contract audit for G11 MORNING/PREDEADLINE/NIGHT runtime.

This audit is intentionally independent from the production runner.  Its job is
not to predict races or mutate runtime state.  It proves that the three
scheduler/phase routes, publication/readback path, and the private relay phase
hooks are still present before a change is accepted.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
FREE = ROOT / ".github" / "workflows" / "g11-free-runner.yml"
NIGHT = ROOT / ".github" / "workflows" / "g11-explicit-night-dispatcher.yml"


class ContractError(RuntimeError):
    pass


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ContractError(code)


def require_text(text: str, needle: str, code: str) -> None:
    require(needle in text, code)


def audit_public() -> dict[str, object]:
    free = FREE.read_text(encoding="utf-8")
    night = NIGHT.read_text(encoding="utf-8")

    # MORNING scheduler and resolver must stay coupled exactly.
    morning_crons = (
        'cron: "15-55/5 17 * * *"',
        'cron: "*/5 18-21 * * *"',
        'cron: "0-5/5 22 * * *"',
    )
    for cron in morning_crons:
        require_text(free, cron, f"MORNING_CRON_MISSING:{cron}")
    require_text(
        free,
        '"15-55/5 17 * * *"|"*/5 18-21 * * *"|"0-5/5 22 * * *")',
        "MORNING_RESOLVER_SET",
    )
    require_text(free, 'phase="morning"', "MORNING_RESOLVER_PHASE")

    # PREDEADLINE scheduler and resolver must stay coupled exactly.
    pre_crons = (
        'cron: "*/5 23 * * *"',
        'cron: "*/5 0-11 * * *"',
    )
    for cron in pre_crons:
        require_text(free, cron, f"PREDEADLINE_CRON_MISSING:{cron}")
    require_text(
        free,
        '"*/5 23 * * *"|"*/5 0-11 * * *")',
        "PREDEADLINE_RESOLVER_SET",
    )
    require_text(free, 'phase="predeadline"', "PREDEADLINE_RESOLVER_PHASE")

    # NIGHT must be dispatched only by the explicit dispatcher.
    require('cron: "*/5 13-16 * * *"' not in free, "NIGHT_CRON_LEAK_IN_FREE_RUNNER")
    require_text(night, 'cron: "*/5 13-16 * * *"', "NIGHT_CRON_MISSING")
    require_text(
        night,
        'actions/workflows/g11-free-runner.yml/dispatches',
        "NIGHT_DISPATCH_TARGET",
    )
    require_text(night, 'phase:"night"', "NIGHT_DISPATCH_PHASE")

    # Main runner cannot silently lose its safety/readback/publication chain.
    required_steps = (
        "Audit public surface",
        "Resolve the bounded phase and race day",
        "Capture due PREDEADLINE official inputs before regression checks",
        "Run the private fail-closed relay",
        "Audit WILD4 training-food capture",
        "Gate NIGHT publication on fully settled research",
        "Publish a newly verified feed through OIDC",
        "Verify source identity and projected app readback",
        "Keep the predeadline relay alive between delayed cron starts",
        "Propagate private runtime failure after state checkpoint",
    )
    for name in required_steps:
        require_text(free, f"- name: {name}", f"RUNNER_STEP_MISSING:{name}")

    require_text(
        free,
        "vars.G11_RELAY_ENABLED == 'true'",
        "RUNNER_ENABLE_GATE_MISSING",
    )
    require_text(free, "cancel-in-progress: false", "RUNNER_CONCURRENCY_POLICY")
    require_text(free, "G11_APP_ORIGIN:", "APP_ORIGIN_CONTRACT")
    require_text(free, "G11_RUNTIME_BRANCH:", "RUNTIME_BRANCH_CONTRACT")

    # PREDEADLINE reliability contract: cron delay must not suppress the
    # self-dispatch heartbeat.  Only another workflow_dispatch continuation
    # may block dispatch; TRUE-AI artifacts are not an availability gate.
    continuation_start = free.index(
        "- name: Keep the predeadline relay alive between delayed cron starts"
    )
    continuation_end = free.index(
        "- name: Propagate private runtime failure after state checkpoint"
    )
    continuation = free[continuation_start:continuation_end]
    require_text(
        continuation,
        '.event == "workflow_dispatch"',
        "PREDEADLINE_CHAIN_DISPATCH_ONLY_DEDUPE",
    )
    require_text(
        continuation,
        "sleep 45",
        "PREDEADLINE_CHAIN_SPACING",
    )
    require_text(
        continuation,
        'actions/workflows/g11-free-runner.yml/dispatches',
        "PREDEADLINE_CHAIN_DISPATCH_TARGET",
    )
    require(
        "morning-predictions.manifest.json" not in continuation,
        "PREDEADLINE_CHAIN_TRUE_AI_GATE",
    )
    require(
        '.event == "schedule"' not in continuation,
        "PREDEADLINE_CHAIN_CRON_MUST_NOT_BLOCK",
    )

    return {
        "MORNING": "PASS",
        "PREDEADLINE": "PASS",
        "NIGHT": "PASS",
        "PUBLICATION_READBACK_CHAIN": "PASS",
    }


def audit_private(private_root: Path) -> dict[str, object]:
    relay = private_root / "g11" / "relay" / "v1.py"
    require(relay.is_file(), "PRIVATE_RELAY_MISSING")
    text = relay.read_text(encoding="utf-8")

    # Morning canonical/recovery windows must remain explicit and bounded.
    require_text(
        text,
        "canonical_deadline = datetime.combine(target, datetime_time(7, 10), JST)",
        "MORNING_CANONICAL_DEADLINE",
    )
    require_text(
        text,
        "recovery_deadline = datetime.combine(target, datetime_time(8, 0), JST)",
        "MORNING_RECOVERY_DEADLINE",
    )
    require_text(text, "MORNING_RECOVERY_WINDOW_CLOSED", "MORNING_RECOVERY_FAIL_CLOSED")

    # Three independent phase hooks must remain present.
    require_text(
        text,
        'if phase in {"morning", "predeadline"}:',
        "PRIVATE_MORNING_PREDEADLINE_PHASE_HOOK",
    )
    require_text(text, "wild_runtime.run_morning(store, target, now)", "WILD_MORNING_HOOK")
    require_text(text, "wild_runtime.run_predeadline(store, target)", "WILD_PREDEADLINE_HOOK")
    require_text(text, 'if phase == "night":', "PRIVATE_NIGHT_PHASE_HOOK")
    require_text(text, "wild_runtime.run_night(store, target)", "WILD_NIGHT_HOOK")
    require_text(text, "current_growth_p3.publish_future(", "GROWTH_P3_PUBLISH_HOOK")
    require_text(text, "FEED_EMITTED", "PRIVATE_FEED_EMISSION_CONTRACT")

    # A PREDEADLINE invocation must not masquerade as MORNING.
    require(
        re.search(r'if phase in \{"morning", "predeadline"\}:', text) is not None,
        "PHASE_SET_PARSE",
    )

    return {
        "PRIVATE_RELAY": "PASS",
        "MORNING_WINDOW": "PASS",
        "WILD_PHASE_HOOKS": "PASS",
        "GROWTH_P3_HOOK": "PASS",
    }


def night_operational_date(now):
    """Return the NIGHT day only inside 22:00 <= JST time < 02:00.

    This is deliberately the same 02:00 rollover as the production relay.
    A delayed NIGHT scheduler must not enter the next morning's runtime.
    """
    from datetime import timedelta, timezone

    require(now.tzinfo is not None, "NIGHT_CLOCK_TIMEZONE_REQUIRED")
    local = now.astimezone(timezone(timedelta(hours=9)))
    if local.hour >= 22:
        return local.date().isoformat()
    if local.hour < 2:
        return (local.date() - timedelta(days=1)).isoformat()
    return None


def night_feed_ready(feed, operational_date):
    """Read-only stop decision, not a replacement for the Site validator.

    A PASS badge or nonzero result count alone is never completion. Check the
    requested day, the complete race set, final result states, and displayed
    money. Never promote excluded races or mutate the incoming feed.
    """
    from datetime import date

    def no(reason):
        return {"ready": False, "reason": reason}

    try:
        day = date.fromisoformat(operational_date)
    except (ValueError, TypeError):
        return no("INVALID_OPERATIONAL_DATE")
    if type(feed) is not dict or feed.get("operational_date_jst") != day.isoformat():
        return no("FEED_DATE_MISMATCH")
    if feed.get("stage") != "NIGHT" or feed.get("status") != "PASS":
        return no("NIGHT_NOT_PUBLISHED")
    rows, counts, db = feed.get("races"), feed.get("counts"), feed.get("research_db")
    if type(rows) is not list or not rows or type(counts) is not dict or type(db) is not dict:
        return no("FEED_SHAPE")
    for field in ("races", "results", "pending", "formal", "excluded", "research_samples"):
        if type(counts.get(field)) is not int or counts[field] < 0:
            return no("COUNT_TYPE:" + field)
    if counts["races"] != len(rows) or counts["results"] != len(rows) or counts["pending"] != 0:
        return no("RESULTS_PENDING_OR_COUNT_MISMATCH")
    keys, formal_money = set(), []

    def valid_bet(value):
        return (type(value) is str and re.fullmatch(r"[1-6]-[1-6]-[1-6]", value) is not None
                and len(set(value.split("-"))) == 3)

    def valid_finance(finance, bets, settlements, expected_status):
        if type(finance) is not dict or finance.get("status") != expected_status:
            return False
        fields = ("gross_stake", "refund_amount", "net_investment", "prize_return", "profit")
        if any(type(finance.get(field)) is not int for field in fields):
            return False
        gross, refund, net, prize, profit = (finance[field] for field in fields)
        if not (gross == 100 * len(bets) and 0 <= refund <= gross and net == gross - refund
                and prize >= 0 and profit == prize - net):
            return False
        if prize != sum(settlements.get(bet, 0) for bet in bets):
            return False
        if expected_status == "VERIFIED_HYPOTHETICAL_ONLY":
            if type(finance.get("total_return")) is not int or finance["total_return"] != prize + refund:
                return False
        return True

    for race in rows:
        if type(race) is not dict:
            return no("RACE_SHAPE")
        key = race.get("key")
        if (type(key) is not str or re.fullmatch(day.strftime("%Y%m%d") + r"-\d{2}-\d{2}", key) is None
                or key in keys or type(race.get("research_eligible")) is not bool):
            return no("RACE_IDENTITY_OR_ELIGIBILITY")
        if not (1 <= int(key[9:11]) <= 24 and 1 <= int(key[12:14]) <= 12):
            return no("RACE_KEY_RANGE")
        keys.add(key)
        meta = race.get("result_meta")
        if type(meta) is not dict:
            return no("RESULT_META_MISSING:" + key)
        values = meta.get("trifecta_settlements")
        if type(values) is not list:
            return no("SETTLEMENTS_MISSING:" + key)
        settlements = {}
        for item in values:
            if (type(item) is not dict or not valid_bet(item.get("trifecta"))
                    or type(item.get("payout")) is not int or item["payout"] <= 0
                    or item["trifecta"] in settlements):
                return no("SETTLEMENT_INVALID:" + key)
            settlements[item["trifecta"]] = item["payout"]
        if meta.get("result_status") == "ESTABLISHED":
            result = race.get("result_trifecta")
            if (not valid_bet(result) or result not in settlements
                    or type(race.get("payout")) is not int or race["payout"] != settlements[result]):
                return no("RESULT_NOT_CONFIRMED:" + key)
        elif meta.get("result_status") == "TRIFECTA_NOT_ESTABLISHED":
            if settlements or race.get("result_trifecta") is not None or race.get("payout") is not None:
                return no("NO_TRIFECTA_CONFLICT:" + key)
        else:
            return no("RESULT_NOT_FINAL:" + key)
        bets = race.get("practical_bets")
        if (type(bets) is not list or not all(valid_bet(bet) for bet in bets)
                or len(set(bets)) != len(bets)):
            return no("DISPLAY_BETS_INVALID:" + key)
        canonical_bets = race.get("p3_production_bets")
        if (len(bets) == 7 and (
                "正本特例8点=" in str(race.get("caution") or "")
                or (type(canonical_bets) is list and len(canonical_bets) == 8))):
            return no("DISPLAY_SPECIAL8_TRUNCATED:" + key)
        if race["research_eligible"]:
            if not bets:
                return no("FORMAL_BETS_MISSING:" + key)
            finance = race.get("research_finance")
            if race.get("formal_status") != "FORMAL" or not valid_finance(finance, bets, settlements, "VERIFIED"):
                return no("FORMAL_MONEY_NOT_VERIFIED:" + key)
            formal_money.append(finance)
        else:
            if race.get("formal_status") != "EXCLUDED":
                return no("EXCLUDED_FLAG_CONFLICT:" + key)
            reference = race.get("late_reference")
            # A mixed formal/SG day can legitimately have no reference bets.
            # An entirely nonresearch rescue day must not be marked fixed
            # while the reference-money display is still absent.
            if reference is not None or counts["formal"] == 0:
                if (not bets or type(reference) is not dict
                        or reference.get("status") != "RECORDED_NON_FORMAL_SETTLED"
                        or reference.get("included_in_formal_metrics") is not False
                        or reference.get("included_in_coefficient_learning") is not False
                        or not valid_finance(reference.get("reference_finance"), bets, settlements,
                                             "VERIFIED_HYPOTHETICAL_ONLY")):
                    return no("REFERENCE_MONEY_NOT_VERIFIED:" + key)
    n = len(formal_money)
    if counts["formal"] != n or counts["excluded"] != len(rows) - n or counts["research_samples"] != n:
        return no("RESEARCH_COUNT_PARTITION")
    if n:
        if db.get("available") is not True or type(db.get("formal_races")) is not int or db["formal_races"] != n:
            return no("RESEARCH_DB_BINDING")
        totals = {
            "practical_investment": sum(item["gross_stake"] for item in formal_money),
            "practical_return": sum(item["prize_return"] + item["refund_amount"] for item in formal_money),
            "practical_profit": sum(item["profit"] for item in formal_money),
        }
        if any(type(db.get(field)) is not int or db[field] != value for field, value in totals.items()):
            return no("RESEARCH_MONEY_TOTAL_MISMATCH")
    elif db.get("available") is not False:
        return no("NONRESEARCH_DB_BINDING")
    return {"ready": True, "reason": "RESULT_AND_DISPLAY_MONEY_COMPLETE", "races": len(rows), "formal": n}


def test_night_policy():
    """Executable synthetic regressions; no network, store, prediction or sync."""
    from copy import deepcopy
    from datetime import datetime

    checks = 0

    def check(condition, name):
        nonlocal checks
        require(condition, "NIGHT_POLICY_TEST:" + name)
        checks += 1

    for timestamp, expected in (
        ("2026-09-25T21:59:59+09:00", None),
        ("2026-09-25T22:00:00+09:00", "2026-09-25"),
        ("2026-09-25T23:59:59+09:00", "2026-09-25"),
        ("2026-09-26T00:00:00+09:00", "2026-09-25"),
        ("2026-09-26T01:59:59+09:00", "2026-09-25"),
        ("2026-09-26T02:00:00+09:00", None),
        ("2026-09-26T04:59:59+09:00", None),
        ("2026-09-26T10:00:00+09:00", None),
        ("2026-09-25T13:00:00+00:00", "2026-09-25"),
    ):
        check(night_operational_date(datetime.fromisoformat(timestamp)) == expected, timestamp)
    finance = {"status": "VERIFIED", "gross_stake": 300, "refund_amount": 0,
               "net_investment": 300, "prize_return": 1450, "profit": 1150}
    race = {"key": "20260925-01-01", "research_eligible": True, "formal_status": "FORMAL",
            "practical_bets": ["1-2-3", "1-3-2", "2-1-3"], "result_trifecta": "1-2-3", "payout": 1450,
            "result_meta": {"result_status": "ESTABLISHED", "trifecta_settlements": [{"trifecta": "1-2-3", "payout": 1450}]},
            "research_finance": finance}
    feed = {"operational_date_jst": "2026-09-25", "stage": "NIGHT", "status": "PASS", "races": [race],
            "counts": {"races": 1, "results": 1, "pending": 0, "formal": 1, "excluded": 0, "research_samples": 1},
            "research_db": {"available": True, "formal_races": 1, "practical_investment": 300,
                            "practical_return": 1450, "practical_profit": 1150}}
    original = deepcopy(feed)
    check(night_feed_ready(feed, "2026-09-25")["ready"], "formal_complete")
    check(feed == original, "input_immutable")
    check(not night_feed_ready(feed, "2026-09-24")["ready"], "wrong_day")
    for name, mutate in (
        ("morning_not_complete", lambda f: f.update(stage="MORNING")),
        ("empty", lambda f: f.update(races=[])),
        ("partial", lambda f: f["counts"].update(results=0, pending=1)),
        ("bool_count", lambda f: f["counts"].update(races=True)),
        ("duplicate", lambda f: f["races"].append(deepcopy(f["races"][0]))),
        ("missing_settlements", lambda f: f["races"][0]["result_meta"].pop("trifecta_settlements")),
        ("pending_result", lambda f: f["races"][0]["result_meta"].update(result_status="PENDING")),
        ("wrong_result", lambda f: f["races"][0].update(result_trifecta="6-5-4")),
        ("wrong_payout", lambda f: f["races"][0].update(payout=1451)),
        ("missing_finance", lambda f: f["races"][0].pop("research_finance")),
        ("wrong_stake", lambda f: f["races"][0]["research_finance"].update(gross_stake=500)),
        ("wrong_profit", lambda f: f["races"][0]["research_finance"].update(profit=0)),
        ("wrong_aggregate", lambda f: f["research_db"].update(practical_return=9999)),
    ):
        variant = deepcopy(feed)
        mutate(variant)
        check(not night_feed_ready(variant, "2026-09-25")["ready"], name)
    mixed = deepcopy(feed)
    excluded = deepcopy(race)
    excluded.update(key="20260925-01-02", research_eligible=False, formal_status="EXCLUDED", research_finance=None)
    mixed["races"].append(excluded)
    mixed["counts"].update(races=2, results=2, excluded=1)
    check(night_feed_ready(mixed, "2026-09-25")["ready"], "mixed_formal_excluded")
    rescue = deepcopy(feed)
    rescue["races"] = [deepcopy(excluded)]
    rescue["counts"].update(formal=0, research_samples=0, excluded=1)
    rescue["research_db"] = {"available": False}
    check(not night_feed_ready(rescue, "2026-09-25")["ready"], "rescue_missing_money")
    rescue["races"][0]["late_reference"] = {"status": "RECORDED_NON_FORMAL_SETTLED", "included_in_formal_metrics": False,
                                  "included_in_coefficient_learning": False,
                                  "reference_finance": dict(finance, status="VERIFIED_HYPOTHETICAL_ONLY", total_return=1450)}
    check(night_feed_ready(rescue, "2026-09-25")["ready"], "rescue_with_reference_money")
    check(rescue["counts"]["formal"] == 0, "rescue_not_promoted")
    refund = deepcopy(feed)
    refund["races"][0].update(result_trifecta=None, payout=None)
    refund["races"][0]["result_meta"].update(result_status="TRIFECTA_NOT_ESTABLISHED", trifecta_settlements=[])
    refund["races"][0]["research_finance"].update(refund_amount=300, net_investment=0, prize_return=0, profit=0)
    refund["research_db"].update(practical_return=300, practical_profit=0)
    check(night_feed_ready(refund, "2026-09-25")["ready"], "full_refund_final")
    # A published 8-point set must remain 8 points; the stop decision never truncates.
    eight = deepcopy(feed)
    eight["races"][0]["practical_bets"] += ["2-3-1", "3-1-2", "3-2-1", "4-1-2", "4-2-1"]
    eight["races"][0]["research_finance"].update(gross_stake=800, net_investment=800, profit=650)
    eight["research_db"].update(practical_investment=800, practical_profit=650)
    check(night_feed_ready(eight, "2026-09-25")["ready"], "eight_point_compatible")
    check(len(eight["races"][0]["practical_bets"]) == 8, "eight_points_unchanged")
    truncated = deepcopy(feed)
    truncated["races"][0]["practical_bets"] = eight["races"][0]["practical_bets"][:7]
    truncated["races"][0]["caution"] = "SITE表示互換のみ｜正本特例8点="
    check(not night_feed_ready(truncated, "2026-09-25")["ready"], "special8_truncation_not_complete")
    mixed["races"][1]["practical_bets"] = []
    check(night_feed_ready(mixed, "2026-09-25")["ready"], "excluded_without_bets")
    return {"STATUS": "PASS", "CHECKS": checks, "NETWORK_REQUESTS": 0, "RUNTIME_STATE_MUTATED": False}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-root", type=Path)
    parser.add_argument("--night-day", action="store_true")
    parser.add_argument("--night-ready", type=Path)
    parser.add_argument("--day")
    parser.add_argument("--night-self-test", action="store_true")
    args = parser.parse_args()
    if args.night_day:
        from datetime import datetime, timezone
        day = night_operational_date(datetime.now(timezone.utc))
        if day is None:
            return 2
        print(day)
        return 0
    if args.night_ready is not None:
        try:
            feed = json.loads(args.night_ready.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            print("G11_NIGHT_READINESS=FEED_UNREADABLE")
            return 1
        result = night_feed_ready(feed, args.day)
        print("G11_NIGHT_READINESS=" + json.dumps(result, sort_keys=True))
        return 0 if result["ready"] else 1
    if args.night_self_test:
        print("G11_NIGHT_POLICY_TEST=" + json.dumps(test_night_policy(), sort_keys=True))
        return 0
    result = {"PUBLIC": audit_public(), "NIGHT_POLICY_TEST": test_night_policy()}
    if args.private_root is not None:
        result["PRIVATE"] = audit_private(args.private_root.resolve())
    print("G11_THREE_PHASE_RUNTIME_CONTRACT=" + json.dumps(
        result, ensure_ascii=False, sort_keys=True
    ))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ContractError as exc:
        print(f"G11_THREE_PHASE_RUNTIME_CONTRACT_FAIL:{exc}", file=sys.stderr)
        raise SystemExit(1)
