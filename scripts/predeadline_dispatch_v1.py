"""Bounded PREDEADLINE dispatch with read-before-retry reconciliation.

Only workflow scheduling metadata is read or written. Predictions, saved inputs,
models, settlement and cache contents are never opened. The existing workflow
concurrency group remains the serialization boundary for runtime state writes.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

REPOSITORY = 'tatsuuut/-g11-official-data-relay'
WORKFLOW = 'g11-free-runner.yml'
ACTIVE = ('queued', 'in_progress', 'waiting', 'pending', 'requested')
RETRYABLE = {0, 408, 429, 500, 502, 503, 504}
JST = timezone(timedelta(hours=9))


class DispatchError(RuntimeError):
    """Safe fixed error codes only; never serialize a request or credential."""


class ApiError(DispatchError):
    def __init__(self, status):
        self.status = status
        super().__init__('GITHUB_HTTP_' + str(status))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, token, *, clock=time.monotonic, limit=75):
        if not token:
            raise DispatchError('TOKEN_MISSING')
        self.token, self.clock = token, clock
        self.until = clock() + limit
        self.opener = build_opener(NoRedirect())

    def request(self, method, path, body=None):
        remaining = self.until - self.clock()
        if remaining <= 0:
            raise DispatchError('TIME_BUDGET_EXHAUSTED')
        if not path.startswith('/actions/'):
            raise DispatchError('API_PATH')
        request = Request('https://api.github.com/repos/' + REPOSITORY + path,
                          method=method,
                          data=None if body is None else json.dumps(body).encode(),
                          headers={'Authorization': 'Bearer ' + self.token,
                                   'Accept': 'application/vnd.github+json',
                                   'Content-Type': 'application/json',
                                   'Cache-Control': 'no-cache',
                                   'X-GitHub-Api-Version': '2022-11-28'})
        try:
            with self.opener.open(request, timeout=min(6, remaining)) as response:
                status = response.status
                raw = response.read(2_000_001)
        except HTTPError as exc:
            raise ApiError(exc.code) from None
        except (URLError, TimeoutError, OSError):
            raise ApiError(0) from None
        if len(raw) > 2_000_000:
            raise DispatchError('RESPONSE_SIZE')
        if method == 'POST':
            if status not in (201, 204):
                raise DispatchError('POST_NOT_ACCEPTED')
            return None
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise DispatchError('INVALID_JSON') from None
        if not isinstance(value, dict):
            raise DispatchError('INVALID_RESPONSE')
        return value


def checked_get(client, path, sleep):
    for attempt in range(3):
        try:
            return client.request('GET', path)
        except ApiError as exc:
            if exc.status not in RETRYABLE or attempt == 2:
                raise
            sleep(1 + attempt)
    raise DispatchError('UNREACHABLE')


def active_successor(client, day, current_id, current_number, mode, sleep):
    """Read all active pages, fail closed if enumeration is unavailable."""
    for status in ACTIVE:
        for page in range(1, 6):
            query = urlencode({'branch': 'main', 'event': 'workflow_dispatch',
                               'status': status, 'per_page': 100, 'page': page})
            data = checked_get(client, '/actions/workflows/' + WORKFLOW + '/runs?' + query, sleep)
            rows, total = data.get('workflow_runs'), data.get('total_count')
            if not isinstance(rows, list) or type(total) is not int or total < 0:
                raise DispatchError('RUN_LIST_SHAPE')
            if len(rows) > 100 or (not rows and total > 100 * (page - 1)):
                raise DispatchError('RUN_LIST_INCOMPLETE')
            for row in rows:
                if not isinstance(row, dict):
                    raise DispatchError('RUN_ROW_SHAPE')
                run_id, number = row.get('id'), row.get('run_number')
                if type(run_id) is not int or type(number) is not int:
                    raise DispatchError('RUN_IDENTITY')
                if run_id == current_id or row.get('head_branch') != 'main':
                    continue
                if row.get('event') != 'workflow_dispatch' or row.get('status') not in ACTIVE:
                    continue
                if mode == 'continuation' and number <= current_number:
                    continue
                title = row.get('display_title', '')
                if title == 'LIVE-predeadline-' + day:
                    return run_id
                if title == 'LIVE-predeadline-':
                    try:
                        created = datetime.fromisoformat(row['created_at'].replace('Z', '+00:00'))
                    except (KeyError, ValueError, TypeError):
                        raise DispatchError('RUN_DATE') from None
                    if created.tzinfo is None:
                        raise DispatchError('RUN_DATE')
                    if created.astimezone(JST).date().isoformat() == day:
                        return run_id
            if page * 100 >= total:
                break
        else:
            raise DispatchError('RUN_LIST_PAGE_LIMIT')
    return None


def dispatch(client, day, current_id, current_number, mode, *, sleep=time.sleep):
    if mode not in ('continuation', 'bootstrap', 'rerun'):
        raise DispatchError('MODE')
    if type(current_id) is not int or current_id <= 0 or type(current_number) is not int or current_number <= 0:
        raise DispatchError('RUN_CONTEXT')
    if not isinstance(day, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
        raise DispatchError('DAY')
    try:
        datetime.strptime(day, '%Y-%m-%d')
    except ValueError:
        raise DispatchError('DAY') from None
    body = {'ref': 'main', 'inputs': {'phase': 'predeadline', 'operational_date': day}}
    for attempt in range(1, 4):
        existing = active_successor(client, day, current_id, current_number, mode, sleep)
        if existing is not None:
            return {'status': 'EXISTING_ACTIVE', 'run_id': existing, 'attempts': attempt - 1}
        try:
            client.request('POST', '/actions/workflows/' + WORKFLOW + '/dispatches', body)
            return {'status': 'DISPATCH_ACCEPTED', 'attempts': attempt}
        except ApiError as exc:
            if exc.status not in RETRYABLE:
                raise
            # A failed/timeout response may still have created a run. Never
            # replay the POST without first reconciling fresh scheduling data.
            for delay in (2, 5):
                sleep(delay)
                existing = active_successor(client, day, current_id, current_number, mode, sleep)
                if existing is not None:
                    return {'status': 'ACCEPTED_AFTER_AMBIGUOUS_RESPONSE',
                            'run_id': existing, 'attempts': attempt}
            if attempt == 3:
                raise DispatchError('DISPATCH_RETRY_EXHAUSTED') from None
    raise DispatchError('UNREACHABLE')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--day', required=True)
    parser.add_argument('--mode', required=True, choices=('continuation', 'bootstrap', 'rerun'))
    args = parser.parse_args()
    if os.environ.get('GITHUB_REPOSITORY') != REPOSITORY or os.environ.get('GITHUB_REF') != 'refs/heads/main':
        raise DispatchError('REPOSITORY_REF')
    if args.day != datetime.now(JST).date().isoformat():
        raise DispatchError('CURRENT_DAY_REQUIRED')
    result = dispatch(Client(os.environ.get('GH_TOKEN', '')), args.day,
                      int(os.environ['GITHUB_RUN_ID']), int(os.environ['GITHUB_RUN_NUMBER']), args.mode)
    print('G11_SAFE_PREDEADLINE_DISPATCH=' + json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (DispatchError, KeyError, ValueError) as exc:
        code = str(exc) if isinstance(exc, DispatchError) else 'ENVIRONMENT'
        print('G11_SAFE_PREDEADLINE_DISPATCH=HELD REASON=' + code)
        raise SystemExit(1) from None
