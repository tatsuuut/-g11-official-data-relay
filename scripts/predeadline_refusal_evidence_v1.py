"""Read-only evidence for official predeadline refusals. No HTTP client or retry."""
import json
import re
from datetime import datetime
from threading import Thread
from urllib.error import HTTPError


def emit(fields):
    print('G11_OFFICIAL_REFUSAL=' + json.dumps(fields, sort_keys=True), flush=True)


def body_evidence(source_id, error):
    # The original GET already returned a denial. Only inspect one bounded read
    # on a background thread, after the normal ledger has recorded the error.
    try:
        prefix = error.read1(4096).lower()
        if b'too many requests' in prefix or b'rate limit exceeded' in prefix:
            label = 'RATE_LIMIT_MESSAGE'
        elif b'not available in your country' in prefix:
            label = 'REGION_MESSAGE'
        elif b'verify you are human' in prefix:
            label = 'HUMAN_CHECK_MESSAGE'
        elif b'access denied' in prefix or b'forbidden' in prefix:
            label = 'GENERIC_DENIAL'
        else:
            label = 'NO_RECOGNIZED_MESSAGE'
        emit({'source': source_id, 'status': error.code, 'body_hint': label})
    except Exception:
        emit({'source': source_id, 'status': error.code, 'body_hint': 'UNAVAILABLE'})


def observe(source_id, exc, ledger=None):
    try:
        error = exc.__cause__
        if not isinstance(error, HTTPError) or error.code not in (403, 429):
            return
        if not re.fullmatch(r'(beforeinfo|odds3t)-\d{8}-\d{2}-\d{2}', source_id):
            return
        headers = error.headers or {}
        emit({'source': source_id, 'status': error.code,
              'retry_after_present': bool(headers.get('Retry-After')),
              'challenge_header': headers.get('cf-mitigated', '').lower() == 'challenge'})
        Thread(target=body_evidence, args=(source_id, error), daemon=True).start()
        if ledger is not None:
            incidents = incident_windows(ledger.events())
            if incidents and incidents[-1]['source'] == source_id:
                emit({'request_window': incidents[-1]})
    except Exception:
        # Reporting must not change the original exception or acquisition decision.
        pass


def incident_windows(events):
    starts = {}
    history = []
    incidents = []
    for event in events:
        data = event['PAYLOAD']
        request = data['REQUEST_ID']
        if event['TYPE'] == 'REQUEST_STARTED':
            starts[request] = (datetime.fromisoformat(event['AT_JST']), data)
            continue
        at, start = starts.pop(request)
        if start['HOST'] != 'www.boatrace.jp':
            continue
        source = start['SOURCE_ID']
        outcome = data['OUTCOME']
        if outcome in ('HTTP_403', 'HTTP_429'):
            elapsed = [(at-prev).total_seconds() for prev, _, _ in history]
            recent = history[-1] if history else None
            incidents.append({'source': source, 'at_jst': event['AT_JST'], 'outcome': outcome,
                'previous_source': recent[1] if recent else None,
                'previous_outcome': recent[2] if recent else None,
                'seconds_since_previous_start': elapsed[-1] if elapsed else None,
                'prior_requests_60s': sum(0 <= s < 60 for s in elapsed),
                'prior_requests_10m': sum(0 <= s < 600 for s in elapsed),
                'previous_requests_same_source': sum(s == source for _,s,_ in history),
                'retry_after_present': bool(data.get('RETRY_AFTER'))})
        history.append((at, source, outcome))
    return incidents


if __name__ == '__main__':
    import argparse
    import sys
    from pathlib import Path
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--state-root', required=True, type=Path)
    parser.add_argument('--day', required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source.resolve()))
    from g11.canonical.official_pipeline import OfficialArtifactStore
    store = OfficialArtifactStore(args.state_root.resolve() / 'store')
    if store.manifest()['OPERATIONAL_DATE_JST'] != args.day:
        raise ValueError('REFUSAL_AUDIT_DATE')
    state = store.ledger.state()
    print('G11_LEDGER_REFUSAL_AUDIT=' + json.dumps({
        'date': args.day, 'request_count': state['REQUEST_COUNT'],
        'general_403_count': state['GENERAL_HTTP_403_COUNT'], 'stopped': state['STOPPED'],
        'incidents': incident_windows(store.ledger.events())}, sort_keys=True))
