"""Recover the last published LOCKs after cancellation before a full backup.

All cache objects are disposable local files. The tests use real authenticated
GPG and the production complete-inventory replay, without race-site requests,
inference, model changes, or production state.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import compact_publication_checkpoint_v1 as publication
import compact_runtime_recovery_v1 as recovery
import encrypt_runtime_state_v1 as checkpoint
from extract_runtime_state_v1 import extract_complete_state


DAY = '2026-11-27'
SECRET = b'local-cancel-recovery-fixture-only\n'
BASE_KEY = 'g11-state-v4-published-' + DAY + '-42-1-checkpoint'
CHECKPOINT_KEY = 'g11-state-v4-critical-' + DAY + '-43'
DELTA_KEY = 'g11-state-v4-delta-' + DAY + '-43'
SOURCE_SHA = 'b' * 40
AUTHORITY_SHA = 'a' * 64


class CancelRecoveryTests(unittest.TestCase):
    """The parent has disappeared: only saved cache bytes may recover its LOCKs."""

    @classmethod
    def setUpClass(cls):
        cls.class_tmp = tempfile.TemporaryDirectory(prefix='g11-cancel-cache-fixture-')
        cls.addClassCleanup(cls.class_tmp.cleanup)
        cls.fixture = Path(cls.class_tmp.name)
        cls.home = cls.fixture / 'gnupg'
        cls.home.mkdir(mode=0o700)
        cls.base = cls.fixture / 'base'
        cls.base.mkdir()
        cls.env = {**os.environ, 'GNUPGHOME': str(cls.home)}
        model = os.urandom(1024 * 1024)
        authority = {'schema': 'G11_DAILY_RUNTIME_AUTHORITY_V1',
            'immutable': True, 'operational_date_jst': DAY,
            'authority_sha256': AUTHORITY_SHA, 'runtime_source_sha': SOURCE_SHA,
            'model_files': {'models/static.bin': hashlib.sha256(model).hexdigest()},
            'source_archive': 'source.tgz'}
        state = {'SCHEMA': 'G11_PRIVATE_PUBLIC_RELAY_STATE_V1',
            'OPERATIONAL_DATE_JST': DAY, 'MORNING_COMPLETE': True}
        data = {'relay-state.json': checkpoint.canonical(state),
            'daily-runtime/' + DAY + '/authority.json': checkpoint.canonical(authority),
            'daily-runtime/' + DAY + '/source.tgz': b'fixed prediction source archive',
            'daily-runtime/' + DAY + '/bindings/p3.json': checkpoint.canonical(
                {'lock_files': {'old-formal-lock.json': hashlib.sha256(
                    b'{"saved":"immutable previous LOCK"}').hexdigest()}}),
            'store/old-formal-lock.json': b'{"saved":"immutable previous LOCK"}',
            'store/snapshots/morning/official-day-input.json': b'{"fixed":"morning input"}',
            'store/source-beforeinfo.html': b'previous authenticated source bytes',
            'store/models/static.bin': model,
            '.hidden-research-state': b'also retained'}
        for name, raw in data.items():
            path = cls.base / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        (cls.base / 'empty-history').mkdir()
        cls.base_cipher = cls.fixture / 'base.tgz.gpg'
        cls.addClassCleanup(subprocess.run,
            ['gpgconf', '--homedir', str(cls.home), '--kill', 'gpg-agent'],
            capture_output=True, check=False)
        probe = subprocess.run(['gpg-agent', '--homedir', str(cls.home), '--daemon', '/bin/true'],
            capture_output=True, check=False, timeout=10)
        if probe.returncode:
            blocked_socket = b"can't create socket: Operation not permitted" in probe.stderr
            if blocked_socket and os.environ.get('G11_REQUIRE_ENCRYPTION_TESTS') != '1':
                raise unittest.SkipTest('local sandbox prohibits GPG agent sockets; mandatory in CI')
            raise AssertionError('authenticated GPG agent is required by the contract gate')
        try:
            with patch.dict(os.environ, cls.env), checkpoint.secret_pipe(SECRET) as fd:
                checkpoint.encrypt(cls.base, cls.base_cipher, passphrase_fd=fd, codec='gzip')
        except (OSError, RuntimeError, ValueError) as exc:
            raise AssertionError('authenticated GPG is required by the contract gate') from exc
        cls.base_inventory = checkpoint.snapshot_state(cls.base)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='g11-cancel-recovery-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / 'target'
        shutil.copytree(self.base, self.target)
        self.gate = {'SCHEMA': 'G11_PREDEADLINE_CRITICAL_CHECKPOINT_V1',
            'CRITICAL_CHECKPOINT_STATUS': 'PASS', 'OPERATIONAL_DATE': DAY,
            'LAST_CRITICAL_RUN_ID': '43', 'POST_DEADLINE_GENERATION': 0,
            'LAST_TIMELY_SOURCE_COUNT': 2,
            'AUTHORITY_SHA256': AUTHORITY_SHA, 'RUNTIME_SOURCE_SHA': SOURCE_SHA}
        state = {'SCHEMA': 'G11_PRIVATE_PUBLIC_RELAY_STATE_V1',
            'OPERATIONAL_DATE_JST': DAY, 'MORNING_COMPLETE': True,
            'CRITICAL': self.gate}
        (self.target / 'relay-state.json').write_bytes(checkpoint.canonical(state))
        (self.target / 'store/source-beforeinfo.html').write_bytes(b'new original source bytes')
        (self.target / 'store/new-formal-lock.json').write_bytes(b'{"saved":"newly published LOCK"}')
        (self.target / 'new-empty-directory').mkdir()
        self.manifest = checkpoint.delta_manifest(self.target, self.base_inventory,
            BASE_KEY, checkpoint.file_sha(self.base_cipher))
        self.delta_archive = self.root / 'delta.tgz'
        checkpoint.write_delta(self.target, self.manifest, self.delta_archive)
        self.delta_cipher = self.root / 'delta.tgz.gpg'
        self.encrypt_delta()
        self.expected = checkpoint.snapshot_state(self.target)

    def encrypt_delta(self):
        self.delta_cipher.unlink(missing_ok=True)
        with patch.dict(os.environ, self.env), checkpoint.secret_pipe(SECRET) as fd:
            checkpoint.encrypt_gzip_file(self.delta_archive, self.delta_cipher, fd)

    def rewrite_manifest(self, mutate):
        """Create an authenticated but semantically invalid saved delta."""
        with tarfile.open(self.delta_archive, 'r:gz') as source:
            items = [(member, source.extractfile(member).read())
                     for member in source.getmembers()]
        manifest = json.loads(items[0][1])
        mutate(manifest)
        items[0] = items[0][0], checkpoint.canonical(manifest)
        with tarfile.open(self.delta_archive, 'w:gz') as output:
            for member, raw in items:
                member.size = len(raw)
                output.addfile(member, io.BytesIO(raw))
        self.encrypt_delta()

    @contextmanager
    def workspace(self, *, base_cipher=None, delta_cipher=None):
        """Only cache readback copies enter a fresh, cancelled-run successor."""
        work = self.root / ('successor-' + str(len(list(self.root.glob('successor-*')))))
        work.mkdir()
        out, cache = work / '.relay-output', work / '.relay-cache'
        out.mkdir(); cache.mkdir()
        shutil.copyfile(base_cipher or self.base_cipher, cache / 'state.tgz.gpg')
        shutil.copyfile(delta_cipher or self.delta_cipher, out / 'early-delta.tgz.gpg')
        temporary = work / 'runner-temp'; temporary.mkdir()
        previous = Path.cwd()
        env = {**self.env, 'G11_PRIVATE_REPO_TOKEN': SECRET.decode().rstrip('\n'),
            'GITHUB_REPOSITORY': 'tatsuuut/-g11-official-data-relay',
            'GITHUB_RUN_ID': '44', 'GITHUB_RUN_ATTEMPT': '1',
            'GITHUB_OUTPUT': str(work / 'github-output'),
            'RUNNER_TEMP': str(temporary), 'GITHUB_WORKSPACE': str(work)}
        try:
            os.chdir(work)
            with patch.dict(os.environ, env):
                yield work
        finally:
            os.chdir(previous)

    @staticmethod
    def cache(key, identifier, created):
        return {'key': key, 'id': identifier,
            'created_at': DAY + 'T' + created + 'Z', 'ref': 'refs/heads/main'}

    def restore_base(self, work):
        cipher = work / '.relay-cache/state.tgz.gpg'
        archive = work / 'runner-temp/base.tgz'
        with checkpoint.secret_pipe(SECRET) as fd:
            result = subprocess.run(['gpg', '--batch', '--yes', '--pinentry-mode', 'loopback',
                '--passphrase-fd', str(fd), '--decrypt', '--output', str(archive), str(cipher)],
                pass_fds=(fd,), capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        extract_complete_state(archive, work / '.relay-state',
            inventory_path=work / '.relay-output/base-inventory.json')

    def inspect(self, *, cache_hit='true', full_key=BASE_KEY, delta_key=DELTA_KEY):
        return recovery.inspect_recovery(DAY, delta_key, cache_hit, full_key)

    def test_cancel_after_compact_publication_recovers_every_published_lock_without_target_full(self):
        caches = [self.cache(BASE_KEY, 1, '12:00:00'), self.cache(DELTA_KEY, 2, '12:01:00')]
        self.assertEqual(recovery.choose_delta_key(DAY, caches, BASE_KEY), DELTA_KEY)
        # The process and all unsaved target bytes are gone. No run43 full cache
        # exists: a new normal job has only the two actual saved encrypted blobs.
        shutil.rmtree(self.target)
        with self.workspace() as work:
            self.inspect()
            self.restore_base(work)
            recovery.replay(DAY, BASE_KEY)
            self.assertEqual(checkpoint.snapshot_state(work / '.relay-state'), self.expected)
            self.assertEqual((work / '.relay-state/store/old-formal-lock.json').read_bytes(),
                (self.base / 'store/old-formal-lock.json').read_bytes())
            self.assertTrue((work / '.relay-state/store/new-formal-lock.json').is_file())
            # Keep the physical full's inventory, not a fictional full of the
            # reconstructed target. The next compact must stay cumulative.
            retained = json.loads((work / '.relay-output/base-inventory.json').read_bytes())
            self.assertEqual(retained['inventory'], self.base_inventory)
            self.assertEqual(checkpoint.file_sha(work / '.relay-cache/state.tgz.gpg'),
                checkpoint.file_sha(self.base_cipher))

    def test_actual_early_publication_proof_then_cancel_then_normal_recovery(self):
        # Exercise the producer's real authenticated base and complete delta
        # proof. Only the GitHub cache metadata lookup is replaced by a local
        # exact-key listing; all encryption, inventories and hashes are real.
        durable = self.root / 'durable-published-delta.gpg'
        with self.workspace() as producer, patch.dict(os.environ, {'GITHUB_RUN_ID': '43'}):
            publication.restore(DAY, BASE_KEY)
            shutil.rmtree(producer / '.relay-state')
            shutil.copytree(self.target, producer / '.relay-state')
            (producer / '.relay-output/result.json').write_bytes(checkpoint.canonical(
                {'OPERATIONAL_DATE_JST': DAY, 'CRITICAL': self.gate}))
            checkpoint.prepare_compact_checkpoint(producer / '.relay-state',
                producer / '.relay-cache/state.tgz.gpg', BASE_KEY, secret=SECRET)
            saved = producer / '.relay-output/early-delta.tgz.gpg'
            self.assertTrue(saved.is_file())
            # Cache save+exact readback. No target full checkpoint is produced.
            shutil.copyfile(saved, durable)
            saved.unlink(); shutil.copyfile(durable, saved)
            actual_subprocess = publication.subprocess.run
            def metadata_only(command, **kwargs):
                if command[:2] == ['gh', 'api']:
                    return subprocess.CompletedProcess(command, 0,
                        stdout=json.dumps({'actions_caches': [
                            self.cache(BASE_KEY, 1, '12:00:00')]}), stderr='')
                return actual_subprocess(command, **kwargs)
            with patch.object(publication.subprocess, 'run', side_effect=metadata_only), \
                    patch.object(publication, 'datetime', wraps=datetime) as clock:
                clock.now.return_value = datetime.fromisoformat(DAY + 'T12:02:00+09:00')
                publication.verify(CHECKPOINT_KEY, 'true')
            proof = json.loads((producer / '.relay-output/compact-publication-proof.json').read_bytes())
            self.assertEqual(proof['target_inventory_sha256'], publication.digest(self.expected))
            self.assertEqual(checkpoint.file_sha(producer / '.relay-cache/state.tgz.gpg'),
                checkpoint.file_sha(self.base_cipher))
        shutil.rmtree(self.target)
        with self.workspace(delta_cipher=durable) as successor:
            self.inspect(); self.restore_base(successor); recovery.replay(DAY, BASE_KEY)
            self.assertEqual(checkpoint.snapshot_state(successor / '.relay-state'), self.expected)

    def test_a_newer_complete_target_supersedes_its_compact_cache(self):
        for full in (CHECKPOINT_KEY, 'g11-state-v4-published-' + DAY + '-43-1-checkpoint',
                     'g11-state-v4-critical-' + DAY + '-44'):
            with self.subTest(full=full):
                caches = [self.cache(BASE_KEY, 1, '12:00:00'), self.cache(DELTA_KEY, 2, '12:01:00'),
                    self.cache(full, 3, '12:02:00')]
                self.assertEqual(recovery.choose_delta_key(DAY, caches, full), '')

    def test_late_older_run_full_cannot_hide_a_later_published_compact(self):
        caches = [self.cache(BASE_KEY, 3, '12:05:00'), self.cache(DELTA_KEY, 2, '12:01:00')]
        self.assertEqual(recovery.choose_delta_key(DAY, caches, BASE_KEY), DELTA_KEY)

    def test_cache_prefix_other_branch_and_other_day_do_not_authorize_replay(self):
        rows = [self.cache(DELTA_KEY + '-unexpected', 10, '12:05:00'),
            dict(self.cache(DELTA_KEY, 11, '12:06:00'), ref='refs/pull/1/merge'),
            self.cache(DELTA_KEY.replace(DAY, '2026-11-26'), 12, '12:07:00')]
        self.assertEqual(recovery.choose_delta_key(DAY, rows, BASE_KEY), '')

    def test_delta_requires_exact_cache_hit_even_when_local_bytes_are_valid(self):
        with self.workspace() as work:
            with self.assertRaises((RuntimeError, ValueError)):
                self.inspect(cache_hit='false')
            self.assertFalse((work / '.relay-output/compact-recovery.json').exists())
            self.assertFalse((work / '.relay-state').exists())

    def test_corrupt_authenticated_cipher_is_rejected_before_base_exposure(self):
        damaged = self.root / 'damaged-delta.gpg'
        damaged.write_bytes(self.delta_cipher.read_bytes()[:-16])
        with self.workspace(delta_cipher=damaged) as work:
            with self.assertRaises((RuntimeError, ValueError)):
                self.inspect()
            self.assertFalse((work / '.relay-output/compact-recovery.json').exists())
            self.assertFalse((work / '.relay-state').exists())

    def test_authenticated_wrong_day_run_or_base_day_cannot_select_a_base(self):
        original = self.delta_archive.read_bytes()
        for kind in ('day', 'run', 'base-day'):
            with self.subTest(kind=kind):
                self.delta_archive.write_bytes(original)
                def change(value):
                    if kind == 'day': value['day'] = '2026-11-26'
                    elif kind == 'run': value['run_id'] = '999'
                    else: value['base_key'] = BASE_KEY.replace(DAY, '2026-11-26')
                self.rewrite_manifest(change)
                with self.workspace() as work:
                    with self.assertRaises((RuntimeError, ValueError)):
                        self.inspect()
                    self.assertFalse((work / '.relay-state').exists())

    def test_missing_or_wrong_physical_base_cannot_mutate_the_old_inventory(self):
        for kind in ('missing', 'cipher', 'inventory'):
            with self.subTest(kind=kind), self.workspace() as work:
                self.inspect()
                self.restore_base(work)
                if kind == 'missing':
                    (work / '.relay-cache/state.tgz.gpg').unlink()
                elif kind == 'cipher':
                    (work / '.relay-cache/state.tgz.gpg').write_bytes(b'other saved full ciphertext')
                else:
                    (work / '.relay-state/store/old-formal-lock.json').write_bytes(b'changed old LOCK')
                before = checkpoint.snapshot_state(work / '.relay-state')
                with self.assertRaises((RuntimeError, ValueError, OSError)):
                    recovery.replay(DAY, BASE_KEY)
                self.assertEqual(checkpoint.snapshot_state(work / '.relay-state'), before)
                self.assertFalse((work / '.relay-state/store/new-formal-lock.json').exists())

    def test_changed_cipher_after_inspection_is_rejected_without_mutating_full(self):
        with self.workspace() as work:
            self.inspect()
            self.restore_base(work)
            original = checkpoint.snapshot_state(work / '.relay-state')
            (work / '.relay-output/early-delta.tgz.gpg').write_bytes(self.delta_cipher.read_bytes()[:-16])
            with self.assertRaises((RuntimeError, ValueError)):
                recovery.replay(DAY, BASE_KEY)
            self.assertEqual(checkpoint.snapshot_state(work / '.relay-state'), original)

    def test_replacement_checksum_failure_never_exposes_a_partial_new_lock(self):
        with tarfile.open(self.delta_archive, 'r:gz') as source:
            items = [(member, source.extractfile(member).read()) for member in source.getmembers()]
        member, raw = items[-1]
        items[-1] = member, b'x' * len(raw)
        with tarfile.open(self.delta_archive, 'w:gz') as output:
            for member, raw in items:
                output.addfile(member, io.BytesIO(raw))
        self.encrypt_delta()
        with self.workspace() as work:
            # Inspection can reject this before full restore; if its manifest
            # inspection is structural, replay must fail atomically instead.
            try:
                self.inspect()
            except (RuntimeError, ValueError):
                self.assertFalse((work / '.relay-state').exists())
                return
            self.restore_base(work)
            before = checkpoint.snapshot_state(work / '.relay-state')
            with self.assertRaises((RuntimeError, ValueError)):
                recovery.replay(DAY, BASE_KEY)
            self.assertEqual(checkpoint.snapshot_state(work / '.relay-state'), before)

    def test_authenticated_bad_authority_runtime_or_protected_lock_is_rejected_atomically(self):
        original_target = self.root / 'original-target'
        shutil.copytree(self.target, original_target)
        for kind in ('authority-file', 'authority-id', 'runtime', 'old-lock', 'model', 'source', 'morning'):
            with self.subTest(kind=kind):
                shutil.rmtree(self.target); shutil.copytree(original_target, self.target)
                if kind == 'authority-file':
                    path = self.target / 'daily-runtime' / DAY / 'authority.json'
                    owner = json.loads(path.read_bytes()); owner['runtime_source_sha'] = 'c' * 40
                    path.write_bytes(checkpoint.canonical(owner))
                elif kind in ('authority-id', 'runtime'):
                    path = self.target / 'relay-state.json'
                    state = json.loads(path.read_bytes())
                    state['CRITICAL']['AUTHORITY_SHA256' if kind == 'authority-id' else 'RUNTIME_SOURCE_SHA'] = (
                        'c' * (64 if kind == 'authority-id' else 40))
                    path.write_bytes(checkpoint.canonical(state))
                else:
                    path = self.target / {'old-lock': 'store/old-formal-lock.json',
                        'model': 'store/models/static.bin',
                        'source': 'daily-runtime/' + DAY + '/source.tgz',
                        'morning': 'store/snapshots/morning/official-day-input.json'}[kind]
                    path.write_bytes(b'changed protected bytes')
                # Simulate a producer bug with valid authentication and a fully
                # self-consistent inventory: recovery must still protect the
                # original day's source/models/LOCKs and authority semantics.
                manifest = copy.deepcopy(self.manifest)
                manifest['target'] = checkpoint.snapshot_state(self.target)
                manifest['changed'] = sorted(name for name, item in manifest['target']['files'].items()
                    if manifest['base']['files'].get(name) != item)
                checkpoint.write_delta(self.target, manifest, self.delta_archive)
                self.encrypt_delta()
                with self.workspace() as work:
                    try:
                        self.inspect()
                    except (RuntimeError, ValueError):
                        self.assertFalse((work / '.relay-state').exists())
                        continue
                    self.restore_base(work)
                    before = checkpoint.snapshot_state(work / '.relay-state')
                    with self.assertRaises((RuntimeError, ValueError)):
                        recovery.replay(DAY, BASE_KEY)
                    self.assertEqual(checkpoint.snapshot_state(work / '.relay-state'), before)

    def test_next_delta_after_recovery_contains_previous_published_lock_from_actual_base(self):
        with self.workspace() as work:
            self.inspect(); self.restore_base(work); recovery.replay(DAY, BASE_KEY)
            restored = work / '.relay-state'
            state = json.loads((restored / 'relay-state.json').read_bytes())
            state['CRITICAL']['LAST_CRITICAL_RUN_ID'] = '44'
            (restored / 'relay-state.json').write_bytes(checkpoint.canonical(state))
            (restored / 'store/next-formal-lock.json').write_bytes(b'newly published run44 LOCK')
            manifest = checkpoint.delta_manifest(restored, self.base_inventory,
                BASE_KEY, checkpoint.file_sha(self.base_cipher))
            self.assertIn('store/new-formal-lock.json', manifest['changed'])
            self.assertIn('store/next-formal-lock.json', manifest['changed'])
            archive = work / 'next-delta.tgz'
            checkpoint.write_delta(restored, manifest, archive)
            next_restore = work / 'next-restored-base'
            shutil.copytree(self.base, next_restore)
            checkpoint.apply_delta(archive, next_restore, day=DAY,
                checkpoint_key='g11-state-v4-critical-' + DAY + '-44',
                base_cipher=self.base_cipher)
            self.assertEqual(checkpoint.snapshot_state(next_restore), checkpoint.snapshot_state(restored))


if __name__ == '__main__':
    unittest.main()
