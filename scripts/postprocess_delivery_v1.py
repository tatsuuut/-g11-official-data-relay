"""Resume one durable Site postprocessing stage after saved formal readback.

Only the Site's existing accepted data is used. No official input, inference,
prediction, daily authority, LOCK or canonical state is written by this caller.
The next normal LIVE resumes pending stages; ambiguous replies are not retried.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import threading
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ORIGIN = 'https://g11-race-lab.higatatsunori2.chatgpt.site'
JST = timezone(timedelta(hours=9))
STAGES = {'PRIOR_NIGHT', 'LIFECYCLE', 'PROMOTION_ABEKEN', 'PROMOTION_WILD', 'INTRADAY_RESULTS'}
STATUSES = {'COMPLETE', 'PENDING', 'BUSY', 'HELD'}
MAX_BODY = 8192
# Allow the unchanged 24-second result refresh budget plus OIDC/DB overhead.
NETWORK_WALL_SECONDS = 35


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def require(ok, code):
    if not ok:
        raise ValueError(code)


def read_json(path, maximum=MAX_BODY):
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= maximum,
            'LOCAL_EVIDENCE_UNAVAILABLE')
    return json.loads(path.read_bytes())


def verified_publication(state, day, authority_sha, now):
    """Read only canonical hashes and a retained successful formal receipt.

    An idle LIVE can resume pending work with an earlier verified receipt. The
    workflow's verified checkpoint and expected authority still bind this day.
    No private implementation or prediction model is imported here.
    """
    require(now.utcoffset() == timedelta(hours=9) and day == now.date().isoformat()
            and day >= '2026-11-23' and re.fullmatch('[a-f0-9]{64}', authority_sha or ''),
            'DAY_OR_AUTHORITY_UNVERIFIED')
    root = Path(state) / 'daily-runtime' / day
    authority = read_json(root / 'authority.json', maximum=512000)
    unsigned = dict(authority)
    sha = unsigned.pop('authority_sha256', None)
    require(sha == authority_sha == digest(compact(unsigned))
            and authority.get('schema') == 'G11_DAILY_RUNTIME_AUTHORITY_V1'
            and authority.get('operational_date_jst') == day
            and authority.get('immutable') is True
            and re.fullmatch('[a-f0-9]{40}', str(authority.get('runtime_source_sha', ''))),
            'AUTHORITY_BINDING_UNVERIFIED')
    # The verified encrypted state owns these receipts. Do not promote an
    # unverified HTTP200 or a MORNING-only receipt to formal LIVE completion.
    for path in (root / 'delivery/PREDEADLINE').glob('*.json'):
        try:
            receipt = read_json(path)
            unsigned = dict(receipt)
            receipt_sha = unsigned.pop('receipt_sha256', None)
            if (receipt_sha == digest(compact(unsigned))
                    and receipt.get('schema') == 'G11_DAILY_SITE_DELIVERY_RECEIPT_V1'
                    and receipt.get('operational_date_jst') == day
                    and receipt.get('phase') == 'PREDEADLINE'
                    and receipt.get('authority_sha256') == authority_sha
                    and receipt.get('runtime_source_sha') == authority['runtime_source_sha']
                    and receipt.get('http_success') is True
                    and receipt.get('site_get_readback') == 'PASS'
                    and re.fullmatch('[a-f0-9]{64}', str(receipt.get('site_get_sha256', '')))
                    and re.fullmatch('[a-f0-9]{64}', str(receipt.get('site_payload_sha256', '')))
                    and path.stem == receipt['site_payload_sha256']):
                return
        except (ValueError, OSError, TypeError):
            continue
    raise ValueError('FORMAL_READBACK_UNVERIFIED')


def oidc_token():
    request = Request(os.environ['ACTIONS_ID_TOKEN_REQUEST_URL'] + '&' + urlencode({
        'audience': os.environ['G11_OIDC_AUDIENCE']}), headers={
        'Authorization': 'bearer ' + os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN'],
        'Accept': 'application/json'})
    with urlopen(request, timeout=5) as response:
        raw = response.read(65537)
    require(len(raw) <= 65536, 'OIDC_RESPONSE_INVALID')
    token = json.loads(raw).get('value')
    require(isinstance(token, str) and 1 <= len(token) <= 16000
            and '\r' not in token and '\n' not in token, 'OIDC_RESPONSE_INVALID')
    return token


@contextmanager
def network_deadline():
    require(threading.current_thread() is threading.main_thread()
            and hasattr(signal, 'setitimer') and signal.getitimer(signal.ITIMER_REAL)[0] == 0,
            'POSTPROCESS_DEADLINE_UNAVAILABLE')
    previous = signal.getsignal(signal.SIGALRM)
    def expired(_signum, _frame):
        raise TimeoutError('POSTPROCESS_NETWORK_WALL_TIMEOUT')
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, NETWORK_WALL_SECONDS)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def post_stage(day, token, runner=subprocess.run):
    """One authenticated empty POST, no redirect/retry or official access.

    Use the established curl Site transport. Send credentials on stdin, never
    in argv, files, exception text or logs. curl bounds the entire response.
    """
    with tempfile.TemporaryDirectory() as temporary:
        target = Path(temporary) / 'response.json'
        command = ['curl', '--silent', '--show-error', '--max-time', '30',
            '--max-filesize', str(MAX_BODY), '--config', '-', '--request', 'POST',
            '--output', str(target), '--write-out', '%{http_code}',
            ORIGIN + '/api/g11-postprocess?' + urlencode({'date': day})]
        config = 'header = ' + json.dumps('Authorization: Bearer ' + token) + '\n'
        result = runner(command, input=config, text=True, capture_output=True, timeout=31)
        require(result.returncode == 0, 'POST_RESPONSE_UNVERIFIED')
        require(result.stdout == '200', 'POST_HTTP_UNVERIFIED')
        return read_json(target)


def validate_ack(value, day):
    require(isinstance(value, dict) and value.get('date') == day
            and value.get('status') in STATUSES, 'POST_BINDING_UNVERIFIED')
    pending, processed = value.get('pending'), value.get('processed')
    require(isinstance(pending, list) and len(pending) <= len(STAGES)
            and all(isinstance(x, str) and x in STAGES for x in pending)
            and len(pending) == len(set(pending)), 'POST_PENDING_UNVERIFIED')
    require(processed is None or (isinstance(processed, dict)
            and processed.get('stage') in STAGES
            and isinstance(processed.get('status'), str)
            and re.fullmatch('[A-Z0-9_]{1,40}', processed['status'])),
            'POST_STAGE_UNVERIFIED')
    require(value['status'] != 'COMPLETE' or not pending, 'POST_COMPLETION_UNVERIFIED')
    result = {'status': value['status'], 'date': day, 'pending': pending,
        'processed': ({'stage': processed['stage'], 'status': processed['status']}
                      if processed is not None else None),
        'postprocessing_complete': value['status'] == 'COMPLETE',
        'formal_publication_mutated': False}
    if processed is not None and re.fullmatch('[A-Z0-9_]{1,80}', str(processed.get('reason', ''))):
        result['processed']['reason'] = processed['reason']
    if value.get('retry_at_jst'):
        retry = datetime.fromisoformat(value['retry_at_jst'])
        require(retry.utcoffset() == timedelta(hours=9), 'POST_RETRY_TIME_UNVERIFIED')
        result['retry_at_jst'] = retry.isoformat()
    return result


def resume(state, day, *, now=None, token_provider=oidc_token, sender=post_stage):
    result = {'status': 'HELD', 'date': day, 'reason': 'POST_RESPONSE_UNVERIFIED',
        'pending': None, 'postprocessing_complete': False, 'formal_publication_mutated': False}
    try:
        require(os.environ.get('G11_APP_ORIGIN') == ORIGIN
                and os.environ.get('POSTPROCESS_CHECKPOINT_VERIFIED') == 'true',
                'POSTPROCESS_GATE_UNVERIFIED')
        verified_publication(Path(state), day, os.environ.get('POSTPROCESS_AUTHORITY_SHA'),
                             now or datetime.now(JST))
        with network_deadline():
            result = validate_ack(sender(day, token_provider()), day)
    except Exception as exc:
        # This optional work must not erase the formal readback result. A lost
        # response is pending, never complete; the next regular run resumes it.
        if isinstance(exc, ValueError) and re.fullmatch('[A-Z0-9_]{1,80}', str(exc)):
            result['reason'] = str(exc)
    print('G11_POSTPROCESS=' + json.dumps(result, sort_keys=True), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state-root', type=Path, required=True)
    parser.add_argument('--day', required=True)
    args = parser.parse_args()
    resume(args.state_root, args.day)


if __name__ == '__main__':
    main()
