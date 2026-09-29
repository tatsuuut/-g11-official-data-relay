"""One-day result-only replay from the already accepted public NIGHT feed.

The original encrypted 9/28 checkpoint was evicted. This alternative uses
the *published*, immutable morning and pre-race ticket locks, independently
confirmed BOAT RACE results, and the original NIGHT run's accounting totals.
The feed schema and every existing prediction, ticket and finance field remain
byte-equivalent as JSON values. No prediction engine or ledger is run here.

The P3 PRE_RACE ID below is its published, independently recomputed bet hash.
The Abe ID is a content hash of its accepted public morning lock, *not* a
recovered hash of the unavailable encrypted private snapshot. Never present
these reconstructed IDs as the original private snapshot IDs.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

DAY = "2026-09-28"
SOURCE_SHA = "bb9e7575697b013d2709ac4e7a83584ab6f459d3"
SOURCE_PROJECTION = "53e96bb23f4ba8c23313cb3fea867aaa7c39587b9ba9577a0ba002a5b5a47e3a"
RUN = 36437392793
SCHEMA = "G11_THREE_ENGINE_FINAL_BET_ACCOUNTING_V1"
STATES = ("FINAL_PRE_RACE", "FINAL_PRE_RACE_SAME", "FINAL_MORNING_FALLBACK",
          "FINAL_MORNING_ONLY", "NOT_SCORABLE")
EXPECTED = {
    "p3": (144, 684, 53, 26, 68400, 500, 67900, 39130, -28770),
    "abeken": (144, 2187, 57, 68, 218700, 2300, 216400, 130200, -86200),
    "wild": (0, 0, 0, 0, 0, 0, 0, 0, 0),
}
EXPECTED_SOURCES = {
    "p3": {"FINAL_PRE_RACE": 44, "FINAL_PRE_RACE_SAME": 84,
           "FINAL_MORNING_FALLBACK": 16},
    "abeken": {"FINAL_MORNING_ONLY": 144},
    "wild": {"NOT_SCORABLE": 144},
}


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise SystemExit("HISTORICAL_ACCEPTED_FEED_REPLAY_REJECTED:" + reason)


def digest(value: object) -> str:
    return hashlib.sha256((json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False) + "\n").encode()).hexdigest()


def artifact(key: str, stage: str, bets: list[str]) -> str:
    return digest({"RACE_ID": key, "SNAPSHOT": stage,
                   "PRODUCTION_BET_SET": sorted(bets),
                   "PRODUCTION_BET_ORDER": bets,
                   "PRODUCTION_BET_COUNT": len(bets)})


def tickets(value: object, key: str) -> list[str]:
    require(isinstance(value, list) and bool(value) and len(value) == len(set(value)),
            "LOCKED_BETS:" + key)
    require(all(isinstance(b, str) and re.fullmatch(r"[1-6]-[1-6]-[1-6]", b)
                and len(set(b.split("-"))) == 3 for b in value), "BET_SHAPE:" + key)
    return value


def before(stamp: str, deadline: str, key: str) -> None:
    a, b = datetime.fromisoformat(stamp), datetime.fromisoformat(deadline)
    require(a.utcoffset() is not None and b.utcoffset() is not None and a < b,
            "BEFORE_DEADLINE:" + key)


def score(bets: list[str], head: int, tri: str, payout: int,
          refunds: set[int]) -> dict[str, object]:
    gross = len(bets) * 100
    refund = 100 * sum(bool(refunds.intersection(map(int, b.split("-")))) for b in bets)
    prize = payout if tri in bets else 0
    net = gross - refund
    return {"head_hit": head == int(tri[0]), "trifecta_hit": tri in bets,
            "gross_stake": gross, "refund_amount": refund, "net_investment": net,
            "prize_return": prize, "profit": prize - net,
            "roi": prize / net if net else None}


def final_bet(status: str, source: str, identity: str, locked: str,
              bets: list[str], head: int, result: dict[str, object]) -> dict[str, object]:
    return {"final_status": status, "not_scorable_reason": None,
            "final_bet_source": source, "final_bet_snapshot_id": identity,
            "final_bet_locked_at_jst": locked, "final_bets": bets,
            "final_point_count": len(bets), "final_head": head,
            "wild_bet_generation": None, "score": result}


def main(source_path: Path, official_path: Path, target: Path) -> None:
    source = json.loads(source_path.read_text(encoding="utf-8"))
    official = json.loads(official_path.read_text(encoding="utf-8"))
    require(source.get("operational_date_jst") == DAY and
            source.get("schema") == "G11_PUBLIC_APP_FEED_V2" and
            source.get("stage") == "NIGHT" and source.get("status") == "PASS" and
            source.get("source") == {"github_sha": SOURCE_SHA,
                                     "projection_sha256": SOURCE_PROJECTION,
                                     "run_id": RUN} and
            all(source.get("counts", {}).get(k) == n for k, n in
                (("races", 144), ("formal", 144), ("results", 144), ("pending", 0))) and
            not source.get("final_accounting") and
            official.get("date") == DAY and len(official.get("races", {})) == 144,
            "DATE_SOURCE_AND_FINAL_LOCK")
    require(len(source["races"]) == 144 and
            {r["key"] for r in source["races"]} == set(official["races"]),
            "EXACT_144_RACE_JOIN")
    # Result-page verification: each of the 12 indexed official venue pages
    # has exactly 12 races; only these three individual result pages list a
    # refund boat. This fixture is a facts-only transcription of those pages.
    expected_refunds = {"20260928-14-01": [1], "20260928-15-11": [4],
                        "20260928-15-12": [4]}
    require({key: row["refund_boats"] for key, row in official["races"].items()
             if row["refund_boats"]} == expected_refunds and
            set(official["refund_result_urls"]) == set(expected_refunds),
            "THREE_OFFICIAL_REFUNDS")
    result = copy.deepcopy(source)
    totals = {engine: Counter() for engine in EXPECTED}
    status = {engine: Counter() for engine in EXPECTED}
    by_race, checks, study_rows = {}, {}, {}
    study = {"p3": Counter(), "abeken": Counter()}
    for race in result["races"]:
        key = race["key"]
        fact = official["races"][key]
        refunds = set(fact["refund_boats"])
        require(race["formal_status"] == "FORMAL" and
                key == f"20260928-{race['venue_code']}-{int(race['race']):02}" and
                race["result_trifecta"] == fact["trifecta"] and
                race["payout"] == fact["payout"] and
                race["research_finance"]["race_refund_occurred"] == bool(refunds) and
                len(refunds) <= 1, "OFFICIAL_RESULT_MATCH:" + key)
        meta = race["result_meta"]
        require(all(name not in meta for name in
                    ("result_status", "dead_heat", "trifecta_settlements")) and
                isinstance(meta["trifecta_popularity"], int),
                "SETTLEMENT_CURRENTLY_ABSENT:" + key)
        meta["result_status"] = "ESTABLISHED"
        meta["dead_heat"] = False
        meta["trifecta_settlements"] = [
            {"trifecta": fact["trifecta"], "payout": fact["payout"],
             "popularity": meta["trifecta_popularity"]}]

        morning = tickets(race["practical_bets"], key)
        require(morning == race["p3_production_bets"] and
                len(morning) == race["production_points"] and
                re.fullmatch(r"[0-9a-f]{64}", race["p3_snapshot_sha256"]) is not None,
                "P3_MORNING_LOCK:" + key)
        before(race["p3_published_at_jst"], race["deadline_jst"], key)
        require(artifact(key, "MORNING", morning) ==
                race["trifecta_confidence"]["bet_artifact_sha"],
                "P3_MORNING_BET_DIGEST:" + key)
        pre = race.get("predeadline")
        p3_pre = pre if isinstance(pre, dict) and pre.get("status") == "READY" else None
        if p3_pre:
            pre_bets = tickets(p3_pre["practical_bets"], key)
            before(p3_pre["captured_at_jst"], race["deadline_jst"], key)
            require(len(pre_bets) == p3_pre["production_points"] and
                    p3_pre["morning_lock_mutated"] is False and
                    artifact(key, "LIVE", pre_bets) ==
                    p3_pre["trifecta_confidence"]["bet_artifact_sha"],
                    "P3_PRE_BET_DIGEST:" + key)
            p3_status = "FINAL_PRE_RACE_SAME" if pre_bets == morning else "FINAL_PRE_RACE"
            p3_bets, p3_head, p3_time = pre_bets, p3_pre["top_boat"], p3_pre["captured_at_jst"]
            p3_identity = p3_pre["trifecta_confidence"]["bet_artifact_sha"]
        else:
            require(pre is None, "UNSUPPORTED_PRE_STATUS:" + key)
            p3_status = "FINAL_MORNING_FALLBACK"
            p3_bets, p3_head, p3_time = morning, race["top_boat"], race["p3_published_at_jst"]
            p3_identity = race["p3_snapshot_sha256"]
        p3_score = score(p3_bets, p3_head, fact["trifecta"], fact["payout"], refunds)

        ab = race["abeken_shadow"]
        m = ab["morning"]
        require(ab["immutable_prediction_snapshots"] is True and ab["live"] is None and
                m["prediction_stage"] == "MORNING" and
                m["result_feedback_to_model"] is False and
                m["purchase_executed"] is False, "ABE_MORNING_LOCK:" + key)
        ab_bets = tickets(m["practical_bets"], key)
        before(m["prediction_lock_time_jst"], race["deadline_jst"], key)
        require(m["total_buy_count"] == len(ab_bets), "ABE_POINT_LOCK:" + key)
        ab_score = score(ab_bets, m["main_head"], fact["trifecta"], fact["payout"], refunds)
        # Digest the accepted public lock itself. It is never described as an
        # original encrypted snapshot, which is no longer available.
        ab_identity = digest({"source_projection_sha": SOURCE_PROJECTION,
                              "race_key": key, "accepted_public_abeken_lock": m})
        engines = {
            "p3": final_bet(p3_status, "PRE_RACE" if p3_pre else "MORNING",
                            p3_identity, p3_time, p3_bets, p3_head, p3_score),
            "abeken": final_bet("FINAL_MORNING_ONLY", "MORNING", ab_identity,
                                m["prediction_lock_time_jst"], ab_bets,
                                m["main_head"], ab_score),
            "wild": {"final_status": "NOT_SCORABLE",
                     "not_scorable_reason": "WILD4_FINAL_BET_NOT_ISSUED",
                     "final_bet_source": None, "final_bet_snapshot_id": None,
                     "final_bet_locked_at_jst": None, "final_bets": [],
                     "final_point_count": 0, "final_head": None,
                     "wild_bet_generation": "PRE_WILD4_SHADOW", "score": None},
        }
        by_race[key] = {"official_result": fact["trifecta"],
                        "official_refund_boats": fact["refund_boats"],
                        "engines": engines}
        valid = [bet for bet in p3_bets if not refunds.intersection(map(int, bet.split("-")))]
        checks[key] = {"status": "VOID" if not valid else
                       "SCORED_HIT" if p3_score["trifecta_hit"] else "SCORED_MISS",
                       "trifecta_set_hit": p3_score["trifecta_hit"] if valid else None,
                       "morning_or_live": "LIVE" if p3_pre else "MORNING",
                       "production_bet_count": len(p3_bets),
                       "formal_grade_at_lock": (p3_pre or race)["trifecta_confidence"]["formal_grade"]}
        study_rows[key] = {"p3": {"morning": score(morning, race["top_boat"],
                                          fact["trifecta"], fact["payout"], refunds),
                                  "pre_race": p3_score if p3_pre else None,
                                  "bets_changed": p3_bets != morning if p3_pre else None},
                           "abeken": {"morning": ab_score, "pre_race": None,
                                      "bets_changed": None},
                           "pre_race_absence_reason": None if p3_pre else
                               race.get("predeadline_exclusion_reason") or "NO_VALID_PRE_RACE_LOCK"}
        for engine, entry in engines.items():
            status[engine][entry["final_status"]] += 1
            if entry["score"] is None:
                continue
            t = totals[engine]
            t["races"] += 1
            t["total_points"] += entry["final_point_count"]
            t.update({field: int(value) if isinstance(value, bool) else value
                      for field, value in entry["score"].items() if field != "roi"})
        for stage, s in (("morning", study_rows[key]["p3"]["morning"]),
                         ("pre_race", study_rows[key]["p3"]["pre_race"])):
            if s is not None:
                study["p3"][stage + "_races"] += 1
                for field in ("head_hit", "trifecta_hit", "gross_stake",
                              "refund_amount", "net_investment", "prize_return", "profit"):
                    study["p3"][stage + "_" + field] += s[field]
        study["abeken"]["morning_races"] += 1
        for field in ("head_hit", "trifecta_hit", "gross_stake", "refund_amount",
                      "net_investment", "prize_return", "profit"):
            study["abeken"]["morning_" + field] += ab_score[field]

    metrics = {}
    for engine, expected in EXPECTED.items():
        row = totals[engine]
        actual = tuple(row[field] for field in ("races", "total_points", "head_hit",
                   "trifecta_hit", "gross_stake", "refund_amount",
                   "net_investment", "prize_return", "profit"))
        require(actual == expected, "ORIGINAL_NIGHT_RUN_AGGREGATE:" + engine)
        require(all(status[engine][s] == EXPECTED_SOURCES[engine].get(s, 0)
                    for s in STATES), "ORIGINAL_NIGHT_RUN_SOURCE_COUNTS:" + engine)
        metrics[engine] = {**{field: row[field] for field in
                            ("races", "total_points", "head_hit", "trifecta_hit",
                             "gross_stake", "refund_amount", "net_investment",
                             "prize_return", "profit")},
                           "average_points": row["total_points"] / row["races"] if row["races"] else None,
                           "roi": row["prize_return"] / row["net_investment"]
                           if row["net_investment"] else None}
    final = {"schema": SCHEMA, "status": "FINAL_SCORED_WITH_UNSCORABLE",
             "wild_bet_generation": "PRE_WILD4_SHADOW",
             "source_counts": {engine: {s: status[engine][s] for s in STATES}
                               for engine in EXPECTED},
             "engines": metrics, "by_race": by_race,
             "classifications": {s: 144 if s == "NOT_SCORABLE" else 0 for s in
                                 ("P3_ONLY", "WILD_ONLY", "ABEKEN_ONLY", "TWO_ENGINES",
                                  "ALL_THREE", "ALL_MISS", "NOT_SCORABLE")},
             "morning_pre_race_research": {"summary": {k: dict(v) for k, v in study.items()},
                                           "by_race": study_rows},
             "trifecta_confidence_night": {
                 "status": "LOCKED_CONFIDENCE_RESULT_JOIN_ONLY",
                 "by_race": checks, "by_stage_points_grade": {},
                 "certification_evidence_used": False}}
    result["capabilities"]["final_accounting"] = SCHEMA
    result["final_accounting"] = final
    # Fail if any original field, including legacy comparison, is changed.
    replay = copy.deepcopy(result)
    replay.pop("final_accounting")
    replay["capabilities"].pop("final_accounting")
    for race in replay["races"]:
        for field in ("result_status", "dead_heat", "trifecta_settlements"):
            race["result_meta"].pop(field)
    require(replay == source and result["schema"] == source["schema"],
            "ALL_PREDICTION_FINANCE_AND_SCHEMA_FIELDS_IMMUTABLE")
    target.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), allow_nan=False) + "\n",
                      encoding="utf-8")
    print(json.dumps({"status": "ACCEPTED_FEED_RESULT_OVERLAY_MATCHES_ORIGINAL_RUN",
                      "date": DAY, "schema_unchanged": True, "races": len(by_race),
                      "engines": metrics,
                      "original_private_snapshot_hashes_restored": False},
                     sort_keys=True, ensure_ascii=False))


if __name__ == "__main__":
    require(len(sys.argv) == 4, "USAGE:ACCEPTED_FEED,OFFICIAL_FACTS,OUTPUT")
    main(*(Path(arg) for arg in sys.argv[1:]))
