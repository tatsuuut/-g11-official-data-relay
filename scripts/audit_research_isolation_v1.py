"""Read-only integration audit on disposable authenticated saved state only.

The future close clock is injected solely to exercise the handover validator.
No forecast is generated with that clock, no cache is saved and no API is posted.
"""
import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import shutil
import sys

import isolated_research_runner_v1 as runner
import research_state_isolation_v1 as data


def protected(state, day):
    return {p.relative_to(state).as_posix(): data.digest(p)
            for p in state.rglob('*') if p.is_file()
            and not data.allowed(p.relative_to(state).as_posix(), day)}


def audit(state, source, deme_source, day, source_key):
    data.activate(state, day)
    before = protected(state, day)
    work = state.with_name('research-audit-working')
    shutil.copytree(state, work)
    first, second = state.with_name('research-audit-first'), state.with_name('research-audit-second')
    feed = state.with_name('research-audit-output')/'feed.json'
    runner.run_research(work, source, deme_source, state.with_name('no-previous-bundle'),
                        first, day, source_key, feed)
    data.require(protected(work, day) == before, 'AUDIT_PROTECTED_BYTES_CHANGED')
    data.require(feed.is_file() and (feed.parent/'deme-discovery-public.json').is_file(),
                 'AUDIT_RESEARCH_PROJECTION_MISSING')
    # Recreate the next run's canonical research seed without copying history again.
    for name, path in data.research_files(work, day).items():
        path.unlink()
    for name, path in data.research_files(state, day).items():
        target = work/name; target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    runner.run_research(work, source, deme_source, first, second, day, source_key, feed)
    data.require(protected(work, day) == before, 'AUDIT_RETRY_PROTECTED_BYTES_CHANGED')
    authority, binding = runner.authority_binding(state, day, source)
    seeds = {n:data.digest(p) for n,p in data.research_files(state,day).items()}
    closed = state.with_name('research-audit-closed')
    close_clock = max(data.plan_deadlines(state,day).values())+timedelta(seconds=1)
    close_clock = max(close_clock, datetime.now(data.JST))
    data.export_bundle(work,closed,day,binding,source_key,seeds,data.core_sources(state,day),
                       closed=True,now=close_clock)
    # The injected clock is used only here and in export, never in research capture.
    runner.import_night(state,source,closed,day,now=close_clock)
    authority.verify_runtime(state,day,source)
    authority.verify_locks(state,day,'MORNING',exact=True)
    authority.verify_locks(state,day,'PREDEADLINE',exact=True)
    after = protected(state, day)
    marker = data.MARKER.format(day=day)
    bindings = f'daily-runtime/{day}/bindings/PREDEADLINE/'
    changed = {n for n in set(before)|set(after) if before.get(n)!=after.get(n)}
    data.require(all(n==marker or n.startswith(bindings) for n in changed),
                 'AUDIT_NIGHT_PROTECTED_BYTES_CHANGED')
    print('G11_RESEARCH_SAVED_INTEGRATION='+json.dumps({'status':'PASS',
        'protected_files':len(before),'night_changed_only_binding_and_marker':True,
        'bundle_members':len(data.read_bundle(closed,day)['members']),
        'core_predictions_changed':False,'canonical_publication':False,
        'official_acquisition':0,'simulated_close_validator_only':True},sort_keys=True))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state',type=Path,required=True)
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--deme-source',type=Path,required=True)
    p.add_argument('--source-key',required=True)
    p.add_argument('--day',required=True)
    a=p.parse_args()
    try:
        sys.path.insert(0,str(a.source.resolve()))
        audit(a.state.resolve(),a.source.resolve(),a.deme_source.resolve(),a.day,a.source_key)
    except Exception as exc:
        code=str(exc)
        print('G11_RESEARCH_SAVED_INTEGRATION=FAILED TYPE='+type(exc).__name__+
              (' CODE='+code if code.isupper() and len(code)<100 else ''),flush=True)
        raise SystemExit(1) from None
