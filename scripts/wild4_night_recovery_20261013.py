"""One-date WILD4 settlement recovery. No prediction, training, or source writes.

The public NIGHT and encrypted LOCK ledger are immutable inputs. Only the
existing ledger/settlements contract and existing WILD4 projection are extended.
"""
from __future__ import annotations
import argparse
import ast
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
from types import SimpleNamespace
from typing import Any, Mapping
from urllib.parse import urlsplit
import urllib.request

DAY = "2026-10-13"
MODE = "wild4-recover-20261013"
ORIGIN = "https://g11-race-lab.higatatsunori2.chatgpt.site"
REPO = "tatsuuut/-g11-official-data-relay"
MANIFEST = "7bc952ade97d0fc0d86e03b4af53763d1a65a14795526dbf8ce267986cd03fba"
MODEL = "2d17db06cf9d7f0e73501e84b14f23621a42abb8a4ea1da3ef9d3fd07ac6c412"
CANONICAL = "1fa0607c1ce74b41ccc829790fa1b04bac5cb6b074031413a110616a449ae8e6"
CHECKPOINT = "208e8a84c35444ecfbdfcd49bcd1ba2224e61ca0d33185ba1c9c521e9b7db7a3"
CHECKPOINT_ID = "900da7bc8d5e7d2fefefecfe31c49222ea2a126e112e138900496539f4b21d68"
PUBLIC = "d8f6e525a285a6ea9383abc4bc0ad0db8391f47c7b8236a988f696b5e9dd7e8d"
APP = "b9e0701651a260a6c4ea6f50780828d6554aa198a7c02aef761d696e98c121c3"
PROJECTION = "66e1b3d6d9a6d89f68bada77f91a46c7e443332eabab2667fe5b0a06ffa35745"
ARTIFACT_ID = 11820321418
ARTIFACT_RUN = 38409076971
ARTIFACT_SHA = "sha256:6119413a57a5321e4751eb56bc17814df29a3239262d49a4b93dea8eab391e04"
ARTIFACT_NAME = "g11-wild4-20261013-future-oos-locks-encrypted"
SOURCE_HASHES = {
    "wild_bet_bootstrap_v1.py": "db5ec1921912becbdcad07b52eefc4806afdbcbff1edf7207468cb07e5de7f09",
    "wild_bet_future_oos_v1.py": "c09fa023c91623334646070c7e6afa16a1a73961fcd601b0e52023a4aabd9c81",
    "wild4_official_settlement_v1.py": "79e52752cafb7366a04426c95e21da53812170f0fd9495dc6b32fe71f7e5fd15",
}
SETTLEMENT_FIELDS = ("SCHEMA", "CONTENT_SHA256", "SHADOW_LOCK_SHA256", "OFFICIAL_SOURCE_SHA256",
    "OFFICIAL_CAPTURED_AT", "OFFICIAL_RESULT", "EVALUATION_STATUS", "SHADOW_HIT", "SHADOW_BUY_COUNT",
    "SHADOW_UNIT_100YEN_INVESTMENT", "SHADOW_UNIT_100YEN_PAYOUT", "REFUND_OCCURRED", "EXCLUSION_REASON")

def require(value, code):
    if not value:
        raise RuntimeError("WILD4_RECOVERY:" + code)

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()

def digest(data):
    return hashlib.sha256(data).hexdigest()

def read(path):
    require(path.is_file() and not path.is_symlink(), "REGULAR_FILE:" + path.name)
    return json.loads(path.read_bytes())

def inventory(root):
    result = {}
    for path in root.rglob("*"):
        require(not path.is_symlink(), "SYMLINK")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = digest(path.read_bytes())
    return result

def functions(path, names, environment):
    """Reuse pinned pure functions without importing an inference/training module."""
    require(digest(path.read_bytes()) == SOURCE_HASHES[path.name], "ACCOUNTING_SOURCE_HASH")
    tree = ast.parse(path.read_bytes())
    selected = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in names]
    require({n.name for n in selected} == set(names), "ACCOUNTING_SOURCE_FUNCTIONS")
    scope = {"Any": Any, "Mapping": Mapping, "Path": Path, "datetime": datetime,
             "json": json, "hashlib": hashlib, "os": os, "re": re, **environment}
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *selected], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), scope)
    return SimpleNamespace(**scope)

def accounting(private, settlement):
    root = private / "g11/wild_pack/v1"
    bootstrap = functions(root / "wild_bet_bootstrap_v1.py",
        {"WildBetBootstrapError", "_require", "_canonical_bytes", "sha256_value", "combinations"},
        {"SHA_RE": re.compile(r"[0-9a-f]{64}")})
    future = functions(root / "wild_bet_future_oos_v1.py",
        {"WildBetFutureOOSError", "require", "_time", "settle_shadow_lock"}, {"bootstrap": bootstrap})
    return functions(settlement / "g11/wild_pack/v1/wild4_official_settlement_v1.py",
        {"require", "read_record", "verify_prior", "settle_day"},
        {"bootstrap": bootstrap, "future": future, "KEY": re.compile(r"[0-9]{8}-[0-9]{2}-(?:0[1-9]|1[0-2])\Z"),
         "RESULT_SCHEMA": "G11_WILD_BET_FUTURE_OOS_SHADOW_RESULT_V1",
         "EXCLUSION_SCHEMA": "G11_WILD4_FUTURE_OOS_EXCLUSION_V1",
         "FIELDS": ("標準レースキー", "3連単", "払戻", "返還有無", "レース成立状態", "取得時刻", "公式URL")})

def extract_encrypted(encrypted, destination, work):
    archive = work / (destination.name + ".tgz")
    p = subprocess.run(["gpg", "--batch", "--yes", "--pinentry-mode", "loopback", "--passphrase-fd", "0",
        "--decrypt", "--output", str(archive), str(encrypted)],
        input=(os.environ["STATE_PASSPHRASE"] + "\n").encode(), capture_output=True)
    require(p.returncode == 0, "DECRYPT_FAILED")
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive, "r:gz") as handle:
        members = handle.getmembers()
        require(bool(members), "EMPTY_ARCHIVE")
        for m in members:
            q = Path(m.name)
            require(not q.is_absolute() and ".." not in q.parts and not m.issym() and not m.islnk()
                    and (m.isfile() or m.isdir()), "UNSAFE_ARCHIVE")
        handle.extractall(destination, filter="data")

def api(path):
    req = urllib.request.Request("https://api.github.com/repos/" + REPO + path,
        headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"], "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)

def artifact_guard():
    items = api("/actions/artifacts?name=" + ARTIFACT_NAME + "&per_page=100")["artifacts"]
    current = int(os.environ["GITHUB_RUN_ID"])
    others = [a for a in items if a["name"] == ARTIFACT_NAME and not a["expired"] and a["workflow_run"]["id"] != current]
    require(bool(others), "LOCK_ARTIFACT_MISSING")
    latest = max(others, key=lambda a: a["created_at"])
    require(latest["id"] == ARTIFACT_ID and latest["workflow_run"]["id"] == ARTIFACT_RUN
            and latest["digest"] == ARTIFACT_SHA, "NEWER_OR_CHANGED_LOCK_ARTIFACT")
    require(api("/actions/runs/" + str(ARTIFACT_RUN))["conclusion"] == "success", "LOCK_SOURCE_RUN")

def get_feed(work, app):
    suffix = "?view=v53-final&date=" if app else "?date="
    name = "app" if app else "canonical"
    body, headers = work / (name + "-get.json"), work / (name + "-get.headers")
    run = subprocess.run(["curl", "--silent", "--show-error", "--max-time", "45", "--fail",
        "-D", str(headers), ORIGIN + "/api/g11-feed" + suffix + DAY, "-o", str(body)], capture_output=True)
    require(run.returncode == 0, "PUBLIC_GET_FAILED")
    h = dict(line.split(": ", 1) for line in headers.read_text().lower().splitlines() if ": " in line)
    raw = body.read_bytes()
    require(digest(raw) == h["x-g11-feed-sha256"], "PUBLIC_GET_HASH")
    require(h.get("x-g11-canonical-feed-sha256") == CANONICAL, "PUBLIC_CANONICAL_HASH")
    return json.loads(raw), h, raw

def projection_from_app(app):
    rows = sorted((r["wild_pack"]["wild4"]["goripan"] for r in app["races"]
        if r.get("wild_pack", {}).get("wild4", {}).get("goripan")), key=lambda r: r["race_key"])
    obs = app["goripan"]["observation"]
    p = {"schema": "G11_WILD4_PUBLIC_PROJECTION_V1", "machine_identity": "WILD4", "display_name": "🦍 ゴリぱん",
         "date": DAY, "formal_from": "2026-10-08", "promoted_at": "2026-10-08T00:12:31+09:00",
         "model_sha256": MODEL, "source_manifest_sha256": MANIFEST, "observation": obs, "races": rows,
         "purchase_executed": False, "source_locks_mutated": False, "automatic_candidate_promotion": False}
    life = obs.get("lifecycle")
    if life and life["active"]["identity"] != MODEL:
        p["model_binding"] = life["active"]
    return p

def verified_snapshot(store, name, expected):
    path = store / "snapshots/night" / (name + ".json")
    raw = path.read_bytes()
    require(digest(raw) == expected, "OFFICIAL_SNAPSHOT_HASH:" + name)
    value = read(path)
    require(set(value) == {"SCHEMA", "STAGE", "NAME", "SOURCE_SHA256", "SOURCE_RECEIPTS", "PAYLOAD"}
            and value["STAGE"] == "NIGHT" and value["NAME"] == name, "SNAPSHOT_ENVELOPE")
    receipts = value["SOURCE_RECEIPTS"]
    require(receipts and value["SOURCE_SHA256"] == [r["SHA256"] for r in receipts]
            and len({r["SOURCE_ID"] for r in receipts}) == len(receipts), "SNAPSHOT_RECEIPTS")
    for receipt in receipts:
        source_id = receipt["SOURCE_ID"]
        require(re.fullmatch(r"[a-zA-Z0-9_-]+", source_id), "SOURCE_ID")
        root = store / "responses" / source_id
        require(read(root / "receipt.json") == receipt and receipt["STATUS"] == "HTTP_200_BYTE_EXACT_READBACK"
                and all(urlsplit(receipt[k]).hostname in {"www1.mbrace.or.jp", "www.boatrace.jp"} for k in ("URL", "FINAL_URL")), "OFFICIAL_RECEIPT")
        data = (root / "response.bin").read_bytes()
        require(len(data) == receipt["SIZE_BYTES"] and digest(data) == receipt["SHA256"], "OFFICIAL_RESPONSE_HASH")
    return {"SNAPSHOT": value, "SNAPSHOT_SHA256": expected, "SNAPSHOT_SIZE_BYTES": len(raw)}

def validate_lock_set(ledger, projection, settle):
    manifest = read(ledger / "batch-manifest.json")
    require(manifest["CONTENT_SHA256"] == MANIFEST
            and settle.bootstrap.sha256_value({k:v for k,v in manifest.items() if k != "CONTENT_SHA256"}) == MANIFEST
            and manifest["DATE"] == DAY and manifest["CANDIDATE_MODEL_SHA256"] == MODEL, "FORMAL_MANIFEST")
    keys = sorted(p.stem for p in ledger.glob("*.json") if settle.KEY.fullmatch(p.stem))
    rows = {r["race_key"]: r for r in projection["races"]}
    require(keys == manifest["RACE_KEYS"] == sorted(rows) and len(keys) == 96, "EXACT_96_LOCKS")
    for key in keys:
        lock = settle.read_record(ledger / (key + ".json"), key)
        row = rows[key]
        require(lock["CANDIDATE_MODEL_SHA256"] == row["model_sha256"] == MODEL
                and lock["CONTENT_SHA256"] == row["lock_sha256"]
                and lock["SELECTED_BETS"] == row["bets"]
                and lock["BUY_COUNT"] == row["bet_count"] == len(row["bets"]) > 0
                and lock["LOCKED_AT"] == row["locked_at"]
                and lock["RACE_DEADLINE_AT"] == row["deadline_at"]
                and lock["INPUT_PACKET_SHA256"] == row["upstream_packet_sha256"]
                and lock["ODDS_SNAPSHOT_SHA256"] == row["odds_snapshot_sha256"]
                and row["classification"] == "FORMAL" and not row["abstain"]
                and datetime.fromisoformat(row["locked_at"]) < datetime.fromisoformat(row["deadline_at"]), "FORMAL_LOCK_IDENTITY:" + key)
        require(lock.get("EFFECTIVE_MODEL_IDENTITY") == row.get("effective_model_identity")
                and lock.get("CALIBRATION_SHA256") == row.get("calibration_sha256"), "FORMAL_GENERATION")
    return keys

def result_projection(prior, settled):
    result = deepcopy(prior)
    for row in result["races"]:
        value = settled[row["race_key"]]
        projected = {k: value.get(k) for k in SETTLEMENT_FIELDS}
        require(row["settlement"] in (None, projected), "PUBLIC_SETTLEMENT_CONFLICT")
        row["settlement"] = projected
    return result

def totals_and_join(projection, app):
    rows = {r["key"]: r for r in app["races"]}
    total = {"races": 0, "trifecta_hit": 0, "total_points": 0, "gross_stake": 0,
             "refund_amount": 0, "net_investment": 0, "prize_return": 0, "profit": 0}
    for row in projection["races"]:
        race, result = rows[row["race_key"]], row["settlement"]
        require(result["EVALUATION_STATUS"] == "FIRST_CURRENT_VALID" and not result["REFUND_OCCURRED"], "96R_SETTLEMENT_STATUS")
        official = race["result_meta"]["trifecta_settlements"]
        require(race["result_meta"]["result_status"] == "ESTABLISHED" and len(official) == 1
                and result["OFFICIAL_RESULT"] == race["result_trifecta"] == official[0]["trifecta"], "PUBLIC_OFFICIAL_JOIN")
        hit = result["OFFICIAL_RESULT"] in row["bets"]
        stake, payout = len(row["bets"]) * 100, official[0]["payout"] if hit else 0
        require(result["SHADOW_HIT"] == hit and result["SHADOW_BUY_COUNT"] == row["bet_count"]
                and result["SHADOW_UNIT_100YEN_INVESTMENT"] == stake
                and result["SHADOW_UNIT_100YEN_PAYOUT"] == payout, "FINANCE_JOIN")
        total["races"] += 1; total["trifecta_hit"] += int(hit); total["total_points"] += row["bet_count"]
        total["gross_stake"] += stake; total["net_investment"] += stake
        total["prize_return"] += payout; total["profit"] += payout - stake
    total["roi"] = total["prize_return"] / total["net_investment"]
    return total

def protected_app(feed):
    value = deepcopy(feed)
    for race in value["races"]:
        lock = race.get("wild_pack", {}).get("wild4", {}).get("goripan")
        if lock: lock["settlement"] = None
    final = value["final_accounting"]
    for row in final["by_race"].values(): row["engines"].pop("wild", None)
    final["engines"].pop("wild", None); final["source_counts"].pop("wild", None)
    final.pop("classifications", None); final.pop("wild_bet_generation", None)
    return value

def prepare(args):
    work = args.work; work.mkdir(parents=True, exist_ok=True)
    artifact_guard()
    base, _, raw = get_feed(work, False); require(digest(raw) == PUBLIC, "PUBLIC_NIGHT_CHANGED")
    app, h, raw = get_feed(work, True); require(digest(raw) == APP and h["x-g11-wild4-projection-sha256"] == PROJECTION, "PUBLIC_PROJECTION_CHANGED")
    (work / "app-before.json").write_bytes(raw)
    prior = projection_from_app(app); require(digest(canonical(prior)) == PROJECTION, "EXACT_PRIOR_PROJECTION")
    extract_encrypted(args.state_archive, work / "state", work)
    extract_encrypted(args.lock_archive, work / "restored", work)
    state, ledger = work / "state", work / "restored/g11-wild4-future-locks"
    before_state, before_ledger = inventory(state), inventory(ledger)
    root = state / "daily-runtime" / DAY
    checkpoint = read(root / "night-checkpoint.json")
    require(digest((root / "night-checkpoint.json").read_bytes()) == CHECKPOINT
            and checkpoint["checkpoint_sha256"] == CHECKPOINT_ID
            and checkpoint["feed_sha256"] == CANONICAL
            and digest((root / "night-feed.json").read_bytes()) == CANONICAL, "NIGHT_CANONICAL_BINDING")
    official = verified_snapshot(state / "store", "official-day-results", checkpoint["official_results_sha256"])
    night = verified_snapshot(state / "store", "workbook-ledger-projection", checkpoint["night_projection_sha256"])
    require(official["SNAPSHOT"]["SOURCE_RECEIPTS"][0]["SOURCE_ID"] == "result-k-20261013", "RESULT_DAY_IDENTITY")
    for path, sha in checkpoint["official_source_files"].items():
        require(not Path(path).is_absolute() and ".." not in Path(path).parts
                and digest((state / "store" / path).read_bytes()) == sha, "CHECKPOINT_OFFICIAL_SOURCE")
    settle = accounting(args.private, args.settlement)
    keys = validate_lock_set(ledger, prior, settle)
    previous = settle.verify_prior(ledger)
    require(set(previous).issubset(keys), "PRIOR_SETTLEMENT_SCOPE")
    dry = work / "dry-ledger"; shutil.copytree(ledger, dry)
    dry_result = settle.settle_day(dry, night, DAY, official)
    require(dry_result["total"] == 96 and dry_result["excluded"] == 0, "DRY_96_RESULTS")
    records = settle.verify_prior(dry)
    require(settle.settle_day(dry, night, DAY, official)["new"] == 0, "IDEMPOTENCY")
    projected = result_projection(prior, records)
    totals = totals_and_join(projected, app)
    require(inventory(state) == before_state and inventory(ledger) == before_ledger, "DRY_RUN_SOURCE_MUTATION")
    print("WILD4_RECOVERY_DRY_DIFF=PASS " + json.dumps(totals, sort_keys=True), flush=True)
    # All gates above finish before changing the restored canonical ledger.
    artifact_guard()
    _, _, current_raw = get_feed(work, True); require(digest(current_raw) == APP, "PRE_SAVE_PUBLIC_CONFLICT")
    applied = settle.settle_day(ledger, night, DAY, official)
    require(settle.verify_prior(ledger) == records and applied == dry_result, "SAVE_READBACK")
    after_ledger = inventory(ledger)
    require(all(after_ledger[k] == v for k,v in before_ledger.items())
            and set(after_ledger) - set(before_ledger) == {"settlements/"+k+".json" for k in set(keys)-set(previous)}
            and inventory(state) == before_state, "PROTECTED_SOURCE_MUTATION")
    receipt = {"schema": "G11_WILD4_20261013_SETTLEMENT_RECOVERY_V2", "date": DAY,
        "recorded_at": datetime.now(timezone.utc).isoformat(), "run_id": int(os.environ["GITHUB_RUN_ID"]),
        "canonical_feed_sha256": CANONICAL, "checkpoint_sha256": CHECKPOINT_ID,
        "official_results_sha256": official["SNAPSHOT_SHA256"], "night_projection_sha256": night["SNAPSHOT_SHA256"],
        "source_manifest_sha256": MANIFEST, "race_keys": keys,
        "settlement_hashes": {k:v["CONTENT_SHA256"] for k,v in records.items()}, "totals": totals,
        "new": applied["new"], "source_state_mutated": False, "source_locks_mutated": False,
        "training_executed": False, "prediction_executed": False, "prior_projection_sha256": PROJECTION,
        "projection_sha256": digest(canonical(projected)), "ledger_files": after_ledger}
    (work / "projection.json").write_bytes(canonical(projected))
    (work / "receipt.json").write_bytes(canonical(receipt))
    with (ledger / "settlement-recovery-20261013-receipt.json").open("xb") as out:
        out.write(canonical(receipt) + b"\n")
    (work / "expected-ledger-files.json").write_bytes(canonical(inventory(ledger)))
    print("WILD4_RECOVERY_LOCAL_SAVE_READBACK=PASS new=" + str(applied["new"]), flush=True)

def verify_saved(args):
    extract_encrypted(args.lock_archive, args.work / "saved-readback", args.work)
    require(inventory(args.work / "saved-readback/g11-wild4-future-locks") == read(args.work / "expected-ledger-files.json"), "PERSISTED_ARTIFACT_READBACK")
    print("WILD4_RECOVERY_PERSISTED_HASH_READBACK=PASS", flush=True)

def publish(args):
    work = args.work; receipt = read(work / "receipt.json")
    require(inventory(work / "saved-readback/g11-wild4-future-locks") == read(work / "expected-ledger-files.json"), "PERSISTENCE_GATE")
    artifact_guard()
    _, _, raw = get_feed(work, False); require(digest(raw) == PUBLIC, "PRE_PUBLISH_CANONICAL_CHANGED")
    _, _, raw = get_feed(work, True); require(digest(raw) == APP, "PRE_PUBLISH_PROJECTION_CHANGED")
    data = (work / "projection.json").read_bytes(); require(digest(data) == receipt["projection_sha256"], "PROJECTION_FILE_HASH")
    url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
    req = urllib.request.Request(url + ("&" if "?" in url else "?") + "audience=g11-race-lab",
        headers={"Authorization": "Bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]})
    with urllib.request.urlopen(req, timeout=30) as response: token = json.load(response)["value"]
    headers = "Authorization: Bearer " + token + "\nContent-Type: application/json\nX-G11-Feed-Sha256: " + digest(data) + "\n"
    sent = subprocess.run(["curl", "--silent", "--show-error", "--max-time", "45", "-X", "POST",
        ORIGIN + "/api/g11-wild4-projection", "--header", "@-", "--data-binary", "@"+str(work/"projection.json"),
        "--output", str(work/"publish-response.json"), "--write-out", "%{http_code}"], input=headers, text=True, capture_output=True)
    reply = read(work / "publish-response.json")
    print("WILD4_RECOVERY_HTTP=" + sent.stdout + " " + json.dumps(reply, sort_keys=True), flush=True)
    require(sent.returncode == 0 and sent.stdout == "200" and reply.get("status") == "PASS"
            and reply.get("payload_sha256") == digest(data), "PUBLISH_FAILED_NO_RETRY")
    before = read(work / "app-before.json")
    after, h, _ = get_feed(work, True)
    require(h["x-g11-wild4-projection-sha256"] == digest(data)
            and protected_app(after) == protected_app(before), "PUBLIC_PROTECTED_FIELDS")
    projected = json.loads(data)
    require(projection_from_app(after) == projected, "PUBLIC_96_SETTLEMENT_READBACK")
    totals = totals_and_join(projected, after)
    require(totals == receipt["totals"]
            and all(after["final_accounting"]["engines"]["wild"][k] == v for k,v in totals.items()), "PUBLIC_DAILY_TOTALS")
    for row in projected["races"]:
        final = after["final_accounting"]["by_race"][row["race_key"]]["engines"]["wild"]
        s = row["settlement"]
        require(final["final_bet_snapshot_id"] == row["lock_sha256"] and final["final_bets"] == row["bets"]
                and final["score"]["net_investment"] == s["SHADOW_UNIT_100YEN_INVESTMENT"]
                and final["score"]["prize_return"] == s["SHADOW_UNIT_100YEN_PAYOUT"]
                and final["score"]["trifecta_hit"] == s["SHADOW_HIT"], "PUBLIC_RACE_ACCOUNTING")
    require(after["final_accounting"]["source_counts"]["wild"]["NOT_SCORABLE"] == 36, "PUBLIC_MISSING_36")
    settled_keys = {row["race_key"] for row in projected["races"]}
    for key, row in after["final_accounting"]["by_race"].items():
        if key not in settled_keys:
            require(row["engines"]["wild"] == before["final_accounting"]["by_race"][key]["engines"]["wild"], "MISSING_36_MUTATED")
    _, _, raw = get_feed(work, False); require(digest(raw) == PUBLIC, "CANONICAL_CHANGED_AFTER_PUBLISH")
    print("WILD4_RECOVERY_PUBLIC_GET=PASS " + json.dumps({**totals, "lock_missing":36,
        "projection_sha256":digest(data), "p3_abeken_mutated":False}, sort_keys=True), flush=True)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "verify-saved", "publish"])
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--private", type=Path); parser.add_argument("--settlement", type=Path)
    parser.add_argument("--state-archive", type=Path); parser.add_argument("--lock-archive", type=Path)
    args = parser.parse_args()
    require(os.environ.get("RECOVERY_MODE") == MODE and os.environ.get("RECOVERY_DATE") == DAY
            and os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
            and os.environ.get("GITHUB_REF") == "refs/heads/main"
            and os.environ.get("GITHUB_REPOSITORY") == REPO, "EXPLICIT_RECOVERY_SCOPE")
    {"prepare":prepare, "verify-saved":verify_saved, "publish":publish}[args.action](args)

if __name__ == "__main__":
    main()
