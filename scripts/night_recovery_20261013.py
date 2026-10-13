"""Operator-authorized, result-only repair of the lost 2026-10-13 projection.

Uses the accepted predeadline bytes as evidence, never recreates private LOCKs.
The failed NIGHT checkpoint and transport plan remain immutable history.
"""
from __future__ import annotations
import argparse
import ast
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence

from daily_runtime_bootstrap_v1 import retained_module
import hot_delta_transport_v1 as hot

DAY = "2026-10-13"
KEY = "20261013-20-10"
BASE_SHA = "35dcd582d5c10049e19913fb786403e4e24bf3f8eabca0171a650d727e237fc1"
OLD_CHECKPOINT = "8e0d5e36ff4c5734c479f37973987001d79bc18af2f75745cc5f7f42233cb304"
LOCK_SHA = "cef228e645dd576ba4130c8e7d7cdb0b71df3d00934de86a7b6a998a9987088f"
JST = timezone(timedelta(hours=9))


def require(value, reason):
    if not value:
        raise RuntimeError("NIGHT_RECOVERY:" + reason)


def saved_functions(path, names, environment=None):
    """Load only existing pure accounting helpers, with no engine imports."""
    tree = ast.parse(path.read_bytes())
    functions = [x for x in tree.body if isinstance(x, ast.FunctionDef) and x.name in names]
    require({x.name for x in functions} == set(names), "ACCOUNTING_HELPER_MISSING")
    scope = {"Any": Any, "Mapping": Mapping, "Sequence": Sequence, **(environment or {})}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), scope)
    return scope


def correct_feed(feed, accepted, source):
    require(feed["operational_date_jst"] == accepted["operational_date_jst"] == DAY,
            "DATE")
    require(feed["stage"] == "NIGHT" and accepted["stage"] == "PREDEADLINE", "STAGE")
    require(feed["daily_runtime"]["authority_sha256"] ==
            accepted["daily_runtime"]["authority_sha256"], "AUTHORITY")
    old_rows = {r["key"]: r for r in accepted["races"]}
    new = deepcopy(feed)
    rows = {r["key"]: r for r in new["races"]}
    require(set(rows) == set(old_rows) and len(rows) == 132, "RACE_SET")
    prior, race = old_rows[KEY], rows[KEY]
    v53 = prior["abeken_shadow"]["v53"]
    lock = v53["live"]
    require(v53["formal_use"] is True and v53["legacy_45_fallback"] is False
            and lock["snapshot_sha256"] == LOCK_SHA and lock["buy_count"] == 9
            and len(lock["practical_bets"]) == len(set(lock["practical_bets"])) == 9
            and datetime.fromisoformat(lock["locked_at_jst"]) < datetime.fromisoformat(prior["deadline_jst"])
            and lock["captured_at_jst"] == lock["locked_at_jst"], "ACCEPTED_FORMAL_LOCK")
    require(race["abeken_shadow"]["v53"].get("live") is None
            and race["abeken_shadow"]["v53"]["morning"] == v53["morning"], "EXPECTED_LOST_LOCK")
    require(race["result_trifecta"] == "2-1-3" and race["payout"] == 1200
            and race["result_meta"]["result_status"] == "ESTABLISHED"
            and race["result_meta"]["trifecta_settlements"] == [
                {"trifecta": "2-1-3", "payout": 1200, "popularity": 4}], "OFFICIAL_RESULT")
    # Restore precisely the three published projections which were lost.
    race["abeken_shadow"]["v53"]["live"] = deepcopy(lock)
    race["abeken_shadow"]["live"] = deepcopy(prior["abeken_shadow"]["live"])
    for field in ("pre_race", "changed"):
        require(race["wild_pack"].get(field) is None, "EXPECTED_LOST_POSITION_PROJECTION")
        race["wild_pack"][field] = deepcopy(prior["wild_pack"][field])
    helpers = saved_functions(source / "g11/relay/final_bet_accounting_v1.py", {"_score", "_final"})
    score = helpers["_score"](lock["practical_bets"], lock["main_head"], {"2-1-3": 1200}, set())
    final = new["final_accounting"]
    target = final["by_race"][KEY]
    previous = target["engines"]["abeken"]
    require(previous["final_status"] == "FINAL_MORNING_ONLY"
            and previous["final_point_count"] == 15
            and previous["final_bet_snapshot_id"] == v53["morning"]["snapshot_sha256"]
            and previous["final_bets"] == v53["morning"]["practical_bets"]
            and previous["score"]["profit"] == -1500
            and target["official_refund_boats"] == [], "EXPECTED_ACCOUNTING_CONFLICT")
    fixed = helpers["_final"]("FINAL_PRE_RACE", "PRE_RACE", LOCK_SHA,
            lock["locked_at_jst"], list(lock["practical_bets"]), lock["main_head"])
    fixed["score"] = score
    target["engines"]["abeken"] = fixed
    # MISS/display/classification are unchanged for this saved result.
    require(score["trifecta_hit"] is False and target["display"]["engines"]["abeken"]["outcome"] == "MISS", "OUTCOME")
    aggregate = final["engines"]["abeken"]
    for field, value in score.items():
        if field != "roi":
            aggregate[field] += int(value) - int(previous["score"][field])
    aggregate["total_points"] += fixed["final_point_count"] - previous["final_point_count"]
    aggregate["average_points"] = aggregate["total_points"] / aggregate["races"]
    aggregate["roi"] = aggregate["prize_return"] / aggregate["net_investment"]
    final["source_counts"]["abeken"]["FINAL_MORNING_ONLY"] -= 1
    final["source_counts"]["abeken"]["FINAL_PRE_RACE"] += 1
    comparison = final["morning_pre_race_research"]
    per = comparison["by_race"][KEY]["abeken"]
    require(per["pre_race"] is None and per["morning"] == previous["score"], "COMPARISON_BASE")
    per.update(pre_race=deepcopy(score), bets_changed=True, point_delta=-6,
               payout_delta=0, profit_delta=600)
    total = comparison["summary"]["abeken"]
    total["pre_race_races"] += 1
    total["pre_race_not_issued"] -= 1
    for field in ("head_hit", "trifecta_hit", "gross_stake", "refund_amount",
                  "net_investment", "prize_return", "profit"):
        total["pre_race_" + field] += int(score[field])
    total["point_delta"] -= 6
    total["profit_delta"] += 600
    # Reuse the existing read-model scoring contract for the affected race only.
    research = saved_functions(source / "g11/canonical/public_app_feed.py",
                              {"_research_score", "_race_result_final"})
    race["abeken_score_breakdown"]["authority"] = "V53_PRE_RACE"
    race["abeken_score_breakdown"]["v53"] = research["_research_score"](
        lock["main_head"], lock["practical_bets"], race)
    for key in rows:
        if key != KEY:
            require(rows[key] == next(r for r in feed["races"] if r["key"] == key)
                    and final["by_race"][key] == feed["final_accounting"]["by_race"][key], "UNRELATED_RACE_MUTATED")
    return new


def replace_bytes(path, data):
    fd, temp = tempfile.mkstemp(prefix=".night-recovery-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def recover(state, output, source, run_id, origin):
    require(origin == "https://g11-race-lab.higatatsunori2.chatgpt.site", "ORIGIN")
    impl = retained_module(state, DAY)
    impl.verify_runtime(state, DAY, source)
    root = impl.day_root(state, DAY)
    archive = root / "recovery-20261013-published-lock"
    receipt_path = archive / "receipt.json"
    # A persisted revision may only be resumed byte-for-byte, never recalculated.
    if receipt_path.exists():
        receipt = impl.json_file(receipt_path)
        for name, sha in receipt["revision_files"].items():
            require(impl.digest((archive / name).read_bytes()) == sha, "REVISION_HASH")
        for name in ("night-feed.json", "night-checkpoint.json"):
            replace_bytes(root / name, (archive / ("revised-" + name)).read_bytes())
        plan = root / "night-transport-plan.json"
        if plan.exists() and impl.json_file(plan).get("checkpoint_sha256") == OLD_CHECKPOINT:
            require(plan.read_bytes() == (archive / "prior-night-transport-plan.json").read_bytes(), "PRIOR_PLAN_HASH")
            plan.unlink()
        checkpoint = impl.read_checkpoint(state, DAY)
        impl.emit_checkpoint(state, DAY, output)
        return {"STATUS": "NIGHT_RECOVERY_ALREADY_SAVED", "PHASE": "NIGHT", "OPERATIONAL_DATE_JST": DAY,
                "FEED_EMITTED": True, "CHECKPOINT_SHA256": checkpoint["checkpoint_sha256"],
                "PREDICTION_RECALCULATION": 0, "REPAIRED_RACES": [KEY]}
    checkpoint = impl.read_checkpoint(state, DAY)
    require(checkpoint["checkpoint_sha256"] == OLD_CHECKPOINT, "CHECKPOINT_BASE_CHANGED")
    accepted, headers, raw = hot.fetch(origin + "/api/g11-feed?date=" + DAY)
    require(impl.digest(raw) == headers.get("x-g11-source-feed-sha256") == BASE_SHA, "ACCEPTED_BASE_CHANGED")
    feed = impl.json_file(root / "night-feed.json")
    before_store = {p.relative_to(state / "store").as_posix(): impl.digest(p.read_bytes())
                    for p in (state / "store").rglob("*") if p.is_file()}
    repaired = correct_feed(feed, accepted, source)
    now = datetime.now(JST).isoformat()
    repaired["generated_at_jst"] = now
    repaired["source"]["run_id"] = int(run_id)
    impl.verify_finance(repaired)
    data = impl.canonical(repaired) + b"\n"
    revised = deepcopy(checkpoint)
    revised["feed_sha256"] = impl.digest(data)
    revised["recovery"] = {"prior_checkpoint_sha256": OLD_CHECKPOINT,
                           "accepted_feed_sha256": BASE_SHA, "race_keys": [KEY],
                           "recorded_at_jst": now, "original_private_lock_recreated": False}
    revised.pop("checkpoint_sha256")
    revised["checkpoint_sha256"] = impl.digest(impl.canonical(revised))
    revised_bytes = impl.canonical(revised) + b"\n"
    # Archive first. Original official RAW, results, predictions and research stay untouched.
    for name in ("night-feed.json", "night-checkpoint.json", "night-transport-plan.json"):
        if (root / name).is_file():
            impl.immutable(archive / ("prior-" + name), (root / name).read_bytes())
    impl.immutable(archive / "accepted-predeadline.json", raw)
    impl.immutable(archive / "revised-night-feed.json", data)
    impl.immutable(archive / "revised-night-checkpoint.json", revised_bytes)
    receipt = {"schema": "G11_SCOPED_NIGHT_RECOVERY_RECEIPT_V1", "operational_date_jst": DAY,
               "recorded_at_jst": now, "run_id": int(run_id), "race_keys": [KEY],
               "accepted_feed_sha256": BASE_SHA, "prior_checkpoint_sha256": OLD_CHECKPOINT,
               "revised_checkpoint_sha256": revised["checkpoint_sha256"],
               "saved_lock_sha256": LOCK_SHA, "previous_points": 15, "correct_points": 9,
               "previous_profit": -1500, "correct_profit": -900,
               "normal_race_count_unchanged": 131, "prediction_recalculated": False,
               "store_before_sha256": impl.digest(impl.canonical(before_store)),
               "revision_files": {"revised-night-feed.json": impl.digest(data),
                                  "revised-night-checkpoint.json": impl.digest(revised_bytes)}}
    impl.immutable(receipt_path, impl.canonical(receipt) + b"\n")
    replace_bytes(root / "night-feed.json", data)
    replace_bytes(root / "night-checkpoint.json", revised_bytes)
    if (root / "night-transport-plan.json").exists():
        (root / "night-transport-plan.json").unlink()  # immutable prior plan remains in archive
    require(impl.read_checkpoint(state, DAY) == revised, "CHECKPOINT_READBACK")
    after_store = {p.relative_to(state / "store").as_posix(): impl.digest(p.read_bytes())
                   for p in (state / "store").rglob("*") if p.is_file()}
    require(before_store == after_store, "SOURCE_STORE_MUTATED")
    impl.emit_checkpoint(state, DAY, output)
    print("G11_NIGHT_RECOVERY_SAVE_READBACK=PASS " + json.dumps(receipt, sort_keys=True))
    return {"STATUS": "NIGHT_RECOVERY_SAVED", "PHASE": "NIGHT", "OPERATIONAL_DATE_JST": DAY,
            "FEED_EMITTED": True, "CHECKPOINT_SHA256": revised["checkpoint_sha256"],
            "PREDICTION_RECALCULATION": 0, "REPAIRED_RACES": [KEY], "STORE_MUTATED": False}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--phase"); p.add_argument("--operational-date")
    p.add_argument("--state-root", type=Path); p.add_argument("--feed-output", type=Path)
    p.add_argument("--public-run-id"); p.add_argument("--app-origin")
    args, unused = p.parse_known_args()
    require(args.phase == "night" and args.operational_date == DAY
            and os.environ.get("G11_NIGHT_RECOVERY_20261013") == "true", "SCOPE")
    value = recover(args.state_root, args.feed_output, Path.cwd(), args.public_run_id, args.app_origin)
    print(json.dumps(value, sort_keys=True))


if __name__ == "__main__":
    main()
