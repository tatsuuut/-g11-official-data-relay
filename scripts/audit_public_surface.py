from __future__ import annotations

from collections import Counter
import json
import pathlib
import re
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
ALLOWED_FILES = {
    ".github/workflows/g11-free-runner.yml",
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
    shadow = race.get("original_display_shadow")
    if not isinstance(shadow, dict):
        return False
    changed = False
    boat_keys = {
        "boat", "course", "tilt", "standard_exhibition_time", "start_timing",
        "display_composite_rank", "morning_rank", "movement",
    }
    boats = shadow.get("boats")
    if isinstance(boats, list):
        compact = [
            {key: value for key, value in boat.items() if key in boat_keys}
            if isinstance(boat, dict) else boat
            for boat in boats
        ]
        changed = changed or compact != boats
        shadow["boats"] = compact
    inputs = shadow.get("last_minute_inputs")
    if isinstance(inputs, dict) and "odds_role" in inputs:
        inputs.pop("odds_role", None)
        changed = True
    profiles = shadow.get("profile_projections")
    if isinstance(profiles, dict):
        compact_profiles = {}
        for name, definition in profiles.items():
            if isinstance(definition, dict):
                compact_profiles[name] = {"practical_bets": definition.get("practical_bets", [])}
            else:
                compact_profiles[name] = definition
        changed = changed or compact_profiles != profiles
        shadow["profile_projections"] = compact_profiles
    return changed


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
    reference_races = reference.get("races")
    incoming_races = feed.get("races")
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
            for key in sorted(common_result_meta_keys):
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
    sys.exit(main())
