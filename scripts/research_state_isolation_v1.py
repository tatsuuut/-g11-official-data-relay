"""Research-only bundles; canonical forecasts never accept whole-state writes.

The research worker reads an authenticated canonical copy. Only named research
outputs cross its persistence boundary. A closed bundle may be handed to NIGHT
before settlement, with original bytes and original capture timestamps intact.
"""
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile

JST = timezone(timedelta(hours=9))
SCHEMA = 'G11_ISOLATED_RESEARCH_STATE_20261114_V1'
EFFECTIVE_FROM = '2026-11-14'
MARKER = 'research-isolation/{day}.json'
DEME = 'store/shadow/original-display/v2/deme-discovery.json'


def require(ok, code):
    if not ok:
        raise ValueError(code)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode()


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.isolation-tmp')
    require(not temporary.exists() and not temporary.is_symlink(), 'RESEARCH_TEMP_EXISTS')
    try:
        with temporary.open('xb') as output:
            os.chmod(temporary, 0o600)
            output.write(data); output.flush(); os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def allowed(name, day, *, night=False):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or str(path) != name:
        return False
    key = re.escape(day.replace('-', '')) + r'-\d{2}-(?:0[1-9]|1[0-2])'
    if name == DEME:
        return True
    if re.fullmatch(r'store/snapshots/predeadline/mashiro-v4-shadow-' + key + r'\.json', name):
        return True
    if not night and re.fullmatch(r'store/snapshots/morning/current-growth-p3-confidence-' + key + r'\.json', name):
        return True
    if re.fullmatch(r'store/g11/wild-pack-oos/pre_race/' + re.escape(day)
                    + r'/collection-packet-' + key + r'\.json', name):
        return True
    return name in {
        'store/g11/wild-pack-oos/pre_race/status.json',
        'store/g11/wild-pack-oos/morning/status.json',
        'store/g11/wild-pack-oos/morning/bet-status.json',
        f'store/g11/wild-pack-oos/morning/{day}/bet-prediction-time-odds.json.gz',
        f'store/g11/wild-pack-oos/morning/{day}/bet-prediction-time-odds.manifest.json',
    }


def safe_file(root, name):
    relative = PurePosixPath(name)
    require(not relative.is_absolute() and '..' not in relative.parts
            and str(relative) == name, 'RESEARCH_PATH_TRAVERSAL')
    root, path = Path(root).absolute(), Path(root).absolute()/name
    require(path.is_relative_to(root) and not any(p.is_symlink() for p in
            [path, *path.parents] if p.is_relative_to(root)), 'RESEARCH_PATH_SYMLINK')
    return path


def research_files(state, day):
    roots = [state/'store/snapshots/morning', state/'store/snapshots/predeadline',
             state/'store/g11/wild-pack-oos', state/'store/shadow/original-display/v2']
    result = {}
    for root in roots:
        for path in root.rglob('*') if root.exists() else []:
            name = path.relative_to(state).as_posix()
            if allowed(name, day):
                require(not path.is_symlink(), 'RESEARCH_SOURCE_SYMLINK')
                if path.is_file():
                    result[name] = path
    return result


def core_sources(state, day):
    result = {}
    for stage in ('morning', 'predeadline'):
        for path in (state/'store/snapshots'/stage).glob('*.json'):
            name = path.relative_to(state).as_posix()
            if not allowed(name, day):
                require(not path.is_symlink(), 'RESEARCH_CORE_SYMLINK')
                result[name] = digest(path)
    return result


def protected_metadata(state, day):
    """Catch accidental writes in the disposable copy without rereading history."""
    return {p.relative_to(state).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in state.rglob('*') if p.is_file()
            and not allowed(p.relative_to(state).as_posix(), day)}


def activate(state, day, *, now=None):
    now = now or datetime.now(JST)
    require(day >= EFFECTIVE_FROM and now.date().isoformat() == day, 'RESEARCH_ACTIVATION_DAY')
    path = state/MARKER.format(day=day)
    if path.exists():
        value = json.loads(path.read_bytes())
        require(value.get('schema') == SCHEMA and value.get('day') == day, 'RESEARCH_ACTIVATION_CONFLICT')
        return value
    value = {'schema': SCHEMA, 'day': day, 'activated_at': now.isoformat(),
             'canonical_forecast_writer': False, 'research_separate_group': True}
    atomic(path, canonical(value)+b'\n')
    return value


def plan_deadlines(state, day):
    envelope = json.loads((state/'store/snapshots/morning/official-day-input.json').read_bytes())
    require(envelope.get('STAGE') == 'MORNING' and envelope.get('NAME') == 'official-day-input',
            'RESEARCH_OFFICIAL_PLAN')
    result = {}
    for task in envelope['PAYLOAD']['PREDEADLINE_PLAN']:
        key = task['RACE_KEY']; due = datetime.fromisoformat(task['DUE_AT_JST'])
        deadline = datetime.fromisoformat(task['MUST_FINISH_BEFORE_JST'])
        require(re.fullmatch(day.replace('-', '')+r'-\d{2}-(?:0[1-9]|1[0-2])', key)
                and due.utcoffset() == deadline.utcoffset() == timedelta(hours=9)
                and deadline.date().isoformat() == day and due < deadline
                and (key not in result or result[key] == deadline), 'RESEARCH_PLAN_BINDING')
        result[key] = deadline
    require(bool(result), 'RESEARCH_PLAN_EMPTY')
    return result


def timely_member(path, name, day, deadlines, mtime_ns):
    match = re.search(day.replace('-', '')+r'-\d{2}-(?:0[1-9]|1[0-2])', name)
    if not match:
        return True
    key = match.group(); require(key in deadlines, 'RESEARCH_MEMBER_UNPLANNED')
    deadline = deadlines[key]
    if datetime.fromtimestamp(mtime_ns/1e9, JST) >= deadline:
        return False
    value = json.loads(path.read_bytes())
    payload = value.get('PAYLOAD', value)
    require(payload.get('RACE_ID', payload.get('RACE_KEY', key)) == key,
            'RESEARCH_MEMBER_RACE')
    for field in ('PREDICTION_AT', 'PREDICTION_LOCKED_AT', 'LOCKED_AT',
                  'EVIDENCE_CAPTURED_AT_JST', 'CAPTURED_AT_JST'):
        if payload.get(field):
            at = datetime.fromisoformat(payload[field])
            require(at.utcoffset() is not None, 'RESEARCH_MEMBER_TIMEZONE')
            if at >= deadline:
                return False
    require(not any(payload.get(k) is True for k in ('RESULT_INPUT_USED', 'PAYOUT_INPUT_USED',
            'RESULT_USED_AS_PREDICTION_FEATURE', 'PAYOUT_USED_AS_PREDICTION_FEATURE',
            'RESULT_FEEDBACK_TO_MODEL')), 'RESEARCH_FUTURE_INFORMATION')
    return True


def export_bundle(state, destination, day, binding, source_key, seeds, sources,
                  *, closed=False, now=None):
    now = now or datetime.now(JST)
    deadlines = plan_deadlines(state, day)
    require(not closed or now >= max(deadlines.values()), 'RESEARCH_CLOSE_BEFORE_LAST_DEADLINE')
    require(not destination.exists(), 'RESEARCH_EXPORT_EXISTS')
    (destination/'files').mkdir(parents=True)
    members = {}; held = []; total = 0
    for name, path in sorted(research_files(state, day).items()):
        info = path.stat()
        if not timely_member(path, name, day, deadlines, info.st_mtime_ns):
            held.append(name); continue
        total += info.st_size
        require(info.st_size <= 512*1024*1024 and total <= 768*1024*1024, 'RESEARCH_BUNDLE_BUDGET')
        target = safe_file(destination/'files', name)
        target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(path, target)
        members[name] = {'sha256': digest(target), 'size': info.st_size,
                         'mtime_ns': info.st_mtime_ns, 'seed_sha256': seeds.get(name)}
    value = {'schema': SCHEMA, 'day': day, 'binding': binding, 'source_checkpoint': source_key,
             'source_snapshots': sources, 'members': members, 'closed': closed,
             'created_at': now.isoformat(), 'held_late_count': len(held),
             'prediction_recalculation': 0, 'canonical_state_written': False}
    value['bundle_sha256'] = hashlib.sha256(canonical(value)).hexdigest()
    atomic(destination/'manifest.json', canonical(value)+b'\n')
    return value


def read_bundle(root, day):
    value = json.loads((root/'manifest.json').read_bytes())
    unsigned = dict(value); supplied = unsigned.pop('bundle_sha256', None)
    require(value.get('schema') == SCHEMA and value.get('day') == day
            and supplied == hashlib.sha256(canonical(unsigned)).hexdigest()
            and value.get('prediction_recalculation') == 0
            and value.get('canonical_state_written') is False
            and type(value.get('closed')) is bool, 'RESEARCH_BUNDLE_IDENTITY')
    require(type(value.get('members')) is dict and len(value['members']) <= 10000,
            'RESEARCH_BUNDLE_MEMBERS')
    require(type(value.get('source_snapshots')) is dict
            and len(value['source_snapshots']) <= 10000, 'RESEARCH_BUNDLE_SOURCE_BUDGET')
    require(re.fullmatch(r'g11-state-v4-(?:critical|published)-'+re.escape(day)
            +r'-\d+(?:-\d+-checkpoint)?', str(value.get('source_checkpoint',''))),
            'RESEARCH_BUNDLE_CHECKPOINT')
    at = datetime.fromisoformat(value['created_at'])
    require(at.utcoffset() == timedelta(hours=9) and at.date().isoformat() >= day,
            'RESEARCH_BUNDLE_CREATED_AT')
    actual = {p.relative_to(root/'files').as_posix()
              for p in (root/'files').rglob('*') if p.is_file() or p.is_symlink()}
    require(actual == set(value['members']), 'RESEARCH_BUNDLE_UNLISTED_FILES')
    total = 0
    for name, info in value['members'].items():
        require(allowed(name, day), 'RESEARCH_BUNDLE_PROTECTED_PATH')
        require(type(info) is dict and type(info.get('size')) is int
                and 0 <= info['size'] <= 512*1024*1024
                and type(info.get('mtime_ns')) is int and 0 < info['mtime_ns'] <= int(at.timestamp()*1e9)
                and re.fullmatch('[a-f0-9]{64}', str(info.get('sha256','')))
                and (info.get('seed_sha256') is None or
                     re.fullmatch('[a-f0-9]{64}', str(info['seed_sha256']))),
                'RESEARCH_BUNDLE_MEMBER_METADATA')
        total += info['size']
        require(total <= 768*1024*1024, 'RESEARCH_BUNDLE_BUDGET')
        path = safe_file(root/'files', name)
        require(path.is_file() and path.stat().st_size == info['size']
                and digest(path) == info['sha256']
                and int(path.stat().st_mtime) == info['mtime_ns']//10**9,
                'RESEARCH_BUNDLE_FILE_READBACK')
    return value


def validate_source(value, state, day, binding):
    require(value['binding'] == binding, 'RESEARCH_BUNDLE_FROZEN_AUTHORITY')
    for name, expected in value['source_snapshots'].items():
        require(name.startswith(('store/snapshots/morning/', 'store/snapshots/predeadline/'))
                and not allowed(name, day), 'RESEARCH_BUNDLE_SOURCE_PATH')
        path = safe_file(state, name)
        require(path.is_file() and digest(path) == expected, 'RESEARCH_BUNDLE_SOURCE_CHANGED')
    deadlines = plan_deadlines(state, day)
    if value['closed']:
        require(datetime.fromisoformat(value['created_at']) >= max(deadlines.values()),
                'RESEARCH_BUNDLE_FALSE_CLOSE')
    for name, info in value['members'].items():
        require(timely_member(value['_root']/'files'/name, name, day, deadlines, info['mtime_ns']),
                'RESEARCH_BUNDLE_LATE_MEMBER')


def apply_files(value, state, day, *, night=False, after_copy=None):
    selected = [(name, info) for name, info in value['members'].items() if allowed(name, day, night=night)]
    for name, info in selected:
        target = safe_file(state, name)
        current = digest(target) if target.is_file() else None
        require(current in (info['seed_sha256'], info['sha256']), 'RESEARCH_SEED_CONFLICT')
        if '/snapshots/' in name or '/collection-packet-' in name:
            require(current is None or current == info['sha256'], 'RESEARCH_IMMUTABLE_CONFLICT')
    with tempfile.TemporaryDirectory(prefix='.research-rollback-', dir=state.parent) as tmp:
        backup = Path(tmp); done = []
        try:
            for name, info in selected:
                target = safe_file(state, name)
                if target.is_file() and digest(target) == info['sha256']:
                    continue
                prior = backup/name
                if target.exists():
                    prior.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(target, prior)
                done.append((target, prior))
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name+'.research-import')
                require(not temporary.exists() and not temporary.is_symlink(), 'RESEARCH_IMPORT_TEMP_EXISTS')
                try:
                    shutil.copy2(value['_root']/'files'/name, temporary)
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
            if after_copy:
                after_copy()
        except Exception:
            for target, prior in reversed(done):
                if prior.exists():
                    shutil.copy2(prior, target)
                else:
                    target.unlink(missing_ok=True)
            raise
    return len(selected)


def bundle_payload_sha(value, day):
    return hashlib.sha256(canonical({name: info['sha256'] for name, info in value['members'].items()
        if allowed(name, day, night=True)})).hexdigest()
