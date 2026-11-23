"""Read one verified checkpoint; export only saved morning evidence encrypted.
No acquisition, prediction, runtime execution, state save or Site request.
"""
import hashlib, io, json, os, pathlib, tarfile

DAY = '2026-11-24'
AUTHORITY = '93b88a4386cceed68ded7ccb4cf3a645258617e894f13b46290ff01f3d70635f'
SOURCE = '678550268e42fa22a700ed83e3afab98bf72d132'
MAX_FILE = 32 * 1024 * 1024
MAX_TOTAL = 160 * 1024 * 1024

def canonical(v):
    return json.dumps(v, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()

def digest(v):
    return hashlib.sha256(v).hexdigest()

def run():
    selected = {}
    index = []
    total = 0
    with tarfile.open('.audit/state.tar.gz', 'r:gz') as source:
        for member in source:
            name = member.name.removeprefix('./')
            p = pathlib.PurePosixPath(name)
            if not member.isfile() or p.is_absolute() or '..' in p.parts:
                continue
            wanted = (name.startswith('store/snapshots/morning/') and name.endswith('.json')
                or name.startswith('daily-runtime/' + DAY + '/') and name.endswith('.json')
                or name == 'relay-state.json')
            if not wanted:
                continue
            if member.size > MAX_FILE or total + member.size > MAX_TOTAL or len(selected) >= 3000:
                raise RuntimeError('AUDIT_EVIDENCE_BUDGET')
            if name in selected:
                raise RuntimeError('AUDIT_DUPLICATE_MEMBER')
            raw = source.extractfile(member).read(MAX_FILE + 1)
            if len(raw) != member.size:
                raise RuntimeError('AUDIT_MEMBER_SIZE')
            selected[name] = raw
            total += len(raw)
            index.append({'path': name, 'size': len(raw), 'sha256': digest(raw)})
    authority = json.loads(selected['daily-runtime/' + DAY + '/authority.json'])
    unsigned = dict(authority)
    actual = unsigned.pop('authority_sha256', None)
    if not (actual == AUTHORITY == digest(canonical(unsigned))
            and authority['operational_date_jst'] == DAY
            and authority['runtime_source_sha'] == SOURCE and authority['immutable'] is True):
        raise RuntimeError('AUDIT_DAILY_AUTHORITY_BINDING')
    if not any(x['path'].startswith('store/snapshots/morning/') for x in index):
        raise RuntimeError('AUDIT_MORNING_MISSING')
    manifest = {'schema': 'G11_READ_ONLY_SAVED_MORNING_AUDIT_V1', 'day': DAY,
        'original_run': '45243836258', 'cache_key': os.environ['AUDIT_CACHE_KEY'],
        'authority_sha256': AUTHORITY, 'runtime_source_sha': SOURCE,
        'cipher_sha256': digest(pathlib.Path('.relay-cache/state.tgz.gpg').read_bytes()),
        'members': index, 'official_http': 0, 'prediction_calls': 0,
        'canonical_state_written': False}
    with tarfile.open('.audit/selected.tar.gz', 'w:gz') as output:
        for name, raw in [('audit-manifest.json', canonical(manifest)), *selected.items()]:
            m = tarfile.TarInfo(name); m.size = len(raw); m.mode = 0o600
            output.addfile(m, io.BytesIO(raw))
    print('AUDIT_SELECTED_FILES=' + str(len(index)))
    print('AUDIT_SELECTED_SHA256=' + digest(pathlib.Path('.audit/selected.tar.gz').read_bytes()))
    print('AUDIT_DAILY_AUTHORITY=PASS OFFICIAL_HTTP=0 PREDICTION_CALLS=0 STATE_WRITES=0')

if __name__ == '__main__':
    run()
