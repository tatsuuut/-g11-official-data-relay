"""Keep the complete gzip/AES256 checkpoint, without recompressing gzip bytes."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time

DELTA_SCHEMA = 'G11_COMPLETE_STATE_DELTA_V1'
DELTA_LIMIT = 128 * 1024 * 1024
INDEX_LIMIT = 64 * 1024 * 1024


def require(ok, reason):
    if not ok:
        raise ValueError('CHECKPOINT_DELTA_' + reason)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def snapshot_state(root):
    root = Path(root)
    require(root.is_dir() and not root.is_symlink(), 'ROOT')
    files, directories = {}, {}
    for path in sorted(root.rglob('*')):
        info = path.lstat()
        name = path.relative_to(root).as_posix()
        require(stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode), 'FILE_TYPE')
        if stat.S_ISREG(info.st_mode):
            files[name] = {'sha256': file_sha(path), 'size': info.st_size, 'mode': stat.S_IMODE(info.st_mode)}
        else:
            directories[name] = stat.S_IMODE(info.st_mode)
    return {'files': files, 'directories': directories}


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    require(not temporary.exists() and not temporary.is_symlink() and not path.is_symlink(), 'TEMP_EXISTS')
    try:
        with temporary.open('xb') as output:
            os.chmod(temporary, 0o600)
            output.write(canonical(value)); output.flush(); os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def save_base_inventory(root, path):
    # Called only after authenticated complete extraction and before runtime work.
    atomic_json(path, {'schema': DELTA_SCHEMA, 'inventory': snapshot_state(root)})


def checkpoint_day(key):
    match = re.fullmatch(r'g11-state-v4-(?:critical|published)-(20\d{2}-\d{2}-\d{2})-\d+(?:-\d+-checkpoint)?', key)
    require(match is not None, 'BASE_KEY')
    return match.group(1)


def validate_inventory(value):
    require(isinstance(value, dict) and set(value) == {'files', 'directories'}, 'INVENTORY')
    files, dirs = value['files'], value['directories']
    require(isinstance(files, dict) and isinstance(dirs, dict) and 0 < len(files) + len(dirs) <= 250000, 'INVENTORY_SIZE')
    require(not set(files) & set(dirs), 'TYPE_CONFLICT')
    for name in files.keys() | dirs.keys():
        path = PurePosixPath(name)
        require(isinstance(name, str) and path.parts and str(path) == name and not path.is_absolute()
                and '..' not in path.parts and '\\' not in name, 'PATH')
        require(all(str(parent) in dirs for parent in path.parents if str(parent) != '.'), 'PARENT')
    for name, item in files.items():
        require(isinstance(item, dict) and set(item) == {'sha256', 'size', 'mode'}
                and isinstance(item['sha256'], str) and re.fullmatch('[a-f0-9]{64}', item['sha256'])
                and type(item['size']) is int and item['size'] >= 0, 'FILE_METADATA')
    for mode in [*(i['mode'] for i in files.values()), *dirs.values()]:
        require(type(mode) is int and 0 <= mode <= 0o777, 'MODE')
    return value


def delta_manifest(root, base, base_key, base_cipher_sha):
    root = Path(root)
    state = json.loads((root / 'relay-state.json').read_bytes())
    day, critical = state.get('OPERATIONAL_DATE_JST'), state.get('CRITICAL', {})
    run_id = critical.get('LAST_CRITICAL_RUN_ID')
    require(isinstance(day, str) and checkpoint_day(base_key) == day and isinstance(run_id, str)
            and re.fullmatch('[0-9]+', run_id) and state.get('MORNING_COMPLETE') is True
            and critical.get('SCHEMA') == 'G11_PREDEADLINE_CRITICAL_CHECKPOINT_V1'
            and critical.get('CRITICAL_CHECKPOINT_STATUS') == 'PASS'
            and critical.get('OPERATIONAL_DATE') == day and critical.get('POST_DEADLINE_GENERATION') == 0
            and critical.get('LAST_TIMELY_SOURCE_COUNT', 0) > 0, 'TARGET_BINDING')
    authority = json.loads((root / 'daily-runtime' / day / 'authority.json').read_bytes())
    require(authority.get('immutable') is True and authority.get('operational_date_jst') == day
            and authority.get('authority_sha256') == critical.get('AUTHORITY_SHA256')
            and authority.get('runtime_source_sha') == critical.get('RUNTIME_SOURCE_SHA'), 'AUTHORITY')
    target = snapshot_state(root)
    validate_inventory(base); validate_inventory(target)
    authority_name = 'daily-runtime/' + day + '/authority.json'
    require(base['files'].get(authority_name) == target['files'].get(authority_name), 'BASE_AUTHORITY_CHANGED')
    changed = sorted(name for name, item in target['files'].items() if base['files'].get(name) != item)
    return {'schema': DELTA_SCHEMA, 'day': day, 'run_id': run_id,
            'checkpoint_key': f'g11-state-v4-critical-{day}-{run_id}',
            'delta_key': f'g11-state-v4-delta-{day}-{run_id}', 'base_key': base_key,
            'base_cipher_sha256': base_cipher_sha, 'base': base, 'target': target, 'changed': changed}


def write_delta(root, manifest, destination):
    # The archive contains only replacements, but its manifest covers ALL files
    # and empty directories. No history or model is omitted from reconstruction.
    raw = canonical(manifest)
    require(len(raw) <= INDEX_LIMIT, 'MANIFEST_LIMIT')
    changed_size = sum(manifest['target']['files'][name]['size'] for name in manifest['changed'])
    require(changed_size + len(raw) <= DELTA_LIMIT, 'PAYLOAD_LIMIT')
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, 'w:gz', compresslevel=1) as archive:
        info = tarfile.TarInfo('manifest.json'); info.size = len(raw); info.mode = 0o600
        archive.addfile(info, io.BytesIO(raw))
        for name in manifest['changed']:
            path = Path(root) / name
            require(file_sha(path) == manifest['target']['files'][name]['sha256'], 'SOURCE_ADVANCED')
            archive.add(path, arcname='changes/' + name, recursive=False)


def inspect_delta(archive_path, *, day, checkpoint_key):
    require(re.fullmatch(r'g11-state-v4-critical-' + re.escape(day) + r'-[0-9]+', checkpoint_key), 'TARGET_KEY')
    with tarfile.open(archive_path, 'r:gz') as archive:
        members = archive.getmembers()
        require(0 < len(members) <= 250001 and sum(m.size for m in members) <= DELTA_LIMIT, 'ARCHIVE_LIMIT')
        first = members[0]
        require(first.name == 'manifest.json' and first.isfile() and 0 < first.size <= INDEX_LIMIT, 'MANIFEST')
        manifest = json.load(archive.extractfile(first))
        require(manifest.get('schema') == DELTA_SCHEMA and manifest.get('day') == day
                and manifest.get('checkpoint_key') == checkpoint_key
                and checkpoint_key == f'g11-state-v4-critical-{day}-{manifest.get("run_id")}'
                and manifest.get('delta_key') == f'g11-state-v4-delta-{day}-{manifest.get("run_id")}'
                and checkpoint_day(manifest.get('base_key', '')) == day
                and re.fullmatch('[a-f0-9]{64}', manifest.get('base_cipher_sha256', '')), 'MANIFEST_BINDING')
        base, target = validate_inventory(manifest.get('base')), validate_inventory(manifest.get('target'))
        changed = sorted(name for name, item in target['files'].items() if base['files'].get(name) != item)
        require(manifest.get('changed') == changed, 'CHANGE_SET')
        require([m.name for m in members[1:]] == ['changes/' + n for n in changed], 'MEMBER_SET')
        for member, name in zip(members[1:], changed):
            require(member.isfile() and member.size == target['files'][name]['size'], 'MEMBER_TYPE_OR_SIZE')
        return manifest


def apply_delta(archive_path, root, *, day, checkpoint_key, base_cipher):
    root = Path(root).absolute()
    manifest = inspect_delta(archive_path, day=day, checkpoint_key=checkpoint_key)
    require(file_sha(base_cipher) == manifest['base_cipher_sha256'], 'BASE_CIPHER')
    require(snapshot_state(root) == manifest['base'], 'BASE_INVENTORY')
    stage = Path(tempfile.mkdtemp(prefix='.g11-delta-verified-', dir=root.parent))
    backup = stage.with_name(stage.name + '.base')
    started = time.monotonic()
    committed = False
    try:
        shutil.copytree(root, stage, dirs_exist_ok=True, copy_function=os.link)
        target = manifest['target']
        for name in manifest['base']['files']:
            if name not in target['files'] or name in manifest['changed']:
                (stage / name).unlink()
        for name in sorted(manifest['base']['directories'], key=lambda n: (-len(PurePosixPath(n).parts), n)):
            if name not in target['directories']:
                (stage / name).rmdir()
        for name, mode in sorted(target['directories'].items(), key=lambda item: (len(PurePosixPath(item[0]).parts), item[0])):
            path = stage / name; path.mkdir(exist_ok=True); path.chmod(mode)
        with tarfile.open(archive_path, 'r:gz') as archive:
            for name in manifest['changed']:
                path = stage / name
                with archive.extractfile('changes/' + name) as source, path.open('xb') as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                path.chmod(target['files'][name]['mode'])
        require(snapshot_state(stage) == target, 'TARGET_INVENTORY')
        os.rename(root, backup)
        try:
            os.replace(stage, root)
            committed = True
        except BaseException:
            os.rename(backup, root)
            raise
        print('G11_COMPLETE_DELTA_RECONSTRUCTION=PASS FILES=' + str(len(target['files']))
              + ' CHANGED=' + str(len(manifest['changed'])) + ' SECONDS=' + str(round(time.monotonic() - started, 3)), flush=True)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        if committed:
            shutil.rmtree(backup, ignore_errors=True)


@contextmanager
def secret_pipe(secret):
    read_fd, write_fd = os.pipe()
    try:
        require(0 < len(secret) <= 4096 and secret.endswith(b'\n'), 'SECRET_INPUT')
        os.write(write_fd, secret); os.close(write_fd); write_fd = -1
        yield read_fd
    finally:
        os.close(read_fd)
        if write_fd != -1:
            os.close(write_fd)


def encrypt_gzip_file(source, output, passphrase_fd):
    output = Path(output)
    temporary = output.with_name(output.name + '.tmp')
    require(not temporary.exists() and not output.is_symlink(), 'CIPHER_TEMP')
    try:
        with Path(source).open('rb') as archive:
            result = subprocess.run(['gpg', '--batch', '--yes', '--pinentry-mode', 'loopback',
                '--passphrase-fd', str(passphrase_fd), '--symmetric', '--cipher-algo', 'AES256',
                '--compress-algo', 'none', *authenticated_encryption_options(), '--output', str(temporary)],
                stdin=archive, pass_fds=(passphrase_fd,), capture_output=True, check=False)
        require(result.returncode == 0 and temporary.is_file() and temporary.stat().st_size > 0, 'ENCRYPTION')
        temporary.chmod(0o600)
        with temporary.open('rb') as saved:
            os.fsync(saved.fileno())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_compact_checkpoint(root, base_cipher, base_key, *, secret):
    folder = Path('.relay-output')
    encrypted, ready = folder / 'early-delta.tgz.gpg', folder / 'early-delta-ready.json'
    encrypted.unlink(missing_ok=True); ready.unlink(missing_ok=True)
    try:
        saved = json.loads((folder / 'base-inventory.json').read_bytes())
        require(saved.get('schema') == DELTA_SCHEMA, 'BASE_INDEX_SCHEMA')
        require(Path(base_cipher).is_file(), 'BASE_CIPHER_MISSING')
        manifest = delta_manifest(root, saved['inventory'], base_key, file_sha(base_cipher))
        require(manifest['run_id'] == os.environ.get('GITHUB_RUN_ID'), 'CURRENT_RUN')
        require(int(base_key.split(manifest['day'] + '-')[1].split('-')[0]) < int(manifest['run_id']), 'BASE_RUN')
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix='g11-complete-delta-') as tmp:
            archive = Path(tmp) / 'delta.tgz'
            write_delta(root, manifest, archive)
            require(archive.stat().st_size < min(DELTA_LIMIT, Path(base_cipher).stat().st_size // 4), 'NOT_SMALLER')
            with secret_pipe(secret) as fd:
                encrypt_gzip_file(archive, encrypted, fd)
        atomic_json(ready, {k: manifest[k] for k in ('day', 'run_id', 'checkpoint_key', 'delta_key', 'base_key')} |
                    {'cipher_sha256': file_sha(encrypted)})
        print('G11_COMPACT_CHECKPOINT=READY BYTES=' + str(encrypted.stat().st_size)
              + ' CHANGED=' + str(len(manifest['changed'])) + ' SECONDS=' + str(round(time.monotonic() - started, 3)), flush=True)
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, subprocess.SubprocessError) as error:
        encrypted.unlink(missing_ok=True); ready.unlink(missing_ok=True)
        # Canonical full encryption/publication still runs; no compact dispatch.
        print('G11_COMPACT_CHECKPOINT=FULL_FALLBACK TYPE=' + type(error).__name__, flush=True)


def compact_ready(checkpoint_key):
    folder = Path('.relay-output')
    try:
        value = json.loads((folder / 'early-delta-ready.json').read_bytes())
        require(value['checkpoint_key'] == checkpoint_key
                and checkpoint_key == f'g11-state-v4-critical-{value["day"]}-{value["run_id"]}'
                and value['delta_key'] == f'g11-state-v4-delta-{value["day"]}-{value["run_id"]}'
                and value['run_id'] == os.environ['GITHUB_RUN_ID']
                and checkpoint_day(value['base_key']) == value['day']
                and file_sha(folder / 'early-delta.tgz.gpg') == value['cipher_sha256'], 'READY_BINDING')
        return value
    except (OSError, ValueError, KeyError, TypeError):
        return None


def prepare_gzip_acceleration():
    """Optional signed OS package only; a slow/unavailable mirror is nonfatal."""
    if not shutil.which('igzip'):
        try:
            subprocess.run(['sudo', '-n', 'env', 'DEBIAN_FRONTEND=noninteractive',
                'apt-get', 'install', '-y', '--no-install-recommends', 'isal'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
    available = bool(shutil.which('igzip'))
    print('G11_GZIP_ACCELERATOR=' + ('ISAL' if available else 'EXISTING_GZIP_FALLBACK'), flush=True)
    return available


def compression_command(workers=None, *, codec='auto', accelerated=False):
    """Use available CPUs with OCB; retain the legacy two-worker writer."""
    if codec not in ('auto', 'gzip', 'pigz', 'igzip', 'igzip2'):
        raise ValueError('CHECKPOINT_COMPRESSION_CODEC')
    if workers is None:
        if accelerated:
            try:
                available = len(os.sched_getaffinity(0))
            except (AttributeError, OSError):
                available = os.cpu_count() or 1
            workers = min(4, max(1, available))
        else:
            workers = 2
    if type(workers) is not int or not 1 <= workers <= 4:
        raise ValueError('CHECKPOINT_COMPRESSION_WORKERS')
    if codec in ('igzip', 'igzip2') or (codec == 'auto' and shutil.which('igzip')):
        if not shutil.which('igzip'):
            raise ValueError('CHECKPOINT_ISAL_GZIP_UNAVAILABLE')
        return 'igzip -2' if codec == 'igzip2' else 'igzip -1'
    parallel = bool(shutil.which('pigz'))
    if codec == 'pigz' and not parallel:
        raise ValueError('CHECKPOINT_PARALLEL_GZIP_UNAVAILABLE')
    return (f'pigz -1 -p {workers}' if parallel and codec != 'gzip' else 'gzip -1')


def authenticated_encryption_options(mode='auto'):
    """Prefer supported AES256-OCB; older writers retain authenticated CFB.

    Ordinary GPG decrypt readers support OCB from 2.2.21 onward. Both retained
    Ubuntu reader generations are verified by the checkpoint contract CI.
    Never fall back after an encryption failure; only absent capability permits
    the existing CFB+MDC writer.
    """
    if mode not in ('auto', 'legacy', 'ocb'):
        raise ValueError('CHECKPOINT_AUTHENTICATED_MODE')
    if mode == 'legacy':
        return []
    probe = subprocess.run(['gpg', '--dump-options'], capture_output=True, text=True, check=False)
    supported = set(probe.stdout.splitlines()) if probe.returncode == 0 else set()
    for option in ('--use-ocb-sym', '--force-ocb', '--force-aead'):
        if option in supported:
            return [option]
    if mode == 'ocb':
        raise ValueError('CHECKPOINT_OCB_UNAVAILABLE')
    return []


def encrypt(state, output, *, passphrase_fd=3, workers=None, codec='auto', mode='auto'):
    state, output = Path(state).resolve(), Path(output).absolute()
    if not state.is_dir() or output.is_relative_to(state):
        raise ValueError('CHECKPOINT_ARCHIVE_PATH')
    os.fstat(passphrase_fd)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + '.tmp')
    if temporary.exists() or temporary.is_symlink():
        raise ValueError('CHECKPOINT_TEMP_EXISTS')
    # Complete standard gzip tar, AES256 and authenticated GPG decryption are
    # retained. Do not trade source coverage or integrity for latency.
    encryption_options = authenticated_encryption_options(mode)
    compressor = compression_command(workers, codec=codec, accelerated=bool(encryption_options))
    started = time.monotonic()
    tar = subprocess.Popen(['tar', '-I', compressor, '-cf', '-', '-C', str(state), '.'],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        encrypted = subprocess.run([
            'gpg', '--batch', '--yes', '--pinentry-mode', 'loopback',
            '--passphrase-fd', str(passphrase_fd), '--symmetric',
            '--cipher-algo', 'AES256', '--compress-algo', 'none',
            *encryption_options,
            '--output', str(temporary),
        ], stdin=tar.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            pass_fds=(passphrase_fd,), check=False)
        tar.stdout.close()
        archive_code = tar.wait()
        if encrypted.returncode or archive_code or not temporary.is_file() or not temporary.stat().st_size:
            raise RuntimeError('CHECKPOINT_ENCRYPTION_FAILED')
        os.chmod(temporary, 0o600)
        with temporary.open('rb') as saved:
            os.fsync(saved.fileno())
        os.replace(temporary, output)
    finally:
        if tar.poll() is None:
            tar.terminate()
            tar.wait()
        temporary.unlink(missing_ok=True)
    print('G11_CHECKPOINT_ENCRYPTION_SECONDS=' + str(round(time.monotonic() - started, 3)), flush=True)
    print('G11_CHECKPOINT_COMPRESSOR=' + compressor, flush=True)
    print('G11_CHECKPOINT_AUTHENTICATED_MODE=' + ('AES256_OCB' if encryption_options else 'AES256_CFB_MDC'), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-root', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--prepare-gzip', action='store_true')
    parser.add_argument('--delta-base-key')
    parser.add_argument('--compact-ready')
    args = parser.parse_args()
    if args.prepare_gzip:
        prepare_gzip_acceleration()
        return
    if args.compact_ready:
        value = compact_ready(args.compact_ready)
        if value:
            with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                output.write('key=' + value['delta_key'] + '\n')
        return
    if args.state_root is None or args.output is None:
        parser.error('--state-root and --output are required for encryption')
    if args.delta_base_key:
        with os.fdopen(os.dup(3), 'rb') as source:
            secret = source.readline(4097)
        require(0 < len(secret) <= 4096 and secret.endswith(b'\n'), 'SECRET_INPUT')
        prepare_compact_checkpoint(args.state_root, args.output, args.delta_base_key, secret=secret)
        with secret_pipe(secret) as fd:
            encrypt(args.state_root, args.output, passphrase_fd=fd)
    else:
        encrypt(args.state_root, args.output)


if __name__ == '__main__':
    main()
