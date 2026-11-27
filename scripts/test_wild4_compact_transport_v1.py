"""Read-only first/warm transport and the unchanged WILD4 forecast bodies."""
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

import encrypt_runtime_state_v1 as checkpoint
import test_compact_publication_checkpoint_v1 as fixtures
import wild4_compact_transport_v1 as transport

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / '.github/workflows/g11-wild4-shadow-lock-only.yml').read_text()


def step(name):
    return WORKFLOW.split('      - name: ' + name + '\n', 1)[1].split('      - name: ', 1)[0]


# Exact public workflow baseline 0634adb64f933439ca0cd65c3f15e7d733db95af.
# These current bodies complement the retained historical pinned-source guard.
PROTECTED = {
    'Carry previous-day model registry without copying previous predictions': '08b4d4c9cec584c0d4328fd758cf20c2c1e1758613239bfa836cd5cc94689f02',
    'Bind one model for the operational day': '0aafa72c74beb10949c98a8f77fa14b1c423076b247d3ade529658bcec56fa98',
    'Verify frozen WILD4 inference runtime': '4495877bd12adea8c5d209ed488a85122892ef0858bf8161cda0d4340c6d0202',
    'Check for a matching unexpired official odds packet': '4f31d801abc826a8e31f2e90f9ba68f0c142f2771ebb94b9e10e8da863966f49',
    'Lock future races before deadline without any production write': '4c4fa5c0dec49e4fb47d4322bee731ab7ad8c503d659ee778db5abd13bffb4a6',
    'Settle only immutable locks from verified final NIGHT': '80f4df9e28fada9f0fa810948d3edc943060da57e0e2a52bdd410c08147b7bd4',
    'Continue existing BET learning after completed NIGHT': '13d0c5f33151da4f614e685da3d962974730dcf62bc03a73a47efe980aa07b6d',
}


class SavedStageTransportTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CompletePublicationProofTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.day, self.key = self.fixture.ready['day'], self.fixture.key
        self.first_delta = self.fixture.ready['delta_key']
        self.output = self.fixture.root / 'output'
        self.environ = patch.dict(os.environ, {'OPERATIONAL_DATE': self.day,
            'PREDEADLINE_CHECKPOINT_KEY': self.key, 'PREDEADLINE_DELTA_KEY': self.first_delta,
            'CAPTURE_STAGE': 'critical', 'PREPARED_BASE_KEY': self.fixture.base_key,
            'CHECKPOINT_KEY': self.key, 'DELTA_KEY': self.first_delta,
            'PREWARM_BASE_KEY': self.fixture.base_key})
        self.environ.start()
        self.addCleanup(self.environ.stop)

    def archive(self, stage):
        manifest = self.fixture.manifest
        if stage == 'warm':
            first_target = checkpoint.snapshot_state(self.fixture.target)
            (self.fixture.target / 'store/warm-observation').write_bytes(b'new timely warm observation')
            manifest = checkpoint.delta_manifest(self.fixture.target, self.fixture.before,
                self.fixture.base_key, checkpoint.file_sha(self.fixture.cipher), capture_stage='warm',
                first_target=first_target, first_delta_cipher_sha256='c' * 64)
        checkpoint.write_delta(self.fixture.target, manifest, self.fixture.root / 'g11-compact.tgz')
        return manifest

    def prepared(self):
        (self.fixture.root / 'g11-prewarm-base').mkdir()
        (self.fixture.root / 'g11-prewarm-cipher.tgz.gpg').write_bytes(b'authenticated physical full')

    def test_old_critical_manifest_and_exact_prepared_base_remain_compatible(self):
        self.archive('critical')
        self.prepared()
        before = checkpoint.snapshot_state(self.fixture.target)
        transport.manifest()
        self.assertEqual(self.output.read_text(), 'base_key=' + self.fixture.base_key + '\nuse_prewarm=true\n')
        self.assertEqual(checkpoint.snapshot_state(self.fixture.target), before)

    def test_warm_manifest_uses_real_parent_identity_and_its_own_exact_prepared_base(self):
        self.archive('warm')
        self.prepared()
        with patch.dict(os.environ, {'CAPTURE_STAGE': 'warm',
                                    'PREDEADLINE_DELTA_KEY': self.first_delta + '-warm'}):
            transport.manifest()
        self.assertEqual(self.output.read_text(), 'base_key=' + self.fixture.base_key + '\nuse_prewarm=true\n')
        self.assertEqual(json.loads((self.fixture.target / 'relay-state.json').read_bytes())
                         ['CRITICAL']['LAST_CRITICAL_RUN_ID'], '43')

    def test_wrong_requested_stage_never_adopts_a_saved_manifest(self):
        for actual, requested in (('critical', 'warm'), ('warm', 'critical')):
            with self.subTest(actual=actual, requested=requested):
                self.archive(actual)
                delta = self.first_delta + ('-warm' if actual == 'warm' else '')
                with patch.dict(os.environ, {'CAPTURE_STAGE': requested, 'PREDEADLINE_DELTA_KEY': delta}), \
                        self.assertRaises(ValueError):
                    transport.manifest()
                self.assertFalse(self.output.exists())

    def test_different_prepared_base_is_discarded_before_full_exact_restore(self):
        self.archive('critical')
        self.prepared()
        with patch.dict(os.environ, {'PREPARED_BASE_KEY': self.fixture.base_key + '-other'}):
            transport.manifest()
        self.assertEqual(self.output.read_text(), 'base_key=' + self.fixture.base_key + '\nuse_prewarm=false\n')
        self.assertFalse((self.fixture.root / 'g11-prewarm-base').exists())
        self.assertFalse((self.fixture.root / 'g11-prewarm-cipher.tgz.gpg').exists())

    def test_wait_selects_only_requested_stage_and_retains_boolean_visibility(self):
        for stage in ('critical', 'warm'):
            for visible in (True, False):
                with self.subTest(stage=stage, visible=visible):
                    self.output.unlink(missing_ok=True)
                    delta = self.first_delta + ('-warm' if stage == 'warm' else '')
                    with patch.dict(os.environ, {'CAPTURE_STAGE': stage, 'DELTA_KEY': delta}), \
                            patch.object(transport, 'wait_for_compact', return_value=visible) as wait:
                        transport.wait()
                    self.assertEqual(wait.call_args.args, (self.fixture.base_key, self.key, delta))
                    if stage == 'critical':
                        self.assertEqual(wait.call_args.kwargs, {})
                    else:
                        self.assertTrue(callable(wait.call_args.kwargs['query']))
                    self.assertEqual(self.output.read_text(), 'available=' + str(visible).lower() + '\n')

    def test_mismatched_wait_stage_does_not_poll_metadata_or_open_a_gate(self):
        with patch.dict(os.environ, {'CAPTURE_STAGE': 'warm'}), \
                patch.object(transport, 'wait_for_compact') as wait, self.assertRaises(ValueError):
            transport.wait()
        wait.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_pinned_helper_cli_executes_the_actual_manifest_reader(self):
        self.archive('critical')
        result = subprocess.run([sys.executable, '-S', str(ROOT / 'scripts/wild4_compact_transport_v1.py'),
                                 'manifest'], capture_output=True, text=True, check=False, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('WILD4_AUTHENTICATED_DELTA=PASS BASE=' + self.fixture.base_key, result.stdout)
        self.assertIn('STAGE=critical', result.stdout)


class WarmUnneededWaitTests(unittest.TestCase):
    def metadata(self, *, status='completed', conclusion='skipped', run=43):
        return {'total_count': 1, 'jobs': [{'run_id': run, 'steps': [{
            'name': transport.WARM_SAVE_STEP, 'status': status, 'conclusion': conclusion}]}]}

    def exercise(self, jobs, *, visible=False, requested='warm'):
        import tempfile
        from datetime import datetime
        from zoneinfo import ZoneInfo
        day = datetime.now(ZoneInfo('Asia/Tokyo')).date().isoformat()
        key = 'g11-state-v4-critical-' + day + '-43'
        delta = 'g11-state-v4-delta-' + day + '-43' + ('-warm' if requested == 'warm' else '')
        base = 'g11-state-v4-published-' + day + '-42-1-checkpoint'
        calls, sleeps = [], []
        now = [0]
        def query(path):
            calls.append(path)
            if path.startswith('/actions/caches?'):
                return {'actions_caches': [{'key': delta, 'ref': 'refs/heads/main'}] if visible else []}
            if '/jobs?' in path:
                if isinstance(jobs, Exception):
                    raise jobs
                return jobs
            return {'id': 43, 'status': 'in_progress'}
        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds
        original_wait, original_query = transport.wait_for_compact, transport.warm_query
        def bounded(base, key, delta, **options):
            return original_wait(base, key, delta, maximum=10, clock=lambda: now[0], sleeper=sleep,
                                 **options)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'output'
            stdout = io.StringIO()
            with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'tatsuuut/-g11-official-data-relay',
                    'PREWARM_BASE_KEY': base, 'CHECKPOINT_KEY': key, 'DELTA_KEY': delta,
                    'CAPTURE_STAGE': requested, 'GITHUB_OUTPUT': str(output)}), \
                    patch.object(transport, 'warm_query', side_effect=lambda run:
                                 original_query(run, query=query)) as wrapped, \
                    patch.object(transport, 'wait_for_compact', side_effect=bounded), redirect_stdout(stdout):
                if requested == 'critical':
                    # The original reader is intentionally unchanged, so avoid
                    # real IO while proving no warm wrapper/endpoint is used.
                    with patch.object(transport, 'wait_for_compact', return_value=True) as first:
                        transport.wait()
                    self.assertEqual(first.call_args.kwargs, {})
                else:
                    transport.wait()
            return output.read_text(), stdout.getvalue(), calls, sleeps, wrapped.call_count

    def test_exact_parent_skipped_save_releases_only_an_absent_warm_wait(self):
        output, log, calls, sleeps, _ = self.exercise(self.metadata())
        self.assertEqual(output, 'available=false\n')
        self.assertIn('EXACT_PARENT_CHECKPOINT_SKIPPED', log)
        self.assertEqual(sleeps, [])
        self.assertEqual(len(calls), 3)
        self.assertTrue(calls[0].startswith('/actions/caches?'))
        self.assertEqual(calls[1], '/actions/runs/43')
        self.assertEqual(calls[2], '/actions/runs/43/jobs?filter=latest&per_page=100')

    def test_exact_visible_delta_wins_without_consulting_skipped_job_metadata(self):
        output, log, calls, sleeps, _ = self.exercise(self.metadata(), visible=True)
        self.assertEqual(output, 'available=true\n')
        self.assertIn('EXACT_CACHE_VISIBLE', log)
        self.assertNotIn('CHECKPOINT_SKIPPED', log)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeps, [])

    def test_pending_success_failure_or_unknown_metadata_preserves_bounded_wait(self):
        ambiguous = self.metadata()
        ambiguous['jobs'][0]['steps'].append(dict(ambiguous['jobs'][0]['steps'][0]))
        truncated = self.metadata()
        truncated['total_count'] = 2
        for value in (self.metadata(status='pending', conclusion=None),
                      self.metadata(status='in_progress', conclusion=None),
                      self.metadata(conclusion='success'), self.metadata(conclusion='failure'),
                      self.metadata(run=44), ambiguous, truncated, {},
                      OSError('metadata unavailable')):
            with self.subTest(value=str(value)):
                output, log, _, sleeps, _ = self.exercise(value)
                self.assertEqual(output, 'available=false\n')
                self.assertEqual(sleeps, [10])
                self.assertNotIn('EXACT_PARENT_CHECKPOINT_SKIPPED', log)
                self.assertIn('BOUNDED_WAIT_EXPIRED', log)

    def test_skipped_publication_is_not_skipped_warm_checkpoint(self):
        value = self.metadata()
        value['jobs'][0]['steps'][0]['name'] = 'Publish persisted warm LOCKs'
        _, log, _, sleeps, _ = self.exercise(value)
        self.assertEqual(sleeps, [10])
        self.assertNotIn('EXACT_PARENT_CHECKPOINT_SKIPPED', log)

    def test_critical_uses_no_new_metadata_wrapper_or_endpoint(self):
        output, _, calls, sleeps, wrappers = self.exercise(self.metadata(), requested='critical')
        self.assertEqual(output, 'available=true\n')
        self.assertEqual((calls, sleeps, wrappers), ([], [], 0))

    def test_parent_step_name_is_exactly_bound_to_the_deployed_workflow(self):
        parent = (ROOT / '.github/workflows/g11-free-runner.yml').read_text()
        self.assertEqual(parent.count('      - name: ' + transport.WARM_SAVE_STEP + '\n'), 1)


class WorkflowBoundaryTests(unittest.TestCase):
    def test_all_seven_prediction_and_accounting_blocks_remain_byte_identical(self):
        for name, expected in PROTECTED.items():
            with self.subTest(step=name):
                self.assertEqual(hashlib.sha256(step(name).encode()).hexdigest(), expected)

    def test_legacy_default_and_nonconcurrent_immutable_ledger_are_retained(self):
        self.assertIn('default: "critical"', WORKFLOW.split('      capture_stage:', 1)[1].split('      prewarm_base_key:', 1)[0])
        self.assertIn("CAPTURE_STAGE: ${{ inputs.capture_stage || 'critical' }}", WORKFLOW)
        self.assertIn('group: g11-wild4-prospective-shadow-lock\n  cancel-in-progress: false', WORKFLOW)
        self.assertLessEqual(len(WORKFLOW.encode()), 151029)
        self.assertRegex(step('Prepare pinned WILD4 dependency before state acquisition'),
                         r'uses: tatsuuut/-g11-official-data-relay/\.github/actions/g11-public-python@[a-f0-9]{40}')

    def test_stage_cache_paths_retain_their_distinct_exact_cache_versions(self):
        for name in ('Restore exact authenticated complete reconstruction delta',
                     'Restore the exact handoff after bounded visibility wait'):
            block = step(name)
            self.assertIn("inputs.capture_stage == 'warm' && '.relay-output/warm-delta.tgz.gpg' || '.relay-output/early-delta.tgz.gpg'", block)
            self.assertIn('key: ${{ inputs.predeadline_delta_key }}', block)
            self.assertNotIn('restore-keys:', block)

    def test_remote_exact_readback_and_gpg_authentication_precede_manifest_use(self):
        block = step('Authenticate compact manifest before choosing its exact base')
        self.assertIn("steps.compact-cache.outputs.cache-hit == 'true' || steps.compact-cache-retry.outputs.cache-hit == 'true'", block)
        self.assertIn('set -euo pipefail', block)
        self.assertIn('STATE_PASSPHRASE: ${{ secrets.G11_PRIVATE_REPO_TOKEN }}', block)
        self.assertLess(block.index('--decrypt'), block.index('wild4_compact_transport_v1.py" manifest'))
        self.assertNotIn('continue-on-error:', block)

    def test_transport_shell_uses_only_the_pinned_action_helper_directory(self):
        block = step('Wait only for the exact new saved handoff after preparation')
        self.assertIn('"${G11_PUBLIC_RUNTIME_HELPERS}/wild4_compact_transport_v1.py" wait', block)
        self.assertIn('DELTA_KEY: ${{ inputs.predeadline_delta_key }}', block)
        self.assertIn('GH_TOKEN: ${{ github.token }}', block)


if __name__ == '__main__':
    unittest.main()
