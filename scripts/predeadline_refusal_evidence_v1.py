"""Read-only response/refusal evidence. No new HTTP request, body read or retry.

G11_OFFICIAL_RESPONSE describes transport only: HTTP 200 does not certify parsed
input or a prediction LOCK. Existing refusal/ledger decisions remain separate.
"""
from contextlib import contextmanager
import hashlib
import json
import math
import os
import re
from datetime import datetime, timezone
from threading import Thread
from time import monotonic
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit


def emit(fields):
    print('G11_OFFICIAL_REFUSAL=' + json.dumps(fields, sort_keys=True), flush=True)


# Diagnostic-only allowlist. Never emit authentication, cookies, response URLs,
# arbitrary headers, or raw denial text. Trace IDs let the source operator
# locate an already completed request without sending a diagnostic request.
DIAGNOSTIC_HEADERS = (
    "Server", "Via", "Content-Type", "Retry-After", "X-Cache",
    "CF-Ray", "X-Amz-Cf-Id", "X-Amz-Cf-Pop", "X-Request-Id",
)
RUN_CONTEXT_FIELDS = ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_JOB", "RUNNER_ENVIRONMENT")


def _split_timing(value, delimiter):
    """Split HTTP list/parameters without treating quoted descriptions as data."""
    result, start, quoted, escaped = [], 0, False, False
    for index, char in enumerate(value):
        if quoted and escaped:
            escaped = False
        elif quoted and char == '\\':
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char == delimiter and not quoted:
            result.append(value[start:index].strip())
            start = index + 1
    if quoted or escaped:
        return None
    result.append(value[start:].strip())
    return result


def server_timing(headers):
    """Keep only bounded, unambiguous numeric edge/origin durations in ms."""
    if headers is None:
        return None, {}  # No response headers were received/readable.
    all_values = getattr(headers, 'get_all', None)
    values = all_values('Server-Timing') if callable(all_values) else [headers.get('Server-Timing')]
    if values is None or values == [None]:
        return False, {}
    if (not isinstance(values, (list, tuple)) or len(values) > 32
            or any(not isinstance(value, str) for value in values)
            or sum(len(value) for value in values) + max(0, len(values)-1)*2 > 4096):
        return True, {}
    raw, present = ', '.join(values), True
    if (not raw.isascii()
            or any(ord(char) < 32 or ord(char) == 127 for char in raw)):
        return present, {}
    metrics = _split_timing(raw, ',')
    if metrics is None or len(metrics) > 32:
        return present, {}
    result, seen = {}, set()
    for metric in metrics:
        parts = _split_timing(metric, ';')
        if parts is None or parts[0].lower() not in ('edge', 'origin'):
            continue
        name = parts[0].lower()
        if name in seen:
            result.pop(name, None)
            continue
        seen.add(name)
        duration = [part.partition('=')[2].strip() for part in parts[1:]
                    if part.partition('=')[0].strip().lower() == 'dur']
        if len(duration) != 1:
            continue
        literal = duration[0]
        if len(literal) >= 2 and literal[0] == literal[-1] == '"':
            literal = literal[1:-1]
        if not re.fullmatch(r'(?:\d+(?:\.\d+)?|\.\d+)', literal):
            continue
        value = float(literal)
        if math.isfinite(value) and 0 <= value <= 86_400_000:
            result[name] = value
    return present, result


def _source_from_request(request):
    """Derive a safe public source ID; never emit a URL or arbitrary query."""
    url = request if isinstance(request, str) else getattr(request, 'full_url', '')
    if not isinstance(url, str) or len(url) > 512:
        return None
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.netloc != 'www.boatrace.jp'
            or parsed.fragment or parsed.path not in
            ('/owpc/pc/race/beforeinfo', '/owpc/pc/race/odds3t')):
        return None
    query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=3)
    if set(query) != {'hd', 'jcd', 'rno'} or any(len(v) != 1 for v in query.values()):
        return None
    day, venue, race = (query[key][0] for key in ('hd', 'jcd', 'rno'))
    if (not re.fullmatch(r'\d{8}', day) or not re.fullmatch(r'\d{1,2}', venue)
            or not re.fullmatch(r'\d{1,2}', race)
            or not 1 <= int(venue) <= 24 or not 1 <= int(race) <= 12):
        return None
    datetime.strptime(day, '%Y%m%d')
    return f'{parsed.path.rsplit("/", 1)[-1]}-{day}-{int(venue):02d}-{int(race):02d}'


def _response_event(context, completed_tick, response, *, failed=False):
    # Diagnostics cannot replace the original response/exception or inspect a body.
    try:
        source, started, tick = context
        duration = (completed_tick - tick) * 1000
        if not math.isfinite(duration) or duration < 0:
            return
        status = response.code if isinstance(response, HTTPError) else (
            'TIMEOUT' if failed and isinstance(response, TimeoutError) else
            'NETWORK_ERROR' if failed else int(response.status))
        try:
            present, timing = server_timing(getattr(response, 'headers', None))
        except Exception:
            present, timing = None, {}
        fields = {'collector': 'relay-predeadline', 'source': source,
                  'started': started,
                  'headers_or_error_at': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                  'status': status, 'header_wait_ms': round(duration, 3),
                  'server_timing_present': present, 'server_timing_ms': timing,
                  'run_context': run_context()}
        print('G11_OFFICIAL_RESPONSE=' + json.dumps(fields, sort_keys=True), flush=True)
    except Exception:
        pass


@contextmanager
def observe_responses(client, *, clock=None):
    """Temporarily observe the existing opener; cached receipts make no event."""
    original = getattr(client, 'opener', None)
    if not callable(original) or getattr(original, '_g11_response_evidence', False) is True:
        yield
        return
    clock = clock or monotonic

    def observed(*args, **kwargs):
        context = None
        try:
            request = args[0] if args else kwargs.get('url')
            source = _source_from_request(request)
            if source:
                context = (source, datetime.now(timezone.utc).isoformat(timespec='milliseconds'), clock())
        except Exception:
            pass
        try:
            response = original(*args, **kwargs)
        except Exception as exc:
            try:
                completed = clock()
                if context is not None:
                    _response_event(context, completed, exc, failed=True)
            except Exception:
                pass
            raise
        try:
            completed = clock()
            if context is not None:
                _response_event(context, completed, response)
        except Exception:
            pass
        return response

    observed._g11_response_evidence = True
    client.opener = observed
    try:
        yield
    finally:
        client.opener = original


def response_metadata(headers):
    result = {}
    for name in DIAGNOSTIC_HEADERS:
        value = headers.get(name)
        if not isinstance(value, str):
            continue
        value = value.strip()
        # Keep logs bounded and single-line. Reject suspicious values entirely.
        if (not value or len(value) > 256 or not value.isascii()
                or any(ord(c) < 32 or ord(c) == 127 for c in value)
                or re.search(r"(?i)(authorization|set-cookie|bearer\s|basic\s|(?:token|signature|password)=)", value)):
            continue
        result[name.lower()] = value
    return result


def run_context():
    return {name.lower(): value for name in RUN_CONTEXT_FIELDS
            if re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value := os.environ.get(name, ""))}


def body_evidence(source_id, error):
    # The original GET already returned a denial. Only inspect one bounded read
    # on a background thread, after the normal ledger has recorded the error.
    try:
        raw_prefix = error.read1(4096)
        prefix = raw_prefix.lower()
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
        emit({'source': source_id, 'status': error.code, 'body_hint': label,
              'response_prefix_bytes': len(raw_prefix),
              'response_prefix_sha256': hashlib.sha256(raw_prefix).hexdigest()})
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
              'challenge_header': headers.get('cf-mitigated', '').lower() == 'challenge',
              'response_headers': response_metadata(headers),
              'run_context': run_context()})
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
