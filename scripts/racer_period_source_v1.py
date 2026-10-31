"""Select the official handbook's application half-year before MORNING input.

Selection and record validation only. The retained runtime source, authority,
request ledger, responses, predictions and LOCK validation are not rewritten.
"""
from datetime import date
from functools import wraps
import json
import sys

EFFECTIVE_FROM = date(2026, 11, 1)
BASE = 'https://www.boatrace.jp/static_extra/pc_static/download/data/kibetsu/'


def period_contract(target):
    if type(target) is not date:
        raise ValueError('RACER_PERIOD_TARGET_DATE')
    if target.month <= 6:
        return (target.year, 1, f'{target.year-1}-05-01', f'{target.year-1}-10-31')
    return (target.year, 2, f'{target.year-1}-11-01', f'{target.year}-04-30')


def official_period_url(target):
    _, _, _, end = period_contract(target)
    return BASE + 'fan' + end[2:4] + end[5:7] + '.lzh'


def legacy_period_url(target):
    if 5 <= target.month <= 10:
        year, month = target.year, 4
    elif target.month >= 11:
        year, month = target.year, 10
    else:
        year, month = target.year-1, 10
    return BASE + f'fan{year%100:02d}{month:02d}.lzh'


def validate_period_records(periods, target):
    expected = period_contract(target)
    if not periods or any(
        (p.period_year, p.period_half, p.period_start, p.period_end) != expected
        for p in periods.values()
    ):
        raise ValueError('RACER_PERIOD_APPLICATION_CONTRACT')
    return periods


def adapted_period_morning(official, original):
    """Keep the complete existing authority/LOCK wrapper inside this adapter."""
    @wraps(original)
    def invoke(self, target_date, racer_period_url, captured_at_jst):
        if target_date < EFFECTIVE_FROM:
            return original(self, target_date, racer_period_url, captured_at_jst)
        expected = official_period_url(target_date)
        if racer_period_url not in {expected, legacy_period_url(target_date)}:
            raise ValueError('RACER_PERIOD_UNREVIEWED_SOURCE')
        fetch_extract = self._fetch_extract
        had_instance_fetch = '_fetch_extract' in self.__dict__
        instance_fetch = self.__dict__.get('_fetch_extract')
        expected_source = 'racer-period-' + expected.rsplit('/', 1)[1][:-4]
        def validated_fetch(source_id, url):
            if source_id.startswith('racer-period-') and (source_id != expected_source or url != expected):
                raise ValueError('RACER_PERIOD_FETCH_BINDING')
            receipt, member = fetch_extract(source_id, url)
            if source_id == expected_source:
                validate_period_records(official.parse_racer_period_bytes(member), target_date)
            return receipt, member
        ledger = getattr(getattr(self, 'store', None), 'ledger', None)
        prior_failures = ({k: v for k, v in ledger.state()['TECHNICAL_COUNTS'].items()
                           if k.startswith('racer-period-')} if ledger else {})
        print(json.dumps({'G11_RACER_PERIOD_APPLICATION_V1': {
            'date': target_date.isoformat(), 'requested_url': racer_period_url,
            'selected_url': expected, 'period_contract': period_contract(target_date),
            'ledger_reset': False, 'authority_rewritten': False,
            'retained_period_failure_counts': prior_failures,
        }}, sort_keys=True), file=sys.stderr)
        try:
            self._fetch_extract = validated_fetch
            return original(self, target_date, expected, captured_at_jst)
        finally:
            if had_instance_fetch:
                self._fetch_extract = instance_fetch
            else:
                del self._fetch_extract
    return invoke
