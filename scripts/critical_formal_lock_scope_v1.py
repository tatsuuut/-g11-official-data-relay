"""Keep the critical morning-LOCK check within the saved formal race scope.

The verified workbook already records exclusions. Requiring a prediction for
those rows turns an intentional exclusion into a whole-day outage. No stored
row, source, daily authority, prediction or result is modified by this adapter.
"""
from contextlib import contextmanager
from datetime import date
import hashlib
import inspect
import re

EFFECTIVE_FROM = date(2026, 11, 1)
# Morning authority can select either reviewed runner across the rollout.
REVIEWED_RUN_SHA256 = frozenset({
    'aa19dbecb3230e9836e529e5b73bec9ca560f514e293b23b8bf382dfefe1a5a1',
    '0c6ef2bc60ab53b429f2849e7c3a4b2a1584ff2ad2b273beab952ce5d000ff71',
})


def missing_formal_locks(projection, store, snapshot_name, expected_fields):
    fields = projection.get('LOCK49_FIELDS')
    rows = projection.get('LOCK49_ROWS')
    if fields != list(expected_fields) or type(rows) is not list or not rows:
        raise ValueError('CRITICAL_MORNING_SCOPE_SCHEMA')
    key_index = fields.index('標準レースキー')
    status_index = fields.index('正式/除外')
    reason_index = fields.index('除外理由')
    missing, seen = [], set()
    for row in rows:
        if type(row) is not list or len(row) != len(fields):
            raise ValueError('CRITICAL_MORNING_SCOPE_ROW')
        key, status, reason = row[key_index], row[status_index], row[reason_index]
        if (type(key) is not str or re.fullmatch(r'\d{8}-(?:0[1-9]|1\d|2[0-4])-(?:0[1-9]|1[0-2])', key) is None
                or key in seen or status not in ('FORMAL', 'EXCLUDED')):
            raise ValueError('CRITICAL_MORNING_SCOPE_IDENTITY')
        seen.add(key)
        if status == 'EXCLUDED':
            if type(reason) is not str or not reason.strip():
                raise ValueError('CRITICAL_MORNING_EXCLUSION_REASON')
            continue
        if not (store / 'snapshots/morning' / (snapshot_name(key) + '.json')).is_file():
            missing.append(key)
    return missing


@contextmanager
def formal_lock_scope(day):
    if date.fromisoformat(day) < EFFECTIVE_FROM:
        yield
        return
    from g11.relay import predeadline_critical_v1 as critical
    from morning_race_isolation_v1 import replace_pinned
    original = critical.run
    digest = hashlib.sha256(inspect.getsource(original).encode()).hexdigest()
    if digest not in REVIEWED_RUN_SHA256:
        raise ValueError('CRITICAL_MORNING_SCOPE_SOURCE_CHANGED')
    before = '''        fields = projection["LOCK49_FIELDS"]
        index = fields.index("標準レースキー")
        missing_morning = [
            row[index] for row in projection["LOCK49_ROWS"]
            if not (store / "snapshots" / "morning"
                    / f"{current_runtime.snapshot_name(row[index])}.json").is_file()
        ]'''
    after = '''        missing_morning = _missing_formal_locks(
            projection, store, current_runtime.snapshot_name,
            current_p3.feedlib.LOCK49_FIELDS,
        )'''
    capture = critical.capture
    if day >= '2026-11-17':
        from predeadline_input_health_v1 import observed_capture
        capture = observed_capture(capture, critical.OfficialArtifactStore, day)
    # replace_pinned compiles with a copy of the original globals. Bind the
    # observer in that namespace; changing critical.capture afterward cannot
    # affect the compiled run function.
    critical.run = replace_pinned(original, digest, [(before, after)],
        {'_missing_formal_locks': missing_formal_locks, 'capture': capture})
    try:
        yield
    finally:
        critical.run = original

