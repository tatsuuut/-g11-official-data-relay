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
        self.assertIn('${{ github.run_attempt }}-checkpoint', wrapper)
        prefix = 'g11-state-v4-published-${{ steps.due.outputs.operational_date }}-'
        self.assertIn(prefix, wrapper)
        self.assertIn(prefix, step('Restore only encrypted runtime state', WORKFLOW))
        self.assertIn('fail-on-cache-miss: true', restore)
        self.assertNotIn('lookup-only:', restore)
        publish = step('Publish a newly verified feed through OIDC', WORKFLOW)
        self.assertIn("steps.publication-state-verified.outputs.ready == 'true'", publish)
        self.assertLess(WORKFLOW.index('name: Persist complete state before publication'),
                        WORKFLOW.index('name: Publish a newly verified feed through OIDC'))
        self.assertIn('cancel-in-progress: false', WORKFLOW)


if __name__ == '__main__':
    unittest.main()
