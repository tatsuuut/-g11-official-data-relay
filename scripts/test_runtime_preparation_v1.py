"""Complete byte-preserving restore and public dependency cache boundaries."""
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

import extract_runtime_state_v1 as restore
import public_python_runtime_v1 as runtime


class StateRestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive = self.root / 'state.tgz'
        self.dest = self.root / 'restored'

    def archive_members(self, members):
        with tarfile.open(self.archive, 'w:gz') as handle:
            for member, data in members:
                handle.addfile(member, io.BytesIO(data))

    def member(self, name, data=b'immutable', kind=tarfile.REGTYPE):
        member = tarfile.TarInfo(name)
        member.type = kind
        member.size = len(data) if kind == tarfile.REGTYPE else 0
        return member, data

    def test_every_byte_and_directory_matches_existing_extractor(self):
        source = self.root / 'original'
        source.mkdir()
        files = {'.hidden': b'kept', 'store/lock.json': b'{"before_deadline":true}',
                 'daily-runtime/source/a.py': b'frozen source', 'model.bin': os.urandom(2 ** 20)}
        for name, value in files.items():
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        (source / 'empty').mkdir()
        with tarfile.open(self.archive, 'w:gz') as handle:
            handle.add(source, arcname='.')
        old = self.root / 'old'
        with tarfile.open(self.archive, 'r:gz') as handle:
            handle.getmembers()
            handle.extractall(old, filter='data')
        self.dest.mkdir()
        restore.extract_complete_state(self.archive, self.dest)
        snapshot = lambda p: {str(x.relative_to(p)): (x.read_bytes() if x.is_file() else None)
                              for x in p.rglob('*')}
        self.assertEqual(snapshot(old), snapshot(self.dest))
        self.assertEqual(snapshot(source), snapshot(self.dest))

    def test_late_unsafe_member_leaves_destination_empty(self):
        for name, kind in [('../escape', tarfile.REGTYPE), ('/escape', tarfile.REGTYPE),
                           ('link', tarfile.SYMTYPE), ('hard', tarfile.LNKTYPE),
                           ('device', tarfile.CHRTYPE), ('fifo', tarfile.FIFOTYPE)]:
            with self.subTest(name=name, kind=kind):
                self.archive_members([self.member('good'), self.member(name, kind=kind)])
                with self.assertRaises(ValueError):
                    restore.extract_complete_state(self.archive, self.dest)
                self.assertFalse(self.dest.exists())
                self.assertFalse(list(self.root.glob('.g11-state-extract-*')))

    def test_gzip_crc_failure_after_tar_end_never_exposes_partial_state(self):
        self.archive_members([self.member('good')])
        content = bytearray(self.archive.read_bytes())
        content[-8] ^= 1
        self.archive.write_bytes(content)
        with self.assertRaises((gzip.BadGzipFile, tarfile.ReadError)):
            restore.extract_complete_state(self.archive, self.dest)
        self.assertFalse(self.dest.exists())

    def test_empty_truncated_duplicate_and_conflicting_archives_fail_closed(self):
        for members in [[], [self.member('good'), self.member('./good')],
                        [self.member('parent'), self.member('parent/child')]]:
            self.archive_members(members)
            with self.assertRaises((ValueError, OSError)):
                restore.extract_complete_state(self.archive, self.dest)
            self.assertFalse(self.dest.exists())
        self.archive_members([self.member('large', os.urandom(2 ** 18))])
        self.archive.write_bytes(self.archive.read_bytes()[:1000])
        with self.assertRaises((EOFError, tarfile.ReadError)):
            restore.extract_complete_state(self.archive, self.dest)
        self.assertFalse(self.dest.exists())

    def test_existing_state_and_symlink_are_never_replaced(self):
        self.archive_members([self.member('good')])
        self.dest.mkdir()
        lock = self.dest / 'lock'
        lock.write_bytes(b'existing LOCK')
        with self.assertRaises(ValueError):
            restore.extract_complete_state(self.archive, self.dest)
        self.assertEqual(b'existing LOCK', lock.read_bytes())
        linked = self.root / 'link'
        linked.symlink_to(self.dest)
        with self.assertRaises(ValueError):
            restore.extract_complete_state(self.archive, linked)


class PublicRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'public-env'
        self.pathfile = Path(self.tmp.name) / 'path'
        self.environment = patch.dict(os.environ, {'GITHUB_PATH': str(self.pathfile)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.distributions = [['catboost', '1.2.8'], ['lxml', '6.1.1']]

    def save(self):
        self.root.mkdir(exist_ok=True)
        (self.root / 'g11-public-runtime.json').write_text(json.dumps({
            'identity': runtime.identity(self.root, 'runtime'), 'distributions': self.distributions}))

    def test_verified_hit_does_not_install_or_recreate(self):
        self.save()
        with patch.object(runtime, 'probe', return_value=self.distributions), \
                patch.object(runtime.subprocess, 'run') as install, \
                patch.object(runtime.venv, 'EnvBuilder') as builder:
            self.assertTrue(runtime.prepare(self.root, 'runtime'))
        install.assert_not_called()
        builder.assert_not_called()
        self.assertEqual(str(self.root / 'bin') + '\n', self.pathfile.read_text())

    def test_miss_changed_identity_corruption_or_import_failure_rebuilds(self):
        for condition in ['missing', 'identity', 'corrupt', 'wrong_shape', 'import', 'distribution']:
            with self.subTest(condition=condition):
                self.save()
                manifest = self.root / 'g11-public-runtime.json'
                if condition == 'missing': manifest.unlink()
                if condition == 'identity':
                    value = json.loads(manifest.read_text()); value['identity']['image_version'] = 'wrong'
                    manifest.write_text(json.dumps(value))
                if condition == 'corrupt': manifest.write_text('bad')
                if condition == 'wrong_shape': manifest.write_text('[]')
                results = [self.distributions]
                if condition == 'import': results = [subprocess.CalledProcessError(1, 'probe'), self.distributions]
                if condition == 'distribution': results = [[['catboost', 'wrong']], self.distributions]
                builder = Mock()
                builder.create.side_effect = lambda root: root.mkdir()
                with patch.object(runtime, 'probe', side_effect=results), \
                        patch.object(runtime.venv, 'EnvBuilder', return_value=builder), \
                        patch.object(runtime.subprocess, 'run') as install:
                    self.assertFalse(runtime.prepare(self.root, 'runtime'))
                builder.create.assert_called_once()
                self.assertIn('catboost==1.2.8', install.call_args.args[0])
                self.assertIn('lxml==6.1.1', install.call_args.args[0])

    def test_bad_install_cannot_enable_environment(self):
        builder = Mock()
        builder.create.side_effect = lambda root: root.mkdir()
        with patch.object(runtime.venv, 'EnvBuilder', return_value=builder), \
                patch.object(runtime.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'pip')):
            with self.assertRaises(subprocess.CalledProcessError):
                runtime.prepare(self.root, 'runtime')
        self.assertFalse(self.pathfile.exists())
        self.assertFalse((self.root / 'g11-public-runtime.json').exists())

    def test_key_is_bound_to_profile_runner_image_interpreter_and_absolute_path(self):
        base = runtime.identity(self.root, 'runtime')
        for field in ['root', 'profile', 'requirements', 'python', 'executable_sha256', 'machine', 'image_version']:
            self.assertNotEqual(runtime.fingerprint(base), runtime.fingerprint({**base, field: 'changed'}))
        self.assertNotEqual(runtime.fingerprint(base), runtime.fingerprint(runtime.identity(self.root, 'parser')))

    def test_activating_the_environment_keeps_the_base_interpreter_identity(self):
        before = runtime.identity(self.root, 'runtime')
        with patch.object(runtime.sys, 'executable', str(self.root / 'bin/python3')):
            self.assertEqual(before, runtime.identity(self.root, 'runtime'))


@unittest.skipUnless(os.environ.get('WILD4_WORKFLOW_PATH'), 'tested against the pinned reusable workflow in contract CI')
class CurrentInferenceBoundaryTests(unittest.TestCase):
    def test_current_deployed_model_odds_inference_learning_and_settlement_stay_unchanged(self):
        # Baseline: the actually deployed 1184c7f5 workflow, not the older
        # research branch or the earlier handoff fixture kept by its own tests.
        source = Path(os.environ['WILD4_WORKFLOW_PATH']).read_text()
        protected = {
            'Carry previous-day model registry without copying previous predictions': 'cdfe9943acc632fc699ed539c4becc85db7d140997c9ce1b356fc412e6969ab5',
            'Bind one model for the operational day': '988e96549a566fa5bcbdfce254381445386d56a5e17a02da61743b62ffb49db6',
            'Verify frozen WILD4 inference runtime': '4495877bd12adea8c5d209ed488a85122892ef0858bf8161cda0d4340c6d0202',
            'Check for a matching unexpired official odds packet': '455bd16a042ba59030bff53f69ad844e74b1e57a43352a55eb9c5e2ff117ed7a',
            'Lock future races before deadline without any production write': '4c4fa5c0dec49e4fb47d4322bee731ab7ad8c503d659ee778db5abd13bffb4a6',
            'Settle only immutable locks from verified final NIGHT': '03695c52becd2dcbd700f7dcf8d763442b280356d1ef17fd619aa574923801a3',
            'Continue existing BET learning after completed NIGHT': '797e01d03c64314e9b7a7ba313323e424ed85ffd033e623cb9fefe829534e095'}
        for name, expected in protected.items():
            block = source.split('      - name: ' + name + '\n', 1)[1].split('      - name:', 1)[0]
            if name == 'Settle only immutable locks from verified final NIGHT':
                # The one-day original-LOCK recovery may invoke the identical
                # existing settlement body; only its trigger gains this branch.
                block = block.replace(' && (((github.event_name', ' && ((github.event_name')
                block = block.replace(" || steps.publication-recovery.outputs.changed == 'true')", '')
            with self.subTest(name=name):
                self.assertEqual(expected, hashlib.sha256(block.encode()).hexdigest())


if __name__ == '__main__':
    unittest.main()
