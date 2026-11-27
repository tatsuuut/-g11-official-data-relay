"""Read only existing CRITICAL results; never print prediction or source bodies."""
from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import re

PREFIX = 'G11_PREDEADLINE_CAPTURE_DIAGNOSTIC='
MAX_RESULT_BYTES = 16 * 1024 * 1024
CODE = re.compile(r'[A-Z][A-Z0-9_:. -]{0,120}')
KNOWN_CODES = frozenset('''PASS CURRENT READY CAPTURED INPUT_UNAVAILABLE
STATUS_NOT_REPORTED UNCLASSIFIED NOT_ATTEMPTED UNAVAILABLE
CRITICAL_CHECKPOINT_PASS PREDEADLINE PREDEADLINE_REFORECAST_COMPLETE
PREDEADLINE_REFORECAST_PARTIAL_WAITING PARTIAL_PREDICTION_FAILURES_NOT_PROMOTED
PARTIAL_MISSED_NOT_PROMOTED PREDEADLINE_SOURCE_COMPLETE
MORNING_PREDICTION BEFOREINFO ODDS3T PREDICTION_WINDOW_CLOSED
V53_MORNING_OR_BEFOREINFO_MISSING V53_MORNING_RIGHTS_UNRESOLVED
FORMAL_RACE_EXCLUDED OFFICIAL_UNAVAILABLE OFFICIAL_VENUE_DAY_CANCELLED
SOURCE_DEADLINE_EXPIRED SOURCE_CONTENT_PAUSED SERIAL_PACING LEDGER_BUSY
HOST_PAUSED PIPELINE_STOPPED GLOBAL_STOPPED ODDS_PIPELINE_STOPPED
ODDS_PIPELINE_PAUSED SOURCE_CONTENT_RETRY_EXHAUSTED SOURCE_TECHNICAL_RETRY_EXHAUSTED
LIVE_RECOVERY_ADMISSION_REQUIRED LIVE_RECOVERY_CLAIM_DEFERRED
LIVE_RECOVERY_CLAIM_SPENT LIVE_RECOVERY_DEADLINE_TOO_CLOSE
HTTP_403 HTTP_429 HTTP_5XX TIMEOUT NETWORK_ERROR
TRUE_AI_LIVE_BEFORE_COMMIT_REJECTED MORNING_RETAINED
LIVE_WEATHER_INFERENCE SELECT_AND_VERIFY FINAL_CONTRACT
LIVE_WEATHER_SOURCE_RECEIPT LIVE_WEATHER_PARSED_SNAPSHOT_BINDING
PUBLISHED_LIVE_ENGINE_UNSUPPORTED LIVE_WEATHER_RESPONSE_HASH
CURRENT_P3_LIVE_DEADLINE_EXPIRED
ALL_ALTERNATE_PROVIDERS_PAUSED SHARED_MARKET_CAPTURE_IN_PROGRESS
PRIMARY_ALREADY_SAVED ALTERNATE_ALREADY_SAVED VERIFIED_ALTERNATE_ALREADY_SAVED
INDEPENDENT_PUBLISHED_PREVIEW INDEPENDENT_BOATCAST_PREVIEW
OMURA_OFFICIAL_PUBLISHED_MARKET BOATCAST_PUBLISHED_MARKET
SEPARATE_WORKER_UNVERIFIED'''.split())
ERROR_CLASSES = {
    'CURRENT_P3_': 'CURRENT_P3_VALIDATION_FAILED',
    'FORMAL_': 'FORMAL_ADMISSION_FAILED',
    'BOATCAST_PREVIEW_': 'BOATCAST_PREVIEW_VALIDATION_FAILED',
    'PREVIEW_': 'PREVIEW_VALIDATION_FAILED',
    'TRUE_ONLY_': 'TRUE_ONLY_VALIDATION_FAILED',
    'TRUE_AI_': 'TRUE_AI_VALIDATION_FAILED',
    'LIVE_WEATHER_': 'LIVE_WEATHER_VALIDATION_FAILED',
    'PREDEADLINE_': 'PREDEADLINE_VALIDATION_FAILED',
    'ABEKEN_V53_': 'ABEKEN_V53_VALIDATION_FAILED',
    'V53_': 'V53_VALIDATION_FAILED',
}
ERROR_TYPES = frozenset(('ValueError', 'KeyError', 'TypeError', 'OSError',
    'CurrentGrowthP3RuntimeError', 'FormalP3InvocationError',
    'FormalInputAdmissionError', 'FormalPredictionEngineError', 'TrueOnlyModelError',
    'TrueAILiveCandidateError'))
PROVIDERS = {'race.boatcast.jp': 'BOATCAST', 'boaters-boatrace.com': 'PUBLISHED_DAILY_JSON',
    'www.boaters-boatrace.com': 'PUBLISHED_DAILY_JSON',
    'www.omurakyotei.jp': 'OMURA_OFFICIAL', 'www.boatrace.jp': 'NATIONAL_OFFICIAL'}


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def checked_day(value):
    require(isinstance(value, str) and re.fullmatch(r'20\d{2}-\d{2}-\d{2}', value), 'INVALID_DAY')
    require(date.fromisoformat(value).isoformat() == value, 'INVALID_DAY')
    return value


def safe_code(value):
    if not isinstance(value, str) or CODE.fullmatch(value) is None:
        return 'UNCLASSIFIED'
    if value in KNOWN_CODES:
        return value
    # Exception suffixes may contain arbitrary data. Report only a closed
    # producer class, never any of that suffix, even when entirely uppercase.
    return next((label for prefix, label in ERROR_CLASSES.items()
                 if value.startswith(prefix)), 'UNCLASSIFIED')


def race_key(value, day):
    return (value if isinstance(value, str) and re.fullmatch(
        day.replace('-', '') + r'-(?:0[1-9]|1\d|2[0-4])-(?:0[1-9]|1[0-2])', value) else None)


def records(value, day):
    output = []
    if not isinstance(value, list):
        return output
    for item in value:
        row = item if isinstance(item, dict) else {'RACE_KEY': item}
        key = race_key(row.get('RACE_KEY', row.get('RACE_ID')), day)
        if key is None:
            continue
        clean = {'race_key': key}
        for field in ('STATUS', 'REASON', 'FAILURE_STAGE', 'ERROR_REASON'):
            if field in row:
                clean[field.lower()] = safe_code(row[field])
        if 'ERROR_TYPE' in row:
            value = row['ERROR_TYPE']
            clean['error_type'] = (value if isinstance(value, str) and value in ERROR_TYPES
                                   else 'UNCLASSIFIED')
        if isinstance(row.get('MISSING'), list):
            clean['missing'] = [safe_code(code) for code in row['MISSING']]
        if 'LIVE_SNAPSHOT_WRITTEN' in row:
            clean['live_snapshot_written'] = row['LIVE_SNAPSHOT_WRITTEN'] is True
        if 'P3_SNAPSHOT_WRITE' in row:
            clean['p3_snapshot_write'] = safe_code(row['P3_SNAPSHOT_WRITE'])
        output.append(clean)
    return output


def section(value, day, fields):
    value = value if isinstance(value, dict) else {}
    out = {'status': safe_code(value.get('STATUS'))}
    for field in fields:
        rows = value.get(field)
        if isinstance(rows, list):
            out[field.lower()] = {'count': len(rows), 'races': records(rows, day)}
    return out


def providers(value, day):
    """The adapters return race-keyed status dictionaries, never raw bodies."""
    if not isinstance(value, dict):
        return []
    output = []
    for key, item in value.items():
        if race_key(key, day) is None or not isinstance(item, dict):
            continue
        source = item.get('source')
        row = {'race_key': key,
               'status': safe_code(item.get('status', item.get('STATUS'))),
               'provider': PROVIDERS.get(source, 'UNCLASSIFIED') if isinstance(source, str)
                           else 'UNCLASSIFIED'}
        if 'reason' in item:
            row['reason'] = safe_code(item['reason'])
        # Successful preview outputs have a verified snapshot digest instead
        # of an explicit status. Keep only that presence fact, not the digest.
        row['snapshot_present'] = (isinstance(item.get('snapshot_sha256'), str)
                                  and re.fullmatch(r'[a-f0-9]{64}', item['snapshot_sha256']) is not None)
        output.append(row)
    return output


def summarize(result, day):
    day = checked_day(day)
    require(isinstance(result, dict), 'RESULT_SHAPE')
    critical = result.get('CRITICAL')
    require(isinstance(critical, dict) and critical.get('OPERATIONAL_DATE') == day
            and result.get('OPERATIONAL_DATE_JST', day) == day, 'DAY_BINDING')
    acquisition = result.get('ACQUISITION') or {}
    require(isinstance(acquisition, dict), 'RESULT_SHAPE')
    summary = {
        'schema': 'G11_PREDEADLINE_CAPTURE_DIAGNOSTIC_V1',
        'day': day, 'status': 'READ_ONLY_RESULT',
        'critical_status': safe_code(critical.get('CRITICAL_CHECKPOINT_STATUS')),
        'acquisition': section(acquisition, day, (
            'ACQUIRED', 'MISSED', 'SKIPPED', 'UNAVAILABLE', 'REJECTED',
            'RETRYABLE_FAILURES', 'TASK_DEFERRED')),
        'prediction': section(result.get('PREDICTION'), day, (
            'COMPLETED', 'CURRENT', 'WAITING', 'FAILED', 'UNAVAILABLE',
            'ABEKEN_V53_READY', 'ABEKEN_V53_FORMAL_COMPLETED',
            'ABEKEN_V53_WAITING', 'ABEKEN_V53_FAILED', 'ABEKEN_V53_BLOCKED')),
        'p3_live': section(result.get('P3_LIVE'), day, (
            'READY_RACES', 'COMPLETED', 'SKIPPED_DEADLINE', 'SKIPPED_TRUE_AI_LIVE')),
        'wild': {'status': 'SEPARATE_WORKER_UNVERIFIED'},
        'independent_previews': providers(acquisition.get('INDEPENDENT_PREVIEWS'), day),
        'stadium_markets': providers(acquisition.get('STADIUM_MARKETS'), day),
    }
    if isinstance(acquisition.get('DEFERRED'), dict):
        summary['acquisition']['deferred_reason'] = safe_code(acquisition['DEFERRED'].get('REASON'))
    for field in ('LAST_TIMELY_SOURCE_COUNT', 'LAST_TIMELY_P3_LIVE_COUNT', 'LAST_TIMELY_V53_COUNT',
                  'LAST_TIMELY_ALTERNATE_MARKET_COUNT'):
        value = critical.get(field)
        if type(value) is int and value >= 0:
            summary[field.lower()] = value
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--result', type=Path, required=True)
    parser.add_argument('--day', required=True)
    args = parser.parse_args(argv)
    try:
        day = checked_day(args.day)
        require(args.result.is_file() and not args.result.is_symlink()
                and args.result.stat().st_size <= MAX_RESULT_BYTES, 'RESULT_FILE')
        summary = summarize(json.loads(args.result.read_bytes()), day)
    except (OSError, ValueError, KeyError, TypeError):
        # Optional diagnostics never disclose arbitrary error text or interrupt
        # canonical acquisition, prediction, persistence or delivery.
        summary = {'schema': 'G11_PREDEADLINE_CAPTURE_DIAGNOSTIC_V1',
                   'status': 'UNAVAILABLE', 'reason': 'RESULT_VALIDATION_FAILED',
                   'wild': {'status': 'SEPARATE_WORKER_UNVERIFIED'}}
    print(PREFIX + json.dumps(summary, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
