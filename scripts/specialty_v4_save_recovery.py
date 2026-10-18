"""Bounded research-only append to the existing October 17 WILD4 ledger.

No prediction, settlement, training, model binding, or canonical NIGHT writes.
The existing runtime safety gates and the October 13 quarantine are untouched.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile

import hot_delta_transport_v1 as hot
from wild4_night_recovery_20261013 import canonical, digest, read, inventory, extract_encrypted, api

DAY = "2026-10-17"
THROUGH = "2026-10-18"
MODE = "specialty-v4-save"
MARKER = "[specialty-v4-save]"
ORIGIN = "https://g11-race-lab.higatatsunori2.chatgpt.site"
REPO = "tatsuuut/-g11-official-data-relay"
WORKFLOW = ".github/workflows/g11-morning-odds-footprint-4bet-shadow-v1.yml"
MODEL = "2d17db06cf9d7f0e73501e84b14f23621a42abb8a4ea1da3ef9d3fd07ac6c412"
ARTIFACT_ID = 12103411439
ARTIFACT_RUN = 39039069573
ARTIFACT_SHA = "sha256:923572e50113c1e2d06e18ede8550e34630ef20a32dc282fbffd5313c22e8e7b"
ARTIFACT_NAME = "g11-wild4-20261017-future-oos-locks-encrypted"
APP_SHA = "c77e214b4bc033810409b0eea9ef3b7d24cb0ec772aca00a558cd413f0a17364"
PROJECTION_SHA = "c47bd76cfe8faafc37a417669ca00db7c49bf5160e75113dacc6caa1888e64ea"
CANONICAL_SHA = "cc15e58ee3dcdb922560f39833dcf022ea84c9cdec978120accf5852a2837724"
SOURCE_SHA = "f86bbfd24923d4acf103c67b7ed4f52f09825acb52600d0f6d2510c06dda7ddf"
EXCLUDED_AUTHORITY = "226c9c5de084e6e7c0a5607bb1f56821d2125da46cd122d249ad4c45996160aa"
REVISION = "V4_IMMEDIATE_ADOPTION_500R_CUMULATIVE"
LIFECYCLE = "model-registry/lifecycle.json"


def require(ok, code):
    if not ok:
        raise RuntimeError("SPECIALTY_SAVE:" + code)


def scope():
    require(os.environ.get("GITHUB_REPOSITORY") == REPO
        and os.environ.get("GITHUB_REPOSITORY_ID") == "1367703708"
        and os.environ.get("GITHUB_REF") == "refs/heads/main", "OWNER_MAIN_SCOPE")
    event = read(Path(os.environ["GITHUB_EVENT_PATH"]))
    trigger = os.environ.get("GITHUB_EVENT_NAME")
    require(trigger == "push" and MARKER in event.get("head_commit", {}).get("message", "")
        or trigger == "workflow_dispatch" and event.get("inputs", {}).get("mode") == MODE
        and event["inputs"].get("operational_date") == THROUGH, "EXPLICIT_RESEARCH_ONLY_SCOPE")
    require(datetime.now(timezone(timedelta(hours=9))).date().isoformat() == THROUGH,
        "ONE_DAY_AUTHORITY_EXPIRED")


def artifact_guard():
    current = int(os.environ["GITHUB_RUN_ID"])
    items = api("/actions/artifacts?name=" + ARTIFACT_NAME + "&per_page=100")["artifacts"]
    others = [a for a in items if a["name"] == ARTIFACT_NAME and not a["expired"]
        and a["workflow_run"]["id"] != current]
    require(bool(others), "SOURCE_ARTIFACT_MISSING")
    latest = max(others, key=lambda a: (a["created_at"], a["id"]))
    require(latest["id"] == ARTIFACT_ID and latest["digest"] == ARTIFACT_SHA
        and latest["workflow_run"]["id"] == ARTIFACT_RUN
        and latest["workflow_run"]["repository_id"] == 1367703708
        and latest["workflow_run"]["head_repository_id"] == 1367703708, "SOURCE_ARTIFACT_CHANGED")
    run = api("/actions/runs/" + str(ARTIFACT_RUN))
    require(run["conclusion"] == "success" and run["path"] == WORKFLOW
        and run["head_branch"] == "main", "SOURCE_RUN_AUTHORITY")
    # Never branch the registry behind a newly saved current-day model state.
    today = "g11-wild4-20261018-future-oos-locks-encrypted"
    require(not any(a["name"] == today and not a["expired"] for a in
        api("/actions/artifacts?name=" + today + "&per_page=100")["artifacts"]), "NEWER_DAY_LEDGER_EXISTS")


def download_source(work):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    req = urllib.request.Request("https://api.github.com/repos/" + REPO + "/actions/artifacts/"
        + str(ARTIFACT_ID) + "/zip", headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"],
        "Accept": "application/vnd.github+json"})
    try:
        urllib.request.build_opener(NoRedirect).open(req, timeout=30)
        raise RuntimeError("ARTIFACT_REDIRECT_REQUIRED")
    except urllib.error.HTTPError as exc:
        require(exc.code in (301, 302, 303, 307, 308), "ARTIFACT_HTTP")
        location = exc.headers["Location"]
    require(location.startswith("https://"), "ARTIFACT_SCHEME")
    # The signed artifact host never receives the GitHub Authorization header.
    with urllib.request.urlopen(location, timeout=45) as response:
        raw = response.read(16 * 1024 * 1024 + 1)
    require(len(raw) <= 16 * 1024 * 1024 and "sha256:" + digest(raw) == ARTIFACT_SHA,
        "ARTIFACT_DIGEST")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        require(archive.namelist() == ["future-locks.tgz.gpg"], "ARTIFACT_SHAPE")
        data = archive.read("future-locks.tgz.gpg")
    target = work / "source.gpg"
    target.write_bytes(data)
    extract_encrypted(target, work / "restored", work)
    return work / "restored/g11-wild4-future-locks"


def get(suffix):
    Path(".relay-output").mkdir(exist_ok=True)
    value, headers, raw = hot.fetch(ORIGIN + "/api/g11-feed?" + suffix)
    require(len(raw) <= 8 * 1024 * 1024 and not value.get("TEST_ONLY"), "PUBLIC_INPUT")
    return value, headers, raw


def projection_from_app(app):
    obs = app["goripan"]["observation"]
    rows = sorted((r["wild_pack"]["wild4"]["goripan"] for r in app["races"]
        if r.get("wild_pack", {}).get("wild4", {}).get("goripan")), key=lambda r: r["race_key"])
    require(app["operational_date_jst"] == DAY and app["stage"] == "NIGHT"
        and app["counts"]["results"] == app["counts"]["races"] == 168
        and app["goripan"]["status"] == "VERIFIED_CANONICAL_PROJECTION", "NORMAL_NIGHT_REQUIRED")
    return {"schema": "G11_WILD4_PUBLIC_PROJECTION_V1", "machine_identity": "WILD4",
        "display_name": "🦍 ゴリぱん", "date": DAY, "formal_from": "2026-10-08",
        "promoted_at": "2026-10-08T00:12:31+09:00", "model_sha256": obs["model_sha256"],
        "source_manifest_sha256": obs["source_manifest_sha256"], "observation": deepcopy(obs),
        "races": rows, "purchase_executed": False, "source_locks_mutated": False,
        "automatic_candidate_promotion": False}


def protected_state(value):
    value = deepcopy(value)
    value.pop("specialty", None); value.pop("CONTENT_SHA256", None)
    for record in value.get("generations", {}).values():
        record.pop("specialty_evidence", None); record.pop("specialty_updates", None)
    return value


def protected_public(value):
    value = deepcopy(value)
    life = value["goripan"]["observation"]["lifecycle"]
    life.pop("specialty", None); life.pop("state_sha256", None)
    return value


def validate_difference(before_files, after_files, before, after):
    require(set(before_files) == set(after_files), "LEDGER_FILE_SET_CHANGED")
    require(all(after_files[k] == v for k, v in before_files.items() if k != LIFECYCLE),
        "LOCK_SETTLEMENT_MODEL_BYTES_CHANGED")
    require(protected_state(before) == protected_state(after), "MODEL_TRAINING_STATE_CHANGED")
    for engine, value in before.get("specialty", {}).get("engines", {}).items():
        for gen, record in value.get("generations", {}).items():
            saved = after["specialty"]["engines"][engine]["generations"][gen]
            for key, version in record.get("adoptions", {}).items():
                require(saved["adoptions"][key] == version, "PRIOR_ADOPTION_CHANGED")


def prepare(args):
    work = args.work; work.mkdir(parents=True, exist_ok=False)
    artifact_guard()
    app, headers, raw = get("view=v53-final&date=" + DAY)
    require(digest(raw) == APP_SHA and headers.get("x-g11-canonical-feed-sha256") == CANONICAL_SHA
        and headers.get("x-g11-source-feed-sha256") == SOURCE_SHA, "SOURCE_NIGHT_CHANGED")
    projection = projection_from_app(app)
    require(digest(canonical(projection)) == headers.get("x-g11-wild4-projection-sha256")
        == PROJECTION_SHA, "EXACT_SAVED_PROJECTION")
    (work / "app-before.json").write_bytes(raw)
    evidence, _, evidence_raw = get("view=goripan-generation&specialty_revision=" + REVISION
        + "&generation=" + MODEL + "&through=" + THROUGH)
    sources = {s["date"]: s for s in evidence["sources"]}
    require(evidence["through"] == THROUGH and sources[DAY]["projection_sha256"] == PROJECTION_SHA
        and sources[DAY]["feed_sha256"] == SOURCE_SHA, "EVIDENCE_SOURCE_IDENTITY")
    require(sources[THROUGH].get("research_excluded") is True
        and sources[THROUGH].get("authority_sha256") == EXCLUDED_AUTHORITY
        and sources[THROUGH].get("projection_sha256") is None, "EXCLUDED_DAY_AUTHORITY")
    engines = evidence["specialty"]["engines"]
    require(engines["ABEKEN_V4"]["valid_races"] == 1156
        and engines["ABEKEN_V4"]["evaluation_ready"] is True
        and engines["WILD4"]["valid_races"] == 1137
        and engines["WILD4"]["evaluation_ready"] is False
        and "EARLIER_RESULTS_PENDING" in engines["WILD4"]["hold_reasons"], "AUDITED_COHORT_CHANGED")
    (work / "evidence.json").write_bytes(evidence_raw)
    ledger = download_source(work)
    sys.path.insert(0, str(args.private))
    from g11.wild_pack.v1 import wild4_lifecycle_v1 as life
    before_files = inventory(ledger); before = life.read(life.state_path(ledger))
    require(before["authority"] == life.AUTHORITY and before["active"]["identity"] == MODEL
        and before.get("last_completed_night") == DAY, "LIFECYCLE_AUTHORITY")
    pin = life.read(ledger / "day-model-binding.json")
    require(pin["date"] == DAY and pin["authority"] == life.AUTHORITY
        and pin["model"] == before["active"]
        and life.model_info(ledger / "model-registry/models" / MODEL) == pin["model"], "MODEL_IDENTITY")
    require(life.public_status(ledger) == projection["observation"]["lifecycle"], "CANONICAL_PUBLIC_LIFECYCLE")
    manifest = life.read(ledger / "batch-manifest.json")
    require(manifest["CONTENT_SHA256"] == projection["source_manifest_sha256"], "MANIFEST_IDENTITY")
    for row in projection["races"]:
        lock = life.read(ledger / (row["race_key"] + ".json"))
        require(lock["CONTENT_SHA256"] == row["lock_sha256"]
            and lock["SELECTED_BETS"] == row["bets"]
            and lock["LOCKED_AT"] == row["locked_at"], "LOCK_IDENTITY")
    now = datetime.now(timezone.utc)
    after = life.refresh_specialty_v4(ledger, before, evidence, now=now)
    after_files = inventory(ledger)
    validate_difference(before_files, after_files, before, after)
    ab = after["specialty"]["engines"]["ABEKEN_V4"]
    adoption = ab["generations"][ab["current_generation"]]["adoptions"]["v4:initial"]
    require(adoption["sample"] == 1156 and adoption["saved_at"] == now.isoformat()
        and adoption["source_sha256"] == evidence["evidence_sha256"], "INITIAL_SAVE_RECEIPT")
    require(not after["generations"][MODEL].get("specialty_updates"), "UNRESOLVED_96_ADOPTION")
    # Replay the same inputs/time only in this isolated copy, before durable save.
    require(life.refresh_specialty_v4(ledger, after, evidence, now=now) == after
        and inventory(ledger) == after_files, "IDEMPOTENT_READBACK")
    new_life = life.public_status(ledger)
    for key in set(new_life) | set(projection["observation"]["lifecycle"]):
        if key not in {"specialty", "state_sha256"}:
            require(new_life.get(key) == projection["observation"]["lifecycle"].get(key),
                "PUBLIC_MODEL_STATE_CHANGED:" + key)
    projection["observation"]["lifecycle"] = new_life
    data = canonical(projection); require(len(data) <= 1_000_000, "EXISTING_PUBLISH_BUDGET")
    (work / "projection.json").write_bytes(data)
    (work / "expected-ledger-files.json").write_bytes(canonical(after_files))
    receipt = {"project": "G11_DUAL_ENGINE_SPECIALTY_1000R_AUTO_UPDATE_V1", "revision": REVISION,
        "source_artifact": ARTIFACT_ID, "source_run": ARTIFACT_RUN, "source_artifact_digest": ARTIFACT_SHA,
        "source_day": DAY, "through": THROUGH, "saved_at": now.isoformat(),
        "abeken_generation": ab["current_generation"], "abeken_valid": 1156, "wild4_valid": 1137,
        "wild4_hold": "EARLIER_RESULTS_PENDING", "excluded_20261018_addition": 0,
        "adoption_sha256": adoption["CONTENT_SHA256"], "evidence_sha256": evidence["evidence_sha256"],
        "lifecycle_before": before["CONTENT_SHA256"], "lifecycle_after": after["CONTENT_SHA256"],
        "projection_sha256": digest(data), "protected_files": len(after_files) - 1,
        "protected_files_sha256": digest(canonical({k:v for k,v in after_files.items() if k != LIFECYCLE})),
        "protected_state_sha256": digest(canonical(protected_state(after))),
        "model_prediction_settlement_mutated": False, "idempotency": "PASS"}
    (work / "receipt.json").write_bytes(canonical(receipt))
    print("SPECIALTY_LOCAL_RECEIPT=" + json.dumps(receipt, sort_keys=True), flush=True)


def verify_saved(args):
    extract_encrypted(args.lock_archive, args.work / "saved-readback", args.work)
    require(inventory(args.work / "saved-readback/g11-wild4-future-locks")
        == read(args.work / "expected-ledger-files.json"), "PERSISTED_ARTIFACT_READBACK")
    print("SPECIALTY_PERSISTED_ARTIFACT_READBACK=PASS", flush=True)


def publish(args):
    work = args.work; receipt = read(work / "receipt.json")
    require(inventory(work / "saved-readback/g11-wild4-future-locks")
        == read(work / "expected-ledger-files.json"), "PERSISTENCE_GATE")
    artifact_guard()
    before, headers, raw = get("view=v53-final&date=" + DAY)
    require(digest(raw) == APP_SHA and headers.get("x-g11-canonical-feed-sha256") == CANONICAL_SHA,
        "PRE_PUBLISH_CONCURRENT_CHANGE")
    data = (work / "projection.json").read_bytes()
    require(digest(data) == receipt["projection_sha256"], "PROJECTION_FILE_HASH")
    url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
    request = urllib.request.Request(url + ("&" if "?" in url else "?") + "audience=g11-race-lab",
        headers={"Authorization": "Bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]})
    with urllib.request.urlopen(request, timeout=30) as response: token = json.load(response)["value"]
    headers = "Authorization: Bearer " + token + "\nContent-Type: application/json\nX-G11-Feed-Sha256: " + digest(data) + "\n"
    sent = subprocess.run(["curl", "--silent", "--show-error", "--max-time", "45", "-X", "POST",
        ORIGIN + "/api/g11-wild4-projection", "--header", "@-", "--data-binary", "@" + str(work / "projection.json"),
        "--output", str(work / "publish-response.json"), "--write-out", "%{http_code}"],
        input=headers, text=True, capture_output=True)
    reply = read(work / "publish-response.json")
    require(sent.returncode == 0 and sent.stdout == "200" and reply.get("status") == "PASS"
        and reply.get("payload_sha256") == digest(data), "PUBLISH_FAILED_NO_BLIND_RETRY")
    after, h, _ = get("view=v53-final&date=" + DAY)
    require(h.get("x-g11-wild4-projection-sha256") == digest(data)
        and h.get("x-g11-canonical-feed-sha256") == CANONICAL_SHA
        and h.get("x-g11-source-feed-sha256") == SOURCE_SHA
        and protected_public(after) == protected_public(before)
        and projection_from_app(after) == json.loads(data), "PUBLIC_READBACK_OR_PROTECTED_FIELDS")
    print("SPECIALTY_PUBLIC_GET_READBACK=PASS " + json.dumps(receipt, sort_keys=True), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "verify-saved", "publish"])
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--private", type=Path)
    parser.add_argument("--lock-archive", type=Path)
    args = parser.parse_args(); scope()
    {"prepare": prepare, "verify-saved": verify_saved, "publish": publish}[args.action](args)


if __name__ == "__main__":
    main()
