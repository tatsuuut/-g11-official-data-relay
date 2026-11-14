"""Read-only comparison of complete encrypted checkpoint compression.

Run only on a disposable, authenticated saved-state copy. Output aggregate
timings and byte-equality results, never state paths, contents or passphrases.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from encrypt_runtime_state_v1 import encrypt
from extract_runtime_state_v1 import extract_complete_state


def inventory(root):
    rows = {}
    for path in root.rglob('*'):
        if path.is_symlink():
            raise ValueError('BENCHMARK_STATE_SYMLINK')
        name = path.relative_to(root).as_posix()
        if path.is_dir():
            rows[name] = None
        elif path.is_file():
            digest = hashlib.sha256()
            with path.open('rb') as source:
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(block)
            rows[name] = (path.stat().st_size, digest.hexdigest())
        else:
            raise ValueError('BENCHMARK_STATE_SPECIAL_FILE')
    return rows


def benchmark(state, scratch, secret):
    before = inventory(state)
    results = []
    for codec, workers in (('gzip', 1), ('pigz', 2), ('pigz', 4)):
        with tempfile.TemporaryDirectory(dir=scratch, prefix='checkpoint-benchmark-') as tmp:
            root = Path(tmp)
            encrypted, archive, restored = root/'state.gpg', root/'state.tgz', root/'restored'
            with tempfile.TemporaryFile() as passphrase:
                passphrase.write(secret); passphrase.seek(0)
                started = time.monotonic()
                encrypt(state, encrypted, passphrase_fd=passphrase.fileno(), workers=workers, codec=codec)
                encryption_seconds = time.monotonic() - started
                passphrase.seek(0)
                subprocess.run(['gpg', '--batch', '--yes', '--pinentry-mode', 'loopback',
                    '--passphrase-fd', str(passphrase.fileno()), '--decrypt', '--output', str(archive),
                    str(encrypted)], pass_fds=(passphrase.fileno(),),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            started = time.monotonic()
            extract_complete_state(archive, restored)
            restore_seconds = time.monotonic() - started
            if inventory(restored) != before:
                raise ValueError('BENCHMARK_COMPLETE_BYTES_CHANGED')
            result = {'codec': codec, 'workers': workers, 'encrypt_seconds': round(encryption_seconds, 3),
                'extract_seconds': round(restore_seconds, 3), 'encrypted_bytes': encrypted.stat().st_size,
                'all_files_and_directories_identical': True}
            results.append(result)
            print('G11_SAVED_CHECKPOINT_BENCHMARK=' + json.dumps(result, sort_keys=True), flush=True)
    if inventory(state) != before:
        raise ValueError('BENCHMARK_SOURCE_STATE_CHANGED')
    print('G11_SAVED_CHECKPOINT_BENCHMARK_SUMMARY=' + json.dumps({
        'cpu_count': os.cpu_count(), 'members': len(before), 'source_unchanged': True,
        'files_bytes': sum(row[0] for row in before.values() if row is not None), 'results': results},
        sort_keys=True), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-root', type=Path, required=True)
    parser.add_argument('--scratch', type=Path, required=True)
    args = parser.parse_args()
    if not shutil.which('pigz'):
        raise ValueError('BENCHMARK_PARALLEL_GZIP_REQUIRED')
    secret = os.read(3, 65536)
    if not secret:
        raise ValueError('BENCHMARK_PASSPHRASE_REQUIRED')
    benchmark(args.state_root, args.scratch, secret)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('G11_SAVED_CHECKPOINT_BENCHMARK=FAILED TYPE=' + type(exc).__name__, flush=True)
        raise SystemExit(1) from None
