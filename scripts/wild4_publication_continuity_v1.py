"""Carry authenticated saved work across publication failure; never infer a bet.

The one-day recovery copies only the earliest original Nov 8 LOCKs from the
reviewed immutable artifacts. No odds acquisition, prediction, settlement,
learning, promotion or public write is implemented here.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

REPO = 'tatsuuut/-g11-official-data-relay'
REPOSITORY_ID = 1367703708
CALLER = '.github/workflows/g11-morning-odds-footprint-4bet-shadow-v1.yml'
PERSISTED_STEP = 'Preserve encrypted shadow locks before publication'
DAY = '2026-11-08'
RECEIPT = 'recovery-provenance/publication-20261108/receipt.json'


def require(ok, reason):
    if not ok:
        raise ValueError('WILD4_PUBLICATION_CONTINUITY_' + reason)


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':'), allow_nan=False) + '\n').encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def sealed(value):
    value = {k: v for k, v in value.items() if k != 'CONTENT_SHA256'}
    return {**value, 'CONTENT_SHA256': digest(canonical(value))}


def read_sealed(path):
    value = json.loads(path.read_bytes())
    require(sealed(value) == value, 'CONTENT_HASH:' + path.name)
    return value


def trusted_run(run, *, conclusion):
    return (run.get('status') == 'completed' and run.get('conclusion') == conclusion
            and run.get('path') == CALLER and run.get('head_branch') == 'main'
            and run.get('event') in ('push', 'workflow_run', 'schedule', 'workflow_dispatch')
            and run.get('repository', {}).get('id') == REPOSITORY_ID
            and run.get('head_repository', {}).get('id') == REPOSITORY_ID)


def persisted_failure(run, jobs):
    """Only a new-contract job failing AFTER a successful immutable upload."""
    if not trusted_run(run, conclusion='failure'):
        return False
    matches = [j for j in jobs if j.get('run_id') == run['id']
               and j.get('name', '').endswith('/ lock-future-only')]
    if len(matches) != 1:
        return False
    job = matches[0]
    if job.get('status') != 'completed' or job.get('conclusion') != 'failure':
        return False
    steps = job.get('steps', [])
    markers = [i for i, s in enumerate(steps) if s.get('name') == PERSISTED_STEP]
    if len(markers) != 1:
        return False
    index = markers[0]
    before = steps[:index + 1]
    required = {'Decrypt verified state and candidate',
                'Restore and verify earlier immutable SHADOW locks',
                'Encrypt immutable future locks', PERSISTED_STEP}
    success = {s['name'] for s in before if s.get('conclusion') == 'success'}
    if not required <= success or any(s.get('conclusion') not in ('success', 'skipped') for s in before):
        return False
    allowed = {'Checkout read-only WILD4 public projection adapter',
               'Project saved WILD4 locks and official settlements without source mutation',
               'Deliver WILD4 public projection and verify exact Site GET readback',
               'Require successful attempted public projection and delivery',
               'Require completion for verified NIGHT handoff'}
    failed = {s['name'] for s in steps[index + 1:] if s.get('conclusion') == 'failure'}
    return bool(failed) and failed <= allowed


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


def api_json(path):
    token = os.environ['GH_TOKEN']
    request = urllib.request.Request('https://api.github.com/repos/' + REPO + path,
        headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                 'X-GitHub-Api-Version': '2022-11-28'})
    with urllib.request.urlopen(request, timeout=35) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    require(len(raw) <= 2 * 1024 * 1024, 'API_BUDGET')
    return json.loads(raw)


def verify_donor_metadata(spec, donor, run, listing, jobs):
    require(trusted_run(run, conclusion='failure') and run['id'] == donor['run_id']
            and run.get('head_sha') == spec['head_sha']
            and any(w.get('sha') == spec['reusable_sha'] for w in run.get('referenced_workflows', [])),
            'ORIGINAL_RUN')
    matches = [a for a in listing.get('artifacts', []) if a.get('id') == donor['artifact_id']]
    require(len(matches) == 1, 'ARTIFACT_ID')
    artifact = matches[0]; binding = artifact.get('workflow_run', {})
    require(artifact.get('name') == donor['artifact_name'] and artifact.get('expired') is False
            and artifact.get('size_in_bytes') == donor['artifact_size']
            and artifact.get('digest') == 'sha256:' + donor['artifact_sha256']
            and binding.get('id') == donor['run_id'] and binding.get('head_sha') == spec['head_sha']
            and binding.get('head_branch') == 'main'
            and binding.get('repository_id') == REPOSITORY_ID
            and binding.get('head_repository_id') == REPOSITORY_ID, 'ARTIFACT_BINDING')
    matches = [j for j in jobs if j.get('id') == donor['job_id'] and j.get('run_id') == donor['run_id']]
    require(len(matches) == 1, 'ORIGINAL_JOB')
    steps = {s['name']: s.get('conclusion') for s in matches[0].get('steps', [])}
    require(all(steps.get(n) == 'success' for n in (
        'Restore and verify earlier immutable SHADOW locks', 'Lock future races before deadline without any production write',
        'Encrypt immutable future locks', 'Preserve encrypted shadow locks')),
        'ORIGINAL_PERSISTENCE')


def download_donor(spec, donor, root):
    require(0 < donor['artifact_size'] <= 4 * 1024 * 1024, 'ZIP_BUDGET')
    run = api_json('/actions/runs/' + str(donor['run_id']))
    listing = api_json('/actions/runs/' + str(donor['run_id']) + '/artifacts?per_page=100')
    jobs = api_json('/actions/runs/' + str(donor['run_id']) + '/jobs?per_page=100')['jobs']
    verify_donor_metadata(spec, donor, run, listing, jobs)
    opener = urllib.request.build_opener(NoRedirect())
    req = urllib.request.Request('https://api.github.com/repos/' + REPO + '/actions/artifacts/'
        + str(donor['artifact_id']) + '/zip', headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
        'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'})
    try:
        response = opener.open(req, timeout=35)
    except urllib.error.HTTPError as exc:
        require(exc.code == 302, 'ZIP_REDIRECT')
        location = exc.headers.get('Location', '')
    else:
        response.close(); raise ValueError('WILD4_PUBLICATION_CONTINUITY_ZIP_REDIRECT')
    url = urllib.parse.urlsplit(location)
    require(url.scheme == 'https' and url.hostname and url.username is None and url.password is None
            and url.port in (None, 443), 'SIGNED_URL')
    # The storage request deliberately has no GitHub authorization header.
    with opener.open(urllib.request.Request(location), timeout=35) as response:
        raw = response.read(donor['artifact_size'] + 1)
    require(len(raw) == donor['artifact_size'] and digest(raw) == donor['artifact_sha256'], 'ZIP_HASH')
    folder = root / str(donor['artifact_id']); folder.mkdir()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        require(len(entries) == 1 and entries[0].filename == 'future-locks.tgz.gpg'
                and not entries[0].is_dir() and entries[0].file_size <= 4 * 1024 * 1024
                and archive.testzip() is None, 'ZIP_CONTENTS')
        encrypted = folder / 'saved.tgz.gpg'; encrypted.write_bytes(archive.read(entries[0]))
    clear = folder / 'saved.tgz'
    subprocess.run(['gpg', '--batch', '--yes', '--pinentry-mode', 'loopback', '--passphrase-fd', '0',
                    '--decrypt', '--output', str(clear), str(encrypted)],
                   input=(os.environ['STATE_PASSPHRASE'] + '\n').encode(),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    target = folder / 'original'; target.mkdir()
    with tarfile.open(clear, 'r:gz') as archive:
        members = archive.getmembers(); names = set()
        require(0 < len(members) <= 20000 and sum(m.size for m in members) <= 128 * 1024 * 1024, 'TAR_BUDGET')
        for member in members:
            p = Path(member.name)
            require(not p.is_absolute() and '..' not in p.parts and p.parts
                    and p.parts[0] == 'g11-wild4-future-locks'
                    and (member.isfile() or member.isdir()) and member.name not in names, 'TAR_PATH')
            names.add(member.name)
        archive.extractall(target, filter='data')
    return donor, target / 'g11-wild4-future-locks'


def inventory(root):
    result = {}
    for path in sorted(root.rglob('*')):
        require(not path.is_symlink() and (path.is_dir() or path.is_file()), 'FILE_TYPE')
        if path.is_file():
            result[path.relative_to(root).as_posix()] = digest(path.read_bytes())
    return result


def verify_ledger(root, day):
    from g11.wild_pack.v1 import wild4_lifecycle_v1 as life
    from g11.wild_pack.v1.wild4_display_projection_v1 import project
    pin = life.read(root / 'day-model-binding.json')
    state = life.read(life.state_path(root))
    require(pin.get('schema') == 'G11_WILD4_DAY_MODEL_BINDING_V1' and pin.get('date') == day
            and pin.get('authority') == life.AUTHORITY and state.get('authority') == life.AUTHORITY
            and state.get('active') == pin.get('model')
            and life.model_info(root / 'model-registry/models' / pin['model']['identity']) == pin['model'],
            'MODEL_BINDING')
    projection = project(root, operational_date=day, model_sha256=pin['model']['model_sha256'])
    keys = {r['race_key'] for r in projection['races']}
    for key in keys:
        lock = read_sealed(root / (key + '.json'))
        require(lock.get('CANDIDATE_REGISTRY_SHA256') == pin['model']['registry_sha256'], 'LOCK_REGISTRY')
        if 'EFFECTIVE_MODEL_IDENTITY' in lock:
            require(lock['EFFECTIVE_MODEL_IDENTITY'] == pin['model']['identity']
                    and lock.get('CALIBRATION_SHA256') == pin['model'].get('calibration_sha256'), 'LOCK_MODEL')
    return keys, pin, state


def recover_originals(base, spec, donors, *, verify=verify_ledger):
    """Validate ALL originals before an atomic directory swap; copy bytes only."""
    require(spec['day'] == DAY, 'RECOVERY_DAY')
    before = inventory(base)
    base_keys, pin, state = verify(base, DAY)
    if (base / RECEIPT).is_file():
        receipt = read_sealed(base / RECEIPT)
        require(receipt['inventory_spec_sha256'] == digest(canonical(spec))
                and len(base_keys) == spec['expected_locks']
                and all(digest((base / (k + '.json')).read_bytes()) == v
                        for k, v in receipt['restored_file_sha256'].items()), 'RECOVERY_RECEIPT')
        require(inventory(base) == before, 'NOOP_MUTATION')
        return {'restored': 0, 'locks': len(base_keys), 'generation_count': 0}
    require(len(base_keys) == spec['baseline_locks'], 'BASELINE_COVERAGE')
    require(not any(p.startswith('settlements/') for p in before)
            and state.get('last_completed_night') != DAY
            and (state.get('last_attempt') or {}).get('date') != DAY
            and (state.get('night_coverage_retry') or {}).get('binding', {}).get('date') != DAY,
            'NIGHT_ALREADY_PROCESSED')
    require(len(donors) == len(spec['donors'])
            and {d['artifact_id'] for d, _ in donors} == {d['artifact_id'] for d in spec['donors']}, 'DONOR_COVERAGE')
    additions = {}; provenance = []
    prefix = 'model-registry/models/' + pin['model']['identity'] + '/'
    for donor, original in donors:
        donor_before = inventory(original)
        keys, original_pin, _ = verify(original, DAY)
        selected = {l['race_key'] for l in donor['locks']}
        require(original_pin == pin and (original / 'day-model-binding.json').read_bytes()
                == (base / 'day-model-binding.json').read_bytes(), 'DAY_MODEL_BYTES')
        require({k: v for k, v in donor_before.items() if k.startswith(prefix)}
                == {k: v for k, v in before.items() if k.startswith(prefix)}, 'MODEL_BYTES')
        require(keys == base_keys | selected and not selected & base_keys
                and not selected & additions.keys(), 'ORIGINAL_COVERAGE')
        require(not any(p.startswith('settlements/') for p in donor_before), 'DONOR_SETTLED')
        for key in base_keys:
            require((original / (key + '.json')).read_bytes() == (base / (key + '.json')).read_bytes(),
                    'OVERLAP_BYTES:' + key)
        for saved in donor['locks']:
            key = saved['race_key']; raw = (original / (key + '.json')).read_bytes(); lock = read_sealed(original / (key + '.json'))
            require(lock['STANDARD_RACE_KEY'] == key and lock['LOCKED_AT'] == saved['locked_at']
                    and lock['RACE_DEADLINE_AT'] == saved['deadline_at'] and lock['BUY_COUNT'] == saved['buy_count']
                    and datetime.fromisoformat(lock['LOCKED_AT']) < datetime.fromisoformat(lock['RACE_DEADLINE_AT'])
                    and lock['OFFICIAL_RESULT_USED'] is False and lock['PRODUCTION_BETS_CHANGED'] is False,
                    'ORIGINAL_LOCK_IDENTITY:' + key)
            additions[key] = raw
        require(inventory(original) == donor_before, 'DONOR_MUTATION')
        provenance.append((donor, (original / 'batch-manifest.json').read_bytes()))
    require(len(base_keys | additions.keys()) == spec['expected_locks'], 'FINAL_COVERAGE')
    stage = Path(tempfile.mkdtemp(prefix=base.name + '-validated-', dir=base.parent))
    try:
        shutil.copytree(base, stage, dirs_exist_ok=True)
        proof = stage / Path(RECEIPT).parent; proof.mkdir(parents=True)
        manifest_raw = (base / 'batch-manifest.json').read_bytes(); manifest = read_sealed(base / 'batch-manifest.json')
        (proof / 'base-batch-manifest.json').write_bytes(manifest_raw)
        for donor, raw in provenance:
            (proof / (str(donor['artifact_id']) + '-batch-manifest.json')).write_bytes(raw)
        for key, raw in additions.items():
            with (stage / (key + '.json')).open('xb') as output:
                output.write(raw)
        merged = sealed({**manifest, 'RACE_KEYS': sorted(base_keys | additions.keys()),
                         'LOCKED_BEFORE_DEADLINE': spec['expected_locks']})
        (stage / 'batch-manifest.json').write_bytes(canonical(merged))
        after_keys, after_pin, _ = verify(stage, DAY)
        require(after_keys == base_keys | additions.keys() and after_pin == pin, 'FINAL_PROJECT')
        after = inventory(stage)
        require(all(after.get(k) == v for k, v in before.items() if k != 'batch-manifest.json')
                and all((stage / (k + '.json')).read_bytes() == v for k, v in additions.items()), 'STAGED_MUTATION')
        receipt = sealed({'schema': 'G11_WILD4_PUBLICATION_RECOVERY_V1', 'date': DAY,
            'inventory_spec_sha256': digest(canonical(spec)), 'generation_count': 0,
            'settlement_generated': False, 'base_locks': len(base_keys), 'locks': len(after_keys),
            'restored_file_sha256': {k: digest(v) for k, v in additions.items()},
            'original_artifacts': [d for d, _ in provenance], 'inventory_before': before,
            'derived_manifest_sha256': merged['CONTENT_SHA256']})
        (stage / RECEIPT).write_bytes(canonical(receipt))
        require(read_sealed(stage / RECEIPT) == receipt and inventory(base) == before, 'READBACK')
        backup = base.with_name(base.name + '-before-publication-recovery')
        require(not backup.exists(), 'BACKUP_EXISTS')
        os.rename(base, backup)
        try:
            os.rename(stage, base)
        except BaseException:
            os.rename(backup, base); raise
        require(inventory(backup) == before and read_sealed(base / RECEIPT) == receipt, 'COMMIT_READBACK')
        return {'restored': len(additions), 'locks': len(after_keys), 'generation_count': 0}
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('mode', choices=('admit-failure', 'recover-nov8'))
    parser.add_argument('--run', type=Path); parser.add_argument('--ledger', type=Path)
    args = parser.parse_args()
    require(os.environ.get('GITHUB_REPOSITORY') == REPO, 'REPOSITORY')
    if args.mode == 'admit-failure':
        run = json.loads(args.run.read_bytes())
        if not trusted_run(run, conclusion='failure'):
            raise SystemExit(1)
        jobs = api_json('/actions/runs/' + str(run['id']) + '/jobs?per_page=100')['jobs']
        if not persisted_failure(run, jobs):
            raise SystemExit(1)
        print('WILD4_SAVED_FAILURE_ACCEPTED RUN=' + str(run['id'])); return
    require(os.environ.get('OPERATIONAL_DATE') == DAY, 'DAY')
    from wild4_publication_recovery_20261108 import SPEC
    spec = SPEC
    if (args.ledger / RECEIPT).is_file():
        result = recover_originals(args.ledger, spec, [])
    else:
        with tempfile.TemporaryDirectory(prefix='wild4-originals-', dir=os.environ['RUNNER_TEMP']) as folder:
            with ThreadPoolExecutor(max_workers=4) as executor:
                donors = list(executor.map(lambda d: download_donor(spec, d, Path(folder)), spec['donors']))
            result = recover_originals(args.ledger, spec, donors)
    print('WILD4_PUBLICATION_RECOVERY=' + json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
