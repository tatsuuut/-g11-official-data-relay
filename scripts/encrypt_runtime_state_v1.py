"""Keep the complete gzip/AES256 checkpoint, without recompressing gzip bytes."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import time


def compression_command(workers=None, *, codec='auto'):
    """Use at most four available CPUs; retain the standard gzip wire format."""
    if codec not in ('auto', 'gzip', 'pigz'):
        raise ValueError('CHECKPOINT_COMPRESSION_CODEC')
    if workers is None:
        try:
            available = len(os.sched_getaffinity(0))
        except (AttributeError, OSError):
            available = os.cpu_count() or 1
        workers = min(4, max(1, available))
    if type(workers) is not int or not 1 <= workers <= 4:
        raise ValueError('CHECKPOINT_COMPRESSION_WORKERS')
    parallel = bool(shutil.which('pigz'))
    if codec == 'pigz' and not parallel:
        raise ValueError('CHECKPOINT_PARALLEL_GZIP_UNAVAILABLE')
    return (f'pigz -1 -p {workers}' if parallel and codec != 'gzip' else 'gzip -1')


def encrypt(state, output, *, passphrase_fd=3, workers=None, codec='auto'):
    state, output = Path(state).resolve(), Path(output).absolute()
    if not state.is_dir() or output.is_relative_to(state):
        raise ValueError('CHECKPOINT_ARCHIVE_PATH')
    os.fstat(passphrase_fd)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + '.tmp')
    if temporary.exists() or temporary.is_symlink():
        raise ValueError('CHECKPOINT_TEMP_EXISTS')
    # Both produce the same gzip stream format consumed by every retained
    # reader. The public runner may have four CPUs; do not leave half idle.
    compressor = compression_command(workers, codec=codec)
    started = time.monotonic()
    tar = subprocess.Popen(['tar', '-I', compressor, '-cf', '-', '-C', str(state), '.'],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        encrypted = subprocess.run([
            'gpg', '--batch', '--yes', '--pinentry-mode', 'loopback',
            '--passphrase-fd', str(passphrase_fd), '--symmetric',
            '--cipher-algo', 'AES256', '--compress-algo', 'none',
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    encrypt(args.state_root, args.output)


if __name__ == '__main__':
    main()
