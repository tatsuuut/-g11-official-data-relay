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
import textwrap
import types

COLLECTOR_SHA = '6eef8e082ae3d6c874d83844bbb2fcae04fd0ceab3d829db5a511d29da6a9017'
MAX_WAIT_SECONDS = 30
MIN_REMAINING_SECONDS = 180
RETRY_START_WINDOW_SECONDS = 60
MAX_RETRY_SOURCES = 2


def _report(status, **fields):
    print('G11_PREDEADLINE_CONTENT_RETRY=' + json.dumps(
        {'status': status, **fields}, sort_keys=True), flush=True)


def select_retry(first, plan, now):
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
                or due > now or deadline - due != timedelta(minutes=7)
                or sid in selected):
            raise ValueError('CONTENT_RETRY_SOURCE_BINDING')
        delay = max(0, (retry_at - now).total_seconds())
        if delay > MAX_WAIT_SECONDS or (deadline - now).total_seconds() < MIN_REMAINING_SECONDS:
            return [], 0, 'INSUFFICIENT_TIME'
        selected.append(sid)
        delays.append(delay)
    return selected, max(delays), 'READY'


def _restricted_collector(original, selected, stop_at):
    collector = inspect.unwrap(original)
    source = inspect.getsource(collector)
    if hashlib.sha256(source.encode()).hexdigest() != COLLECTOR_SHA:
        raise ValueError('CONTENT_RETRY_UNREVIEWED_COLLECTOR')
    anchor = '            task = plan[task_index]\n'
    if source.count(anchor) != 1:
        raise ValueError('CONTENT_RETRY_SOURCE_CONTRACT')
    source = source.replace(anchor, anchor +
        '            if task["SOURCE_ID"] not in _retry_source_ids:\n'
        '                task_index += 1\n'
        '                continue\n'
        '            if self.client.clock() >= _retry_stop_at:\n'
        '                break\n', 1)
    namespace = dict(collector.__globals__)
    namespace.update(_retry_source_ids=frozenset(selected), _retry_stop_at=stop_at)
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
    for key in ('ACQUIRED', 'MISSED'):
        result[key] = list(dict.fromkeys(first[key] + second[key]))
    for key in ('SKIPPED', 'REJECTED', 'TASK_DEFERRED', 'RETRYABLE_FAILURES', 'UNAVAILABLE'):
        merged = {row['SOURCE_ID']: row for row in first[key] + second[key]}
        result[key] = [row for sid, row in merged.items()
                       if key == 'SKIPPED' or sid not in acquired]
    result['DEFERRED'] = second['DEFERRED'] or first['DEFERRED']
    if result['MISSED']:
        result['STATUS'] = 'PARTIAL_MISSED_NOT_PROMOTED'
    return result


def collect_with_retry(original, runner, *, max_requests, early_wait_seconds):
    first = original(runner, max_requests=max_requests,
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
        selected, delay, status = select_retry(first, plan, runner.client.clock())
    except (KeyError, TypeError, ValueError) as exc:
        _report('SELECTION_SKIPPED', error_type=type(exc).__name__)
        return first
    if not selected:
        _report(status)
        return first
    _report('WAIT', source_ids=selected, wait_seconds=delay)
    if delay:
        runner.sleeper(delay)
    now = runner.client.clock()
    # A delayed wake-up must not turn the bounded retry into a last-second run.
    selected_after_wait, _, status = select_retry(first, plan, now)
    if selected_after_wait != selected:
        _report('WAKEUP_SKIPPED', reason=status)
        return first
    restricted = _restricted_collector(original, selected,
        now + timedelta(seconds=RETRY_START_WINDOW_SECONDS))
    second = restricted(runner, max_requests=min(max_requests, MAX_RETRY_SOURCES),
                        early_wait_seconds=0)
    _report('ATTEMPTED_ONCE', source_ids=selected, acquired=second['ACQUIRED'],
            unresolved=[row['SOURCE_ID'] for row in second['REJECTED'] + second['TASK_DEFERRED']],
            unavailable=second['UNAVAILABLE'], missed=second['MISSED'])
    return merge_attempts(first, second)


@contextmanager
def bounded_content_retry():
    from g11.canonical.official_pipeline import CanonicalRunner
    original = CanonicalRunner.run_due_predeadline
    source = inspect.getsource(original)
    if hashlib.sha256(source.encode()).hexdigest() != COLLECTOR_SHA:
        _report('UNREVIEWED_SOURCE_UNCHANGED')
        yield
        return

    @wraps(original)
    def bounded(self, *, max_requests=48, early_wait_seconds=300):
        return collect_with_retry(original, self, max_requests=max_requests,
                                  early_wait_seconds=early_wait_seconds)

    CanonicalRunner.run_due_predeadline = bounded
    _report('ADAPTER_APPLIED')
    try:
        yield
    finally:
        CanonicalRunner.run_due_predeadline = original

