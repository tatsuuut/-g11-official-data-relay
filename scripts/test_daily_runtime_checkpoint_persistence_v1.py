"""Exercise the actual pre-publication checkpoint gate without production IO."""
from pathlib import Path
import os
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / '.github/workflows/g11-free-runner.yml').read_text()
ACTION = (ROOT / '.github/actions/g11-publication-checkpoint/action.yml').read_text()


def step(name, source=ACTION):
    return source.split('- name: ' + name + '\n', 1)[1].split('- name:', 1)[0]


def script(name):
    text = step(name).split('      run: |\n', 1)[1]
    return '\n'.join(line[8:] for line in text.splitlines())


class PublicationCheckpointGate(unittest.TestCase):
    def execute(self, cache_hit='true', restored=b'encrypted immutable state'):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.relay-cache').mkdir()
            (root / '.relay-output').mkdir()
            state = root / '.relay-cache/state.tgz.gpg'
            state.write_bytes(b'encrypted immutable state')
            record = step('Record encrypted publication checkpoint digest').split('      run: ', 1)[1].strip()
            subprocess.run(['bash', '-ec', record], cwd=root, check=True, capture_output=True)
            if restored is None:
                state.unlink()
            else:
                state.write_bytes(restored)
            output = root / 'output'
            result = subprocess.run(['bash', '-c', script('Verify encrypted publication checkpoint bytes')],
                cwd=root, env={**os.environ, 'CACHE_HIT': cache_hit, 'GITHUB_OUTPUT': str(output)},
                capture_output=True, text=True)
            return result.returncode, output.read_text() if output.exists() else ''

    def test_verified_round_trip_opens_gate(self):
        self.assertEqual(self.execute(), (0, 'ready=true\n'))

    def test_save_warning_or_prefix_only_hit_never_opens_gate(self):
        for hit in ('', 'false'):
            code, output = self.execute(cache_hit=hit)
            self.assertNotEqual(code, 0)
            self.assertEqual(output, '')

    def test_corruption_or_missing_archive_never_opens_gate(self):
        for restored in (b'older state without the latest LOCK', None):
            code, output = self.execute(restored=restored)
            self.assertNotEqual(code, 0)
            self.assertEqual(output, '')

    def test_checkpoint_is_restorable_after_delivery_cache_failure(self):
        save = step('Save complete encrypted state before publication')
        restore = step('Read back complete encrypted publication checkpoint')
        key = lambda text: re.search(r'^        key: (.+)$', text, re.M).group(1)
        self.assertEqual(key(save), key(restore))
        wrapper = step('Persist complete state before publication', WORKFLOW)
        compact = lambda text: re.sub(r'\s+', '', text)
        self.assertIn('${{github.run_attempt}}-checkpoint', compact(wrapper))
        prefix = 'g11-state-v4-published-${{steps.due.outputs.operational_date}}-'
        self.assertIn(prefix, compact(wrapper))
        self.assertIn(prefix.replace('steps.due.', 'steps.scheduled.'),
                      compact(step('Restore only encrypted runtime state', WORKFLOW)))
        self.assertIn('fail-on-cache-miss: true', restore)
        self.assertNotIn('lookup-only:', restore)
        publish = step('Publish a newly verified feed through OIDC', WORKFLOW)
        self.assertIn("steps.publication-state-verified.outputs.ready=='true'", compact(publish))
        self.assertLess(WORKFLOW.index('name: Persist complete state before publication'),
                        WORKFLOW.index('name: Publish a newly verified feed through OIDC'))
        self.assertIn('cancel-in-progress: false', WORKFLOW)


class ExactReadbackRetryTests(unittest.TestCase):
    """Run deployed shell gates with a cache that becomes visible late."""

    def exercise(self, responses):
        # Only cache IO and sleeps are mocked; the digest/verification shell is real.
        outputs = {}
        calls = []
        iterator = iter(responses)

        def expression(value):
            value = value.removeprefix('${{').removesuffix('}}').strip()
            value = re.sub(r'steps\.([\w-]+)\.outputs\.cache-hit',
                           lambda match: repr(outputs.get(match[1], '')), value)
            return eval(value.replace('||', ' or ').replace('&&', ' and '),
                        {'__builtins__': {}}, {})

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.relay-cache').mkdir()
            (root / '.relay-output').mkdir()
            archive = root / '.relay-cache/state.tgz.gpg'
            original = b'new immutable encrypted LOCK'
            archive.write_bytes(original)
            output = root / 'output'
            for block in ACTION.split('    - name: ')[1:]:
                condition = re.search(r'^      if: (.+)$', block, re.M)
                if condition and not expression(condition[1]):
                    continue
                if 'actions/cache/restore@' in block:
                    self.assertIn('key: ${{ inputs.cache-key }}', block)
                    self.assertIn('fail-on-cache-miss: true', block)
                    self.assertNotIn('restore-keys:', block)
                    name = re.search(r'^      id: (.+)$', block, re.M)[1]
                    hit, restored = next(iterator)
                    calls.append(name)
                    outputs[name] = hit
                    if restored is not None:
                        archive.write_bytes(restored)
                    if hit != 'true':
                        self.assertIn('continue-on-error: true', block)
                    continue
                if 'actions/cache/save@' in block:
                    continue
                if '      run: |\n' in block:
                    code = '\n'.join(line[8:] for line in block.split('      run: |\n', 1)[1].splitlines())
                    hit = re.search(r'^        CACHE_HIT: (.+)$', block, re.M)[1]
                    cache_hit = str(expression(hit)).lower()
                else:
                    code = block.split('      run: ', 1)[1].strip()
                    cache_hit = ''
                result = subprocess.run(['bash', '-c', 'sleep() { :; };\n' + code],
                    cwd=root, env={**os.environ, 'CACHE_HIT': cache_hit, 'GITHUB_OUTPUT': str(output)},
                    text=True, capture_output=True, timeout=5)
                if result.returncode:
                    return False, calls, output.read_text() if output.exists() else ''
            return True, calls, output.read_text()

    def test_first_hit_does_not_wait_or_restore_again(self):
        ok, calls, output = self.exercise([('true', None)])
        self.assertTrue(ok)
        self.assertEqual(calls, ['readback'])
        self.assertEqual(output, 'ready=true\n')

    def test_saved_cache_becomes_visible_on_second_or_third_attempt(self):
        for misses in (1, 2):
            ok, calls, output = self.exercise([('', None)] * misses + [('true', None)])
            self.assertTrue(ok)
            self.assertEqual(len(calls), misses + 1)
            self.assertEqual(output, 'ready=true\n')

    def test_persistent_miss_cannot_publish_even_when_local_bytes_exist(self):
        ok, calls, output = self.exercise([('', None)] * 3)
        self.assertFalse(ok)
        self.assertEqual(len(calls), 3)
        self.assertEqual(output, '')

    def test_retry_cannot_accept_older_or_corrupt_state(self):
        ok, calls, output = self.exercise([('', None), ('true', b'older encrypted LOCK')])
        self.assertFalse(ok)
        self.assertEqual(len(calls), 2)
        self.assertEqual(output, '')

    def test_future_capture_continues_after_readback_failure_but_publication_does_not(self):
        chain = step('Keep the predeadline relay alive between delayed cron starts', WORKFLOW)
        self.assertNotIn('steps.critical-save', chain)
        self.assertIn("steps.critical.outcome=='success'", chain)
        self.assertIn("steps.critical-encrypt.outputs.ready=='true'", chain)
        self.assertIn('!cancelled()', chain)
        self.assertIn("steps.due.outputs.phase=='predeadline'", chain)
        self.assertIn("steps.critical-save.outputs.ready=='true'",
                      step('Publish persisted critical LOCKs', WORKFLOW))
        self.assertIn("steps.critical-save.outcome=='success'",
                      step('Handoff saved early inputs to WILD4', WORKFLOW))


if __name__ == '__main__':
    unittest.main()
