#!/usr/bin/env python3
"""Bounded Site transport. The private encrypted state remains the full authority."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from collections import Counter

SCHEMA = "G11_SITE_HOT_DELTA_V1"
BUDGET = 900_000
PRE_FIELDS = (
    "predeadline", "pre_race_v2", "odds_merit", "best_ev", "best_ev_bet",
    "value_bets", "odds_captured_at_jst", "p3_verification", "abeken_shadow",
    "wild_pack", "trifecta_confidence",
)
NIGHT_FIELDS = PRE_FIELDS + (
    "result_trifecta", "payout", "practical_hit", "validation_hit",
    "miss_classification", "research_decision", "result_meta",
    "research_finance", "actual_purchase", "three_engine_score",
    "three_engine_classification",
)
NIGHT_TOP = (
    "research", "research_db", "three_engine_comparison", "final_accounting",
    "capabilities",
)
PRE_KEYS = {
    "status", "snapshot_sha256", "captured_at_jst", "odds_captured_at_jst",
    "top_boat", "second_boat", "axis", "quality", "turbulence",
    "practical_bets", "production_points", "prediction", "caution",
    "odds_merit", "legacy_odds_merit", "best_ev", "best_ev_bet",
    "value_bets", "weather", "wind_speed", "wind_direction_code",
    "wave_height", "course_display", "morning_lock_mutated",
    "scoring_policy", "purchase_executed", "production_mean_ev",
    "value_alert", "value_alert_type", "trifecta_confidence",
    "special_case_id", "special_strength", "strong_mark", "pre_race_impact",
}
VERIFICATION_KEYS = {
    "SCHEMA", "RACE_ID", "MASHIRO_DEFINITION", "MASHIRO_EVALUATION_STATUS",
    "MASHIRO_OPPORTUNITY", "MASHIRO_CANDIDATE", "MASHIRO_ATTRIBUTE_STATUS",
    "MASHIRO_BOAT", "MASHIRO_RACER_NUMBER", "MASHIRO_RACER_NAME",
    "MASHIRO_EXHIBITION_COURSE", "MASHIRO_ACTUAL_COURSE",
    "MASHIRO_P3_SCORE", "MASHIRO_P3_RANK", "MASHIRO_IS_P3_HEAD",
    "MASHIRO_INCLUDED_IN_PRODUCTION_BETS", "MASHIRO_INCLUDED_BET_COUNT",
    "MASHIRO_INCLUDED_BETS", "MASHIRO_INCLUDED_IN_VERIFICATION10",
    "MASHIRO_VERIFICATION10_BET_COUNT", "MASHIRO_FINISH", "MASHIRO_HIT",
    "MASHIRO_P3_CLASSIFICATION", "MASHIRO_RESULT_CLASSIFICATION",
    "MASHIRO_TARGETS", "INNER_A1_EXISTS", "INNER_A1_BOATS",
    "INNER_A1_COURSES", "INNER_A1_RACER_NUMBERS", "RESULT_JOIN_STATUS",
    "P3_SNAPSHOT_SHA256", "MASHIRO_SHADOW_SNAPSHOT_SHA256",
    "COUNTERFACTUAL_LINK", "SPECIAL_45_ACTIVE", "SPECIAL_45_STRENGTH",
    "SPECIAL_45_FIXED_8", "SPECIAL_45_PRODUCTION_POINTS",
    "SPECIAL_45_FIXED_BETS",
}
TARGET_KEYS = {
    "MASHIRO_BOAT", "MASHIRO_EVALUATION_STATUS", "MASHIRO_OPPORTUNITY",
    "MASHIRO_CANDIDATE", "MASHIRO_DISCOVERY54_MEMBER", "MASHIRO_ATTRIBUTE_STATUS",
    "MASHIRO_RACER_NUMBER", "MASHIRO_RACER_NAME", "MASHIRO_EXHIBITION_COURSE",
    "MASHIRO_ACTUAL_COURSE", "MASHIRO_P3_SCORE", "MASHIRO_P3_RANK",
    "MASHIRO_IS_P3_HEAD", "MASHIRO_INCLUDED_IN_PRODUCTION_BETS",
    "MASHIRO_INCLUDED_BET_COUNT", "MASHIRO_INCLUDED_BETS",
    "MASHIRO_INCLUDED_IN_VERIFICATION10", "MASHIRO_VERIFICATION10_BET_COUNT",
    "MASHIRO_VERIFICATION10_STATUS", "MASHIRO_ACTUAL_OPPORTUNITY",
    "MASHIRO_FINISH", "MASHIRO_HIT", "MASHIRO_P3_CLASSIFICATION",
    "MASHIRO_RESULT_CLASSIFICATION", "INNER_A1_EXISTS", "INNER_A1_BOATS",
    "INNER_A1_COURSES", "INNER_A1_RACER_NUMBERS", "RESULT_JOIN_STATUS",
    "COUNTERFACTUAL_LINK",
}


def compact(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def project(field: str, value: object) -> object:
    if field == "predeadline" and isinstance(value, dict):
        return {k: v for k, v in value.items() if k in PRE_KEYS}
    if field == "p3_verification" and isinstance(value, dict):
        out = {k: v for k, v in value.items() if k in VERIFICATION_KEYS}
        if isinstance(out.get("MASHIRO_TARGETS"), list):
            out["MASHIRO_TARGETS"] = [
                {k: v for k, v in target.items() if k in TARGET_KEYS}
                for target in out["MASHIRO_TARGETS"] if isinstance(target, dict)
            ]
        return out
    if field == "abeken_shadow" and isinstance(value, dict):
        out = {}
        if isinstance(value.get("v53"), dict):
            v53 = value["v53"]
            out["v53"] = {k: v53[k] for k in (
                "live", "formal_use", "runtime_logic_version",
            ) if k in v53}
        if "live" in value:
            out["live"] = value["live"]
        return out
    if field == "wild_pack" and isinstance(value, dict):
        return {k: value[k] for k in (
            "pre_race", "pre_race_stage", "pre_race_history", "changed",
        ) if k in value}
    return value


def diff(old: object, new: object) -> object:
    if isinstance(old, dict) and isinstance(new, dict):
        changed = {}
        for key, value in new.items():
            if key not in old:
                changed[key] = value
            elif old[key] != value:
                child = diff(old[key], value)
                if child != {}:
                    changed[key] = child
        return changed
    return new


def includes(actual: object, expected: object) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and includes(actual[key], value)
            for key, value in expected.items()
        )
    return actual == expected


def fetch(url: str) -> tuple[dict, dict[str, str], bytes]:
    body_path = Path(".relay-output/hot-get-body.json")
    header_path = Path(".relay-output/hot-get-headers.txt")
    subprocess.run([
        "curl", "--fail-with-body", "--silent", "--show-error", "--location",
        "--max-time", "35", "--dump-header", str(header_path),
        "--output", str(body_path), url,
    ], check=True)
    body = body_path.read_bytes()
    headers = {}
    for line in header_path.read_text().splitlines():
        if line.startswith("HTTP/"):
            headers = {}
        elif ":" in line:
            key, value = line.split(":", 1)
            headers[key.lower()] = value.strip()
    if hash_bytes(body) != headers.get("x-g11-feed-sha256"):
        raise RuntimeError("HOT_READBACK_SHA")
    return json.loads(body), headers, body


def create_delta(incoming: dict, accepted: dict, base_sha: str) -> dict:
    day = incoming["operational_date_jst"]
    if day != accepted["operational_date_jst"] or incoming["stage"] not in ("PREDEADLINE", "NIGHT"):
        raise RuntimeError("HOT_DATE_OR_STAGE")
    old = {row["key"]: row for row in accepted["races"]}
    if incoming["stage"] == "NIGHT":
        counts = incoming.get("counts", {})
        if (counts.get("results") != counts.get("races") or
                counts.get("pending") != 0 or
                counts.get("races") != len(incoming["races"]) or
                {row["key"] for row in incoming["races"]} != set(old)):
            raise RuntimeError("HOT_NIGHT_INCOMPLETE_SOURCE")
    changes = []
    allowed = NIGHT_FIELDS if incoming["stage"] == "NIGHT" else PRE_FIELDS
    for row in incoming["races"]:
        prior = old.get(row["key"])
        if prior is None:
            raise RuntimeError("HOT_UNKNOWN_RACE")
        fields = {}
        for field in allowed:
            if field not in row or row[field] is None and incoming["stage"] != "NIGHT":
                continue
            current = project(field, row[field])
            previous = prior.get(field)
            delta = diff(previous, current)
            if previous != current and delta != {}:
                fields[field] = delta
        if not fields:
            continue
        live = row.get("predeadline")
        if isinstance(live, dict) and isinstance(fields.get("predeadline"), dict):
            from datetime import datetime
            captured = datetime.fromisoformat(live["captured_at_jst"])
            deadline = datetime.fromisoformat(row["deadline_jst"])
            if captured >= deadline or len(str(live.get("snapshot_sha256", ""))) != 64:
                raise RuntimeError("HOT_POST_DEADLINE_FORECAST")
        version = (prior.get("predeadline") or {}).get("snapshot_sha256") or prior.get("p3_snapshot_sha256")
        changes.append({"RACE_KEY": row["key"], "EXPECTED_RACE_VERSION": version, "CHANGED_FIELDS": fields})
    top = {"source": incoming["source"], "generated_at_jst": incoming["generated_at_jst"]}
    if incoming["stage"] == "NIGHT":
        top.update({key: incoming[key] for key in NIGHT_TOP if key in incoming})
    fingerprint = hash_bytes(compact({"stage": incoming["stage"], "day": day, "top": top, "races": changes}))
    changed = Counter(field for race in changes for field in race["CHANGED_FIELDS"])
    print("G11_HOT_DELTA_FIELD_COUNTS=" + json.dumps(changed, sort_keys=True))
    return {
        "SCHEMA": SCHEMA, "OPERATIONAL_DATE": day, "BASE_FEED_SHA": base_sha,
        "PATCH_SEQUENCE": incoming["source"]["run_id"] * 1000 + 1,
        "PATCH_CREATED_AT": incoming["generated_at_jst"],
        "PATCH_SHA256": fingerprint, "STAGE": incoming["stage"],
        "TOP_LEVEL": top, "RACES": changes,
    }


def publish(feed_path: Path, origin: str) -> None:
    incoming = json.loads(feed_path.read_text(encoding="utf-8"))
    day = incoming["operational_date_jst"]
    url = f"{origin}/api/g11-feed?date={day}"
    accepted, headers, _ = fetch(url)
    base_sha = headers["x-g11-source-feed-sha256"]
    delta = create_delta(incoming, accepted, base_sha)
    data = compact(delta)
    print("G11_HOT_DELTA_BYTES=" + str(len(data)))
    print("G11_HOT_DELTA_RACES=" + str(len(delta["RACES"])))
    if len(data) > BUDGET:
        raise RuntimeError("HOT_DELTA_OVER_BUDGET:" + str(len(data)))
    if incoming["stage"] == "PREDEADLINE" and not delta["RACES"]:
        print("G11_HOT_DELTA_NO_CHANGE=PASS")
        Path(".relay-output/hot-delta-accepted").write_text(base_sha + "\n")
        Path(os.environ["GITHUB_OUTPUT"]).open("a").write("feed_sha=" + base_sha + "\n")
        return
    token = os.environ["G11_SYNC_OIDC"]
    request_path = Path(".relay-output/hot-delta-request.json")
    response_path = Path(".relay-output/hot-delta-response.json")
    request_path.write_bytes(data)
    result = subprocess.run([
        "curl", "--silent", "--show-error", "--location", "--max-time", "35",
        "-X", "POST", f"{origin}/api/g11-hot-delta",
        "-H", "Authorization: Bearer " + token,
        "-H", "Content-Type: application/json",
        "-H", "x-g11-feed-sha256: " + hash_bytes(data),
        "--data-binary", "@" + str(request_path), "--output", str(response_path),
        "--write-out", "%{http_code}",
    ], check=True, capture_output=True, text=True)
    status = int(result.stdout)
    print("G11_HOT_DELTA_HTTP=" + str(status))
    body = json.loads(response_path.read_text())
    if status < 200 or status >= 300:
        raise RuntimeError("HOT_DELTA_REJECTED:" + str(body.get("error")))
    if body.get("status") != "PASS":
        raise RuntimeError("HOT_DELTA_REJECTED")
    readback, next_headers, raw = fetch(url)
    if next_headers.get("x-g11-source-feed-sha256") != body.get("payload_sha256"):
        raise RuntimeError("HOT_DELTA_SOURCE_SHA")
    for field, value in delta["TOP_LEVEL"].items():
        if field not in readback or not includes(readback[field], value):
            raise RuntimeError("HOT_DELTA_READBACK_TOP:" + field)
    rows = {row["key"]: row for row in readback["races"]}
    for change in delta["RACES"]:
        key = change["RACE_KEY"]
        for field, value in change["CHANGED_FIELDS"].items():
            if field not in rows[key] or not includes(rows[key][field], value):
                raise RuntimeError("HOT_DELTA_READBACK_FIELD:" + key + ":" + field)
    if incoming["stage"] == "NIGHT":
        counts = readback["counts"]
        if (readback["stage"] != "NIGHT" or counts["pending"] != 0 or
                counts["results"] != counts["races"] or
                len(rows) != len(incoming["races"])):
            raise RuntimeError("HOT_NIGHT_COVERAGE")
    Path(".relay-output/hot-delta-accepted").write_text(body["payload_sha256"] + "\n")
    Path(".relay-output/hot-delta-readback.json").write_bytes(raw)
    Path(os.environ["GITHUB_OUTPUT"]).open("a").write("feed_sha=" + body["payload_sha256"] + "\n")
    print("G11_HOT_DELTA_READBACK=PASS SOURCE_SHA=" + body["payload_sha256"])


if __name__ == "__main__":
    try:
        publish(Path(sys.argv[1]), sys.argv[2].rstrip("/"))
    except Exception as error:
        print("G11_HOT_DELTA_FAIL=" + str(error), file=sys.stderr)
        raise SystemExit(1)
