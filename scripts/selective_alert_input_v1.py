"""Optional input-evidence sidecar. Reads saved bytes; never runs a predictor.

No candidate selection, notification calls, subscriptions, model imports or
canonical writes. Invoked only after the existing verified Site delivery.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import re
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import urllib.parse
import urllib.request

SCHEMA = "G11_DUAL_ALERT_ODDS_TRANSPORT_V1"
PROBABILITY_SCHEMA = "G11_WILD4_PROBABILITY_TRANSPORT_V1"
PROBABILITY_PROOF_SCHEMA = "G11_WILD4_LOCK_PROBABILITY_PROOF_V1"
ORIGIN = "https://g11-race-lab.higatatsunori2.chatgpt.site"
WIRE_LIMIT = 240_000
RESCUE_FROM_DAY = "2026-10-21"

def rescue_source(source, day, is_gori):
    """Input transport scope only; the Site revalidates its accepted boundary."""
    if day < RESCUE_FROM_DAY:
        return False
    if is_gori:
        return bool(re.fullmatch(r"[a-f0-9]{64}", str(source.get("rescue_boundary_sha256", ""))))
    boundary = source.get("same_day_rescue") or {}
    return (source.get("SNAPSHOT_CLASS") == "SAME_DAY_RESCUE_BOUNDARY" and
            source.get("ACCOUNTING_CLASS") == "TODAY_RESCUE" and
            source.get("CANONICAL_MORNING_PROMOTED") is False and
            source.get("RESEARCH_ELIGIBLE_AS_MORNING") is False and
            boundary.get("immutable") is True and boundary.get("operational_date_jst") == day and
            boundary.get("canonical_morning_promoted") is False and boundary.get("research_eligible") is False and
            boundary.get("same_day_results_used") is False and
            bool(re.fullmatch(r"[a-f0-9]{64}", str(boundary.get("authority_sha256", "")))))

def require(ok, code):
    if not ok:
        raise ValueError(code)

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()

def sha(value):
    return hashlib.sha256(canonical(value)).hexdigest()

def read_official(store_path, race_key):
    # Existing byte/receipt/RAW-reparse/120-combination/window verifier.
    from g11.canonical.official_pipeline import OfficialArtifactStore, CanonicalRunner
    # Preserve symlink rejection in the existing verifier; resolve() would erase it.
    store = OfficialArtifactStore(Path(os.path.abspath(store_path)))
    morning = store.load_verified_snapshot("morning", "official-day-input")
    saved = store.load_verified_snapshot("predeadline", "odds3t-" + race_key)
    odds = CanonicalRunner._validated_odds_only_market(SimpleNamespace(store=store), morning, saved, race_key)
    receipt = saved["SNAPSHOT"]["SOURCE_RECEIPTS"][0]
    require(all(type(v) in (int, float) and math.isfinite(v) and v > 0 for v in odds.values()), "OFFICIAL_ODDS_VALUES")
    return saved, receipt, odds

def make_proof(engine, row, saved, receipt, odds, odds_day=None, packet=None):
    key = row["race_key"]
    bets = row["bets"]
    require(len(bets) == row["bet_count"] and len(set(bets)) == len(bets) and all(b in odds for b in bets), "SAVED_BET_ODDS_COVERAGE")
    if engine == "WILD4":
        require(packet is not None and packet["STANDARD_RACE_KEY"] == key and packet.get("RESULT_DATA_PRESENT") is False,
                "GORE_ODDS_PACKET")
        require(packet["BET_PACKET_HASH"] == sha({k:v for k,v in packet.items() if k != "BET_PACKET_HASH"}), "GORE_PACKET_HASH")
        require(packet["BET_PACKET_HASH"] == row["upstream_packet_sha256"] and
                packet["ODDS_SNAPSHOT_ID"] == saved["SNAPSHOT_SHA256"] == row["odds_snapshot_sha256"] and
                packet["ODDS_RESPONSE_SHA256"] == receipt["SHA256"] and packet["ODDS_120"] == odds and
                datetime.fromisoformat(packet["ODDS_CAPTURED_AT"]) == datetime.fromisoformat(receipt["CAPTURED_AT_JST"]) and
                datetime.fromisoformat(packet["ODDS_CAPTURED_AT"]) <= datetime.fromisoformat(row["locked_at"]), "GORE_LOCK_ODDS_BINDING")
    return {"engine": engine, "race_key": key, "lock_sha256": row["lock_sha256"], "deadline_at": row["deadline_at"],
            "purpose": "BET_DECISION_INPUT" if engine == "WILD4" else "ALERT_EVALUATION_ONLY",
            "source_id": receipt["SOURCE_ID"], "snapshot_sha256": saved["SNAPSHOT_SHA256"], "response_sha256": receipt["SHA256"],
            "captured_at": receipt["CAPTURED_AT_JST"], "host": receipt["HOST"],
            "verification": "BYTE_EXACT_RAW_REPARSE_EXACT_120",
            "values": [{"combination": b, "market_odds": odds[b]} for b in bets],
            "packet_sha256": packet["BET_PACKET_HASH"] if packet else None,
            "odds_day_sha256": odds_day["CONTENT_SHA256"] if odds_day else None}

def read_player_context(store_path, race_key, readback_at, cache):
    # Private runtime owns the official source schema; public relay only routes it.
    from g11.canonical.condition_context_transport_v1 import read_player_context as read
    return read(store_path, race_key, readback_at, cache)

def collect(store_path, source, *, odds_day=None, reader=read_official, context_reader=read_player_context):
    is_gori = source.get("schema") == "G11_WILD4_PUBLIC_PROJECTION_V1"
    day = source["date"] if is_gori else source["operational_date_jst"]
    rescue = rescue_source(source, day, is_gori)
    if is_gori:
        require(odds_day and odds_day.get("RESULT_DATA_PRESENT") is False and
                odds_day["CONTENT_SHA256"] == sha({k:v for k,v in odds_day.items() if k != "CONTENT_SHA256"}), "GORE_ODDS_DAY_HASH")
    packets = {p["STANDARD_RACE_KEY"]: p for p in odds_day["RECORDS"]} if is_gori else {}
    records, held = [], []
    context_cache = {}
    readback_at = datetime.now(timezone.utc).isoformat()
    for race in source["races"]:
        key = race["race_key"] if is_gori else race["key"]
        if is_gori:
            if race.get("classification") != ("LINEAGE" if rescue else "FORMAL"):
                continue
            row = race
        else:
            ab = race.get("abeken_shadow", {}) or {}
            v = ab.get("v53", {}) or {}
            live = v.get("live")
            eligible = (v.get("formal_use") is False and race.get("formal_status") == "EXCLUDED" and
                        race.get("research_eligible") is False and bool(race.get("same_day_rescue_lock"))) if rescue else v.get("formal_use") is True
            if not (ab.get("immutable_prediction_snapshots") is True and eligible and
                    v.get("legacy_45_fallback") is False and isinstance(live, dict) and live.get("buy_count", 0) > 0):
                continue
            row = {"race_key": key, "bets": live["practical_bets"], "bet_count": live["buy_count"],
                   "lock_sha256": live.get("snapshot_sha256"), "deadline_at": race["deadline_jst"], "locked_at": live["locked_at_jst"]}
        try:
            saved, receipt, odds = reader(store_path, key)
            proof = make_proof("WILD4" if is_gori else "ABEKEN_V4", row, saved, receipt, odds, odds_day, packets.get(key))
            if datetime.fromisoformat(readback_at) < datetime.fromisoformat(row["deadline_at"]):
                try:
                    proof["player_context"] = context_reader(store_path, key, readback_at, context_cache)
                except Exception as exc:
                    # Optional context must not hold healthy odds or another engine.
                    code = str(exc)
                    held.append({"race_key": key, "scope": "PLAYER_CONTEXT_ONLY",
                                 "reason": code if code.isupper() and len(code) < 100 else type(exc).__name__})
            records.append(proof)
        except Exception as exc:
            # Reasons only; never dump decrypted input, tokens or stack traces.
            code = str(exc)
            held.append({"race_key": key, "reason": code if code.isupper() and len(code) < 100 else type(exc).__name__})
    return {"schema": SCHEMA, "date": day, "records": records}, held

def post_batch(origin, token, payload):
    require(origin == ORIGIN, "EVIDENCE_ORIGIN")
    raw = canonical(payload)
    require(len(raw) <= WIRE_LIMIT, "INPUT_WIRE_BUDGET")
    url = origin + "/api/g11-feed?" + urllib.parse.urlencode({"view": "selective-alert-input", "date": payload["date"]})
    # Bearer is memory/stdin only, never argv, files or log output.
    with tempfile.TemporaryDirectory(prefix="g11-input-") as temp:
        body, output = Path(temp)/"input.json", Path(temp)/"readback.json"
        body.write_bytes(raw)
        headers = "Authorization: Bearer " + token + "\nContent-Type: application/json\nX-G11-Feed-Sha256: " + hashlib.sha256(raw).hexdigest() + "\n"
        sent = subprocess.run(["curl", "--silent", "--show-error", "--max-time", "25", "-X", "POST", url,
                              "--header", "@-", "--data-binary", "@"+str(body), "--output", str(output), "--write-out", "%{http_code}"],
                              input=headers, capture_output=True, text=True)
        require(sent.returncode == 0 and sent.stdout == "200", "INPUT_DELIVERY_HTTP")
        value = json.loads(output.read_bytes())
        require(value.get("status") == "SAVED", "INPUT_SAVE_NOT_CONFIRMED")
        return value

def deliver_optional(store_path, source, origin=ORIGIN, token=None, odds_day=None, sender=post_batch):
    try:
        payload, held = collect(store_path, source, odds_day=odds_day)
        records = urgent_first(payload["records"], datetime.now(timezone.utc))
        acknowledgements = []
        # Existing update opportunity, bounded chunks; not a new retry/scheduler.
        batch = []
        for record in records:
            candidate = {**payload, "records": batch + [record]}
            if batch and (len(batch) >= 40 or len(canonical(candidate)) > WIRE_LIMIT):
                acknowledgements.append(sender(origin, token or os.environ["G11_SYNC_OIDC"], {**payload, "records": batch}))
                batch = []
            require(len(canonical({**payload, "records": [record]})) <= WIRE_LIMIT, "INPUT_SINGLE_RECORD_BUDGET")
            batch.append(record)
        if batch:
            acknowledgements.append(sender(origin, token or os.environ["G11_SYNC_OIDC"], {**payload, "records": batch}))
        result = {"status": "SAVED" if acknowledgements else "NO_VERIFIED_ODDS", "date": payload["date"],
                  "sources": len(records), "player_context_sources": sum("player_context" in r for r in records),
                  "held": held, "selection_enabled": False, "delivery_enabled": False,
                  "notification_send_count": 0, "last_ack": acknowledgements[-1] if acknowledgements else None}
    except Exception as exc:
        result = {"status": "HELD", "reason": type(exc).__name__, "selection_enabled": False, "delivery_enabled": False, "notification_send_count": 0}
    print("G11_SELECTIVE_INPUT=" + json.dumps(result, ensure_ascii=False, sort_keys=True))
    return result

def urgent_first(records, now):
    """Publish still-open race evidence before historical batches; retain all."""
    def priority(record):
        try:
            deadline = datetime.fromisoformat(record['deadline_at'])
            if deadline.tzinfo is not None and deadline > now:
                return (0, deadline)
        except (KeyError, ValueError, TypeError):
            pass
        return (1, now)
    return sorted(records, key=priority)

def collect_probabilities(ledger, source):
    """Read immutable hash preimages; never infer, calibrate or select a BET."""
    day = source["date"]
    require(source.get("schema") == "G11_WILD4_PUBLIC_PROJECTION_V1", "PROBABILITY_PROJECTION")
    root = Path(os.path.abspath(ledger))
    records, held = [], []
    for row in source["races"]:
        if row.get("classification") != "FORMAL" or row.get("abstain") is True or row.get("bet_count", 0) <= 0:
            continue
        key = row.get("race_key")
        try:
            require(isinstance(key, str) and re.fullmatch(r"\d{8}-\d{2}-(?:0[1-9]|1[0-2])", key)
                    and key[:8] == day.replace("-", ""), "PROBABILITY_RACE_KEY")
            path = root / (key + ".json")
            require(path.is_file() and not path.is_symlink() and path.resolve(strict=True) == path
                    and path.stat().st_size <= 100_000, "PROBABILITY_LOCK_FILE")
            lock = json.loads(path.read_bytes())
            # Same Python canonical hash preimage (including LF) as the existing
            # producer/ledger verifier. Keep floats intact through the wire string.
            raw = canonical({k:v for k,v in lock.items() if k != "CONTENT_SHA256"}) + b"\n"
            require(len(raw) <= 32_768 and hashlib.sha256(raw).hexdigest() ==
                    lock.get("CONTENT_SHA256") == row["lock_sha256"], "PROBABILITY_LOCK_HASH")
            require(lock.get("SCHEMA") == "G11_WILD_BET_FUTURE_OOS_SHADOW_LOCK_V1"
                    and lock.get("STATUS") == "LOCKED_BEFORE_RESULT" and lock.get("OFFICIAL_RESULT_USED") is False
                    and lock.get("STANDARD_RACE_KEY") == key and lock.get("SELECTED_BETS") == row["bets"]
                    and lock.get("BUY_COUNT") == row["bet_count"] and lock.get("CANDIDATE_MODEL_SHA256") == row["model_sha256"]
                    and lock.get("INPUT_PACKET_SHA256") == row["upstream_packet_sha256"]
                    and lock.get("ODDS_SNAPSHOT_SHA256") == row["odds_snapshot_sha256"], "PROBABILITY_LOCK_BINDING")
            require(datetime.fromisoformat(lock["LOCKED_AT"]) == datetime.fromisoformat(row["locked_at"])
                    and datetime.fromisoformat(lock["RACE_DEADLINE_AT"]) == datetime.fromisoformat(row["deadline_at"])
                    and datetime.fromisoformat(lock["LOCKED_AT"]) < datetime.fromisoformat(row["deadline_at"]),
                    "PROBABILITY_LOCK_TIME")
            vector = lock.get("CALIBRATED_PROBABILITIES")
            require(isinstance(vector, dict) and len(vector) == 120
                    and all(re.fullmatch(r"[1-6]-[1-6]-[1-6]", k) and len(set(k.split("-"))) == 3
                            and type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1 for k,v in vector.items())
                    and abs(sum(vector.values()) - 1) <= 1e-9, "PROBABILITY_VECTOR")
            records.append({"schema": PROBABILITY_PROOF_SCHEMA, "source_lock_sha256": row["lock_sha256"],
                            "source_lock_json": raw.decode("utf-8")})
        except Exception as exc:
            code = str(exc)
            held.append({"race_key": key, "reason": code if code.isupper() and len(code) < 100 else type(exc).__name__})
    return {"schema": PROBABILITY_SCHEMA, "date": day, "records": records}, held

def deliver_probabilities_optional(ledger, source, *, token, origin=ORIGIN, sender=post_batch):
    acknowledgements, held, sent = [], [], 0
    try:
        payload, held = collect_probabilities(ledger, source)
        batches, batch = [], []
        for record in payload["records"]:
            if batch and (len(batch) >= 40 or len(canonical({**payload, "records": batch + [record]})) > WIRE_LIMIT):
                batches.append(batch); batch = []
            require(len(canonical({**payload, "records": [record]})) <= WIRE_LIMIT, "PROBABILITY_WIRE_BUDGET")
            batch.append(record)
        if batch: batches.append(batch)
        for batch in batches:
            try:
                ack = sender(origin, token, {**payload, "records": batch})
                acknowledgements.append({k:ack.get(k) for k in ("status", "sources_added", "evaluations_added")})
                sent += len(batch)
                held.extend(ack.get("rejected", []))
            except Exception as exc:
                held.append({"reason": type(exc).__name__, "records": len(batch)})
        result = {"status": "SAVED" if acknowledgements else "HELD" if held else "NO_FORMAL_LOCKS",
                  "date": payload["date"], "sources_sent": sent, "held": held, "acks": acknowledgements}
    except Exception as exc:
        result = {"status": "HELD", "reason": type(exc).__name__}
    print("G11_LOCKED_PROBABILITY_INPUT=" + json.dumps(result, ensure_ascii=False, sort_keys=True))
    return result

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--projection", type=Path, required=True)
    parser.add_argument("--odds-day", type=Path, required=True)
    parser.add_argument("--probability-ledger", type=Path)
    args = parser.parse_args()
    # Reuse the caller's existing GitHub OIDC audience and official projection.
    request = urllib.request.Request(os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"] + "&audience=g11-race-lab",
                                    headers={"Authorization": "Bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]})
    with urllib.request.urlopen(request, timeout=25) as response:
        token = json.load(response)["value"]
    source = json.loads(args.projection.read_bytes())
    deliver_optional(args.store, source, token=token, odds_day=json.loads(args.odds_day.read_bytes()))
    # Existing WILD4 observer already owns this immutable ledger. A missing
    # optional source must never block ordinary projection/odds publication.
    ledger = args.probability_ledger
    if ledger is None and os.environ.get("RUNNER_TEMP"):
        ledger = Path(os.environ["RUNNER_TEMP"]) / "g11-wild4-future-locks"
    if ledger is not None and source.get("schema") == "G11_WILD4_PUBLIC_PROJECTION_V1":
        deliver_probabilities_optional(ledger, source, token=token)

if __name__ == "__main__":
    main()


