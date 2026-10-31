"""Publish already persisted daily LOCKs before optional PREDEADLINE work.

This adapter invokes the frozen read-only projector and existing authenticated
transport. It never acquires inputs, calls inference, seals or changes a LOCK.
The workflow must verify the encrypted critical checkpoint before invoking it.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import time
from urllib.request import Request, urlopen

JST = timezone(timedelta(hours=9))


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
    if not (critical.get('LAST_TIMELY_P3_LIVE_COUNT', 0)
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
    if relay is None:
        sys.path.insert(0, str(source))
        from g11.relay import v1 as relay
        from g11.growth_p3.v1_1 import relay_publish_v1 as publisher
    store = state / 'store'
    publication = publisher.load_locked_publication(store, relay._morning_projection(store), now)
    require(publication.get('READ_ONLY') is True, 'EARLY_PUBLICATION_READ_ONLY_REQUIRED')
    feed = relay.build_feed(store, run_id=int(run_id),
        github_sha=authority['runtime_source_sha'], current_p3_publication=publication,
        relay_phase='predeadline')
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--day', required=True)
    parser.add_argument('--select-restore', action='store_true')
    args = parser.parse_args()
    if args.select_restore:
        return select_restore(args.day)
    root = Path.cwd()
    output = root / '.relay-output/critical-feed.json'
    require(os.environ.get('CRITICAL_CHECKPOINT_VERIFIED') == 'true',
            'EARLY_PUBLICATION_PERSISTENCE_REQUIRED')
    ready = project_saved(root / '.relay-state', args.day, os.environ['GITHUB_RUN_ID'],
                          root / '.g11-private', output)
    if not ready:
        print('G11_EARLY_PUBLICATION=NO_NEW_LOCKS')
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
    receipt = deliver(root / '.relay-state', output, ORIGIN, include_optional_inputs=False)
    # HOME's condition comparison also needs the saved odds proof. Send just
    # current, newly saved races now; the regular path retains the full history.
    feed = json.loads(output.read_bytes())
    critical = json.loads((root / '.relay-state/relay-state.json').read_bytes())['CRITICAL']
    fresh = fresh_input_source(feed, critical, datetime.now(JST))
    if fresh['races']:
        from selective_alert_input_v1 import deliver_optional
        deliver_optional(root / '.relay-state/store', fresh, ORIGIN, token=token)
    print('G11_EARLY_PUBLICATION=' + json.dumps({
        'status': receipt['site_get_readback'], 'at': datetime.now(JST).isoformat(),
        'authority': receipt['authority_sha256'], 'prediction_recalculation': 0}, sort_keys=True))


def fresh_input_source(feed, critical, now):
    keys = set(critical.get('V53_LOCKED_BEFORE_DEADLINE', []))
    rows = [row for row in feed['races'] if row['key'] in keys
            and datetime.fromisoformat(row['deadline_jst']) > now]
    return {**feed, 'races': rows}


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Never render a transport exception containing a signed request/token.
        code = str(exc)
        print('G11_EARLY_PUBLICATION=HELD REASON=' +
              (code if code.isupper() and len(code) < 160 else type(exc).__name__), file=sys.stderr)
        raise SystemExit(1) from None
