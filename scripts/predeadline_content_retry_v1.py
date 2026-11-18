"""One bounded retry for rejected official inputs; frozen admission stays intact.

Never hold newly acquired input behind a retry. Only an otherwise empty capture
may revisit at most two content-rejected sources after their existing pause.
The reviewed canonical collector still owns HTTP pacing, request budgets,
parsing, eligibility, immutable saves and cutoff checks. No model is called here.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import wraps
import hashlib
import inspect
import json
import re
import textwrap
import types

COLLECTOR_SHA = '6eef8e082ae3d6c874d83844bbb2fcae04fd0ceab3d829db5a511d29da6a9017'
# Keep the frozen collector and the reviewed November 11 withdrawal boundary.
COLLECTOR_SHAS = frozenset({
    COLLECTOR_SHA,
    'a40b3bdfb83e88fac0b12485add0536a1a60555ac32b15ba1af1687ddb2f8649',
})
MAX_WAIT_SECONDS = 30
MIN_REMAINING_SECONDS = 180
RETRY_START_WINDOW_SECONDS = 60
MAX_RETRY_SOURCES = 2
MAX_HOST_RESUME_WAIT_SECONDS = 180


def _report(status, **fields):
    print('G11_PREDEADLINE_CONTENT_RETRY=' + json.dumps(
        {'status': status, **fields}, sort_keys=True), flush=True)


def select_retry(first, plan, now, *, early_capture=False):
    """Select existing, same-day content pauses; do not retry HTTP failures."""
    if now.utcoffset() != timedelta(hours=9):
        raise ValueError('CONTENT_RETRY_TIMEZONE')
    if first['ACQUIRED']:
        return [], 0, 'PUBLICATION_READY'
    if first['DEFERRED'] or first['RETRYABLE_FAILURES'] or first['UNAVAILABLE']:
        return [], 0, 'EXISTING_FAILURE_POLICY'
    pending = first['REJECTED'] + first['TASK_DEFERRED']
    if not pending or len(pending) > MAX_RETRY_SOURCES:
        return [], 0, 'NO_BOUNDED_CONTENT_RETRY'
    by_source = {row['SOURCE_ID']: row for row in plan}
    if len(by_source) != len(plan):
        raise ValueError('CONTENT_RETRY_DUPLICATE_PLAN')
    selected, delays = [], []
    for row in pending:
        if row in first['TASK_DEFERRED'] and row['REASON'] != 'SOURCE_CONTENT_PAUSED':
            return [], 0, 'EXISTING_FAILURE_POLICY'
        sid = row['SOURCE_ID']
        task = by_source[sid]
        due = datetime.fromisoformat(task['DUE_AT_JST'])
        deadline = datetime.fromisoformat(task['MUST_FINISH_BEFORE_JST'])
        retry_at = datetime.fromisoformat(row['RETRY_AT_JST'])
        if (task['KIND'] not in {'BEFOREINFO', 'ODDS3T'}
                or sid != task['KIND'].lower() + '-' + task['RACE_KEY']
                or row['RACE_KEY'] != task['RACE_KEY']
                or task['RACE_KEY'][:8] != now.strftime('%Y%m%d')
                or any(t.utcoffset() != timedelta(hours=9)
                       or t.date() != now.date() for t in (due, deadline, retry_at))
                or deadline - due != timedelta(minutes=7)
                or sid in selected):
            raise ValueError('CONTENT_RETRY_SOURCE_BINDING')
        if early_capture:
            from predeadline_capture_window_v1 import effective_due
            due = effective_due(due, deadline)
        if due > now:
            raise ValueError('CONTENT_RETRY_SOURCE_BINDING')
        delay = max(0, (retry_at - now).total_seconds())
        if delay > MAX_WAIT_SECONDS or (deadline - now).total_seconds() < MIN_REMAINING_SECONDS:
            return [], 0, 'INSUFFICIENT_TIME'
        selected.append(sid)
        delays.append(delay)
    return selected, max(delays), 'READY'


def official_host_guard(store, now):
    state = store.ledger.state()
    if state['STOPPED'] is True:
        return {'REASON': 'PIPELINE_STOPPED'}
    until = state['PAUSE_UNTIL'].get('www.boatrace.jp')
    if until and now < until:
        return {'REASON': 'HOST_PAUSED', 'RETRY_AT_JST': until.isoformat(timespec='seconds')}
    return None


def _isolate_host(source):
    # Only the two reviewed collector hashes get here. Keep validation, source
    # receipt verification, race scope, immutable writes and cutoff in the original.
    changes = [
        ('        task_index = 0\n', '        deferred = _host_guard(self.store, self.client.clock())\n        task_index = 0\n'),
        ('            try:\n                receipt, parsed = ',
         '            if deferred and self.store.response_receipt(task["SOURCE_ID"]) is None:\n'
         '                task_deferred.append({"SOURCE_ID": task["SOURCE_ID"], "RACE_KEY": task["RACE_KEY"], **deferred})\n'
         '                task_index += 1\n'
         '                continue\n'
         '            try:\n                receipt, parsed = '),
        ('                delay = max(0.0, (exc.retry_at - now).total_seconds())\n',
         '                if exc.reason == "HOST_PAUSED":\n'
         '                    deferred = {"REASON": exc.reason, "RETRY_AT_JST": _format_jst(exc.retry_at)}\n'
         '                    task_deferred.append({"SOURCE_ID": task["SOURCE_ID"], "RACE_KEY": task["RACE_KEY"], **deferred})\n'
         '                    task_index += 1\n'
         '                    continue\n'
         '                delay = max(0.0, (exc.retry_at - now).total_seconds())\n'),
        ('                reason = str(exc)\n',
         '                reason = str(exc)\n'
         '                if reason in ("HTTP_403", "HTTP_429", "PIPELINE_STOPPED"):\n'
         '                    deferred = _host_guard(self.store, self.client.clock()) or deferred\n'
         '                if reason == "PIPELINE_STOPPED" and deferred and deferred["REASON"] == reason:\n'
         '                    task_deferred.append({"SOURCE_ID": task["SOURCE_ID"], "RACE_KEY": task["RACE_KEY"], **deferred})\n'
         '                    task_index += 1\n'
         '                    continue\n'),
    ]
    for anchor, replacement in changes:
        if source.count(anchor) != 1:
            raise ValueError('HOST_GUARD_COLLECTOR_CONTRACT')
        source = source.replace(anchor, replacement, 1)
    return source


def _restricted_collector(original, selected, stop_at, *, early_capture=False, isolate_host=False):
    collector = inspect.unwrap(original)
    source = inspect.getsource(collector)
    if hashlib.sha256(source.encode()).hexdigest() not in COLLECTOR_SHAS:
        raise ValueError('CONTENT_RETRY_UNREVIEWED_COLLECTOR')
    anchor = '            task = plan[task_index]\n'
    if source.count(anchor) != 1:
        raise ValueError('CONTENT_RETRY_SOURCE_CONTRACT')
    if selected is not None:
        source = source.replace(anchor, anchor +
            '            if task["SOURCE_ID"] not in _retry_source_ids:\n'
            '                task_index += 1\n'
            '                continue\n'
            '            if self.client.clock() >= _retry_stop_at:\n'
            '                break\n', 1)
    namespace = dict(collector.__globals__)
    namespace.update(_retry_source_ids=frozenset(selected or []), _retry_stop_at=stop_at)
    if early_capture:
        from predeadline_capture_window_v1 import effective_due
        anchor = '            deadline = _parse_jst(task["MUST_FINISH_BEFORE_JST"])\n'
        if source.count(anchor) != 1:
            raise ValueError('EARLY_CAPTURE_COLLECTOR_CONTRACT')
        source = source.replace(anchor, anchor+'            due = _capture_due(due, deadline)\n', 1)
        namespace['_capture_due'] = effective_due
    if isolate_host:
        source = _isolate_host(source)
        namespace['_host_guard'] = official_host_guard
    exec(compile(textwrap.dedent(source), collector.__code__.co_filename,
                 'exec'), namespace)
    restricted = namespace[collector.__name__]
    # inspect.getsource unwraps @wraps, but __globals__ belongs to the wrapper.
    # Compile in the collector's namespace, then retain the authority boundary
    # around the second call as well. Never mutate the frozen function or cells.
    if original is collector:
        return restricted
    if (not getattr(original, '_g11_daily_authority_boundary', False)
            or getattr(original, '__wrapped__', None) is not collector
            or not original.__closure__):
        raise ValueError('CONTENT_RETRY_UNREVIEWED_BOUNDARY')
    cells = original.__closure__
    if sum(cell.cell_contents is collector for cell in cells) != 1:
        raise ValueError('CONTENT_RETRY_BOUNDARY_CLOSURE')
    replacement = types.FunctionType(
        original.__code__, original.__globals__, original.__name__,
        original.__defaults__, tuple(
            types.CellType(restricted) if cell.cell_contents is collector else cell
            for cell in cells))
    replacement.__kwdefaults__ = original.__kwdefaults__
    return wraps(restricted)(replacement)


def merge_attempts(first, second):
    """Keep the normal return contract; resolved rejection evidence stays on disk."""
    result = dict(first)
    acquired = set(second['ACQUIRED'])
    unavailable_races = {
        row['RACE_KEY'] for row in second['UNAVAILABLE']
        if row.get('REASON') == 'OFFICIAL_WITHDRAWAL_UNSUPPORTED_FIELD_SIZE'
        and isinstance(row.get('RACE_KEY'), str)
        and re.fullmatch(r'\d{8}-(?:0[1-9]|1[0-9]|2[0-4])-(?:0[1-9]|1[0-2])', row['RACE_KEY'])
        and row['RACE_KEY'][:8] >= '20261111'
        and row.get('SOURCE_ID') in {
            'beforeinfo-' + row['RACE_KEY'], 'odds3t-' + row['RACE_KEY']}
        and isinstance(row.get('AVAILABILITY_SNAPSHOT_SHA256'), str)
        and re.fullmatch(r'[a-f0-9]{64}', row['AVAILABILITY_SNAPSHOT_SHA256'])
    }
    for key in ('ACQUIRED', 'MISSED'):
        result[key] = list(dict.fromkeys(first[key] + second[key]))
    for key in ('SKIPPED', 'REJECTED', 'TASK_DEFERRED', 'RETRYABLE_FAILURES', 'UNAVAILABLE'):
        merged = {row['SOURCE_ID']: row for row in first[key] + second[key]}
        result[key] = [row for sid, row in merged.items()
                       if (key == 'SKIPPED' or sid not in acquired)
                       and not (key in ('REJECTED', 'TASK_DEFERRED')
                           and row.get('RACE_KEY') in unavailable_races
                           and sid in {'beforeinfo-' + row['RACE_KEY'],
                                       'odds3t-' + row['RACE_KEY']})]
    result['DEFERRED'] = second['DEFERRED'] or first['DEFERRED']
    if result['MISSED']:
        result['STATUS'] = 'PARTIAL_MISSED_NOT_PROMOTED'
    return result


def resume_at_host_release(first, plan, runner, capture, max_requests, now):
    """Use the existing worker only when the official pause is about to expire."""
    from predeadline_input_health_v1 import host_pause
    from predeadline_capture_window_v1 import effective_due
    pause = host_pause(first['DEFERRED'], now.date().isoformat(), now)
    if not pause or first['ACQUIRED'] or first['RETRYABLE_FAILURES'] or first['REJECTED']:
        return None
    release = datetime.fromisoformat(first['DEFERRED']['RETRY_AT_JST'])
    delay = (release - now).total_seconds()
    if delay > MAX_HOST_RESUME_WAIT_SECONDS or release.date() != now.date():
        return None
    def eligible_at(at):
        for task in plan:
            due = datetime.fromisoformat(task['DUE_AT_JST'])
            deadline = datetime.fromisoformat(task['MUST_FINISH_BEFORE_JST'])
            if effective_due(due, deadline) <= at < deadline:
                saved = runner.store.root / 'snapshots' / 'predeadline' / (task['SOURCE_ID'] + '.json')
                if not saved.exists():
                    return True
        return False
    if not eligible_at(release):
        return None
    _report('WAIT_FOR_HOST_RELEASE', retry_at_jst=pause['retry_at_jst'])
    runner.sleeper(delay)
    wake = runner.client.clock()
    if wake < release or wake.date() != now.date() or not eligible_at(wake):
        return first
    # The original admission code rechecks host backoff, pacing, stored
    # receipts, daily limits and each deadline. It may defer again.
    second = capture(runner, max_requests=max_requests, early_wait_seconds=0)
    result = merge_attempts(first, second)
    result['DEFERRED'] = second['DEFERRED']
    _report('HOST_RELEASE_CHECK', acquired=len(second['ACQUIRED']), deferred=bool(second['DEFERRED']))
    return result


def collect_with_retry(original, runner, *, max_requests, early_wait_seconds, early_capture=False):
    isolate = early_wait_seconds == 0 and runner.client.clock().date().isoformat() >= '2026-11-18'
    capture = (_restricted_collector(original, None, None, early_capture=early_capture, isolate_host=isolate)
               if early_capture or isolate else original)
    first = capture(runner, max_requests=max_requests,
                     early_wait_seconds=early_wait_seconds)
    # The existing nowait caller must opt in. Other callers retain their policy.
    if early_wait_seconds != 0:
        return first
    try:
        if first['ACQUIRED']:
            _report('PUBLICATION_READY', acquired=len(first['ACQUIRED']))
            return first
        morning = runner.store.load_verified_snapshot('morning', 'official-day-input')
        plan = morning['SNAPSHOT']['PAYLOAD']['PREDEADLINE_PLAN']
        now = runner.client.clock()
        selected, delay, status = select_retry(first, plan, now,early_capture=early_capture)
    except (KeyError, TypeError, ValueError) as exc:
        _report('SELECTION_SKIPPED', error_type=type(exc).__name__)
        return first
    if early_capture and first['DEFERRED'] and now.date().isoformat() >= '2026-11-18':
        resumed = resume_at_host_release(first, plan, runner, capture, max_requests, now)
        if resumed is not None:
            return resumed
    if not selected:
        _report(status)
        return first
    _report('WAIT', source_ids=selected, wait_seconds=delay)
    if delay:
        runner.sleeper(delay)
    now = runner.client.clock()
    # A delayed wake-up must not turn the bounded retry into a last-second run.
    selected_after_wait, _, status = select_retry(first, plan, now,early_capture=early_capture)
    if selected_after_wait != selected:
        _report('WAKEUP_SKIPPED', reason=status)
        return first
    restricted = _restricted_collector(original, selected,
        now + timedelta(seconds=RETRY_START_WINDOW_SECONDS),early_capture=early_capture, isolate_host=isolate)
    second = restricted(runner, max_requests=min(max_requests, MAX_RETRY_SOURCES),
                        early_wait_seconds=0)
    _report('ATTEMPTED_ONCE', source_ids=selected, acquired=second['ACQUIRED'],
            unresolved=[row['SOURCE_ID'] for row in second['REJECTED'] + second['TASK_DEFERRED']],
            unavailable=second['UNAVAILABLE'], missed=second['MISSED'])
    return merge_attempts(first, second)


@contextmanager
def bounded_content_retry(*, early_capture=False):
    from g11.canonical.official_pipeline import CanonicalRunner
    original = CanonicalRunner.run_due_predeadline
    source = inspect.getsource(original)
    if hashlib.sha256(source.encode()).hexdigest() not in COLLECTOR_SHAS:
        _report('UNREVIEWED_SOURCE_UNCHANGED')
        yield
        return

    @wraps(original)
    def bounded(self, *, max_requests=48, early_wait_seconds=300):
        from predeadline_capture_window_v1 import enabled
        advance = (early_capture and early_wait_seconds == 0
            and enabled(self.client.clock())
            and getattr(CanonicalRunner,'_g11_ten_minute_odds_window',False))
        if advance:
            _report('TEN_MINUTE_CAPTURE',capture_lead_minutes=10,
                    saved_plan_rewritten=False,clock_shift_seconds=0)
        return collect_with_retry(original, self, max_requests=max_requests,
                                  early_wait_seconds=early_wait_seconds,early_capture=advance)

    CanonicalRunner.run_due_predeadline = bounded
    _report('ADAPTER_APPLIED')
    try:
        yield
    finally:
        CanonicalRunner.run_due_predeadline = original

