"""Short serialized LIVE cycle, effective 2026-11-08 JST.

Only orchestration changes: frozen official capture/predictors still own LOCKs.
After their verified publication, retain saved-input WILD/P3 research sidecars
only when the next official capture has enough time. Checkpoints and delivery
receipts remain durable even when optional research yields its serialized slot.
"""
from datetime import date, datetime, timedelta, timezone
import argparse
import json
import os
from pathlib import Path
import sys

EFFECTIVE_FROM = '2026-11-08'
JST = timezone(timedelta(hours=9))
RESEARCH_RESERVE_SECONDS = 300


def enabled(day, phase, frozen, rescue, now):
    date.fromisoformat(day)
    return (day >= EFFECTIVE_FROM and now.astimezone(JST).date().isoformat() == day
            and phase == 'predeadline' and frozen == 'true' and rescue != 'true')


def research_budget(plan, saved_sources, now):
    """Reserve the next capture window; never fetch, predict or modify a LOCK."""
    if now.utcoffset() != timedelta(hours=9):
        raise ValueError('RESEARCH_BUDGET_TIMEZONE')
    pending = []
    for task in plan:
        due = datetime.fromisoformat(task['DUE_AT_JST'])
        deadline = datetime.fromisoformat(task['MUST_FINISH_BEFORE_JST'])
        source = task['SOURCE_ID']
        if (due.utcoffset() != deadline.utcoffset()
                or due.utcoffset() != timedelta(hours=9)
                or due.date() != now.date() or deadline.date() != now.date()
                or deadline - due != timedelta(minutes=7)
                or not isinstance(source, str) or not source):
            raise ValueError('RESEARCH_BUDGET_PLAN')
        if source not in saved_sources and now < deadline:
            pending.append(due)
    next_due = min(pending, default=None)
    defer = next_due is not None and next_due <= now + timedelta(seconds=RESEARCH_RESERVE_SECONDS)
    return {'deferred': defer, 'reason': 'CAPTURE_DUE_SOON' if defer else 'CLEAR_WINDOW',
            'pending_sources': len(pending),
            'next_due_at_jst': next_due.isoformat() if next_due else None}


def saved_research_budget(state, day, *, now=None, artifact_class=None, runner=None):
    """Read the verified morning scope. Uncertain scheduling defers research."""
    try:
        now = now or datetime.now(JST)
        if now.date().isoformat() != day:
            raise ValueError('RESEARCH_BUDGET_DAY')
        if artifact_class is None:
            from g11.canonical.official_pipeline import CanonicalRunner, OfficialArtifactStore
            artifact_class, runner = OfficialArtifactStore, CanonicalRunner
        morning = artifact_class(state / 'store').load_verified_snapshot('morning', 'official-day-input')
        _, eligible, _ = runner._formal_prediction_scope(morning, failure_prefix='RESEARCH_BUDGET_SCOPE')
        eligible = set(eligible)
        plan = [row for row in morning['SNAPSHOT']['PAYLOAD']['PREDEADLINE_PLAN']
                if row['RACE_KEY'] in eligible]
        saved = {path.stem for path in (state / 'store/snapshots/predeadline').glob('*.json')}
        return research_budget(plan, saved, now)
    except Exception:
        # Optional research must not hold up the next critical cycle if the
        # verified schedule cannot be read. Its own capture guards still apply.
        return {'deferred': True, 'reason': 'BUDGET_UNVERIFIED',
                'pending_sources': None, 'next_due_at_jst': None}


def saved_sidecars(state, day, source, *, authority=None, wild=None,
                   publisher=None, relay=None, now=None):
    now = now or datetime.now(JST)
    if not enabled(day, 'predeadline', 'true', 'false', now):
        raise RuntimeError('FAST_LANE_CURRENT_EFFECTIVE_DAY_REQUIRED')
    metadata = json.loads((state / 'relay-state.json').read_bytes())
    c = metadata.get('CRITICAL', {})
    if (metadata.get('OPERATIONAL_DATE_JST') != day
            or metadata.get('MORNING_COMPLETE') is not True
            or c.get('OPERATIONAL_DATE') != day
            or c.get('CRITICAL_CHECKPOINT_STATUS') != 'PASS'
            or c.get('LAST_CRITICAL_RUN_ID') != os.environ.get('GITHUB_RUN_ID')):
        raise RuntimeError('FAST_LANE_CRITICAL_BINDING')
    if authority is None:
        from g11.relay import daily_runtime_authority_v1 as authority
    binding = authority.verify_runtime(state, day, source)
    if (binding['authority_sha256'] != c.get('AUTHORITY_SHA256')
            or binding['runtime_source_sha'] != c.get('RUNTIME_SOURCE_SHA')):
        raise RuntimeError('FAST_LANE_AUTHORITY_BINDING')
    for phase in ('MORNING', 'PREDEADLINE'):
        authority.verify_locks(state, day, phase, exact=True)
    last_deadline = datetime.fromisoformat(c['LAST_RACE_DEADLINE_JST'])
    if last_deadline.utcoffset() != timedelta(hours=9) or last_deadline.date().isoformat() != day:
        raise RuntimeError('FAST_LANE_LAST_DEADLINE_BINDING')
    if now >= last_deadline:
        return {'p3_status': 'NIGHT_HANDOFF', 'wild_changed': False}
    if wild is None:
        from g11.wild_pack.v1 import runtime_orchestrator_v1 as wild
        from g11.growth_p3.v1_1 import relay_publish_v1 as publisher
        from g11.relay import v1 as relay
    store = state / 'store'
    results = {}
    errors = []
    try:
        # The existing adapter attaches odds first, isolates malformed races,
        # and rejects PRE_RACE records actually saved at/after the deadline.
        try:
            results['wild_changed'] = wild.run_predeadline(store, date.fromisoformat(day))
        except Exception as exc:
            errors.append('WILD:' + type(exc).__name__)
        # Preserve prospective confidence and MASHIRO evidence. Morning and
        # LIVE predictors are idempotent and retain their existing cutoff guards.
        try:
            result = publisher.publish_future(store, relay._morning_projection(store),
                datetime.now(JST), include_live=True, include_research=True,
                strict_deadline=True)
            results['p3_status'] = result['STATUS']
        except Exception as exc:
            errors.append('P3:' + type(exc).__name__)
    finally:
        # Even a partial optional failure must preserve earlier immutable LOCKs
        # and seal only valid new predeadline evidence before checkpointing.
        authority.verify_locks(state, day, 'MORNING', exact=True)
        authority.verify_locks(state, day, 'PREDEADLINE', exact=True, allow_append=True)
        authority.seal(state, day, 'PREDEADLINE')
    if errors:
        raise RuntimeError('FAST_SIDECARS_PARTIAL:' + ','.join(errors))
    return results


def main():
    p = argparse.ArgumentParser()
    p.add_argument('mode', choices=['select', 'budget', 'sidecars'])
    p.add_argument('--day', required=True)
    p.add_argument('--phase', default='')
    p.add_argument('--frozen', default='')
    p.add_argument('--rescue', default='')
    p.add_argument('--state-root', type=Path)
    args = p.parse_args()
    if args.mode == 'select':
        active = enabled(args.day, args.phase, args.frozen, args.rescue, datetime.now(JST))
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write('enabled=' + str(active).lower() + '\n')
        print('G11_FAST_LANE=' + ('ENABLED' if active else 'LEGACY'))
        return
    source = Path.cwd()
    sys.path.insert(0, str(source))
    if args.mode == 'budget':
        result = saved_research_budget(args.state_root.resolve(), args.day)
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write('deferred=' + str(result['deferred']).lower() + '\n')
        print('G11_FAST_RESEARCH_BUDGET=' + json.dumps(result, sort_keys=True))
        return
    from daily_predeadline_authority_recovery_v1 import apply, guard_optional_mashiro_shadow
    from g11.relay import daily_runtime_authority_v1 as authority
    from predeadline_race_isolation_v1 import isolate_predeadline_inputs
    from rescue_night_result_parser_v1 import official_input_compatibility
    apply(authority, args.day)
    guard_optional_mashiro_shadow()
    with official_input_compatibility(), isolate_predeadline_inputs():
        result = saved_sidecars(args.state_root.resolve(), args.day, source, authority=authority)
    print('G11_FAST_SAVED_RESEARCH=' + json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
