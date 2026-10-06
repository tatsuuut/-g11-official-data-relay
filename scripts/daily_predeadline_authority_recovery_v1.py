"""2026-10-04 compatibility gate for the immutable, already-frozen runtime.

The frozen source and signed bindings are never edited.  The frozen inventory
mistakenly included two rolling WILD research files as prediction LOCKs.  This
adapter recognizes only those two historical paths as research, and validates
every real LOCK against its prior SHA and the morning deadline plan.
"""
from __future__ import annotations

from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re
import sys

DAY = "2026-10-04"
RESEARCH_NAMES = frozenset(
    f"g11/wild-pack-oos/morning/{DAY}/bet-prediction-time-odds.{suffix}"
    for suffix in ("json.gz", "manifest.json")
)
RACE = re.compile(r"20261004-\d{2}-(?:0[1-9]|1[0-2])")


def _require(condition, code):
    if not condition:
        raise RuntimeError(code)


def _time(value):
    _require(isinstance(value, str), "PREDEADLINE_APPEND_TIME_MISSING")
    instant = datetime.fromisoformat(value)
    _require(instant.tzinfo is not None, "PREDEADLINE_APPEND_TIME_ZONE")
    return instant


def _research_readback(store, known):
    """Only the two explicitly identified rolling files are exempt from LOCK SHA."""
    if not RESEARCH_NAMES.intersection(known):
        return
    root = store / "g11/wild-pack-oos/morning" / DAY
    artifact = root / "bet-prediction-time-odds.json.gz"
    manifest_path = root / "bet-prediction-time-odds.manifest.json"
    _require(all(p.is_file() and not p.is_symlink() for p in (artifact, manifest_path)),
             "PREDEADLINE_RESEARCH_ARTIFACT_MISSING")
    manifest = json.loads(manifest_path.read_bytes())
    raw = artifact.read_bytes()
    value = json.loads(gzip.decompress(raw))
    unsigned = dict(value)
    content_sha = unsigned.pop("CONTENT_SHA256", None)
    _require(manifest.get("KIND") == "ODDS"
             and manifest.get("ARTIFACT") == artifact.name
             and manifest.get("OPERATIONAL_DATE_JST") == DAY
             and value.get("OPERATIONAL_DATE_JST") == DAY
             and manifest.get("PRODUCTION_MUTATED") is False
             and value.get("PRODUCTION_MUTATED") is False
             and manifest.get("ARTIFACT_SHA256") == hashlib.sha256(raw).hexdigest()
             and manifest.get("CONTENT_SHA256") == content_sha
             and content_sha == hashlib.sha256(json.dumps(
                 unsigned, ensure_ascii=False, sort_keys=True,
                 separators=(",", ":"), allow_nan=False).encode()).hexdigest(),
             "PREDEADLINE_RESEARCH_ARTIFACT_INVALID")


def _plan(store):
    morning = json.loads((store / "snapshots/morning/official-day-input.json").read_bytes())
    _require(morning.get("STAGE") == "MORNING"
             and morning.get("NAME") == "official-day-input", "PREDEADLINE_PLAN_MISSING")
    tasks = morning["PAYLOAD"]["PREDEADLINE_PLAN"]
    result = {}
    for task in tasks:
        key = task["RACE_KEY"]
        due = _time(task["DUE_AT_JST"])
        deadline = _time(task["MUST_FINISH_BEFORE_JST"])
        _require(RACE.fullmatch(key) is not None and due < deadline,
                 "PREDEADLINE_PLAN_INVALID")
        if key in result:
            _require(result[key] == deadline, "PREDEADLINE_PLAN_DEADLINE_CONFLICT")
        result[key] = deadline
    return result


def _validate_append(store, name, sha, deadlines):
    path = store / name
    _require(path.is_file() and not path.is_symlink()
             and hashlib.sha256(path.read_bytes()).hexdigest() == sha,
             "PREDEADLINE_APPEND_SHA_MISMATCH:" + name)
    match = RACE.search(name)
    _require(match is not None and match.group() in deadlines,
             "PREDEADLINE_APPEND_RACE_UNPLANNED:" + name)
    key = match.group()
    deadline = deadlines[key]
    _require(datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < deadline,
             "PREDEADLINE_APPEND_AFTER_DEADLINE:" + name)
    value = json.loads(path.read_bytes())
    if name.startswith("snapshots/predeadline/"):
        _require(value.get("STAGE") == "PREDEADLINE"
                 and value.get("NAME") == path.stem
                 and isinstance(value.get("SOURCE_RECEIPTS"), list)
                 and bool(value["SOURCE_RECEIPTS"]),
                 "PREDEADLINE_APPEND_SNAPSHOT_INVALID:" + name)
        payload = value.get("PAYLOAD", {})
        _require(payload.get("RACE_KEY", payload.get("RACE_ID")) == key,
                 "PREDEADLINE_APPEND_RACE_MISMATCH:" + name)
        for receipt in value["SOURCE_RECEIPTS"]:
            _require(_time(receipt["CAPTURED_AT_JST"]) < deadline,
                     "PREDEADLINE_APPEND_SOURCE_LATE:" + name)
    elif "/collection-packet-" in name:
        payload = value
        _require(payload.get("RACE_ID") == key, "PREDEADLINE_APPEND_RACE_MISMATCH:" + name)
    else:
        payload = value
        _require(payload.get("STATUS") == "LOCKED_BEFORE_RESULT",
                 "PREDEADLINE_APPEND_WILD_LOCK_INVALID:" + name)
    for field in ("CAPTURED_AT_JST", "PREDICTION_AT", "PREDICTION_LOCKED_AT", "LOCKED_AT"):
        if payload.get(field) is not None:
            _require(_time(payload[field]) < deadline,
                     "PREDEADLINE_APPEND_AFTER_DEADLINE:" + name)
    _require(payload.get("RESULT_FEEDBACK_TO_MODEL") is not True
             and payload.get("RESULT_USED_AS_PREDICTION_FEATURE") is not True
             and payload.get("PAYOUT_USED_AS_PREDICTION_FEATURE") is not True,
             "PREDEADLINE_APPEND_RESULT_BACKFLOW:" + name)
    if payload.get("RACE_DEADLINE_AT") is not None:
        _require(_time(payload["RACE_DEADLINE_AT"]) == deadline,
                 "PREDEADLINE_APPEND_DEADLINE_MISMATCH:" + name)


def apply(implementation, day):
    """Patch only the retained Oct 4 module in memory; never change its bytes."""
    if day != DAY:
        return implementation
    original_inventory = implementation.lock_inventory
    original_verify = implementation.verify_locks
    if getattr(original_verify, "_g11_oct4_recovery", False):
        return implementation

    def inventory(store, target, phase):
        observed = original_inventory(store, target, phase)
        if phase == "PREDEADLINE" and target == DAY:
            return {name: sha for name, sha in observed.items() if name not in RESEARCH_NAMES}
        return observed

    def verify(state, target, phase, *, exact=False):
        if phase != "PREDEADLINE" or target != DAY:
            return original_verify(state, target, phase, exact=exact)
        records = implementation.bindings(state, target, phase)
        _require(bool(records), "DAILY_PREDEADLINE_AUTHORITY_MISSING")
        known = {}
        for record in records:
            for name, sha in record["lock_files"].items():
                _require(name in RESEARCH_NAMES or name not in known or known[name] == sha,
                         "DAILY_LOCK_BINDING_CONFLICT:" + name)
                known[name] = sha
        store = state / "store"
        raw = original_inventory(store, target, phase)
        previous_mismatches = sorted(name for name, sha in known.items()
                                     if raw.get(name) != sha)
        if previous_mismatches:
            print("G11_FROZEN_PREDEADLINE_LEGACY_MISMATCH_PATHS",
                  json.dumps(previous_mismatches), file=sys.stderr)
        observed = inventory(store, target, phase)
        for name, sha in known.items():
            if name not in RESEARCH_NAMES:
                _require(observed.get(name) == sha,
                         "DAILY_PREDEADLINE_LOCK_MISMATCH:" + name)
        _research_readback(store, known)
        added = sorted(set(observed) - set(known))
        if added:
            deadlines = _plan(store)
            for name in added:
                _validate_append(store, name, observed[name], deadlines)
        print("G11_PREDEADLINE_LOCK_GATE", json.dumps({
            "known_locks": len(set(known) - RESEARCH_NAMES),
            "new_locks": len(added), "new_paths": added[:8],
            "rolling_research_paths": sorted(set(known) & RESEARCH_NAMES),
            "exact_requested": exact}, sort_keys=True), file=sys.stderr)
        return observed

    verify._g11_oct4_recovery = True
    implementation.lock_inventory = inventory
    implementation.verify_locks = verify
    return implementation


def allow_optional_shadow(store_root, race_id, snapshot_name, *, now=None):
    path = store_root / "snapshots/predeadline" / (snapshot_name + ".json")
    if path.is_file():
        return
    deadlines = _plan(store_root)
    _require(race_id in deadlines
             and (now or datetime.now(timezone.utc)) < deadlines[race_id],
             "MASHIRO_RESEARCH_SHADOW_AFTER_DEADLINE:" + race_id)


def guard_optional_mashiro_shadow():
    """Keep optional research from first writing a PRE_RACE sidecar after cutoff."""
    from g11.growth_p3.v1_1 import mashiro_v4_runtime_adapter_v1 as mashiro
    original = mashiro.publish_shadow

    def before_deadline(store, race_id, *, is_sg):
        allow_optional_shadow(store.root, race_id, mashiro.shadow_name(race_id))
        return original(store, race_id, is_sg=is_sg)

    mashiro.publish_shadow = before_deadline


def _run_same_day_rescue_predeadline(day):
    """Continue only the isolated 2026-10-06 rescue when canonical MORNING is absent."""
    if day != "2026-10-06":
        return False
    values = sys.argv[1:]
    if "--state-root" not in values:
        return False
    state = Path(values[values.index("--state-root") + 1])
    rescue_boundary = state / "rescue" / "boundary.json"
    canonical_authority = state / "daily-runtime" / day / "authority.json"
    if not rescue_boundary.is_file() or canonical_authority.exists():
        return False
    sys.path.insert(0, str(Path.cwd()))
    original_argv = sys.argv
    try:
        sys.argv = [sys.argv[0]]
        from g11.relay import rescue_entry_v2
    finally:
        sys.argv = original_argv
    rescue_entry_v2._rescue._normal_morning_exists = lambda _state: False
    base = rescue_entry_v2._CanonicalRunner
    class BoundaryRescueRunner(rescue_entry_v2._RescueCanonicalRunner):
        def run_due_predeadline(self, *args, **kwargs):
            return base.run_due_predeadline.__wrapped__(self, *args, **kwargs)
        def run_due_predeadline_predictions(self, *args, **kwargs):
            return base.run_due_predeadline_predictions.__wrapped__(self, *args, **kwargs)
        def run_predeadline_cycle(self, *args, **kwargs):
            return base.run_predeadline_cycle.__wrapped__(self, *args, **kwargs)
    rescue_entry_v2._rescue.CanonicalRunner = BoundaryRescueRunner
    if rescue_entry_v2.maybe_run_cli_rescue(values):
        print("G11_PREDEADLINE_RECOVERY=SAME_DAY_RESCUE_V2")
        return True
    return False


def main():
    mode = sys.argv[1]
    sys.argv = [sys.argv[0], *sys.argv[2:]]
    _require("--operational-date" in sys.argv, "PREDEADLINE_RECOVERY_DAY_MISSING")
    day = sys.argv[sys.argv.index("--operational-date") + 1]
    if mode == "predeadline" and _run_same_day_rescue_predeadline(day):
        return 0
    # The workflow has already verified the archive and restored the frozen
    # source; only that source is imported here.
    sys.path.insert(0, str(Path.cwd()))
    from g11.relay import daily_runtime_authority_v1 as implementation
    apply(implementation, day)
    if mode == "critical":
        from g11.relay import predeadline_critical_v1
        predeadline_critical_v1.main()
    elif mode == "predeadline":
        guard_optional_mashiro_shadow()
        from g11.relay import predeadline_nowait_v1
        return predeadline_nowait_v1.main(sys.argv[1:])
    elif mode == "night":
        from g11.relay import v1
        return v1.main(sys.argv[1:])
    else:
        raise RuntimeError("PREDEADLINE_RECOVERY_MODE")


if __name__ == "__main__":
    raise SystemExit(main())
