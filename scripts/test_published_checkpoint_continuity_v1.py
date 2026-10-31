"""Regression for restoring the newest persisted daily state."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from critical_saved_publication_v1 import newest_checkpoint_key, select_restore


class RestoreTests(unittest.TestCase):
    def cache(self, lane, number, created, **extra):
        return {'id': number, 'key': f'g11-state-v4-{lane}-2026-10-31-{number}',
                'ref': 'refs/heads/main', 'created_at': created, **extra}

    def test_newer_critical_beats_older_full_even_if_full_was_accessed_later(self):
        old = self.cache('published', 10, '2026-10-31T02:10:00Z', last_accessed_at='2026-10-31T03:00:00Z')
        fresh = self.cache('critical', 11, '2026-10-31T02:19:00Z')
        self.assertEqual(newest_checkpoint_key('2026-10-31', [old, fresh]), fresh['key'])

    def test_newer_full_beats_older_critical(self):
        old = self.cache('critical', 11, '2026-10-31T02:19:00Z')
        fresh = self.cache('published', 11, '2026-10-31T02:22:00Z', key='g11-state-v4-published-2026-10-31-11-1-checkpoint')
        self.assertEqual(newest_checkpoint_key('2026-10-31', [fresh, old]), fresh['key'])

    def test_other_day_branch_and_unrecognized_keys_are_not_selected(self):
        rows = [self.cache('critical', 12, '2026-10-31T03:00:00Z', ref='refs/heads/other'),
                self.cache('published', 13, '2026-10-31T03:00:00Z', key='g11-state-v4-published-2026-10-30-13'),
                self.cache('published', 14, '2026-10-31T03:00:00Z', key='g11-state-v4-published-2026-10-31-14-unverified')]
        self.assertEqual(newest_checkpoint_key('2026-10-31', rows), '')

    def test_next_day_never_reuses_previous_day_selection(self):
        yesterday = self.cache('critical', 10, '2026-10-31T14:00:00Z')
        self.assertEqual(newest_checkpoint_key('2026-11-01', [yesterday]), '')
        today = self.cache('critical', 11, '2026-11-01T00:00:00Z',
                           key='g11-state-v4-critical-2026-11-01-11')
        self.assertEqual(newest_checkpoint_key('2026-11-01', [today, yesterday]), today['key'])

    def test_metadata_selection_queries_both_lanes_and_exports_exact_key(self):
        latest = self.cache('critical', 11, '2026-10-31T02:19:00Z')
        older = self.cache('published', 10, '2026-10-31T02:10:00Z')
        responses = [SimpleNamespace(stdout=json.dumps({'actions_caches': [row]}))
                     for row in (latest, older)]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'outputs'
            with patch.dict(os.environ, GITHUB_REPOSITORY='owner/relay', GITHUB_OUTPUT=str(output)), \
                    patch('subprocess.run', side_effect=responses) as request:
                select_restore('2026-10-31')
            self.assertEqual(output.read_text(), 'cache_key=' + latest['key'] + '\n')
        urls = [call.args[0][2] for call in request.call_args_list]
        self.assertIn('key=g11-state-v4-critical-2026-10-31-', urls[0])
        self.assertIn('key=g11-state-v4-published-2026-10-31-', urls[1])
        for url in urls:
            self.assertIn('ref=refs%2Fheads%2Fmain', url)
            self.assertIn('sort=created_at&direction=desc', url)

    def test_metadata_failure_does_not_select_an_older_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'outputs'
            with patch.dict(os.environ, GITHUB_REPOSITORY='owner/relay', GITHUB_OUTPUT=str(output)), \
                    patch('subprocess.run', side_effect=subprocess.CalledProcessError(1, ['gh'])), \
                    self.assertRaises(subprocess.CalledProcessError):
                select_restore('2026-10-31')
            self.assertFalse(output.exists())


if __name__ == "__main__": unittest.main()
