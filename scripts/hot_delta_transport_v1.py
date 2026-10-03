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
NIGHT_TARGET_CHUNK_BYTES = 295_000  # JSON envelope keeps wire chunks below 300 KB.
NIGHT_HARD_CHUNK_BYTES = 500_000
NIGHT_TRANSACTION_SCHEMA = "G11_NIGHT_TRANSACTION_V1"
BASE_BUDGET = 1_700_000
BASE_TOP_FIELDS = {
    "schema", "status", "stage", "operational_date_jst", "generated_at_jst",
    "source", "counts", "research", "research_db", "integrity", "capabilities",
    "canonical_authorities", "wild_pack", "true_ai", "abeken",
    "three_engine_comparison", "original_display_research",
    "conditional_finish_research", "logic_candidate_research", "late_reference_summary",
    "final_accounting", "races", "daily_runtime",
}
BASE_RACE_FIELDS = {
    "key", "date_jst", "venue", "venue_code", "race", "deadline_jst",
    "formal_status", "exclusion_reason", "research_eligible", "top_boat",
    "second_boat", "axis", "quality", "turbulence", "practical_bets",
    "production_points", "prediction", "caution", "p3_head", "p3_logic_id",
    "p3_production_bets", "p3_published_at_jst", "p3_snapshot",
    "p3_snapshot_sha256", "p3_status", "special_case_id", "special_strength",
    "strong_mark", "trifecta_confidence", "predeadline", "pre_race_v2",
    "predeadline_exclusion_reason", "odds_merit", "best_ev", "best_ev_bet",
    "value_bets", "odds_captured_at_jst", "result_trifecta", "payout",
    "practical_hit", "validation_hit", "miss_classification", "research_decision",
    "result_meta", "research_finance", "actual_purchase", "abeken_shadow",
    "wild_pack", "original_display_shadow", "three_engine_score",
    "three_engine_classification", "late_reference", "abeken_score_breakdown",
}
PRE_FIELDS = (
    "predeadline", "pre_race_v2", "odds_merit", "best_ev", "best_ev_bet",
    "value_bets", "odds_captured_at_jst", "p3_verification", "abeken_shadow",
    "wild_pack", "trifecta_confidence",
)
NIGHT_FIELDS = PRE_FIELDS + (
    "result_trifecta", "payout", "practical_hit", "validation_hit",
    "miss_classification", "research_decision", "result_meta",
    "research_finance", "actual_purchase", "three_engine_score",
    "three_engine_classification", "abeken_score_breakdown",
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
MASHIRO_HOT_TARGET_KEYS = {
    "MASHIRO_BOAT", "MASHIRO_OPPORTUNITY", "MASHIRO_RACER_NAME",
    "MASHIRO_EXHIBITION_COURSE", "MASHIRO_ACTUAL_COURSE", "MASHIRO_P3_RANK",
    "MASHIRO_INCLUDED_BET_COUNT", "MASHIRO_VERIFICATION10_STATUS",
    "MASHIRO_VERIFICATION10_BET_COUNT", "MASHIRO_FINISH", "MASHIRO_HIT",
    "RESULT_JOIN_STATUS",
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


def site_hash(value: object) -> str:
    """Hash the JSON value as the Site validates it after JavaScript JSON.parse."""
    result = subprocess.run(
        ["node", "-e", "const crypto=require('node:crypto');let data='';"
         "process.stdin.on('data',chunk=>data+=chunk);"
         "process.stdin.on('end',()=>process.stdout.write(crypto.createHash('sha256')"
         ".update(JSON.stringify(JSON.parse(data))).digest('hex')))"],
        input=compact(value), capture_output=True, check=True,
    )
    return result.stdout.decode("ascii")


def reproject_completed_night(store: Path, output: Path, run_id: str,
                              source_sha: str, checked_out_sha: str) -> None:
    """Read the completed immutable NIGHT state without invoking acquisition or settlement."""
    import re
    if not run_id.isdecimal() or not re.fullmatch(r"[a-f0-9]{40}", source_sha):
        raise RuntimeError("NIGHT_TRANSPORT_SOURCE_ID_REQUIRED")
    if source_sha != checked_out_sha:
        raise RuntimeError("NIGHT_TRANSPORT_PRIVATE_SOURCE_CHANGED")
    from g11.relay import v1 as relay
    from g11.growth_p3.v1_1 import relay_publish_v1 as current
    night_path = store / "snapshots" / "night" / "workbook-ledger-projection.json"
    if not night_path.is_file():
        raise RuntimeError("NIGHT_TRANSPORT_CANONICAL_MISSING")
    canonical = json.loads(night_path.read_text())
    if canonical.get("PAYLOAD", {}).get("COUNTS", {}).get("PENDING_RACES") != 0:
        raise RuntimeError("NIGHT_TRANSPORT_CANONICAL_INCOMPLETE")
    before = relay._tree_sha(store)
    publication = current.load_locked_publication(
        store, relay._morning_projection(store), relay._now_jst())
    feed = relay.build_feed(
        store, run_id=int(run_id), github_sha=source_sha,
        current_p3_publication=publication, relay_phase="night")
    if (feed["stage"] != "NIGHT" or feed["status"] != "PASS" or
            feed["counts"]["pending"] != 0 or
            feed["counts"]["results"] != feed["counts"]["races"] or
            relay._tree_sha(store) != before):
        raise RuntimeError("NIGHT_TRANSPORT_CANONICAL_MUTATION_OR_COVERAGE")
    relay._write_json(output, feed)
    (output.parent / "night-canonical.sha").write_text(
        relay._sha(night_path) + "  " + str(night_path) + "\n", encoding="utf-8")
    print("G11_NIGHT_TRANSPORT_ONLY_CANONICAL_SHA=" + relay._sha(night_path))
    print("G11_NIGHT_TRANSPORT_ONLY_STORE_SHA=" + before)
    print("G11_NIGHT_TRANSPORT_ONLY_RESULTS=" + str(feed["counts"]["results"]))


def mashiro_hot(value: dict) -> dict:
    state = value["MASHIRO_EVALUATION_STATUS"]
    return {
        "SCHEMA": "G11_MASHIRO_HOT_SUMMARY_V1", "RACE_ID": value["RACE_ID"],
        "MASHIRO_EVALUATION_STATUS": state,
        "MASHIRO_OPPORTUNITY": value["MASHIRO_OPPORTUNITY"],
        "P3_SNAPSHOT_SHA256": value.get("P3_SNAPSHOT_SHA256"),
        "MASHIRO_TARGETS": [
            {k: v for k, v in target.items() if k in MASHIRO_HOT_TARGET_KEYS}
            for target in value.get("MASHIRO_TARGETS", []) if isinstance(target, dict)
        ] if state == "PRESENT" else [],
        "INNER_A1_COURSES": value.get("INNER_A1_COURSES"),
        "SPECIAL_45_ACTIVE": value.get("SPECIAL_45_ACTIVE"),
        "SPECIAL_45_STRENGTH": value.get("SPECIAL_45_STRENGTH"),
        "RESEARCH_REF_SHA256": hash_bytes(compact(value)),
    }


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


def project_morning_base(path: Path) -> None:
    source = path.read_bytes()
    feed = json.loads(source)
    if feed.get("stage") != "MORNING" or not isinstance(feed.get("races"), list):
        raise RuntimeError("HOT_BASE_STAGE")
    base = {key: value for key, value in feed.items() if key in BASE_TOP_FIELDS}
    base["races"] = [
        {key: value for key, value in race.items() if key in BASE_RACE_FIELDS}
        for race in feed["races"]
    ]
    # Full p3_verification and all unknown research fields remain in the
    # encrypted canonical state; only an identity of the source is published.
    base["hot_reference"] = {"schema": "G11_SITE_HOT_BASE_V1",
        "source_feed_sha256": hash_bytes(source), "status": "COLD_RESEARCH_RETAINED"}
    data = compact(base)
    print("G11_HOT_BASE_BYTES=" + str(len(data)))
    if len(data) > BASE_BUDGET:
        raise RuntimeError("HOT_BASE_OVER_BUDGET:" + str(len(data)))
    path.write_bytes(data + b"\n")


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
            if field not in row or row[field] is None and not (
                incoming["stage"] == "NIGHT" and
                field in {"result_trifecta", "payout", "miss_classification"}
            ):
                continue
            output_field = "mashiro_hot" if field == "p3_verification" and "p3_verification" not in prior else field
            current = mashiro_hot(row[field]) if output_field == "mashiro_hot" else project(field, row[field])
            previous = prior.get(output_field)
            delta = diff(previous, current)
            if previous != current and delta != {}:
                fields[output_field] = delta
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
    if "daily_runtime" in incoming:
        top["daily_runtime"] = incoming["daily_runtime"]
    if incoming["stage"] == "NIGHT":
        top.update({key: incoming[key] for key in NIGHT_TOP if key in incoming})
    fingerprint = hash_bytes(compact({"stage": incoming["stage"], "day": day, "top": top, "races": changes}))
    changed = Counter(field for race in changes for field in race["CHANGED_FIELDS"])
    print("G11_HOT_DELTA_FIELD_COUNTS=" + json.dumps(changed, sort_keys=True))
    if incoming["stage"] == "NIGHT":
        sizes = Counter()
        for race in changes:
            for field, value in race["CHANGED_FIELDS"].items():
                sizes[field] += len(compact(value))
        print("G11_NIGHT_FIELD_BYTES=" + json.dumps(sizes.most_common(), ensure_ascii=False))
    return {
        "SCHEMA": SCHEMA, "OPERATIONAL_DATE": day, "BASE_FEED_SHA": base_sha,
        "PATCH_SEQUENCE": incoming["source"]["run_id"] * 1000 + 1,
        "PATCH_CREATED_AT": incoming["generated_at_jst"],
        "PATCH_SHA256": fingerprint, "STAGE": incoming["stage"],
        "TOP_LEVEL": top, "RACES": changes,
    }


def post(origin: str, token: str, payload: dict, endpoint: str) -> dict:
    data = compact(payload)
    request_path = Path(".relay-output/night-transport-request.json")
    response_path = Path(".relay-output/night-transport-response.json")
    request_path.write_bytes(data)
    result = subprocess.run([
        "curl", "--silent", "--show-error", "--max-time", "45",
        "-X", "POST", f"{origin}{endpoint}",
        "-H", "Authorization: Bearer " + token,
        "-H", "Content-Type: application/json",
        "-H", "x-g11-feed-sha256: " + hash_bytes(data),
        "--data-binary", "@" + str(request_path), "--output", str(response_path),
        "--write-out", "%{http_code}",
    ], check=True, capture_output=True, text=True)
    status = int(result.stdout)
    body = json.loads(response_path.read_text())
    print("G11_NIGHT_TRANSACTION_HTTP=" + str(status) + " ACTION=" + payload["ACTION"])
    if not 200 <= status < 300 or body.get("status") != "PASS":
        raise RuntimeError("NIGHT_TRANSACTION_REJECTED:" + str(body.get("error")))
    return body


def night_chunks(delta: dict, incoming: dict, checkpoint: dict | None = None) -> tuple[dict, list[dict]]:
    source_sha = incoming["source"]["projection_sha256"]
    if len(source_sha) != 64:
        raise RuntimeError("NIGHT_SOURCE_SHA_REQUIRED")
    top = delta["TOP_LEVEL"]
    date = delta["OPERATIONAL_DATE"]
    keys = sorted(row["key"] for row in incoming["races"])

    def nested(path: tuple[str, ...], value: object) -> dict:
        for key in reversed(path):
            value = {key: value}
        return value

    def fragments(value: object, path: tuple[str, ...] = ()):
        fragment = nested(path, value)
        if len(compact(fragment)) < NIGHT_TARGET_CHUNK_BYTES - 1500:
            yield fragment
        elif isinstance(value, dict) and value:
            for key, child in value.items():
                yield from fragments(child, path + (key,))
        else:
            raise RuntimeError("NIGHT_TOP_FIELD_EXCEEDS_CHUNK_BUDGET:" + ".".join(path))

    def merge(base: dict, patch: dict) -> dict:
        result = base.copy()
        for key, value in patch.items():
            result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else value
        return result

    top_chunks: list[dict] = []
    current_top: dict = {}
    for fragment in fragments(top):
        candidate = merge(current_top, fragment)
        if len(compact(candidate)) > NIGHT_TARGET_CHUNK_BYTES - 1500 and current_top:
            top_chunks.append(current_top)
            current_top = fragment
        else:
            current_top = candidate
    if current_top:
        top_chunks.append(current_top)
    race_chunks: list[list[dict]] = []
    current: list[dict] = []
    for row in delta["RACES"]:
        candidate = current + [row]
        if len(compact(candidate)) > NIGHT_TARGET_CHUNK_BYTES and current:
            race_chunks.append(current)
            current = [row]
        else:
            current = candidate
        if len(compact(current)) > NIGHT_HARD_CHUNK_BYTES - 1500:
            raise RuntimeError("NIGHT_RACE_EXCEEDS_CHUNK_BUDGET:" + row["RACE_KEY"])
    if current:
        race_chunks.append(current)
    if not race_chunks:
        raise RuntimeError("NIGHT_NO_CHANGED_RACES")
    parts = [(part, []) for part in top_chunks] + [({}, rows) for rows in race_chunks]
    chunk_hashes = [site_hash({"TOP_LEVEL": part, "RACES": rows}) for part, rows in parts]
    transaction_id = hash_bytes(compact([date, delta["BASE_FEED_SHA"], source_sha, chunk_hashes]))
    manifest = {
        "SCHEMA": NIGHT_TRANSACTION_SCHEMA, "ACTION": "MANIFEST",
        "OPERATIONAL_DATE": date, "NIGHT_TRANSACTION_ID": transaction_id,
        "BASE_FEED_SHA": delta["BASE_FEED_SHA"], "SOURCE_NIGHT_SHA": source_sha,
        "PATCH_SEQUENCE": delta["PATCH_SEQUENCE"],
        "PATCH_CREATED_AT": delta["PATCH_CREATED_AT"], "PATCH_SHA256": delta["PATCH_SHA256"],
        "CHUNK_COUNT": len(parts), "CHUNK_SHA256": chunk_hashes,
        "EXPECTED_RACES": incoming["counts"]["races"],
        "EXPECTED_RESULTS": incoming["counts"]["results"],
        "EXPECTED_PENDING": incoming["counts"]["pending"],
        "EXPECTED_RACE_KEY_SET_SHA": site_hash(keys),
        "EXPECTED_FINAL_ACCOUNTING_SHA": site_hash(top["final_accounting"]),
        "EXPECTED_TOP_LEVEL_SHA": site_hash(top),
        "EXPECTED_FINANCE_SHA": site_hash([
            [row["RACE_KEY"], row["CHANGED_FIELDS"].get("research_finance")]
            for row in delta["RACES"]
        ]),
    }
    if checkpoint is not None:
        if (checkpoint["operational_date_jst"] != date or checkpoint["pending"] != 0
                or checkpoint["authority_sha256"] != incoming["daily_runtime"]["authority_sha256"]):
            raise RuntimeError("NIGHT_CHECKPOINT_TRANSPORT_IDENTITY")
        manifest.update({"DAILY_AUTHORITY_SHA256": checkpoint["authority_sha256"],
                         "CANONICAL_FEED_SHA256": checkpoint["feed_sha256"],
                         "CHECKPOINT_SHA256": checkpoint["checkpoint_sha256"]})
    manifest["MANIFEST_SHA256"] = hash_bytes(compact(manifest))
    if len(compact(manifest)) > NIGHT_HARD_CHUNK_BYTES:
        raise RuntimeError("NIGHT_MANIFEST_EXCEEDS_HARD_BUDGET:" + str(len(compact(manifest))))
    chunks = [{
        "SCHEMA": NIGHT_TRANSACTION_SCHEMA, "ACTION": "CHUNK",
        "OPERATIONAL_DATE": date, "NIGHT_TRANSACTION_ID": transaction_id,
        "MANIFEST_SHA256": manifest["MANIFEST_SHA256"], "SOURCE_NIGHT_SHA": source_sha,
        "CHUNK_INDEX": i, "CHUNK_COUNT": len(parts),
        "CHUNK_SHA256": chunk_hashes[i],
        "RACE_KEYS": [row["RACE_KEY"] for row in rows], "TOP_LEVEL": part,
        "RACES": rows,
    } for i, (part, rows) in enumerate(parts)]
    if any(len(compact(chunk)) > NIGHT_HARD_CHUNK_BYTES for chunk in chunks):
        raise RuntimeError("NIGHT_CHUNK_EXCEEDS_HARD_BUDGET")
    print("G11_NIGHT_TRANSACTION_CHUNK_BYTES=" + json.dumps([len(compact(c)) for c in chunks]))
    print("G11_NIGHT_TRANSACTION_MANIFEST_BYTES=" + str(len(compact(manifest))))
    return manifest, chunks


def publish_night_transaction(delta: dict, incoming: dict, origin: str, token: str) -> dict:
    manifest, chunks = night_chunks(delta, incoming)
    endpoint = "/api/g11-night-transaction"
    post(origin, token, manifest, endpoint)
    for chunk in chunks:
        post(origin, token, chunk, endpoint)
    result = post(origin, token, {
        "SCHEMA": NIGHT_TRANSACTION_SCHEMA, "ACTION": "COMMIT",
        "OPERATIONAL_DATE": manifest["OPERATIONAL_DATE"],
        "NIGHT_TRANSACTION_ID": manifest["NIGHT_TRANSACTION_ID"],
        "MANIFEST_SHA256": manifest["MANIFEST_SHA256"],
        "SOURCE_NIGHT_SHA": manifest["SOURCE_NIGHT_SHA"],
    }, endpoint)
    print("G11_NIGHT_TRANSACTION_COMMITTED=" + manifest["NIGHT_TRANSACTION_ID"])
    return result


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
    if len(data) > BUDGET and incoming["stage"] != "NIGHT":
        raise RuntimeError("HOT_DELTA_OVER_BUDGET:" + str(len(data)))
    if incoming["stage"] == "PREDEADLINE" and not delta["RACES"]:
        print("G11_HOT_DELTA_NO_CHANGE=PASS")
        Path(".relay-output/hot-delta-accepted").write_text(base_sha + "\n")
        Path(os.environ["GITHUB_OUTPUT"]).open("a").write("feed_sha=" + base_sha + "\n")
        return
    token = os.environ["G11_SYNC_OIDC"]
    if incoming["stage"] == "NIGHT":
        body = publish_night_transaction(delta, incoming, origin, token)
        readback, next_headers, raw = fetch(url)
        if next_headers.get("x-g11-source-feed-sha256") != body.get("payload_sha256"):
            raise RuntimeError("NIGHT_TRANSACTION_READBACK_SHA")
        if (readback.get("stage") != "NIGHT" or readback.get("status") != "PASS" or
                readback["counts"]["results"] != len(incoming["races"]) or
                readback["counts"]["pending"] != 0):
            raise RuntimeError("NIGHT_TRANSACTION_READBACK_COVERAGE")
        rows = {row["key"]: row for row in readback["races"]}
        for change in delta["RACES"]:
            if change["RACE_KEY"] not in rows or any(
                    not includes(rows[change["RACE_KEY"]].get(field), value)
                    for field, value in change["CHANGED_FIELDS"].items()):
                raise RuntimeError("NIGHT_TRANSACTION_READBACK_RACE:" + change["RACE_KEY"])
        for field, value in delta["TOP_LEVEL"].items():
            if not includes(readback.get(field), value):
                raise RuntimeError("NIGHT_TRANSACTION_READBACK_TOP:" + field)
        Path(".relay-output/hot-delta-accepted").write_text(body["payload_sha256"] + "\n")
        Path(".relay-output/hot-delta-readback.json").write_bytes(raw)
        Path(os.environ["GITHUB_OUTPUT"]).open("a").write("feed_sha=" + body["payload_sha256"] + "\n")
        print("G11_NIGHT_TRANSACTION_READBACK=PASS SOURCE_SHA=" + body["payload_sha256"])
        return
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
        if sys.argv[1] == "--night-projection":
            reproject_completed_night(Path(sys.argv[2]), Path(sys.argv[3]),
                                      sys.argv[4], sys.argv[5], sys.argv[6])
        elif sys.argv[1] == "--morning-base":
            project_morning_base(Path(sys.argv[2]))
        else:
            publish(Path(sys.argv[1]), sys.argv[2].rstrip("/"))
    except Exception as error:
        print("G11_HOT_DELTA_FAIL=" + str(error), file=sys.stderr)
        raise SystemExit(1)
