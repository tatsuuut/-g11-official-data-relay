"""Early encrypted input delivery; all dispatches and predictions are mocked."""
import ast
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = Path(os.environ.get('WILD4_WORKFLOW_PATH', '/tmp/g11-wild4-handoff.yml'))
DAY = '2026-10-24'
KEY = 'g11-state-v4-critical-' + DAY + '-123'


def digest_tree(root):
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*')):
        if path.is_file():
            name = path.relative_to(root).as_posix().encode()
            digest.update(len(name).to_bytes(8, 'big'))
            digest.update(name)
            digest.update(path.read_bytes())
    return digest.hexdigest()


class CheckpointBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        store = self.root / 'store'
        store.mkdir()
        (store / 'morning.json').write_text('immutable morning')
        (store / 'odds.json').write_text('verified odds captured before deadline')
        authority = self.root / 'daily-runtime' / DAY / 'authority.json'
        authority.parent.mkdir(parents=True)
        authority.write_text(json.dumps({'operational_date_jst': DAY, 'immutable': True,
                                        'authority_sha256': 'a' * 64, 'runtime_source_sha': 'b' * 40}))
        self.state = {'OPERATIONAL_DATE_JST': DAY, 'MORNING_COMPLETE': True,
                      'CRITICAL': {'SCHEMA': 'G11_PREDEADLINE_CRITICAL_CHECKPOINT_V1',
                                   'CRITICAL_CHECKPOINT_STATUS': 'PASS', 'OPERATIONAL_DATE': DAY,
                                   'LAST_CRITICAL_RUN_ID': '123', 'POST_DEADLINE_GENERATION': 0,
                                   'CRITICAL_CHECKPOINT_SHA': digest_tree(store),
                                   'AUTHORITY_SHA256': 'a' * 64, 'RUNTIME_SOURCE_SHA': 'b' * 40}}
        workflow = WORKFLOW.read_text()
        fragment = workflow[workflow.index('          def verify_early_checkpoint('):
                            workflow.index('          verify_early_checkpoint(pathlib.Path')]
        scope = {'hashlib': hashlib, 'json': json}
        exec(compile(ast.parse(textwrap.dedent(fragment)), '<deployed-checkpoint-guard>', 'exec'), scope)
        self.verify = scope['verify_early_checkpoint']

    def test_same_verified_input_is_read_only_before_publication(self):
        before = {p.relative_to(self.root).as_posix(): p.read_bytes()
                  for p in self.root.rglob('*') if p.is_file()}
        original = copy.deepcopy(self.state)
        self.verify(self.root, self.state, DAY, KEY)
        self.assertEqual(self.state, original)
        self.assertEqual(before, {p.relative_to(self.root).as_posix(): p.read_bytes()
                                 for p in self.root.rglob('*') if p.is_file()})

    def test_wrong_run_day_or_unsafe_state_cannot_fall_back_to_stale_input(self):
        for key in (KEY + '4', KEY.replace(DAY, '2026-10-23'), 'g11-state-v4-published-' + DAY + '-123'):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'BINDING'):
                self.verify(self.root, self.state, DAY, key)
        for field, value in [('CRITICAL_CHECKPOINT_STATUS', 'PARTIAL'), ('POST_DEADLINE_GENERATION', 1),
                             ('OPERATIONAL_DATE', '2026-10-23')]:
            state = copy.deepcopy(self.state); state['CRITICAL'][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'BINDING'):
                self.verify(self.root, state, DAY, KEY)
        self.state['MORNING_COMPLETE'] = False
        with self.assertRaisesRegex(ValueError, 'BINDING'):
            self.verify(self.root, self.state, DAY, KEY)

    def test_modified_saved_input_or_daily_source_is_rejected(self):
        (self.root / 'store/odds.json').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'HASH'):
            self.verify(self.root, self.state, DAY, KEY)
        self.state['CRITICAL']['CRITICAL_CHECKPOINT_SHA'] = digest_tree(self.root / 'store')
        self.state['CRITICAL']['RUNTIME_SOURCE_SHA'] = 'c' * 40
        with self.assertRaisesRegex(ValueError, 'AUTHORITY'):
            self.verify(self.root, self.state, DAY, KEY)

    def test_existing_night_and_rescue_without_early_key_are_unchanged(self):
        self.verify(self.root, {}, DAY, '')


class EarlyDispatchTests(unittest.TestCase):
    def exercise(self, count, dispatch_status=0):
        workflow = (ROOT / '.github/workflows/g11-free-runner.yml').read_text()
        step = workflow.split('        id: wild4-early\n', 1)[1].split(
            '      - name: Keep the predeadline relay alive', 1)[0]
        script = textwrap.dedent(step.split('        run: |\n', 1)[1])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / '.relay-output').mkdir()
            (root / '.relay-output/result.json').write_text(json.dumps({'CRITICAL': {
                'CRITICAL_CHECKPOINT_STATUS': 'PASS', 'LAST_TIMELY_SOURCE_COUNT': count}}))
            output = root / 'output'; calls = root / 'calls'
            env = dict(os.environ, OPERATIONAL_DATE=DAY, GITHUB_RUN_ID='123',
                       GITHUB_OUTPUT=str(output), TEST_CALLS=str(calls),
                       TEST_STATUS=str(dispatch_status))
            stub = 'gh() { printf "%s\\n" "$@" >> "$TEST_CALLS"; return "$TEST_STATUS"; }\n'
            result = subprocess.run(['bash', '-c', stub + script], cwd=root, env=env,
                                    text=True, capture_output=True, timeout=10)
            return result, output.read_text() if output.exists() else '', calls.read_text() if calls.exists() else ''

    def test_new_capture_dispatches_exact_checkpoint_before_optional_work(self):
        result, output, calls = self.exercise(2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('sent=true', output)
        self.assertIn('predeadline_checkpoint_key=' + KEY, calls)
        workflow = (ROOT / '.github/workflows/g11-free-runner.yml').read_text()
        self.assertLess(workflow.index('id: critical-save'), workflow.index('id: wild4-early'))
        self.assertLess(workflow.index('id: wild4-early'), workflow.index('id: predeadline-chain'))

    def test_no_new_input_does_not_create_empty_observer_job(self):
        result, output, calls = self.exercise(0)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls, '')
        self.assertNotIn('sent=true', output)

    def test_failed_dispatch_does_not_claim_handoff_or_retry_ambiguous_post(self):
        result, output, calls = self.exercise(2, 1)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('sent=true', output)
        self.assertEqual(calls.splitlines().count('workflow'), 1)

    def test_normal_and_night_keys_still_use_existing_fallback_contract(self):
        source = WORKFLOW.read_text()
        self.assertIn("inputs.predeadline_checkpoint_key == '' && inputs.night_checkpoint_key == ''", source)
        self.assertIn("inputs.predeadline_checkpoint_key || inputs.night_checkpoint_key || format(", source)
        self.assertIn("steps.canonical-cache.outputs.cache-hit }}' != true", source)


class ProtectedLogicTests(unittest.TestCase):
    def test_model_inference_learning_and_settlement_blocks_are_unchanged(self):
        source = WORKFLOW.read_text()
        expected = {'Carry previous-day model registry without copying previous predictions': 'cdfe9943acc632fc699ed539c4becc85db7d140997c9ce1b356fc412e6969ab5', 'Bind one model for the operational day': '6b21757a328b8f303adf5ed700f8c663dd49e2dfcbfc79ad3769de62a936d67f', 'Verify frozen WILD4 inference runtime': '4495877bd12adea8c5d209ed488a85122892ef0858bf8161cda0d4340c6d0202', 'Check for a matching unexpired official odds packet': 'e3e74afcbc78bcbf78ab82035aab7537b036e466a6ccd819d014f93a4cf110e3', 'Lock future races before deadline without any production write': '297200df10c01e1c6417db3e4f3dbc4a8accb0bfee7c7286c9a86f502eaee8d1', 'Settle only immutable locks from verified final NIGHT': '03695c52becd2dcbd700f7dcf8d763442b280356d1ef17fd619aa574923801a3', 'Continue existing BET learning after completed NIGHT': '797e01d03c64314e9b7a7ba313323e424ed85ffd033e623cb9fefe829534e095'}
        for name, digest in expected.items():
            block = source.split("      - name: " + name + "\n", 1)[1].split("      - name: ", 1)[0]
            with self.subTest(step=name):
                self.assertEqual(hashlib.sha256(block.encode()).hexdigest(), digest)
