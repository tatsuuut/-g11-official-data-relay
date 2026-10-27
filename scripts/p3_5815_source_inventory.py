"""Check retained historical day caches without writing to production."""
import collections, json, os, tarfile
from pathlib import Path, PurePosixPath
p = Path(os.environ['RUNNER_TEMP']) / 'p3-history-source.tgz'
day=os.environ['TARGET_DAY']
r={'schema':'P3_HISTORICAL_CACHE_COVERAGE_V1','day':day,'cache_available':p.is_file(),'production_mutated':False}
if p.is_file():
    counts=collections.Counter()
    samples={}
    with tarfile.open(p,'r|gz') as t:
        for m in t:
            q=PurePosixPath(m.name)
            if q.is_absolute() or '..' in q.parts or m.issym() or m.islnk(): raise ValueError('UNSAFE_MEMBER')
            name='/'.join(q.parts)
            if not m.isfile(): continue
            if name.startswith('store/snapshots/predeadline/current-growth-p3-live-') and q.suffix=='.json':
                counts['p3_live_original']+=1
                if day.replace('-','') in name: counts['target_day_live_original']+=1
                if not samples.get('p3_live'):
                    o=json.loads(t.extractfile(m).read())
                    b=o.get('PAYLOAD',{})
                    samples['p3_live']={'path':name,'keys':sorted(o),'payload_keys':sorted(b),'scoring_policy':b.get('SCORING_POLICY'),'race':b.get('RACE_ID'),'version':b.get('VERSION'),'selection':b.get('BET_SELECTION_MODE')}
            elif name.startswith('store/snapshots/morning/'): counts['morning_snapshot']+=1
    r.update(counts=dict(counts),samples=samples)
print('P3_CACHE_COVERAGE_JSON='+json.dumps(r,ensure_ascii=False,separators=(',',':')))
