"""Retain an authenticated prior prediction even when the rolling input advances."""
import ast
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import textwrap
import unittest


def sha(value):
    return hashlib.sha256((json.dumps(value, sort_keys=True, separators=(',', ':'))+'\n').encode()).hexdigest()


class RetainedLockTests(unittest.TestCase):
    def setUp(self):
        path = Path(os.environ.get('WILD4_WORKFLOW_PATH', Path(__file__).resolve().parents[1] / '.github/workflows/g11-wild4-shadow-lock-only.yml'))
        source = path.read_text()
        start = source.index('          def retain_existing_lock(')
        end = source.index('          considered=expired=', start)
        ns = {'datetime': datetime, 're': re}
        exec(compile(ast.parse(textwrap.dedent(source[start:end])), str(path), 'exec'), ns)
        self.retain = ns['retain_existing_lock']
        self.binding = {'REGISTRY_SHA256': 'a'*64, 'MODEL_SHA256': 'b'*64}
        self.row = {'STANDARD_RACE_KEY': '20261031-14-08', 'BET_PACKET_HASH': 'c'*64,
                    'UPSTREAM_PACKET': {'RACE_DEADLINE_AT': '2026-10-31T12:00:00+09:00'}}
        self.lock = {'CONTENT_SHA256': '', 'STANDARD_RACE_KEY': self.row['STANDARD_RACE_KEY'],
                     'STATUS': 'LOCKED_BEFORE_RESULT', 'CANDIDATE_REGISTRY_SHA256': 'a'*64,
                     'CANDIDATE_MODEL_SHA256': 'b'*64, 'INPUT_PACKET_SHA256': 'c'*64,
                     'ODDS_SNAPSHOT_SHA256': 'd'*64, 'LOCKED_AT': '2026-10-31T11:54:00+09:00',
                     'RACE_DEADLINE_AT': self.row['UPSTREAM_PACKET']['RACE_DEADLINE_AT'],
                     'PRODUCTION_BETS_CHANGED': False, 'OFFICIAL_RESULT_USED': False,
                     'SELECTED_BETS': ['1-2-3']}
        self.seal()

    def seal(self):
        self.lock['CONTENT_SHA256'] = sha({k:v for k,v in self.lock.items() if k!='CONTENT_SHA256'})

    def test_same_input_is_idempotent(self):
        before = deepcopy(self.lock)
        self.assertFalse(self.retain(self.lock, self.row, self.binding, sha))
        self.assertEqual(self.lock, before)

    def test_newer_input_keeps_original_lock_bytes_and_tickets(self):
        self.row['BET_PACKET_HASH'] = 'e'*64
        before = json.dumps(self.lock)
        self.assertTrue(self.retain(self.lock, self.row, self.binding, sha))
        self.assertEqual(json.dumps(self.lock), before)

    def test_tampered_content_is_rejected(self):
        self.lock['SELECTED_BETS'] = ['6-5-4']
        with self.assertRaisesRegex(RuntimeError, 'content'):
            self.retain(self.lock, self.row, self.binding, sha)

    def test_model_race_time_and_safety_bindings_remain_fail_closed(self):
        cases = [('CANDIDATE_MODEL_SHA256','e'*64), ('CANDIDATE_REGISTRY_SHA256','e'*64),
                 ('STANDARD_RACE_KEY','20261031-14-09'), ('LOCKED_AT','2026-10-31T12:00:00+09:00'),
                 ('RACE_DEADLINE_AT','2026-10-31T12:01:00+09:00'), ('INPUT_PACKET_SHA256','bad'),
                 ('ODDS_SNAPSHOT_SHA256','bad'), ('PRODUCTION_BETS_CHANGED',True),
                 ('OFFICIAL_RESULT_USED',True), ('STATUS','OTHER')]
        base = deepcopy(self.lock)
        for field,value in cases:
            with self.subTest(field=field):
                self.lock = {**base, field:value}; self.seal()
                with self.assertRaises(RuntimeError):
                    self.retain(self.lock, self.row, self.binding, sha)


if __name__ == '__main__':
    unittest.main()
