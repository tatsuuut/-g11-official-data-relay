"""Restore the verified 2026-09-28 FINAL read model from the original NIGHT lock.

Run only in the one-off relay workflow with the encrypted historical cache.
No runtime phase, prediction engine, snapshot commit, or result learning runs.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from g11.canonical.official_pipeline import OfficialArtifactStore
from g11.canonical.public_app_feed import _night_settlements_by_key
from g11.relay.final_bet_accounting_v1 import (
    SCHEMA, _snapshot_sources, build_final,
)
from g11.relay.night_transport_finance_v1 import (
    _current_special_bets, _ledger_rows, _load, normalize,
)

DAY = "2026-09-28"
SOURCE_SHA = "bb9e7575697b013d2709ac4e7a83584ab6f459d3"
ORIGINAL_RUN = 36437392793
ORIGINAL_PROJECTION_SHA = "53e96bb23f4ba8c23313cb3fea867aaa7c39587b9ba9577a0ba002a5b5a47e3a"
EXPECTED = {
    "p3": (144, 684, 26, 67900, 39130, -28770),
    "abeken": (144, 2187, 68, 216400, 130200, -86200),
    "wild": (0, 0, 0, 0, 0, 0),
}


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise SystemExit("HISTORICAL_FINAL_REPAIR_REJECTED:" + reason)


def main() -> None:
    source, store_root, target = map(Path, sys.argv[1:])
    before = _load(source)
    require(
        before.get("operational_date_jst") == DAY
        and before.get("stage") == "NIGHT"
        and before.get("status") == "PASS"
        and before.get("counts", {}).get("races") == 144
        and before.get("counts", {}).get("formal") == 144
        and before.get("counts", {}).get("results") == 144
        and before.get("counts", {}).get("pending") == 0
        and not before.get("final_accounting")
        and before.get("source") == {
            "github_sha": SOURCE_SHA,
            "projection_sha256": ORIGINAL_PROJECTION_SHA,
            "run_id": ORIGINAL_RUN,
        },
        "ACCEPTED_NIGHT_IDENTITY",
    )
    store = OfficialArtifactStore(store_root)
    verified = store.load_verified_snapshot("night", "workbook-ledger-projection")
    night = verified["SNAPSHOT"]
    require(night.get("PAYLOAD") is not None, "VERIFIED_NIGHT_LEDGER")
    ledger = _ledger_rows(night)
    settlements, _multi = _night_settlements_by_key(night["PAYLOAD"])
    keys = {race["key"] for race in before["races"]}
    require(len(keys) == 144 and keys == set(ledger) and keys == set(settlements),
            "EXACT_OFFICIAL_RACE_BINDING")
    result = json.loads(json.dumps(before, ensure_ascii=False))
    for race in result["races"]:
        key = race["key"]
        row = ledger[key]
        items = settlements[key]
        meta = race["result_meta"]
        require(
            race["formal_status"] == "FORMAL"
            and row["結果成立状態"] == "成立"
            and len(items) >= 1
            and race["result_trifecta"] == row["3連単"] == items[0]["combination"]
            and race["payout"] == row["払戻"] == items[0]["payout"]
            and meta["trifecta_popularity"] == row["3連単人気"] == items[0]["popularity"]
            and meta["winning_technique"] == row["決まり手"]
            and not any(field in meta for field in (
                "result_status", "dead_heat", "trifecta_settlements")),
            "OFFICIAL_RESULT_MATCH:" + key,
        )
        meta["result_status"] = "ESTABLISHED"
        meta["dead_heat"] = len(items) > 1
        meta["trifecta_settlements"] = [
            {"trifecta": item["combination"], "payout": item["payout"],
             "popularity": item["popularity"]}
            for item in items
        ]

    special = _current_special_bets(store_root, keys)
    # Recompute the transport accounting on the originally accepted tickets.
    # Any discrepancy with the immutable public finance is a hard stop.
    checked = normalize(result, night, canonical_bets_by_race=special)
    require(checked["counts"] == before["counts"]
            and checked["research_db"] == before["research_db"], "FINANCE_TOTAL_DRIFT")
    for old, checked_race in zip(before["races"], checked["races"]):
        require(old["key"] == checked_race["key"]
                and old["practical_bets"] == checked_race["practical_bets"]
                and old["practical_hit"] == checked_race["practical_hit"]
                and old["research_finance"] == checked_race["research_finance"],
                "RACE_FINANCE_DRIFT:" + old["key"])

    final = build_final(result, night, special_bets=special,
                        snapshot_sources=_snapshot_sources(store_root, keys))
    require(final["status"] == "FINAL_SCORED_WITH_UNSCORABLE"
            and len(final["by_race"]) == 144, "FINAL_COMPLETENESS")
    for engine, expected in EXPECTED.items():
        metrics = final["engines"][engine]
        observed = tuple(metrics[field] for field in (
            "races", "total_points", "trifecta_hit",
            "net_investment", "prize_return", "profit"))
        require(observed == expected, "ORIGINAL_FINAL_AUDIT_MISMATCH:" + engine)
    require(final["source_counts"]["p3"]["NOT_SCORABLE"] == 0
            and final["source_counts"]["abeken"]["NOT_SCORABLE"] == 0
            and final["source_counts"]["wild"]["NOT_SCORABLE"] == 144,
            "LOCKED_FINAL_SOURCE_COUNTS")

    result.pop("three_engine_comparison", None)
    result["capabilities"].pop("three_engine_comparison", None)
    for race in result["races"]:
        race.pop("three_engine_score", None)
        race.pop("three_engine_classification", None)
    result["final_accounting"] = final
    result["capabilities"]["final_accounting"] = SCHEMA
    target.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":"), allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": "VERIFIED_HISTORICAL_FINAL_ONLY",
                      "date": DAY, "races": 144, "engines": final["engines"]},
                     ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
