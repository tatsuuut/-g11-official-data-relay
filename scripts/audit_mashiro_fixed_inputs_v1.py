"""Read-only saved-input audit or bounded daily evidence export; no acquisition."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state-root',type=Path,required=True)
    p.add_argument('--source-root',type=Path,required=True)
    p.add_argument('--observer-root',type=Path,required=True)
    p.add_argument('--day',required=True)
    p.add_argument('--archive',type=Path)
    p.add_argument('--source-checkpoint',default='')
    a=p.parse_args()
    sys.path.insert(0,str(a.source_root.resolve()))
    from g11.canonical.official_pipeline import OfficialArtifactStore
    path=a.observer_root/'g11/research/mashiro_fixed_v1/observer.py'
    spec=importlib.util.spec_from_file_location('g11_fixed_mashiro_audit',path)
    observer=importlib.util.module_from_spec(spec);spec.loader.exec_module(observer)
    state=a.state_root.resolve();store=OfficialArtifactStore(state/'store')
    before={p.relative_to(state).as_posix():(p.stat().st_size,p.stat().st_mtime_ns)
            for p in state.rglob('*') if p.is_file()}
    if a.archive:
        result=observer.export_evidence(store,a.day,a.archive.resolve(),{'source_checkpoint':a.source_checkpoint})
    else:
        result=observer.audit_inputs(store,a.day)
    after={p.relative_to(state).as_posix():(p.stat().st_size,p.stat().st_mtime_ns)
           for p in state.rglob('*') if p.is_file()}
    if before!=after:raise ValueError('MASHIRO_FIXED_AUDIT_MUTATED_INPUT')
    print('G11_MASHIRO_FIXED_SAVED_AUDIT='+json.dumps(result,sort_keys=True),flush=True)
    if result['STATUS']!='PASS':raise SystemExit(1)


if __name__=='__main__':main()
