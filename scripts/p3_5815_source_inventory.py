"""Inspect retained historical projection inputs without changing production."""
import collections
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tarfile

archive = Path(os.environ['RUNNER_TEMP']) / 'p3-history-source.tgz'
report = {'schema': 'G11_P3_5815_REPLAY_SOURCE_INVENTORY_V1',
          'production_mutated': False, 'prediction_recalculated': False,
          'snapshot_reconstruction': False}
if not archive.is_file():
    report['status'] = 'CACHE_UNAVAILABLE'
else:
    roots = collections.Counter()
    feeds, proofs, true_days = [], {}, collections.defaultdict(list)
    with tarfile.open(archive, 'r|gz') as handle:
        for member in handle:
            p = PurePosixPath(member.name)
            if p.is_absolute() or '..' in p.parts or member.issym() or member.islnk():
                raise ValueError('UNSAFE_MEMBER')
            name = '/'.join(p.parts)
            if not member.isfile():
                continue
            roots['/'.join(p.parts[:2])] += 1
            if 'true-ai-history/' in name and member.name.endswith('.manifest.json'):
                true_days[str(p.parent)].append(p.name)
            if name.startswith('daily-runtime/') and p.name.endswith('.json'):
                if p.name not in {'night-feed.json', 'morning-feed.json', 'night-checkpoint.json', 'authority.json'}:
                    continue
                if member.size > 30_000_000:
                    raise ValueError('OVERSIZE_PROJECTION')
                raw = handle.extractfile(member).read()
                obj = json.loads(raw)
                day = p.parts[1]
                if p.name != 'night-feed.json':
                    proofs.setdefault(day, {})[p.name] = obj
                    continue
                rows = obj.get('races', [])
                live = [r for r in rows if isinstance(r.get('predeadline'), dict)]
                impact = [r for r in live if len((r['predeadline'].get('pre_race_impact') or {}).get('BOATS', [])) == 6]
                item = {'path': name, 'sha256': hashlib.sha256(raw).hexdigest(),
                        'date': obj.get('operational_date_jst'), 'stage': obj.get('stage'),
                        'status': obj.get('status'), 'rows': len(rows), 'live': len(live),
                        'impact_six': len(impact), 'top_fields': sorted(obj),
                        'source': obj.get('source'), 'daily_p3': obj.get('daily_runtime', {}).get('p3'),
                        'selection_policies': dict(collections.Counter((r['predeadline'].get('trifecta_confidence') or {}).get('selection_policy') for r in live))}
                if live and day >= '2026-10-22':
                    r = (impact or live)[0]
                    item['sample'] = {'key': r['key'], 'race_fields': sorted(r),
                                      'live_fields': sorted(r['predeadline']),
                                      'live': r['predeadline'],
                                      'confidence': r['predeadline'].get('trifecta_confidence'),
                                      'current_p3': r.get('current_p3')}
                    item['p3_race_fields'] = {k:v for k,v in r.items() if 'p3' in k or 'growth' in k}
                feeds.append(item)
    for item in feeds:
        day_proof = proofs.get(item['date'], {})
        check = day_proof.get('night-checkpoint.json', {})
        item['checkpoint_feed_hash_matches'] = check.get('feed_sha256') == item['sha256']
        item['checkpoint_fields'] = sorted(check)
        authority = day_proof.get('authority.json', {})
        item['authority'] = {k:v for k,v in authority.items() if k in {'runtime_source_sha','source_archive_sha256','source_archive','p3','authority_sha256'}}
        item['p3_source_files'] = {k:v for k,v in authority.get('source_files', {}).items() if 'growth_p3/v1_1/' in k and k.endswith('.py') and '/test_' not in k}
    report.update(status='COMPLETE', member_groups=dict(roots),
                  retained_night_projections=sorted(feeds, key=lambda r:r['date']),
                  true_source_manifests=dict(true_days))
print('P3_INITIAL_SOURCE_JSON=' + json.dumps(report, ensure_ascii=False, separators=(',', ':')))
