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
RUN_SHA256 = 'aa19dbecb3230e9836e529e5b73bec9ca560f514e293b23b8bf382dfefe1a5a1'
# The Nov 21 candidate only adds the private optional display warm-up before
# acquisition. The formal morning scope and checkpoint sequence are identical.
DISPLAY_WARM_RUN_SHA = '0c6ef2bc60ab53b429f2849e7c3a4b2a1584ff2ad2b273beab952ce5d000ff71'


def reviewed_run(day, source_sha):
    return source_sha == RUN_SHA256 or (day >= '2026-11-21' and source_sha == DISPLAY_WARM_RUN_SHA)


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


def stadium_inventory(store, day):
    return {p.stem for p in (store / 'snapshots/predeadline').glob(
        'venue-market-' + day.replace('-', '') + '-24-??.json')}


def timely_stadium_sources(artifact, day, plan, prior, now):
    """Signal new, byte-verified first-party odds to the existing handoff."""
    from datetime import datetime
    due = {t['RACE_KEY']: datetime.fromisoformat(t['MUST_FINISH_BEFORE_JST'])
           for t in plan if t['KIND'] == 'ODDS3T'}
    output = []
    for name in sorted(stadium_inventory(artifact.root, day) - prior):
        key = name.removeprefix('venue-market-')
        limit = due.get(key)
        if limit is None or now >= limit:
            continue
        try:
            packet = artifact.load_verified_snapshot('predeadline', name)['SNAPSHOT']
            receipt, = packet['SOURCE_RECEIPTS']
            payload = packet['PAYLOAD']
            raw = artifact.response_bytes(receipt['SOURCE_ID'])
            digest = hashlib.sha256(raw).hexdigest()
            url = 'https://www.omurakyotei.jp/yosou/sp/odds/?day=' + key[:8] + '&race=' + key[-2:]
            captured = datetime.fromisoformat(receipt['CAPTURED_AT_JST'])
            if (payload['KIND'] == 'ODDS3T' and payload['RACE_KEY'] == key
                    and payload['STATUS'] == 'OMURA_OFFICIAL_PUBLISHED_MARKET'
                    and receipt['STATUS'] == 'HTTP_200_BYTE_EXACT_READBACK'
                    and payload['PUBLISHER'] == receipt['HOST'] == 'www.omurakyotei.jp'
                    and receipt['URL'] == receipt['FINAL_URL'] == url
                    and receipt['SOURCE_ID'] == 'odds3t-omura-' + key + '-' + digest
                    and digest == receipt['SHA256']
                    and payload['CAPTURED_AT_JST'] == receipt['CAPTURED_AT_JST']
                    and limit.timestamp() - 420 <= captured.timestamp() <= now.timestamp()
                    and len(payload['PARSED']['ODDS_ROWS']) == 120):
                output.append(name)
        except (KeyError, TypeError, ValueError, OSError) as exc:
            print('VENUE_MARKET_HANDOFF_WAIT', key, str(exc)[:160], flush=True)
    if output:
        print('VENUE_MARKET_HANDOFF_VERIFIED', *output, flush=True)
    return output


def complete_from_venue(artifact, prediction):
    """Admit timely input after a verified host stop under the active day's authority."""
    from pathlib import Path
    import importlib.util
    import os
    import subprocess
    from g11.canonical import official_pipeline
    if artifact.ledger.state()['STOPPED'] is not True:
        return prediction
    try:
        root = Path(os.environ['GITHUB_WORKSPACE']).resolve() / '.g11-venue-input'
        path = root / 'g11/relay/verified_venue_live_v1.py'
        if not path.is_file():
            return prediction
        commit = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
        if commit != '845eea35405dc81447b71a699c9a14c93fe22f2a':
            raise ValueError('VENUE_COLLECTOR_VERSION')
        spec = importlib.util.spec_from_file_location('_g11_verified_venue_input', path)
        provider = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(provider)
        return provider.complete(artifact, prediction, official_pipeline)
    except (KeyError, OSError, ImportError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print('VENUE_FORMAL_UNAVAILABLE', type(exc).__name__, str(exc)[:180], flush=True)
        return prediction


@contextmanager
def formal_lock_scope(day):
    if date.fromisoformat(day) < EFFECTIVE_FROM:
        yield
        return
    from g11.relay import predeadline_critical_v1 as critical
    from morning_race_isolation_v1 import replace_pinned
    original = critical.run
    run_sha = hashlib.sha256(inspect.getsource(original).encode()).hexdigest()
    if not reviewed_run(day, run_sha):
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
    substitutions = [(before, after)]
    additions = {'_missing_formal_locks': missing_formal_locks, 'capture': capture}
    if day == '2026-11-20':
        native = '        prediction = CanonicalRunner(artifact).run_due_predeadline_predictions()'
        substitutions.append((native, native + '\n        prediction = _complete_from_venue(artifact, prediction)'))
        additions['_complete_from_venue'] = complete_from_venue
    if day >= '2026-11-20':
        begin = '        acquisition = capture(store)'
        end = '    timely_live = [item["RACE_ID"]'
        substitutions.extend([
            (begin, '        prior_venue = _stadium_inventory(store, operational_date)\n' + begin),
            (end, '    if now < last_deadline:\n'
             '        timely_sources.extend(_timely_stadium_sources(artifact, operational_date, plan, prior_venue, datetime.now(JST)))\n' + end)])
        additions.update(_stadium_inventory=stadium_inventory, _timely_stadium_sources=timely_stadium_sources)
    critical.run = replace_pinned(original, run_sha, substitutions, additions)
    try:
        yield
    finally:
        critical.run = original
