import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('hot', Path(__file__).with_name('hot_delta_transport_v1.py'))
hot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hot)


class ResearchDeltaTest(unittest.TestCase):
    def setUp(self):
        self.accepted = {'operational_date_jst': '2026-10-13', 'stage': 'PREDEADLINE',
            'source': {'run_id': 10}, 'generated_at_jst': '2026-10-13T08:00:00+09:00',
            'counts': {'races': 1}, 'abeken': {'updated_at_jst': 'unchanged'},
            'races': [{'key': '20261013-01-01', 'p3_snapshot_sha256': 'a'*64,
                       'result_trifecta': None, 'practical_bets': ['1-2-3']}]}
        self.incoming = copy.deepcopy(self.accepted)
        self.incoming.update(source={'run_id': 20}, generated_at_jst='2026-10-13T09:00:00+09:00')
        self.research = {'schema': 'G11_P3_DEME_DISCOVERY_V1', 'available': True,
            'production_mutated': False, 'updated_at': '2026-10-13T09:01:00+09:00',
            'counts': {'discovery': 0, 'validating': 0, 'review': 0}, 'candidates': []}

    def test_metadata_only_excludes_production_fields(self):
        before = copy.deepcopy((self.incoming, self.accepted))
        delta = hot.create_delta(self.incoming, self.accepted, 'b'*64, self.research)
        self.assertEqual(delta['RACES'], [])
        self.assertEqual(delta['TOP_LEVEL'], {'deme_discovery': self.research})
        self.assertEqual((self.incoming, self.accepted), before)

    def test_missing_or_identical_projection_is_noop(self):
        self.assertEqual(hot.create_delta(self.incoming, self.accepted, 'b'*64)['TOP_LEVEL'], {})
        self.accepted['deme_discovery'] = self.research
        self.assertEqual(hot.create_delta(self.incoming, self.accepted, 'b'*64, self.research)['TOP_LEVEL'], {})

    def test_normal_race_delta_retains_existing_transport(self):
        self.incoming['races'][0]['odds_captured_at_jst'] = '2026-10-13T09:00:00+09:00'
        delta = hot.create_delta(self.incoming, self.accepted, 'b'*64, self.research)
        self.assertEqual(len(delta['RACES']), 1)
        self.assertEqual(delta['TOP_LEVEL']['source'], self.incoming['source'])
        self.assertEqual(delta['TOP_LEVEL']['deme_discovery'], self.research)

    def _publish(self, mutated=False, same=False):
        if same:
            self.accepted['deme_discovery'] = self.research
        readback = copy.deepcopy(self.accepted)
        readback['deme_discovery'] = self.research
        readback['transport'] = {'latest_sequence': 20001}
        if mutated:
            readback['generated_at_jst'] = self.incoming['generated_at_jst']
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp)
                Path('.relay-output').mkdir()
                Path('incoming.json').write_text(json.dumps(self.incoming))
                Path('.relay-output/hot-delta-response.json').write_text(json.dumps({'status': 'PASS', 'payload_sha256': 'c'*64}))
                reads = [(self.accepted, {'x-g11-source-feed-sha256': 'b'*64}, b''),
                         (readback, {'x-g11-source-feed-sha256': 'c'*64}, hot.compact(readback))]
                with patch.object(hot, 'fetch', side_effect=reads), patch.object(hot.subprocess, 'run') as run, patch.dict(os.environ, {'GITHUB_OUTPUT': 'output', 'G11_SYNC_OIDC': 'test-only'}):
                    run.return_value.stdout = '200'
                    hot.publish(Path('incoming.json'), 'https://example.invalid', self.research)
                    self.assertEqual(run.call_count, 0 if same else 1)
            finally:
                os.chdir(original_cwd)

    def test_published_projection_readback_preserves_production(self):
        self._publish()
        with self.assertRaisesRegex(RuntimeError, 'RESEARCH_PRODUCTION_CHANGED'):
            self._publish(mutated=True)

    def test_identical_projection_does_not_post(self):
        self._publish(same=True)


if __name__ == '__main__':
    unittest.main()
