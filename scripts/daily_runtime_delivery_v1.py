"""Daily transport adapter: locked feeds in, verified Site receipts out.

No acquisition, prediction or settlement is executed by delivery.
NIGHT retries use exactly the immutable checkpoint bytes and transaction plan.
"""
from __future__ import annotations
from copy import deepcopy
import argparse
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
import subprocess
import tempfile
import time
import sys

from daily_runtime_bootstrap_v1 import retained_module
import hot_delta_transport_v1 as hot

ORIGIN = "https://g11-race-lab.higatatsunori2.chatgpt.site"
MAX_ATTEMPTS = 3
N13_DAY = "2026-10-13"
N13_FEED_SHA = "1fa0607c1ce74b41ccc829790fa1b04bac5cb6b074031413a110616a449ae8e6"
N13_CHECKPOINT_SHA = "900da7bc8d5e7d2fefefecfe31c49222ea2a126e112e138900496539f4b21d68"
N13_CHECKPOINT_FILE_SHA = "208e8a84c35444ecfbdfcd49bcd1ba2224e61ca0d33185ba1c9c521e9b7db7a3"
N13_CREATED = "2026-10-14T02:20:52.268957+09:00"


def n13_transport_only():
    return os.environ.get("G11_N13T") == "true"


def check_saved_n13(implementation, state, feed_path):
    root = implementation.day_root(state, N13_DAY)
    # Historical transport is bound to the already verified revision bytes,
    # not mutable current-day RAW pointers. Do not invoke NIGHT or settlement.
    checkpoint_path = root / "night-checkpoint.json"
    checkpoint = implementation.json_file(checkpoint_path)
    require(hot.hash_bytes(checkpoint_path.read_bytes()) == N13_CHECKPOINT_FILE_SHA
            and checkpoint["checkpoint_sha256"] == N13_CHECKPOINT_SHA
            and checkpoint["feed_sha256"] == N13_FEED_SHA
            and hot.hash_bytes((root / "night-feed.json").read_bytes()) == N13_FEED_SHA
            and hot.hash_bytes(feed_path.read_bytes()) == N13_FEED_SHA,
            "N13_SAVED_CANONICAL_HASH")
    return checkpoint


def emit_saved_n13(state, output):
    require(n13_transport_only(), "N13_TRANSPORT_SCOPE")
    implementation = retained_module(state, N13_DAY)
    root = implementation.day_root(state, N13_DAY)
    checkpoint = check_saved_n13(implementation, state, root / "night-feed.json")
    # Byte copy only: no private NIGHT runner, settlement, or recovery import.
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes((root / "night-feed.json").read_bytes())
    check_saved_n13(implementation, state, output)
    saved_n13_plan(root, implementation)
    print(json.dumps({"STATUS": "SAVED_NIGHT_TRANSPORT_ONLY", "PHASE": "NIGHT",
        "OPERATIONAL_DATE_JST": N13_DAY, "FEED_EMITTED": True,
        "CHECKPOINT_SHA256": checkpoint["checkpoint_sha256"],
        "CANONICAL_FEED_SHA256": N13_FEED_SHA, "PREDICTION_RECALCULATION": 0,
        "SETTLEMENT_EXECUTIONS": 0}, sort_keys=True))


def seconds_only_n13_plan(plan):
    """Keep the failed plan and all accounting; change wire timestamps only."""
    previous = deepcopy(plan)
    manifest = previous["manifest"]
    require(previous["checkpoint_sha256"] == N13_CHECKPOINT_SHA
            and manifest["CANONICAL_FEED_SHA256"] == N13_FEED_SHA
            and manifest["CHECKPOINT_SHA256"] == N13_CHECKPOINT_SHA
            and manifest["OPERATIONAL_DATE"] == N13_DAY
            and manifest["PATCH_CREATED_AT"] == N13_CREATED, "N13_PLAN_SCOPE")
    chunks = previous["chunks"]
    top, races = {}, []
    def merge(base, patch):
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                merge(base[key], value)
            else:
                base[key] = deepcopy(value)
    for index, chunk in enumerate(chunks):
        require(chunk["CHUNK_INDEX"] == index and chunk["CHUNK_SHA256"] ==
                hot.site_hash({"TOP_LEVEL": chunk["TOP_LEVEL"], "RACES": chunk["RACES"]}),
                "N13_PRIOR_CHUNK_HASH")
        merge(top, chunk["TOP_LEVEL"])
        races.extend(chunk["RACES"])
    require(top["generated_at_jst"] == N13_CREATED and hot.site_hash(top) ==
            manifest["EXPECTED_TOP_LEVEL_SHA"], "N13_PRIOR_TOP_HASH")
    stamp = datetime.fromisoformat(N13_CREATED).isoformat(timespec="seconds")
    top["generated_at_jst"] = stamp
    for chunk in chunks:
        if "generated_at_jst" in chunk["TOP_LEVEL"]:
            chunk["TOP_LEVEL"]["generated_at_jst"] = stamp
        chunk["CHUNK_SHA256"] = hot.site_hash({"TOP_LEVEL": chunk["TOP_LEVEL"], "RACES": chunk["RACES"]})
    manifest["PATCH_CREATED_AT"] = stamp
    manifest["PATCH_SHA256"] = hot.hash_bytes(hot.compact({"stage": "NIGHT", "day": N13_DAY,
                                                         "top": top, "races": races}))
    manifest["EXPECTED_TOP_LEVEL_SHA"] = hot.site_hash(top)
    manifest["CHUNK_SHA256"] = [c["CHUNK_SHA256"] for c in chunks]
    manifest["NIGHT_TRANSACTION_ID"] = hot.hash_bytes(hot.compact([N13_DAY,
        manifest["BASE_FEED_SHA"], manifest["SOURCE_NIGHT_SHA"], manifest["CHUNK_SHA256"]]))
    manifest.pop("MANIFEST_SHA256")
    manifest["MANIFEST_SHA256"] = hot.hash_bytes(hot.compact(manifest))
    for chunk in chunks:
        chunk["NIGHT_TRANSACTION_ID"] = manifest["NIGHT_TRANSACTION_ID"]
        chunk["MANIFEST_SHA256"] = manifest["MANIFEST_SHA256"]
    result = {"checkpoint_sha256": N13_CHECKPOINT_SHA, "manifest": manifest, "chunks": chunks}
    result["plan_sha256"] = hot.hash_bytes(json.dumps(result, ensure_ascii=False, sort_keys=True,
                                                   separators=(",", ":"), allow_nan=False).encode())
    return result


def saved_n13_plan(root, implementation):
    prior_path = root / "night-transport-plan.json"
    prior = implementation.json_file(prior_path)
    unsigned = dict(prior)
    expected = unsigned.pop("plan_sha256", None)
    require(expected == implementation.digest(implementation.canonical(unsigned)), "N13_PRIOR_PLAN_HASH")
    fixed = seconds_only_n13_plan(prior)
    path = root / "night-transport-plan-jst-seconds.json"
    implementation.immutable(path, hot.compact(fixed))
    require(implementation.json_file(path) == fixed, "N13_TRANSPORT_PLAN_READBACK")
    return path


def require(value, code):
    if not value:
        raise RuntimeError(code)


def identity(feed, authority, stage):
    binding = feed.get("daily_runtime", {})
    expected = {"schema": "G11_DAILY_RUNTIME_AUTHORITY_V1",
                "operational_date_jst": authority["operational_date_jst"],
                "authority_sha256": authority["authority_sha256"],
                "runtime_source_sha": authority["runtime_source_sha"],
                "phase": stage, "immutable": True,
                **{k: authority[k] for k in ("p3", "abeken_v53", "wild", "true_ai")}}
    require(binding == expected and feed.get("operational_date_jst") == authority["operational_date_jst"]
            and feed.get("stage") == stage and feed.get("status") == "PASS"
            and feed.get("source", {}).get("github_sha") == authority["runtime_source_sha"],
            "DAILY_SITE_AUTHORITY_MISMATCH")


def utf8_night_plan(plan):
    """Correct wire hashes only; leave saved predictions, results and finance intact."""
    result = deepcopy(plan)
    manifest, chunks = result['manifest'], result['chunks']
    require(len(chunks) == manifest['CHUNK_COUNT'], 'NIGHT_PLAN_CHUNK_COUNT')
    top, races = {}, []
    def merge(base, patch):
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                merge(base[key], value)
            else:
                base[key] = deepcopy(value)
    for index, chunk in enumerate(chunks):
        require(chunk['CHUNK_INDEX'] == index and chunk['CHUNK_COUNT'] == len(chunks)
                and chunk['OPERATIONAL_DATE'] == manifest['OPERATIONAL_DATE']
                and chunk['NIGHT_TRANSACTION_ID'] == manifest['NIGHT_TRANSACTION_ID']
                and chunk['MANIFEST_SHA256'] == manifest['MANIFEST_SHA256']
                and chunk['SOURCE_NIGHT_SHA'] == manifest['SOURCE_NIGHT_SHA']
                and chunk['RACE_KEYS'] == [r['RACE_KEY'] for r in chunk['RACES']],
                'NIGHT_PLAN_CHUNK_BINDING')
        merge(top, chunk['TOP_LEVEL'])
        races.extend(chunk['RACES'])
        chunk['CHUNK_SHA256'] = hot.site_hash({'TOP_LEVEL': chunk['TOP_LEVEL'], 'RACES': chunk['RACES']})
    require(hot.hash_bytes(hot.compact({'stage': 'NIGHT', 'day': manifest['OPERATIONAL_DATE'],
            'top': top, 'races': races})) == manifest['PATCH_SHA256'], 'NIGHT_PLAN_PAYLOAD_HASH_MISMATCH')
    manifest['CHUNK_SHA256'] = [chunk['CHUNK_SHA256'] for chunk in chunks]
    manifest['EXPECTED_TOP_LEVEL_SHA'] = hot.site_hash(top)
    manifest['EXPECTED_FINAL_ACCOUNTING_SHA'] = hot.site_hash(top['final_accounting'])
    manifest['EXPECTED_FINANCE_SHA'] = hot.site_hash([
        [row['RACE_KEY'], row['CHANGED_FIELDS'].get('research_finance')] for row in races])
    manifest['NIGHT_TRANSACTION_ID'] = hot.hash_bytes(hot.compact([
        manifest['OPERATIONAL_DATE'], manifest['BASE_FEED_SHA'], manifest['SOURCE_NIGHT_SHA'],
        manifest['CHUNK_SHA256']]))
    manifest.pop('MANIFEST_SHA256')
    manifest['MANIFEST_SHA256'] = hot.hash_bytes(hot.compact(manifest))
    for chunk in chunks:
        chunk['NIGHT_TRANSACTION_ID'] = manifest['NIGHT_TRANSACTION_ID']
        chunk['MANIFEST_SHA256'] = manifest['MANIFEST_SHA256']
    result.pop('plan_sha256', None)
    result['plan_sha256'] = hot.hash_bytes(json.dumps(result, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode())
    return result


def verified_utf8_plan(root, plan, implementation):
    fixed = utf8_night_plan(plan)
    if fixed == plan:
        return plan
    path = root / 'night-transport-plan-utf8-v1.json'
    implementation.immutable(path, hot.compact(fixed))
    require(implementation.json_file(path) == fixed, 'NIGHT_UTF8_PLAN_READBACK')
    print('G11_NIGHT_UTF8_WIRE_HASH_REPAIRED=PASS CANONICAL_PAYLOAD_CHANGED=0')
    return fixed


def published_lock_night_20261031(feed, accepted, checkpoint):
    """Bind today's result display to the original published LOCK, in memory only.

    The incident has exactly five identity/time differences and identical bets.
    This is not a canonical repair or permission to replace any prediction.
    """
    if feed.get('operational_date_jst') != '2026-10-31':
        return feed, None
    require(checkpoint['feed_sha256'] ==
            '18447a8b1489d99594d0e7ee37667e0b930db64992d34a5e6e5651fbabc08d6b'
            and checkpoint['authority_sha256'] ==
            '48aae10804d6906d6834d11ec09ce59bca05e6b43a132887dfe0a8eeb3b7f66c'
            and feed['stage'] == 'NIGHT', 'N1031_CANONICAL_SCOPE')
    key = '20261031-09-03'
    current = next(r for r in feed['races'] if r['key'] == key)
    prior = next(r for r in accepted['races'] if r['key'] == key)
    old = hot.project('abeken_shadow', prior['abeken_shadow'])
    new = hot.project('abeken_shadow', current['abeken_shadow'])
    paths = [('v53', 'live', field) for field in
             ('captured_at_jst', 'locked_at_jst', 'snapshot_sha256')]
    paths += [('live', field) for field in
              ('feature_asof_time_jst', 'prediction_lock_time_jst')]
    normalized = deepcopy(new)
    for path in paths:
        left, right = normalized, old
        for part in path[:-1]:
            left, right = left[part], right[part]
        require(path[-1] in left and path[-1] in right, 'N1031_LOCK_METADATA_MISSING')
        left[path[-1]] = right[path[-1]]
    require(normalized == old, 'N1031_PREDICTION_CONTENT_CONFLICT')
    previous = old['v53']['live']
    saved = new['v53']['live']
    require(previous['snapshot_sha256'] ==
            '8ec81787e5f75441efcb5fc85bb66412ef548f69277f03580c32c90d5c5027fe'
            and previous['locked_at_jst'] == previous['captured_at_jst'] ==
            '2026-10-31T11:18:45+09:00'
            and old['live']['feature_asof_time_jst'] == old['live']['prediction_lock_time_jst'] ==
            previous['locked_at_jst']
            and current['deadline_jst'] == prior['deadline_jst'] == '2026-10-31T11:25:00+09:00',
            'N1031_PUBLISHED_LOCK_IDENTITY')
    entry = feed['final_accounting']['by_race'][key]['engines']['abeken']
    require(entry['final_bet_source'] == 'PRE_RACE'
            and entry['final_bets'] == saved['practical_bets'] == previous['practical_bets']
            and entry['final_head'] == saved['main_head'] == previous['main_head']
            and entry['final_bet_snapshot_id'] == saved['snapshot_sha256']
            and entry['final_bet_locked_at_jst'] == saved['locked_at_jst'],
            'N1031_FINAL_LOCK_AUTHORITY')
    wire = deepcopy(feed)
    target = next(r for r in wire['races'] if r['key'] == key)
    target['abeken_shadow']['v53']['live'] = deepcopy(previous)
    target['abeken_shadow']['live'] = deepcopy(old['live'])
    final = wire['final_accounting']['by_race'][key]['engines']['abeken']
    final['final_bet_snapshot_id'] = previous['snapshot_sha256']
    final['final_bet_locked_at_jst'] = previous['locked_at_jst']
    proof = {'schema': 'G11_N1031_PUBLISHED_LOCK_RESULT_JOIN_V1', 'race_key': key,
             'canonical_feed_sha256': checkpoint['feed_sha256'],
             'canonical_snapshot_sha256': saved['snapshot_sha256'],
             'published_snapshot_sha256': previous['snapshot_sha256'],
             'prediction_content_changed': False, 'settlement_changed': False,
             'canonical_files_changed': False,
             'wire_reference_paths': ['abeken_shadow.' + '.'.join(p) for p in paths] +
                 ['final_accounting.by_race.' + key + '.engines.abeken.' + p for p in
                  ('final_bet_snapshot_id', 'final_bet_locked_at_jst')]}
    return wire, proof


def published_lock_night_plan(root, prior, wire, accepted, checkpoint, proof, implementation):
    """Save a separate transport projection; never modify the failed plan."""
    research = {}
    def merge(base, patch):
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                merge(base[key], value)
            else:
                base[key] = deepcopy(value)
    for chunk in prior['chunks']:
        if 'deme_discovery' in chunk['TOP_LEVEL']:
            merge(research, chunk['TOP_LEVEL']['deme_discovery'])
    delta = hot.create_delta(wire, accepted, prior['manifest']['BASE_FEED_SHA'], research or None)
    manifest, chunks = hot.night_chunks(delta, wire, checkpoint)
    for field in ('BASE_FEED_SHA', 'CANONICAL_FEED_SHA256', 'CHECKPOINT_SHA256',
                  'DAILY_AUTHORITY_SHA256', 'SOURCE_NIGHT_SHA', 'PATCH_SEQUENCE',
                  'PATCH_CREATED_AT', 'EXPECTED_RACES', 'EXPECTED_RESULTS', 'EXPECTED_PENDING'):
        require(manifest[field] == prior['manifest'][field], 'N1031_TRANSPORT_BINDING:' + field)
    plan = {'checkpoint_sha256': checkpoint['checkpoint_sha256'],
            'manifest': manifest, 'chunks': chunks, 'published_lock_result_join': proof}
    plan['plan_sha256'] = implementation.digest(implementation.canonical(plan))
    path = root / 'night-transport-plan-published-lock-20261031.json'
    implementation.immutable(path, hot.compact(plan))
    require(implementation.json_file(path) == plan, 'N1031_PLAN_READBACK')
    print('G11_N1031_PUBLISHED_LOCK_RESULT_JOIN=PASS RACE=20261031-09-03 '
          'PREDICTION_CHANGED=0 SETTLEMENT_CHANGED=0 CANONICAL_FILES_CHANGED=0')
    return plan


def verify_night_readback(feed, readback, headers, checkpoint, authority):
    identity(readback, authority, "NIGHT")
    counts = readback.get("counts", {})
    require(counts.get("results") == counts.get("races") == checkpoint["races"]
            and counts.get("pending") == 0
            and {r["key"] for r in feed["races"]} == {r["key"] for r in readback["races"]},
            "DAILY_SITE_NIGHT_COVERAGE")
    transport = readback.get("transport", {})
    require(transport.get("canonical_feed_sha256") == checkpoint["feed_sha256"]
            and headers.get("x-g11-canonical-feed-sha256") == checkpoint["feed_sha256"]
            and transport.get("night_checkpoint_sha256") == checkpoint["checkpoint_sha256"]
            and transport.get("daily_authority_sha256") == checkpoint["authority_sha256"]
            and headers.get("x-g11-daily-authority-sha256") == checkpoint["authority_sha256"],
            "DAILY_SITE_CHECKPOINT_HASH_MISMATCH")
    rows = {r["key"]: r for r in readback["races"]}
    for row in feed["races"]:
        actual = rows[row["key"]]
        for field in hot.NIGHT_FIELDS:
            if field not in row or row[field] is None:
                continue
            target = "mashiro_hot" if field == "p3_verification" and field not in actual else field
            expected = hot.mashiro_hot(row[field]) if target == "mashiro_hot" else hot.project(field, row[field])
            require(hot.includes(actual.get(target), expected), "DAILY_SITE_RACE_READBACK:" + row["key"] + ":" + target)
    for field in hot.NIGHT_TOP:
        if field in feed:
            require(hot.includes(readback.get(field), feed[field]), "DAILY_SITE_TOP_READBACK:" + field)
    if n13_transport_only():
        require(feed["generated_at_jst"] == N13_CREATED and readback["generated_at_jst"] ==
                datetime.fromisoformat(N13_CREATED).isoformat(timespec="seconds"), "N13_WIRE_TIME_READBACK")


def commit_request(manifest):
    return {"SCHEMA": hot.NIGHT_TRANSACTION_SCHEMA, "ACTION": "COMMIT",
            **{k: manifest[k] for k in ("OPERATIONAL_DATE", "NIGHT_TRANSACTION_ID", "MANIFEST_SHA256", "SOURCE_NIGHT_SHA")}}


def optional_deme_projection(day):
    """Read only the bounded shadow output; a research failure cannot stop NIGHT."""
    path = Path(".relay-output/deme-discovery-public.json")
    if not path.is_file():
        return None
    try:
        raw = path.read_bytes()
        if len(raw) > 256_000:
            raise ValueError("BUDGET")
        value = json.loads(raw)
        packed = value.get("high_payout_packed") if isinstance(value, dict) else None
        base = {k:v for k,v in value.items() if k != "high_payout_packed"} if isinstance(value,dict) else value
        if len(json.dumps(base,ensure_ascii=False,separators=(",", ":")).encode()) > 180_000:
            raise ValueError("BASE_BUDGET")
        if packed is not None and (not isinstance(packed,dict)
                or packed.get("schema") != "G11_DEME_HIGH_PAYOUT_PACKED_V1"
                or packed.get("encoding") != "gzip-base64"
                or not isinstance(packed.get("data"),str) or len(packed["data"]) > 75_000
                or re.fullmatch(r"[A-Za-z0-9+/]*={0,2}",packed["data"]) is None
                or re.fullmatch(r"[a-f0-9]{64}",str(packed.get("sha256"))) is None
                or type(packed.get("uncompressed_bytes")) is not int
                or not 0 < packed["uncompressed_bytes"] <= 1_000_000):
            raise ValueError("HIGH_PAYOUT_BUDGET")
        if (not isinstance(value, dict) or value.get("schema") != "G11_P3_DEME_DISCOVERY_V1"
                or value.get("production_mutated") is not False
                or not isinstance(value.get("available"), bool)
                or not isinstance(value.get("counts"), dict)
                or not isinstance(value.get("candidates"), list)
                or str(value.get("discovery_through") or "") > day):
            raise ValueError("CONTRACT")
        # This hot-delta route requires second-resolution JST. The dedicated
        # research delivery below accepts the original richer timestamp. Do
        # not retry an invalid research-only delta or rewrite its saved bytes.
        stamp = value.get('updated_at')
        if (not isinstance(stamp, str) or not re.fullmatch(
                r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+09:00', stamp)
                or stamp[:10] > day):
            print('G11_DEME_PUBLIC=DEFERRED_TO_DEDICATED_DELIVERY')
            return None
        datetime.fromisoformat(stamp)
        return value
    except (OSError, ValueError, UnicodeDecodeError):
        print("G11_DEME_PUBLIC=HELD_INVALID_OPTIONAL_PROJECTION")
        return None


def send_night(state, day, feed, checkpoint, authority, implementation, origin, token):
    url = f"{origin}/api/g11-feed?date={day}"
    accepted, headers, _ = hot.fetch(url)
    identity(accepted, authority, accepted["stage"])
    wire_feed, lock_proof = published_lock_night_20261031(feed, accepted, checkpoint)
    root = implementation.day_root(state, day)
    plan_path = root / "night-transport-plan.json"
    if n13_transport_only():
        require(day == N13_DAY, "N13_TRANSPORT_SCOPE")
        plan_path = saved_n13_plan(root, implementation)
    if accepted["stage"] == "NIGHT":
        # ACK/readback may have been lost after an atomic commit. Repeat only
        # COMMIT against the server's original immutable manifest.
        verify_night_readback(wire_feed, accepted, headers, checkpoint, authority)
        transport = accepted["transport"]
        manifest = {"OPERATIONAL_DATE": day,
                    "NIGHT_TRANSACTION_ID": transport["night_transaction_id"],
                    "MANIFEST_SHA256": transport["night_manifest_sha256"],
                    "SOURCE_NIGHT_SHA": feed["source"]["projection_sha256"]}
    else:
        if plan_path.exists():
            try:
                plan = implementation.json_file(plan_path)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise RuntimeError("NIGHT_TRANSPORT_PLAN_HASH_MISMATCH") from None
            require(plan.get("checkpoint_sha256") == checkpoint["checkpoint_sha256"], "NIGHT_TRANSPORT_PLAN_CHECKPOINT_MISMATCH")
            unsigned = dict(plan)
            expected = unsigned.pop("plan_sha256", None)
            require(expected == implementation.digest(implementation.canonical(unsigned)), "NIGHT_TRANSPORT_PLAN_HASH_MISMATCH")
            if not n13_transport_only():
                plan = verified_utf8_plan(root, plan, implementation)
            manifest, chunks = plan["manifest"], plan["chunks"]
            require(headers["x-g11-source-feed-sha256"] == manifest["BASE_FEED_SHA"], "NIGHT_TRANSPORT_BASE_CHANGED")
        else:
            delta = hot.create_delta(feed, accepted, headers["x-g11-source-feed-sha256"], optional_deme_projection(day))
            manifest, chunks = hot.night_chunks(delta, feed, checkpoint)
            plan = {"checkpoint_sha256": checkpoint["checkpoint_sha256"], "manifest": manifest, "chunks": chunks}
            plan["plan_sha256"] = implementation.digest(implementation.canonical(plan))
            # Preserve the serialized JavaScript hash order in one exclusive
            # write. No partially-persisted two-file plan needs repair.
            implementation.immutable(plan_path, hot.compact(plan))
        if lock_proof is not None:
            plan = published_lock_night_plan(root, plan, wire_feed, accepted, checkpoint,
                                             lock_proof, implementation)
            manifest, chunks = plan['manifest'], plan['chunks']
        hot.post(origin, token, manifest, "/api/g11-night-transaction")
        for chunk in chunks:
            hot.post(origin, token, chunk, "/api/g11-night-transaction")
    ack = hot.post(origin, token, commit_request(manifest), "/api/g11-night-transaction")
    readback, next_headers, raw = hot.fetch(url)
    require(ack.get("payload_sha256") == next_headers.get("x-g11-source-feed-sha256"), "DAILY_SITE_COMMIT_READBACK_SHA")
    verify_night_readback(wire_feed, readback, next_headers, checkpoint, authority)
    # The saved immutable transaction plan, including optional research, is the
    # retry authority. Research readback is diagnostic and never blocks NIGHT.
    if plan_path.is_file():
        try:
            plan = implementation.json_file(plan_path)
            parts = [c["TOP_LEVEL"].get("deme_discovery") for c in plan.get("chunks", [])
                     if "deme_discovery" in c.get("TOP_LEVEL", {})]
            if parts:
                print("G11_DEME_SITE_READBACK=" + ("PASS" if all(hot.includes(readback.get("deme_discovery"), p) for p in parts) else "HELD"))
        except (OSError, ValueError, KeyError):
            print("G11_DEME_SITE_READBACK=HELD")
    return ack, next_headers, raw, manifest


def send_morning(feed_path, feed, authority, origin, token):
    with tempfile.TemporaryDirectory() as directory:
        projected = Path(directory) / "morning.json"
        projected.write_bytes(feed_path.read_bytes())
        hot.project_morning_base(projected, optional_deme_projection(feed["operational_date_jst"]))
        data = projected.read_bytes()
        response = Path(directory) / "response.json"
        result = subprocess.run(["curl", "--silent", "--show-error", "--max-time", "45",
            "-X", "POST", origin + "/api/g11-sync", "-H", "Authorization: Bearer " + token,
            "-H", "Content-Type: application/json", "-H", "x-g11-feed-sha256: " + hot.hash_bytes(data),
            "--data-binary", "@" + str(projected), "--output", str(response), "--write-out", "%{http_code}"],
            capture_output=True, text=True, check=True)
        ack = json.loads(response.read_bytes())
        require(200 <= int(result.stdout) < 300 and ack.get("status") == "PASS", "DAILY_MORNING_HTTP_REJECTED")
        readback, headers, raw = hot.fetch(origin + "/api/g11-feed?date=" + feed["operational_date_jst"])
        identity(readback, authority, "MORNING")
        require(headers.get("x-g11-source-feed-sha256") == hot.hash_bytes(data) == ack.get("payload_sha256"), "DAILY_MORNING_READBACK_SHA")
        return ack, headers, raw


def deliver(state: Path, feed_path: Path, origin: str, *, attempts=MAX_ATTEMPTS, pause=time.sleep,
            include_optional_inputs=True):
    if n13_transport_only():
        attempts = 1
    require(origin == ORIGIN and 1 <= attempts <= MAX_ATTEMPTS, "DAILY_DELIVERY_CONFIGURATION")
    Path(".relay-output").mkdir(exist_ok=True)
    incoming = json.loads(feed_path.read_bytes())
    day = incoming["operational_date_jst"]
    rescue_class = incoming.get("SNAPSHOT_CLASS")
    if rescue_class == "SAME_DAY_RESCUE_BOUNDARY":
        from daily_predeadline_authority_recovery_v1 import is_delayed_day
        if is_delayed_day(day) and incoming.get("stage") == "NIGHT":
            from daily_predeadline_authority_recovery_v1 import deliver_delayed_night
            return deliver_delayed_night(state, feed_path, origin)
        if (is_delayed_day(day)
                and (state / "rescue/predeadline-audit.json").is_file()):
            from daily_predeadline_authority_recovery_v1 import deliver as deliver_delayed_live
            return deliver_delayed_live(state, feed_path, origin)
        return deliver_rescue(state, feed_path, origin, attempts, pause)
    if rescue_class == "TODAY_ONLY_RESCUE_PREDEADLINE":
        return deliver_rescue_predeadline(state, feed_path, origin)
    if rescue_class == "SAME_DAY_RESCUE_NONRESEARCH" and incoming.get("stage") == "NIGHT":
        return deliver_rescue_night(state, feed_path, origin)
    implementation = retained_module(state, day)
    authority = implementation.load(state, day)
    feed = implementation.json_file(feed_path)
    phase = feed["stage"]
    identity(feed, authority, phase)
    if not n13_transport_only():
        implementation.verify_locks(state, day, "MORNING", exact=True)
        implementation.verify_locks(state, day, "PREDEADLINE", exact=True)
    token = os.environ["G11_SYNC_OIDC"]
    checkpoint = None
    if phase == "NIGHT":
        checkpoint = (check_saved_n13(implementation, state, feed_path) if n13_transport_only()
                      else implementation.read_checkpoint(state, day))
        require(hot.hash_bytes(feed_path.read_bytes()) == checkpoint["feed_sha256"], "NIGHT_DELIVERY_CANONICAL_FEED_CHANGED")
    if n13_transport_only():
        require(day == N13_DAY and phase == "NIGHT", "N13_TRANSPORT_SCOPE")
        check_saved_n13(implementation, state, feed_path)
        saved_n13_plan(implementation.day_root(state, day), implementation)
        print("G11_N13_CANONICAL_BEFORE_SEND=PASS SHA256=" + N13_FEED_SHA)
    for attempt in range(attempts):
        # Integrity failures stop before network, including between retries.
        implementation.load(state, day)
        if not n13_transport_only():
            implementation.verify_locks(state, day, "MORNING", exact=True)
            implementation.verify_locks(state, day, "PREDEADLINE", exact=True)
        if checkpoint:
            if n13_transport_only():
                check_saved_n13(implementation, state, feed_path)
            else:
                implementation.read_checkpoint(state, day)
        try:
            if phase == "NIGHT":
                ack, headers, raw, manifest = send_night(state, day, feed, checkpoint, authority, implementation, origin, token)
            elif phase == "MORNING":
                ack, headers, raw = send_morning(feed_path, feed, authority, origin, token)
            elif phase == "PREDEADLINE":
                if include_optional_inputs:
                    hot.publish(feed_path, origin, optional_deme_projection(day))
                else:
                    # Reserve slot zero for the saved critical projection;
                    # the regular update in this same run still uses slot one.
                    hot.publish(feed_path, origin, sequence_offset=0)
                readback, headers, raw = hot.fetch(origin + "/api/g11-feed?date=" + day)
                identity(readback, authority, "PREDEADLINE")
                ack = {"payload_sha256": headers["x-g11-source-feed-sha256"]}
            else:
                raise RuntimeError("DAILY_DELIVERY_PHASE")
            break
        except (subprocess.CalledProcessError, RuntimeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            # Transport failures are bounded. Authority/checkpoint/LOCK failures
            # never invoke repairs, inference or settlement on this route.
            reason = ("TRANSPORT_PROCESS_FAILURE:" + str(exc.returncode)
                      if isinstance(exc, subprocess.CalledProcessError)
                      else "TRANSPORT_RESPONSE_INVALID" if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError))
                      else str(exc))
            if any(word in reason for word in ("AUTHORITY", "CHECKPOINT", "HASH_MISMATCH", "BASE_CHANGED", "PLAN_HASH")) or attempt + 1 == attempts:
                # curl's CalledProcessError command contains the bearer token.
                # Keep it out of both the message and exception traceback chain.
                raise RuntimeError(reason) from None
            print("G11_DAILY_TRANSPORT_RETRY=" + str(attempt + 1) + " REASON=" + reason)
            pause((2, 5)[attempt])
    Path(".relay-output/hot-delta-accepted").write_text(ack["payload_sha256"] + "\n")
    Path(".relay-output/hot-delta-readback.json").write_bytes(raw)
    if n13_transport_only():
        check_saved_n13(implementation, state, feed_path)
        print("G11_N13_CANONICAL_AFTER_READBACK=PASS SHA256=" + N13_FEED_SHA)
    receipt = {"schema": "G11_DAILY_SITE_DELIVERY_RECEIPT_V1", "operational_date_jst": day,
               "phase": phase, "authority_sha256": authority["authority_sha256"],
               "runtime_source_sha": authority["runtime_source_sha"],
               "site_payload_sha256": ack["payload_sha256"], "site_get_sha256": hot.hash_bytes(raw),
               "http_success": True, "site_get_readback": "PASS"}
    if checkpoint:
        receipt.update({"canonical_feed_sha256": checkpoint["feed_sha256"], "checkpoint_sha256": checkpoint["checkpoint_sha256"],
                        "night_transaction_id": manifest["NIGHT_TRANSACTION_ID"], "manifest_sha256": manifest["MANIFEST_SHA256"],
                        "atomic_transaction": "PASS", "results": checkpoint["results"], "races": checkpoint["races"], "pending": 0,
                        "daily_operation_complete": "PASS"})
        # The immutable phase bindings, not a live research version or an
        # optional earlier delivery receipt, establish the previous phases.
        if not n13_transport_only():
            for previous in ("MORNING", "PREDEADLINE"):
                implementation.verify_locks(state, day, previous, exact=True)
        receipt["completion_gate"] = {
            "DAILY_AUTHORITY_READBACK": "PASS", "MORNING_AUTHORITY_MATCH": "PASS",
            "PREDEADLINE_AUTHORITY_MATCH": "PASS", "NIGHT_AUTHORITY_MATCH": "PASS",
            "RESULTS": checkpoint["results"], "RACES": checkpoint["races"], "PENDING": 0,
            "MORNING_LOCK_MUTATION": 0, "PREDEADLINE_LOCK_MUTATION": 0, "PREDICTION_RECALCULATION": 0,
            "FINANCE_VERIFIED": "PASS", "IMMUTABLE_NIGHT_CHECKPOINT": "PASS",
            "ATOMIC_TRANSACTION": "PASS", "SITE_GET_READBACK": "PASS"}
        target = implementation.day_root(state, day) / "daily-operation-complete.json"
    else:
        target = implementation.day_root(state, day) / "delivery" / phase / (ack["payload_sha256"] + ".json")
    receipt["receipt_sha256"] = implementation.digest(implementation.canonical(receipt))
    implementation.immutable(target, implementation.canonical(receipt) + b"\n")
    # Optional saved-input evidence only, after canonical delivery/readback.
    # No prediction pin, authority, notification candidate or delivery changes.
    try:
        from selective_alert_input_v1 import deliver_optional
        sys.path.insert(0, str(Path(".g11-private").resolve()))
        if include_optional_inputs and os.environ.get("G11_R13") != "true":
            deliver_optional(state / "store", feed, origin, token=token)
    except Exception:
        print("G11_SELECTIVE_INPUT=HELD_OPTIONAL_READER_UNAVAILABLE")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write("feed_sha=" + ack["payload_sha256"] + "\n")
    print(json.dumps(receipt, sort_keys=True))
    return receipt



def deliver_rescue_night(state, feed_path, origin):
    """Append official NIGHT results to the accepted rescue feed without daily authority."""
    incoming = json.loads(feed_path.read_bytes())
    require(incoming.get("operational_date_jst") == "2026-10-06"
            and incoming.get("stage") == "NIGHT"
            and incoming.get("SNAPSHOT_CLASS") == "SAME_DAY_RESCUE_NONRESEARCH"
            and incoming.get("counts", {}).get("pending") == 0
            and incoming.get("counts", {}).get("results") == incoming.get("counts", {}).get("races")
            and incoming.get("counts", {}).get("formal") == 0
            and incoming.get("counts", {}).get("research_samples") == 0,
            "RESCUE_NIGHT_TRANSPORT_IDENTITY")
    boundary = json.loads((state.resolve() / "rescue/boundary.json").read_bytes())
    authority_sha = boundary.get("authority_sha256")
    accepted, _, _ = hot.fetch(origin + "/api/g11-feed?date=2026-10-06")
    require(accepted.get("same_day_rescue", {}).get("authority_sha256") == authority_sha,
            "RESCUE_NIGHT_ACCEPTED_BOUNDARY")
    prior = {row["key"]: row for row in accepted["races"]}
    current = {row["key"]: row for row in incoming["races"]}
    require(set(prior) == set(current), "RESCUE_NIGHT_RACE_SET")
    out = json.loads(json.dumps(accepted))
    out["stage"] = "NIGHT"
    out["status"] = "PASS"
    out["generated_at_jst"] = incoming.get("generated_at_jst", out.get("generated_at_jst"))
    out["SNAPSHOT_CLASS"] = "SAME_DAY_RESCUE_NONRESEARCH"
    out["ACCOUNTING_CLASS"] = "TODAY_RESCUE"
    out["CANONICAL_MORNING_PROMOTED"] = False
    out["RESEARCH_ELIGIBLE_AS_MORNING"] = False
    out["MORNING_HISTORY_BACKFILL"] = False
    out["research_db"] = incoming.get("research_db", {"available": False, "formal_races": 0})
    out["counts"] = incoming["counts"]
    for row in out["races"]:
        src = current[row["key"]]
        for field in ("result_trifecta", "payout", "result_meta"):
            row[field] = src.get(field)
        row["research_eligible"] = False
        row["formal_status"] = "EXCLUDED"
        row["exclusion_reason"] = "SAME_DAY_RESCUE_NONRESEARCH"
        row["practical_hit"] = None
        row["validation_hit"] = None
        row["miss_classification"] = None
        row["research_decision"] = None
        row["research_finance"] = src.get("research_finance")
    data = hot.compact(out)
    token = os.environ["G11_SYNC_OIDC"]
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "rescue-night.json"
        response = Path(directory) / "response.json"
        path.write_bytes(data)
        result = subprocess.run([
            "curl", "--silent", "--show-error", "--max-time", "45", "-X", "POST",
            origin + "/api/g11-sync", "-H", "Authorization: Bearer " + token,
            "-H", "Content-Type: application/json",
            "-H", "x-g11-feed-sha256: " + hot.hash_bytes(data),
            "--data-binary", "@" + str(path), "--output", str(response), "--write-out", "%{http_code}",
        ], capture_output=True, text=True, check=True)
        ack = json.loads(response.read_bytes())
        require(200 <= int(result.stdout) < 300 and ack.get("status") == "PASS",
                "RESCUE_NIGHT_SITE_REJECTED:" + str(ack.get("error")))
    readback, headers, raw = hot.fetch(origin + "/api/g11-feed?date=2026-10-06")
    require(readback.get("stage") == "NIGHT"
            and readback.get("counts", {}).get("pending") == 0
            and readback.get("counts", {}).get("results") == readback.get("counts", {}).get("races")
            and readback.get("counts", {}).get("formal") == 0
            and headers.get("x-g11-source-feed-sha256") == hot.hash_bytes(data) == ack.get("payload_sha256"),
            "RESCUE_NIGHT_READBACK")
    rows = {row["key"]: row for row in readback["races"]}
    for key, before in prior.items():
        actual = rows[key]
        for field in ("same_day_rescue_lock", "practical_bets", "p3_production_bets",
                      "p3_snapshot_sha256", "production_points", "p3_head", "axis", "predeadline", "abeken_shadow"):
            require(actual.get(field) == before.get(field),
                    "RESCUE_NIGHT_PREDICTION_CHANGED:" + key + ":" + field)
        require(actual.get("result_meta") == current[key].get("result_meta"),
                "RESCUE_NIGHT_RESULT_READBACK:" + key)
    Path(".relay-output/hot-delta-accepted").write_text(ack["payload_sha256"] + "\n")
    Path(".relay-output/hot-delta-readback.json").write_bytes(raw)
    receipt = {"schema":"G11_SAME_DAY_RESCUE_NIGHT_DELIVERY_V1","status":"PASS",
               "operational_date_jst":"2026-10-06","results":readback["counts"]["results"],
               "races":readback["counts"]["races"],"pending":0,"site_get_readback":"PASS",
               "site_payload_sha256":ack["payload_sha256"]}
    (state / "rescue/night-delivery.json").write_bytes(hot.compact(receipt))
    print(json.dumps(receipt, sort_keys=True))
    return receipt

def deliver_rescue_predeadline(state, feed_path, origin):
    """Append only timely PREDEADLINE locks to the accepted rescue boundary."""
    boundary_path = state.resolve() / 'rescue/boundary.json'
    require(boundary_path.is_file() and not boundary_path.is_symlink(), 'RESCUE_BOUNDARY_MISSING')
    cert = json.loads(boundary_path.read_bytes())
    unsigned = dict(cert)
    authority_sha = unsigned.pop('authority_sha256', None)
    canonical = json.dumps(unsigned, ensure_ascii=False, sort_keys=True,
                           separators=(',', ':'), allow_nan=False).encode()
    require(
        cert.get('schema') == 'G11_SAME_DAY_RESCUE_BOUNDARY_V1'
        and cert.get('operational_date_jst') == '2026-10-06'
        and cert.get('canonical_morning_promoted') is False
        and cert.get('research_eligible') is False
        and authority_sha == hashlib.sha256(canonical).hexdigest()
        and not (state / 'daily-runtime/2026-10-06/authority.json').exists(),
        'RESCUE_BOUNDARY_INVALID',
    )
    incoming = json.loads(feed_path.read_bytes())
    require(
        incoming.get('SNAPSHOT_CLASS') == 'TODAY_ONLY_RESCUE_PREDEADLINE'
        and incoming.get('operational_date_jst') == '2026-10-06'
        and incoming.get('CANONICAL_MORNING_PROMOTED') is False
        and incoming.get('RESEARCH_ELIGIBLE_AS_MORNING') is False
        and all(isinstance(row.get('today_rescue'), dict) for row in incoming.get('races', [])),
        'RESCUE_PREDEADLINE_TRANSPORT_IDENTITY',
    )
    url = origin + '/api/g11-feed?date=2026-10-06'
    accepted, _, _ = hot.fetch(url)
    require(
        accepted.get('operational_date_jst') == '2026-10-06'
        and accepted.get('SNAPSHOT_CLASS') == 'SAME_DAY_RESCUE_BOUNDARY'
        and accepted.get('same_day_rescue', {}).get('authority_sha256') == authority_sha,
        'RESCUE_ACCEPTED_BOUNDARY_IDENTITY',
    )
    current = {row['key']: row for row in incoming['races']}
    prior = {row['key']: row for row in accepted['races']}
    require(set(current) == set(prior), 'RESCUE_PREDEADLINE_RACE_SET')
    out = json.loads(json.dumps(accepted))
    out['source'] = incoming['source']
    out['generated_at_jst'] = incoming['generated_at_jst']
    added = []
    v4_added = []
    for row in out['races']:
        new = current[row['key']]
        proof = new['today_rescue']
        pre = new.get('predeadline')
        old = row.get('predeadline')
        v4 = (new.get('abeken_shadow') or {}).get('v53') or {}
        prior_v4 = (row.get('abeken_shadow') or {}).get('v53') or {}
        v4live = v4.get('live')
        old_v4live = prior_v4.get('live')
        if isinstance(old, dict) and isinstance(pre, dict):
            for field in ('snapshot_sha256', 'captured_at_jst', 'practical_bets', 'production_points'):
                require(old.get(field) == pre.get(field), 'RESCUE_LIVE_LOCK_CHANGED:' + row['key'])
        if old_v4live is not None and v4live is not None:
            require(old_v4live == v4live, 'RESCUE_V4_LIVE_LOCK_CHANGED:' + row['key'])
        new_p3 = isinstance(pre, dict) and not isinstance(old, dict)
        new_v4 = v4live is not None and old_v4live is None
        if not (new_p3 or new_v4):
            continue
        # P3 and V4 are independently delivered immutable locks. A P3 lock
        # arriving first must not suppress a later V4 lock from the same input.
        # Both additions retain the original pre-deadline P3 proof gate.
        deadline = datetime.fromisoformat(row['deadline_jst'])
        require(
            isinstance(pre, dict)
            and deadline > datetime.now(deadline.tzinfo)
            and proof.get('status') == 'LOCKED'
            and proof.get('p3_snapshot_sha256') == pre.get('snapshot_sha256')
            and datetime.fromisoformat(pre['captured_at_jst']) < deadline
            and datetime.fromisoformat(pre['odds_captured_at_jst']) < deadline
            and row.get('research_eligible') is False,
            'RESCUE_LIVE_PROOF_INVALID:' + row['key'],
        )
        if new_v4:
            require(isinstance(v4live, dict) and bool(v4live),
                    'RESCUE_V4_LIVE_INVALID:' + row['key'])
            require(isinstance(prior_v4.get('morning'), dict)
                    and v4.get('morning') == prior_v4['morning'],
                    'RESCUE_V4_MORNING_CHANGED:' + row['key'])
            row['abeken_shadow']['v53']['live'] = v4live
            v4_added.append(row['key'])
        if new_p3:
            row['predeadline'] = pre
            for field in ('best_ev', 'best_ev_bet', 'value_bets', 'odds_captured_at_jst'):
                row[field] = pre[field]
            row['odds_merit'] = pre['legacy_odds_merit']
            added.append(row['key'])
    data = hot.compact(out)
    token = os.environ['G11_SYNC_OIDC']
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'rescue-predeadline.json'
        response = Path(directory) / 'response.json'
        path.write_bytes(data)
        result = subprocess.run([
            'curl', '--silent', '--show-error', '--max-time', '45', '-X', 'POST',
            origin + '/api/g11-sync', '-H', 'Authorization: Bearer ' + token,
            '-H', 'Content-Type: application/json',
            '-H', 'x-g11-feed-sha256: ' + hot.hash_bytes(data),
            '--data-binary', '@' + str(path), '--output', str(response), '--write-out', '%{http_code}',
        ], capture_output=True, text=True, check=True)
        ack = json.loads(response.read_bytes())
        require(200 <= int(result.stdout) < 300 and ack.get('status') == 'PASS',
                'RESCUE_PREDEADLINE_SITE_REJECTED:' + str(ack.get('error')))
    readback, headers, raw = hot.fetch(url)
    rows = {row['key']: row for row in readback['races']}
    require(
        readback.get('same_day_rescue', {}).get('authority_sha256') == authority_sha
        and headers.get('x-g11-source-feed-sha256') == hot.hash_bytes(data) == ack.get('payload_sha256'),
        'RESCUE_PREDEADLINE_READBACK_IDENTITY',
    )
    require(set(rows) == set(prior), 'RESCUE_PREDEADLINE_READBACK_RACE_SET')
    expected_rows = {row['key']: row for row in out['races']}
    for key, before in prior.items():
        actual = rows[key]
        for field in ('same_day_rescue_lock', 'practical_bets', 'p3_production_bets',
                      'p3_snapshot_sha256', 'production_points', 'p3_head', 'axis',
                      'formal_status', 'research_eligible'):
            require(actual.get(field) == before.get(field),
                    'RESCUE_MORNING_READBACK_LOCK:' + key + ':' + field)
        before_v4 = (before.get('abeken_shadow') or {}).get('v53') or {}
        actual_v4 = (actual.get('abeken_shadow') or {}).get('v53') or {}
        expected_v4 = (expected_rows[key].get('abeken_shadow') or {}).get('v53') or {}
        require(actual_v4.get('morning') == before_v4.get('morning'),
                'RESCUE_V4_MORNING_READBACK_LOCK:' + key)
        require(actual_v4.get('live') == expected_v4.get('live'),
                'RESCUE_V4_READBACK_LOCK:' + key)
        require(actual.get('predeadline') == expected_rows[key].get('predeadline'),
                'RESCUE_PREDEADLINE_READBACK_LOCK:' + key)
    Path('.relay-output/hot-delta-accepted').write_text(ack['payload_sha256'] + '\n')
    Path('.relay-output/hot-delta-readback.json').write_bytes(raw)
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write('feed_sha=' + ack['payload_sha256'] + '\n')
    receipt = {
        'schema': 'G11_SAME_DAY_RESCUE_PREDEADLINE_DELIVERY_V1',
        'status': 'PASS', 'operational_date_jst': '2026-10-06',
        'authority_sha256': authority_sha, 'site_get_readback': 'PASS',
        'delivery_state': 'APPENDED' if added or v4_added else 'NO_NEW_LOCKS',
        'new_live_locks': added,
        'new_v4_live_locks': v4_added,
        'verified_p3_live_count': sum(isinstance(row.get('predeadline'), dict) for row in rows.values()),
        'verified_v4_live_count': sum(isinstance(((row.get('abeken_shadow') or {}).get('v53') or {}).get('live'), dict)
                                      for row in rows.values()),
        'live_proof': [{'key': k, 'captured_at_jst': rows[k]['predeadline']['captured_at_jst'],
                        'deadline_jst': rows[k]['deadline_jst'],
                        'morning_p3_snapshot_sha256': rows[k]['p3_snapshot_sha256']} for k in added], 'site_payload_sha256': ack['payload_sha256'],
    }
    (state / 'rescue/predeadline-delivery.json').write_bytes(hot.compact(receipt))
    print(json.dumps(receipt, sort_keys=True))
    return receipt

def deliver_rescue(state, feed_path, origin, attempts, pause):
    """Transport one explicitly nonresearch boundary; no canonical fallback."""
    boundary_path = state.resolve() / 'rescue/boundary.json'
    require(boundary_path.is_file() and not boundary_path.is_symlink(), 'RESCUE_BOUNDARY_MISSING')
    cert = json.loads(boundary_path.read_bytes())
    unsigned = dict(cert)
    authority_sha = unsigned.pop('authority_sha256', None)
    day = cert.get('operational_date_jst', '')
    delayed = cert.get('schema') == 'G11_DELAYED_MORNING_RECOVERY_BOUNDARY_V1' and day >= '2026-10-16'
    if delayed:
        parent = json.loads((state / 'daily-runtime' / day / 'authority.json').read_bytes())
        require(parent['authority_sha256'] == cert.get('parent_authority_sha256')
                and cert.get('canonical_morning_promoted') is False
                and cert.get('research_eligible') is False
                and cert.get('same_day_results_used') is False
                and cert.get('safety_margin_seconds') == 300, 'DELAYED_RECOVERY_PARENT')
    require((delayed or (cert.get('schema') == 'G11_SAME_DAY_RESCUE_BOUNDARY_V1' and day == '2026-10-06'))
            and authority_sha == hashlib.sha256(json.dumps(unsigned, ensure_ascii=False, sort_keys=True,
                separators=(',', ':'), allow_nan=False).encode()).hexdigest(), 'RESCUE_BOUNDARY_INVALID')
    feed = json.loads(feed_path.read_bytes())
    require(feed['operational_date_jst'] == day and feed['stage'] == 'MORNING'
            and feed['same_day_rescue']['authority_sha256'] == cert['authority_sha256']
            and feed.get('daily_runtime') is None and feed['counts']['formal'] == 0
            and feed['counts']['research_samples'] == 0, 'RESCUE_TRANSPORT_IDENTITY')
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'rescue.json'
        path.write_bytes(feed_path.read_bytes())
        hot.project_morning_base(path)
        projected = json.loads(path.read_bytes())
        for field in ('SNAPSHOT_CLASS', 'CANONICAL_MORNING_PROMOTED', 'RESEARCH_ELIGIBLE_AS_MORNING',
                      'MORNING_HISTORY_BACKFILL', 'ACCOUNTING_CLASS', 'same_day_rescue'):
            projected[field] = feed[field]
        proofs = {r['key']: r.get('same_day_rescue_lock') for r in feed['races']}
        for row in projected['races']:
            if proofs[row['key']]:
                row['same_day_rescue_lock'] = proofs[row['key']]
        data = hot.compact(projected)
        path.write_bytes(data)
        response = Path(directory) / 'response.json'
        token = os.environ['G11_SYNC_OIDC']
        for attempt in range(attempts):
            result = subprocess.run(['curl', '--silent', '--show-error', '--max-time', '45',
                '-X', 'POST', origin + '/api/g11-sync', '-H', 'Authorization: Bearer ' + token,
                '-H', 'Content-Type: application/json', '-H', 'x-g11-feed-sha256: ' + hot.hash_bytes(data),
                '--data-binary', '@' + str(path), '--output', str(response), '--write-out', '%{http_code}'],
                capture_output=True, text=True)
            require(result.returncode == 0, 'RESCUE_TRANSPORT_PROCESS')
            ack = json.loads(response.read_bytes())
            code = int(result.stdout)
            if 200 <= code < 300 and ack.get('status') == 'PASS':
                break
            if code < 500 or attempt + 1 == attempts:
                raise RuntimeError('RESCUE_SITE_REJECTED:' + str(ack.get('error')))
            pause((2, 5)[attempt])
        readback, headers, raw = hot.fetch(origin + '/api/g11-feed?date=' + day)
        require(readback.get('same_day_rescue') == projected['same_day_rescue']
                and readback.get('SNAPSHOT_CLASS') == 'SAME_DAY_RESCUE_BOUNDARY'
                and headers.get('x-g11-source-feed-sha256') == hot.hash_bytes(data) == ack.get('payload_sha256'),
                'RESCUE_SITE_READBACK_IDENTITY')
        expected = {r['key']: r for r in projected['races']}
        for row in readback['races']:
            before = expected[row['key']]
            for field in ('same_day_rescue_lock', 'practical_bets', 'p3_snapshot_sha256', 'abeken_shadow'):
                require(before.get(field) == row.get(field), 'RESCUE_SITE_LOCK_READBACK:' + row['key'] + ':' + field)
        Path('.relay-output/hot-delta-accepted').write_text(ack['payload_sha256'] + '\n')
        Path('.relay-output/hot-delta-readback.json').write_bytes(raw)
        receipt = {'schema': 'G11_SAME_DAY_RESCUE_DELIVERY_V1', 'status': 'PASS',
            'operational_date_jst': day, 'site_get_readback': 'PASS',
            'rescued_race_count': feed['same_day_rescue']['rescued_race_count'],
            'excluded_past_deadline_race_count': feed['same_day_rescue']['excluded_past_deadline_race_count'],
            'authority_sha256': cert['authority_sha256'], 'site_payload_sha256': ack['payload_sha256']}
        (state / 'rescue/delivery.json').write_bytes(hot.compact(receipt))
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                output.write('feed_sha=' + ack['payload_sha256'] + '\n')
        print(json.dumps(receipt, sort_keys=True))
        return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--feed", type=Path, required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--emit-saved-n13", action="store_true")
    args = parser.parse_args()
    if args.emit_saved_n13:
        emit_saved_n13(args.state_root, args.feed)
    else:
        deliver(args.state_root, args.feed, args.origin)
        if not n13_transport_only():
            from official_result_delivery_v1 import optional_publish as publish_official_results
            if publish_official_results(args.state_root, args.feed, args.origin)['status'] == 'HELD':
                raise SystemExit('OFFICIAL_RESULT_PUBLICATION_INCOMPLETE')
            from deme_research_delivery_v1 import optional_publish
            optional_publish(args.state_root, args.feed, args.origin)







