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
    original_argv = sys.argv
    try:
        sys.argv = [sys.argv[0]]
        from g11.canonical import public_app_feed as feedlib
        from g11.canonical.official_pipeline import CanonicalRunner, OfficialArtifactStore, build_night_workbook_projection
        target = datetime.fromisoformat(day).date()
        artifact_store = OfficialArtifactStore(store)
        runner = CanonicalRunner(artifact_store)
        run_night = getattr(CanonicalRunner.run_night, "__wrapped__", CanonicalRunner.run_night)
        run_night(runner, target, require_complete=True)
        projection, _odds_records = build_night_workbook_projection(artifact_store, target)
        feed = json.loads(json.dumps(baseline))
        feedlib._apply_night(feed["races"], projection)
    finally:
        sys.argv = original_argv
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
    if mode == "night-baseline":
        restore_night_baseline(Path(sys.argv[2]).resolve(),Path(sys.argv[3]));return 0
    sys.argv = [sys.argv[0], *sys.argv[2:]]
    _require("--operational-date" in sys.argv, "PREDEADLINE_RECOVERY_DAY_MISSING")
    day = sys.argv[sys.argv.index("--operational-date") + 1]
    if mode == "night" and day in DELAYED_DAYS:
        sys.path.insert(0, str(Path.cwd()))
        state = Path(sys.argv[sys.argv.index("--state-root") + 1]).resolve()
        run_delayed_night(state, day, Path(sys.argv[sys.argv.index("--feed-output") + 1]), sys.argv[sys.argv.index("--app-origin") + 1])
        return 0
    if mode == "predeadline" and day in DELAYED_DAYS and "--state-root" in sys.argv:
        state = Path(sys.argv[sys.argv.index("--state-root") + 1]).resolve()
        if (state / "rescue/feed.json").is_file():
            sys.path.insert(0, str(Path.cwd()))
            run(state, day, Path(sys.argv[sys.argv.index("--feed-output") + 1]),
                sys.argv[sys.argv.index("--public-run-id") + 1])
            return 0
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


"""Continue accepted delayed recovery through the unchanged LIVE engines.

Only the explicitly admitted isolated rescue days are writable. The normal authority,
models, morning snapshots, research and monetary paths remain untouched.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re

DELAYED_DAY = "2026-10-16"
DELAYED_DAYS = {DELAYED_DAY, "2026-10-18", "2026-10-21"}
SCHEMA = "G11_DELAYED_MORNING_RECOVERY_BOUNDARY_V1"
JST = timezone(timedelta(hours=9))

def require(ok, reason):
    if not ok:
        raise RuntimeError("DELAYED_LIVE:" + reason)

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode()

def sha(data):
    return hashlib.sha256(data).hexdigest()

def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)

def clock():
    require(not os.environ.get("G11_RELAY_NOW_JST"), "CLOCK_OVERRIDE")
    return datetime.now(JST)

def certificate(state, day, now=None, *, allow_night_rollover=False):
    current = now or clock()
    date_ok = current.date().isoformat() == day or (allow_night_rollover and current.date() == datetime.fromisoformat(day).date() + timedelta(days=1) and current.hour < 2)
    require(day in DELAYED_DAYS and date_ok, "CURRENT_DAY_ONLY")
    path = state / "rescue/boundary.json"
    require(path.is_file() and not path.is_symlink(), "BOUNDARY_MISSING")
    cert = json.loads(path.read_bytes())
    unsigned = dict(cert)
    digest = unsigned.pop("authority_sha256", None)
    require(cert.get("schema") == SCHEMA and cert.get("operational_date_jst") == day
            and cert.get("immutable") is True and cert.get("canonical_morning_promoted") is False
            and cert.get("research_eligible") is False and cert.get("same_day_results_used") is False
            and digest == sha(canonical(unsigned)), "CERTIFICATE")
    parent = json.loads((state / "daily-runtime" / day / "authority.json").read_bytes())
    parent_unsigned = dict(parent)
    parent_sha = parent_unsigned.pop("authority_sha256", None)
    require(parent_sha == sha(canonical(parent_unsigned)) == cert.get("parent_authority_sha256")
            and parent.get("operational_date_jst") == day and parent.get("immutable") is True,
            "PARENT_AUTHORITY")
    meta = json.loads((state / "relay-state.json").read_bytes())
    require(meta.get("OPERATIONAL_DATE_JST") == day and meta.get("MORNING_COMPLETE") is False
            and not any((state / "store/snapshots/morning").glob("*.json")), "NORMAL_LOCK_CONFLICT")
    require(re.fullmatch(r"[a-f0-9]{40}", cert.get("runtime_source_sha", "")) is not None
            and bool(cert.get("source_files")) and bool(cert.get("model_files")), "RUNTIME_BINDING")
    if day == "2026-10-18":
        require(cert["authority_sha256"] == "226c9c5de084e6e7c0a5607bb1f56821d2125da46cd122d249ad4c45996160aa"
                and parent_sha == "74e0837747328847c7059d499be7197b2ba07a9a92b10f1940707c800d8c4fe8"
                and cert["runtime_source_sha"] == "122a785806395b43145a0189c5f6724d5e8e8d4a", "ADMITTED_OCT18_BOUNDARY")
    if day == "2026-10-21":
        require(cert["runtime_source_sha"] == "e0e192dd8e34df628439ccdd332748848aaf7725", "ADMITTED_OCT21_ACQUISITION_REPAIR")
    return cert

def inventory(root):
    return {p.relative_to(root).as_posix(): sha(p.read_bytes()) for p in root.rglob("*")
            if p.is_file() and not p.is_symlink()}

def validate_write(stage, name, value, deadlines, now):
    require(stage == "predeadline", "ONLY_PREDEADLINE_WRITES")
    match = re.search(r"\d{8}-\d{2}-\d{2}", name)
    key = value.get("RACE_KEY", value.get("RACE_ID", match.group() if match else None))
    require(key in deadlines and match and match.group() == key, "RESCUED_RACE_ONLY")
    require(now < deadlines[key], "SAVE_AFTER_DEADLINE")
    require(value.get("FORMAL_USE") is not True and value.get("PURCHASE_EXECUTED") is not True,
            "NO_FORMAL_PROMOTION")
    return key

def timely_keys(rows, now):
    return [r["key"] for r in rows if r.get("same_day_rescue_lock")
            and now < datetime.fromisoformat(r["deadline_jst"])]

def append_projection(accepted, incoming, now):
    require(accepted["same_day_rescue"] == incoming["same_day_rescue"], "PUBLIC_BOUNDARY_CHANGED")
    prior = {r["key"]: r for r in accepted["races"]}
    newer = {r["key"]: r for r in incoming["races"]}
    require(len(newer) == len(incoming["races"]) and set(prior) == set(newer), "PUBLIC_RACE_SET")
    out = deepcopy(accepted)
    additions = {"p3": [], "v4": [], "expired": []}
    for row in out["races"]:
        key = row["key"]
        new = newer[key]
        for field in ("same_day_rescue_lock", "practical_bets", "p3_production_bets",
                      "p3_snapshot_sha256", "production_points", "formal_status", "research_eligible"):
            require(row.get(field) == new.get(field), "MORNING_CHANGED:" + key)
        before_v4 = ((row.get("abeken_shadow") or {}).get("v53") or {})
        next_v4 = ((new.get("abeken_shadow") or {}).get("v53") or {})
        require(before_v4.get("morning") == next_v4.get("morning"), "V4_MORNING_CHANGED:" + key)
        for field, old, live in (("p3", row.get("predeadline"), new.get("predeadline")),
                                 ("v4", before_v4.get("live"), next_v4.get("live"))):
            if old is not None:
                require(old == live, "EXISTING_LIVE_CHANGED:" + key + ":" + field)
                continue
            if live is None:
                continue
            deadline = datetime.fromisoformat(row["deadline_jst"])
            if now >= deadline:
                additions["expired"].append(key + ":" + field)
                continue
            require(row.get("same_day_rescue_lock") and row.get("research_eligible") is False,
                    "NON_RESCUED_LIVE:" + key)
            require(re.fullmatch(r"[a-f0-9]{64}", live.get("snapshot_sha256", "")) is not None,
                    "LIVE_SHA:" + key)
            history = new.get("pre_race_v2", {})
            active = history.get("final") or history.get("early") or {}
            locked = active.get("locked_at_jst") if field == "p3" else live.get("locked_at_jst")
            require(locked and datetime.fromisoformat(live["captured_at_jst"]) <= datetime.fromisoformat(locked) < deadline,
                    "LIVE_TIME:" + key)
            if field == "p3":
                row["predeadline"] = deepcopy(live)
                row["pre_race_v2"] = deepcopy(new["pre_race_v2"])
                row["p3_morning_snapshot_sha256"] = new["p3_morning_snapshot_sha256"]
                for name in ("best_ev", "best_ev_bet", "value_bets", "odds_captured_at_jst"):
                    row[name] = live[name]
                row["odds_merit"] = live["legacy_odds_merit"]
            else:
                row["abeken_shadow"]["v53"]["live"] = deepcopy(live)
            additions[field].append(key)
        if row.get("pre_race_v2"):
            # Derive history only from the LIVE actually admitted above. An
            # expired unpublished V4 must never leak through its history view.
            admitted_v4 = row["abeken_shadow"]["v53"]
            live_view = admitted_v4.get("live")
            admitted_v4["pre_race_history"] = {"early": None, "final":
                {"snapshot_sha256": live_view["snapshot_sha256"], "view": deepcopy(live_view)} if live_view else None}
            admitted_v4["pre_race_stage"] = "FINAL" if live_view else "NONE"
    out["generated_at_jst"] = incoming["generated_at_jst"]
    out["source"] = deepcopy(incoming["source"])
    return out, additions

def run(state, day, output, run_id):
    cert = certificate(state, day)
    # Import without the normal CLI auto-run hook, then verify the accepted
    # rescue certificate, pinned source and both model inventories below.
    original_argv = sys.argv
    try:
        sys.argv = [sys.argv[0]]
        from g11.relay import daily_runtime_authority_v1 as daily
        from g11.relay import delayed_morning_recovery_v1 as recovery
        from g11.canonical.official_pipeline import CanonicalRunner, OfficialArtifactStore
        from g11.canonical import public_app_feed as feedlib
        from g11.growth_p3.v1_1 import current_runtime_v1 as p3
    finally:
        sys.argv = original_argv
    root = state / "rescue"
    store_path = root / "store"
    recovery.verify_boundary(root, day)
    require(daily.source_inventory(Path.cwd()) == cert["source_files"], "FROZEN_SOURCE_CHANGED")
    baseline_bytes = (root / "feed.json").read_bytes()
    baseline = json.loads(baseline_bytes)
    require(baseline["same_day_rescue"]["authority_sha256"] == cert["authority_sha256"], "BASELINE_BINDING")
    rescued = {r["key"]: r for r in baseline["races"] if r.get("same_day_rescue_lock")}
    deadlines = {key: datetime.fromisoformat(row["deadline_jst"]) for key, row in rescued.items()}
    before_morning = inventory(store_path / "snapshots/morning")
    before_canonical = inventory(state / "store/snapshots")
    before_live = inventory(store_path / "snapshots/predeadline")
    for key, row in rescued.items():
        proof = row["same_day_rescue_lock"]
        for name, field in ((p3.snapshot_name(key), "p3_snapshot_sha256"),
                            (CanonicalRunner._abeken_v53_morning_name(key), "v4_snapshot_sha256")):
            require(before_morning[name + ".json"] == proof[field], "MORNING_PROOF:" + key)
    receipts_path = root / "live-write-receipts.json"
    receipts = json.loads(receipts_path.read_bytes()) if receipts_path.exists() else {}
    class LiveStore(OfficialArtifactStore):
        def commit_snapshot(self, stage, name, value, source_receipts):
            validate_write(stage, name, value, deadlines, clock())
            target = self.root / "snapshots" / stage / (name + ".json")
            exists = target.exists()
            result = super().commit_snapshot(stage, name, value, source_receipts)
            if not exists:
                at = datetime.fromtimestamp(target.stat().st_mtime, JST).isoformat()
                validate_write(stage, name, value, deadlines, datetime.fromisoformat(at))
                receipts[name] = {"snapshot_sha256": sha(target.read_bytes()), "saved_at_jst": at}
                atomic(receipts_path, canonical(receipts) + b"\n")
            return result
    class LiveRunner(CanonicalRunner):
        @staticmethod
        def _formal_prediction_scope(verified, *, failure_prefix):
            d, keys, excluded = CanonicalRunner._formal_prediction_scope(verified, failure_prefix=failure_prefix)
            return d, [k for k in keys if k in rescued], excluded
        def run_due_predeadline(self, **kwargs):
            return CanonicalRunner.run_due_predeadline.__wrapped__(self, **kwargs)
        def run_due_predeadline_predictions(self):
            return CanonicalRunner.run_due_predeadline_predictions.__wrapped__(self)
    store = LiveStore(store_path)
    runner = LiveRunner(store)
    started = clock()
    eligible = timely_keys(baseline["races"], started)
    acquisition = runner.run_due_predeadline(max_requests=8, early_wait_seconds=0)
    prediction = runner.run_due_predeadline_predictions()
    # Independent prospective WILD input can be admitted now for future races.
    # Keep it outside canonical morning authority and never block P3/V4 delivery.
    try:
        from g11.relay import v1 as relay_runtime
        relay_runtime._run_true_ai_morning_shadow(store_path, datetime.fromisoformat(day).date())
        from g11.wild_pack.v1 import runtime_orchestrator_v1 as wild_runtime
        wild_runtime.run_morning(store_path, datetime.fromisoformat(day).date(), clock())
        original_attach = wild_runtime.bet.attach_prediction_time_odds
        def attach_future_only(upstream, snapshots):
            accepted = {}
            for record in upstream["RECORDS"]:
                packet = record["PAYLOAD"]
                key = packet["STANDARD_RACE_KEY"]
                verified_odds = snapshots.get(key)
                if verified_odds is None:
                    continue
                receipt = verified_odds["SNAPSHOT"]["SOURCE_RECEIPTS"][0]
                captured = datetime.fromisoformat(receipt["CAPTURED_AT_JST"])
                cutoff = datetime.fromisoformat(packet["UPSTREAM_PREDICTION_CUTOFF_AT"])
                deadline = datetime.fromisoformat(packet["RACE_DEADLINE_AT"])
                if cutoff <= captured < deadline:
                    accepted[key] = verified_odds
                else:
                    print("G11_RESCUE_WILD_STALE_ODDS_EXCLUDED:" + key, flush=True)
            return original_attach(upstream, accepted)
        try:
            wild_runtime.bet.attach_prediction_time_odds = attach_future_only
            wild_runtime.run_predeadline(store_path, datetime.fromisoformat(day).date())
        finally:
            wild_runtime.bet.attach_prediction_time_odds = original_attach
        print("G11_RESCUE_WILD_PROSPECTIVE_INPUT=READY", flush=True)
    except Exception as exc:
        print("G11_RESCUE_WILD_PROSPECTIVE_INPUT_UNAVAILABLE:" + type(exc).__name__ + ":" + str(exc), flush=True)
    live_publish = p3.publish_ready_live(store, store_path / "snapshots/morning",
        timely_keys(baseline["races"], clock()), deadlines_jst=deadlines)
    live_rows, _ = p3.load_public_live_projection(store, sorted(rescued))
    _, v4_rows = feedlib._abeken_public_projection(root, "PREDEADLINE", day, set(rescued))
    out = deepcopy(baseline)
    for row in out["races"]:
        key = row["key"]
        if key not in rescued:
            continue
        original_row = deepcopy(row)
        pre = live_rows.get(key)
        if pre:
            receipt = receipts[p3.live_snapshot_name(key)]
            require(receipt["snapshot_sha256"] == pre["snapshot_sha256"], "P3_LIVE_READBACK")
            row["predeadline"] = pre
            # The existing collector captures at T-7. Project that actual lock
            # into the accepted FINAL slot; do not invent an EARLY prediction.
            captured = datetime.fromisoformat(pre["captured_at_jst"])
            require(deadlines[key] - timedelta(minutes=7) <= captured < deadlines[key], "LIVE_T7_WINDOW")
            morning_sha = row["same_day_rescue_lock"]["p3_snapshot_sha256"]
            row["p3_morning_snapshot_sha256"] = morning_sha
            row["pre_race_v2"] = {"schema": "G11_PRE_RACE_DUAL_LOCK_V2", "current_stage": "FINAL", "early": None,
                "final": {"stage": "FINAL", "race_key": key, "snapshot_sha256": pre["snapshot_sha256"],
                    "morning_snapshot_sha256": morning_sha, "engine_version_pin": cert["p3"]["version"],
                    "morning_lock_mutated": False, "result_input_used": False, "predeadline": deepcopy(pre),
                    "locked_at_jst": receipt["saved_at_jst"], "captured_at_jst": pre["captured_at_jst"]}}
            final_lock = row["pre_race_v2"]["final"]
            print("G11_RESCUE_FINAL_LOCK_FULL:" + key + ":" + json.dumps(row["pre_race_v2"], ensure_ascii=False, sort_keys=True), flush=True)
            saved_at = datetime.fromisoformat(final_lock["locked_at_jst"])
            print("G11_RESCUE_FINAL_LOCK_DIAGNOSTIC:" + json.dumps({"key": key, "captured": final_lock["captured_at_jst"], "saved": final_lock["locked_at_jst"], "deadline": deadlines[key].isoformat(), "snapshot_sha": final_lock["snapshot_sha256"], "morning_sha": final_lock["morning_snapshot_sha256"], "race_morning_sha": row["p3_morning_snapshot_sha256"], "predeadline_status": final_lock["predeadline"].get("status"), "predeadline_captured": final_lock["predeadline"].get("captured_at_jst")}, sort_keys=True), flush=True)
            if not (captured <= saved_at < deadlines[key]
                    and isinstance(final_lock["snapshot_sha256"], str)
                    and len(final_lock["snapshot_sha256"]) == 64
                    and final_lock["predeadline"]["captured_at_jst"] == final_lock["captured_at_jst"]):
                print("G11_RESCUE_RACE_QUARANTINED:" + key + ":INVALID_FINAL_LOCK", flush=True)
                row.clear()
                row.update(original_row)
                continue
        av = (v4_rows.get(key, {}).get("v53") or {})
        if av.get("live"):
            require(av["morning"] == row["abeken_shadow"]["v53"]["morning"], "V4_BASELINE_READBACK")
            live = deepcopy(av["live"])
            receipt = receipts[CanonicalRunner._abeken_v53_live_name(key)]
            require(receipt["snapshot_sha256"] == live["snapshot_sha256"], "V4_LIVE_READBACK")
            live["locked_at_jst"] = receipt["saved_at_jst"]
            row["abeken_shadow"]["v53"]["live"] = live
        if row.get("pre_race_v2"):
            av = row["abeken_shadow"]["v53"]
            av["pre_race_history"] = {"early": None, "final":
                {"snapshot_sha256": av["live"]["snapshot_sha256"], "view": deepcopy(av["live"])} if av.get("live") else None}
            av["pre_race_stage"] = "FINAL" if av.get("live") else "NONE"
    require(before_morning == inventory(store_path / "snapshots/morning")
            and before_canonical == inventory(state / "store/snapshots")
            and baseline_bytes == (root / "feed.json").read_bytes(), "PROTECTED_STATE_CHANGED")
    after_live = inventory(store_path / "snapshots/predeadline")
    require(all(after_live.get(k) == v for k, v in before_live.items()), "LIVE_OVERWRITE")
    recovery.verify_boundary(root, day)
    out["generated_at_jst"] = clock().replace(microsecond=0).isoformat()
    out["source"]["run_id"] = int(run_id)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(canonical(out) + b"\n")
    result = {"STATUS": "SAME_DAY_RESCUE_COMPLETE", "PHASE": "PREDEADLINE_RESCUE",
        "OPERATIONAL_DATE_JST": day, "FEED_EMITTED": True,
        "RESCUE_FUTURE_RACE_COUNT": len(eligible), "RESCUE_EXPIRED_RACE_COUNT": len(rescued)-len(eligible),
        "LAST_RACE_DEADLINE_JST": max(deadlines.values()).isoformat(),
        "STARTED_AT_JST": started.isoformat(), "ACQUISITION": acquisition,
        "PREDICTION": prediction, "P3_LIVE": live_publish,
        "CANONICAL_MORNING_PROMOTED": False, "MORNING_IMMUTABLE": True,
        "NORMAL_SNAPSHOTS_IMMUTABLE": True, "MODELS_IMMUTABLE": True,
        "GORIPAN": "NOT_CONNECTED_NO_RESCUE_MORNING_LOCK", "NIGHT_RESEARCH_CONNECTED": False}
    atomic(root / "predeadline-audit.json", canonical(result) + b"\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return result

def deliver(state, feed_path, origin):
    """Existing OIDC sync route; append-only merge and exact public readback."""
    import subprocess
    import tempfile
    import hot_delta_transport_v1 as hot
    incoming = json.loads(feed_path.read_bytes())
    day = incoming["operational_date_jst"]
    cert = certificate(state, day)
    url = origin + "/api/g11-feed?date=" + day
    accepted, base_headers, base_raw = hot.fetch(url)
    require(accepted.get("SNAPSHOT_CLASS") == incoming.get("SNAPSHOT_CLASS") == "SAME_DAY_RESCUE_BOUNDARY"
            and accepted["same_day_rescue"]["authority_sha256"] == cert["authority_sha256"]
            and incoming.get("operational_date_jst") == day, "DELIVERY_IDENTITY")
    out, added = append_projection(accepted, incoming, clock())
    # Lossless, explicitly scoped rescue LIVE delta. Normal HOT stays unchanged.
    require(base_headers.get("x-g11-source-feed-sha256") == sha(base_raw), "DELTA_BASE_SHA")
    encoded = subprocess.run(["node", "-e",
        "let s='';process.stdin.setEncoding('utf8');process.stdin.on('data',c=>s+=c);process.stdin.on('end',()=>{"
        "const canon=v=>Array.isArray(v)?'['+v.map(canon).join(',')+']':"
        "v!==null&&typeof v==='object'?'{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+canon(v[k])).join(',')+'}':JSON.stringify(v);"
        "process.stdout.write(canon(JSON.parse(s)));});"],
        input=canonical(out), capture_output=True, check=True).stdout
    data = encoded
    prior = {r["key"]: r for r in accepted["races"]}
    changes = []
    allowed = {"predeadline", "pre_race_v2", "p3_morning_snapshot_sha256", "abeken_shadow",
               "best_ev", "best_ev_bet", "value_bets", "odds_captured_at_jst", "odds_merit"}
    for row in out["races"]:
        before = prior[row["key"]]
        diff = {k: row[k] for k in row if row.get(k) != before.get(k)}
        require(set(diff) <= allowed, "DELTA_FIELD_ADMISSION")
        if diff:
            changes.append({"RACE_KEY": row["key"], "CHANGED_FIELDS": diff})
    patch = {"SCHEMA": "G11_RESCUE_LIVE_DELTA_V1", "OPERATIONAL_DATE": day,
        "BASE_FEED_SHA": sha(base_raw), "TARGET_FEED_SHA": sha(data),
        "AUTHORITY_SHA256": cert["authority_sha256"],
        "TOP_LEVEL": {"source": out["source"], "generated_at_jst": out["generated_at_jst"]},
        "RACES": changes}
    wire = canonical(patch)
    require(len(wire) <= 900_000, "DELTA_WIRE_BUDGET")
    with tempfile.TemporaryDirectory() as directory:
        payload, response = Path(directory) / "feed.json", Path(directory) / "response.json"
        payload.write_bytes(wire)
        result = subprocess.run(["curl", "--silent", "--show-error", "--max-time", "45", "-X", "POST",
            origin + "/api/g11-sync", "-H", "Authorization: Bearer " + os.environ["G11_SYNC_OIDC"],
            "-H", "Content-Type: application/json", "-H", "x-g11-feed-sha256: " + sha(wire),
            "--data-binary", "@" + str(payload), "--output", str(response), "--write-out", "%{http_code}"],
            capture_output=True, text=True, check=True)
        ack = json.loads(response.read_bytes())
        require(200 <= int(result.stdout) < 300 and ack.get("status") == "PASS",
                "SITE_REJECTED:" + str(ack.get("error")))
    readback, headers, raw = hot.fetch(url)
    require(headers.get("x-g11-source-feed-sha256") == sha(data) == ack.get("payload_sha256"), "SITE_SHA_READBACK")
    require(readback == out, "DELTA_FULL_READBACK")
    require(readback["same_day_rescue"] == out["same_day_rescue"] and readback["counts"]["formal"] == 0,
            "SITE_BOUNDARY_READBACK")
    actual = {r["key"]: r for r in readback["races"]}
    require(set(actual) == {r["key"] for r in out["races"]}, "SITE_RACE_READBACK")
    for row in out["races"]:
        for field in ("same_day_rescue_lock", "practical_bets", "p3_production_bets", "p3_snapshot_sha256",
                      "production_points", "formal_status", "research_eligible", "abeken_shadow", "predeadline", "pre_race_v2"):
            require(actual[row["key"]].get(field) == row.get(field), "SITE_FIELD_READBACK:" + row["key"] + ":" + field)
    Path(".relay-output/hot-delta-accepted").write_text(sha(data) + "\n")
    Path(".relay-output/hot-delta-readback.json").write_bytes(raw)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
            handle.write("feed_sha=" + sha(data) + "\n")
    receipt = {"schema": "G11_DELAYED_RESCUE_PREDEADLINE_DELIVERY_V1", "status": "PASS",
        "operational_date_jst": day, "authority_sha256": cert["authority_sha256"],
        "site_get_readback": "PASS", "new_live_locks": added["p3"], "new_v4_live_locks": added["v4"],
        "not_published_after_deadline": added["expired"], "site_payload_sha256": sha(data),
        "verified_p3_live_count": sum(bool(r.get("predeadline")) for r in actual.values()),
        "verified_v4_live_count": sum(bool(((r.get("abeken_shadow") or {}).get("v53") or {}).get("live")) for r in actual.values())}
    atomic(state / "rescue/predeadline-delivery.json", canonical(receipt) + b"\n")
    print(json.dumps(receipt, sort_keys=True))
    return receipt


def night_overlay(accepted, results, now):
    """Result-only projection; every prediction/LOCK/research field is retained."""
    day = accepted.get('operational_date_jst')
    require(day in DELAYED_DAYS
            and accepted.get('SNAPSHOT_CLASS') == 'SAME_DAY_RESCUE_BOUNDARY'
            and accepted.get('counts', {}).get('formal') == 0, 'NIGHT_ACCEPTED_SCOPE')
    rows = accepted['races']
    keys = [r['key'] for r in rows]
    require(len(set(keys)) == len(keys) and all(k.startswith(day.replace('-', '')+'-') for k in keys), 'NIGHT_DUPLICATE_OR_DAY')
    by_key = {r.race_key: r for r in results}
    require(len(by_key) == len(results) and set(by_key) == set(keys), 'NIGHT_RESULT_SCOPE')
    require(all(datetime.fromisoformat(r['deadline_jst']) < now for r in rows), 'NIGHT_BEFORE_DEADLINE')
    out = deepcopy(accepted)
    for row in out['races']:
        result = by_key[row['key']]
        require(result.operational_date == day and result.venue_code == row['venue_code']
                and result.race_no == row['race'], 'NIGHT_RESULT_IDENTITY')
        meta = deepcopy(row['result_meta'])
        meta.update(trifecta_popularity=result.trifecta_popularity, winning_technique=result.winning_method,
            result_status=result.race_status, dead_heat=len(result.trifecta_settlements)>1,
            trifecta_settlements=[{'trifecta':s.combination,'payout':s.payout,'popularity':s.popularity}
                                  for s in result.trifecta_settlements])
        require(result.race_status in {'ESTABLISHED','TRIFECTA_NOT_ESTABLISHED'}, 'NIGHT_RESULT_STATUS')
        if row.get('result_trifecta') is not None or accepted.get('stage') == 'NIGHT':
            require(row['result_trifecta'] == result.trifecta_combination and row['payout'] == result.trifecta_payout
                    and row['result_meta'] == meta, 'NIGHT_EXISTING_RESULT_CONFLICT')
        row['result_trifecta'], row['payout'], row['result_meta'] = result.trifecta_combination,result.trifecta_payout,meta
        # No formal or research finance is generated for a rescue day.
    out['stage']='NIGHT';out['status']='PASS'
    if accepted.get('stage') != 'NIGHT': out['generated_at_jst']=now.replace(microsecond=0).isoformat()
    out['counts']={**out['counts'],'results':len(rows),'pending':0}
    return out


def night_scores(feed, results):
    """Separate rescue audit, never a formal BET or research ledger."""
    official={r.race_key:r for r in results};audit={}
    for row in feed['races']:
        r=official[row['key']]; proof=row.get('same_day_rescue_lock') or {}
        v4=(row.get('abeken_shadow') or {}).get('v53') or {}
        lock=(row.get('pre_race_v2') or {}).get('final') or {}
        selections={'p3_rescue':(row.get('practical_bets'),proof.get('p3_locked_at_jst')),
          'abeken_rescue':((v4.get('morning') or {}).get('practical_bets'),proof.get('v4_locked_at_jst')),
          'p3_live':((row.get('predeadline') or {}).get('practical_bets'),lock.get('locked_at_jst')),
          'abeken_live':((v4.get('live') or {}).get('practical_bets'),(v4.get('live') or {}).get('locked_at_jst'))}
        entries={}
        for system,(bets,at) in selections.items():
            if not bets or not at:
                entries[system]={'status':'NO_FIXED_PREDICTION'};continue
            require(len(set(bets))==len(bets) and datetime.fromisoformat(at)<datetime.fromisoformat(row['deadline_jst']), 'NIGHT_FIXED_PROOF')
            require(all(re.fullmatch(r'[1-6]-[1-6]-[1-6]',b) and len(set(b.split('-')))==3 for b in bets), 'NIGHT_BETS')
            gross=100*len(bets);refund=100*sum(bool(set(map(int,b.split('-')))&set(r.refund_boats)) for b in bets)
            payout=sum(s.payout for s in r.trifecta_settlements if s.combination in bets)
            entries[system]={'status':'SETTLED','fixed_at':at,'bets':bets,'hit':any(s.combination in bets for s in r.trifecta_settlements),
              'virtual_investment':gross,'virtual_refund':refund,'virtual_payout':payout,'virtual_profit':payout+refund-gross}
        entries['goripan']={'status':'NO_FIXED_BET_NO_BACKFILL'}
        audit[row['key']]={'official_result':r.trifecta_combination,'official_payout':r.trifecta_payout,'engines':entries}
    return audit


def restore_night_baseline(state, archive):
    """Read an exact pre-incident checkpoint; restore only a derived status timestamp."""
    import tarfile
    roots=['store','daily-runtime','rescue/store/snapshots/morning','rescue/store/snapshots/predeadline']
    baseline=[{} for _ in roots];old_status=None
    target='store/g11/wild-pack-oos/morning/bet-status.json'
    with tarfile.open(archive,'r:gz') as tar:
        for member in tar:
            name=member.name.removeprefix('./')
            if not member.isfile():continue
            for i,root in enumerate(roots):
                if name.startswith(root+'/'):
                    data=tar.extractfile(member).read();baseline[i][name[len(root)+1:]]=sha(data)
                    if name==target:old_status=data
    require(all(baseline) and old_status is not None,'NIGHT_BASELINE_MISSING')
    current=[inventory(state/root) for root in roots]
    changes=[roots[i]+'/'+k for i in range(len(roots)) for k in set(current[i])|set(baseline[i]) if current[i].get(k)!=baseline[i].get(k)]
    require(set(changes)<= {target},'NIGHT_BASELINE_UNEXPECTED_CHANGES:'+','.join(changes[:8]))
    if changes:
        old=json.loads(old_status);now=json.loads((state/target).read_bytes())
        for value in (old,now):
            value.pop('UPDATED_AT_UTC',None);value.pop('CONTENT_SHA256',None)
        require(old==now,'NIGHT_BASELINE_STATUS_CONTENT_CHANGED')
        atomic(state/target,old_status)
    require([inventory(state/root) for root in roots]==baseline,'NIGHT_BASELINE_READBACK')
    proof={'schema':'G11_NIGHT_PREINCIDENT_PRESERVATION_V1','cache_key':'g11-state-v4-published-2026-10-16-38911423461',
        'protected_sha256':sha(canonical(baseline)),'restored_derived_status_only':bool(changes)}
    atomic(state/'rescue/night-preservation.json',canonical(proof)+b'\n')
    print(json.dumps({'NIGHT_PREINCIDENT_READBACK':'PASS','RESTORED_DERIVED_STATUS_ONLY':bool(changes)}))


def rescue_night_delta(accepted, out, base_sha):
    """Bound the result-only wire, preserving every saved canonical byte/LOCK."""
    require(out['operational_date_jst'] == accepted['operational_date_jst'] and out['operational_date_jst'] in {'2026-10-18', '2026-10-21'}
        and accepted['stage'] == 'MORNING' and out['stage'] == 'NIGHT'
        and out['same_day_rescue'] == accepted['same_day_rescue']
        and len(out['races']) == len(accepted['races']) == 156, 'NIGHT_DELTA_IDENTITY')
    protected = deepcopy(out)
    for k in ('stage', 'status', 'generated_at_jst'):
        protected[k] = accepted[k]
    protected['counts'] = deepcopy(accepted['counts'])
    prior = {r['key']: r for r in accepted['races']}
    require(len(prior) == 156 and {r['key'] for r in out['races']} == set(prior), 'NIGHT_DELTA_RACE_SET')
    changes = []
    for row, restored in zip(out['races'], protected['races']):
        old = prior[row['key']]
        changes.append({'RACE_KEY': row['key'], 'CHANGED_FIELDS':
            {k: deepcopy(row[k]) for k in ('result_trifecta', 'payout', 'result_meta')}})
        for k in ('result_trifecta', 'payout', 'result_meta'):
            restored[k] = deepcopy(old[k])
    require(protected == accepted and out['status'] == 'PASS'
        and out['counts'] == {**accepted['counts'], 'results': 156, 'pending': 0}, 'NIGHT_DELTA_NONRESULT_MUTATION')
    wire = canonical({'SCHEMA': 'G11_RESCUE_NIGHT_DELTA_V1', 'OPERATIONAL_DATE': out['operational_date_jst'],
        'BASE_FEED_SHA': base_sha, 'TARGET_FEED_SHA': sha(canonical(out)+b'\n'),
        'AUTHORITY_SHA256': out['same_day_rescue']['authority_sha256'],
        'TOP_LEVEL': {k: deepcopy(out[k]) for k in ('stage','status','generated_at_jst','counts')},
        'RACES': changes})
    require(len(wire) <= 900_000, 'NIGHT_DELTA_WIRE_BUDGET')
    return wire


def run_delayed_night(state, day, output, origin):
    Path(".relay-output").mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    cert=certificate(state, day, allow_night_rollover=True)
    original_argv=sys.argv
    try:
        sys.argv=[sys.argv[0]]
        from g11.relay import daily_runtime_authority_v1 as daily
        from g11.relay import delayed_morning_recovery_v1 as recovery
        from g11.canonical.official_pipeline import CanonicalRunner,OfficialArtifactStore,result_url,parse_result_text,decode_cp932,_serializable
    finally:sys.argv=original_argv
    import hot_delta_transport_v1 as hot
    recovery.verify_boundary(state/'rescue',day)
    require(daily.source_inventory(Path.cwd()) == cert['source_files'], 'NIGHT_SOURCE_CHANGED')
    protected_roots=[state/'store',state/'daily-runtime',state/'rescue/store/snapshots/morning',state/'rescue/store/snapshots/predeadline']
    before=[inventory(p) for p in protected_roots]
    boundary_bytes=(state/'rescue/boundary.json').read_bytes();baseline_bytes=(state/'rescue/feed.json').read_bytes()
    accepted,headers,raw=hot.fetch(origin+'/api/g11-feed?date='+day)
    require(accepted.get('same_day_rescue',{}).get('authority_sha256') == cert['authority_sha256'], 'NIGHT_BOUNDARY')
    root=state/'rescue'; saved=root/'night-feed.json';report=root/'night-audit.json'
    if saved.exists():
        out=json.loads(saved.read_bytes());a=json.loads(report.read_bytes())
        if day == DELAYED_DAY:
            proof=json.loads((root/'night-preservation.json').read_bytes())
            require(proof.get('cache_key')=='g11-state-v4-published-2026-10-16-38911423461', 'NIGHT_INCIDENT_BASELINE')
        else:
            proof=a
        require(a['feed_sha256']==sha(saved.read_bytes()) and proof['protected_sha256']==sha(canonical(before)), 'NIGHT_REPLAY_BINDING')
        require(out['same_day_rescue']==accepted['same_day_rescue'], 'NIGHT_REPLAY_BOUNDARY')
        output.write_bytes(saved.read_bytes())
        print(json.dumps(a['runtime_result'],sort_keys=True));return
    class ResultsOnlyStore(OfficialArtifactStore):
        def commit_snapshot(self,stage,name,value,source_receipts):
            require(stage=='night' and name=='official-day-results','NIGHT_ONLY_OFFICIAL_RESULTS_WRITE')
            return super().commit_snapshot(stage,name,value,source_receipts)
    store=ResultsOnlyStore(root/'store');runner=CanonicalRunner(store)
    target=datetime.fromisoformat(day).date();url=result_url(target)
    morning=store.load_verified_snapshot('morning','official-day-input')['SNAPSHOT']['PAYLOAD']
    require(morning['OPERATIONAL_DATE_JST']==day,'NIGHT_MORNING_DAY')
    expected={r['operational_date'].replace('-','')+f"-{r['venue_code']}-{r['race_no']:02d}" for r in morning['RACES']}
    require(expected=={r['key'] for r in accepted['races']},'NIGHT_INPUT_SCOPE')
    def parse(raw):
        results=parse_result_text(decode_cp932(runner.extractor.extract_one(raw,runner._archive_member(url))),target,require_full_races=False)
        require(len(results)==len(expected) and {r.race_key for r in results}==expected,'NIGHT_OFFICIAL_INCOMPLETE')
        return results
    receipt,results=runner.client.fetch_validated_once(f'result-k-{target:%Y%m%d}',url,parse)
    store.commit_snapshot('night','official-day-results',{'STATUS':'NIGHT_RESULTS_COMPLETE','OPERATIONAL_DATE_JST':day,
      'EXPECTED_RESULT_COUNT':len(expected),'PENDING_RESULT_COUNT':0,'PENDING_RACE_KEYS':[],
      'RESULTS':[_serializable(r) for r in results]},[receipt])
    # Existing accepted forecasts must still be backed by the immutable rescue baseline.
    baseline=json.loads(baseline_bytes);base_rows={r['key']:r for r in baseline['races']}
    for row in accepted['races']:
        for field in ('same_day_rescue_lock','practical_bets','p3_production_bets','p3_snapshot_sha256'):
            require(row.get(field)==base_rows[row['key']].get(field),'NIGHT_BASELINE_CHANGED')
        proof=row.get('same_day_rescue_lock')
        if proof:
            require(proof['p3_snapshot_sha256'] in before[2].values() and proof['v4_snapshot_sha256'] in before[2].values(),'NIGHT_MORNING_SNAPSHOT')
        for obj in [row.get('predeadline'),((row.get('abeken_shadow') or {}).get('v53') or {}).get('live')]:
            if obj:require(obj.get('snapshot_sha256') in before[3].values(),'NIGHT_LIVE_SNAPSHOT')
        require(not ((row.get('wild_pack') or {}).get('wild4') or {}).get('production_bet') and not (row.get('wild_pack') or {}).get('morning') and not (row.get('wild_pack') or {}).get('pre_race'),'NIGHT_UNEXPECTED_WILD_BET')
    out=night_overlay(accepted,results,clock())
    # Audit also includes already-fixed LIVE snapshots withheld by a failed
    # predeadline publication. They are never appended to the public forecast.
    from g11.growth_p3.v1_1 import current_runtime_v1 as p3
    from g11.canonical import public_app_feed as feedlib
    rescued={r['key'] for r in accepted['races'] if r.get('same_day_rescue_lock')}
    p3_rows,_=p3.load_public_live_projection(store,sorted(rescued))
    _,v4_rows=feedlib._abeken_public_projection(root,'PREDEADLINE',day,rescued)
    receipts=json.loads((root/'live-write-receipts.json').read_bytes())
    audit_feed=deepcopy(out);unpublished=[]
    for row in audit_feed['races']:
        key=row['key']
        if key not in rescued:continue
        for engine,live,name in [('p3',p3_rows.get(key),p3.live_snapshot_name(key)),
              ('abeken',(v4_rows.get(key,{}).get('v53') or {}).get('live'),CanonicalRunner._abeken_v53_live_name(key))]:
            if not live:continue
            receipt_live=receipts[name];at=receipt_live['saved_at_jst']
            require(receipt_live['snapshot_sha256']==live['snapshot_sha256']
                and live['snapshot_sha256'] in before[3].values()
                and datetime.fromisoformat(live['captured_at_jst'])<=datetime.fromisoformat(at)<datetime.fromisoformat(row['deadline_jst']), 'NIGHT_STORED_LIVE_PROOF')
            published=row.get('predeadline') if engine=='p3' else row['abeken_shadow']['v53'].get('live')
            if published:require(published['snapshot_sha256']==live['snapshot_sha256'],'NIGHT_PUBLIC_LIVE_CONFLICT')
            else:unpublished.append(key+':'+engine)
            if engine=='p3':
                row['predeadline']=deepcopy(live);row['pre_race_v2']={'final':{'locked_at_jst':at}}
            else:
                row['abeken_shadow']['v53']['live']={**deepcopy(live),'locked_at_jst':at}
    scores=night_scores(audit_feed,results)
    require(before==[inventory(p) for p in protected_roots] and boundary_bytes==(root/'boundary.json').read_bytes()
        and baseline_bytes==(root/'feed.json').read_bytes(),'NIGHT_PROTECTED_MUTATION')
    recovery.verify_boundary(root,day)
    result={'STATUS':'SAME_DAY_RESCUE_NIGHT_RESULTS_COMPLETE','PHASE':'NIGHT','OPERATIONAL_DATE_JST':day,
        'RESULTS':len(results),'RACES':len(expected),'PENDING':0,'PREDICTION_RECALCULATION':0,
        'CANONICAL_MORNING_PROMOTED':False,'NIGHT_RESEARCH_CONNECTED':False,
        'SETTLED_BY_SYSTEM':{s:sum(v['engines'][s]['status']=='SETTLED' for v in scores.values()) for s in ('p3_rescue','abeken_rescue','p3_live','abeken_live')},
        'GORIPAN':'NO_FIXED_BET_NO_BACKFILL','PROTECTED_UNCHANGED':True,'FIXED_BUT_NOT_PUBLISHED_LIVE':unpublished}
    data=canonical(out)+b'\n'
    if day in {'2026-10-18', '2026-10-21'}:
        rescue_night_delta(accepted, out, sha(raw))
    else:
        require(len(data)<=2097152,'NIGHT_DELIVERY_SIZE')
    atomic(saved,data);atomic(report,canonical({'runtime_result':result,'official_url':url,
        'official_receipt':_serializable(receipt),'feed_sha256':sha(data),'accepted_feed_sha256':sha(raw),
        'protected_sha256':sha(canonical(before)),'by_race':scores})+b'\n')
    output.write_bytes(data);print(json.dumps(result,sort_keys=True))


def deliver_delayed_night(state,feed_path,origin):
    Path(".relay-output").mkdir(parents=True, exist_ok=True)
    import hot_delta_transport_v1 as hot
    import subprocess,tempfile
    data=feed_path.read_bytes();out=json.loads(data)
    day=out['operational_date_jst']
    certificate(state,day,allow_night_rollover=True)
    require(data==(state/'rescue/night-feed.json').read_bytes(),'NIGHT_SAVED_OUTPUT')
    accepted,headers,raw=hot.fetch(origin+'/api/g11-feed?date='+day)
    def without_results(f):
        v=deepcopy(f)
        for k in ('stage','status','generated_at_jst'):v.pop(k,None)
        for k in ('results','pending'):v['counts'].pop(k,None)
        for row in v['races']:
            for k in ('result_trifecta','payout','result_meta'):row.pop(k,None)
        return v
    require(without_results(accepted)==without_results(out),'NIGHT_PREDICTION_OR_RESEARCH_MUTATION')
    require(out['stage']=='NIGHT' and out['counts']['results']==len(out['races']) and out['counts']['pending']==0,'NIGHT_INCOMPLETE')
    base_sha=headers.get('x-g11-source-feed-sha256')
    require(base_sha==sha(raw),'NIGHT_BASE_SHA')
    audit_sha=sha((state/'rescue/night-audit.json').read_bytes())
    checkpoint_sha=sha(('\n'.join(['G11_DELAYED_RESCUE_NIGHT_COMMIT_V1',day,base_sha,sha(data),
        out['same_day_rescue']['authority_sha256'],audit_sha])).encode())
    if accepted['stage']=='NIGHT':require(accepted==out,'NIGHT_ALREADY_SETTLED_CONFLICT')
    else:
        with tempfile.TemporaryDirectory() as directory:
            wire = rescue_night_delta(accepted,out,base_sha) if day in {'2026-10-18', '2026-10-21'} else data
            p=Path(directory)/'feed.json';resp=Path(directory)/'response.json';p.write_bytes(wire)
            r=subprocess.run(['curl','--silent','--show-error','--max-time','45','-X','POST',origin+'/api/g11-sync',
              '-H','Authorization: Bearer '+os.environ['G11_SYNC_OIDC'],'-H','Content-Type: application/json',
              '-H','x-g11-feed-sha256: '+sha(wire),'-H','x-g11-rescue-night-base-sha256: '+base_sha,
              '-H','x-g11-rescue-night-audit-sha256: '+audit_sha,'-H','x-g11-rescue-night-checkpoint-sha256: '+checkpoint_sha,'--data-binary','@'+str(p),'--output',str(resp),'--write-out','%{http_code}'],capture_output=True,text=True,check=True)
            ack=json.loads(resp.read_bytes())
            require(200<=int(r.stdout)<300 and ack.get('status')=='PASS','NIGHT_SITE_REJECTED:'+str(ack.get('error')))
    actual,headers,raw=hot.fetch(origin+'/api/g11-feed?date='+day)
    require(actual==out and headers.get('x-g11-source-feed-sha256')==sha(data),'NIGHT_SITE_READBACK')
    Path('.relay-output/hot-delta-accepted').write_text(sha(data)+'\n')
    Path('.relay-output/hot-delta-readback.json').write_bytes(raw)
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'],'a') as f:f.write('feed_sha='+sha(data)+'\n')
    receipt={'schema':'G11_DELAYED_RESCUE_NIGHT_DELIVERY_V1','status':'PASS','operational_date_jst':day,
        'results':len(out['races']),'pending':0,'audit_sha256':audit_sha,'checkpoint_sha256':checkpoint_sha,'site_get_readback':'PASS','site_payload_sha256':sha(data),'protected_unchanged':True}
    atomic(state/'rescue/night-delivery.json',canonical(receipt)+b'\n');print(json.dumps(receipt,sort_keys=True));return receipt

if __name__ == "__main__":
    raise SystemExit(main())

