"""Isolate LIVE input failures without changing the day's frozen predictors.

Reviewed orchestration functions only. Source/model files, formal input gates,
LOCKs, predictions and result handlers are never rewritten. Official exceptional
ST remains unsupported where the unchanged engine requires a numeric value.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import inspect
import json
import sys
import textwrap
from datetime import datetime
from pathlib import Path
import re
import subprocess

COLLECT_SHA = 'a1d5c2166a4e305825d593f6f48ffa76717bc5998226d5522eb0bde38ff6e42b'
BET_HANDOFF_SHA = 'ed14b607cb6b590be3a03eff2d08ddc153bc204df6d8ef6c32feffe731adedd6'
DISPLAY_SHA = '69c84eb70e24ca43e3ca46aa987b3cdd2db965b3884a97a084bd61ad8efa896d'


def _replace(source, before, after):
    if source.count(before) != 1:
        raise RuntimeError('PREDEADLINE_ISOLATION_SOURCE_CONTRACT_CHANGED')
    return source.replace(before, after, 1)


def _compile(function, expected, transform, additions=None):
    source = inspect.getsource(function)
    if hashlib.sha256(source.encode()).hexdigest() != expected:
        raise RuntimeError('PREDEADLINE_ISOLATION_UNREVIEWED_SOURCE:' + function.__name__)
    namespace = dict(function.__globals__)
    namespace.update(additions or {})
    exec(compile(textwrap.dedent(transform(source)), function.__code__.co_filename,
                 'exec'), namespace)
    return namespace[function.__name__]


def _report(target, attempted, written, failures):
    print('G11_PREDEADLINE_RACE_ISOLATION=' + json.dumps({
        'OPERATIONAL_DATE_JST': target.isoformat(),
        'ENGINE': 'WILD_PRE_RACE',
        'STATUS': 'PARTIAL' if failures else 'PASS',
        'ATTEMPTED_RACES': attempted,
        'NEW_LOCKS': written,
        'FAILED': failures,
        'POST_DEADLINE_LOCKS_WRITTEN': 0,
        'INPUT_VALUES_REPLACED': 0,
    }, ensure_ascii=False, sort_keys=True), file=sys.stderr)


def _collect_source(source):
    # Keep global source/manifest verification outside the per-race boundary.
    source = _replace(source, '    for race_key, race_tasks in sorted(tasks.items()):\n',
        '    attempted, written, failures = [], [], []\n'
        '    for race_key, race_tasks in sorted(tasks.items()):\n')
    start = source.index('        before_task = race_tasks.get("BEFOREINFO")\n')
    end = source.index('\n    status_path = status_root / STATUS_FILE\n', start)
    body = source[start:end]
    body = _replace(body, '        packet = {\n',
        '        locked_at = datetime.now(JST)\n'
        '        _require(captured <= locked_at < deadline,\n'
        '                 f"PRE_RACE_SAVE_TIME_SAFE:{race_key}")\n'
        '        packet = {\n')
    body = _replace(body,
        '        prospective._write_json_atomic(packet_path, packet)',
        '        _require(datetime.now(JST) < deadline,\n'
        '                 f"PRE_RACE_SAVE_TIME_SAFE:{race_key}")\n'
        '        prospective._write_json_atomic(packet_path, packet)')
    body = _replace(body, '        changed = True\n',
        '        changed = True\n        written.append(race_key)\n')
    wrapped = ('        attempted.append(race_key)\n        try:\n'
        + textwrap.indent(body, '    ')
        + '        except _isolated_input_errors as exc:\n'
        + '            partial_races.add(race_key)\n'
        + '            failures.append({"RACE_KEY": race_key, "REASON": str(exc)})\n'
        + '            continue\n')
    source = source[:start] + wrapped + source[end:]
    return _replace(source, '    return changed or previous != status_path.read_bytes()\n',
        '    _isolation_report(target, attempted, written, failures)\n'
        '    return changed or previous != status_path.read_bytes()\n')


def _handoff_source(source):
    # Morning BET odds depend on verified ODDS3T, not optional PRE_RACE models.
    source = _replace(source,
        '    pre_race_changed = _collect_pre_race_packets(store, target, official_store)\n', '')
    source = _replace(source, '        return pre_race_changed\n',
        '        return _collect_pre_race_packets(store, target, official_store)\n')
    return _replace(source,
        '    return pre_race_changed or previous_hash != _file_sha(previous)\n',
        '    pre_race_changed = _collect_pre_race_packets(store, target, official_store)\n'
        '    return pre_race_changed or previous_hash != _file_sha(previous)\n')


def _display_source(source):
    return _replace(source,
        '        audit = build_capture_audit(\n'
        '            store, race_key, parsed, raw_sha, captured, url\n'
        '        )\n',
        '        try:\n'
        '            audit = build_capture_audit(\n'
        '                store, race_key, parsed, raw_sha, captured, url\n'
        '            )\n'
        '        except ShadowError as exc:\n'
        '            failed.append({"RACE_KEY": race_key, "REASON": str(exc)})\n'
        '            continue\n')


@contextmanager
def isolate_predeadline_inputs():
    try:
        from g11.canonical import official_pipeline as official
        from g11.canonical import original_display_shadow as display
        from g11.wild_pack.v1 import runtime_orchestrator_v1 as wild
    except ImportError as exc:
        print('PREDEADLINE_ISOLATION_IMPORT_UNAVAILABLE:' + str(exc), file=sys.stderr)
        yield
        return
    originals = (wild._collect_pre_race_packets, wild.run_predeadline, display.capture_due)
    try:
        collector = _compile(originals[0], COLLECT_SHA, _collect_source, {
            '_isolated_input_errors': (wild.pre_position.PreRacePositionError,
                wild.WildRuntimeOrchestratorError, official.PipelineError),
            '_isolation_report': _report,
        })
        handoff = _compile(originals[1], BET_HANDOFF_SHA, _handoff_source,
                           {'_collect_pre_race_packets': collector})
        capture = _compile(originals[2], DISPLAY_SHA, _display_source)
    except RuntimeError as exc:
        # Unknown future sources retain their own semantics; never claim a patch
        # was applied and never block canonical acquisition for an optional fix.
        print(str(exc), file=sys.stderr)
        yield
        return
    wild._collect_pre_race_packets, wild.run_predeadline, display.capture_due = collector, handoff, capture
    print('G11_PREDEADLINE_ISOLATION_ADAPTER=APPLIED', file=sys.stderr)
    try:
        yield
    finally:
        wild._collect_pre_race_packets, wild.run_predeadline, display.capture_due = originals


def followup_odds(morning, critical, load_snapshot, now, early_sent):
    """Select saved, unexpired odds not covered by an accepted early handoff."""
    day = now.date().isoformat()
    if morning.get('OPERATIONAL_DATE_JST') != day or now.utcoffset() is None:
        raise ValueError('FOLLOWUP_DAY_BINDING')
    cutoff = None
    if early_sent:
        if (critical.get('OPERATIONAL_DATE') != day
                or critical.get('CRITICAL_CHECKPOINT_STATUS') != 'PASS'):
            raise ValueError('FOLLOWUP_CRITICAL_BINDING')
        cutoff = datetime.fromisoformat(critical['CRITICAL_CAPTURE_AT'])
        if cutoff.utcoffset() is None or cutoff > now:
            raise ValueError('FOLLOWUP_CRITICAL_TIME')
    rows = []
    seen = set()
    for task in morning['PREDEADLINE_PLAN']:
        if task.get('KIND') != 'ODDS3T':
            continue
        key = task.get('RACE_KEY', '')
        name = 'odds3t-' + key.lower()
        if (re.fullmatch(day.replace('-', '') + r'-(?:0[1-9]|1[0-9]|2[0-4])-(?:0[1-9]|1[0-2])', key) is None
                or key in seen or task.get('SOURCE_ID') != name):
            raise ValueError('FOLLOWUP_SOURCE_IDENTITY')
        seen.add(key)
        deadline = datetime.fromisoformat(task['MUST_FINISH_BEFORE_JST'])
        if deadline.utcoffset() is None:
            raise ValueError('FOLLOWUP_DEADLINE_ZONE')
        if now >= deadline:
            continue
        verified = load_snapshot(name)
        if verified is None:
            continue
        payload = verified['SNAPSHOT']['PAYLOAD']
        if (payload.get('RACE_KEY') != key or payload.get('KIND') != 'ODDS3T'
                or payload.get('STATUS') != 'PREDEADLINE_SOURCE_COMPLETE'):
            raise ValueError('FOLLOWUP_SNAPSHOT_BINDING')
        captured = datetime.fromisoformat(payload['CAPTURED_AT_JST'])
        if captured.utcoffset() is None or not captured <= now < deadline:
            raise ValueError('FOLLOWUP_SNAPSHOT_TIME')
        if cutoff is None or captured > cutoff:
            rows.append({'RACE_KEY': key, 'CAPTURED_AT_JST': captured.isoformat(),
                         'DEADLINE_AT_JST': deadline.isoformat()})
    return sorted(rows, key=lambda row: row['RACE_KEY'])


def dispatch_followup(rows, day, output_path, run=subprocess.run):
    if output_path:
        with open(output_path, 'a') as output:
            output.write('needed=' + str(bool(rows)).lower() + '\n')
    if not rows:
        print('WILD4_FOLLOWUP_HANDOFF=NO_NEW_UNEXPIRED_INPUT')
        return
    run(['gh', 'workflow', 'run', '364923281', '-f', 'mode=wild4',
         '-f', 'operational_date=' + day], check=True)
    if output_path:
        with open(output_path, 'a') as output:
            output.write('sent=true\n')
    print('WILD4_FOLLOWUP_HANDOFF=DISPATCH_ACCEPTED')


def _handoff_main():
    import argparse
    import os
    from zoneinfo import ZoneInfo
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['handoff'])
    parser.add_argument('--state-root', type=Path, required=True)
    parser.add_argument('--operational-date', required=True)
    parser.add_argument('--early-sent', default='')
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'.g11-private'))
    from g11.canonical.official_pipeline import OfficialArtifactStore
    now = datetime.now(ZoneInfo('Asia/Tokyo'))
    if args.operational_date != now.date().isoformat():
        raise ValueError('FOLLOWUP_NOT_CURRENT_DAY')
    root = args.state_root.resolve()
    metadata = json.loads((root/'relay-state.json').read_text())
    if metadata.get('OPERATIONAL_DATE_JST') != args.operational_date:
        raise ValueError('FOLLOWUP_STATE_DAY')
    store = OfficialArtifactStore(root/'store')
    morning = store.load_verified_snapshot('morning', 'official-day-input')['SNAPSHOT']['PAYLOAD']
    def load(name):
        if not (root/'store/snapshots/predeadline'/f'{name}.json').is_file():
            return None
        return store.load_verified_snapshot('predeadline', name)
    rows = followup_odds(morning, metadata.get('CRITICAL', {}), load, now,
                         args.early_sent == 'true')
    print('G11_WILD4_FOLLOWUP_INPUTS=' + json.dumps(rows, sort_keys=True))
    dispatch_followup(rows, args.operational_date, os.environ.get('GITHUB_OUTPUT'))
    return 0


if __name__ == '__main__':
    raise SystemExit(_handoff_main())
