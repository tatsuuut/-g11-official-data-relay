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


def rescue_certificate(state, day, *, now=None, allow_night_rollover=False):
    """A rescue certificate authorizes only same-day future continuation."""
    from datetime import timedelta
    import os
    _require(not os.environ.get("G11_RELAY_NOW_JST"), "RESCUE_CLOCK_OVERRIDE")
    now = now or datetime.now(timezone(timedelta(hours=9)))
    path = state / "rescue/boundary.json"
    _require(path.is_file() and not path.is_symlink(), "RESCUE_BOUNDARY_MISSING")
    cert = json.loads(path.read_bytes())
    unsigned = dict(cert)
    sha = unsigned.pop("authority_sha256", None)
    date_ok = now.date().isoformat() == day or (allow_night_rollover and day == "2026-10-06" and now.date().isoformat() == "2026-10-07" and now.hour < 2)
    _require(day == "2026-10-06" == cert.get("operational_date_jst") and date_ok
             and cert.get("schema") == "G11_SAME_DAY_RESCUE_BOUNDARY_V1"
             and cert.get("canonical_morning_promoted") is False
             and cert.get("research_eligible") is False
             and cert.get("same_day_results_used") is False
             and cert.get("immutable") is True
             and sha == hashlib.sha256(json.dumps(unsigned, ensure_ascii=False,
                 sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
             and not (state / "daily-runtime" / day / "authority.json").exists(),
             "RESCUE_BOUNDARY_INVALID")
    return cert


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
    cert = rescue_certificate(state, day)
    sys.path.insert(0, str(Path.cwd()))
    original_argv = sys.argv
    try:
        sys.argv = [sys.argv[0]]
        from g11.relay import rescue_entry_v2
    finally:
        sys.argv = original_argv
    from g11.relay import daily_runtime_authority_v1 as daily
    _require(cert["source_files"] == daily.source_inventory(Path.cwd()), "RESCUE_SOURCE_CHANGED")
    rescue = rescue_entry_v2._rescue
    store = state / "rescue/store"
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in (store / "snapshots/morning").glob("*.json")}
    _require(bool(before), "RESCUE_BASELINE_MISSING")
    baseline = json.loads((state / "rescue/feed.json").read_bytes())
    _require(baseline["same_day_rescue"]["authority_sha256"] == cert["authority_sha256"], "RESCUE_BASELINE_BINDING")
    for row in baseline["races"]:
        proof = row.get("same_day_rescue_lock")
        if not proof:
            continue
        for name, field in (("current-growth-p3-" + row["key"].lower(), "p3_snapshot_sha256"),
                            ("abeken-v53-shadow-morning-" + row["key"].lower(), "v4_snapshot_sha256")):
            path = store / "snapshots/morning" / (name + ".json")
            _require(path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == proof[field],
                     "RESCUE_ACCEPTED_MORNING_SHA:" + name)
    # Reuse the accepted baseline; never call morning generation during bootstrap.
    manifest = {"RESCUE_LOCK_JST": cert["created_at_jst"],
                "FUTURE_RACE_KEYS": [r["key"] for r in baseline["races"] if r.get("same_day_rescue_lock")],
                "OPERATIONAL_DATE_JST": day}
    manifest.update(P3_COMPLETED=manifest["FUTURE_RACE_KEYS"], ABEKEN_COMPLETED=manifest["FUTURE_RACE_KEYS"])
    saved_manifest = state / "rescue/rescue-manifest.json"
    if saved_manifest.exists():
        manifest = json.loads(saved_manifest.read_bytes())
    rescue._ensure_rescue_baseline = lambda *_: (store, manifest)
    rescue._normal_morning_exists = lambda _state: False
    rescue._copy_hash_locked_model(state, store)
    import shutil
    for name in ("wild-pack-models",):
        shutil.copytree(state / "store/g11" / name, store / "g11" / name, dirs_exist_ok=True)
    from g11.relay import v1 as relay
    publish = relay.current_growth_p3.publish_future
    def live_only(*args, **kwargs):
        kwargs.update(include_research=False, strict_deadline=True)
        return publish(*args, **kwargs)
    relay.current_growth_p3.publish_future = live_only
    base = rescue_entry_v2._CanonicalRunner
    class BoundaryRescueRunner(rescue_entry_v2._RescueCanonicalRunner):
        def run_due_predeadline(self, *args, **kwargs):
            return base.run_due_predeadline.__wrapped__(self, *args, **kwargs)
        def run_due_predeadline_predictions(self, *args, **kwargs):
            return base.run_due_predeadline_predictions.__wrapped__(self, *args, **kwargs)
        def run_predeadline_cycle(self, *args, **kwargs):
            return base.run_predeadline_cycle.__wrapped__(self, *args, **kwargs)
    rescue_entry_v2._rescue.CanonicalRunner = BoundaryRescueRunner
    from g11.canonical import public_app_feed as rescue_feedlib
    abeken_projection = rescue_feedlib._abeken_public_projection
    rescue_feedlib._abeken_public_projection = lambda *args, **kwargs: (None, {})
    if rescue_entry_v2.maybe_run_cli_rescue(values):
        from g11.relay import same_day_boundary_20261006 as boundary
        def verified_boundary(root, source=None):
            _require(root.resolve() == store.resolve(), "RESCUE_STORE_BINDING")
            return rescue_certificate(state, day)
        boundary.verify = verified_boundary
        _, abeken_rows = abeken_projection(state / "rescue", "PREDEADLINE", day,
                                            {r["key"] for r in baseline["races"]})
        output = Path(values[values.index("--feed-output") + 1])
        incoming = json.loads(output.read_bytes())
        for row in incoming["races"]:
            if row["key"] in abeken_rows:
                row["abeken_shadow"] = abeken_rows[row["key"]]
        output.write_text(json.dumps(incoming, ensure_ascii=False, separators=(",", ":")))
        after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in (store / "snapshots/morning").glob("*.json")}
        _require(before == after, "RESCUE_MORNING_LOCK_CHANGED")
        print("G11_RESCUE_MORNING_LOCK_IMMUTABILITY=PASS")
        print("G11_PREDEADLINE_RECOVERY=SAME_DAY_RESCUE_V2")
        return True
    return False



def _run_same_day_rescue_night(day):
    """Settle the isolated 2026-10-06 rescue without inventing daily authority."""
    if day != "2026-10-06":
        return False
    values = sys.argv[1:]
    if "--state-root" not in values or "--feed-output" not in values:
        return False
    state = Path(values[values.index("--state-root") + 1]).resolve()
    if not (state / "rescue/boundary.json").is_file():
        return False
    if (state / "daily-runtime" / day / "authority.json").exists():
        return False
    cert = rescue_certificate(state, day, allow_night_rollover=True)
    store = state / "rescue/store"
    baseline = json.loads((state / "rescue/feed.json").read_bytes())
    _require(baseline["same_day_rescue"]["authority_sha256"] == cert["authority_sha256"],
             "RESCUE_NIGHT_BASELINE_BINDING")
    sys.path.insert(0, str(Path.cwd()))
    from g11.canonical import public_app_feed as feedlib
    from g11.canonical.official_pipeline import CanonicalRunner, OfficialArtifactStore, build_night_workbook_projection
    target = datetime.fromisoformat(day).date()
    artifact_store = OfficialArtifactStore(store)
    CanonicalRunner(artifact_store).run_night(target, require_complete=True)
    projection = build_night_workbook_projection(artifact_store, target)
    feed = json.loads(json.dumps(baseline))
    feedlib._apply_night(feed["races"], projection)
    for race in feed["races"]:
        race["research_eligible"] = False
        race["formal_status"] = "EXCLUDED"
        race["exclusion_reason"] = "SAME_DAY_RESCUE_NONRESEARCH"
        race["practical_hit"] = None
        race["validation_hit"] = None
        race["miss_classification"] = None
        race["research_decision"] = None
        race["research_finance"] = {"status":"NOT_ELIGIBLE","race_refund_occurred":None,
            "gross_stake":None,"refund_amount":None,"net_investment":None,
            "prize_return":None,"profit":None,"source":None}
    result_count = sum(1 for race in feed["races"]
        if isinstance(race.get("result_meta"), dict)
        and race["result_meta"].get("result_status") in ("ESTABLISHED","TRIFECTA_NOT_ESTABLISHED"))
    feed["counts"] = {"races":len(feed["races"]),"formal":0,"excluded":len(feed["races"]),
        "odds_enriched":0,"odds_waiting":0,"win_candidates":0,"results":result_count,
        "pending":len(feed["races"])-result_count,"practical_hits":0,"validation_hits":0,
        "research_samples":0}
    feed["research_db"] = {"available":False,"formal_races":0,"excluded_races":0,
        "practical_hits":0,"practical_investment":0,"practical_return":0,"practical_profit":0,
        "validation_hits":0,"rescue_hits":0,"axis_first_hits":0,"axis_top3_hits":0,
        "refund_races":0,"last_updated_at_jst":None}
    feed["stage"] = "NIGHT"
    feed["status"] = "PASS"
    feed["same_day_rescue"] = {k: v for k, v in cert.items() if k != "source_files"}
    feed["same_day_rescue"]["night_results_only"] = True
    feed["same_day_rescue"]["night_research_connected"] = False
    feed["same_day_rescue"]["same_day_results_used_for_settlement_only"] = True
    feed["daily_runtime"] = None
    output = Path(values[values.index("--feed-output") + 1])
    output.write_text(json.dumps(feed, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    result = {
        "STATUS": "SAME_DAY_RESCUE_NIGHT_RESULTS_COMPLETE",
        "PHASE": "NIGHT",
        "OPERATIONAL_DATE_JST": day,
        "RESULTS": feed["counts"]["results"],
        "RACES": feed["counts"]["races"],
        "PENDING": feed["counts"]["pending"],
        "CANONICAL_MORNING_PROMOTED": False,
        "RESEARCH_ELIGIBLE_AS_MORNING": False,
        "NIGHT_RESEARCH_CONNECTED": False,
        "PREDICTION_RECALCULATION": 0,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return True

def main():
    mode = sys.argv[1]
    sys.argv = [sys.argv[0], *sys.argv[2:]]
    _require("--operational-date" in sys.argv, "PREDEADLINE_RECOVERY_DAY_MISSING")
    day = sys.argv[sys.argv.index("--operational-date") + 1]
    if mode == "predeadline" and _run_same_day_rescue_predeadline(day):
        return 0
    if mode == "night" and _run_same_day_rescue_night(day):
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