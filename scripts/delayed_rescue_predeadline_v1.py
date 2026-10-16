"""Continue accepted delayed recovery through the unchanged LIVE engines.

Only 2026-10-16's isolated rescue store is writable. The normal authority,
models, morning snapshots, research and monetary paths remain untouched.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re

DAY = "2026-10-16"
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

def certificate(state, day, now=None):
    require(day == DAY == (now or clock()).date().isoformat(), "CURRENT_DAY_ONLY")
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
            for field in ("pre_race_history", "pre_race_stage"):
                row["abeken_shadow"]["v53"][field] = deepcopy(next_v4.get(field))
    out["generated_at_jst"] = incoming["generated_at_jst"]
    out["source"] = deepcopy(incoming["source"])
    return out, additions

def run(state, day, output, run_id):
    cert = certificate(state, day)
    from g11.relay import daily_runtime_authority_v1 as daily
    from g11.relay import delayed_morning_recovery_v1 as recovery
    from g11.canonical.official_pipeline import CanonicalRunner, OfficialArtifactStore
    from g11.canonical import public_app_feed as feedlib
    from g11.growth_p3.v1_1 import current_runtime_v1 as p3
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
    live_publish = p3.publish_ready_live(store, store_path / "snapshots/morning",
        timely_keys(baseline["races"], clock()), deadlines_jst=deadlines)
    live_rows, _ = p3.load_public_live_projection(store, sorted(rescued))
    _, v4_rows = feedlib._abeken_public_projection(root, "PREDEADLINE", day, set(rescued))
    out = deepcopy(baseline)
    for row in out["races"]:
        key = row["key"]
        if key not in rescued:
            continue
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
    cert = certificate(state, DAY)
    incoming = json.loads(feed_path.read_bytes())
    url = origin + "/api/g11-feed?date=" + DAY
    accepted, _, _ = hot.fetch(url)
    require(accepted.get("SNAPSHOT_CLASS") == incoming.get("SNAPSHOT_CLASS") == "SAME_DAY_RESCUE_BOUNDARY"
            and accepted["same_day_rescue"]["authority_sha256"] == cert["authority_sha256"]
            and incoming.get("operational_date_jst") == DAY, "DELIVERY_IDENTITY")
    out, added = append_projection(accepted, incoming, clock())
    data = hot.compact(out)
    with tempfile.TemporaryDirectory() as directory:
        payload, response = Path(directory) / "feed.json", Path(directory) / "response.json"
        payload.write_bytes(data)
        result = subprocess.run(["curl", "--silent", "--show-error", "--max-time", "45", "-X", "POST",
            origin + "/api/g11-sync", "-H", "Authorization: Bearer " + os.environ["G11_SYNC_OIDC"],
            "-H", "Content-Type: application/json", "-H", "x-g11-feed-sha256: " + sha(data),
            "--data-binary", "@" + str(payload), "--output", str(response), "--write-out", "%{http_code}"],
            capture_output=True, text=True, check=True)
        ack = json.loads(response.read_bytes())
        require(200 <= int(result.stdout) < 300 and ack.get("status") == "PASS",
                "SITE_REJECTED:" + str(ack.get("error")))
    readback, headers, raw = hot.fetch(url)
    require(headers.get("x-g11-source-feed-sha256") == sha(data) == ack.get("payload_sha256"), "SITE_SHA_READBACK")
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
        "operational_date_jst": DAY, "authority_sha256": cert["authority_sha256"],
        "site_get_readback": "PASS", "new_live_locks": added["p3"], "new_v4_live_locks": added["v4"],
        "not_published_after_deadline": added["expired"], "site_payload_sha256": sha(data),
        "verified_p3_live_count": sum(bool(r.get("predeadline")) for r in actual.values()),
        "verified_v4_live_count": sum(bool(((r.get("abeken_shadow") or {}).get("v53") or {}).get("live")) for r in actual.values())}
    atomic(state / "rescue/predeadline-delivery.json", canonical(receipt) + b"\n")
    print(json.dumps(receipt, sort_keys=True))
    return receipt
