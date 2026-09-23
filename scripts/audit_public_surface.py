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
    ".github/workflows/g11-explicit-night-dispatcher.yml",
    ".github/workflows/g11-night-backfill-20260917-20260919.yml",
    ".github/workflows/g11-wild-position-bootstrap.yml",
    ".gitignore",
    "README.md",
    "SECURITY.md",
    "scripts/audit_public_surface.py",
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
                        for field in (
                            "predeadline",
                            "predeadline_exclusion_reason",
                            "odds_merit",
                            "best_ev_bet",
                            "best_ev",
                            "value_bets",
                            "odds_captured_at_jst",
                        ):
                            if field in prior:
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
            if points not in (3, 5, 7) or (
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
    for race in incoming_races:
        if not isinstance(race, dict):
            continue
        for key in sorted(common_race_keys):
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

    dropped = []
    feed["races"] = [
        _prune_to_schema(race, schema, "race", dropped)
        if isinstance(race, dict) else race
        for race in incoming_races
    ]
    feed_path.write_text(
        json.dumps(feed, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print("G11_SITE_REFERENCE_RACE_KEYSETS=" + json.dumps(_keyset_variants(reference_races), ensure_ascii=False, sort_keys=True))
    print("G11_SITE_INCOMING_RACE_KEYSETS=" + json.dumps(_keyset_variants(incoming_races), ensure_ascii=False, sort_keys=True))
    for field in ("original_display_shadow", "wild_pack", "abeken_shadow", "p3_snapshot", "result_meta", "research_finance", "actual_purchase", "predeadline", "three_engine_score"):
        print("G11_SITE_REFERENCE_NESTED_" + field.upper() + "=" + json.dumps(_nested_keyset_variants(reference_races, field), ensure_ascii=False, sort_keys=True))
        print("G11_SITE_INCOMING_NESTED_" + field.upper() + "=" + json.dumps(_nested_keyset_variants(incoming_races, field), ensure_ascii=False, sort_keys=True))
    print("G11_SITE_SCHEMA_PROJECT=PASS")
    print("G11_SITE_SCHEMA_DROPPED=" + json.dumps(sorted(set(dropped)), ensure_ascii=False))
    print("G11_SITE_SCHEMA_FILLED=" + json.dumps(sorted(set(filled)), ensure_ascii=False))
    return 0


def special_eight_site_compat(feed_path: pathlib.Path) -> int:
    """Transport-only adapter for the deployed Site's legacy 3/5/7 display contract."""
    feed = json.loads(feed_path.read_text(encoding="utf-8"))
    races = feed.get("races")
    if not isinstance(races, list):
        fail("special-eight feed has no races")
    changed = []

    def compact_projection(value, *, key, scope, p3_bets=None):
        if not isinstance(value, dict):
            return
        bets = value.get("practical_bets")
        if value.get("production_points") != 8 or not isinstance(bets, list) or len(bets) != 8:
            return
        if p3_bets is not None and (not isinstance(p3_bets, list) or len(p3_bets) != 8):
            fail(f"special-eight p3 binding invalid: {key}")
        canonical = list(bets)
        value["production_points"] = 7
        value["practical_bets"] = canonical[:7]
        if p3_bets is not None:
            p3_bets[:] = p3_bets[:7]
        note = "SITE表示互換のみ｜正本特例8点=" + "/".join(canonical)
        caution = value.get("caution")
        value["caution"] = note if not caution else str(caution) + "｜" + note
        changed.append({
            "key": key,
            "scope": scope,
            "canonical_8": canonical,
            "display_7": canonical[:7],
            "display_extra": canonical[7],
        })

    for race in races:
        if not isinstance(race, dict):
            continue
        key = race.get("key")
        p3_bets = race.get("p3_production_bets")
        compact_projection(race, key=key, scope="MORNING_ROOT", p3_bets=p3_bets)
        pre = race.get("predeadline")
        compact_projection(pre, key=key, scope="PREDEADLINE")

    feed_path.write_text(
        json.dumps(feed, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print("G11_SITE_SPECIAL8_COMPAT=" + json.dumps(changed, ensure_ascii=False, sort_keys=True))
    return 0

def fail(message: str) -> None:
    raise SystemExit(f"PUBLIC_SURFACE_AUDIT_FAIL: {message}")


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
        if path.stat().st_size > 100_000:
            fail(f"oversized public file: {name}")
        text = path.read_text(encoding="utf-8")
        if name != PATTERN_DEFINITION_FILE:
            for label, pattern in FORBIDDEN_PATTERNS.items():
                if pattern.search(text):
                    fail(f"{label}: {name}")

    missing = sorted(ALLOWED_FILES - discovered)
    if missing:
        fail(f"missing allowlisted files: {', '.join(missing)}")
    print("PUBLIC_SURFACE_AUDIT_PASS")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "project-feed-schema":
        sys.exit(project_feed_schema(pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])))
    if len(sys.argv) == 3 and sys.argv[1] == "special-eight-site-compat":
        sys.exit(special_eight_site_compat(pathlib.Path(sys.argv[2])))
    sys.exit(main())
