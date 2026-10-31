from datetime import date
from functools import wraps
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import racer_period_source_v1 as source


def record(year=2026, half=2, start='2025-11-01', end='2026-04-30'):
    return SimpleNamespace(period_year=year, period_half=half,
                           period_start=start, period_end=end)


def fake_runner():
    return SimpleNamespace(_fetch_extract=lambda source_id,url: ({'URL':url},b'official'))


class PeriodSourceTests(unittest.TestCase):
    def test_application_calendar_boundaries(self):
        # Published grading applies in January/July, not May/November.
        cases = {
            '2026-01-01':'fan2510', '2026-04-30':'fan2510',
            '2026-05-01':'fan2510', '2026-06-30':'fan2510',
            '2026-07-01':'fan2604', '2026-10-31':'fan2604',
            '2026-11-01':'fan2604', '2026-12-31':'fan2604',
            '2027-01-01':'fan2610', '2027-05-01':'fan2610',
            '2027-07-01':'fan2704', '2028-02-29':'fan2710',
        }
        for day, token in cases.items():
            with self.subTest(day=day):
                self.assertEqual(source.official_period_url(date.fromisoformat(day)),
                                 source.BASE + token + '.lzh')

    def test_invalid_date_rejected(self):
        with self.assertRaisesRegex(ValueError, 'TARGET_DATE'):
            source.official_period_url('2026-11-01')

    def test_official_2026_second_half_records(self):
        values={1234:record(),5678:record()}
        self.assertIs(source.validate_period_records(values,date(2026,11,1)),values)

    def test_wrong_future_stale_mixed_or_empty_period_rejected(self):
        invalid = [
            {}, {1:record(2027,1,'2026-05-01','2026-10-31')},
            {1:record(2026,1,'2025-05-01','2025-10-31')},
            {1:record(),2:record(end='2026-10-31')},
        ]
        for values in invalid:
            with self.subTest(values=values), self.assertRaisesRegex(ValueError,'APPLICATION_CONTRACT'):
                source.validate_period_records(values,date(2026,11,1))

    def test_fixed_input_passes_through_existing_boundary(self):
        parsed={1234:record()}; calls=[]
        original_parser=lambda data: parsed
        official=SimpleNamespace(parse_racer_period_bytes=original_parser)
        def inner(runner, target, url, captured):
            calls.append((target,url,captured))
            receipt,member=runner._fetch_extract('racer-period-fan2604',url)
            self.assertEqual(receipt,{'URL':url})
            self.assertEqual(member,b'official')
            self.assertIs(original_parser(member),parsed)
            return 'LOCK_BY_ORIGINAL_RUNNER'
        @wraps(inner)
        def boundary(*args):
            calls.append('AUTHORITY_AND_LOCK_CHECK')
            return inner(*args)
        boundary._g11_daily_authority_boundary=True
        wrapped=source.adapted_period_morning(official,boundary)
        captured='2026-11-01T06:18:00+09:00'
        runner=fake_runner(); original_fetch=runner._fetch_extract
        self.assertEqual(wrapped(runner,date(2026,11,1),source.BASE+'fan2610.lzh',captured),
                         'LOCK_BY_ORIGINAL_RUNNER')
        self.assertEqual(calls,['AUTHORITY_AND_LOCK_CHECK',
            (date(2026,11,1),source.BASE+'fan2604.lzh',captured)])
        self.assertIs(wrapped.__wrapped__,boundary)
        self.assertTrue(wrapped._g11_daily_authority_boundary)
        self.assertIs(official.parse_racer_period_bytes,original_parser)
        self.assertIs(runner._fetch_extract,original_fetch)

    def test_existing_correct_url_remains_identical(self):
        official=SimpleNamespace(parse_racer_period_bytes=lambda _: {})
        expected=source.BASE+'fan2604.lzh'
        wrapped=source.adapted_period_morning(official,lambda _,day,url,at:url)
        self.assertEqual(wrapped(fake_runner(),date(2026,11,1),expected,'actual-clock'),expected)

    def test_historical_calls_not_changed(self):
        parser=lambda _: {}; official=SimpleNamespace(parse_racer_period_bytes=parser)
        wrapped=source.adapted_period_morning(official,lambda *args:args)
        args=(object(),date(2026,5,1),'original-retained-url','original-time')
        self.assertEqual(wrapped(*args),args)
        self.assertIs(official.parse_racer_period_bytes,parser)

    def test_unknown_source_rejected_before_runner(self):
        official=SimpleNamespace(parse_racer_period_bytes=lambda _: {})
        wrapped=source.adapted_period_morning(official,lambda *args:self.fail('called'))
        with self.assertRaisesRegex(ValueError,'UNREVIEWED_SOURCE'):
            wrapped(object(),date(2026,11,1),source.BASE+'fan2504.lzh','actual-clock')

    def test_parser_and_failure_retained_on_error(self):
        parser=lambda _: {}; official=SimpleNamespace(parse_racer_period_bytes=parser)
        state={'TECHNICAL_COUNTS':{'racer-period-fan2610':2,'other-source':2}}
        runner=fake_runner()
        runner.store=SimpleNamespace(ledger=SimpleNamespace(state=lambda:state))
        original_fetch=runner._fetch_extract
        def blocked(*args):
            raise RuntimeError('EXISTING_LOCK_OR_AUTHORITY_GUARD')
        wrapped=source.adapted_period_morning(official,blocked)
        with self.assertRaisesRegex(RuntimeError,'EXISTING_LOCK_OR_AUTHORITY_GUARD'):
            wrapped(runner,date(2026,11,1),source.BASE+'fan2610.lzh','actual-clock')
        self.assertIs(official.parse_racer_period_bytes,parser)
        self.assertIs(runner._fetch_extract,original_fetch)
        self.assertEqual(state,{'TECHNICAL_COUNTS':{'racer-period-fan2610':2,'other-source':2}})

    def test_wrong_period_rejected_before_copied_parser_can_admit_it(self):
        copied_parser=lambda _: {1:record(2027,1,'2026-05-01','2026-10-31')}
        official=SimpleNamespace(parse_racer_period_bytes=copied_parser)
        def original(runner, target, url, captured):
            _,member=runner._fetch_extract('racer-period-fan2604',url)
            self.fail('mismatched records reached the downstream copied parser')
            return copied_parser(member)
        runner=fake_runner(); before=runner._fetch_extract
        with self.assertRaisesRegex(ValueError,'APPLICATION_CONTRACT'):
            source.adapted_period_morning(official,original)(runner,date(2026,11,1),
                source.BASE+'fan2610.lzh','actual-clock')
        self.assertIs(runner._fetch_extract,before)

    def test_no_instance_fetch_attribute_left_after_guard_failure(self):
        class Runner:
            def _fetch_extract(self,*args):
                raise AssertionError('network called')
        runner=Runner(); official=SimpleNamespace(parse_racer_period_bytes=lambda _: {})
        def original(*args):
            raise RuntimeError('AUTHORITY_STOP')
        with self.assertRaisesRegex(RuntimeError,'AUTHORITY_STOP'):
            source.adapted_period_morning(official,original)(runner,date(2026,11,1),
                source.BASE+'fan2610.lzh','actual-clock')
        self.assertNotIn('_fetch_extract',runner.__dict__)


class PrivateBoundaryIntegrationTests(unittest.TestCase):
    def test_real_runtime_guard_and_source_bytes_retained(self):
        try:
            from g11.canonical import official_pipeline as official
        except ModuleNotFoundError:
            if os.environ.get('G11_REQUIRE_PRIVATE_PERIOD_TESTS') == '1':
                raise
            self.skipTest('private runtime not available locally; required in CI')
        from rescue_night_result_parser_v1 import official_input_compatibility
        original=official.CanonicalRunner.run_morning
        parser=official.parse_racer_period_bytes
        path=Path(official.__file__)
        before=hashlib.sha256(path.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            store=official.OfficialArtifactStore.initialize(Path(directory)/'store',
                '2026-11-01',None,'2026-11-01T06:18:00+09:00')
            runner=official.CanonicalRunner(store)
            with official_input_compatibility():
                self.assertTrue(official.CanonicalRunner.run_morning._g11_daily_authority_boundary)
                with self.assertRaisesRegex(Exception,'DAILY_AUTHORITY_UNKNOWN'):
                    runner.run_morning(date(2026,11,1),source.BASE+'fan2610.lzh',
                                       '2026-11-01T06:18:00+09:00')
            self.assertIs(official.CanonicalRunner.run_morning,original)
            self.assertIs(official.parse_racer_period_bytes,parser)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),before)


if __name__ == '__main__':
    unittest.main()
