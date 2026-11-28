"""One read-only recovery inventory; never run a private prediction module.

select reads cache metadata; extract authenticates a disposable checkpoint;
check requires an OS read-only mount and a separate empty network namespace.
No report contains source text, tickets, input values, credentials, or URLs.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tarfile

REPOSITORY = 'tatsuuut/-g11-official-data-relay'
DAY = '2026-11-28'
DAYS = ('2026-11-27', DAY)
HEX = re.compile(r'[0-9a-f]{64}')
SOURCE_HEX = re.compile(r'[0-9a-f]{40}')
EXTRACT_HELPER_SHA = 'c35c9fe498a12fb036608e42facced6aea6b399b9b1ac30ff7a53595369d69d1'
MODEL_ROOTS = ('g11/true-ai-models', 'g11/wild-pack-models')
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_FILES = 200000
MAX_ROWS = 20000


def require(value, code):
    if not value:
        raise RuntimeError(code)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def file_sha(path):
    value = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def read_json(path):
    require(path.is_file() and not path.is_symlink(), 'REGULAR_JSON_REQUIRED')
    require(path.stat().st_size <= MAX_JSON_BYTES, 'JSON_SIZE_LIMIT')
    value = json.loads(path.read_bytes())
    require(type(value) is dict, 'JSON_OBJECT_REQUIRED')
    return value


def safe_hash(value, *, source=False):
    pattern = SOURCE_HEX if source else HEX
    return value if type(value) is str and pattern.fullmatch(value) else None


def safe_error(exc):
    code = str(exc)
    return code if re.fullmatch(r'[A-Z][A-Z0-9_]{1,100}', code) else type(exc).__name__.upper()


def storage_evidence():
    report = {}
    for endpoint, fields in (('storage-limit', ('max_cache_size_gb',)),
                             ('usage', ('active_caches_count', 'active_caches_size_in_bytes'))):
        label = endpoint.replace('-', '_')
        try:
            result = subprocess.run(['gh', 'api', 'repos/' + REPOSITORY + '/actions/cache/' + endpoint],
                                    capture_output=True, text=True, timeout=40, check=False)
            require(result.returncode == 0, 'CACHE_STORAGE_READ_UNAVAILABLE')
            value = json.loads(result.stdout)
            require(type(value) is dict and all(type(value.get(name)) in (int, float)
                    and value[name] >= 0 for name in fields), 'CACHE_STORAGE_SHAPE_UNAVAILABLE')
            report[label] = {name: value[name] for name in fields}
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
            report[label] = {'error_code': safe_error(exc)}
    return report


# Restore only original checkpoints from the observed incident runs.
ORIGINAL_RECOVERY = {
    '2026-11-28': (
        '819fd40eb3a5000707af4061529a7686260cfdc6dfe889cba33e56ffa272b9d4',
        '614905a4001d6f4392ece21f49a30b3b4991581c',
        ('g11-state-v4-published-2026-11-28-46043616172',
         'g11-state-v4-published-2026-11-28-46043616172-1-checkpoint',
         'g11-state-v3-2026-11-28-46043616172')),
    '2026-11-27': (
        '01a42edf0de4733183f8637e001565f2d86298de1c33a8ecb0ab8b0e84a30fb6',
        '6797fa3205f62e0f969b0d76b5612aeb556802fe',
        ('g11-state-v4-published-2026-11-27-46045867781',
         'g11-state-v3-2026-11-27-46045867781',
         'g11-state-v4-published-2026-11-27-45994034079',
         'g11-state-v3-2026-11-27-45994034079')),
}


def select_original(day):
    from urllib.parse import urlencode
    require(os.environ.get('GITHUB_REPOSITORY') == REPOSITORY, 'REPOSITORY_BINDING')
    require(os.environ.get('GITHUB_REF') == 'refs/heads/main', 'REF_BINDING')
    require(day in ORIGINAL_RECOVERY, 'ORIGINAL_DAY_SCOPE')
    for key in ORIGINAL_RECOVERY[day][2]:
        query = urlencode({'key': key, 'ref': 'refs/heads/main', 'per_page': 100})
        result = subprocess.run(['gh', 'api', 'repos/' + REPOSITORY + '/actions/caches?' + query],
                                capture_output=True, text=True, timeout=40, check=False)
        require(result.returncode == 0, 'CACHE_METADATA_READ_FAILED')
        value = json.loads(result.stdout)
        require(type(value) is dict and type(value.get('actions_caches')) is list
                and type(value.get('total_count')) is int and value['total_count'] <= 100,
                'CACHE_METADATA_SHAPE')
        matches = [r for r in value['actions_caches'] if type(r) is dict and r.get('key') == key
                   and r.get('ref') == 'refs/heads/main' and type(r.get('size_in_bytes')) is int
                   and r['size_in_bytes'] > 0]
        require(len(matches) <= 1, 'EXACT_CACHE_AMBIGUOUS')
        if matches:
            with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                output.write('cache_key=' + key + '\n')
            return {'status': 'EXACT_ORIGINAL_SELECTED', 'day': day,
                    'cache_key_sha256': sha(key.encode()), 'encrypted_bytes': matches[0]['size_in_bytes']}
    raise RuntimeError('ORIGINAL_CHECKPOINT_NOT_RETAINED')


def verify_original_inventory(report, day):
    authority, source, _ = ORIGINAL_RECOVERY[day]
    found = report['days'][day]
    require(found.get('authority_present') == 1 and found.get('authority_self_hash_valid') == 1
            and found.get('authority_sha256') == authority and found.get('runtime_source_sha') == source
            and found.get('source_archive_present') == 1 and found.get('source_archive_bytes_match') == 1,
            'ORIGINAL_AUTHORITY_NOT_VERIFIED')
    def complete(binding, nonempty=False):
        return (type(binding) is dict and not binding.get('error_codes')
                and binding.get('missing_file_count') == 0 and binding.get('mismatched_file_count') == 0
                and type(binding.get('bound_file_count')) is int
                and binding.get('matched_file_count') == binding['bound_file_count']
                and (not nonempty or binding['bound_file_count'] > 0))
    require(complete(found.get('model_binding'), True), 'ORIGINAL_MODELS_NOT_VERIFIED')
    phases = found.get('phases', {})
    require(complete(phases.get('MORNING'), True) and phases['MORNING'].get('binding_record_count', 0) > 0
            and complete(phases.get('PREDEADLINE')) and phases['PREDEADLINE'].get('binding_record_count', 0) > 0
            and safe_hash(report.get('morning_input_file_sha256')), 'ORIGINAL_LOCKS_NOT_VERIFIED')
    results = report['previous_result_sources'].get('result-k-20261127', {})
    return {'status': 'ORIGINAL_CHECKPOINT_RESTORED_AND_INVENTORY_VERIFIED', 'day': day,
            'authority_sha256': authority, 'morning_files': phases['MORNING']['matched_file_count'],
            'predeadline_files': phases['PREDEADLINE']['matched_file_count'],
            'model_files': found['model_binding']['matched_file_count'],
            'night_input_present': bool(report.get('night_input_file_sha256')),
            'nov27_result_bytes_match': results.get('matching_response_byte_count') == 1,
            'production_mutated': False, 'prediction_executed': False, 'site_write': False}


def select_cache():
    require(os.environ.get('GITHUB_REPOSITORY') == REPOSITORY, 'REPOSITORY_BINDING')
    require(os.environ.get('GITHUB_REF') == 'refs/heads/main', 'REF_BINDING')
    rows = []
    for lane in ('critical', 'published'):
        endpoint = ('repos/' + REPOSITORY + '/actions/caches?key=g11-state-v4-'
                    + lane + '-' + DAY + '-&ref=refs%2Fheads%2Fmain'
                    '&per_page=100&sort=created_at&direction=desc')
        result = subprocess.run(['gh', 'api', endpoint], capture_output=True,
                                text=True, timeout=40, check=False)
        require(result.returncode == 0, 'CACHE_METADATA_READ_FAILED')
        value = json.loads(result.stdout)
        require(type(value) is dict and type(value.get('actions_caches')) is list,
                'CACHE_METADATA_SHAPE')
        require(value.get('total_count', 0) <= 100, 'CACHE_METADATA_PAGINATION_REQUIRED')
        rows.extend(value['actions_caches'])
    pattern = re.compile(r'g11-state-v4-(?:critical|published)-' + DAY + r'-[0-9]+(?:-[0-9]+-checkpoint)?')
    rows = [row for row in rows if type(row) is dict
            and row.get('ref') == 'refs/heads/main'
            and pattern.fullmatch(str(row.get('key', '')))
            and type(row.get('id')) is int and type(row.get('created_at')) is str]
    require(bool(rows), 'NO_CURRENT_FULL_CHECKPOINT')
    row = max(rows, key=lambda item: (item['created_at'], item['id']))
    require(type(row.get('size_in_bytes')) is int and row['size_in_bytes'] > 0,
            'CACHE_METADATA_SIZE')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write('cache_key=' + row['key'] + '\n')
    return {'status': 'SELECTED', 'candidate_count': len(rows),
            'cache_key_sha256': sha(row['key'].encode()),
            'encrypted_bytes': row['size_in_bytes'], 'cache_storage': storage_evidence()}


def extract_checkpoint(cipher, destination, original_day=None):
    require(destination.parent.resolve() == Path(os.environ['RUNNER_TEMP']).resolve(),
            'DISPOSABLE_DESTINATION_REQUIRED')
    require(not destination.exists() and not destination.is_symlink(), 'DESTINATION_EXISTS')
    selected = os.environ.get('SELECTED_CHECKPOINT', '')
    require(selected and selected == os.environ.get('RESTORED_CHECKPOINT'), 'EXACT_RESTORE_REQUIRED')
    if original_day is not None:
        require(original_day in ORIGINAL_RECOVERY and selected in ORIGINAL_RECOVERY[original_day][2], 'ORIGINAL_CACHE_KEY_BINDING')
    else:
        require(re.fullmatch(r'g11-state-v4-(?:critical|published)-' + DAY
                            + r'-[0-9]+(?:-[0-9]+-checkpoint)?', selected), 'CACHE_KEY_BINDING')
    require(os.environ.get('RESTORED_HIT') == 'true', 'EXACT_CACHE_HIT_REQUIRED')
    require(cipher.is_file() and not cipher.is_symlink(), 'CIPHER_REQUIRED')
    helper = Path(__file__).resolve().with_name('extract_runtime_state_v1.py')
    require(file_sha(helper) == EXTRACT_HELPER_SHA, 'EXTRACTION_HELPER_HASH')
    token = os.environ.get('G11_PRIVATE_REPO_TOKEN', '')
    require(0 < len(token) <= 4096 and '\n' not in token and '\r' not in token, 'DECRYPT_SECRET_REQUIRED')
    archive = destination.with_name('g11-recovery-inventory.authenticated.tgz')
    require(not archive.exists() and not archive.is_symlink(), 'ARCHIVE_EXISTS')
    encrypted_sha = file_sha(cipher)
    try:
        result = subprocess.run(['gpg', '--batch', '--yes', '--pinentry-mode', 'loopback',
                                 '--passphrase-fd', '0', '--decrypt', '--output', str(archive), str(cipher)],
                                input=(token + '\n').encode(), stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, timeout=900, check=False)
        require(result.returncode == 0, 'CHECKPOINT_AUTHENTICATION_FAILED')
        from extract_runtime_state_v1 import extract_complete_state
        with redirect_stdout(io.StringIO()):
            extract_complete_state(archive, destination, decompressor='python')
    finally:
        archive.unlink(missing_ok=True)
    return {'status': 'EXTRACTED', 'cache_key_sha256': sha(selected.encode()),
            'encrypted_file_sha256': encrypted_sha, 'encrypted_bytes': cipher.stat().st_size}


def enforce_checker_isolation(root, host_netns):
    require(root.is_dir() and not root.is_symlink(), 'STATE_DIRECTORY_REQUIRED')
    require(bool(os.statvfs(root).f_flag & os.ST_RDONLY), 'STATE_OS_READ_ONLY_REQUIRED')
    require(host_netns.isdigit() and os.stat('/proc/self/ns/net').st_ino != int(host_netns),
            'SEPARATE_NETWORK_NAMESPACE_REQUIRED')
    interfaces = {line.split(':', 1)[0].strip()
                  for line in Path('/proc/self/net/dev').read_text().splitlines()[2:] if ':' in line}
    require(interfaces <= {'lo'}, 'EMPTY_NETWORK_NAMESPACE_REQUIRED')
    sys.dont_write_bytecode = True
    def guard(event, args):
        if event.startswith('socket.') or event in ('subprocess.Popen', 'os.system'):
            raise RuntimeError('CHECKER_NETWORK_OR_PROCESS_PROHIBITED')
        if event == 'open':
            _, mode, flags = args
            if ((isinstance(mode, str) and any(c in mode for c in 'wax+'))
                    or isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
                raise RuntimeError('CHECKER_WRITE_PROHIBITED')
        if event in {'os.remove', 'os.rmdir', 'os.mkdir', 'os.rename', 'os.link', 'os.symlink',
                     'os.chmod', 'os.chown', 'os.utime', 'os.truncate'}:
            raise RuntimeError('CHECKER_WRITE_PROHIBITED')
    sys.addaudithook(guard)


def inspect_state(root):
    root = root.resolve(strict=True)
    paths = {}
    for path in root.rglob('*'):
        mode = path.lstat().st_mode
        require(not stat.S_ISLNK(mode), 'STATE_SYMLINK_PROHIBITED')
        if stat.S_ISDIR(mode):
            continue
        require(stat.S_ISREG(mode), 'STATE_SPECIAL_FILE_PROHIBITED')
        paths[path.relative_to(root).as_posix()] = path
        require(len(paths) <= MAX_FILES, 'STATE_FILE_LIMIT')
    cache = {}
    def digest(name):
        if name not in cache:
            cache[name] = file_sha(paths[name])
        return cache[name]
    def row(name):
        return {'path_sha256': sha(name.encode()), 'file_sha256': digest(name),
                'bytes': paths[name].stat().st_size}
    snapshots = []
    for name in sorted(paths):
        if name.startswith('store/snapshots/') and name.endswith('.json'):
            item = row(name)
            try:
                value = read_json(paths[name])
                payload = value.get('PAYLOAD')
                item['payload_sha256'] = sha(canonical(payload))
                item['source_receipt_count'] = len(value.get('SOURCE_RECEIPTS', [])) if type(value.get('SOURCE_RECEIPTS')) is list else 0
                item['snapshot_stage_sha256'] = sha(str(value.get('STAGE', '')).encode())
            except (RuntimeError, ValueError, OSError) as exc:
                item['error_code'] = safe_error(exc)
            snapshots.append(item)
    models = [row(name) for name in sorted(paths)
              if any(name.startswith('store/' + prefix + '/') for prefix in MODEL_ROOTS)]
    require(len(snapshots) + len(models) <= MAX_ROWS, 'REPORT_ROW_LIMIT')
    days = {}
    for day in DAYS:
        prefix = 'daily-runtime/' + day + '/'
        names = sorted(name for name in paths if name.startswith(prefix))
        info = {'day_file_count': len(names), 'authority_present': 0,
                'daily_files': [row(name) for name in names]}
        authority_name = prefix + 'authority.json'
        if authority_name in paths:
            authority = read_json(paths[authority_name])
            unsigned = dict(authority)
            supplied = unsigned.pop('authority_sha256', None)
            valid = (safe_hash(supplied) is not None and supplied == sha(canonical(unsigned))
                     and authority.get('operational_date_jst') == day and authority.get('immutable') is True)
            info.update({'authority_present': 1, 'authority_self_hash_valid': int(valid),
                         'authority_sha256': safe_hash(supplied),
                         'runtime_source_sha': safe_hash(authority.get('runtime_source_sha'), source=True),
                         'model_set_sha256': safe_hash(authority.get('model_set_sha256')),
                         'source_archive_sha256': safe_hash(authority.get('source_archive_sha256'))})
            archive = authority.get('source_archive')
            if type(archive) is str and re.fullmatch(r'source-[0-9a-f]{64}\.tgz', archive):
                archive_name = prefix + archive
                info['source_archive_present'] = int(archive_name in paths)
                info['source_archive_bytes_match'] = int(archive_name in paths and digest(archive_name) == authority.get('source_archive_sha256'))
            expected_models = authority.get('model_files', {})
            require(type(expected_models) is dict, 'MODEL_MAP_SHAPE')
            info['model_binding'] = compare_bindings(expected_models, paths, digest, 'store/')
            phases = {}
            for phase in ('MORNING', 'PREDEADLINE'):
                bound, errors, record_count = {}, [], 0
                for name in names:
                    if not name.startswith(prefix + 'bindings/' + phase + '/') or not name.endswith('.json'):
                        continue
                    record_count += 1
                    binding = read_json(paths[name])
                    body = dict(binding); binding_sha = body.pop('binding_sha256', None)
                    if not (safe_hash(binding_sha) and binding_sha == sha(canonical(body))
                            and Path(name).stem == binding_sha
                            and binding.get('authority_sha256') == supplied
                            and binding.get('runtime_source_sha') == authority.get('runtime_source_sha')
                            and binding.get('operational_date_jst') == day
                            and binding.get('phase') == phase):
                        errors.append('PHASE_BINDING_INVALID'); continue
                    locks = binding.get('lock_files')
                    require(type(locks) is dict, 'LOCK_MAP_SHAPE')
                    for key, value in locks.items():
                        require(key not in bound or bound[key] == value, 'LOCK_BINDING_CONFLICT')
                        bound[key] = value
                phases[phase] = {'binding_record_count': record_count, 'error_codes': errors,
                                 **compare_bindings(bound, paths, digest, 'store/')}
            info['phases'] = phases
        days[day] = info
    previous = {}
    for source_id in ('result-k-20261127', 'result-k-20261128'):
        response_prefix = 'store/responses/' + source_id + '/'
        members = sorted(name for name in paths if name.startswith(response_prefix))
        previous[source_id] = {'file_count': len(members), 'files': [row(name) for name in members]}
        receipt_name = response_prefix + 'receipt.json'
        if receipt_name in paths:
            try:
                receipt = read_json(paths[receipt_name])
                expected = safe_hash(receipt.get('SHA256'))
                raw_members = [name for name in members if name != receipt_name]
                previous[source_id].update({'response_sha256': expected,
                    'source_id_matches': int(receipt.get('SOURCE_ID') == source_id),
                    'matching_response_byte_count': sum(int(digest(name) == expected) for name in raw_members)})
            except (RuntimeError, ValueError, OSError) as exc:
                previous[source_id]['error_code'] = safe_error(exc)
    candidates = []
    for name in sorted(paths):
        if name.startswith('daily-runtime/') or any(name.startswith('store/' + prefix + '/') for prefix in MODEL_ROOTS):
            continue
        if (name.endswith(('.tgz', '.tar', '.tar.gz', '.gpg', '.zip'))
                or any(piece in name.lower() for piece in ('backup', 'baseline', 'preserv', 'recovery-attempt', 'previous-night'))
                or name.startswith('store/archives/')):
            item = row(name)
            if name.endswith(('.tgz', '.tar', '.tar.gz')) and paths[name].stat().st_size <= MAX_JSON_BYTES:
                try:
                    with tarfile.open(paths[name], 'r:*') as archive:
                        count = old_day = morning = night = 0
                        for member in archive:
                            count += 1; require(count <= MAX_FILES, 'ARCHIVE_MEMBER_LIMIT')
                            old_day += int('2026-11-27' in member.name or '20261127' in member.name)
                            morning += int('/snapshots/morning/' in '/' + member.name)
                            night += int('/snapshots/night/' in '/' + member.name)
                    item.update({'archive_member_count': count, 'previous_day_member_count': old_day,
                                 'morning_snapshot_member_count': morning, 'night_snapshot_member_count': night})
                except (tarfile.TarError, OSError, EOFError):
                    item['error_code'] = 'ARCHIVE_UNREADABLE'
            candidates.append(item)
    require(len(candidates) <= MAX_ROWS, 'BACKUP_ROW_LIMIT')
    dated = [row(name) for name in sorted(paths)
             if any(day in name or day.replace('-', '') in name for day in DAYS)
             and not name.startswith(('daily-runtime/', 'store/snapshots/'))
             and not any(name.startswith('store/' + prefix + '/') for prefix in MODEL_ROOTS)]
    require(len(dated) <= MAX_ROWS, 'DATED_ROW_LIMIT')
    day_directories = sorted(p.name for p in (root / 'daily-runtime').iterdir()
                             if p.is_dir()) if (root / 'daily-runtime').is_dir() else []
    morning_input = 'store/snapshots/morning/official-day-input.json'
    night_input = 'store/snapshots/night/official-day-results.json'
    inventory = {name: {'bytes': path.stat().st_size, 'mtime_ns': path.stat().st_mtime_ns}
                 for name, path in paths.items()}
    return {'status': 'INVENTORIED', 'state_file_count': len(paths),
            'state_metadata_sha256': sha(canonical(inventory)),
            'snapshot_count': len(snapshots), 'snapshot_files': snapshots,
            'model_file_count': len(models), 'model_files': models, 'days': days,
            'day_directory_count': len(day_directories),
            'day_directory_name_sha256': [sha(name.encode()) for name in day_directories],
            'target_dated_file_count': len(dated), 'target_dated_files': dated,
            'previous_result_sources': previous, 'backup_candidate_count': len(candidates),
            'backup_candidates': candidates,
            'morning_input_file_sha256': digest(morning_input) if morning_input in paths else None,
            'night_input_file_sha256': digest(night_input) if night_input in paths else None,
            'snapshot_path_lookup_contract_sha256': sha(b'path_sha256=SHA256(UTF8(relative_posix_path_from_state_root))')}


def compare_bindings(expected, paths, digest, prefix):
    output = []; missing = mismatch = matched = 0
    for name, wanted in sorted(expected.items()):
        relative = PurePosixPath(name)
        require(not relative.is_absolute() and '..' not in relative.parts
                and str(relative) == name and safe_hash(wanted), 'BOUND_PATH_OR_HASH_INVALID')
        full = prefix + name
        found = digest(full) if full in paths else None
        missing += int(found is None); mismatch += int(found is not None and found != wanted)
        matched += int(found == wanted)
        output.append({'path_sha256': sha(full.encode()), 'expected_sha256': wanted,
                       'actual_sha256': found})
        require(len(output) <= MAX_ROWS, 'BINDING_ROW_LIMIT')
    return {'bound_file_count': len(output), 'matched_file_count': matched,
            'missing_file_count': missing, 'mismatched_file_count': mismatch, 'files': output}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    select = sub.add_parser('select')
    select.add_argument('--original-day', choices=DAYS)
    extract = sub.add_parser('extract')
    extract.add_argument('--cipher', type=Path, required=True)
    extract.add_argument('--destination', type=Path, required=True)
    extract.add_argument('--original-day', choices=DAYS)
    check = sub.add_parser('check')
    check.add_argument('--state', type=Path, required=True)
    check.add_argument('--host-netns', required=True)
    check.add_argument('--original-day', choices=DAYS)
    args = parser.parse_args()
    try:
        if args.command == 'select':
            report = select_original(args.original_day) if args.original_day else select_cache()
        elif args.command == 'extract':
            report = extract_checkpoint(args.cipher, args.destination, args.original_day)
        else:
            enforce_checker_isolation(args.state, args.host_netns)
            report = inspect_state(args.state)
            if args.original_day:
                report = verify_original_inventory(report, args.original_day)
        print(json.dumps(report, ensure_ascii=True, sort_keys=True, separators=(',', ':')))
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'BLOCKED', 'error_code': safe_error(exc)}, sort_keys=True))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
