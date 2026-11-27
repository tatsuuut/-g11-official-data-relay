"""Authenticate the requested saved capture stage before WILD4 reads its base.

This helper only waits for saved cache metadata and inspects authenticated
transport. The existing frozen workflow owns all inference and immutable LOCKs.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

from encrypt_runtime_state_v1 import inspect_delta, require, validate_compact_keys, wait_for_compact

WARM_SAVE_STEP = 'Save and verify the warm checkpoint before full backup'


class WarmCheckpointSkipped(Exception):
    """The exact parent's warm checkpoint was unambiguously not attempted."""


def warm_checkpoint_skipped(value, run):
    if (type(value) is not dict or type(value.get('jobs')) is not list
            or type(value.get('total_count')) is not int
            or value['total_count'] != len(value['jobs']) or len(value['jobs']) > 100):
        return False
    matches = []
    for job in value['jobs']:
        if (type(job) is not dict or str(job.get('run_id')) != run
                or type(job.get('steps')) is not list
                or any(type(step) is not dict for step in job['steps'])):
            return False
        matches.extend(step for step in job['steps']
                       if type(step) is dict and step.get('name') == WARM_SAVE_STEP)
    return (len(matches) == 1 and matches[0].get('status') == 'completed'
            and matches[0].get('conclusion') == 'skipped')


def warm_query(run, *, query=None):
    """Only shorten an otherwise bounded wait; never authorize input use."""
    repository = os.environ['GITHUB_REPOSITORY']
    require(repository == 'tatsuuut/-g11-official-data-relay', 'PREWARM_REPOSITORY')
    def read(path):
        response = subprocess.run(['gh', 'api', 'repos/' + repository + path],
            capture_output=True, text=True, timeout=8, check=True)
        return json.loads(response.stdout)
    read = query or read
    parent_path = '/actions/runs/' + run
    def guarded(path):
        value = read(path)
        if type(value) is not dict:
            raise OSError('WARM_PREWARM_METADATA_SHAPE')
        if path.startswith('/actions/caches?') and (type(value.get('actions_caches')) is not list
                or any(type(row) is not dict for row in value['actions_caches'])):
            raise OSError('WARM_PREWARM_CACHE_METADATA_SHAPE')
        if path == parent_path:
            if str(value.get('id')) != run:
                raise OSError('WARM_PREWARM_PARENT_ID')
            # wait_for_compact calls this only after the exact warm cache miss.
            # A failed save/publication is insufficient: valid saved bytes may
            # still become visible. Only an explicitly skipped save can stop it.
            try:
                jobs = read(parent_path + '/jobs?filter=latest&per_page=100')
            except (OSError, ValueError, subprocess.SubprocessError):
                print('WILD4_WARM_PREWARM=JOB_METADATA_UNAVAILABLE_WAIT_RETAINED', flush=True)
            else:
                if warm_checkpoint_skipped(jobs, run):
                    raise WarmCheckpointSkipped
        return value
    return guarded


def stage(day, checkpoint, delta):
    requested = os.environ.get('CAPTURE_STAGE', 'critical')
    require(requested in {'critical', 'warm'}
            and validate_compact_keys(day, checkpoint, delta) == requested, 'REQUESTED_STAGE')
    return requested


def wait():
    checkpoint, delta = os.environ['CHECKPOINT_KEY'], os.environ['DELTA_KEY']
    # The checkpoint carries the real parent run; the suffix binds only its
    # second saved capture. No synthetic run or alternate forecast is created.
    day = checkpoint.removeprefix('g11-state-v4-critical-')[:10]
    requested = stage(day, checkpoint, delta)
    options = ({'query': warm_query(checkpoint.rsplit('-', 1)[1])} if requested == 'warm' else {})
    try:
        available = wait_for_compact(os.environ['PREWARM_BASE_KEY'], checkpoint, delta, **options)
    except WarmCheckpointSkipped:
        print('WILD4_WARM_PREWARM=EXACT_PARENT_CHECKPOINT_SKIPPED', flush=True)
        available = False
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write('available=' + str(available).lower() + '\n')


def manifest():
    day = os.environ['OPERATIONAL_DATE']
    checkpoint, delta = os.environ['PREDEADLINE_CHECKPOINT_KEY'], os.environ['PREDEADLINE_DELTA_KEY']
    requested = stage(day, checkpoint, delta)
    temporary = Path(os.environ['RUNNER_TEMP'])
    value = inspect_delta(temporary / 'g11-compact.tgz', day=day, checkpoint_key=checkpoint)
    require(value['delta_key'] == delta
            and value.get('capture_stage', 'critical') == requested, 'REQUESTED_STAGE')
    reuse = (value['base_key'] == os.environ.get('PREPARED_BASE_KEY')
             and (temporary / 'g11-prewarm-base').is_dir()
             and (temporary / 'g11-prewarm-cipher.tgz.gpg').is_file())
    if not reuse:
        shutil.rmtree(temporary / 'g11-prewarm-base', ignore_errors=True)
        (temporary / 'g11-prewarm-cipher.tgz.gpg').unlink(missing_ok=True)
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write('base_key=' + value['base_key'] + '\nuse_prewarm=' + str(reuse).lower() + '\n')
    print('WILD4_AUTHENTICATED_DELTA=PASS BASE=' + value['base_key'] + ' STAGE=' + requested, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('wait', 'manifest'))
    args = parser.parse_args()
    if args.mode == 'wait':
        wait()
    else:
        manifest()


if __name__ == '__main__':
    main()
