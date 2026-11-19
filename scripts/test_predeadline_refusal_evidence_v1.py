import io
import unittest
from contextlib import redirect_stdout
from urllib.error import HTTPError
from unittest.mock import Mock, patch
import predeadline_refusal_evidence_v1 as evidence


class RefusalEvidenceTests(unittest.TestCase):
    def test_bounded_redacted_and_async(self):
        key = 'beforeinfo-20261118-15-09'
        body = io.BytesIO(b'Too Many Requests SECRET ' + b'x'*8192)
        error = HTTPError('https://www.boatrace.jp/',403,'Forbidden',
                          {'Retry-After':'120','Set-Cookie':'SECRET'},body)
        original = ValueError('HTTP_403')
        original.__cause__ = error
        capture = io.StringIO()
        with redirect_stdout(capture), patch.object(evidence,'Thread') as thread:
            evidence.observe(key,original)
            thread.return_value.start.assert_called_once()
            self.assertEqual(body.tell(),0)
            evidence.body_evidence(key,error)
        self.assertEqual(body.tell(),4096)
        self.assertIn('RATE_LIMIT_MESSAGE',capture.getvalue())
        self.assertIn('"retry_after_present": true',capture.getvalue())
        self.assertNotIn('SECRET',capture.getvalue())

    def test_previous_host_window_and_repeat_identity(self):
        rows=[]
        for i,(minute,sec,host,source,code) in enumerate([
            (0,0,'www.boatrace.jp','beforeinfo-A','HTTP_200'),
            (9,45,'www.boatrace.jp','odds3t-B','HTTP_200'),
            (9,55,'other','ignored','HTTP_200'),
            (10,0,'www.boatrace.jp','beforeinfo-A','HTTP_403')]):
            at=f'2026-11-18T12:{minute:02d}:{sec:02d}+09:00'
            rows.extend([{'TYPE':'REQUEST_STARTED','AT_JST':at,'PAYLOAD':{'REQUEST_ID':i,'HOST':host,'SOURCE_ID':source}},
                         {'TYPE':'OUTCOME_RECORDED','AT_JST':at,'PAYLOAD':{'REQUEST_ID':i,'OUTCOME':code}}])
        row,=evidence.incident_windows(rows)
        self.assertEqual((row['prior_requests_60s'],row['prior_requests_10m'],row['seconds_since_previous_start']),(1,1,15))
        self.assertEqual(row['previous_requests_same_source'],1)
        self.assertEqual(row['previous_source'],'odds3t-B')

    def test_live_refusal_logs_the_recorded_window_once(self):
        key = 'beforeinfo-20261119-15-08'
        previous = 'odds3t-20261119-12-03'
        rows = []
        for i, (source, outcome) in enumerate([(previous, 'HTTP_200'), (key, 'HTTP_403')]):
            at = f'2026-11-19T18:36:{20*i:02d}+09:00'
            rows.extend([
                {'TYPE': 'REQUEST_STARTED', 'AT_JST': at,
                 'PAYLOAD': {'REQUEST_ID': i, 'HOST': 'www.boatrace.jp', 'SOURCE_ID': source}},
                {'TYPE': 'OUTCOME_RECORDED', 'AT_JST': at,
                 'PAYLOAD': {'REQUEST_ID': i, 'OUTCOME': outcome}},
            ])
        ledger = Mock()
        ledger.events.return_value = rows
        failure = ValueError('HTTP_403')
        failure.__cause__ = HTTPError('https://www.boatrace.jp/', 403, 'Forbidden', {}, io.BytesIO())
        with patch.object(evidence, 'Thread'), patch.object(evidence, 'emit') as emit:
            evidence.observe(key, failure, ledger)
        ledger.events.assert_called_once_with()
        row = emit.call_args.args[0]['request_window']
        self.assertEqual((row['source'], row['previous_source'], row['prior_requests_60s']),
                         (key, previous, 1))
