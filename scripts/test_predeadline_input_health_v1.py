from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
from contextlib import redirect_stdout
import io
import tempfile
import unittest
from unittest.mock import Mock, patch

import predeadline_input_health_v1 as health

DAY = '2026-11-17'
KEY = '20261117-03-01'
SID = 'beforeinfo-' + KEY
URL = 'https://www.boatrace.jp/owpc/pc/race/beforeinfo?rno=1&jcd=03&hd=20261117'
NOW = datetime.fromisoformat(DAY + 'T16:08:00+09:00')


def page():
    headers = ['<th>レース</th>']
    for race in range(1, 13):
        headers.append(f'<th><a href="/owpc/pc/race/beforeinfo?rno={race}&amp;jcd=03&amp;hd=20261117">{race}R</a></th>')
    clocks = ['11:14', '11:38', '12:02', '12:26', '12:50', '13:14',
              '13:38', '14:03', '14:30', '14:59', '15:29', '16:00']
    return ('<table><thead><tr>' + ''.join(headers) + '</tr></thead><tbody><tr>' +
            '<td colspan="2">締切予定時刻</td>' + ''.join('<td>' + t + '</td>' for t in clocks) +
            '</tr></tbody></table>').encode()


class Artifact:
    def __init__(self, root):
        self.root = root
        self.plan = [{'SOURCE_ID': SID, 'RACE_KEY': KEY, 'KIND': 'BEFOREINFO', 'URL': URL,
                      'MUST_FINISH_BEFORE_JST': DAY + 'T10:59:00+09:00'}]

    def load_verified_snapshot(self, stage, name):
        assert (stage, name) == ('morning', 'official-day-input')
        return {'SNAPSHOT': {'PAYLOAD': {'OPERATIONAL_DATE_JST': DAY, 'PREDEADLINE_PLAN': self.plan}}}

    def rejected_response_receipt(self, sid, request):
        path = self.root / 'rejected-responses' / sid / request
        r = json.loads((path / 'receipt.json').read_bytes())
        if hashlib.sha256((path / 'response.bin').read_bytes()).hexdigest() != r['SHA256']:
            raise ValueError('REJECTED_RESPONSE_READBACK')
        return r


class HealthTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        self.artifact = Artifact(self.root)
        self.rejected = self.root / 'rejected-responses' / SID / ('request-' + 'a' * 32)
        self.rejected.mkdir(parents=True)
        self.rejected.joinpath('response.bin').write_bytes(page())
        self.receipt = {'URL': URL, 'CAPTURED_AT_JST': DAY + 'T10:52:35+09:00',
                        'SHA256': hashlib.sha256(page()).hexdigest(), 'REJECTION_REASON': 'HTML_NUMBER_PARSE'}
        self.save_receipt()

    def save_receipt(self):
        (self.rejected / 'receipt.json').write_text(json.dumps(self.receipt))

    def summary(self, value=None):
        return health.summarize(self.artifact, DAY, value or {'MISSED': [SID]}, now=NOW)

    def test_saved_rejection_proves_schedule_drift_without_rewriting_or_prediction(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = self.summary()
        issue = result['issues'][0]
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual((result['race_issue_count'], result['source_issue_count']), (1, 1))
        self.assertEqual(issue['official_deadline_jst'], DAY + 'T11:14:00+09:00')
        self.assertTrue(issue['observed_before_saved_cutoff'])
        self.assertEqual(issue['rejection_reason'], 'HTML_NUMBER_PARSE')
        self.assertFalse(result['prediction_boundary_changed'])
        self.assertFalse(result['prediction_generated'])
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_successful_checkpoint_is_not_a_claim_of_input_coverage(self):
        self.assertEqual(health.summarize(self.artifact, DAY, {}, now=NOW)['status'], 'PASS')
        deferred = {'REJECTED': [{'SOURCE_ID': SID, 'RACE_KEY': KEY, 'REASON': 'HTML_NUMBER_PARSE'}]}
        self.assertEqual(self.summary(deferred)['status'], 'DEFERRED')
        deferred['MISSED'] = [SID]
        value = self.summary(deferred)
        self.assertEqual(value['status'], 'PARTIAL')
        self.assertEqual(value['source_issue_count'], 1)
        self.assertEqual(value['scope'], 'OFFICIAL_INPUTS_ONLY_NOT_ENGINE_OR_DEVICE_COMPLETENESS')

    def test_verified_official_withdrawal_is_separate_from_input_failure(self):
        item = {'SOURCE_ID': SID, 'RACE_KEY': KEY,
                'REASON': 'OFFICIAL_WITHDRAWAL_UNSUPPORTED_FIELD_SIZE',
                'AVAILABILITY_SNAPSHOT_SHA256': 'a' * 64}
        result = self.summary({'UNAVAILABLE': [item]})
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['official_unavailable_race_count'], 1)
        self.assertEqual(result['source_issue_count'], 0)
        del item['AVAILABILITY_SNAPSHOT_SHA256']
        self.assertEqual(self.summary({'UNAVAILABLE': [item]})['status'], 'PARTIAL')

    def test_unknown_reason_is_not_echoed_into_public_logs(self):
        self.receipt['REJECTION_REASON'] = 'private-content\n::error::secret'
        self.save_receipt()
        self.assertEqual(self.summary()['issues'][0]['rejection_reason'], 'UNCLASSIFIED_INPUT_FAILURE')
        self.assertNotIn('secret', json.dumps(self.summary()))

    def test_bad_evidence_does_not_become_schedule_proof(self):
        self.rejected.joinpath('response.bin').write_bytes(b'corrupt')
        issue = self.summary()['issues'][0]
        self.assertEqual(issue['evidence_status'], 'UNVERIFIED')
        self.assertNotIn('official_deadline_jst', issue)

    def test_evidence_from_another_day_future_time_or_url_is_rejected(self):
        for field, value in [('CAPTURED_AT_JST', '2026-11-16T10:50:00+09:00'),
                             ('CAPTURED_AT_JST', DAY + 'T17:00:00+09:00'),
                             ('URL', URL.replace('jcd=03', 'jcd=04'))]:
            original = self.receipt[field]
            self.receipt[field] = value
            self.save_receipt()
            issue = self.summary()['issues'][0]
            self.assertEqual(issue['evidence_status'], 'UNVERIFIED')
            self.assertNotIn('official_deadline_jst', issue)
            self.receipt[field] = original

    def test_changed_schedule_does_not_turn_missing_input_into_success(self):
        receipt_time = DAY + 'T11:05:00+09:00'
        self.receipt['CAPTURED_AT_JST'] = receipt_time
        self.save_receipt()
        value = self.summary()
        self.assertEqual(value['status'], 'PARTIAL')
        self.assertFalse(value['issues'][0]['observed_before_saved_cutoff'])

    def test_unbound_source_and_race_are_rejected(self):
        for acquisition in [{'MISSED': ['beforeinfo-20261117-04-01']},
                            {'REJECTED': [{'SOURCE_ID': SID, 'RACE_KEY': '20261117-04-01'}]}]:
            with self.assertRaisesRegex(ValueError, 'HEALTH_ISSUE_'):
                self.summary(acquisition)

    def test_wrong_day_and_duplicate_plan_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'HEALTH_MORNING_DAY'):
            health.summarize(self.artifact, '2026-11-16', {}, now=NOW)
        self.artifact.plan += deepcopy(self.artifact.plan)
        with self.assertRaisesRegex(ValueError, 'HEALTH_PLAN_IDENTITY'):
            self.summary()

    def test_table_identity_cardinality_and_order_are_strict(self):
        mutations = [page().replace(b'jcd=03', b'jcd=04', 1), page() + page(),
                     page().replace(b'>11:14<', b'>25:14<'),
                     page().replace(b'>11:38<', b'>10:38<'),
                     page().replace(b'20261117', b'20261116', 1)]
        for raw in mutations:
            with self.assertRaisesRegex(ValueError, 'HEALTH_SCHEDULE_'):
                health.official_schedule(raw, URL, DAY, '03')

    def test_table_is_not_accepted_from_an_unbound_origin(self):
        for url in [URL.replace('www.boatrace.jp', 'example.com'),
                    URL.replace('hd=20261117', 'hd=20261116'),
                    URL.replace('beforeinfo', 'raceresult')]:
            with self.assertRaisesRegex(ValueError, 'HEALTH_SOURCE_IDENTITY'):
                health.official_schedule(page(), url, DAY, '03')

    def test_observer_calls_capture_once_and_returns_identical_evidence(self):
        acquisition = {'MISSED': [SID]}
        original = Mock(return_value=acquisition)
        observed = health.observed_capture(original, lambda root: self.artifact, DAY)
        with patch.object(health, 'report') as report:
            self.assertIs(observed(self.root), acquisition)
        original.assert_called_once_with(self.root)
        report.assert_called_once_with(self.artifact, DAY, acquisition)

    def test_observer_failure_does_not_prevent_prediction_handoff_or_echo_details(self):
        acquisition = {'ACQUIRED': [SID]}
        original = Mock(return_value=acquisition)
        observed = health.observed_capture(original, lambda root: self.artifact, DAY)
        output = io.StringIO()
        with patch.object(health, 'report', side_effect=ValueError('sensitive content')), redirect_stdout(output):
            self.assertIs(observed(self.root), acquisition)
        self.assertIn('UNVERIFIED=ValueError', output.getvalue())
        self.assertNotIn('sensitive', output.getvalue())

    def test_original_capture_failure_propagates_without_false_diagnostics(self):
        observed = health.observed_capture(Mock(side_effect=RuntimeError('capture failed')), lambda root: self.artifact, DAY)
        with patch.object(health, 'report') as report, self.assertRaisesRegex(RuntimeError, 'capture failed'):
            observed(self.root)
        report.assert_not_called()


if __name__ == '__main__': unittest.main()
