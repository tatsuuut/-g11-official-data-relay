#!/usr/bin/env python3
"""Result-only 2026-09-29 three-engine FINAL scoring repair.

The accepted Site feed is the authority for every prediction/LOCK field. This
script only joins already-stored official NIGHT settlements to those immutable
prediction surfaces and refreshes result-side comparison fields.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

DAY = "2026-09-29"
LABELS = ("p3", "true_ai", "abeken")
RESULT_SIDE_RACE_FIELDS = {"three_engine_score", "three_engine_classification"}
RESULT_SIDE_TOP_FIELDS = {"three_engine_comparison"}


def require(condition: bool, code: str) -> None:
    if not condition:
        raise SystemExit(code)


def race_final(race: Mapping[str, Any]) -> bool:
    meta = race.get("result_meta")
    return isinstance(meta, dict) and meta.get("result_status") in {
        "ESTABLISHED", "TRIFECTA_NOT_ESTABLISHED"
    }


def score(head: int | None, bets: Sequence[str], race: Mapping[str, Any]) -> dict[str, Any]:
    if not race_final(race):
        return {
            "status": "PENDING", "head_hit": None,
            "second_evaluation_success": None, "third_evaluation_success": None,
            "trifecta_hit": None, "investment": None, "payout": None,
            "profit": None, "roi": None, "miss_classification": None,
        }
    settlements = (race.get("result_meta") or {}).get("trifecta_settlements", [])
    established = (race.get("result_meta") or {}).get("result_status") == "ESTABLISHED"
    if not established or not settlements:
        return {
            "status": "TRIFECTA_NOT_ESTABLISHED", "head_hit": None,
            "second_evaluation_success": None, "third_evaluation_success": None,
            "trifecta_hit": None, "investment": None, "payout": None,
            "profit": None, "roi": None, "miss_classification": None,
        }
    winners = [str(item["trifecta"]) for item in settlements]
    winner_parts = [tuple(int(value) for value in winner.split("-")) for winner in winners]
    bet_set = set(bets)
    head_hit = any(parts[0] == head for parts in winner_parts)
    second_success = any(
        any(ticket.startswith(f"{head}-{parts[1]}-") for ticket in bet_set)
        for parts in winner_parts
    )
    third_success = any(
        any(ticket.startswith(f"{head}-") and ticket.endswith(f"-{parts[2]}") for ticket in bet_set)
        for parts in winner_parts
    )
    hit = any(winner in bet_set for winner in winners)
    refund = (race.get("research_finance") or {}).get("race_refund_occurred") is True
    investment = len(bet_set) * 100
    prize = sum(int(item["payout"]) for item in settlements if item["trifecta"] in bet_set)
    if not head_hit:
        miss = "HEAD_MISS"
    elif not second_success:
        miss = "SECOND_EVALUATION_MISS"
    elif not third_success:
        miss = "THIRD_EVALUATION_MISS"
    elif not hit:
        miss = "COMBINATION_MISS"
    else:
        miss = None
    return {
        "status": "REFUND_FINANCE_EXCLUDED" if refund else "SETTLED",
        "head_hit": head_hit,
        "second_evaluation_success": second_success,
        "third_evaluation_success": third_success,
        "trifecta_hit": hit,
        "investment": None if refund else investment,
        "payout": None if refund else prize,
        "profit": None if refund else prize - investment,
        "roi": None if refund or investment == 0 else prize / investment,
        "miss_classification": miss,
    }


def prediction_inputs(feed: Mapping[str, Any], race: Mapping[str, Any]) -> dict[str, tuple[int, list[str]]]:
    ai_by_race = {
        item["race_id"]: item
        for item in ((feed.get("true_ai") or {}).get("public_predictions") or [])
        if isinstance(item, dict) and isinstance(item.get("race_id"), str)
    }
    p3_live = race.get("predeadline")
    if isinstance(p3_live, dict):
        p3_head = p3_live.get("top_boat")
        p3_bets = list(p3_live.get("practical_bets") or [])
    else:
        p3_head = race.get("top_boat")
        p3_bets = list(race.get("practical_bets") or [])

    ai = ai_by_race.get(race["key"])
    ai_shadow = None if ai is None else ai.get("derived_trifecta_shadow")
    ai_head = None if ai is None else ai.get("ai_head")
    ai_bets = [] if not isinstance(ai_shadow, dict) else list(ai_shadow.get("practical_bets") or [])

    abeken = race.get("abeken_shadow")
    ab_pred = None
    if isinstance(abeken, dict):
        ab_pred = abeken.get("live") or abeken.get("morning")
    ab_head = None if not isinstance(ab_pred, dict) else ab_pred.get("main_head")
    ab_bets = [] if not isinstance(ab_pred, dict) else list(ab_pred.get("practical_bets") or [])

    output = {
        "p3": (p3_head, p3_bets),
        "true_ai": (ai_head, ai_bets),
        "abeken": (ab_head, ab_bets),
    }
    for label, (head, bets) in output.items():
        require(isinstance(head, int) and 1 <= head <= 6, f"MISSING_HEAD:{race['key']}:{label}")
        require(bool(bets), f"MISSING_BETS:{race['key']}:{label}")
        require(len(bets) == len(set(bets)), f"DUPLICATE_BETS:{race['key']}:{label}")
    return output


def strip_result_side(feed: Mapping[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(feed)
    for field in RESULT_SIDE_TOP_FIELDS:
        value.pop(field, None)
    for race in value.get("races", []):
        for field in RESULT_SIDE_RACE_FIELDS:
            race.pop(field, None)
    return value


def repair(feed: dict[str, Any]) -> dict[str, Any]:
    require(feed.get("operational_date_jst") == DAY, "DATE")
    require(feed.get("stage") == "NIGHT" and feed.get("status") == "PASS", "NIGHT_ONLY")
    races = feed.get("races")
    require(isinstance(races, list) and len(races) == 144, "RACE_COUNT")
    counts = feed.get("counts") or {}
    require(counts.get("races") == 144 and counts.get("results") == 144 and counts.get("pending") == 0,
            "RESULT_CLOSURE")
    require(all(race_final(race) for race in races), "RESULT_META_INCOMPLETE")

    before = strip_result_side(feed)
    aggregate = {
        label: {
            "prediction_races": 0, "settled_races": 0, "head_hits": 0,
            "second_evaluation_hits": 0, "third_evaluation_hits": 0,
            "trifecta_hits": 0, "investment": 0, "payout": 0, "profit": 0,
            "financial_races": 0, "total_points": 0,
        }
        for label in LABELS
    }
    classifications = {
        "P3_ONLY": 0, "TRUE_AI_ONLY": 0, "ABEKEN_ONLY": 0,
        "TWO_ENGINES": 0, "ALL_THREE": 0, "ALL_MISS": 0, "PENDING": 0,
    }

    for race in races:
        inputs = prediction_inputs(feed, race)
        scores = {}
        for label, (head, bets) in inputs.items():
            current = score(head, bets, race)
            scores[label] = current
            aggregate[label]["prediction_races"] += 1
            aggregate[label]["total_points"] += len(set(bets))
            if current["status"] in {"SETTLED", "REFUND_FINANCE_EXCLUDED"}:
                aggregate[label]["settled_races"] += 1
                aggregate[label]["head_hits"] += int(current["head_hit"] is True)
                aggregate[label]["second_evaluation_hits"] += int(current["second_evaluation_success"] is True)
                aggregate[label]["third_evaluation_hits"] += int(current["third_evaluation_success"] is True)
                aggregate[label]["trifecta_hits"] += int(current["trifecta_hit"] is True)
            if current["status"] == "SETTLED":
                aggregate[label]["financial_races"] += 1
                aggregate[label]["investment"] += current["investment"]
                aggregate[label]["payout"] += current["payout"]
                aggregate[label]["profit"] += current["profit"]
        race["three_engine_score"] = scores
        hits = [label for label in LABELS if scores[label]["trifecta_hit"] is True]
        if len(hits) == 3:
            classification = "ALL_THREE"
        elif len(hits) == 2:
            classification = "TWO_ENGINES"
        elif hits == ["p3"]:
            classification = "P3_ONLY"
        elif hits == ["true_ai"]:
            classification = "TRUE_AI_ONLY"
        elif hits == ["abeken"]:
            classification = "ABEKEN_ONLY"
        else:
            classification = "ALL_MISS"
        race["three_engine_classification"] = classification
        classifications[classification] += 1

    for values in aggregate.values():
        values["average_points"] = (
            values["total_points"] / values["prediction_races"]
            if values["prediction_races"] else None
        )
        values["head_hit_rate"] = (
            values["head_hits"] / values["settled_races"]
            if values["settled_races"] else None
        )
        values["trifecta_hit_rate"] = (
            values["trifecta_hits"] / values["settled_races"]
            if values["settled_races"] else None
        )
        values["roi"] = (
            values["payout"] / values["investment"]
            if values["investment"] else None
        )

    feed["three_engine_comparison"] = {
        "schema": "G11_THREE_ENGINE_COMPARISON_V1",
        "status": "SCORED",
        "engines": aggregate,
        "classifications": classifications,
        "abeken_result_feedback_to_model": False,
        "all_predictions_locked_before_result": True,
    }

    require(strip_result_side(feed) == before, "PREDICTION_OR_SCHEMA_MUTATED")
    require(all(
        isinstance(race.get("three_engine_score"), dict)
        and set(race["three_engine_score"]) == set(LABELS)
        and race.get("three_engine_classification") != "PENDING"
        for race in races
    ), "FINAL_SCORE_INCOMPLETE")
    require(all(values["settled_races"] == 144 for values in aggregate.values()),
            "ENGINE_SETTLEMENT_COUNT")
    return feed


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: repair_20260929_three_engine_final.py INPUT OUTPUT")
    source = Path(sys.argv[1])
    target = Path(sys.argv[2])
    feed = json.loads(source.read_text(encoding="utf-8"))
    original = copy.deepcopy(feed)
    repaired = repair(feed)
    target.write_text(
        json.dumps(repaired, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "status": "READY",
        "day": DAY,
        "races": len(repaired["races"]),
        "classifications": repaired["three_engine_comparison"]["classifications"],
        "hits": {
            label: repaired["three_engine_comparison"]["engines"][label]["trifecta_hits"]
            for label in LABELS
        },
        "prediction_payload_preserved": strip_result_side(repaired) == strip_result_side(original),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
