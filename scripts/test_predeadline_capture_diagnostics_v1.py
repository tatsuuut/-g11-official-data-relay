"""Saved-result diagnostics expose classified outcomes, never prediction content."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import predeadline_capture_diagnostics_v1 as diagnostic

DAY = '2026-11-27'
RACE = '20261127-13-04'


def result():
    return {'OPERATIONAL_DATE_JST': DAY,
        'CRITICAL': {'OPERATIONAL_DATE': DAY, 'CRITICAL_CHECKPOINT_STATUS': 'PASS',
            'LAST_TIMELY_SOURCE_COUNT': 0, 'LAST_TIMELY_P3_LIVE_COUNT': 0,
            'LAST_TIMELY_V53_COUNT': 1},
        'ACQUISITION': {'STATUS': 'PARTIAL_MISSED_NOT_PROMOTED', 'ACQUIRED': [],
            'TASK_DEFERRED': [{'RACE_KEY': RACE, 'REASON': 'LIVE_RECOVERY_CLAIM_DEFERRED'}],
            'DEFERRED': {'REASON': 'LIVE_RECOVERY_CLAIM_DEFERRED'},
            'INDEPENDENT_PREVIEWS': {RACE: {'source': 'race.boatcast.jp',
                'snapshot_sha256': 'a' * 64}},
            'STADIUM_MARKETS': {RACE: {'source': 'race.boatcast.jp',
                'status': 'INPUT_UNAVAILABLE', 'reason': 'ALL_ALTERNATE_PROVIDERS_PAUSED'}}},
        'PREDICTION': {'STATUS': 'PREDEADLINE_REFORECAST_PARTIAL_WAITING',
            'WAITING': [{'RACE_KEY': RACE, 'MISSING': ['ODDS3T']}],
            'FAILED': [], 'ABEKEN_V53_FORMAL_COMPLETED': [RACE]},
        'P3_LIVE': {'STATUS': 'PASS', 'READY_RACES': [], 'COMPLETED': [],
            'SKIPPED_DEADLINE': [], 'SKIPPED_TRUE_AI_LIVE': []}}


class DiagnosticTests(unittest.TestCase):
    def test_distinguishes_missing_odds_from_new_v53_and_unknown_wild(self):
        summary = diagnostic.summarize(result(), DAY)
        self.assertEqual(summary['prediction']['waiting']['races'],
            [{'race_key': RACE, 'missing': ['ODDS3T']}])
        self.assertEqual(summary['prediction']['abeken_v53_formal_completed']['races'],
            [{'race_key': RACE}])
        self.assertEqual(summary['last_timely_p3_live_count'], 0)
        self.assertEqual(summary['last_timely_v53_count'], 1)
        self.assertEqual(summary['wild']['status'], 'SEPARATE_WORKER_UNVERIFIED')
        self.assertNotIn('count', summary['wild'])

    def test_true_candidate_skip_retains_only_known_failure_stage(self):
        value = result()
        value['P3_LIVE']['SKIPPED_TRUE_AI_LIVE'] = [{'RACE_KEY': RACE,
            'STATUS': 'MORNING_RETAINED', 'REASON': 'TRUE_AI_LIVE_BEFORE_COMMIT_REJECTED',
            'FAILURE_STAGE': 'LIVE_WEATHER_INFERENCE', 'ERROR_TYPE': 'ValueError',
            'ERROR_REASON': 'LIVE_WEATHER_SOURCE_RECEIPT', 'LIVE_SNAPSHOT_WRITTEN': False,
            'MORNING_PRODUCTION_BETS': ['1-2-3'], 'MORNING_HEAD': 1}]
        row = diagnostic.summarize(value, DAY)['p3_live']['skipped_true_ai_live']['races'][0]
        self.assertEqual(row['failure_stage'], 'LIVE_WEATHER_INFERENCE')
        self.assertEqual(row['error_reason'], 'LIVE_WEATHER_SOURCE_RECEIPT')
        self.assertFalse(row['live_snapshot_written'])
        self.assertNotIn('MORNING_PRODUCTION_BETS', json.dumps(row))

    def test_arbitrary_uppercase_secrets_urls_bets_features_and_nested_fields_never_print(self):
        value = result()
        value['PREDICTION']['FAILED'] = [{'RACE_KEY': RACE, 'REASON': 'SECRET_123',
            'URL': 'https://private.example/?token=abc', 'BETS': ['1-2-3'],
            'MODEL': {'weights': 'hidden-model'}, 'FEATURES': ['hidden-feature']}]
        value['P3_LIVE']['COMPLETED'] = [{'RACE_ID': RACE, 'STATUS': 'https://private.example',
            'P3_SNAPSHOT_WRITE': 'PASS', 'TOKEN': 'SECRET_123', 'PRODUCTION_BETS': ['1-2-3']}]
        value['ACQUISITION']['STADIUM_MARKETS'][RACE]['source'] = 'https://private.example'
        value['ACQUISITION']['STADIUM_MARKETS'][RACE]['reason'] = 'SECRET_123'
        raw = json.dumps(diagnostic.summarize(value, DAY))
        for secret in ('SECRET_123', 'private.example', '1-2-3', 'hidden-model', 'hidden-feature', 'TOKEN'):
            self.assertNotIn(secret, raw)
        self.assertIn('UNCLASSIFIED', raw)

    def test_known_exception_prefix_reports_class_without_arbitrary_suffix(self):
        self.assertEqual(diagnostic.safe_code('CURRENT_P3_SECRET_123'), 'CURRENT_P3_VALIDATION_FAILED')
        self.assertNotIn('SECRET', diagnostic.safe_code('CURRENT_P3_SECRET_123'))
        self.assertEqual(diagnostic.safe_code('ARBITRARY_UPPERCASE_SECRET'), 'UNCLASSIFIED')

    def test_provider_status_and_snapshot_presence_have_closed_labels(self):
        summary = diagnostic.summarize(result(), DAY)
        self.assertEqual(summary['independent_previews'][0]['provider'], 'BOATCAST')
        self.assertTrue(summary['independent_previews'][0]['snapshot_present'])
        self.assertEqual(summary['stadium_markets'][0]['status'], 'INPUT_UNAVAILABLE')
        self.assertEqual(summary['stadium_markets'][0]['reason'], 'ALL_ALTERNATE_PROVIDERS_PAUSED')

    def test_wrong_day_and_invalid_day_are_rejected(self):
        value = result(); value['CRITICAL']['OPERATIONAL_DATE'] = '2026-11-26'
        with self.assertRaisesRegex(ValueError, 'DAY_BINDING'):
            diagnostic.summarize(value, DAY)
        for day in ('2026-11-31', DAY + '\nSECRET_123', 'https://private.example'):
            with self.assertRaises(ValueError):
                diagnostic.summarize(result(), day)

    def test_invalid_or_foreign_race_ids_are_not_emitted(self):
        value = result()
        value['PREDICTION']['WAITING'] += [{'RACE_KEY': key, 'MISSING': ['BEFOREINFO']} for key in
            ('20261126-13-04', '20261127-25-01', '20261127-13-13', 'SECRET_123')]
        races = diagnostic.summarize(value, DAY)['prediction']['waiting']['races']
        self.assertEqual([row['race_key'] for row in races], [RACE])

    def test_malformed_optional_values_are_classified_without_echoing_objects(self):
        value = result()
        value['PREDICTION']['FAILED'] = [{'RACE_KEY': RACE, 'ERROR_TYPE': {'secret': 'SECRET_123'},
            'REASON': ['SECRET_123']}]
        value['ACQUISITION']['STADIUM_MARKETS'][RACE]['source'] = ['SECRET_123']
        self.assertNotIn('SECRET', json.dumps(diagnostic.summarize(value, DAY)))

    def test_cli_is_read_only_and_prints_one_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'result.json'; path.write_text(json.dumps(result()))
            before = path.read_bytes(); output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(diagnostic.main(['--result', str(path), '--day', DAY]), 0)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(Path(temporary).iterdir()), [path])
            lines = output.getvalue().splitlines(); self.assertEqual(len(lines), 1)
            self.assertTrue(lines[0].startswith(diagnostic.PREFIX))
            self.assertEqual(json.loads(lines[0].split('=', 1)[1])['day'], DAY)

    def test_cli_bad_input_is_safe_and_cannot_stop_canonical_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'result.json'; path.write_text('SECRET_123')
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(diagnostic.main(['--result', str(path), '--day', DAY]), 0)
            self.assertNotIn('SECRET', output.getvalue())
            self.assertEqual(json.loads(output.getvalue().split('=', 1)[1])['status'], 'UNAVAILABLE')


if __name__ == '__main__':
    unittest.main()
