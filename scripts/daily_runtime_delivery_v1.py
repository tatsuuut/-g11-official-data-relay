"""Daily transport adapter: locked feeds in, verified Site receipts out.

No private runner, acquisition, prediction or settlement is imported here.
NIGHT retries use exactly the immutable checkpoint bytes and transaction plan.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
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


def commit_request(manifest):
    return {"SCHEMA": hot.NIGHT_TRANSACTION_SCHEMA, "ACTION": "COMMIT",
            **{k: manifest[k] for k in ("OPERATIONAL_DATE", "NIGHT_TRANSACTION_ID", "MANIFEST_SHA256", "SOURCE_NIGHT_SHA")}}


def send_night(state, day, feed, checkpoint, authority, implementation, origin, token):
    url = f"{origin}/api/g11-feed?date={day}"
    accepted, headers, _ = hot.fetch(url)
    identity(accepted, authority, accepted["stage"])
    root = implementation.day_root(state, day)
    plan_path = root / "night-transport-plan.json"
    if accepted["stage"] == "NIGHT":
        # ACK/readback may have been lost after an atomic commit. Repeat only
        # COMMIT against the server's original immutable manifest.
        verify_night_readback(feed, accepted, headers, checkpoint, authority)
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
            manifest, chunks = plan["manifest"], plan["chunks"]
            require(headers["x-g11-source-feed-sha256"] == manifest["BASE_FEED_SHA"], "NIGHT_TRANSPORT_BASE_CHANGED")
        else:
            delta = hot.create_delta(feed, accepted, headers["x-g11-source-feed-sha256"])
            manifest, chunks = hot.night_chunks(delta, feed, checkpoint)
            plan = {"checkpoint_sha256": checkpoint["checkpoint_sha256"], "manifest": manifest, "chunks": chunks}
            plan["plan_sha256"] = implementation.digest(implementation.canonical(plan))
            # Preserve the serialized JavaScript hash order in one exclusive
            # write. No partially-persisted two-file plan needs repair.
            implementation.immutable(plan_path, hot.compact(plan))
        hot.post(origin, token, manifest, "/api/g11-night-transaction")
        for chunk in chunks:
            hot.post(origin, token, chunk, "/api/g11-night-transaction")
    ack = hot.post(origin, token, commit_request(manifest), "/api/g11-night-transaction")
    readback, next_headers, raw = hot.fetch(url)
    require(ack.get("payload_sha256") == next_headers.get("x-g11-source-feed-sha256"), "DAILY_SITE_COMMIT_READBACK_SHA")
    verify_night_readback(feed, readback, next_headers, checkpoint, authority)
    return ack, next_headers, raw, manifest


def send_morning(feed_path, feed, authority, origin, token):
    with tempfile.TemporaryDirectory() as directory:
        projected = Path(directory) / "morning.json"
        projected.write_bytes(feed_path.read_bytes())
        hot.project_morning_base(projected)
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


def deliver(state: Path, feed_path: Path, origin: str, *, attempts=MAX_ATTEMPTS, pause=time.sleep):
    require(origin == ORIGIN and 1 <= attempts <= MAX_ATTEMPTS, "DAILY_DELIVERY_CONFIGURATION")
    Path(".relay-output").mkdir(exist_ok=True)
    incoming = json.loads(feed_path.read_bytes())
    day = incoming["operational_date_jst"]
    rescue_class = incoming.get("SNAPSHOT_CLASS")
    if rescue_class == "SAME_DAY_RESCUE_BOUNDARY":
        return deliver_rescue(state, feed_path, origin, attempts, pause)
    if rescue_class == "TODAY_ONLY_RESCUE_PREDEADLINE":
        return deliver_rescue_predeadline(state, feed_path, origin)
    implementation = retained_module(state, day)
    authority = implementation.load(state, day)
    feed = implementation.json_file(feed_path)
    phase = feed["stage"]
    identity(feed, authority, phase)
    implementation.verify_locks(state, day, "MORNING", exact=True)
    implementation.verify_locks(state, day, "PREDEADLINE", exact=True)
    token = os.environ["G11_SYNC_OIDC"]
    checkpoint = None
    if phase == "NIGHT":
        checkpoint = implementation.read_checkpoint(state, day)
        require(hot.hash_bytes(feed_path.read_bytes()) == checkpoint["feed_sha256"], "NIGHT_DELIVERY_CANONICAL_FEED_CHANGED")
    for attempt in range(attempts):
        # Integrity failures stop before network, including between retries.
        implementation.load(state, day)
        implementation.verify_locks(state, day, "MORNING", exact=True)
        implementation.verify_locks(state, day, "PREDEADLINE", exact=True)
        if checkpoint:
            implementation.read_checkpoint(state, day)
        try:
            if phase == "NIGHT":
                ack, headers, raw, manifest = send_night(state, day, feed, checkpoint, authority, implementation, origin, token)
            elif phase == "MORNING":
                ack, headers, raw = send_morning(feed_path, feed, authority, origin, token)
            elif phase == "PREDEADLINE":
                hot.publish(feed_path, origin)
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
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write("feed_sha=" + ack["payload_sha256"] + "\n")
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
    for row in out['races']:
        new = current[row['key']]
        proof = new['today_rescue']
        pre = new.get('predeadline')
        old = row.get('predeadline')
        if isinstance(old, dict):
            if isinstance(pre, dict):
                for field in ('snapshot_sha256', 'captured_at_jst', 'practical_bets', 'production_points'):
                    require(old.get(field) == pre.get(field), 'RESCUE_LIVE_LOCK_CHANGED:' + row['key'])
            continue
        if not isinstance(pre, dict):
            continue
        deadline = datetime.fromisoformat(row['deadline_jst'])
        require(
            deadline > datetime.now(deadline.tzinfo)
            and proof.get('status') == 'LOCKED'
            and proof.get('p3_snapshot_sha256') == pre.get('snapshot_sha256')
            and datetime.fromisoformat(pre['captured_at_jst']) < deadline
            and datetime.fromisoformat(pre['odds_captured_at_jst']) < deadline
            and row.get('research_eligible') is False,
            'RESCUE_LIVE_PROOF_INVALID:' + row['key'],
        )
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
    for key in added:
        require(rows.get(key, {}).get('predeadline') == current[key]['predeadline'],
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
        'new_live_locks': added, 'site_payload_sha256': ack['payload_sha256'],
    }
    (state / 'rescue/predeadline-delivery.json').write_bytes(hot.compact(receipt))
    print(json.dumps(receipt, sort_keys=True))
    return receipt

def deliver_rescue(state, feed_path, origin, attempts, pause):
    """Transport one explicitly nonresearch boundary; no canonical fallback."""
    sys.path.insert(0, str(Path('.g11-private').resolve()))
    from g11.relay import same_day_boundary_20261006 as boundary
    cert = boundary.verify(state.resolve() / 'rescue/store')
    feed = json.loads(feed_path.read_bytes())
    require(feed['operational_date_jst'] == '2026-10-06' and feed['stage'] == 'MORNING'
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
        readback, headers, raw = hot.fetch(origin + '/api/g11-feed?date=2026-10-06')
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
            'operational_date_jst': '2026-10-06', 'site_get_readback': 'PASS',
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
    args = parser.parse_args()
    deliver(args.state_root, args.feed, args.origin)
