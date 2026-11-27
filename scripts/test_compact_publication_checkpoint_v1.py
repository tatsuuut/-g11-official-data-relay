"""Exercise complete compact proof, fail-closed fallback and LIVE scheduling."""
import copy
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
import unittest
from unittest.mock import patch

import compact_publication_checkpoint_v1 as proof
import encrypt_runtime_state_v1 as checkpoint

ROOT = Path(__file__).resolve().parents[1]
ACTION = (ROOT / '.github/actions/g11-publication-checkpoint/action.yml').read_text()
WORKFLOW = (ROOT / '.github/workflows/g11-free-runner.yml').read_text()
DAY = datetime.now(proof.JST).date().isoformat()


def step(name, source=ACTION):
    return source.split('- name: ' + name + '\n', 1)[1].split('- name:', 1)[0]


class CompletePublicationProofTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        self.environ = patch.dict(os.environ, {'GITHUB_RUN_ID': '43',
            'GITHUB_REPOSITORY': 'tatsuuut/-g11-official-data-relay',
            'GITHUB_OUTPUT': str(self.root / 'output'), 'G11_PRIVATE_REPO_TOKEN': 'synthetic',
            'RUNNER_TEMP': str(self.root), 'GNUPGHOME': str(self.root / 'gnupg')})
        self.environ.start()
        self.addCleanup(self.environ.stop)
        (self.root / 'gnupg').mkdir(mode=0o700)
        if shutil.which('gpgconf'):
            self.addCleanup(lambda: subprocess.run(['gpgconf', '--homedir', str(self.root / 'gnupg'),
                '--kill', 'gpg-agent'], capture_output=True, check=False))
        self.out = self.root / '.relay-output'
        self.out.mkdir()
        (self.root / '.relay-cache').mkdir()
        self.cipher = self.root / '.relay-cache/state.tgz.gpg'
        self.cipher.write_bytes(b'authenticated complete base ciphertext')
        self.base = self.root / 'base'
        self.base.mkdir()
        owner = {'immutable': True, 'operational_date_jst': DAY,
                 'authority_sha256': 'a' * 64, 'runtime_source_sha': 'b' * 40}
        data = {'relay-state.json': json.dumps({'OPERATIONAL_DATE_JST': DAY,
                    'MORNING_COMPLETE': True}).encode(),
                'daily-runtime/' + DAY + '/authority.json': json.dumps(owner).encode(),
                'store/prior-lock': b'immutable original LOCK',
                'store/odds': b'old odds', 'store/removed': b'old temporary observation',
                'models/static.bin': b'unchanged complete frozen model' * 1000}
        for name, value in data.items():
            path = self.base / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        (self.base / 'empty-history').mkdir()
        self.base_key = 'g11-state-v4-published-' + DAY + '-42-1-checkpoint'
        self.key = 'g11-state-v4-critical-' + DAY + '-43'
        checkpoint.save_base_inventory(self.base, self.out / 'base-inventory.json')
        proof.retain_base(self.base, self.cipher, self.base_key, DAY)
        self.target = self.root / '.relay-state'
        shutil.copytree(self.base, self.target)
        (self.target / 'store/removed').unlink()
        (self.target / 'new-empty-history').mkdir()
        (self.target / 'store/odds').write_bytes(b'new timely original odds')
        (self.target / 'store/new-lock').write_bytes(b'new immutable timely LOCK')
        gate = {'SCHEMA': 'G11_PREDEADLINE_CRITICAL_CHECKPOINT_V1',
            'OPERATIONAL_DATE': DAY, 'LAST_CRITICAL_RUN_ID': '43',
            'CRITICAL_CHECKPOINT_STATUS': 'PASS', 'POST_DEADLINE_GENERATION': 0,
            'LAST_TIMELY_SOURCE_COUNT': 2, 'AUTHORITY_SHA256': 'a' * 64,
            'RUNTIME_SOURCE_SHA': 'b' * 40}
        metadata = {'OPERATIONAL_DATE_JST': DAY, 'MORNING_COMPLETE': True, 'CRITICAL': gate}
        (self.target / 'relay-state.json').write_text(json.dumps(metadata))
        (self.out / 'result.json').write_text(json.dumps({
            'OPERATIONAL_DATE_JST': DAY, 'CRITICAL': gate}))
        self.before = checkpoint.snapshot_state(self.base)
        self.manifest = checkpoint.delta_manifest(self.target, self.before, self.base_key,
                                                 checkpoint.file_sha(self.cipher))
        self.archive = self.out / 'remote-readback.tgz'
        checkpoint.write_delta(self.target, self.manifest, self.archive)
        self.delta = self.out / 'early-delta.tgz.gpg'
        self.delta.write_bytes(b'authenticated remote compact readback ciphertext')
        self.ready = {name: self.manifest[name] for name in
                      ('day', 'run_id', 'checkpoint_key', 'delta_key', 'base_key')}
        self.ready['cipher_sha256'] = checkpoint.file_sha(self.delta)
        checkpoint.atomic_json(self.out / 'early-delta-ready.json', self.ready)

    def verify(self, hit='true'):
        # Authentication is covered separately with real GPG. These cases isolate
        # every manifest/inventory/remote dependency gate after authentication.
        with patch.object(proof, 'decrypt', side_effect=lambda _, path:
                          shutil.copyfile(self.archive, path)), \
                patch.object(proof, 'require_remote_base') as remote:
            proof.verify(self.key, hit)
            remote.assert_called_once_with(self.base_key)

    def test_readback_proves_complete_reconstruction_without_mutating_any_state(self):
        target = checkpoint.snapshot_state(self.target)
        self.verify()
        receipt = json.loads((self.out / 'compact-publication-proof.json').read_bytes())
        self.assertEqual(receipt['schema'], proof.PROOF_SCHEMA)
        self.assertEqual(receipt['target_inventory_sha256'], proof.digest(target))
        self.assertEqual(receipt['base_cipher_sha256'], checkpoint.file_sha(self.cipher))
        self.assertEqual((self.root / 'output').read_text(), 'ready=true\n')
        self.assertEqual(checkpoint.snapshot_state(self.target), target)
        self.assertEqual(checkpoint.snapshot_state(self.base), self.before)
        reconstructed = self.root / 'reconstructed'
        shutil.copytree(self.base, reconstructed)
        checkpoint.apply_delta(self.archive, reconstructed, day=DAY,
                               checkpoint_key=self.key, base_cipher=self.cipher)
        self.assertEqual(checkpoint.snapshot_state(reconstructed), target)

    def test_missing_remote_exact_readback_cannot_use_local_bytes_or_stale_proof(self):
        (self.out / 'compact-publication-proof.json').write_text('stale proof')
        with self.assertRaisesRegex(ValueError, 'EXACT_READBACK'):
            self.verify('false')
        self.assertFalse((self.out / 'compact-publication-proof.json').exists())
        self.assertFalse((self.root / 'output').exists())

    def test_corrupt_cipher_or_wrong_run_cannot_open_gate(self):
        original = self.delta.read_bytes()
        self.delta.write_bytes(original + b'corrupt')
        with self.assertRaisesRegex(ValueError, 'CURRENT_READY'):
            self.verify()
        self.delta.write_bytes(original)
        with patch.dict(os.environ, {'GITHUB_RUN_ID': '44'}), \
                self.assertRaisesRegex(ValueError, 'CURRENT_READY'):
            self.verify()
        self.assertFalse((self.root / 'output').exists())

    def test_wrong_base_cipher_index_and_target_file_each_fail_closed(self):
        for kind in ('cipher', 'index', 'model', 'lock', 'directory'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as folder:
                backup = Path(folder) / 'backup'
                shutil.copytree(self.root, backup)
                if kind == 'cipher':
                    self.cipher.write_bytes(b'wrong full base')
                elif kind == 'index':
                    index = json.loads((self.out / 'base-inventory.json').read_bytes())
                    index['inventory']['files']['store/prior-lock']['sha256'] = 'c' * 64
                    (self.out / 'base-inventory.json').write_text(json.dumps(index))
                elif kind == 'directory':
                    (self.target / 'new-empty-history').rmdir()
                else:
                    name = 'models/static.bin' if kind == 'model' else 'store/prior-lock'
                    (self.target / name).write_bytes(b'modified original')
                with self.assertRaises(ValueError):
                    self.verify()
                for path in self.root.iterdir():
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                shutil.copytree(backup, self.root, dirs_exist_ok=True)
        self.assertFalse((self.root / 'output').exists())

    def test_authenticated_replacement_payload_must_match_its_full_inventory_hash(self):
        with tarfile.open(self.archive, 'r:gz') as source:
            items = [(copy.copy(member), source.extractfile(member).read())
                     for member in source.getmembers()]
        member, raw = items[-1]
        items[-1] = member, b'x' * len(raw)
        with tarfile.open(self.archive, 'w:gz') as destination:
            for member, raw in items:
                destination.addfile(member, io.BytesIO(raw))
        with self.assertRaisesRegex(ValueError, 'REPLACEMENT_BYTES'):
            self.verify()
        self.assertFalse((self.root / 'output').exists())

    def test_remote_base_requires_exact_key_and_main_ref(self):
        valid = {'key': self.base_key, 'ref': 'refs/heads/main'}
        proof.require_remote_base(self.base_key, query=lambda _: {'actions_caches': [valid]})
        for rows in ([], [dict(valid, key=self.base_key + '-prefix')],
                     [dict(valid, ref='refs/heads/other')]):
            with self.subTest(rows=rows), self.assertRaisesRegex(ValueError, 'REMOTE_BASE_MISSING'):
                proof.require_remote_base(self.base_key, query=lambda _: {'actions_caches': rows})
        with patch.object(proof.subprocess, 'run', side_effect=subprocess.TimeoutExpired('gh', 12)), \
                self.assertRaises(subprocess.TimeoutExpired):
            proof.require_remote_base(self.base_key)

    def test_missing_base_after_delta_readback_never_creates_a_publication_proof(self):
        with patch.object(proof, 'decrypt', side_effect=lambda _, path:
                          shutil.copyfile(self.archive, path)), \
                patch.object(proof, 'require_remote_base',
                             side_effect=ValueError('G11_COMPACT_PUBLICATION_REMOTE_BASE_MISSING')), \
                self.assertRaisesRegex(ValueError, 'REMOTE_BASE_MISSING'):
            proof.verify(self.key, 'true')
        self.assertFalse((self.out / 'compact-publication-proof.json').exists())
        self.assertFalse((self.root / 'output').exists())

    @unittest.skipUnless(shutil.which('gpg') or os.environ.get('G11_REQUIRE_ENCRYPTION_TESTS') == '1',
                         'GPG is required')
    def test_real_authenticated_delta_roundtrip_opens_the_same_complete_gate(self):
        probe = subprocess.run(['gpg-agent', '--homedir', os.environ['GNUPGHOME'],
            '--daemon', '/bin/true'], capture_output=True, text=True, check=False)
        if (probe.returncode and 'Operation not permitted' in probe.stderr
                and os.environ.get('G11_REQUIRE_ENCRYPTION_TESTS') != '1'):
            self.skipTest('This executor prohibits the local GPG agent socket; CI requires the real roundtrip')
        self.assertEqual(probe.returncode, 0, probe.stderr)
        with checkpoint.secret_pipe(b'synthetic\n') as fd:
            checkpoint.encrypt_gzip_file(self.archive, self.delta, fd)
        self.ready['cipher_sha256'] = checkpoint.file_sha(self.delta)
        (self.out / 'early-delta-ready.json').write_bytes(checkpoint.canonical(self.ready))
        with patch.object(proof, 'require_remote_base'):
            proof.verify(self.key, 'true')
        self.assertEqual((self.root / 'output').read_text(), 'ready=true\n')


class CompleteRestoreEquivalenceTests(unittest.TestCase):
    def exercise(self, same_day):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source'
            source.mkdir()
            prior = DAY if same_day else '2026-11-26'
            metadata = {'OPERATIONAL_DATE_JST': prior, 'MORNING_COMPLETE': True,
                        'EXISTING': 'retained byte for byte'}
            data = {'relay-state.json': json.dumps(metadata).encode(),
                    'store/manifest.json': b'old manifest', 'store/snapshots/old': b'saved source',
                    'store/models/frozen': b'complete model', 'store/history/old': b'complete history',
                    'rescue/old': b'rescue state',
                    'daily-runtime/' + prior + '/authority.json': json.dumps({
                        'immutable': True, 'operational_date_jst': prior,
                        'authority_sha256': 'a' * 64, 'runtime_source_sha': 'b' * 40}).encode()}
            for name, raw in data.items():
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
            archive = root / 'complete.tgz'
            with tarfile.open(archive, 'w:gz') as stream:
                stream.add(source, arcname='.')
            (root / '.relay-cache').mkdir()
            (root / '.relay-cache/state.tgz.gpg').write_bytes(b'authenticated full ciphertext')
            previous = Path.cwd()
            try:
                os.chdir(root)
                with patch.dict(os.environ, {'RUNNER_TEMP': str(root)}), \
                        patch.object(proof, 'decrypt', side_effect=lambda _, path: shutil.copyfile(archive, path)):
                    proof.restore(DAY, 'g11-state-v4-published-' + prior + '-42-1-checkpoint')
                restored = root / '.relay-state'
                if same_day:
                    self.assertEqual(checkpoint.snapshot_state(restored), checkpoint.snapshot_state(source))
                    receipt = json.loads((root / '.relay-output/authenticated-base.json').read_bytes())
                    self.assertEqual(receipt['inventory_sha256'], proof.digest(checkpoint.snapshot_state(source)))
                else:
                    self.assertEqual(json.loads((restored / 'relay-state.json').read_bytes()), {
                        'SCHEMA': 'G11_PRIVATE_PUBLIC_RELAY_STATE_V1', 'OPERATIONAL_DATE_JST': DAY,
                        'MORNING_COMPLETE': False, 'LAST_PHASE': None, 'LAST_STORE_SHA256': None,
                        'LAST_PUBLISHED_FEED_SHA256': None})
                    self.assertFalse((restored / 'store/snapshots').exists())
                    self.assertFalse((restored / 'store/manifest.json').exists())
                    self.assertFalse((restored / 'rescue').exists())
                    self.assertEqual((restored / 'store/models/frozen').read_bytes(), b'complete model')
                    self.assertEqual((restored / 'store/history/old').read_bytes(), b'complete history')
                    self.assertFalse((root / '.relay-output/authenticated-base.json').exists())
            finally:
                os.chdir(previous)

    def test_complete_same_day_restore_retains_every_byte_and_records_authenticated_base(self):
        self.exercise(True)

    def test_existing_cross_day_reset_retains_history_and_models_without_compact_admission(self):
        self.exercise(False)


class WorkflowIntegrationTests(unittest.TestCase):
    def test_complete_proof_is_opt_in_and_default_full_gate_is_retained(self):
        self.assertIn("default: 'false'", ACTION.split('  allow-compact:\n', 1)[1].split('outputs:', 1)[0])
        gate = step('Verify complete compact publication reconstruction')
        self.assertIn("inputs.allow-compact == 'true'", gate)
        self.assertIn("steps.compact-readback.outputs.cache-hit == 'true'", gate)
        self.assertIn('continue-on-error: true', gate)
        self.assertIn('compact_publication_checkpoint_v1.py verify', gate)
        for name in ('Encrypt the complete state before full backup and canonical publication',
                     'Record encrypted publication checkpoint digest',
                     'Save complete encrypted state before publication',
                     'Read back complete encrypted publication checkpoint',
                     'Verify encrypted publication checkpoint bytes'):
            self.assertIn("steps.compact-verified.outputs.ready != 'true'", step(name))
        self.assertIn('value: ${{ steps.compact-verified.outputs.ready || steps.verified.outputs.ready }}', ACTION)
        self.assertIn('  full-ready:', ACTION)

    def test_fast_scope_requires_frozen_isolated_predeadline_and_full_backup_still_follows(self):
        save = step('Save early encrypted critical checkpoint', WORKFLOW)
        expression = re.search(r'allow-compact: \$\{\{(.+)\}\}', save)[1]
        for frozen in ('true', 'false'):
            for isolated in ('true', 'false'):
                for phase in ('predeadline', 'night', 'morning', 'dry-run'):
                    values = {'steps.authority.outputs.frozen': frozen,
                              'steps.research-isolation.outputs.active': isolated,
                              'steps.due.outputs.phase': phase}
                    code = re.sub(r'steps\.[\w.-]+', lambda m: repr(values[m[0]]), expression)
                    self.assertEqual(eval(code.replace('&&', ' and '), {'__builtins__': {}}),
                                     frozen == isolated == 'true' and phase == 'predeadline')
        encrypt = step('Encrypt changed state before persistence', WORKFLOW)
        full = step('Persist complete state before publication', WORKFLOW)
        self.assertNotIn('steps.critical-save', encrypt)
        self.assertNotIn('allow-compact:', full)
        self.assertIn("steps.encrypt-state.outputs.ready=='true'", full)
        self.assertLess(WORKFLOW.index('id: warm'), WORKFLOW.index('id: encrypt-state'))
        self.assertLess(WORKFLOW.index('id: encrypt-state'), WORKFLOW.index('id: publication-state-verified'))

    def test_frozen_isolated_lane_executes_no_optional_work_before_full_backup(self):
        lane = WORKFLOW.split('      - name: Resume remaining capture slots', 1)[1]
        lane = lane.split('      - name: Encrypt changed state before persistence', 1)[0]
        values = {'steps.due.outputs.phase': 'predeadline', 'steps.authority.outputs.frozen': 'true',
                  'steps.authority.outputs.rescue_bootstrap': 'false',
                  'steps.research-isolation.outputs.active': 'true',
                  'steps.fast-lane.outputs.enabled': 'true', 'steps.critical-save.outputs.ready': 'true',
                  'steps.runtime.outcome': 'skipped', 'steps.deme-source.outcome': 'skipped'}
        count = 0
        for block in lane.split('      - name: ')[1:]:
            lines = block.splitlines()
            for index, line in enumerate(lines):
                if not line.startswith('        if: '):
                    continue
                expression = line.split('if: ', 1)[1]
                if expression == '>-':
                    # Folded conditions end at the next eight-space YAML key.
                    expression = ''
                    for part in lines[index + 1:]:
                        if part.startswith('          '):
                            expression += ' ' + part.strip()
                        else:
                            break
                expression = re.sub(r'(?:steps|env)\.[\w.-]+',
                                    lambda m: repr(values.get(m[0], '')), expression)
                expression = re.sub(r"hashFiles\([^)]*\)", "'present'", expression)
                expression = re.sub(r"contains\([^)]*\)", 'False', expression)
                expression = re.sub(r'!(?!=)', ' not ', expression)
                result = eval(expression.replace('&&', ' and ').replace('||', ' or '),
                              {'__builtins__': {}}, {})
                self.assertFalse(result, lines[0])
                count += 1
                break
        self.assertEqual(count, 17)

    def test_recovery_precedes_capture_and_physical_full_base_identity_is_retained(self):
        names = ('Restore the exact newer compact checkpoint', 'Authenticate the compact recovery manifest',
                 'Restore only encrypted runtime state', 'Require the selected checkpoint to be restored exactly',
                 'Decrypt and validate restored state', 'Replay the newest authenticated compact checkpoint',
                 'Capture and lock dependency-free PREDEADLINE critical lane')
        positions = [WORKFLOW.index('- name: ' + name + '\n') for name in names]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('fail-on-cache-miss: true', step(names[0], WORKFLOW))
        restore = step(names[2], WORKFLOW)
        self.assertIn('steps.compact-recovery.outputs.base_key || steps.checkpoint-choice.outputs.cache_key', restore)
        self.assertIn('--base-key \'${{steps.restore-state.outputs.cache-matched-key}}\'',
                      step(names[5], WORKFLOW))
        self.assertIn('G11_PRIVATE_REPO_TOKEN: ${{secrets.G11_PRIVATE_REPO_TOKEN}}',
                      step(names[5], WORKFLOW))
        chain = step('Keep the predeadline relay alive between delayed cron starts', WORKFLOW)
        self.assertNotIn('steps.critical-save', chain)
        self.assertNotIn('steps.runtime.outcome', chain)
        self.assertIn("steps.critical.outcome=='success'", chain)
        self.assertIn("steps.critical-encrypt.outputs.ready=='true'", chain)


if __name__ == '__main__':
    unittest.main()
