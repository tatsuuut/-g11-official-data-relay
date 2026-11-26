"""Exercise the optional alternate-input routing in the current public worker.

All fixtures are local. Source acquisition and model inference are not called.
"""
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
import textwrap
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / '.github/workflows/g11-wild4-shadow-lock-only.yml'
DAY = '2026-11-27'
ADMISSION = 'VERIFIED_ALTERNATE_LIVE_RECEIPT_EXACT_120_V2'


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class AlternateWorkerTests(unittest.TestCase):
    def setUp(self):
        self.source = WORKER.read_text()

    def test_verifier_is_isolated_pinned_and_future_day_only(self):
        start = self.source.index('      - name: Detect retained live alternate market quotes\n')
        end = self.source.index('      - name: Check for a matching unexpired official odds packet\n', start)
        section = self.source[start:end]
        self.assertIn("steps.day.outputs.day >= '2026-11-27'", section)
        self.assertIn("steps.decrypted.outputs.canonical_ready == 'true'", section)
        pin = re.search(r'          ref: ([0-9a-f]{40})\n', section)[1]
        self.assertIn("rev-parse HEAD)\" = '" + pin + "'", section)
        self.assertIn('persist-credentials: false', section)
        self.assertIn('path: .g11-alternate-input', section)
        self.assertIn('verified_wild_alternate_odds_v1.py', section)
        self.assertNotIn('working-directory: .g11-private', section)
        self.assertNotIn('curl ', section)
        self.assertNotIn('urllib', section)

    def test_frozen_model_lock_settlement_and_learning_steps_are_byte_identical(self):
        expected = {
            'Carry previous-day model registry without copying previous predictions': '08b4d4c9cec584c0d4328fd758cf20c2c1e1758613239bfa836cd5cc94689f02',
            'Bind one model for the operational day': '0aafa72c74beb10949c98a8f77fa14b1c423076b247d3ade529658bcec56fa98',
            'Verify frozen WILD4 inference runtime': '4495877bd12adea8c5d209ed488a85122892ef0858bf8161cda0d4340c6d0202',
            'Lock future races before deadline without any production write': '4c4fa5c0dec49e4fb47d4322bee731ab7ad8c503d659ee778db5abd13bffb4a6',
            'Settle only immutable locks from verified final NIGHT': '80f4df9e28fada9f0fa810948d3edc943060da57e0e2a52bdd410c08147b7bd4',
            'Continue existing BET learning after completed NIGHT': '13d0c5f33151da4f614e685da3d962974730dcf62bc03a73a47efe980aa07b6d',
        }
        for name, expected_sha in expected.items():
            block = self.source.split('      - name: ' + name + '\n', 1)[1].split('      - name: ', 1)[0]
            with self.subTest(step=name):
                self.assertEqual(sha256(block.encode()).hexdigest(), expected_sha)
        start = self.source.index('          def frozen_source_context_readback(')
        end = self.source.index('      - name: Lock future races', start)
        self.assertEqual(sha256(self.source[start:end].encode()).hexdigest(),
                         'f19be16cb99ea6cc43f19c491fd7d073257bd5bdcec3ffd91140e73cc70697db')

    def merge(self, ordinary, alternate):
        start = self.source.index("          alternate_path=pathlib.Path(os.environ['RUNNER_TEMP'])/'alternate-wild4-odds.json'\n")
        end = self.source.index("          print('WILD4_VERIFIED_INPUT_UNION='", start)
        code = compile(textwrap.dedent(self.source[start:end]), str(WORKER), 'exec')
        calls = []

        def verified(left, right, day):
            # The private merger verifies source/packet seals separately. This
            # boundary test checks the routing and precedence given its result.
            self.assertEqual(day, DAY)
            calls.append((deepcopy(left), deepcopy(right)))
            return deepcopy(right) if left is None else dict(left, RECORDS=left['RECORDS'] + right['RECORDS'])

        with tempfile.TemporaryDirectory() as tmp:
            if alternate is not None:
                (Path(tmp) / 'alternate-wild4-odds.json').write_text(json.dumps(alternate))
            scope = {'os': os, 'pathlib': __import__('pathlib'), 'day': DAY,
                     'chosen': deepcopy(ordinary), 'merge_verified_odds': verified,
                     'train': SimpleNamespace(daily_sha=digest)}
            with patch.dict(os.environ, {'RUNNER_TEMP': tmp}):
                exec(code, scope)
            return scope['chosen'], calls

    def alternate(self, keys):
        return {'INPUT_ADMISSION': ADMISSION, 'ORDINARY_TRAINING_STORE_MUTATED': False,
                'PRODUCTION_MUTATED': False, 'UPSTREAM_RACE_COUNT': 3,
                'RECORDS': [{'STANDARD_RACE_KEY': k, 'ODDS_SOURCE_ADMISSION': ADMISSION,
                             'UPSTREAM_PACKET': {'saved': True}, 'fixture': 'alternate'} for k in keys]}

    def test_ordinary_packets_win_and_new_alternate_packets_are_merged(self):
        first, second = '20261127-01-01', '20261127-01-02'
        ordinary = {'RECORDS': [{'STANDARD_RACE_KEY': first, 'fixture': 'ordinary'}]}
        alternate = self.alternate([first, second])
        before = deepcopy((ordinary, alternate))
        merged, calls = self.merge(ordinary, alternate)
        self.assertEqual((ordinary, alternate), before)
        self.assertEqual(merged['RECORDS'], ordinary['RECORDS'] + alternate['RECORDS'][1:])
        self.assertEqual(calls[-1][1]['ODDS_PACKET_RACE_COUNT'], 1)
        self.assertEqual(calls[-1][1]['ODDS_MISSING_RACE_COUNT'], 2)
        sealed = calls[-1][1]
        self.assertEqual(sealed['CONTENT_SHA256'], digest({k:v for k,v in sealed.items() if k != 'CONTENT_SHA256'}))

    def test_verifier_admission_v2_preserves_each_original_source(self):
        alternate = self.alternate(['20261127-01-01', '20261127-24-01'])
        urls = ['https://race.boatcast.jp/txt/01/bc_smt_od3_20261127_01_01.txt',
                'https://www.omurakyotei.jp/yosou/sp/odds/?day=20261127&race=01']
        for row, url in zip(alternate['RECORDS'], urls, strict=True):
            row['ODDS_SOURCE'] = url
        merged, _ = self.merge(None, alternate)
        self.assertEqual([r['ODDS_SOURCE'] for r in merged['RECORDS']], urls)
        self.assertTrue(all(r['ODDS_SOURCE_ADMISSION'] == ADMISSION for r in merged['RECORDS']))

    def test_legacy_v1_envelope_and_packet_admission_are_rejected(self):
        old_admission = 'BOATCAST_LIVE_RECEIPT_EXACT_120_V1'
        for field in ('INPUT_ADMISSION', 'ODDS_SOURCE_ADMISSION'):
            alternate = self.alternate(['20261127-01-01'])
            if field == 'INPUT_ADMISSION':
                alternate[field] = old_admission
            else:
                alternate['RECORDS'][0][field] = old_admission
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'BOUNDARY'):
                self.merge(None, alternate)

    def test_missing_alternate_is_noop_and_wrong_admission_is_rejected(self):
        ordinary = {'RECORDS': [{'STANDARD_RACE_KEY': '20261127-01-01'}]}
        merged, calls = self.merge(ordinary, None)
        self.assertEqual(merged, ordinary)
        self.assertEqual(calls, [])
        for mutate in ('INPUT_ADMISSION', 'PRODUCTION_MUTATED', 'row'):
            alternate = self.alternate(['20261127-01-02'])
            if mutate == 'row':
                alternate['RECORDS'][0]['ODDS_SOURCE_ADMISSION'] = 'unknown'
            elif mutate == 'PRODUCTION_MUTATED':
                alternate[mutate] = True
            else:
                alternate[mutate] = 'unknown'
            with self.subTest(field=mutate), self.assertRaisesRegex(ValueError, 'BOUNDARY'):
                self.merge(ordinary, alternate)


if __name__ == '__main__':
    unittest.main()
