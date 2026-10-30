"""Read-only V53 first-event proof for the public relay's verified readback step."""
import json
import sys
import hashlib
import re
from datetime import datetime
from pathlib import Path

C1_SOURCE = "59ef15ffa2cc2358411ad28d33e5b3487dcd5462"
C1_VERSION = "ABEKEN_IMITATION_COMBINATION_C1_V1"
C1_MARKER = "IMITATION_COMBINATION_VERSION"
E1_VERSION = "ABEKEN_IMITATION_BASELINE_E_V1"


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def clock(value):
    result = datetime.fromisoformat(value)
    require(result.utcoffset() is not None, "C1_TIMEZONE")
    return result


def verify_c1_record(day, race, morning, live=None, beforeinfo=None):
    """Inspect verified saved envelopes; never run a decoder or create a LOCK."""
    key = race["key"]
    require(re.fullmatch(day.replace("-", "") + r"-[0-9]{2}-(?:0[1-9]|1[0-2])", key), "C1_RACE_DAY")
    public = race["abeken_shadow"]["v53"]
    mp = morning["SNAPSHOT"]["PAYLOAD"]
    mr = mp["RESULT"]
    deadline = clock(race["deadline_jst"])
    require(mp.get("FORMAL_USE") is True and mp.get("RACE_KEY") == key
        and mr.get("STATUS") == "READY" and mr.get("SNAPSHOT_TYPE") == "MORNING"
        and mr.get("RACE_ID") == key and mr.get("RACE_DATE") == day
        and mr.get("CANDIDATE_INPUT", {}).get(C1_MARKER) == C1_VERSION
        and mr.get("CANDIDATE_INPUT", {}).get("IMITATION_BASELINE_VERSION") == E1_VERSION,
        "C1_MORNING_MARKER_OR_IDENTITY")
    require(clock(mr["LOCKED_AT"]) < deadline
        and morning["SNAPSHOT_SHA256"] == public["morning"]["snapshot_sha256"]
        and mr["DECISION"]["BUY_SET"] == public["morning"]["practical_bets"]
        and mr["DECISION"]["BUY_COUNT"] == public["morning"]["buy_count"], "C1_MORNING_READBACK")
    result = {"race_key": key, "morning_snapshot_sha256": morning["SNAPSHOT_SHA256"],
        "marker": C1_VERSION, "live_status": "NOT_YET_OBSERVED"}
    if live is None:
        require(public.get("live") is None, "C1_PUBLIC_LIVE_WITHOUT_SOURCE")
        return result
    lp = live["SNAPSHOT"]["PAYLOAD"]
    lr = lp["RESULT"]
    audit = lr["RIGHT_TRANSITION_AUDIT"]
    baseline = audit.get("E1_BASELINE_AUDIT", {})
    require(lp.get("FORMAL_USE") is True and lp.get("RACE_KEY") == key
        and lp.get("MORNING_V53_SNAPSHOT_SHA256") == morning["SNAPSHOT_SHA256"]
        and lp.get("P3_INPUT_USED") is False and lp.get("WILD_INPUT_USED") is False
        and lp.get("RESULT_FEEDBACK_TO_MODEL") is False
        and lr.get("RACE_ID") == key and lr.get("RACE_DATE") == day
        and lr.get("STATUS") == "READY" and lr.get("SNAPSHOT_TYPE") == "PRE_RACE"
        and audit.get(C1_MARKER) == C1_VERSION, "C1_LIVE_MARKER_OR_IDENTITY")
    combination = audit.get("COMBINATION_AUDIT", {})
    require(combination.get("CONFIG_ID") == "INSIDE_CLAIM|SEMANTIC4|E|ONE|E"
        and combination.get("LABELS_USED") is False
        and combination.get("OFFICIAL_RESULTS_USED") is False
        and baseline.get("REFERENCE_ONLY_NOT_FORMAL_BET") is True
        and baseline.get("IMITATION_BASELINE_VERSION") == E1_VERSION
        and isinstance(baseline.get("BUY_SET"), list) and bool(baseline["BUY_SET"]),
        "C1_E1_REFERENCE_OR_CONFIG")
    bp = beforeinfo["SNAPSHOT"]["PAYLOAD"]
    require(bp.get("RACE_KEY") == key and bp.get("KIND") == "BEFOREINFO"
        and bp.get("STATUS") == "PREDEADLINE_SOURCE_COMPLETE"
        and lp.get("BEFOREINFO_SNAPSHOT_SHA256") == beforeinfo["SNAPSHOT_SHA256"]
        and lr["PRE_RACE_DATA_CAPTURED_AT"] == bp["CAPTURED_AT_JST"]
        and clock(mr["LOCKED_AT"]) <= clock(lr["PRE_RACE_DATA_CAPTURED_AT"])
        <= clock(lr["LOCKED_AT"]) < deadline, "C1_PREDEADLINE_BINDING")
    require(live["SNAPSHOT_SHA256"] == public["live"]["snapshot_sha256"]
        and lr["DECISION"]["BUY_SET"] == public["live"]["practical_bets"]
        and lr["DECISION"]["BUY_COUNT"] == public["live"]["buy_count"]
        and lr["DECISION"]["MAIN_HEAD"] == public["live"]["main_head"]
        and clock(lr["LOCKED_AT"]) == clock(public["live"]["locked_at_jst"]), "C1_LIVE_READBACK")
    return {**result, "live_status": "PASS", "live_snapshot_sha256": live["SNAPSHOT_SHA256"],
        "e1_reference_sha256": digest(baseline), "e1_bets_sha256": digest(baseline["BUY_SET"]),
        "c1_bets_sha256": digest(lr["DECISION"]["BUY_SET"]),
        "e1_points": len(baseline["BUY_SET"]), "c1_points": lr["DECISION"]["BUY_COUNT"],
        "same_bets": set(baseline["BUY_SET"]) == set(lr["DECISION"]["BUY_SET"])}


def audit_c1(day, feed, load_snapshot):
    if day < "2026-10-30" or feed.get("daily_runtime", {}).get("runtime_source_sha") != C1_SOURCE:
        return {"status": "OUTSIDE_PINNED_C1_SOURCE", "date": day}
    require(feed.get("operational_date_jst") == day, "C1_FEED_DAY")
    records, failures = [], []
    races = [r for r in feed.get("races", []) if r.get("date_jst") == day
        and r.get("abeken_shadow", {}).get("v53", {}).get("formal_use") is True]
    require(len({r["key"] for r in races}) == len(races), "C1_DUPLICATE_RACE")
    for race in races:
        key = race["key"]
        try:
            morning = load_snapshot("morning", "abeken-v53-shadow-morning-" + key.lower())
            live = beforeinfo = None
            if isinstance(race["abeken_shadow"]["v53"].get("live"), dict):
                live = load_snapshot("predeadline", "abeken-v53-shadow-live-" + key.lower())
                beforeinfo = load_snapshot("predeadline", "beforeinfo-" + key.lower())
            records.append(verify_c1_record(day, race, morning, live, beforeinfo))
        except Exception as exc:
            failures.append({"race_key": key, "reason": type(exc).__name__ + ":" + str(exc)[:160]})
    return {"status": "FAIL" if failures else "PASS" if records else "NO_FORMAL_RECORDS",
        "date": day, "runtime_source_sha": C1_SOURCE, "v53_source_records": len(races),
        "formal_eligible_records": sum(r.get("formal_status") == "FORMAL"
            and r.get("research_eligible") is True for r in races),
        "morning_verified": len(records), "live_verified": sum(r["live_status"] == "PASS" for r in records),
        "forward_imitation_accuracy": "UNOBSERVED_GENUINE_LABELS_REQUIRED",
        "records": records, "failures": failures, "prediction_calls": 0, "source_writes": 0}


def require(condition, code):
    if not condition:
        raise ValueError(code)
def main(day, store_root, readback_path):
    from g11.abeken.v53 import abeken_shadow_v53_runtime as v53_runtime
    from g11.canonical.official_pipeline import OfficialArtifactStore
    print("PRE_MORNING_ARM_STATUS=PASS")
    print("V53_PROVISIONAL_ACTIVE_STATUS=PROVISIONAL_ACTIVE")
    if day < "2026-09-29":
        print("V53_FIRST_EVENT_PROOF=WAITING_FIRST_FORMAL_DAY")
        return
    feed = json.loads(Path(readback_path).read_text())
    races = sorted(
        (row for row in feed.get("races", [])
         if row.get("date_jst") == day
         and isinstance(row.get("abeken_shadow"), dict)
         and isinstance(row["abeken_shadow"].get("v53"), dict)
         and row["abeken_shadow"]["v53"].get("formal_use") is True),
        key=lambda row: (row["deadline_jst"], row["key"]),
    )
    if not races:
        print("V53_MORNING_LIVE_PROOF=FAIL TECHNICAL_BLOCKER=NO_FORMAL_V53_MORNING_IN_APP_READBACK")
        return
    first = races[0]
    race_key = first["key"]
    public = first["abeken_shadow"]["v53"]
    root = Path(store_root).resolve(strict=True)
    store = OfficialArtifactStore(root)
    # This is diagnostic only: failed evidence never changes normal operation.
    try:
        c1 = audit_c1(day, feed, store.load_verified_snapshot)
    except Exception as exc:
        c1 = {"status": "FAIL", "date": day, "reason": type(exc).__name__ + ":" + str(exc)[:160]}
    print("V53_C1_SAVED_READBACK=" + json.dumps(c1, sort_keys=True))
    print(f"FIRST_LIVE_MORNING_RACE_ID={race_key}")
    try:
        morning = store.load_verified_snapshot("morning", f"abeken-v53-shadow-morning-{race_key.lower()}")
        payload = morning["SNAPSHOT"]["PAYLOAD"]
        decision = v53_runtime.validate_decision(payload["RESULT"]["DECISION"])
        version = v53_runtime.RUNTIME_LOGIC_VERSION
        pin = json.loads((root / "abeken-v53-daily-version" / f"{day}.json").read_text())
        require(
            payload.get("FORMAL_USE") is True
            and payload.get("RACE_KEY") == race_key
            and payload.get("RESULT_FEEDBACK_TO_MODEL") is False
            and payload.get("P3_INPUT_USED") is False
            and payload.get("WILD_INPUT_USED") is False
            and payload["RESULT"].get("STATUS") == "READY"
            and payload["RESULT"].get("LEGACY45_MORNING_SEED") is True
            and payload["RESULT"].get("LEGACY_45_FALLBACK") is False
            and payload["RESULT"].get("RUNTIME_LOGIC_VERSION") == version
            and pin.get("DAILY_ABEKEN_SHADOW_VERSION") == version
            and public.get("runtime_logic_version") == version
            and public["morning"]["practical_bets"] == decision["BUY_SET"]
            and public["morning"]["buy_count"] == decision["BUY_COUNT"]
            and public["morning"]["main_head"] == decision["MAIN_HEAD"],
            "MORNING_PIN_SEED_RIGHTS_OR_APP_READBACK_MISMATCH",
        )
        print(f"V53_MORNING_LIVE_PROOF=PASS RACE_ID={race_key} "
              f"VERSION={version} SNAPSHOT_SHA256={morning['SNAPSHOT_SHA256']}")
        print(f"APP_V53_MORNING_READBACK=PASS RACE_ID={race_key}")
    except Exception as exc:
        print(f"V53_MORNING_LIVE_PROOF=FAIL RACE_ID={race_key} "
              f"TECHNICAL_BLOCKER={type(exc).__name__}:{exc}")
        return
    print(f"FIRST_LIVE_PRE_RACE_RACE_ID={race_key}")
    if not isinstance(public.get("live"), dict):
        deadline = datetime.fromisoformat(first["deadline_jst"])
        status = "WAITING_T7" if datetime.now(deadline.tzinfo) < deadline else "FAIL"
        print(f"V53_PRE_RACE_LIVE_PROOF={status} RACE_ID={race_key}")
        if status == "FAIL":
            print("TECHNICAL_BLOCKER=FIRST_V53_RACE_MISSING_PRE_RACE_AFTER_DEADLINE")
        return
    try:
        live = store.load_verified_snapshot("predeadline", f"abeken-v53-shadow-live-{race_key.lower()}")
        source = store.load_verified_snapshot("predeadline", f"beforeinfo-{race_key.lower()}")
        live_payload = live["SNAPSHOT"]["PAYLOAD"]
        source_payload = source["SNAPSHOT"]["PAYLOAD"]
        result = live_payload["RESULT"]
        decision = v53_runtime.validate_decision(result["DECISION"])
        morning_bets = set(public["morning"]["practical_bets"])
        live_bets = set(decision["BUY_SET"])
        require(
            source_payload.get("STATUS") == "PREDEADLINE_SOURCE_COMPLETE"
            and source_payload.get("KIND") == "BEFOREINFO"
            and source_payload.get("RACE_KEY") == race_key
            and live_payload.get("FORMAL_USE") is True
            and live_payload.get("MORNING_V53_SNAPSHOT_SHA256") == morning["SNAPSHOT_SHA256"]
            and live_payload.get("BEFOREINFO_SNAPSHOT_SHA256") == source["SNAPSHOT_SHA256"]
            and live_payload.get("P3_INPUT_USED") is False
            and live_payload.get("WILD_INPUT_USED") is False
            and live_payload.get("RESULT_FEEDBACK_TO_MODEL") is False
            and result.get("STATUS") == "READY"
            and result.get("RUNTIME_LOGIC_VERSION") == version
            and result.get("LEGACY_45_FALLBACK") is False
            and result.get("PRE_RACE_DATA_CAPTURED_AT") == source_payload.get("CAPTURED_AT_JST")
            and datetime.fromisoformat(result["PRE_RACE_DATA_CAPTURED_AT"])
                <= datetime.fromisoformat(first["deadline_jst"])
            and public["live"].get("captured_at_jst") == result["PRE_RACE_DATA_CAPTURED_AT"]
            and public["live"]["practical_bets"] == decision["BUY_SET"]
            and public["live"]["buy_count"] == decision["BUY_COUNT"]
            and public["live"]["main_head"] == decision["MAIN_HEAD"]
            and public["live"]["added_bets"] == sorted(live_bets - morning_bets)
            and public["live"]["removed_bets"] == sorted(morning_bets - live_bets),
            "PRE_RACE_OFFICIAL_SOURCE_VERSION_RIGHTS_DIFF_OR_APP_READBACK_MISMATCH",
        )
        print(f"V53_PRE_RACE_LIVE_PROOF=PASS RACE_ID={race_key} "
              f"CAPTURED_AT_JST={result['PRE_RACE_DATA_CAPTURED_AT']} "
              f"SNAPSHOT_SHA256={live['SNAPSHOT_SHA256']}")
        print(f"APP_V53_PRE_RACE_READBACK=PASS RACE_ID={race_key}")
        print("FORMAL_V53_OPERATION_READY=PASS")
    except Exception as exc:
        print(f"V53_PRE_RACE_LIVE_PROOF=FAIL RACE_ID={race_key} "
              f"TECHNICAL_BLOCKER={type(exc).__name__}:{exc}")
if __name__ == "__main__":
    main(*sys.argv[1:])
