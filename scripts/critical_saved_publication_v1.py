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
from urllib.request import Request, urlopen

JST = timezone(timedelta(hours=9))


def require(ok, code):
    if not ok:
        raise RuntimeError(code)


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
    args = parser.parse_args()
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
    print('G11_EARLY_PUBLICATION=' + json.dumps({
        'status': receipt['site_get_readback'], 'at': datetime.now(JST).isoformat(),
        'authority': receipt['authority_sha256'], 'prediction_recalculation': 0}, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Never render a transport exception containing a signed request/token.
        code = str(exc)
        print('G11_EARLY_PUBLICATION=HELD REASON=' +
              (code if code.isupper() and len(code) < 160 else type(exc).__name__), file=sys.stderr)
        raise SystemExit(1) from None
