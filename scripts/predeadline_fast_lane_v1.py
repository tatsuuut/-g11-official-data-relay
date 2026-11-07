"""Short serialized LIVE cycle, effective 2026-11-08 JST.

Only orchestration changes: frozen official capture/predictors still own LOCKs.
After their verified publication, retain saved-input WILD/P3 research sidecars,
without a second official collection or repeated morning model preparation.
"""
from datetime import date, datetime, timedelta, timezone
import argparse
import json
import os
from pathlib import Path
import sys

EFFECTIVE_FROM = '2026-11-08'
JST = timezone(timedelta(hours=9))


def enabled(day, phase, frozen, rescue, now):
    date.fromisoformat(day)
    return (day >= EFFECTIVE_FROM and now.astimezone(JST).date().isoformat() == day
            and phase == 'predeadline' and frozen == 'true' and rescue != 'true')


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
    p.add_argument('mode', choices=['select', 'sidecars'])
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
