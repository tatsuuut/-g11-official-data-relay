from __future__ import annotations

from collections import Counter
import json
import pathlib
import re
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
ALLOWED_FILES = {
    ".github/workflows/g11-free-runner.yml",
    ".github/workflows/g11-morning-odds-footprint-4bet-shadow-v1.yml",
    ".github/workflows/g11-three-phase-runtime-contract-gate.yml",
    ".github/workflows/g11-explicit-night-dispatcher.yml",
    ".github/workflows/g11-predeadline-critical-watchdog.yml",
    ".github/workflows/g11-night-backfill-20260917-20260919.yml",
    ".github/workflows/g11-wild-position-bootstrap.yml",
    ".gitignore",
    "README.md",
    "SECURITY.md",
    "scripts/audit_public_surface.py",
    "scripts/hot_delta_transport_v1.py",
    "scripts/daily_runtime_bootstrap_v1.py",
    "scripts/daily_runtime_delivery_v1.py",
    "scripts/test_daily_runtime_delivery_v1.py",
    "scripts/repair_20260928_final.py",
    "scripts/overlay_20260928_accepted_feed_final.py",
    "scripts/official_20260928_results.txt",
    "scripts/audit_three_phase_runtime_contract.py",
    "scripts/dynamic_last_race_heartbeat.py",
    "scripts/test_dynamic_last_race_heartbeat.py",
    "scripts/predeadline_watchdog_v1.py",
    "scripts/test_predeadline_watchdog_v1.py",
}
IGNORED_PARTS = {".git", "__pycache__"}
FORBIDDEN_SUFFIXES = {
    ".cbm",
    ".gz",
    ".joblib",
    ".json",
    ".onnx",
    ".pickle",
    ".pkl",
    ".tar",
    ".zip",
}
FORBIDDEN_PATTERNS = {
    "github_token_literal": re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "auth_header": re.compile(r"Authorization:\\s*Bearer\\s+(?!\\$|\\{)", re.IGNORECASE),
    "production_logic": re.compile(
        r"(?:P3_(?:COEFFICIENT|WEIGHT)|AI_COEFFICIENT_LEGACY|catboost_model|SHAP_VALUES)",
        re.IGNORECASE,
    ),
}
PATTERN_DEFINITION_FILE = "scripts/audit_public_surface.py"




def _keyset_variants(rows):
    variants = Counter(
        tuple(sorted(row.keys()))
        for row in rows
        if isinstance(row, dict)
    )
    return [
        {"count": count, "keys": list(keys)}
        for keys, count in sorted(variants.items(), key=lambda item: (-item[1], item[0]))
    ]


def _nested_keyset_variants(rows, field):
    variants = Counter()
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = row.get(field)
        if isinstance(value, dict):
            variants[tuple(sorted(value.keys()))] += 1
        elif value is None:
            variants[("<NULL>",)] += 1
        else:
            variants[(f"<TYPE:{type(value).__name__}>",)] += 1
    return [
        {"count": count, "keys": list(keys)}
        for keys, count in sorted(variants.items(), key=lambda item: (-item[1], item[0]))
    ]

def _compact_original_display_shadow(race):
    """Preserve the deployed Site display-shadow schema exactly."""
    return False

def _merge_schema(current, value):
    if isinstance(value, dict):
        node = current if isinstance(current, dict) else {}
        for key, child in value.items():
            node[key] = _merge_schema(node.get(key), child)
        return node
    if isinstance(value, list):
        child_schema = current[0] if isinstance(current, list) and current else None
        for child in value:
            child_schema = _merge_schema(child_schema, child)
        return [child_schema]
    return current if current is not None else True


def _prune_to_schema(value, schema, path, dropped):
    if isinstance(value, dict) and isinstance(schema, dict):
        output = {}
        for key, child in value.items():
            if key not in schema:
                dropped.append(f"{path}.{key}")
                continue
            output[key] = _prune_to_schema(child, schema[key], f"{path}.{key}", dropped)
        return output
    if isinstance(value, list) and isinstance(schema, list) and schema:
        return [
            _prune_to_schema(child, schema[0], f"{path}[]", dropped)
            for child in value
        ]
    return value


def verified_historical_special_pre_upgrade(prior, race, proof):
    """Only preserve a read-model correction backed by the original morning lock."""
    if not isinstance(prior, dict) or not isinstance(race, dict) or not isinstance(proof, dict):
        return False
    key = race.get("key")
    if (
        not isinstance(key, str) or key[:8] not in {"20260925", "20260926"}
        or prior.get("key") != key
        or not isinstance(proof.get("bets"), list) or len(proof["bets"]) != 8
        or len(set(proof["bets"])) != 8
        or prior.get("p3_snapshot_sha256") != proof.get("snapshot_sha")
        or not isinstance(proof.get("snapshot_sha"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", proof["snapshot_sha"])
    ):
        return False
    bets = proof["bets"]
    note = "｜SITE表示互換のみ｜正本特例8点=" + "/".join(bets)
    old_pre = prior.get("predeadline")
    new_pre = race.get("predeadline")
    if not isinstance(old_pre, dict) or not isinstance(new_pre, dict):
        return False
    return (
        prior.get("practical_bets") == bets[:7]
        and prior.get("p3_production_bets") == bets[:7]
        and prior.get("production_points") == 7
        and str(prior.get("caution") or "").endswith(note)
        and race.get("practical_bets") == bets
        and race.get("p3_production_bets") == bets
        and race.get("production_points") == 8
        and old_pre.get("practical_bets") == bets[:7]
        and old_pre.get("production_points") == 7
        and str(old_pre.get("caution") or "").endswith(note)
        and new_pre.get("practical_bets") == bets
        and new_pre.get("production_points") == 8
        and new_pre.get("captured_at_jst") == old_pre.get("captured_at_jst")
        and isinstance(new_pre.get("caution"), str)
        and new_pre["caution"] == old_pre["caution"][:-len(note)]
        and (new_pre.get("special_case_id") is None or
             new_pre.get("special_case_id") == "G11_P3_SPECIAL_45_2_EQ_145_V1")
    )


def project_feed_schema(feed_path: pathlib.Path, reference_path: pathlib.Path) -> int:
    feed = json.loads(feed_path.read_text(encoding="utf-8"))
    reference = json.loads(reference_path.read_text(encoding="utf-8"))
    if feed.get("stage") == "NIGHT":
        stored_path = pathlib.Path(".relay-output/stored-feed.json")
        if stored_path.is_file():
            stored = json.loads(stored_path.read_text(encoding="utf-8"))
            if (
                stored.get("operational_date_jst") == feed.get("operational_date_jst")
                and isinstance(stored.get("races"), list)
            ):
                stored_by_key = {
                    race.get("key"): race
                    for race in stored["races"]
                    if isinstance(race, dict) and isinstance(race.get("key"), str)
                }
                proof_path = pathlib.Path(".relay-output/special-eight-proof.json")
                special_proof = json.loads(proof_path.read_text(encoding="utf-8")) if proof_path.is_file() else {}
                frozen = 0
                prediction_frozen = 0
                for race in feed.get("races", []):
                    if not isinstance(race, dict):
                        continue
                    prior = stored_by_key.get(race.get("key"))
                    if isinstance(prior, dict) and "original_display_shadow" in prior:
                        race["original_display_shadow"] = prior["original_display_shadow"]
                        frozen += 1
                    if isinstance(prior, dict):
                        keep_repaired_pre = verified_historical_special_pre_upgrade(
                            prior, race, special_proof.get(race.get("key")))
                        for field in (
                            "predeadline",
                            "predeadline_exclusion_reason",
                            "odds_merit",
                            "best_ev_bet",
                            "best_ev",
                            "value_bets",
                            "odds_captured_at_jst",
                        ):
                            if field in prior and not (field == "predeadline" and keep_repaired_pre):
                                race[field] = prior[field]
                        prediction_frozen += 1

                    shadow = race.get("original_display_shadow")
                    if isinstance(shadow, dict):
                        boats = shadow.get("boats")
                        if isinstance(boats, list):
                            for boat in boats:
                                if not isinstance(boat, dict):
                                    continue
                                if isinstance(boat.get("metric_ranks"), dict):
                                    boat["metric_ranks"] = {}
                                if isinstance(boat.get("metric_values"), dict):
                                    boat["metric_values"] = {}
                        profiles = shadow.get("profile_projections")
                        if isinstance(profiles, dict):
                            for value in profiles.values():
                                if isinstance(value, dict) and isinstance(value.get("verification10"), list):
                                    value["verification10"] = []
                if "original_display_research" in stored:
                    feed["original_display_research"] = stored["original_display_research"]
                stored_counts = stored.get("counts")
                incoming_counts = feed.get("counts")
                if isinstance(stored_counts, dict) and isinstance(incoming_counts, dict):
                    for field in ("odds_enriched", "odds_waiting", "win_candidates"):
                        if field in stored_counts:
                            incoming_counts[field] = stored_counts[field]
                feed_path.write_text(
                    json.dumps(
                        feed,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ) + "\n",
                    encoding="utf-8",
                )
                print(
                    "G11_SITE_NIGHT_DISPLAY_FROZEN_TO_STORED="
                    + json.dumps(
                        {
                            "races": frozen,
                            "prediction_races_frozen": prediction_frozen,
                            "canonical_night_evidence_mutated": False,
                            "transport_only": True,
                        },
                        sort_keys=True,
                    )
                )
    reference_races = reference.get("races")
    incoming_races = feed.get("races")
    if feed.get("stage") != "MORNING":
        special_eight_site_compat(feed_path)
        feed = json.loads(feed_path.read_text(encoding="utf-8"))
        incoming_races = feed.get("races")
    if feed.get("stage") == "MORNING":
        anomalies = []
        for race in incoming_races if isinstance(incoming_races, list) else []:
            if not isinstance(race, dict):
                continue
            points = race.get("production_points")
            p3_bets = race.get("p3_production_bets")
            practical = race.get("practical_bets")
            if points not in (3, 5, 7, 8) or (
                isinstance(practical, list) and isinstance(points, int)
                and len(practical) != points
            ):
                anomalies.append({
                    "key": race.get("key"),
                    "venue": race.get("venue"),
                    "race": race.get("race"),
                    "production_points": points,
                    "practical_bets": practical,
                    "p3_production_bets": p3_bets,
                    "p3_status": race.get("p3_status"),
                    "p3_snapshot": race.get("p3_snapshot"),
                })
        print("G11_MORNING_POINT_ANOMALIES=" + json.dumps(anomalies, ensure_ascii=False, sort_keys=True))
        special_eight_site_compat(feed_path)
        print("G11_SITE_SCHEMA_PROJECT=SKIP_MORNING")
        return 0
    if not isinstance(reference_races, list) or not reference_races:
        fail("schema reference has no races")
    if not isinstance(incoming_races, list):
        fail("incoming feed has no races")

    # The deployed Site already supports the legacy non-formal late_reference
    # object. A same-day rescue NIGHT may use that exact race-local transport
    # shape to show hit/miss and hypothetical 100-yen-per-bet money without
    # becoming a formal research sample. Previous-day schema projection must
    # not strip this optional field merely because yesterday was a normal day.
    rescue_reference_by_key = {}
    if (
        feed.get("stage") == "NIGHT"
        and isinstance(feed.get("counts"), dict)
        and feed["counts"].get("formal") == 0
        and isinstance(feed.get("research_db"), dict)
        and feed["research_db"].get("available") is False
    ):
        required_ref = {
            "schema", "status", "captured_at_jst", "morning_lock_established",
            "stored_in_existing_research_ledger", "included_in_formal_metrics",
            "included_in_coefficient_learning", "included_in_promotion_gate",
            "notification_eligible", "purchase_executed",
            "official_result_captured_at_jst", "validation_bets",
            "reference_practical_hit", "reference_validation_hit",
            "reference_finance",
        }
        required_finance = {
            "status", "race_refund_occurred", "gross_stake", "refund_amount",
            "net_investment", "prize_return", "total_return", "profit",
        }
        for race in incoming_races:
            if not isinstance(race, dict):
                continue
            ref = race.get("late_reference")
            if not isinstance(ref, dict):
                continue
            finance = ref.get("reference_finance")
            valid = (
                set(ref) == required_ref
                and ref.get("schema") == "G11_LATE_REFERENCE_FEED_V1"
                and ref.get("status") == "RECORDED_NON_FORMAL_SETTLED"
                and ref.get("morning_lock_established") is False
                and ref.get("included_in_formal_metrics") is False
                and ref.get("included_in_coefficient_learning") is False
                and ref.get("included_in_promotion_gate") is False
                and ref.get("notification_eligible") is False
                and ref.get("purchase_executed") is False
                and isinstance(ref.get("validation_bets"), list)
                and isinstance(ref.get("reference_practical_hit"), bool)
                and isinstance(ref.get("reference_validation_hit"), bool)
                and isinstance(finance, dict)
                and set(finance) == required_finance
                and finance.get("status") == "VERIFIED_HYPOTHETICAL_ONLY"
                and isinstance(finance.get("race_refund_occurred"), bool)
                and all(isinstance(finance.get(key), int) for key in (
                    "gross_stake", "refund_amount", "net_investment",
                    "prize_return", "total_return", "profit",
                ))
                and finance["gross_stake"] - finance["refund_amount"]
                    == finance["net_investment"]
                and finance["prize_return"] + finance["refund_amount"]
                    == finance["total_return"]
                and finance["total_return"] - finance["gross_stake"]
                    == finance["profit"]
                and race.get("formal_status") == "EXCLUDED"
                and race.get("research_eligible") is False
            )
            if not valid:
                fail("invalid same-day rescue late_reference transport")
            rescue_reference_by_key[race.get("key")] = ref

    schema = None
    for race in reference_races:
        if isinstance(race, dict):
            schema = _merge_schema(schema, race)
    if not isinstance(schema, dict):
        fail("schema reference invalid")
    def collect_profile(races):
        type_map = {}
        keyset_map = {}

        def kind(value):
            if value is None:
                return "null"
            if isinstance(value, bool):
                return "bool"
            if isinstance(value, (int, float)):
                return "number"
            if isinstance(value, str):
                return "string"
            if isinstance(value, dict):
                return "object"
            if isinstance(value, list):
                return "array"
            return type(value).__name__

        def walk(value, path):
            type_map.setdefault(path, set()).add(kind(value))
            if isinstance(value, dict):
                keyset_map.setdefault(path, set()).add(tuple(sorted(value)))
                for key, child in value.items():
                    walk(child, f"{path}.{key}")
            elif isinstance(value, list):
                for child in value:
                    walk(child, f"{path}[]")

        for race in races:
            if isinstance(race, dict):
                walk(race, "race")
        return type_map, keyset_map

    accepted_types, accepted_keysets = collect_profile(reference_races)
    incoming_types, incoming_keysets = collect_profile(incoming_races)
    type_diffs = {
        path: {
            "accepted": sorted(accepted_types.get(path, set())),
            "incoming": sorted(types),
        }
        for path, types in incoming_types.items()
        if not types.issubset(accepted_types.get(path, set()))
    }
    keyset_diffs = {
        path: {
            "accepted": [list(keys) for keys in sorted(accepted_keysets.get(path, set()))],
            "incoming": [list(keys) for keys in sorted(keysets)],
        }
        for path, keysets in incoming_keysets.items()
        if not keysets.issubset(accepted_keysets.get(path, set()))
    }
    print("G11_SITE_SCHEMA_TYPE_DIFF=" + json.dumps(type_diffs, ensure_ascii=False, sort_keys=True))
    print("G11_SITE_SCHEMA_KEYSET_DIFF=" + json.dumps(keyset_diffs, ensure_ascii=False, sort_keys=True))

    display_compacted = sum(
        _compact_original_display_shadow(race)
        for race in incoming_races
        if isinstance(race, dict)
    )
    print(f"G11_SITE_DISPLAY_COMPAT=COMPACT_V1:{display_compacted}")

    reference_abeken_live = [
        race.get("abeken_shadow", {}).get("live")
        for race in reference_races
        if isinstance(race, dict)
        and isinstance(race.get("abeken_shadow"), dict)
    ]
    abeken_live_suppressed = 0
    if reference_abeken_live and all(value is None for value in reference_abeken_live):
        for race in incoming_races:
            if not isinstance(race, dict):
                continue
            shadow = race.get("abeken_shadow")
            if isinstance(shadow, dict) and shadow.get("live") is not None:
                shadow["live"] = None
                abeken_live_suppressed += 1
    print(f"G11_SITE_ABEKEN_LIVE_COMPAT=NULL_V1:{abeken_live_suppressed}")

    p3_fields = {
        "p3_head", "p3_logic_id", "p3_production_bets", "p3_published_at_jst",
        "p3_snapshot", "p3_snapshot_sha256", "p3_status",
    }
    missing_p3 = []
    for race in incoming_races:
        if not isinstance(race, dict):
            continue
        missing = sorted(p3_fields - set(race))
        if missing:
            missing_p3.append({
                "key": race.get("key"),
                "venue": race.get("venue"),
                "race": race.get("race"),
                "formal_status": race.get("formal_status"),
                "exclusion_reason": race.get("exclusion_reason"),
                "top_boat": race.get("top_boat"),
                "practical_bets": race.get("practical_bets"),
                "quality": race.get("quality"),
                "missing": missing,
            })
    print("G11_SITE_MISSING_P3_RACES=" + json.dumps(missing_p3, ensure_ascii=False, sort_keys=True))

    filled = []
    reference_dicts = [race for race in reference_races if isinstance(race, dict)]
    common_race_keys = set(reference_dicts[0])
    for race in reference_dicts[1:]:
        common_race_keys &= set(race)
    reference_result_meta = [
        race.get("result_meta") for race in reference_dicts
        if isinstance(race.get("result_meta"), dict)
    ]
    common_result_meta_keys = set(reference_result_meta[0]) if reference_result_meta else set()
    for meta in reference_result_meta[1:]:
        common_result_meta_keys &= set(meta)
    final_replacement = (
        feed.get("stage") == "NIGHT"
        and feed.get("operational_date_jst") in {"2026-09-25", "2026-09-26"}
        and isinstance(feed.get("final_accounting"), dict)
        and isinstance(feed.get("capabilities"), dict)
        and feed["capabilities"].get("final_accounting") == "G11_THREE_ENGINE_FINAL_BET_ACCOUNTING_V1"
        and "three_engine_comparison" not in feed
    )
    for race in incoming_races:
        if not isinstance(race, dict):
            continue
        for key in sorted(common_race_keys):
            if key in {"three_engine_score", "three_engine_classification"} and final_replacement:
                continue
            if key not in race:
                race[key] = None
                filled.append(f"race.{key}")
        meta = race.get("result_meta")
        if isinstance(meta, dict):
            # Result-finality fields are semantic state, not shape padding.
            # The Site accepts them only when an official result has actually
            # established the race.  Synthesizing null result_status/dead_heat/
            # trifecta_settlements from yesterday's settled schema makes a
            # live PREDEADLINE feed fail FEED_RESULT_META_STATUS.
            finality_keys = {
                "result_status", "dead_heat", "trifecta_settlements",
            }
            for key in sorted(common_result_meta_keys - finality_keys):
                if key not in meta:
                    meta[key] = None
                    filled.append(f"race.result_meta.{key}")

    # Preserve exact per-race proof for this one operational rescue day.
    # Yesterday's schema lacks the field; pruning it would remove the only
    # evidence that a LIVE ticket was locked before its own deadline.
    rescue_proof_by_key = {}
    if feed.get("SNAPSHOT_CLASS") == "TODAY_ONLY_RESCUE_PREDEADLINE":
        if feed.get("operational_date_jst") != "2026-09-27" or feed.get("stage") != "PREDEADLINE":
            fail("today-only rescue scope")
        for race in incoming_races:
            key = race.get("key") if isinstance(race, dict) else None
            proof = race.get("today_rescue") if isinstance(race, dict) else None
            if not isinstance(key, str) or key in rescue_proof_by_key or not isinstance(proof, dict):
                fail("missing or duplicate race-local rescue proof")
            rescue_proof_by_key[key] = proof

    dropped = []
    feed["races"] = [
        _prune_to_schema(race, schema, "race", dropped)
        if isinstance(race, dict) else race
        for race in incoming_races
    ]

    # A previous day's schema cannot remove official settlement evidence from
    # a completed NIGHT. FINAL accounting is validated against these exact
    # per-race settlements by the Site, including refunds and dead heats.
    if (feed.get("stage") == "NIGHT"
            and isinstance(feed.get("final_accounting"), dict)
            and feed.get("capabilities", {}).get("final_accounting")
                == "G11_THREE_ENGINE_FINAL_BET_ACCOUNTING_V1"):
        for source, projected in zip(incoming_races, feed["races"]):
            original = source.get("result_meta") if isinstance(source, dict) else None
            target = projected.get("result_meta") if isinstance(projected, dict) else None
            if (not isinstance(original, dict) or not isinstance(target, dict)
                    or not all(field in original for field in (
                        "result_status", "dead_heat", "trifecta_settlements"))):
                fail("FINAL official settlement metadata missing")
            for field in ("result_status", "dead_heat", "trifecta_settlements"):
                target[field] = original[field]
        dropped[:] = [name for name in dropped
                      if name not in {
                          "race.result_meta.result_status",
                          "race.result_meta.dead_heat",
                          "race.result_meta.trifecta_settlements",
                      }]

    # A previous day's feed is a display reference, never the authority for
    # removing today's formal prediction and three-engine read-model fields.
    # Copy only fields present in the verified incoming feed; missing snapshots
    # remain missing, without manufacturing an evaluated or missed prediction.
    current_race_fields = (
        "three_engine_score", "three_engine_classification",
        "trifecta_confidence", "special_case_id", "special_strength",
        "strong_mark", "wild_pack", "abeken_shadow", "p3_verification",
    )
    current_live_fields = (
        "trifecta_confidence", "special_case_id",
        "special_strength", "strong_mark",
    )
    preserved = set()
    for source, projected in zip(incoming_races, feed["races"]):
        if not isinstance(source, dict) or not isinstance(projected, dict):
            continue
        for field in current_race_fields:
            if field in source:
                if field == "p3_verification" and not (
                    feed.get("operational_date_jst", "") >= "2026-10-01"
                    and isinstance(source[field], dict)
                    and source[field].get("SCHEMA")
                        == "G11_MASHIRO_P3_INTEGRATED_VERIFICATION_V1"
                    and source[field].get("RACE_ID") == source.get("key")
                ):
                    continue
                projected[field] = source[field]
                preserved.add("race." + field)
        source_pre = source.get("predeadline")
        projected_pre = projected.get("predeadline")
        if isinstance(source_pre, dict):
            if not isinstance(projected_pre, dict):
                fail("formal PRE_RACE snapshot removed by schema projection")
            for field in current_live_fields:
                if field in source_pre:
                    projected_pre[field] = source_pre[field]
                    preserved.add("race.predeadline." + field)
        if (
            isinstance(feed.get("capabilities"), dict)
            and feed["capabilities"].get("three_engine_comparison")
                == "G11_THREE_ENGINE_COMPARISON_V1"
            and (
                not isinstance(projected.get("three_engine_score"), dict)
                or not isinstance(projected.get("three_engine_classification"), str)
            )
        ):
            fail("three-engine capability without race-level contract")
    dropped[:] = [
        path for path in dropped
        if not any(
            path == field or path.startswith(field + ".")
            or path.startswith(field + "[")
            for field in preserved
        )
    ]
    print("G11_SITE_CURRENT_FIELDS_PRESERVED=" + json.dumps(
        sorted(preserved), ensure_ascii=False))
    if rescue_proof_by_key:
        for race in feed["races"]:
            if not isinstance(race, dict) or race.get("key") not in rescue_proof_by_key:
                fail("rescue proof race binding")
            race["today_rescue"] = rescue_proof_by_key[race["key"]]
            # Yesterday's optional engine fields can be shape-padded as null;
            # absence means NOT_READY on this day, never a fabricated snapshot.
            if "abeken_status" not in feed.get("capabilities", {}):
                race.pop("abeken_shadow", None)
            elif race.get("abeken_shadow") is None:
                race.pop("abeken_shadow", None)
            if race.get("wild_pack") is None:
                race.pop("wild_pack", None)
        dropped[:] = [item for item in dropped if item != "race.today_rescue" and not item.startswith("race.today_rescue.")]
        print("G11_SITE_TODAY_RESCUE_PROOF_PRESERVED=" + str(len(rescue_proof_by_key)))
    if rescue_reference_by_key:
        for race in feed["races"]:
            if not isinstance(race, dict):
                continue
            ref = rescue_reference_by_key.get(race.get("key"))
            if ref is not None:
                race["late_reference"] = ref
        dropped[:] = [
            item for item in dropped
            if item != "race.late_reference"
            and not item.startswith("race.late_reference.")
        ]
        print(
            "G11_SITE_RESCUE_LATE_REFERENCE_PRESERVED="
            + str(len(rescue_reference_by_key))
        )

        finance_mismatches = []
        for race in feed["races"]:
            if not isinstance(race, dict):
                continue
            ref = race.get("late_reference")
            if not isinstance(ref, dict):
                continue
            finance = ref.get("reference_finance")
            bets = race.get("practical_bets")
            meta = race.get("result_meta")
            settlements = (
                meta.get("trifecta_settlements")
                if isinstance(meta, dict) else None
            )
            if (
                not isinstance(finance, dict)
                or not isinstance(bets, list)
                or not isinstance(settlements, list)
            ):
                finance_mismatches.append({
                    "key": race.get("key"),
                    "reason": "shape",
                })
                continue
            payout_by_bet = {
                item.get("trifecta"): item.get("payout")
                for item in settlements
                if isinstance(item, dict)
                and isinstance(item.get("trifecta"), str)
                and isinstance(item.get("payout"), int)
            }
            expected_gross = len(bets) * 100
            expected_prize = sum(
                payout_by_bet.get(bet, 0)
                for bet in bets
                if isinstance(bet, str)
            )
            refund = finance.get("refund_amount")
            checks = {
                "gross": finance.get("gross_stake") == expected_gross,
                "prize": finance.get("prize_return") == expected_prize,
                "net": (
                    isinstance(refund, int)
                    and finance.get("net_investment")
                    == expected_gross - refund
                ),
                "total": (
                    isinstance(refund, int)
                    and finance.get("total_return")
                    == expected_prize + refund
                ),
                "profit": (
                    isinstance(refund, int)
                    and finance.get("profit")
                    == expected_prize + refund - expected_gross
                ),
                "hit": ref.get("reference_practical_hit")
                    == (expected_prize > 0),
            }
            if not all(checks.values()):
                finance_mismatches.append({
                    "key": race.get("key"),
                    "bets": bets,
                    "checks": checks,
                    "expected_gross": expected_gross,
                    "actual_gross": finance.get("gross_stake"),
                    "expected_prize": expected_prize,
                    "actual_prize": finance.get("prize_return"),
                    "refund": refund,
                    "net": finance.get("net_investment"),
                    "total": finance.get("total_return"),
                    "profit": finance.get("profit"),
                    "reference_hit": ref.get("reference_practical_hit"),
                })
        print(
            "G11_RESCUE_REFERENCE_FINANCE_MISMATCHES="
            + json.dumps(finance_mismatches[:30], ensure_ascii=False, sort_keys=True)
        )
    feed_path.write_text(
        json.dumps(feed, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print("G11_SITE_REFERENCE_RACE_KEYSETS=" + json.dumps(_keyset_variants(reference_races), ensure_ascii=False, sort_keys=True))
    print("G11_SITE_INCOMING_RACE_KEYSETS=" + json.dumps(_keyset_variants(incoming_races), ensure_ascii=False, sort_keys=True))
    for field in ("original_display_shadow", "wild_pack", "abeken_shadow", "p3_snapshot", "result_meta", "research_finance", "actual_purchase", "predeadline", "three_engine_score", "late_reference"):
        print("G11_SITE_REFERENCE_NESTED_" + field.upper() + "=" + json.dumps(_nested_keyset_variants(reference_races, field), ensure_ascii=False, sort_keys=True))
        print("G11_SITE_INCOMING_NESTED_" + field.upper() + "=" + json.dumps(_nested_keyset_variants(incoming_races, field), ensure_ascii=False, sort_keys=True))
    print("G11_SITE_SCHEMA_PROJECT=PASS")
    print("G11_SITE_SCHEMA_DROPPED=" + json.dumps(sorted(set(dropped)), ensure_ascii=False))
    print("G11_SITE_SCHEMA_FILLED=" + json.dumps(sorted(set(filled)), ensure_ascii=False))
    return 0


def special_eight_site_compat(feed_path: pathlib.Path) -> int:
    """Reject truncated special tickets before public serialization."""
    feed = json.loads(feed_path.read_text(encoding="utf-8"))
    races = feed.get("races")
    if not isinstance(races, list):
        fail("special-eight feed has no races")
    verified = []
    canonical = ["4-2-1", "4-2-5", "4-1-2", "4-5-2", "5-2-1", "5-2-4", "5-1-2", "5-4-2"]

    def verify_projection(value, *, key, scope, p3_bets=None, historical_root_proven=False):
        if not isinstance(value, dict):
            return
        bets = value.get("practical_bets")
        case_id = value.get("special_case_id")
        historical_date = isinstance(key, str) and key[:8] in {"20260925", "20260926"}
        historical_unmarked = (
            historical_date and case_id is None and
            ((scope == "MORNING_ROOT" and
              "特例45-2=145" in str(value.get("caution") or "") and
              "8点固定" in str(value.get("caution") or ""))
             or (scope == "PREDEADLINE" and historical_root_proven))
        )
        special = case_id == "G11_P3_SPECIAL_45_2_EQ_145_V1" or historical_unmarked
        legacy = "正本特例8点=" in str(value.get("caution") or "")
        if not special and not legacy and value.get("production_points") != 8:
            return
        if (not special or value.get("production_points") != 8
                or bets != canonical or len(set(bets)) != 8
                or (p3_bets is not None and p3_bets != canonical)):
            if isinstance(key, str) and key[:8] in {"20260925", "20260926"}:
                fail("special-eight public artifact mismatch: "
                     f"{key}:{scope}:root_proven={bool(historical_root_proven)},"
                     f"case_id={repr(case_id)},points={repr(value.get('production_points'))},"
                     f"count={len(bets) if isinstance(bets, list) else 'invalid'},"
                     f"ordered_eight={bets == canonical},"
                     f"note8={'8点固定' in str(value.get('caution') or '')}")
            fail(f"special-eight public artifact mismatch: {key}:{scope}")
        verified.append({"key": key, "scope": scope, "points": 8})
        return True

    for race in races:
        if not isinstance(race, dict):
            continue
        key = race.get("key")
        p3_bets = race.get("p3_production_bets")
        root_proven = verify_projection(race, key=key, scope="MORNING_ROOT", p3_bets=p3_bets)
        pre = race.get("predeadline")
        historical_root_proven = root_proven and race.get("special_case_id") is None
        verify_projection(pre, key=key, scope="PREDEADLINE", historical_root_proven=historical_root_proven)

    print("G11_SITE_SPECIAL8_CANONICAL=" + json.dumps(verified, ensure_ascii=False, sort_keys=True))
    return 0

def project_rescue_stored_schema(feed_path: pathlib.Path, stored_path: pathlib.Path) -> int:
    """Carry timely rescue LIVE locks in the accepted same-day Site shape."""
    import copy
    from datetime import datetime
    import hashlib
    import json
    from pathlib import Path

    path = feed_path
    incoming = json.loads(path.read_text(encoding="utf-8"))
    stored = json.loads(stored_path.read_text(encoding="utf-8"))
    day = "2026-09-30"
    if (incoming.get("operational_date_jst") != day or stored.get("operational_date_jst") != day
            or incoming.get("stage") != "PREDEADLINE" or stored.get("stage") != "PREDEADLINE"
            or incoming.get("SNAPSHOT_CLASS") != "TODAY_ONLY_RESCUE_PREDEADLINE"
            or incoming.get("CANONICAL_MORNING_PROMOTED") is not False
            or incoming.get("RESEARCH_ELIGIBLE_AS_MORNING") is not False):
        raise SystemExit("RESCUE_STORED_SCHEMA_BOUNDARY")
    current = {row["key"]: row for row in incoming["races"]}
    prior = {row["key"]: row for row in stored["races"]}
    if len(current) != 144 or len(prior) != 144 or set(current) != set(prior):
        raise SystemExit("RESCUE_STORED_SCHEMA_RACE_SET")
    out = copy.deepcopy(stored)
    out["source"] = copy.deepcopy(incoming["source"])
    out["generated_at_jst"] = incoming["generated_at_jst"]
    accepted_pre = next((row["predeadline"] for row in stored["races"]
                         if isinstance(row.get("predeadline"), dict)), None)
    if accepted_pre is None:
        raise SystemExit("RESCUE_STORED_SCHEMA_NO_ACCEPTED_LIVE")
    added = []
    locked = []
    expired_unpublished = []
    for row in out["races"]:
        new = current[row["key"]]
        proof = new.get("today_rescue")
        if not isinstance(proof, dict) or proof.get("snapshot_class") != "TODAY_ONLY_RESCUE_PREDEADLINE":
            raise SystemExit("RESCUE_TRANSPORT_PROOF_MISSING:" + row["key"])
        for field in ("p3_snapshot_sha256", "practical_bets", "p3_production_bets", "production_points"):
            if row.get(field) != new.get(field):
                raise SystemExit("RESCUE_TRANSPORT_P3_LOCK_CHANGED:" + row["key"])
        pre = new.get("predeadline")
        previous = row.get("predeadline")
        if isinstance(previous, dict):
            if (not isinstance(pre, dict) or
                any(previous.get(field) != pre.get(field) for field in
                    ("snapshot_sha256", "captured_at_jst", "practical_bets", "production_points"))):
                raise SystemExit("RESCUE_TRANSPORT_LIVE_LOCK_CHANGED:" + row["key"])
            continue
        if not isinstance(pre, dict):
            continue
        deadline = datetime.fromisoformat(row["deadline_jst"])
        # A valid earlier capture is still not permission to publish a missed
        # PRE_RACE after its deadline. Keep the accepted Site row untouched.
        if deadline <= datetime.now(deadline.tzinfo):
            expired_unpublished.append(row["key"])
            continue
        lock = proof.get("locked_at_jst")
        if (proof.get("status") != "LOCKED" or pre.get("status") != "READY"
                or proof.get("p3_snapshot_sha256") != pre.get("snapshot_sha256")
                or not isinstance(lock, str) or datetime.fromisoformat(lock) >= deadline
                or datetime.fromisoformat(pre["captured_at_jst"]) >= deadline
                or datetime.fromisoformat(pre["odds_captured_at_jst"]) >= deadline
                or row.get("result_trifecta") is not None or row.get("research_eligible") is not False
                or set(pre) != set(accepted_pre)
                or set(pre["trifecta_confidence"]) != set(accepted_pre["trifecta_confidence"])):
            raise SystemExit("RESCUE_TRANSPORT_LIVE_PROOF_INVALID:" + row["key"])
        row["predeadline"] = copy.deepcopy(pre)
        for field in ("best_ev", "best_ev_bet", "value_bets", "odds_captured_at_jst"):
            row[field] = copy.deepcopy(pre[field])
        row["odds_merit"] = pre["legacy_odds_merit"]
        added.append(row["key"])
        locked.append({"key": row["key"], "snapshot_sha256": pre["snapshot_sha256"], "locked_at_jst": lock})
    if (set(out) != set(stored) or
        any(set(row) != set(prior[row["key"]]) for row in out["races"])):
        raise SystemExit("RESCUE_STORED_SCHEMA_KEYSET_CHANGED")
    payload = json.dumps(out, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    path.write_text(payload, encoding="utf-8")
    print("G11_RESCUE_STORED_SCHEMA_TRANSPORT=" + json.dumps({
        "day": day, "race_count": 144, "new_live_locks": added,
        "expired_unpublished": expired_unpublished,
        "locked_proof": locked, "today_rescue_published": 0,
        "canonical_morning_promoted": False, "result_leakage": 0,
        "raw_bytes": len(payload.encode("utf-8")),
        "evidence_sha256": hashlib.sha256(json.dumps(
            [current[key]["today_rescue"] for key in sorted(current)],
            ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest(),
    }, ensure_ascii=False, sort_keys=True))
    return 0

def fail(message: str) -> None:
    raise SystemExit(f"PUBLIC_SURFACE_AUDIT_FAIL: {message}")


def audit_predeadline_critical_order() -> None:
    """Keep optional dependencies behind the official cutoff lane."""
    workflow = (ROOT / ".github/workflows/g11-free-runner.yml").read_text(encoding="utf-8")
    names = re.findall(r"^      - name: (.+)$", workflow, re.MULTILINE)
    required = [
        "Checkout private G11 runtime",
        "Resolve immutable private source identity",
        "Capture and lock dependency-free PREDEADLINE critical lane",
        "Encrypt critical state before optional work",
        "Save early encrypted critical checkpoint",
        "Keep the predeadline relay alive between delayed cron starts",
        "Checkout hash-locked research runtime",
        "Install pinned private runtime dependencies",
    ]
    if any(names.count(name) != 1 for name in required):
        fail("critical step set changed")
    indices = [names.index(name) for name in required]
    if indices != sorted(indices) or "cancel-in-progress: false" not in workflow:
        fail("critical checkpoint must precede optional work without cancellation")
    critical = workflow.split("      - name: Capture and lock dependency-free PREDEADLINE critical lane", 1)[1].split("      - name:", 1)[0]
    if "python3 -S -m g11.relay.predeadline_critical_v1" not in critical:
        fail("critical stdlib import contract missing")
    before = workflow.split("      - name: Capture and lock dependency-free PREDEADLINE critical lane", 1)[0]
    if "pip install" in before or ".g11-model" in before or "catboost" in before:
        fail("optional dependency before official capture")
    chain = workflow.split("      - name: Keep the predeadline relay alive between delayed cron starts", 1)[1].split("      - name:", 1)[0]
    if "steps.runtime.outcome" in chain or "steps.critical-save.outcome == 'success'" not in chain:
        fail("optional runtime controls critical self-chain")


def main() -> int:
    discovered: set[str] = set()
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if any(part in IGNORED_PARTS for part in relative.parts):
            continue
        if path.is_symlink():
            fail(f"symlink forbidden: {relative}")
        if not path.is_file():
            continue
        name = relative.as_posix()
        discovered.add(name)
        if name not in ALLOWED_FILES:
            fail(f"unexpected public file: {name}")
        if any(name.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
            fail(f"forbidden artifact type: {name}")
        # Existing OIDC workflow plus bounded authority/transport routing.
        # No prediction code or model is admitted by this larger YAML budget.
        cap = 120_000 if name == ".github/workflows/g11-free-runner.yml" else 100_000
        if path.stat().st_size > cap:
            fail(f"oversized public file: {name}")
        text = path.read_text(encoding="utf-8")
        if name != PATTERN_DEFINITION_FILE:
            for label, pattern in FORBIDDEN_PATTERNS.items():
                if pattern.search(text):
                    fail(f"{label}: {name}")

    missing = sorted(ALLOWED_FILES - discovered)
    if missing:
        fail(f"missing allowlisted files: {', '.join(missing)}")
    audit_predeadline_critical_order()
    print("PUBLIC_SURFACE_AUDIT_PASS")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "project-rescue-stored-schema":
        sys.exit(project_rescue_stored_schema(pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])))
    if len(sys.argv) == 4 and sys.argv[1] == "project-feed-schema":
        sys.exit(project_feed_schema(pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])))
    if len(sys.argv) == 3 and sys.argv[1] == "special-eight-site-compat":
        sys.exit(special_eight_site_compat(pathlib.Path(sys.argv[2])))
    sys.exit(main())
