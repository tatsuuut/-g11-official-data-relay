"""Regression for Nov14's 1,733,793-byte saved MORNING publication."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

import hot_delta_transport_v1 as hot


class MorningBudgetTests(unittest.TestCase):
    def feed(self, size):
        data = {'schema': 'G11_PUBLIC_APP_FEED_V2', 'status': 'PASS',
                'stage': 'MORNING', 'operational_date_jst': '2026-11-14',
                'counts': {'races': 168, 'formal': 168}, 'races': []}
        for n in range(168):
            data['races'].append({'key': f'20261114-{n // 12 + 1:02d}-{n % 12 + 1:02d}',
                'practical_bets': ['1-2-3', '1-3-2'], 'production_points': 2,
                'p3_snapshot_sha256': 'a' * 64, 'p3_morning_snapshot_sha256': 'a' * 64,
                'p3_published_at_jst': '2026-11-14T03:42:00+09:00',
                'abeken_shadow': {'v53': {'morning': {'snapshot_sha256': 'b' * 64,
                    'practical_bets': ['2-1-3'], 'buy_count': 1}}}})
        data['races'][0]['prediction'] = 'x' * (size - len(hot.compact(data)) - len(',"prediction":""'))
        return data

    def test_observed_size_retains_all_original_tickets_and_proof(self):
        feed = self.feed(1_733_793)
        before = copy.deepcopy(feed)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'feed.json'
            raw = hot.compact(feed)
            path.write_bytes(raw)
            hot.project_morning_base(path)
            result = json.loads(path.read_bytes())
            self.assertEqual(result.pop('hot_reference'), {
                'schema': 'G11_SITE_HOT_BASE_V1', 'source_feed_sha256': hot.hash_bytes(raw),
                'status': 'COLD_RESEARCH_RETAINED'})
            self.assertEqual(result, before)
            self.assertLessEqual(path.stat().st_size, 2_100_000)

    def test_budget_still_rejects_oversize_without_touching_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'feed.json'
            raw = hot.compact(self.feed(2_100_001))
            path.write_bytes(raw)
            with self.assertRaisesRegex(RuntimeError, 'HOT_BASE_OVER_BUDGET'):
                hot.project_morning_base(path)
            self.assertEqual(path.read_bytes(), raw)
        self.assertLess(hot.BASE_BUDGET, 2_100_000)


if __name__ == '__main__':
    unittest.main()
