import hashlib
import io
import json
from email.message import Message
import unittest
from contextlib import redirect_stdout
from urllib.error import HTTPError
from urllib.request import Request
from types import SimpleNamespace
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


class ResponseObservationTests(unittest.TestCase):
    URL = 'https://www.boatrace.jp/owpc/pc/race/beforeinfo?rno=3&jcd=14&hd=20261122'

    def setUp(self):
        self.network = patch('socket.create_connection', side_effect=AssertionError('NETWORK_FORBIDDEN'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def events(self, capture):
        return [json.loads(line.split('=', 1)[1]) for line in capture.getvalue().splitlines()
                if line.startswith('G11_OFFICIAL_RESPONSE=')]

    def test_200_headers_observed_before_body_and_same_response_returned(self):
        body = io.BytesIO(b'PRIVATE BODY')
        response = SimpleNamespace(status=200, headers={
            'Server-Timing': 'cdn-cache;desc="MISS", edge;dur=9000.25, origin;dur=21',
            'Set-Cookie': 'NEVER_LOG'}, read=body.read)
        opener = Mock(return_value=response)
        client = SimpleNamespace(opener=opener)
        request = Request(self.URL, headers={'Authorization': 'NEVER_LOG'})
        capture = io.StringIO()
        with redirect_stdout(capture), evidence.observe_responses(client, clock=Mock(side_effect=[10, 19.1])):
            returned = client.opener(request, timeout=30)
            self.assertIs(returned, response)
            self.assertEqual(body.tell(), 0)
        self.assertIs(client.opener, opener)
        opener.assert_called_once_with(request, timeout=30)
        event, = self.events(capture)
        self.assertEqual(event['source'], 'beforeinfo-20261122-14-03')
        self.assertEqual(event['status'], 200)
        self.assertEqual(event['header_wait_ms'], 9100)
        self.assertEqual(event['server_timing_ms'], {'edge': 9000.25, 'origin': 21})
        self.assertTrue(event['server_timing_present'])
        self.assertNotIn('G11_OFFICIAL_REFUSAL=', capture.getvalue())
        self.assertNotIn('NEVER_LOG', capture.getvalue())
        self.assertNotIn('PRIVATE BODY', capture.getvalue())
        self.assertNotIn('https://', capture.getvalue())

    def test_http_error_same_instance_and_unread_body_with_restoration(self):
        body = io.BytesIO(b'FORBIDDEN PRIVATE TEXT')
        error = HTTPError(self.URL, 403, 'PRIVATE MESSAGE',
                          {'Server-Timing': 'edge;dur=8, origin;dur=2'}, body)
        opener = Mock(side_effect=error)
        client = SimpleNamespace(opener=opener)
        capture = io.StringIO()
        with redirect_stdout(capture):
            with self.assertRaises(HTTPError) as caught:
                with evidence.observe_responses(client, clock=Mock(side_effect=[10, 10.01])):
                    client.opener(Request(self.URL), timeout=30)
        self.assertIs(caught.exception, error)
        self.assertIs(client.opener, opener)
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(body.tell(), 0)
        event, = self.events(capture)
        self.assertEqual((event['status'], event['server_timing_ms']), (403, {'edge': 8, 'origin': 2}))
        self.assertNotIn('PRIVATE', capture.getvalue())

    def test_no_opener_call_emits_nothing_and_nested_observation_does_not_duplicate(self):
        response = SimpleNamespace(status=200, headers={})
        opener = Mock(return_value=response)
        client = SimpleNamespace(opener=opener)
        capture = io.StringIO()
        with redirect_stdout(capture):
            with evidence.observe_responses(client):
                pass  # A cached receipt never calls the opener.
            self.assertEqual(capture.getvalue(), '')
            with evidence.observe_responses(client):
                outer = client.opener
                with evidence.observe_responses(client):
                    self.assertIs(client.opener, outer)
                    self.assertIs(client.opener(self.URL), response)
                self.assertIs(client.opener, outer)
        self.assertEqual(len(self.events(capture)), 1)
        self.assertIs(client.opener, opener)
        opener.assert_called_once_with(self.URL)

    def test_no_server_timing_does_not_invent_zero_durations(self):
        response = SimpleNamespace(status=200, headers={'Server': 'edge'})
        client = SimpleNamespace(opener=Mock(return_value=response))
        capture = io.StringIO()
        with redirect_stdout(capture), evidence.observe_responses(client):
            client.opener(self.URL)
        event, = self.events(capture)
        self.assertEqual(event['server_timing_ms'], {})
        self.assertFalse(event['server_timing_present'])

    def test_quoted_descriptions_duplicates_and_invalid_durations_are_not_metrics(self):
        cases = [
            ('cdn-cache;desc="origin;dur=8, edge;dur=9999", edge;dur=7, origin;dur=12', {'edge': 7, 'origin': 12}),
            ('edge;desc="text;dur=9999, token=PRIVATE";dur=8, origin;dur=2.25', {'edge': 8, 'origin': 2.25}),
            ('edge;dur=9, edge;dur=10, edge;dur=11, origin;dur=12', {'origin': 12}),
            ('edge;dur=7;dur=8, origin;dur=0', {'origin': 0}),
            ('edge;dur=NaN, origin;dur=Infinity', {}),
            ('edge;dur=-2, origin;dur=1e3', {}),
            ('edge;dur=8oops, origin;dur="9"', {'origin': 9}),
            ('EDGE;DUR="22.5", ORIGIN;dur=0', {'edge': 22.5, 'origin': 0}),
            ('edge;dur=86400001, origin;dur=.5', {'origin': .5}),
            ('edge;dur=8, mystery;desc="unterminated', {}),
            ('edge;dur=8\nAuthorization: PRIVATE', {}),
            ('edge;dur=8, ' + 'x'*4096, {}),
            (','.join(['other;dur=1']*33) + ',edge;dur=8', {}),
        ]
        for value, expected in cases:
            with self.subTest(value=value[:80]):
                self.assertEqual(evidence.server_timing({'Server-Timing': value}), (True, expected))

    def test_multiple_server_timing_headers_are_bounded_and_distinguish_missing(self):
        headers = Message()
        headers['Server-Timing'] = 'cdn-cache;desc="MISS"'
        headers['Server-Timing'] = 'edge;dur=8912'
        headers['Server-Timing'] = 'origin;dur=13'
        self.assertEqual(evidence.server_timing(headers), (True, {'edge': 8912, 'origin': 13}))
        headers['Server-Timing'] = 'edge;dur=8913'
        self.assertEqual(evidence.server_timing(headers), (True, {'origin': 13}))
        for _ in range(32):
            headers['Server-Timing'] = 'unused;dur=1'
        self.assertEqual(evidence.server_timing(headers), (True, {}))
        self.assertEqual(evidence.server_timing({}), (False, {}))
        self.assertEqual(evidence.server_timing({'Server-Timing': ''}), (True, {}))
        self.assertEqual(evidence.server_timing(None), (None, {}))

    def test_metadata_and_logging_failures_cannot_replace_response_or_exception(self):
        class BadHeaders:
            def get(self, _key):
                raise ValueError('PRIVATE')
        response = SimpleNamespace(status=200, headers=BadHeaders())
        error = HTTPError(self.URL, 429, 'PRIVATE', BadHeaders(), io.BytesIO())
        for result in (response, error):
            with self.subTest(error=isinstance(result, Exception)):
                opener = Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)
                client = SimpleNamespace(opener=opener)
                with evidence.observe_responses(client):
                    if isinstance(result, Exception):
                        with self.assertRaises(HTTPError) as caught:
                            client.opener(self.URL)
                        self.assertIs(caught.exception, result)
                    else:
                        self.assertIs(client.opener(self.URL), result)
                self.assertIs(client.opener, opener)
                self.assertEqual(opener.call_count, 1)
        response.headers = error.headers = {}
        for result in (response, error):
            opener = Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)
            client = SimpleNamespace(opener=opener)
            with patch('builtins.print', side_effect=OSError('LOG_UNAVAILABLE')):
                with evidence.observe_responses(client):
                    if isinstance(result, Exception):
                        with self.assertRaises(HTTPError) as caught:
                            client.opener(self.URL)
                        self.assertIs(caught.exception, result)
                    else:
                        self.assertIs(client.opener(self.URL), result)
            self.assertIs(client.opener, opener)

    def test_untrusted_urls_are_passed_unchanged_without_diagnostic_output(self):
        response = SimpleNamespace(status=200, headers={})
        urls = [self.URL + '&token=PRIVATE', self.URL.replace('www.boatrace.jp', 'example.com'),
                self.URL.replace('hd=20261122', 'hd=20269999'), self.URL + '#PRIVATE',
                self.URL.replace('rno=3', 'rno=99'), self.URL.replace('https://', 'http://')]
        capture = io.StringIO()
        for url in urls:
            opener = Mock(return_value=response)
            client = SimpleNamespace(opener=opener)
            with redirect_stdout(capture), evidence.observe_responses(client):
                self.assertIs(client.opener(url, timeout=30), response)
            opener.assert_called_once_with(url, timeout=30)
        self.assertEqual(capture.getvalue(), '')

    def test_network_failure_and_internal_error_preserve_exception_and_restore(self):
        for error in (TimeoutError('PRIVATE'), RuntimeError('PRIVATE')):
            opener = Mock(side_effect=error)
            client = SimpleNamespace(opener=opener)
            capture = io.StringIO()
            with redirect_stdout(capture):
                with self.assertRaises(type(error)) as caught:
                    with evidence.observe_responses(client):
                        client.opener(self.URL)
            self.assertIs(caught.exception, error)
            self.assertIs(client.opener, opener)
            event, = self.events(capture)
            self.assertEqual(event['status'], 'TIMEOUT' if isinstance(error, TimeoutError) else 'NETWORK_ERROR')
            self.assertEqual(event['server_timing_ms'], {})
            self.assertIsNone(event['server_timing_present'])
            self.assertNotIn('PRIVATE', capture.getvalue())
