"""Offline transport, crash and whole-response deadline contracts."""
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import signal
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.request import Request

import live_refusal_recovery_v1 as recovery

DAY = '2026-11-23'
NOW = datetime.fromisoformat(DAY + 'T14:00:00+09:00')
SOURCE = 'beforeinfo-20261123-01-03'
URL = 'https://www.boatrace.jp/owpc/pc/race/beforeinfo?hd=20261123&jcd=01&rno=3'


class Deferred(Exception):
    def __init__(self, retry_at, reason):
        self.retry_at, self.reason = retry_at, reason
        super().__init__(reason)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / 'store'
        ledger_root = root / 'ledger'
        (ledger_root / 'events').mkdir(parents=True)
        self.event = {'TYPE': 'OUTCOME_RECORDED', 'INDEX': 4,
            'AT_JST': DAY + 'T12:00:00+09:00', 'PAYLOAD': {'OUTCOME': 'HTTP_403'}}
        (ledger_root / 'events/000004.json').write_text(json.dumps(self.event))
        self.ledger = SimpleNamespace(root=ledger_root,
            _manifest=lambda: {'OPERATIONAL_DATE_JST': DAY}, events=lambda: [self.event])
        self.cutoff = NOW + timedelta(minutes=5)
        self.plan = {'OPERATIONAL_DATE_JST': DAY, 'PREDEADLINE_PLAN': [{
            'SOURCE_ID': SOURCE, 'KIND': 'BEFOREINFO', 'RACE_KEY': '20261123-01-03',
            'URL': URL, 'MUST_FINISH_BEFORE_JST': self.cutoff.isoformat()}]}
        self.now = NOW
        self.client = SimpleNamespace(clock=lambda: self.now, opener=lambda *_a, **_k: None)
        self.runner = SimpleNamespace(client=self.client, store=SimpleNamespace(root=root,
            ledger=self.ledger, load_verified_snapshot=lambda *_a: {'SNAPSHOT': {'PAYLOAD': self.plan}}))
        self.official = SimpleNamespace(AcquisitionDeferred=Deferred,
            LIVE_REFUSAL_RECOVERY_CLAIM_PROVIDER=None)
        self.state = {'POLICY': recovery.POLICY, 'STATUS': 'COOLDOWN',
            'ARMED_AT_JST': self.event['AT_JST'], 'RETRY_AT_JST': NOW.isoformat()}
        self.payload = {'SOURCE_ID': SOURCE, 'HOST': 'www.boatrace.jp',
            'REQUEST_ID': 'request-' + '1' * 32, 'URL_SHA256': hashlib.sha256(URL.encode()).hexdigest()}
        self.requests = []
        self.claimed = False
        self.env = patch.dict('os.environ', {'GITHUB_RUN_ID': '123456', 'G11_APP_ORIGIN': recovery.ORIGIN})
        self.env.start(); self.addCleanup(self.env.stop)

    def site(self, body):
        self.requests.append(body)
        if self.claimed:
            raise recovery.ClaimUnavailable(spent=True)
        self.claimed = True
        return {**body, 'schema': recovery.GRANT_SCHEMA, 'claim_id': 'c' * 32,
            'granted_at_jst': self.now.isoformat(timespec='seconds'),
            'lease_until_jst': (self.now + timedelta(seconds=90)).isoformat(timespec='seconds')}

    def provider(self, transport=None):
        return recovery.ClaimProvider(self.runner, self.official, transport or self.site,
            authority_loader=lambda *_a: {'authority_sha256': 'a' * 64})

    def call(self, provider):
        return provider(self.ledger, self.payload, NOW, self.state)

    def test_grant_binds_actual_saved_request_schedule_and_trigger_bytes(self):
        provider = self.provider()
        grant = self.call(provider)
        self.assertEqual(grant['source_id'], SOURCE)
        self.assertEqual(grant['trigger_sha256'], hashlib.sha256(
            (self.ledger.root / 'events/000004.json').read_bytes()).hexdigest())
        self.assertEqual(provider.pending['url'], URL)
        self.assertEqual(len(self.requests), 1)

    def test_lost_ack_spends_claim_and_restored_worker_cannot_get_another(self):
        def lost(body):
            self.site(body)
            raise TimeoutError()
        with self.assertRaisesRegex(Deferred, 'LIVE_RECOVERY_CLAIM_DEFERRED'):
            self.call(self.provider(lost))
        self.payload['REQUEST_ID'] = 'request-' + '2' * 32
        with self.assertRaisesRegex(Deferred, 'LIVE_RECOVERY_CLAIM_SPENT'):
            self.call(self.provider())
        self.assertTrue(self.claimed)
        self.assertEqual(len(self.requests), 2)

    def test_same_process_does_not_retry_ambiguous_claim_after_wait(self):
        def lost(body):
            self.requests.append(body)
            raise TimeoutError()
        provider = self.provider(lost)
        with self.assertRaises(Deferred): self.call(provider)
        self.now += timedelta(minutes=2)
        with self.assertRaises(Deferred) as exc: self.call(provider)
        self.assertGreater(exc.exception.retry_at, self.now)
        self.assertEqual(len(self.requests), 1)

    def test_wrong_url_old_day_and_insufficient_cutoff_make_zero_claims(self):
        for change in ('url', 'day', 'cutoff'):
            with self.subTest(change=change):
                original = dict(self.payload)
                original_day = self.ledger._manifest
                old_cutoff = self.plan['PREDEADLINE_PLAN'][0]['MUST_FINISH_BEFORE_JST']
                if change == 'url': self.payload['URL_SHA256'] = 'f' * 64
                if change == 'day': self.ledger._manifest = lambda: {'OPERATIONAL_DATE_JST': '2026-11-22'}
                if change == 'cutoff': self.plan['PREDEADLINE_PLAN'][0]['MUST_FINISH_BEFORE_JST'] = (NOW + timedelta(seconds=49)).isoformat()
                with self.assertRaises(Deferred): self.call(self.provider())
                self.payload = original
                self.ledger._manifest = original_day
                self.plan['PREDEADLINE_PLAN'][0]['MUST_FINISH_BEFORE_JST'] = old_cutoff
        self.assertEqual(self.requests, [])

    def test_wrong_receipt_and_late_ack_never_authorize_official_http(self):
        for change in ('source_id', 'request_id', 'authority_sha256', 'late'):
            with self.subTest(change=change):
                self.claimed = False; self.now = NOW
                def tamper(body):
                    grant = self.site(body)
                    if change == 'late': self.now = self.cutoff - timedelta(seconds=25)
                    else: grant[change] = 'wrong'
                    return grant
                provider = self.provider(tamper)
                with self.assertRaises(Deferred): self.call(provider)
                self.assertIsNone(provider.pending)
                self.assertTrue(self.claimed)

    def test_context_restores_native_provider_and_original_opener(self):
        original = self.client.opener
        with patch.dict('sys.modules', {'g11.canonical': SimpleNamespace(official_pipeline=self.official)}):
            with self.assertRaisesRegex(RuntimeError, 'fixture'):
                with recovery.durable_refusal_admission(self.runner):
                    self.assertIsInstance(self.official.LIVE_REFUSAL_RECOVERY_CLAIM_PROVIDER, recovery.ClaimProvider)
                    raise RuntimeError('fixture')
        self.assertIsNone(self.official.LIVE_REFUSAL_RECOVERY_CLAIM_PROVIDER)
        self.assertIs(self.client.opener, original)

    def test_legacy_runtime_has_no_provider_or_opener_change(self):
        original = self.client.opener
        with patch.dict('sys.modules', {'g11.canonical': SimpleNamespace(official_pipeline=SimpleNamespace())}):
            with recovery.durable_refusal_admission(self.runner):
                self.assertIs(self.client.opener, original)

    def test_probe_open_and_whole_read_are_wall_bounded_and_restore_alarm(self):
        old = signal.getsignal(signal.SIGALRM)
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_a): return False
            def read(self, *_a): time.sleep(.3); return b''
        for slow_open in (True, False):
            with self.subTest(slow_open=slow_open):
                self.claimed = False
                provider = self.provider(); self.call(provider)
                def opener(*_a, **_k):
                    if slow_open: time.sleep(.3)
                    return Response()
                started = time.monotonic()
                with patch.object(recovery, 'PROBE_WALL_SECONDS', .03):
                    with self.assertRaises(TimeoutError):
                        with recovery.TimedProbeResponse(opener, Request(URL), {}, provider) as response:
                            response.read(1024)
                self.assertLess(time.monotonic() - started, .25)
                self.assertIs(signal.getsignal(signal.SIGALRM), old)
                self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
                self.assertIsNone(provider.pending)

    def test_post_begin_rejection_is_settleable_timeout_without_http(self):
        provider = self.provider(); self.call(provider)
        opened = []
        self.now = self.cutoff
        with self.assertRaisesRegex(TimeoutError, 'LIVE_RECOVERY_PREOPEN_REJECTED'):
            recovery.TimedProbeResponse(lambda *_a, **_k: opened.append(True), Request(URL), {}, provider)
        self.assertEqual(opened, [])
        self.assertIsNone(provider.pending)


if __name__ == '__main__':
    unittest.main()
