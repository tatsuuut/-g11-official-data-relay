"""Read-only comparison of complete encrypted checkpoint compression.

Run only on a disposable, authenticated saved-state copy. Output aggregate
timings and byte-equality results, never state paths, contents or passphrases.
"""
import argparse
import base64
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
    for mode, codec, reader in [('legacy','pigz','python'), ('legacy','igzip','igzip'), ('ocb','igzip','igzip')]:
        with tempfile.TemporaryDirectory(dir=scratch, prefix='checkpoint-benchmark-') as tmp:
            root = Path(tmp)
            encrypted, archive, restored = root/'state.gpg', root/'state.tgz', root/'restored'
            with tempfile.TemporaryFile() as passphrase:
                passphrase.write(secret); passphrase.seek(0)
                started = time.monotonic()
                encrypt(state, encrypted, passphrase_fd=passphrase.fileno(), mode=mode, codec=codec)
                encryption_seconds = time.monotonic() - started
                passphrase.seek(0)
                subprocess.run(['gpg', '--batch', '--yes', '--pinentry-mode', 'loopback',
                    '--passphrase-fd', str(passphrase.fileno()), '--decrypt', '--output', str(archive),
                    str(encrypted)], pass_fds=(passphrase.fileno(),),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            started = time.monotonic()
            extract_complete_state(archive, restored, decompressor=reader)
            restore_seconds = time.monotonic() - started
            if inventory(restored) != before:
                raise ValueError('BENCHMARK_COMPLETE_BYTES_CHANGED')
            result = {'encryption_mode': mode, 'codec': codec, 'reader': reader, 'encrypt_seconds': round(encryption_seconds, 3),
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


SYNTHETIC_SECRET = b'checkpoint-compatibility-test-only\n'
SYNTHETIC_FILES = {'.hidden': b'public synthetic fixture',
                   'nested/lock.txt': b'no private state or prediction data\n'}


def compatibility_fixture(scratch):
    """Export only known public synthetic bytes, never the saved state."""
    with tempfile.TemporaryDirectory(dir=scratch) as tmp:
        root = Path(tmp)
        state = root/'synthetic'; state.mkdir()
        for name, value in SYNTHETIC_FILES.items():
            path = state/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(value)
        with tempfile.TemporaryFile() as secret:
            secret.write(SYNTHETIC_SECRET); secret.seek(0)
            encrypt(state, root/'synthetic.gpg', passphrase_fd=secret.fileno(), mode='ocb')
        encoded = base64.b64encode((root/'synthetic.gpg').read_bytes()).decode('ascii')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write('ocb_fixture=' + encoded + '\n')


def verify_compatibility_fixture(encoded):
    with tempfile.TemporaryDirectory(prefix='g11-ocb-') as tmp:
        root = Path(tmp)
        raw = base64.b64decode(encoded, validate=True)
        if not 0 < len(raw) < 8192:
            raise ValueError('COMPATIBILITY_SYNTHETIC_FIXTURE_SIZE')
        for label, data, password in [('valid', raw, SYNTHETIC_SECRET),
                ('truncated', raw[:-10], SYNTHETIC_SECRET),
                ('tampered', raw[:len(raw)//2] + bytes([raw[len(raw)//2]^1]) + raw[len(raw)//2+1:], SYNTHETIC_SECRET),
                ('wrong-passphrase', raw, b'incorrect-synthetic-passphrase\n')]:
            encrypted, archive = root/(label+'.gpg'), root/(label+'.tgz')
            encrypted.write_bytes(data)
            result = subprocess.run(['gpg', '--batch', '--yes', '--no-symkey-cache',
                '--pinentry-mode', 'loopback', '--passphrase-fd', '0', '--decrypt',
                '--output', str(archive), str(encrypted)], input=password,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if label == 'valid':
                if result.returncode:
                    raise ValueError('COMPATIBILITY_OCB_READER_REJECTED')
                destination = root/'restored'
                extract_complete_state(archive, destination)
                if {p.relative_to(destination).as_posix():p.read_bytes()
                        for p in destination.rglob('*') if p.is_file()} != SYNTHETIC_FILES:
                    raise ValueError('COMPATIBILITY_SYNTHETIC_BYTES_CHANGED')
            elif result.returncode == 0:
                raise ValueError('COMPATIBILITY_DAMAGED_ARCHIVE_ACCEPTED')
            archive.unlink(missing_ok=True)
        print('G11_OCB_READER_COMPATIBILITY=PASS VALID_AND_CORRUPT_CASES=4')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-root', type=Path)
    parser.add_argument('--scratch', type=Path)
    parser.add_argument('--verify-fixture', action='store_true')
    args = parser.parse_args()
    if args.verify_fixture:
        return verify_compatibility_fixture(os.environ['G11_OCB_SYNTHETIC_FIXTURE'])
    secret = os.read(3, 65536)
    if not secret:
        raise ValueError('BENCHMARK_PASSPHRASE_REQUIRED')
    benchmark(args.state_root, args.scratch, secret)
    compatibility_fixture(args.scratch)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        code = str(exc)
        print('G11_SAVED_CHECKPOINT_BENCHMARK=FAILED TYPE=' + type(exc).__name__ +
              (' CODE=' + code if code.isupper() and len(code) < 100 else ''), flush=True)
        raise SystemExit(1) from None
