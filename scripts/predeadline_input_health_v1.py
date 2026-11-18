"""Read-only diagnostics for saved input gaps and official deadline changes.

Checkpoint persistence is independent of input coverage. Never fetch, predict,
rewrite a schedule/LOCK, or extend the day's admitted prediction boundary here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import wraps
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import parse_qs, urlsplit

JST = timezone(timedelta(hours=9))
SCHEMA = 'G11_PREDEADLINE_INPUT_HEALTH_V1'
MAX_DETAILS = 24
KEY = re.compile(r'\d{8}-(?:0[1-9]|1\d|2[0-4])-(?:0[1-9]|1[0-2])')
CODE = re.compile(r'[A-Z][A-Z0-9_:.-]{0,159}')


def host_pause(deferred, day, now):
    if not isinstance(deferred, dict) or deferred.get('REASON') != 'HOST_PAUSED':
        return None
    try:
        retry = datetime.fromisoformat(deferred['RETRY_AT_JST'])
    except (KeyError, ValueError, TypeError):
        return None
    if (now.utcoffset() != timedelta(hours=9) or now.date().isoformat() != day
            or retry.utcoffset() != timedelta(hours=9) or retry <= now):
        return None
    return {'date': day, 'reason': 'HOST_PAUSED', 'observed_at_jst': now.isoformat(timespec='seconds'),
            'retry_at_jst': retry.isoformat(timespec='seconds')}


def saved_beforeinfo_pause(ledger, day, now, duration):
    """Recover the observed start from the checked request chain, including just after release."""
    state = ledger.state()
    retry = state['PAUSE_UNTIL'].get('www.boatrace.jp')
    if (state['STOPPED'] or state['GENERAL_HTTP_403_COUNT'] != 1 or retry is None
            or retry < now - timedelta(minutes=15)):
        return None
    start = retry - timedelta(seconds=duration)
    if start.date().isoformat() != day or start > now:
        return None
    events = ledger.events()
    for i in range(len(events) - 1, 0, -1):
        event, prior = events[i], events[i-1]
        if (event['TYPE'] == 'OUTCOME_RECORDED' and event['PAYLOAD']['OUTCOME'] == 'HTTP_403'
                and datetime.fromisoformat(event['AT_JST']) == start and prior['TYPE'] == 'REQUEST_STARTED'
                and prior['PAYLOAD']['REQUEST_ID'] == event['PAYLOAD']['REQUEST_ID']
                and prior['PAYLOAD']['HOST'] == 'www.boatrace.jp' and prior['PAYLOAD']['SOURCE_ID'].startswith('beforeinfo-')):
            return host_pause({'REASON':'HOST_PAUSED', 'RETRY_AT_JST':retry.isoformat()}, day, start)
    return None


class ScheduleTable(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.table, self.row, self.cell = [], None, None, None
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            self.depth += 1
            if self.depth == 1: self.table = []
        if self.depth != 1: return
        if tag == 'tr': self.row = []
        if tag in ('th', 'td'): self.cell = {'text': '', 'links': []}
        if tag == 'a' and self.cell is not None:
            self.cell['links'].append(dict(attrs).get('href', ''))

    def handle_endtag(self, tag):
        if self.depth == 1:
            if tag in ('th', 'td') and self.cell is not None:
                if self.row is not None: self.row.append(self.cell)
                self.cell = None
            if tag == 'tr' and self.row is not None:
                self.table.append(self.row)
                self.row = None
            if tag == 'table':
                self.tables.append(self.table)
                self.table = None
        if tag == 'table': self.depth = max(0, self.depth - 1)

    def handle_data(self, data):
        if self.depth == 1 and self.cell is not None:
            self.cell['text'] += data


def official_schedule(raw, source_url, day, venue):
    """Require an exact day/venue and twelve bound race links in one table."""
    if type(raw) is not bytes or not 0 < len(raw) <= 2_000_000:
        raise ValueError('HEALTH_RESPONSE_SIZE')
    source = urlsplit(source_url)
    expected_day = datetime.fromisoformat(day).strftime('%Y%m%d')
    query = parse_qs(source.query)
    if (source.scheme != 'https' or source.netloc != 'www.boatrace.jp'
            or source.path != '/owpc/pc/race/beforeinfo'
            or query.get('hd') != [expected_day] or query.get('jcd') != [venue]
            or len(query.get('rno', [])) != 1
            or not re.fullmatch(r'(?:[1-9]|1[0-2])', query['rno'][0])):
        raise ValueError('HEALTH_SOURCE_IDENTITY')
    parser = ScheduleTable()
    parser.feed(raw.decode('utf-8'))
    clean = lambda cell: re.sub(r'\s+', '', cell['text'])
    candidates = []
    for table in parser.tables:
        headers = [row for row in table if [clean(c) for c in row] ==
                   ['レース'] + [f'{i}R' for i in range(1, 13)]]
        clocks = [row for row in table if len(row) == 13 and clean(row[0]) == '締切予定時刻']
        if len(headers) != 1 or len(clocks) != 1: continue
        for number, cell in enumerate(headers[0][1:], 1):
            if len(cell['links']) != 1: raise ValueError('HEALTH_SCHEDULE_LINK')
            link = urlsplit(cell['links'][0])
            q = parse_qs(link.query)
            if (link.netloc not in ('', 'www.boatrace.jp')
                    or link.scheme not in ('', 'https') or link.path != source.path
                    or q != {'hd': [expected_day], 'jcd': [venue], 'rno': [str(number)]}):
                raise ValueError('HEALTH_SCHEDULE_IDENTITY')
        values = [clean(cell) for cell in clocks[0][1:]]
        if not all(re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value) for value in values):
            raise ValueError('HEALTH_SCHEDULE_TIME')
        stamps = [day + 'T' + value + ':00+09:00' for value in values]
        if any(a >= b for a, b in zip(stamps, stamps[1:])):
            raise ValueError('HEALTH_SCHEDULE_ORDER')
        candidates.append(stamps)
    if len(candidates) != 1: raise ValueError('HEALTH_SCHEDULE_UNIQUE')
    return candidates[0]


def _safe_code(value):
    return value if isinstance(value, str) and CODE.fullmatch(value) else 'UNCLASSIFIED_INPUT_FAILURE'


def _plan(artifact, day):
    morning = artifact.load_verified_snapshot('morning', 'official-day-input')['SNAPSHOT']['PAYLOAD']
    if morning['OPERATIONAL_DATE_JST'] != day:
        raise ValueError('HEALTH_MORNING_DAY')
    tasks = {}
    for row in morning['PREDEADLINE_PLAN']:
        key, kind, sid = row['RACE_KEY'], row['KIND'], row['SOURCE_ID']
        deadline = datetime.fromisoformat(row['MUST_FINISH_BEFORE_JST'])
        if (not KEY.fullmatch(key) or key[:8] != day.replace('-', '')
                or kind not in ('BEFOREINFO', 'ODDS3T') or sid != kind.lower() + '-' + key
                or sid in tasks or deadline.utcoffset() != timedelta(hours=9)
                or deadline.date().isoformat() != day):
            raise ValueError('HEALTH_PLAN_IDENTITY')
        tasks[sid] = row
    return tasks


def latest_rejection(artifact, task, day, now):
    """Revalidate retained receipt and bytes; do not promote rejected input."""
    sid, key = task['SOURCE_ID'], task['RACE_KEY']
    parent = artifact.root / 'rejected-responses' / sid
    if parent.is_symlink(): raise ValueError('HEALTH_REJECTION_SYMLINK')
    receipts = []
    paths = list(parent.glob('request-*'))
    if len(paths) > 64: raise ValueError('HEALTH_REJECTION_BOUND')
    for path in paths:
        if path.is_symlink(): raise ValueError('HEALTH_REJECTION_SYMLINK')
        receipt = artifact.rejected_response_receipt(sid, path.name)
        if receipt is None: continue
        at = datetime.fromisoformat(receipt['CAPTURED_AT_JST'])
        if (at.utcoffset() != timedelta(hours=9) or at.date().isoformat() != day
                or at > now or receipt['URL'] != task['URL']):
            raise ValueError('HEALTH_REJECTION_BINDING')
        receipts.append((at, path, receipt))
    if not receipts: return {}
    at, path, receipt = max(receipts, key=lambda item: (item[0], item[1].name))
    result = {'rejection_reason': _safe_code(receipt['REJECTION_REASON']),
              'evidence_sha256': receipt['SHA256'], 'captured_at_jst': at.isoformat()}
    if task['KIND'] == 'BEFOREINFO':
        try:
            schedule = official_schedule((path / 'response.bin').read_bytes(), receipt['URL'], day, key[9:11])
            official = schedule[int(key[-2:]) - 1]
            if official != task['MUST_FINISH_BEFORE_JST']:
                result.update(official_deadline_jst=official,
                              schedule_status='OFFICIAL_DEADLINE_CHANGED',
                              observed_before_saved_cutoff=at < datetime.fromisoformat(task['MUST_FINISH_BEFORE_JST']))
        except (ValueError, UnicodeError):
            result['schedule_status'] = 'UNVERIFIED'
    return result


def summarize(artifact, day, acquisition, *, now=None):
    now = now or datetime.now(JST)
    if now.utcoffset() != timedelta(hours=9): raise ValueError('HEALTH_CLOCK')
    tasks = _plan(artifact, day)
    issues = {}
    official_unavailable = set()
    for category in ('MISSED', 'REJECTED', 'TASK_DEFERRED', 'RETRYABLE_FAILURES', 'UNAVAILABLE'):
        for item in acquisition.get(category, []):
            sid = item if isinstance(item, str) else item['SOURCE_ID']
            if sid not in tasks: raise ValueError('HEALTH_ISSUE_BINDING')
            task = tasks[sid]
            if isinstance(item, dict) and item.get('RACE_KEY') != task['RACE_KEY']:
                raise ValueError('HEALTH_ISSUE_RACE')
            if (category == 'UNAVAILABLE' and isinstance(item, dict)
                    and item.get('REASON') == 'OFFICIAL_WITHDRAWAL_UNSUPPORTED_FIELD_SIZE'
                    and re.fullmatch(r'[a-f0-9]{64}', str(item.get('AVAILABILITY_SNAPSHOT_SHA256', '')))):
                # The existing collector has verified the official exclusion.
                # Do not call a legitimate withdrawal an acquisition outage.
                official_unavailable.add(task['RACE_KEY'])
                continue
            issue = issues.setdefault(sid, {'source_id': sid, 'race_key': task['RACE_KEY'],
                'kind': task['KIND'], 'saved_deadline_jst': task['MUST_FINISH_BEFORE_JST'],
                'categories': [], 'reasons': []})
            issue['categories'].append(category)
            reason = 'SOURCE_DEADLINE_EXPIRED' if isinstance(item, str) else _safe_code(item.get('REASON'))
            if reason not in issue['reasons']: issue['reasons'].append(reason)
    details = sorted(issues.values(), key=lambda row: row['source_id'])[:MAX_DETAILS]
    for row in details:
        try: row.update(latest_rejection(artifact, tasks[row['source_id']], day, now))
        except (ValueError, KeyError, OSError, RuntimeError, TypeError): row['evidence_status'] = 'UNVERIFIED'
    terminal = any('MISSED' in row['categories'] or 'UNAVAILABLE' in row['categories'] for row in issues.values())
    return {'schema': SCHEMA, 'operational_date_jst': day,
            'status': 'PARTIAL' if terminal else 'DEFERRED' if issues or acquisition.get('DEFERRED') else 'PASS',
            'host_pause': host_pause(acquisition.get('DEFERRED'), day, now),
            'scope': 'OFFICIAL_INPUTS_ONLY_NOT_ENGINE_OR_DEVICE_COMPLETENESS',
            'source_issue_count': len(issues),
            'race_issue_count': len({row['race_key'] for row in issues.values()}),
            'official_unavailable_race_count': len(official_unavailable),
            'omitted_source_details': max(0, len(issues) - len(details)), 'issues': details,
            'prediction_boundary_changed': False, 'prediction_generated': False}


def report(artifact, day, acquisition):
    value = summarize(artifact, day, acquisition)
    print('G11_PREDEADLINE_INPUT_HEALTH=' + json.dumps(value, ensure_ascii=False, sort_keys=True), flush=True)
    pause = value['host_pause']
    if pause or acquisition.get('MISSED'):
        try:
            from g11.canonical.official_pipeline import FIRST_403_PAUSE_SECONDS
            recorded = saved_beforeinfo_pause(artifact.ledger, day, datetime.now(JST), FIRST_403_PAUSE_SECONDS)
            if recorded and (not pause or pause['retry_at_jst'] == recorded['retry_at_jst']):
                pause = recorded
        except (ImportError, AttributeError, KeyError, ValueError, OSError, RuntimeError, TypeError):
            pass
    if pause:
        print('G11_VERIFIED_HOST_PAUSE=' + json.dumps(pause, sort_keys=True), flush=True)
        if value['host_pause']:
            print('::warning title=G11 official input paused::Next eligible request at ' + pause['retry_at_jst'], flush=True)
        workspace = os.environ.get('GITHUB_WORKSPACE')
        if workspace and os.environ.get('GITHUB_RUN_ID'):
            path = Path(workspace) / '.relay-output' / 'host-pause.json'
            path.write_text(json.dumps({**pause, 'run_id': os.environ['GITHUB_RUN_ID']}))
    if value['status'] == 'PARTIAL':
        print('::warning title=G11 predeadline input gaps::' +
              str(value['race_issue_count']) + ' races have missing or unavailable official inputs; checkpoint success is not full operation success.', flush=True)
    return value


def publish_host_pause(day, token=None):
    from urllib.parse import urlencode
    from urllib.request import Request, urlopen
    from daily_runtime_delivery_v1 import ORIGIN
    root = Path(os.environ['GITHUB_WORKSPACE'])
    pause = json.loads((root / '.relay-output' / 'host-pause.json').read_bytes())
    state = json.loads((root / '.relay-state' / 'relay-state.json').read_bytes())['CRITICAL']
    if (os.environ['G11_APP_ORIGIN'] != ORIGIN or pause['date'] != day or state['OPERATIONAL_DATE'] != day
            or pause['run_id'] != os.environ['GITHUB_RUN_ID'] or state['LAST_CRITICAL_RUN_ID'] != pause['run_id']
            or state['CRITICAL_CHECKPOINT_STATUS'] != 'PASS' or not re.fullmatch('[0-9a-f]{64}', state['AUTHORITY_SHA256'])):
        raise ValueError('INPUT_HEALTH_PUBLICATION_BINDING')
    pause['authority_sha256'] = state['AUTHORITY_SHA256']
    if token is None:
        oidc = Request(os.environ['ACTIONS_ID_TOKEN_REQUEST_URL'] + '&' + urlencode({'audience': os.environ['G11_OIDC_AUDIENCE']}),
                       headers={'Authorization': 'bearer ' + os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']})
        with urlopen(oidc, timeout=5) as response:
            token = json.load(response)['value']
    request = Request(ORIGIN + '/api/g11-input-pause', data=json.dumps(pause).encode(),
                      headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    with urlopen(request, timeout=6) as response:
        saved = json.load(response)
    if saved.get('retry_at_jst') != pause['retry_at_jst'] or saved.get('date') != day:
        raise ValueError('INPUT_HEALTH_PUBLICATION_READBACK')
    print('G11_INPUT_PAUSE_PUBLISHED=' + pause['retry_at_jst'], flush=True)


def publish_pause_if_observed(root, day, token=None):
    if (root / '.relay-output' / 'host-pause.json').is_file():
        try:
            publish_host_pause(day, token)
        except Exception as exc:
            print('::warning title=G11 official input pause::Status publication unverified: ' + type(exc).__name__, flush=True)


def observed_capture(original, artifact_factory, day):
    @wraps(original)
    def capture(store):
        acquisition = original(store)
        try:
            report(artifact_factory(store), day, acquisition)
        except Exception as exc:
            print('G11_PREDEADLINE_INPUT_HEALTH_UNVERIFIED=' + type(exc).__name__, flush=True)
        return acquisition
    return capture


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--state-root', type=Path, required=True)
    parser.add_argument('--day', required=True)
    parser.add_argument('--source', type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source.resolve()))
    from g11.canonical.official_pipeline import CanonicalRunner, OfficialArtifactStore
    artifact = OfficialArtifactStore(args.state_root.resolve() / 'store')
    tasks = _plan(artifact, args.day)
    verified = artifact.load_verified_snapshot('morning', 'official-day-input')
    from rescue_night_result_parser_v1 import official_input_compatibility
    with official_input_compatibility():
        _, eligible, _ = CanonicalRunner._formal_prediction_scope(verified, failure_prefix='HEALTH_FORMAL_SCOPE')
    tasks = {sid: task for sid, task in tasks.items() if task['RACE_KEY'] in set(eligible)}
    now = datetime.now(JST)
    # Existing saved snapshots, not current network pages, decide missingness.
    missed = [sid for sid, task in tasks.items()
              if now >= datetime.fromisoformat(task['MUST_FINISH_BEFORE_JST'])
              and not (artifact.root / 'snapshots/predeadline' / (sid + '.json')).exists()]
    report(artifact, args.day, {'MISSED': missed})


if __name__ == '__main__': main()
