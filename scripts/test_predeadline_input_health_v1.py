from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
from contextlib import redirect_stdout
import io
import tempfile
from types import SimpleNamespace
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
    def recovery_chain(self, terminal=None):
        """Two real refusals separated by normal traffic; no recovery-only GET."""
        times = [DAY+'T11:46:00+09:00', DAY+'T12:20:00+09:00', DAY+'T13:53:00+09:00']
        entries = [(SID, 'HTTP_403'), ('odds3t-'+KEY, 'HTTP_200'), (SID, 'HTTP_403')]
        if terminal:
            times.append(DAY+'T15:55:00+09:00')
            entries.append(('odds3t-'+KEY, terminal))
        events = []
        for i, ((source, outcome), at) in enumerate(zip(entries, times)):
            request = 'request-' + str(i)
            events.extend([{'TYPE':'REQUEST_STARTED', 'AT_JST':at,
                'PAYLOAD':{'REQUEST_ID':request, 'SOURCE_ID':source, 'HOST':'www.boatrace.jp'}},
                {'TYPE':'OUTCOME_RECORDED', 'AT_JST':at,
                 'PAYLOAD':{'REQUEST_ID':request, 'OUTCOME':outcome}}])
        state = {'REQUEST_POLICY':health.RECOVERY_POLICY, 'STOPPED':terminal not in (None, 'HTTP_200'),
                 'GENERAL_HTTP_403_COUNT':2, 'STOPPED_AT_JST':times[-1] if terminal not in (None, 'HTTP_200') else None,
                 'PAUSE_UNTIL':{'www.boatrace.jp':datetime.fromisoformat(DAY+'T15:53:00+09:00')},
                 'PAUSE_STARTED_AT_JST':{'www.boatrace.jp':times[2]}}
        ledger = Mock()
        ledger.state.return_value, ledger.events.return_value = state, events
        return ledger, state

    def recovery_report(self, ledger, acquisition, at=NOW, workspace=''):
        self.artifact.ledger = ledger
        output = io.StringIO()
        with patch.dict('sys.modules', {'g11.canonical.official_pipeline':SimpleNamespace(FIRST_403_PAUSE_SECONDS=1800)}), \
                patch.object(health, 'datetime', wraps=datetime) as clock, \
                patch.dict('os.environ', {'GITHUB_WORKSPACE':workspace, 'GITHUB_RUN_ID':'12345'}), redirect_stdout(output):
            clock.now.return_value = at
            value = health.report(self.artifact, DAY, acquisition)
        return value, output.getvalue()

    def test_recovery_second_refusal_publishes_120_minute_pause_from_actual_event(self):
        ledger, state = self.recovery_chain()
        at = datetime.fromisoformat(DAY+'T13:53:01+09:00')
        (self.root/'.relay-output').mkdir()
        acquisition = {'RETRYABLE_FAILURES':[{'SOURCE_ID':SID, 'RACE_KEY':KEY, 'REASON':'HTTP_403'}]}
        value, output = self.recovery_report(ledger, acquisition, at, str(self.root))
        self.assertEqual(value['status'], 'DEFERRED')
        self.assertIsNone(value['host_stop'])
        self.assertEqual(value['host_pause'], {'date':DAY, 'reason':'HOST_PAUSED',
            'observed_at_jst':DAY+'T13:53:00+09:00', 'retry_at_jst':DAY+'T15:53:00+09:00'})
        self.assertNotIn('G11_VERIFIED_HOST_STOP=', output)
        self.assertEqual(json.loads((self.root/'.relay-output/host-pause.json').read_text()),
                         {**value['host_pause'], 'run_id':'12345'})
        self.assertFalse(value['prediction_generated'])
        self.assertFalse(value['prediction_boundary_changed'])

    def test_recovery_probe_failure_uses_its_outcome_time_even_for_odds(self):
        for outcome in ('TIMEOUT', 'NETWORK_ERROR', 'HTTP_OTHER', 'HTTP_5XX', 'HTTP_403', 'HTTP_429', 'CONTENT_INVALID'):
            with self.subTest(outcome=outcome):
                ledger, state = self.recovery_chain(outcome)
                self.assertEqual(health.saved_pipeline_stop(ledger, DAY, NOW, state),
                    {'date':DAY, 'reason':'PIPELINE_STOPPED', 'observed_at_jst':DAY+'T15:55:00+09:00'})
        ledger, _ = self.recovery_chain('TIMEOUT')
        stale_pause = {'DEFERRED':{'REASON':'HOST_PAUSED', 'RETRY_AT_JST':DAY+'T16:30:00+09:00'}}
        value, output = self.recovery_report(ledger, stale_pause)
        self.assertEqual(value['status'], 'STOPPED')
        self.assertEqual(value['host_stop']['observed_at_jst'], DAY+'T15:55:00+09:00')
        self.assertIsNone(value['host_pause'])
        self.assertNotIn('G11_VERIFIED_HOST_PAUSE=', output)
        self.assertNotIn('Next eligible request', output)

    def test_recovery_later_third_refusal_is_not_backdated_to_arming_refusal(self):
        ledger, state = self.recovery_chain('HTTP_200')
        state.update(STOPPED=True, GENERAL_HTTP_403_COUNT=3, STOPPED_AT_JST=DAY+'T16:05:00+09:00')
        ledger.events.return_value.extend([
            {'TYPE':'REQUEST_STARTED', 'PAYLOAD':{'REQUEST_ID':'third', 'SOURCE_ID':SID, 'HOST':'www.boatrace.jp'}},
            {'TYPE':'OUTCOME_RECORDED', 'AT_JST':state['STOPPED_AT_JST'], 'PAYLOAD':{'REQUEST_ID':'third', 'OUTCOME':'HTTP_403'}}])
        self.assertEqual(health.saved_pipeline_stop(ledger, DAY, NOW, state)['observed_at_jst'], state['STOPPED_AT_JST'])

    def test_recovery_expired_pause_remains_history_without_reporting_current_stop(self):
        ledger, _ = self.recovery_chain('HTTP_200')
        value, output = self.recovery_report(ledger, {}, datetime.fromisoformat(DAY+'T15:56:00+09:00'))
        self.assertEqual(value['status'], 'PASS')
        self.assertIsNone(value['host_pause'])
        self.assertIsNone(value['host_stop'])
        self.assertIn('G11_VERIFIED_HOST_PAUSE=', output)
        self.assertNotIn('Next eligible request', output)

    def test_recovery_retry_after_keeps_actual_start_and_can_cross_midnight(self):
        for start_text, retry_text in [(DAY+'T11:46:00+09:00', DAY+'T12:46:00+09:00'),
                                      (DAY+'T23:00:00+09:00', '2026-11-18T01:00:00+09:00')]:
            with self.subTest(retry=retry_text):
                ledger, state = self.recovery_chain()
                state['PAUSE_STARTED_AT_JST']['www.boatrace.jp'] = start_text
                state['PAUSE_UNTIL']['www.boatrace.jp'] = datetime.fromisoformat(retry_text)
                ledger.events.return_value = [
                    {'TYPE':'REQUEST_STARTED','PAYLOAD':{'REQUEST_ID':'first','SOURCE_ID':SID,'HOST':'www.boatrace.jp'}},
                    {'TYPE':'OUTCOME_RECORDED','AT_JST':start_text,'PAYLOAD':{'REQUEST_ID':'first','OUTCOME':'HTTP_403'}}]
                result = health.saved_beforeinfo_pause(ledger, DAY, datetime.fromisoformat(start_text), 1800, state=state)
                self.assertEqual((result['observed_at_jst'], result['retry_at_jst']), (start_text, retry_text))

    def test_recovery_unknown_policy_suppresses_stale_publication_and_never_falls_back(self):
        ledger, state = self.recovery_chain('TIMEOUT')
        state['REQUEST_POLICY'] = 'UNKNOWN_FUTURE_POLICY'
        (self.root/'.relay-output').mkdir()
        path = self.root/'.relay-output/host-pause.json'
        path.write_text(json.dumps({'reason':'PIPELINE_STOPPED', 'observed_at_jst':DAY+'T13:53:00+09:00'}))
        value, output = self.recovery_report(ledger, {'DEFERRED':{'REASON':'PIPELINE_STOPPED'}}, workspace=str(self.root))
        self.assertEqual(value['status'], 'UNVERIFIED')
        self.assertIsNone(value['host_stop'])
        self.assertIsNone(value['host_pause'])
        self.assertFalse(path.exists())
        self.assertNotIn('G11_VERIFIED_HOST_', output)

    def test_recovery_transition_time_and_request_binding_are_verified(self):
        for mutation in ('future', 'other_day', 'wrong_zone', 'unbound_time', 'wrong_request', 'success', 'missing_time'):
            with self.subTest(mutation=mutation):
                ledger, state = self.recovery_chain('TIMEOUT')
                if mutation == 'future': state['STOPPED_AT_JST'] = DAY+'T17:00:00+09:00'
                elif mutation == 'other_day': state['STOPPED_AT_JST'] = '2026-11-16T15:55:00+09:00'
                elif mutation == 'wrong_zone': state['STOPPED_AT_JST'] = DAY+'T06:55:00+00:00'
                elif mutation == 'unbound_time': state['STOPPED_AT_JST'] = DAY+'T15:56:00+09:00'
                elif mutation == 'wrong_request': ledger.events.return_value[-1]['PAYLOAD']['REQUEST_ID'] = 'different'
                elif mutation == 'success': ledger.events.return_value[-1]['PAYLOAD']['OUTCOME'] = 'HTTP_200'
                else: del state['STOPPED_AT_JST']
                value, output = self.recovery_report(ledger, {'DEFERRED':{'REASON':'PIPELINE_STOPPED'}})
                self.assertEqual(value['status'], 'UNVERIFIED')
                self.assertIsNone(value['host_stop'])
                self.assertNotIn('G11_VERIFIED_HOST_STOP=', output)

    def test_recovery_pause_longer_than_site_contract_is_not_shortened(self):
        ledger, state = self.recovery_chain()
        state['PAUSE_UNTIL']['www.boatrace.jp'] = datetime.fromisoformat('2026-11-18T16:00:00+09:00')
        before = deepcopy(state)
        at = datetime.fromisoformat(DAY+'T13:54:00+09:00')
        value, output = self.recovery_report(ledger, {'DEFERRED':{'REASON':'HOST_PAUSED',
            'RETRY_AT_JST':'2026-11-18T16:00:00+09:00'}}, at)
        self.assertEqual(value['status'], 'UNVERIFIED')
        self.assertIsNone(value['host_pause'])
        self.assertEqual(state, before)
        self.assertNotIn('G11_VERIFIED_HOST_', output)

    def test_recovery_pause_cannot_use_other_host_or_other_days_beforeinfo(self):
        for key, replacement in [('HOST', 'other.invalid'), ('SOURCE_ID', 'beforeinfo-20261116-03-01')]:
            with self.subTest(key=key):
                ledger, _ = self.recovery_chain()
                ledger.events.return_value[-2]['PAYLOAD'][key] = replacement
                value, output = self.recovery_report(ledger, {'DEFERRED':{'REASON':'HOST_PAUSED',
                    'RETRY_AT_JST':DAY+'T15:53:00+09:00'}}, datetime.fromisoformat(DAY+'T13:54:00+09:00'))
                self.assertEqual(value['status'], 'UNVERIFIED')
                self.assertIsNone(value['host_pause'])
                self.assertNotIn('G11_VERIFIED_HOST_', output)

    def test_last_source_403_is_reported_in_same_pass_with_no_missed_or_deferred_source(self):
        ledger = Mock()
        until = NOW + health.timedelta(minutes=30)
        ledger.state.return_value = {'STOPPED':False,'GENERAL_HTTP_403_COUNT':1,'PAUSE_UNTIL':{'www.boatrace.jp':until}}
        ledger.events.return_value = [
            {'TYPE':'REQUEST_STARTED','PAYLOAD':{'HOST':'www.boatrace.jp','SOURCE_ID':SID,'REQUEST_ID':'one'}},
            {'TYPE':'OUTCOME_RECORDED','AT_JST':NOW.isoformat(),'PAYLOAD':{'OUTCOME':'HTTP_403','REQUEST_ID':'one'}}]
        self.artifact.ledger = ledger
        acquisition = {'RETRYABLE_FAILURES':[{'SOURCE_ID':SID,'RACE_KEY':KEY,'REASON':'HTTP_403'}]}
        output = io.StringIO()
        with patch.dict('sys.modules', {'g11.canonical.official_pipeline':SimpleNamespace(FIRST_403_PAUSE_SECONDS=1800)}), \
                patch.object(health, 'datetime', wraps=datetime) as clock, \
                patch.dict('os.environ', {'GITHUB_WORKSPACE':''}), redirect_stdout(output):
            clock.now.return_value = NOW
            value = health.report(self.artifact,DAY,acquisition)
        self.assertEqual(value['status'], 'DEFERRED')
        self.assertEqual(value['host_pause']['retry_at_jst'], until.isoformat())
        self.assertIn('G11_VERIFIED_HOST_PAUSE=',output.getvalue())

    def test_terminal_event_is_bound_to_its_checked_day_and_excludes_odds_only_errors(self):
        ledger = Mock()
        state = {'STOPPED': True}
        def events(outcomes):
            rows = []
            for i, (source, outcome) in enumerate(outcomes):
                request = str(i)
                rows.extend([{'TYPE':'REQUEST_STARTED','PAYLOAD':{'REQUEST_ID':request,'SOURCE_ID':source}},
                    {'TYPE':'OUTCOME_RECORDED','AT_JST':(NOW-health.timedelta(minutes=10-i)).isoformat(),
                     'PAYLOAD':{'REQUEST_ID':request,'OUTCOME':outcome}}])
            return rows
        for code, needed in [('HTTP_403',2), ('HTTP_429',3)]:
            ledger.events.return_value = events([('odds3t-'+KEY,code)] + [(SID,code)]*needed + [(SID,'HTTP_200')])
            result = health.saved_pipeline_stop(ledger,DAY,NOW,state)
            self.assertEqual(result, {'date':DAY,'reason':'PIPELINE_STOPPED',
                'observed_at_jst':(NOW-health.timedelta(minutes=10-needed)).isoformat()})
            self.assertIsNone(health.saved_pipeline_stop(ledger,DAY,NOW,{'STOPPED':False}))
            self.assertIsNone(health.saved_pipeline_stop(ledger,DAY,NOW-health.timedelta(days=1),state))
            ledger.events.return_value[1]['PAYLOAD']['REQUEST_ID'] = 'wrong'
            self.assertIsNone(health.saved_pipeline_stop(ledger,DAY,NOW,state))
        self.assertEqual(self.summary({'DEFERRED':{'REASON':'PIPELINE_STOPPED'}})['status'], 'STOPPED')

    def test_only_the_bound_beforeinfo_event_can_date_a_saved_host_pause(self):
        ledger = Mock()
        until = NOW + health.timedelta(minutes=20)
        start = until - health.timedelta(minutes=30)
        ledger.state.return_value = {'STOPPED':False,'GENERAL_HTTP_403_COUNT':1,'PAUSE_UNTIL':{'www.boatrace.jp':until}}
        events = [{'TYPE':'REQUEST_STARTED','PAYLOAD':{'HOST':'www.boatrace.jp','SOURCE_ID':SID,'REQUEST_ID':'one'}},
                  {'TYPE':'OUTCOME_RECORDED','AT_JST':start.isoformat(),'PAYLOAD':{'OUTCOME':'HTTP_403','REQUEST_ID':'one'}}]
        ledger.events.return_value = events
        for at in (NOW, until+health.timedelta(minutes=1)):
            self.assertEqual(health.saved_beforeinfo_pause(ledger,DAY,at,1800)['observed_at_jst'],start.isoformat())
        events[0]['PAYLOAD']['SOURCE_ID'] = 'odds3t-'+KEY
        self.assertIsNone(health.saved_beforeinfo_pause(ledger,DAY,NOW,1800))

    def test_host_pause_remains_visible_even_without_a_new_per_source_error(self):
        result = self.summary({'DEFERRED': {'REASON': 'HOST_PAUSED','RETRY_AT_JST': DAY+'T16:29:00+09:00'}})
        self.assertEqual((result['status'],result['source_issue_count']), ('DEFERRED',0))
        self.assertEqual(result['host_pause'], {'date':DAY, 'reason':'HOST_PAUSED',
            'observed_at_jst':NOW.isoformat(), 'retry_at_jst':DAY+'T16:29:00+09:00'})
        for deferred in ({'REASON':'HOST_PAUSED','RETRY_AT_JST':'bad'}, {'REASON':'SERIAL_PACING'},
                         {'REASON':'HOST_PAUSED','RETRY_AT_JST':DAY+'T15:00:00+09:00'}):
            self.assertIsNone(self.summary({'DEFERRED':deferred})['host_pause'])

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

    def test_first_race_requires_the_bound_selected_day_cancellation_before_its_cutoff(self):
        yesterday = '<li><a>11月16日<span>中止</span></a></li>'
        row = '<li class="is-active2"><span class="tab2_inner">11月17日<span>中止</span></span></li>'
        self.receipt['FINAL_URL'] = URL
        another = '20261117-04-01'
        self.artifact.plan += [dict(self.artifact.plan[0], RACE_KEY=another, SOURCE_ID='beforeinfo-'+another,
                                    URL=URL.replace('jcd=03','jcd=04'))]
        def save_calendar(entries):
            raw = ('<ul class="tab2_tabs">' + yesterday + entries + '</ul>').encode() + page()
            (self.rejected / 'response.bin').write_bytes(raw)
            self.receipt['SHA256'] = hashlib.sha256(raw).hexdigest()
            self.save_receipt()
        save_calendar(row)
        summary = self.summary({'MISSED':[SID, 'beforeinfo-'+another]})
        self.assertEqual(summary['official_cancelled_venue_days'], {KEY[:-3]:self.receipt['SHA256']})
        self.assertEqual(summary['official_cancelled_race_count'],1)
        self.assertEqual([i['race_key'] for i in summary['issues']], [another])
        for entries in [row.replace('11月17日','11月18日'), row.replace('中止','初日'),
                        row.replace('is-active2','inactive'), row + row, '']:
            save_calendar(entries)
            self.assertEqual(self.summary()['official_cancelled_race_count'],0)
        save_calendar(row)
        for field, altered in [('FINAL_URL',URL.replace('jcd=03','jcd=04')),
                               ('CAPTURED_AT_JST',DAY+'T10:59:00+09:00')]:
            original = self.receipt[field]
            self.receipt[field] = altered
            self.save_receipt()
            self.assertEqual(self.summary()['official_cancelled_race_count'],0)
            self.receipt[field] = original
            self.save_receipt()

    def test_engine_hold_distinguishes_complete_inputs_from_a_saved_live_lock(self):
        pre = self.root / 'snapshots/predeadline'
        pre.mkdir(parents=True)
        row = {'key':KEY,'formal_status':'FORMAL','predeadline':None}
        run = {'PREDICTION':{'FAILED':[{'RACE_KEY':KEY, 'REASON':'P12_EXHIBITION_START'}]}}
        validator = Mock(side_effect=ValueError('P12_EXHIBITION_START'))
        self.assertEqual(health.p3_input_holds(self.artifact,DAY,[row],run,validator)['race_count'],0)
        for name in (SID, 'odds3t-'+KEY):
            (pre / (name + '.json')).touch()
        snap = {'SNAPSHOT':{'PAYLOAD':{'CAPTURED_AT_JST':DAY+'T10:52:35+09:00'}}}
        with patch.object(self.artifact,'load_verified_snapshot',return_value=snap):
            report = health.p3_input_holds(self.artifact,DAY,[row],run,validator)
        validator.assert_called_once_with(snap,snap,KEY)
        self.assertEqual((report['races'][0]['canonical_reforecast_saved'], report['races'][0]['input_gate'],
                          report['races'][0]['reported_reason']), (False,'P12_EXHIBITION_START','P12_EXHIBITION_START'))
        (pre / ('current-growth-p3-live-'+KEY+'.json')).touch()
        self.assertEqual(health.p3_input_holds(self.artifact,DAY,[row],run,validator)['race_count'],0)

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
        with patch.object(self.artifact, 'rejected_response_receipt', side_effect=RuntimeError('REJECTED_RESPONSE_READBACK')):
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

    def test_site_pause_uses_the_relay_transport_and_keeps_bearer_off_argv(self):
        receipt = {'date': DAY, 'observed_at_jst': DAY + 'T15:00:00+09:00', 'retry_at_jst': DAY + 'T15:30:00+09:00'}
        def send(argv, *, input, **kwargs):
            self.assertEqual(argv[0], 'curl')
            self.assertIn('https://example.invalid/api/g11-input-pause', argv)
            self.assertIn('Authorization: Bearer fixture-token\n', input)
            self.assertNotIn('fixture-token', str(argv))
            path = Path(argv[argv.index('--data-binary') + 1][1:])
            self.assertEqual(json.loads(path.read_bytes()), receipt)
            return Mock(returncode=0, stdout=json.dumps(receipt) + '\n200')
        with patch.object(health.subprocess, 'run', side_effect=send):
            self.assertEqual(health.send_pause_to_site('https://example.invalid','fixture-token',receipt), receipt)

    def test_rejected_site_pause_records_only_the_http_status(self):
        output = io.StringIO()
        with patch.object(health.subprocess, 'run', return_value=Mock(returncode=0, stdout='private body\n409')), redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, 'INPUT_HEALTH_PUBLICATION_HTTP'):
                health.send_pause_to_site('https://example.invalid', 'fixture-token', {})
        self.assertEqual(output.getvalue().strip(), 'G11_INPUT_PAUSE_HTTP=409')

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

