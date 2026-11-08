"""Cache only pinned public Python packages, never private source/state/models."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
import venv

PROFILES = {'parser': ['lxml==6.1.1'], 'runtime': ['lxml==6.1.1', 'catboost==1.2.8']}
SCHEMA = 'G11_PUBLIC_PYTHON_RUNTIME_V1'


def identity(root, profile):
    # EnvBuilder may copy its launcher. Bind to the base interpreter so calling
    # this helper after PATH activation does not invalidate an identical cache.
    executable = Path(getattr(sys, '_base_executable', sys.executable)).resolve()
    return {'schema': SCHEMA, 'root': str(Path(root).absolute()), 'profile': profile,
            'requirements': PROFILES[profile], 'python': sys.version,
            'executable': str(executable),
            'executable_sha256': hashlib.sha256(executable.read_bytes()).hexdigest(),
            'platform': platform.platform(), 'machine': platform.machine(),
            'image': os.environ.get('ImageOS', ''), 'image_version': os.environ.get('ImageVersion', '')}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def probe(root, profile):
    # The venv sees the same runner-provided packages as the old pip command.
    # Exact direct versions and the full resolved distribution list are checked.
    code = """import importlib.metadata as m,json
import lxml.etree
assert m.version('lxml') == '6.1.1'
"""
    if profile == 'runtime':
        code += "assert m.version('catboost') == '1.2.8'\nfrom catboost import CatBoostClassifier\n"
    code += "print(json.dumps(sorted((d.metadata['Name'].lower(),d.version) for d in m.distributions())))\n"
    result = subprocess.run([str(Path(root) / 'bin/python3'), '-c', code],
                            check=True, text=True, capture_output=True, timeout=45)
    return json.loads(result.stdout)


def prepare(root, profile):
    root = Path(root).absolute()
    expected = identity(root, profile)
    manifest = root / 'g11-public-runtime.json'
    started = time.monotonic()
    reused = False
    if root.is_symlink():
        raise ValueError('G11_PUBLIC_PYTHON_ROOT_SYMLINK')
    try:
        saved = json.loads(manifest.read_text())
        if saved['identity'] == expected and saved['distributions'] == probe(root, profile):
            reused = True
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        pass
    if not reused:
        if root.exists():
            shutil.rmtree(root)
        venv.EnvBuilder(with_pip=True, system_site_packages=True).create(root)
        subprocess.run([str(root / 'bin/python3'), '-m', 'pip', 'install',
                        '--disable-pip-version-check', '--no-input', *PROFILES[profile]],
                       check=True, timeout=180)
        distributions = probe(root, profile)
        manifest.write_text(json.dumps({'identity': expected, 'distributions': distributions},
                                       sort_keys=True) + '\n')
    print('G11_PUBLIC_PYTHON_ACTION=' + ('REUSED_VERIFIED' if reused else 'BUILT_VERIFIED'))
    print(f'G11_PUBLIC_PYTHON_SECONDS={time.monotonic() - started:.3f}')
    with open(os.environ['GITHUB_PATH'], 'a') as handle:
        handle.write(str(root / 'bin') + '\n')
    return reused


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['identity', 'prepare'])
    parser.add_argument('--profile', choices=PROFILES, default='runtime')
    args = parser.parse_args()
    # This path contains only venv-created files and public pip dependencies.
    root = Path(os.environ['RUNNER_TEMP']) / ('g11-public-python-v1-' + args.profile)
    if args.command == 'identity':
        with open(os.environ['GITHUB_OUTPUT'], 'a') as handle:
            handle.write('key=g11-public-python-v1-' + fingerprint(identity(root, args.profile)) + '\n')
            handle.write('path=' + str(root) + '\n')
    else:
        prepare(root, args.profile)


if __name__ == '__main__':
    main()
