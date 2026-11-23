"""Publish already persisted daily LOCKs before optional PREDEADLINE work.

This adapter invokes the frozen read-only projector and existing authenticated
transport. It never acquires inputs, calls inference, seals or changes a LOCK.
The workflow must verify the encrypted critical checkpoint before invoking it.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.request import Request, urlopen
from rescue_night_result_parser_v1 import official_input_compatibility

JST = timezone(timedelta(hours=9))
INDEPENDENT_LIVE_FROM = '2026-11-15'
SAVED_PROJECTOR_SHA = 'ce17fedac3770fe73f8059ceeb0a833c0bfdbc0b4d67902ebd4994ccd7337956'


def newest_checkpoint_key(day, caches):
    """Use creation order, never access order, across the two saved lanes."""
    import re
    datetime.strptime(day, '%Y-%m-%d')
    pattern = re.compile(r'g11-state-v4-(?:critical|published)-' + re.escape(day) + r'-[0-9]+(?:-[0-9]+-checkpoint)?$')
    candidates = [row for row in caches if isinstance(row, dict)
                  and row.get('ref') == 'refs/heads/main'
                  and pattern.fullmatch(str(row.get('key', '')))]
    if not candidates:
        return ''
    return max(candidates, key=lambda row: (datetime.fromisoformat(
        row['created_at'].replace('Z', '+00:00')), row['id']))['key']


def select_restore(day):
    """Select metadata only; the normal encrypted restore/readback still gates use."""
    import subprocess
    caches = []
    for lane in ('critical', 'published'):
        url = (f"repos/{os.environ['GITHUB_REPOSITORY']}/actions/caches?"
               f"key=g11-state-v4-{lane}-{day}-&ref=refs%2Fheads%2Fmain"
               '&per_page=100&sort=created_at&direction=desc')
        response = subprocess.run(['gh', 'api', url], check=True, capture_output=True, text=True)
        caches.extend(json.loads(response.stdout)['actions_caches'])
    key = newest_checkpoint_key(day, caches)
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write('cache_key=' + key + '\n')
    print('G11_RESTORE_NEWEST_CHECKPOINT=' + (key or 'NO_SAME_DAY_CHECKPOINT'))


def require(ok, code):
    if not ok:
        raise RuntimeError(code)


def prewarm_worker(day, phase, *, now=None):
    """Start preparation only; the child still waits for authenticated new input."""
    import subprocess
    selected = os.environ.get('SELECTED_CHECKPOINT', '')
    require(selected and selected == os.environ.get('RESTORED_CHECKPOINT'),
            'G11_SELECTED_CHECKPOINT_MISMATCH')
    print('G11_SELECTED_CHECKPOINT_RESTORED=PASS', flush=True)
    now = now or datetime.now(JST)
    run = os.environ.get('GITHUB_RUN_ID', '')
    match = re.fullmatch(r'g11-state-v4-(?:critical|published)-' + re.escape(day)
                        + r'-([0-9]+)(?:-[0-9]+-checkpoint)?', selected)
    if (phase != 'predeadline' or now.date().isoformat() != day or not match
            or not run.isdigit() or int(match.group(1)) >= int(run)
            or os.environ.get('GITHUB_RUN_ATTEMPT') != '1'):
        print('WILD4_PREWARM=NOT_CURRENT_LIVE')
        return
    def status(value):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write('status=' + value + '\n')
    status('attempted')
    try:
        result = subprocess.run(['gh', 'workflow', 'run', '364923281',
            '-f', 'mode=wild4', '-f', 'operational_date=' + day,
            '-f', f'predeadline_checkpoint_key=g11-state-v4-critical-{day}-{run}',
            '-f', f'predeadline_delta_key=g11-state-v4-delta-{day}-{run}',
            '-f', 'prewarm_base_key=' + selected], capture_output=True, timeout=12, check=False)
        if result.returncode == 0:
            status('sent')
            print('WILD4_PREWARM=ACCEPTED_BEFORE_CAPTURE', flush=True)
        else:
            print('WILD4_PREWARM=DISPATCH_UNCONFIRMED', flush=True)
    except (OSError, subprocess.SubprocessError):
        print('WILD4_PREWARM=DISPATCH_UNCONFIRMED', flush=True)


def capture_boundary_delay(plan, saved_sources, now, maximum=300):
    """Wait once for the nearest capture boundary only when no race is due.

    Never wait between captures: the frozen collector keeps its eight-source,
    no-future-wait policy. An active race, including a partially saved packet,
    must return to capture/prediction/publication immediately.
    """
    require(now.utcoffset() == timedelta(hours=9), 'CAPTURE_BOUNDARY_TIMEZONE')
    future = []
    for task in plan:
        due = datetime.fromisoformat(task['DUE_AT_JST'])
        deadline = datetime.fromisoformat(task['MUST_FINISH_BEFORE_JST'])
        require(due.utcoffset() == deadline.utcoffset() == timedelta(hours=9)
                and due.date() == deadline.date() == now.date()
                and deadline - due == timedelta(minutes=7), 'CAPTURE_BOUNDARY_PLAN')
        from predeadline_capture_window_v1 import effective_due
        due = effective_due(due, deadline)
        if now >= deadline:
            continue
        if due <= now:
            return 0
        if task['SOURCE_ID'] not in saved_sources:
            future.append((due - now).total_seconds())
    delay = min(future, default=0)
    return delay if 0 < delay <= maximum else 0


def align_empty_capture(state, day, *, clock=None, sleeper=None):
    """Read the verified saved plan; never request or rewrite official input."""
    if os.environ.get('G11_WARM_CAPTURE_REMAINING'):
        return  # The first pass already identified queued work; never sleep again here.
    clock = clock or (lambda: datetime.now(JST))
    sleeper = sleeper or time.sleep
    try:
        now = clock()
        require(now.date().isoformat() == day, 'CAPTURE_BOUNDARY_DAY')
        from g11.canonical.official_pipeline import CanonicalRunner, OfficialArtifactStore
        artifact = OfficialArtifactStore(state / 'store')
        morning = artifact.load_verified_snapshot('morning', 'official-day-input')
        _, eligible, _ = CanonicalRunner._formal_prediction_scope(
            morning, failure_prefix='CAPTURE_BOUNDARY_SCOPE')
        eligible = set(eligible)
        plan = [row for row in morning['SNAPSHOT']['PAYLOAD']['PREDEADLINE_PLAN']
                if row['RACE_KEY'] in eligible]
        saved = {path.stem for path in (state / 'store/snapshots/predeadline').glob('*.json')}
        delay = capture_boundary_delay(plan, saved, now)
    except Exception as exc:
        # Optional alignment must never replace the critical lane's own guards.
        print('G11_CAPTURE_BOUNDARY=SKIPPED TYPE=' + type(exc).__name__, flush=True)
        return
    print('G11_CAPTURE_BOUNDARY_WAIT_SECONDS=' + str(round(delay, 3)), flush=True)
    # One fixed, bounded target. Do not extend the wait for subsequent races.
    while delay > 0:
        interval = min(delay, 30)
        sleeper(interval)
        delay -= interval


def independent_live_projector(relay, store, day, morning, publication):
    """Use verified independent LIVE locks to select the read-only feed stage.

    The reviewed projector previously waited for a legacy P3 snapshot even
    when V53 already had a valid saved LIVE. Read the existing native V53/P3
    projections first; only their verified formal locks can open this stage.
    No forecast, source file, saved plan or snapshot is written by this adapter.
    Unknown projector versions retain their original stage policy.
    """
    original = relay.build_feed
    if day < INDEPENDENT_LIVE_FROM:
        return original
    try:
        source = inspect.getsource(inspect.unwrap(original))
    except (OSError, TypeError):
        return original
    if hashlib.sha256(source.encode()).hexdigest() != SAVED_PROJECTOR_SHA:
        return original
    fields = morning.get('LOCK49_FIELDS')
    rows = morning.get('LOCK49_ROWS')
    require(morning.get('OPERATIONAL_DATE_JST') == day
            and fields == list(relay.feedlib.LOCK49_FIELDS)
            and type(rows) is list, 'EARLY_PUBLICATION_LIVE_MORNING_BINDING')
    formal = {}
    keys = set()
    for row in rows:
        require(type(row) is list and len(row) == len(fields),
                'EARLY_PUBLICATION_LIVE_MORNING_ROW')
        value = dict(zip(fields, row))
        key = value['標準レースキー']
        require(type(key) is str and key not in keys
                and key.startswith(day.replace('-', '') + '-')
                and value['正式/除外'] in {'FORMAL', 'EXCLUDED'},
                'EARLY_PUBLICATION_LIVE_RACE_BINDING')
        keys.add(key)
        if value['正式/除外'] == 'FORMAL':
            deadline = datetime.fromisoformat(relay.feedlib._deadline(
                value['日付'], value['締切']))
            require(deadline.utcoffset() == timedelta(hours=9)
                    and deadline.date().isoformat() == day,
                    'EARLY_PUBLICATION_LIVE_DEADLINE_BINDING')
            formal[key] = deadline

    def verified_time(view, key, *, v53=False):
        require(type(view) is dict
                and re.fullmatch(r'[0-9a-f]{64}', str(view.get('snapshot_sha256', ''))),
                'EARLY_PUBLICATION_LIVE_SNAPSHOT_BINDING')
        captured = datetime.fromisoformat(view['captured_at_jst'])
        require(captured.utcoffset() == timedelta(hours=9)
                and captured.date().isoformat() == day and captured < formal[key],
                'EARLY_PUBLICATION_LIVE_CAPTURE_BINDING')
        if v53:
            locked = datetime.fromisoformat(view['locked_at_jst'])
            require(locked.utcoffset() == timedelta(hours=9)
                    and captured <= locked < formal[key],
                    'EARLY_PUBLICATION_LIVE_LOCK_BINDING')

    p3 = publication.get('LIVE_BY_RACE')
    readback = publication.get('LIVE_READBACK')
    require(publication.get('STATUS') == 'PASS' and publication.get('READ_ONLY') is True
            and type(p3) is dict and type(readback) is dict
            and readback.get('STATUS') == 'PASS'
            and readback.get('READBACK_RACES') == len(p3)
            and type(readback.get('SNAPSHOT_SHA256')) is dict
            and set(readback['SNAPSHOT_SHA256']) == set(p3)
            and set(p3) <= keys, 'EARLY_PUBLICATION_LIVE_P3_READBACK')
    p3_live = set()
    for key in sorted(set(p3) & set(formal)):
        require(p3[key].get('status') == 'READY'
                and p3[key].get('snapshot_sha256') == readback['SNAPSHOT_SHA256'][key],
                'EARLY_PUBLICATION_LIVE_P3_BINDING')
        verified_time(p3[key], key)
        p3_live.add(key)
    v53_live = set()
    if any((store / 'snapshots/predeadline').glob('abeken-v53-shadow-live-*.json')):
        # This native reader re-proves response bytes, snapshot identity,
        # approved daily version, decisions, and the original MORNING hash.
        _, abeken = relay.feedlib._abeken_public_projection(
            store.parent, 'PREDEADLINE', day, keys)
        for key in sorted(set(abeken) & set(formal)):
            v53 = abeken[key].get('v53')
            if (type(v53) is dict and v53.get('formal_use') is True
                    and type(v53.get('live')) is dict):
                verified_time(v53['live'], key, v53=True)
                v53_live.add(key)
    if not (p3_live or v53_live):
        return original
    from morning_race_isolation_v1 import replace_pinned
    adapted = replace_pinned(original, SAVED_PROJECTOR_SHA, [(
        'or predeadline_availability.public_by_race(OfficialArtifactStore(store), morning_keys))',
        'or predeadline_availability.public_by_race(OfficialArtifactStore(store), morning_keys)\n'
        '             or _verified_independent_live)',
    )], {'_verified_independent_live': True})
    require(adapted is not original, 'EARLY_PUBLICATION_LIVE_PROJECTOR_BINDING')
    print('G11_EARLY_PUBLICATION_STAGE_EVIDENCE=' + json.dumps({
        'p3_live': len(p3_live), 'v53_live': len(v53_live),
        'source': 'VERIFIED_SAVED_NATIVE_READBACK',
    }, sort_keys=True), flush=True)
    return adapted


def completed_night_publication(state, day, authority, implementation, now):
    """Skip stale LIVE delivery only for this authority's immutable NIGHT receipt.

    This is a saved-proof check, not an app GET or a NIGHT regeneration. The
    retained reader re-verifies sources, accounting, phase bindings and LOCKs.
    Missing proof keeps the existing PREDEADLINE stage gate in force.
    """
    root = state / 'daily-runtime' / day
    receipt_path = root / 'daily-operation-complete.json'
    if not receipt_path.exists():
        return False
    require(receipt_path.is_file() and not receipt_path.is_symlink()
            and receipt_path.stat().st_size <= 64000,
            'EARLY_PUBLICATION_NIGHT_RECEIPT_FILE')
    receipt = json.loads(receipt_path.read_bytes())
    require(type(receipt) is dict, 'EARLY_PUBLICATION_NIGHT_RECEIPT_SHAPE')
    unsigned = dict(receipt)
    digest = unsigned.pop('receipt_sha256', None)
    require(digest == implementation.digest(implementation.canonical(unsigned)),
            'EARLY_PUBLICATION_NIGHT_RECEIPT_HASH')
    checkpoint = implementation.read_checkpoint(state, day)
    require(receipt.get('schema') == 'G11_DAILY_SITE_DELIVERY_RECEIPT_V1'
            and receipt.get('operational_date_jst') == day and receipt.get('phase') == 'NIGHT'
            and receipt.get('authority_sha256') == authority['authority_sha256']
            and receipt.get('runtime_source_sha') == authority['runtime_source_sha']
            and receipt.get('canonical_feed_sha256') == checkpoint['feed_sha256']
            and receipt.get('checkpoint_sha256') == checkpoint['checkpoint_sha256']
            and receipt.get('http_success') is True and receipt.get('site_get_readback') == 'PASS'
            and receipt.get('atomic_transaction') == 'PASS'
            and receipt.get('daily_operation_complete') == 'PASS'
            and type(receipt.get('races')) is int and receipt['races'] > 0
            and receipt['races'] == receipt.get('results') == checkpoint['races']
            and receipt.get('pending') == checkpoint['pending'] == 0
            and all(re.fullmatch(r'[0-9a-f]{64}', str(receipt.get(key, '')))
                    for key in ('site_payload_sha256', 'site_get_sha256',
                                'night_transaction_id', 'manifest_sha256')),
            'EARLY_PUBLICATION_NIGHT_RECEIPT_BINDING')
    gate = {'DAILY_AUTHORITY_READBACK': 'PASS', 'MORNING_AUTHORITY_MATCH': 'PASS',
            'PREDEADLINE_AUTHORITY_MATCH': 'PASS', 'NIGHT_AUTHORITY_MATCH': 'PASS',
            'RESULTS': checkpoint['races'], 'RACES': checkpoint['races'], 'PENDING': 0,
            'MORNING_LOCK_MUTATION': 0, 'PREDEADLINE_LOCK_MUTATION': 0,
            'PREDICTION_RECALCULATION': 0, 'FINANCE_VERIFIED': 'PASS',
            'IMMUTABLE_NIGHT_CHECKPOINT': 'PASS', 'ATOMIC_TRANSACTION': 'PASS',
            'SITE_GET_READBACK': 'PASS'}
    require(receipt.get('completion_gate') == gate,
            'EARLY_PUBLICATION_NIGHT_COMPLETION_GATE')
    feed = json.loads((root / 'night-feed.json').read_bytes())
    rows = feed.get('races')
    require(feed.get('operational_date_jst') == day and feed.get('stage') == 'NIGHT'
            and feed.get('status') == 'PASS'
            and feed.get('daily_runtime') == implementation.feed_binding(authority, 'NIGHT')
            and feed.get('source', {}).get('github_sha') == authority['runtime_source_sha']
            and type(rows) is list and len(rows) == checkpoint['races'],
            'EARLY_PUBLICATION_NIGHT_FEED_BINDING')
    deadlines = [datetime.fromisoformat(row['deadline_jst']) for row in rows]
    require(all(deadline.utcoffset() == timedelta(hours=9)
                and deadline.date().isoformat() == day and deadline <= now
                for deadline in deadlines), 'EARLY_PUBLICATION_NIGHT_FUTURE_RACE')
    print('G11_EARLY_PUBLICATION=SKIPPED_VERIFIED_COMPLETED_NIGHT', flush=True)
    return True


def project_saved(state, day, run_id, source, output, *, authority_module=None,
                  relay=None, publisher=None, now=None):
    now = now or datetime.now(JST)
    require(now.date().isoformat() == day and now.utcoffset() == timedelta(hours=9),
            'EARLY_PUBLICATION_CURRENT_DAY')
    metadata = json.loads((state / 'relay-state.json').read_bytes())
    critical = metadata.get('CRITICAL', {})
    require(metadata.get('OPERATIONAL_DATE_JST') == day
            and critical.get('OPERATIONAL_DATE') == day
            and critical.get('CRITICAL_CHECKPOINT_STATUS') == 'PASS'
            and critical.get('LAST_CRITICAL_RUN_ID') == str(run_id),
            'EARLY_PUBLICATION_CHECKPOINT_BINDING')
    # A successful capture is not proof of successful transport. From the
    # short-cycle rollout onward, retry the verified saved projection even if
    # this invocation produced no new LOCKs. No prediction is regenerated.
    if day < '2026-11-08' and not (critical.get('LAST_TIMELY_P3_LIVE_COUNT', 0)
            or critical.get('LAST_TIMELY_V53_COUNT', 0)):
        return False
    if authority_module is None:
        from daily_runtime_bootstrap_v1 import retained_module
        authority_module = retained_module(state, day)
    authority = authority_module.verify_runtime(state, day, source)
    require(authority['authority_sha256'] == critical.get('AUTHORITY_SHA256')
            and authority['runtime_source_sha'] == critical.get('RUNTIME_SOURCE_SHA'),
            'EARLY_PUBLICATION_AUTHORITY_BINDING')
    for phase in ('MORNING', 'PREDEADLINE'):
        authority_module.verify_locks(state, day, phase, exact=True)
    if completed_night_publication(state, day, authority, authority_module, now):
        return False
    if relay is None:
        sys.path.insert(0, str(source))
        from g11.relay import v1 as relay
        from g11.growth_p3.v1_1 import relay_publish_v1 as publisher
    store = state / 'store'
    from predeadline_capture_window_v1 import verified_odds_window
    from optional_display_isolation_v1 import isolate_optional_display
    pipeline = getattr(getattr(relay, 'predeadline_availability', None), '_pipeline', None)
    receipt_window = (verified_odds_window(pipeline())
                      if callable(pipeline) else nullcontext(False))
    # Reopen saved receipts under the same reviewed admission window used to
    # capture them. Unknown source versions keep their original fail-closed
    # gates; source bytes, MORNING plans, LOCKs and cutoffs remain unchanged.
    with official_input_compatibility(), receipt_window, isolate_optional_display(relay):
        morning = relay._morning_projection(store)
        publication = publisher.load_locked_publication(store, morning, now)
        require(publication.get('READ_ONLY') is True, 'EARLY_PUBLICATION_READ_ONLY_REQUIRED')
        # replace_pinned copies globals; copy the isolated optional reader too.
        projector = independent_live_projector(relay, store, day, morning, publication)
        feed = projector(store, run_id=int(run_id),
            github_sha=authority['runtime_source_sha'], current_p3_publication=publication,
            relay_phase='predeadline')
    if (day >= '2026-11-08' and feed.get('operational_date_jst') == day
            and feed.get('status') == 'PASS' and feed.get('stage') == 'MORNING'
            and not critical.get('LAST_TIMELY_P3_LIVE_COUNT', 0)
            and not critical.get('LAST_TIMELY_V53_COUNT', 0)):
        # Before the first LIVE lock, the accepted MORNING feed is already the
        # correct display. Do not create an empty LIVE publication or an error.
        for phase in ('MORNING', 'PREDEADLINE'):
            authority_module.verify_locks(state, day, phase, exact=True)
        return False
    require(feed.get('stage') == 'PREDEADLINE' and feed.get('status') == 'PASS'
            and feed.get('operational_date_jst') == day, 'EARLY_PUBLICATION_FEED_BINDING')
    feed['daily_runtime'] = authority_module.feed_binding(authority, 'PREDEADLINE')
    # Projection may write derived display files, never authoritative snapshots.
    for phase in ('MORNING', 'PREDEADLINE'):
        authority_module.verify_locks(state, day, phase, exact=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(feed, ensure_ascii=False, sort_keys=True,
                                separators=(',', ':'), allow_nan=False) + '\n')
    return True


def _prepared_binding(root, day):
    proof = (root / '.relay-output/publication-state.sha256').read_text().split()
    require(len(proof) == 2 and re.fullmatch('[0-9a-f]{64}', proof[0])
            and proof[1] == '.relay-cache/state.tgz.gpg', 'PREPARED_CHECKPOINT_DIGEST')
    return {'day': day, 'run': os.environ['GITHUB_RUN_ID'], 'attempt': os.environ['GITHUB_RUN_ATTEMPT'],
            'cipher_sha256': proof[0],
            'metadata_sha256': hashlib.sha256((root / '.relay-state/relay-state.json').read_bytes()).hexdigest()}


def prepare_feed(root, day):
    # Full encryption is complete. Only the encrypted file is transferred while
    # the original verifier reads sealed data and builds the derived view.
    dest = root / '.relay-output'
    binding = _prepared_binding(root, day)
    prepared = dest / 'critical-prepared-feed.json'
    if not project_saved(root / '.relay-state', day, binding['run'], root / '.g11-private', prepared):
        return
    require(binding == _prepared_binding(root, day), 'PREPARED_AUTHORITY_CHANGED')
    binding['feed_sha256'] = hashlib.sha256(prepared.read_bytes()).hexdigest()
    temp = dest / 'critical-prepared-proof.tmp'
    temp.write_text(json.dumps(binding))
    temp.replace(dest / 'critical-prepared-proof.json')


def use_prepared_feed(root, day, output):
    # Only invoked by the primary send after the exact remote checkpoint was
    # downloaded and verified. Later publications use their own live state.
    if os.environ.get('CRITICAL_CHECKPOINT_VERIFIED') != 'true' or datetime.now(JST).date().isoformat() != day:
        return False
    dest = root / '.relay-output'
    try:
        proof = json.loads((dest / 'critical-prepared-proof.json').read_bytes())
        data = (dest / 'critical-prepared-feed.json').read_bytes()
        expected = {**_prepared_binding(root, day), 'feed_sha256': hashlib.sha256(data).hexdigest()}
        if proof != expected:
            return False
        feed = json.loads(data)
        if (feed.get('operational_date_jst') != day or feed.get('stage') != 'PREDEADLINE'
                or feed.get('status') != 'PASS'):
            return False
    except (ValueError, OSError, KeyError, TypeError, RuntimeError):
        return False
    output.write_bytes(data)
    print('G11_CRITICAL_FEED_PREPARATION=VERIFIED_CACHE_HIT', flush=True)
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--day', required=True)
    parser.add_argument('--select-restore', action='store_true')
    parser.add_argument('--prepare-feed', action='store_true')
    parser.add_argument('--use-prepared', action='store_true')
    parser.add_argument('--prewarm', action='store_true')
    parser.add_argument('--phase', default='')
    parser.add_argument('--continuation-delay', action='store_true')
    parser.add_argument('--rescue-continuation-delay', action='store_true')
    args = parser.parse_args()
    if args.select_restore:
        return select_restore(args.day)
    if args.prewarm:
        return prewarm_worker(args.day, args.phase)
    root = Path.cwd()
    if args.prepare_feed:
        return prepare_feed(root, args.day)
    if args.rescue_continuation_delay:
        result = json.loads((root / '.relay-output/result.json').read_bytes())
        feed = json.loads((root / '.relay-output/feed.json').read_bytes())
        print(rescue_continuation_delay(result, feed, args.day,
                                       os.environ['GITHUB_RUN_ID'], datetime.now(JST)))
        return
    if args.continuation_delay:
        result = json.loads((root / '.relay-output/result.json').read_bytes())
        print(continuation_delay(result, args.day, os.environ['GITHUB_RUN_ID'], datetime.now(JST)))
        return
    output = root / '.relay-output/critical-feed.json'
    require(os.environ.get('CRITICAL_CHECKPOINT_VERIFIED') == 'true',
            'EARLY_PUBLICATION_PERSISTENCE_REQUIRED')
    from predeadline_input_health_v1 import publish_pause_if_observed
    publish_pause_if_observed(root, args.day)
    ready = args.use_prepared and use_prepared_feed(root, args.day, output)
    if not ready:
        ready = project_saved(root / '.relay-state', args.day, os.environ['GITHUB_RUN_ID'],
                              root / '.g11-private', output)
    if not ready:
        print('G11_EARLY_PUBLICATION=NO_LIVE_PUBLICATION_REQUIRED')
        return
    request = Request(os.environ['ACTIONS_ID_TOKEN_REQUEST_URL'] + '&audience=' +
        os.environ['G11_OIDC_AUDIENCE'], headers={
            'Authorization': 'bearer ' + os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN'],
            'Accept': 'application/json'})
    with urlopen(request, timeout=20) as response:
        token = json.loads(response.read())['value']
    os.environ['G11_SYNC_OIDC'] = token
    from daily_runtime_delivery_v1 import deliver, ORIGIN
    require(os.environ['G11_APP_ORIGIN'] == ORIGIN, 'EARLY_PUBLICATION_ORIGIN')
    warm = os.environ.get('G11_WARM_PUBLICATION') == 'true'
    receipt = deliver(root / '.relay-state', output, ORIGIN, include_optional_inputs=False,
                      **({'critical_sequence_offset': 1} if warm else {}))
    feed = json.loads(output.read_bytes())
    critical = json.loads((root / '.relay-state/relay-state.json').read_bytes())['CRITICAL']
    report_publication_timing(publication_timing(feed, critical, datetime.now(JST)))
    # HOME's condition comparison also needs the saved odds proof. Send just
    # current, newly saved races now; the regular path retains the full history.
    fresh = fresh_input_source(feed, critical, datetime.now(JST))
    if fresh['races']:
        from selective_alert_input_v1 import deliver_optional
        deliver_optional(root / '.relay-state/store', fresh, ORIGIN, token=token)
    from predeadline_input_health_v1 import report_p3_after_delivery
    report_p3_after_delivery(root, feed)
    print('G11_EARLY_PUBLICATION=' + json.dumps({
        'status': receipt['site_get_readback'], 'at': datetime.now(JST).isoformat(),
        'authority': receipt['authority_sha256'], 'prediction_recalculation': 0}, sort_keys=True))


def fresh_input_source(feed, critical, now):
    keys = set(critical.get('V53_LOCKED_BEFORE_DEADLINE', []))
    rows = [row for row in feed['races'] if row['key'] in keys
            and datetime.fromisoformat(row['deadline_jst']) > now]
    return {**feed, 'races': rows}


def publication_timing(feed, critical, verified_at):
    """Server readback completion is an upper bound, never device arrival."""
    require(verified_at.utcoffset() == timedelta(hours=9)
            and verified_at.date().isoformat() == feed['operational_date_jst'],
            'PUBLICATION_TIMING_DAY')
    keys = set(critical.get('V53_LOCKED_BEFORE_DEADLINE', [])) | set(
        critical.get('PREDICTION_LOCKED_BEFORE_DEADLINE', []))
    rows = []
    for row in feed['races']:
        if row['key'] not in keys:
            continue
        deadline = datetime.fromisoformat(row['deadline_jst'])
        require(deadline.utcoffset() == timedelta(hours=9), 'PUBLICATION_TIMING_ZONE')
        remaining = round((deadline - verified_at).total_seconds(), 3)
        rows.append({'race': row['key'], 'deadline': deadline.isoformat(),
                     'readback_remaining_seconds': remaining,
                     'five_minute_target_met': remaining >= 300,
                     'before_deadline': remaining > 0})
    return {'verified_at': verified_at.isoformat(), 'scope': 'SERVER_READBACK_NOT_DEVICE_ARRIVAL',
            'races': rows}


def report_publication_timing(timing):
    print('G11_PREDEADLINE_PUBLICATION_TIMING=' + json.dumps(timing, sort_keys=True), flush=True)
    for row in timing['races']:
        if row['five_minute_target_met']:
            continue
        remaining = row['readback_remaining_seconds']
        if row['before_deadline']:
            timing_label = f'{remaining:.1f}s before cutoff (under the five-minute target)'
        elif remaining < 0:
            timing_label = f'{-remaining:.1f}s after cutoff'
        else:
            timing_label = 'at cutoff'
        print(f"::warning::PREDEADLINE {row['race']} server readback {timing_label}", flush=True)


def continuation_delay(result, day, run_id, now):
    """Retain 45s after capture completion, crediting persistence/publication."""
    c = result.get('CRITICAL', {})
    require(c.get('OPERATIONAL_DATE') == day == now.date().isoformat()
            and c.get('CRITICAL_CHECKPOINT_STATUS') == 'PASS'
            and c.get('LAST_CRITICAL_RUN_ID') == str(run_id)
            and now.utcoffset() == timedelta(hours=9), 'CONTINUATION_SPACING_BINDING')
    at = datetime.fromisoformat(c['CRITICAL_CAPTURE_AT'])
    require(at.utcoffset() == timedelta(hours=9) and at.date() == now.date()
            and at <= now, 'CONTINUATION_SPACING_CLOCK')
    return max(0, round(45 - (now - at).total_seconds(), 3))


def rescue_continuation_delay(result, feed, day, run_id, now):
    """Credit time already spent saving/publishing this run's rescue capture."""
    require(result.get('STATUS') == 'SAME_DAY_RESCUE_COMPLETE'
            and result.get('PHASE') == 'PREDEADLINE_RESCUE'
            and result.get('OPERATIONAL_DATE_JST') == day
            and result.get('FEED_EMITTED') is True
            and result.get('MORNING_IMMUTABLE') is True
            and feed.get('operational_date_jst') == day
            and feed.get('stage') == 'MORNING' and feed.get('status') == 'PASS'
            and feed.get('SNAPSHOT_CLASS') == 'SAME_DAY_RESCUE_BOUNDARY'
            and str(feed.get('source', {}).get('run_id')) == str(run_id),
            'RESCUE_CONTINUATION_BINDING')
    return continuation_delay({'CRITICAL': {
        'OPERATIONAL_DATE': day, 'CRITICAL_CHECKPOINT_STATUS': 'PASS',
        'LAST_CRITICAL_RUN_ID': str(run_id),
        'CRITICAL_CAPTURE_AT': feed['generated_at_jst']}}, day, run_id, now)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Never render a transport exception containing a signed request/token.
        code = str(exc)
        print('G11_EARLY_PUBLICATION=HELD REASON=' +
              (code if code.isupper() and len(code) < 160 else type(exc).__name__), file=sys.stderr)
        raise SystemExit(1) from None
