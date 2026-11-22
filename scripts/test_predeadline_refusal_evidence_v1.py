import hashlib
import io
import json
from email.message import Message
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


class ImmediateThread:
    def __init__(self, target, args, daemon):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


class RefusalMetadataTests(unittest.TestCase):
    def setUp(self):
        self.network = patch('socket.create_connection', side_effect=AssertionError('NETWORK_FORBIDDEN'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def test_allowlist_excludes_authentication_and_cookie(self):
        headers = Message()
        headers['Server'] = 'example-edge'
        headers['CF-Ray'] = 'a123b456-NRT'
        headers['Authorization'] = 'Bearer NEVER_LOG'
        headers['Set-Cookie'] = 'session=NEVER_LOG'
        headers['X-Secret'] = 'NEVER_LOG'
        self.assertEqual(evidence.response_metadata(headers), {'server':'example-edge', 'cf-ray':'a123b456-NRT'})

    def test_header_injection_and_unbounded_values_are_dropped(self):
        values = {'Server':'edge\nAuthorization: secret', 'Via':'v'*257,
                  'X-Request-Id':'Bearer should-not-appear', 'X-Cache':'MISS'}
        self.assertEqual(evidence.response_metadata(values), {'x-cache':'MISS'})

    def test_run_context_is_bounded_to_explicit_fields(self):
        with patch.dict(evidence.os.environ, {'GITHUB_RUN_ID':'123', 'GITHUB_RUN_ATTEMPT':'2',
                'GITHUB_JOB':'collector', 'RUNNER_ENVIRONMENT':'github-hosted',
                'GITHUB_TOKEN':'NEVER_LOG', 'EXTRA_SECRET':'NEVER_LOG'}, clear=True):
            self.assertEqual(evidence.run_context(), {'github_run_id':'123','github_run_attempt':'2',
                'github_job':'collector','runner_environment':'github-hosted'})

    def test_denial_hashes_original_bytes_without_printing_text(self):
        raw = b'ACCESS DENIED: secret-in-body'
        error = HTTPError('https://www.boatrace.jp/example',403,'Forbidden',{},io.BytesIO(raw))
        out = io.StringIO()
        with redirect_stdout(out):
            evidence.body_evidence('beforeinfo-20261121-04-11', error)
        item = json.loads(out.getvalue().split('=',1)[1])
        self.assertEqual(item['body_hint'],'GENERIC_DENIAL')
        self.assertEqual(item['response_prefix_bytes'], len(raw))
        self.assertEqual(item['response_prefix_sha256'],hashlib.sha256(raw).hexdigest())
        self.assertNotIn('secret-in-body',out.getvalue())

    def test_body_read_is_limited_to_4096(self):
        raw = b'forbidden ' + b'x'*6000
        error = HTTPError('https://www.boatrace.jp/example',403,'Forbidden',{},io.BytesIO(raw))
        out = io.StringIO()
        with redirect_stdout(out):
            evidence.body_evidence('beforeinfo-20261121-04-11',error)
        item = json.loads(out.getvalue().split('=',1)[1])
        self.assertEqual(item['response_prefix_bytes'],4096)
        self.assertEqual(item['response_prefix_sha256'],hashlib.sha256(raw[:4096]).hexdigest())

    def test_observe_does_not_change_exception_or_request_again(self):
        headers = Message()
        headers['Server']='edge'
        headers['Retry-After']='120'
        error = HTTPError('https://www.boatrace.jp/example?secret=NEVER_LOG',403,'Forbidden',headers,io.BytesIO(b'Forbidden'))
        exc=RuntimeError('HTTP_403')
        exc.__cause__=error
        out=io.StringIO()
        with patch.object(evidence,'Thread',ImmediateThread), redirect_stdout(out):
            result=evidence.observe('beforeinfo-20261121-04-11',exc)
        lines=[json.loads(s.split('=',1)[1]) for s in out.getvalue().splitlines()]
        self.assertIsNone(result)
        self.assertIs(exc.__cause__,error)
        self.assertEqual(str(exc),'HTTP_403')
        self.assertEqual(lines[0]['response_headers'],{'server':'edge','retry-after':'120'})
        self.assertNotIn('NEVER_LOG',out.getvalue())

    def test_diagnostic_failure_does_not_change_acquisition_error(self):
        error=HTTPError('https://www.boatrace.jp/example',403,'Forbidden',{},io.BytesIO(b'Forbidden'))
        exc=RuntimeError('HTTP_403'); exc.__cause__=error
        with patch.object(evidence,'response_metadata',side_effect=ValueError('bad metadata')):
            self.assertIsNone(evidence.observe('beforeinfo-20261121-04-11',exc))
        self.assertIs(exc.__cause__,error)

    def test_unexpected_source_and_status_produce_no_extra_output(self):
        out=io.StringIO()
        with redirect_stdout(out):
            for source,status in [('untrusted-source',403),('beforeinfo-20261121-04-11',500)]:
                exc=RuntimeError('original')
                exc.__cause__=HTTPError('https://www.boatrace.jp/example',status,'error',{},io.BytesIO(b'body'))
                evidence.observe(source,exc)
        self.assertEqual(out.getvalue(),'')
