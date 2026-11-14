"""Keep the complete gzip/AES256 checkpoint, without recompressing gzip bytes."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import time


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
    args = parser.parse_args()
    if args.prepare_gzip:
        prepare_gzip_acceleration()
        return
    if args.state_root is None or args.output is None:
        parser.error('--state-root and --output are required for encryption')
    encrypt(args.state_root, args.output)


if __name__ == '__main__':
    main()
