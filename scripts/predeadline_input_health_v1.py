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
import subprocess
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit

JST = timezone(timedelta(hours=9))
SCHEMA = 'G11_PREDEADLINE_INPUT_HEALTH_V1'
MAX_DETAILS = 24
KEY = re.compile(r'\d{8}-(?:0[1-9]|1\d|2[0-4])-(?:0[1-9]|1[0-2])')
CODE = re.compile(r'[A-Z][A-Z0-9_:.-]{0,159}')
RECOVERY_POLICY = 'G11_LIVE_REFUSAL_RECOVERY_V1'


def recovery_policy(state):
    """Only the versioned native ledger can supply recovery incident metadata."""
    if 'REQUEST_POLICY' not in state:
        return False
    if state['REQUEST_POLICY'] != RECOVERY_POLICY:
        raise ValueError('HEALTH_REQUEST_POLICY')
    return True


def recorded_incident(ledger, day, now, recorded_at):
    """Bind a native transition timestamp to its checked request/outcome pair."""
    at = datetime.fromisoformat(recorded_at)
    if (now.utcoffset() != timedelta(hours=9) or now.date().isoformat() != day
            or at.utcoffset() != timedelta(hours=9) or at.date().isoformat() != day or at > now
            or at.isoformat(timespec='seconds') != recorded_at):
        raise ValueError('HEALTH_INCIDENT_TIME')
    active, matches = None, []
    for event in ledger.events():
        payload = event['PAYLOAD']
        if event['TYPE'] == 'REQUEST_STARTED':
            if active is not None or not all(isinstance(payload.get(k), str) and payload[k]
                                            for k in ('REQUEST_ID', 'SOURCE_ID', 'HOST')):
                raise ValueError('HEALTH_INCIDENT_REQUEST')
            active = payload
        elif event['TYPE'] == 'OUTCOME_RECORDED' and active and payload['REQUEST_ID'] == active['REQUEST_ID']:
            if event['AT_JST'] == recorded_at:
                matches.append((active, payload))
            active = None
        else:
            raise ValueError('HEALTH_INCIDENT_BINDING')
    if len(matches) != 1:
        raise ValueError('HEALTH_INCIDENT_EVENT')
    return at, *matches[0]


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


def saved_beforeinfo_pause(ledger, day, now, duration, *, state=None):
    """Recover the observed start from the checked request chain, including just after release."""
    if state is None:
        state = ledger.state()
    versioned = recovery_policy(state)
    retry = state['PAUSE_UNTIL'].get('www.boatrace.jp')
    if versioned:
        if state['STOPPED'] or retry is None or retry < now - timedelta(minutes=15):
            return None
        start, request, outcome = recorded_incident(ledger, day, now,
            state['PAUSE_STARTED_AT_JST']['www.boatrace.jp'])
        if (retry.utcoffset() != timedelta(hours=9) or not start < retry
                or retry - start > timedelta(hours=24)):
            raise ValueError('HEALTH_PAUSE_PUBLICATION_BOUNDARY')
        if request['HOST'] != 'www.boatrace.jp':
            raise ValueError('HEALTH_PAUSE_HOST')
        if outcome['OUTCOME'] != 'HTTP_403' or not request['SOURCE_ID'].startswith('beforeinfo-'):
            return None
        key = request['SOURCE_ID'][len('beforeinfo-'):]
        if not KEY.fullmatch(key) or key[:8] != day.replace('-', ''):
            raise ValueError('HEALTH_PAUSE_SOURCE_DAY')
        return host_pause({'REASON': 'HOST_PAUSED', 'RETRY_AT_JST': retry.isoformat()}, day, start)
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


def saved_pipeline_stop(ledger, day, now, state):
    """Date the first terminal transition in the already verified daily request chain."""
    versioned = recovery_policy(state)
    if state['STOPPED'] is not True or now.date().isoformat() != day or now.utcoffset() != timedelta(hours=9):
        return None
    if versioned:
        at, _, outcome = recorded_incident(ledger, day, now, state['STOPPED_AT_JST'])
        if outcome['OUTCOME'] not in {'HTTP_403', 'HTTP_429', 'HTTP_5XX', 'HTTP_OTHER',
                                     'TIMEOUT', 'NETWORK_ERROR', 'CONTENT_INVALID'}:
            raise ValueError('HEALTH_STOP_OUTCOME')
        return {'date': day, 'reason': 'PIPELINE_STOPPED', 'observed_at_jst': at.isoformat(timespec='seconds')}
    counts = {'HTTP_403': 0, 'HTTP_429': 0}
    thresholds = {'HTTP_403': 2, 'HTTP_429': 3}
    active = None
    for event in ledger.events():
        payload = event['PAYLOAD']
        if event['TYPE'] == 'REQUEST_STARTED':
            active = payload
            continue
        if event['TYPE'] != 'OUTCOME_RECORDED' or not active or payload['REQUEST_ID'] != active['REQUEST_ID']:
            return None
        source = active['SOURCE_ID']
        active = None
        outcome = payload['OUTCOME']
        if outcome not in counts or source.startswith('odds3t-'):
            continue
        counts[outcome] += 1
        if counts[outcome] == thresholds[outcome]:
            at = datetime.fromisoformat(event['AT_JST'])
            if at.utcoffset() == timedelta(hours=9) and at.date().isoformat() == day and at <= now:
                return {'date': day, 'reason': 'PIPELINE_STOPPED', 'observed_at_jst': at.isoformat(timespec='seconds')}
            return None
    return None


class ScheduleTable(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.table, self.row, self.cell = [], None, None, None
        self.depth = 0
        self.in_day_list = False
        self.active_day = None
        self.active_days = []

    def handle_starttag(self, tag, attrs):
        classes = dict(attrs).get('class', '').split()
        if tag == 'ul' and 'tab2_tabs' in classes:
            self.in_day_list = True
        if tag == 'li' and self.in_day_list and 'is-active2' in classes:
            self.active_day = []
        if tag == 'table':
            self.depth += 1
            if self.depth == 1: self.table = []
        if self.depth != 1: return
        if tag == 'tr': self.row = []
        if tag in ('th', 'td'): self.cell = {'text': '', 'links': []}
        if tag == 'a' and self.cell is not None:
            self.cell['links'].append(dict(attrs).get('href', ''))

    def handle_endtag(self, tag):
        if tag == 'li' and self.active_day is not None:
            self.active_days.append(''.join(''.join(self.active_day).split()))
            self.active_day = None
        if tag == 'ul':
            self.in_day_list = False
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
        if self.active_day is not None:
            self.active_day.append(data)
        if self.depth == 1 and self.cell is not None:
            self.cell['text'] += data


def _verified_schedule(raw, source_url, day, venue):
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
    return candidates[0], parser.active_days


def official_schedule(raw, source_url, day, venue):
    return _verified_schedule(raw, source_url, day, venue)[0]


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
            schedule, active_days = _verified_schedule(
                (path / 'response.bin').read_bytes(), receipt['URL'], day, key[9:11])
            if (key.endswith('-01') and parse_qs(urlsplit(receipt['URL']).query)['rno'] == ['1']
                    and receipt.get('FINAL_URL') == receipt['URL']
                    and active_days == [f'{at.month}月{at.day}日中止']
                    and at < datetime.fromisoformat(task['MUST_FINISH_BEFORE_JST'])):
                result['venue_day_cancelled'] = True
            official = schedule[int(key[-2:]) - 1]
            if official != task['MUST_FINISH_BEFORE_JST']:
                result.update(official_deadline_jst=official,
                              schedule_status='OFFICIAL_DEADLINE_CHANGED',
                              observed_before_saved_cutoff=at < datetime.fromisoformat(task['MUST_FINISH_BEFORE_JST']))
        except (ValueError, UnicodeError):
            result['schedule_status'] = 'UNVERIFIED'
    return result


def verified_cancelled_venue_days(artifact, plan, now):
    """Only a verified first-race day cancellation captured before its saved cutoff can close the venue."""
    day = now.date().isoformat()
    cancelled = {}
    for task in plan:
        key = task['RACE_KEY']
        if (task['KIND'] != 'BEFOREINFO' or not KEY.fullmatch(key) or not key.endswith('-01')
                or key[:8] != day.replace('-', '') or task['SOURCE_ID'] != 'beforeinfo-' + key):
            continue
        if not (artifact.root / 'rejected-responses' / task['SOURCE_ID']).is_dir():
            continue
        try:
            evidence = latest_rejection(artifact, task, day, now)
        except (ValueError, KeyError, OSError, RuntimeError, TypeError):
            continue
        if evidence.get('venue_day_cancelled') is True:
            cancelled[key[:-3]] = evidence['evidence_sha256']
    return cancelled


def summarize(artifact, day, acquisition, *, now=None):
    now = now or datetime.now(JST)
    if now.utcoffset() != timedelta(hours=9): raise ValueError('HEALTH_CLOCK')
    tasks = _plan(artifact, day)
    issues = {}
    official_unavailable = set()
    cancelled = verified_cancelled_venue_days(artifact, tasks.values(), now)
    cancelled_keys = {task['RACE_KEY'] for task in tasks.values() if task['RACE_KEY'][:-3] in cancelled}
    for category in ('MISSED', 'REJECTED', 'TASK_DEFERRED', 'RETRYABLE_FAILURES', 'UNAVAILABLE'):
        for item in acquisition.get(category, []):
            sid = item if isinstance(item, str) else item['SOURCE_ID']
            if sid not in tasks: raise ValueError('HEALTH_ISSUE_BINDING')
            task = tasks[sid]
            if isinstance(item, dict) and item.get('RACE_KEY') != task['RACE_KEY']:
                raise ValueError('HEALTH_ISSUE_RACE')
            if task['RACE_KEY'] in cancelled_keys:
                continue
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
            'status': ('STOPPED' if (acquisition.get('DEFERRED') or {}).get('REASON') == 'PIPELINE_STOPPED'
                       else 'PARTIAL' if terminal else 'DEFERRED' if issues or acquisition.get('DEFERRED') else 'PASS'),
            'host_pause': host_pause(acquisition.get('DEFERRED'), day, now),
            'scope': 'OFFICIAL_INPUTS_ONLY_NOT_ENGINE_OR_DEVICE_COMPLETENESS',
            'source_issue_count': len(issues),
            'race_issue_count': len({row['race_key'] for row in issues.values()}),
            'official_unavailable_race_count': len(official_unavailable),
            'official_cancelled_race_count': len(cancelled_keys),
            'official_cancelled_venue_days': cancelled,
            'omitted_source_details': max(0, len(issues) - len(details)), 'issues': details,
            'prediction_boundary_changed': False, 'prediction_generated': False}


def p3_input_holds(artifact, day, rows, result, verify_context):
    """Read saved input pairs for missing P3 LOCKs. Never invoke a predictor."""
    pre = artifact.root / 'snapshots' / 'predeadline'
    reasons = {}
    for item in result.get('PREDICTION', {}).get('FAILED', []):
        reasons[item['RACE_KEY']] = _safe_code(item.get('REASON'))
    live = result.get('P3_LIVE', {})
    for key in live.get('SKIPPED_DEADLINE', []):
        reasons[key] = 'P3_LIVE_DEADLINE_EXPIRED'
    for item in live.get('SKIPPED_TRUE_AI_LIVE', []):
        reasons[item['RACE_ID']] = _safe_code(item.get('ERROR_REASON'))
    count, evidence = 0, []
    for row in rows:
        key = row['key']
        if (not KEY.fullmatch(key) or key[:8] != day.replace('-', '') or row.get('formal_status') != 'FORMAL'
                or row.get('predeadline') is not None or (pre / f'current-growth-p3-live-{key}.json').exists()):
            continue
        names = [f'beforeinfo-{key}', f'odds3t-{key}']
        if not all((pre / (name + '.json')).is_file() for name in names):
            continue
        count += 1
        if len(evidence) >= MAX_DETAILS:
            continue
        record = {'race_key':key, 'canonical_reforecast_saved':(pre / f'predeadline-prediction-{key}.json').is_file(),
                  'reported_reason':reasons.get(key), 'input_gate':'UNVERIFIED'}
        try:
            before, odds = [artifact.load_verified_snapshot('predeadline', name) for name in names]
            for label, snap in [('before', before), ('odds', odds)]:
                at = datetime.fromisoformat(snap['SNAPSHOT']['PAYLOAD']['CAPTURED_AT_JST'])
                if at.utcoffset() != timedelta(hours=9) or at.date().isoformat() != day:
                    raise ValueError('HEALTH_INPUT_TIME_BINDING')
                record[label + '_captured_at_jst'] = at.isoformat()
            verify_context(before, odds, key)
            record['input_gate'] = 'PASS'
        except (ValueError, KeyError, OSError, RuntimeError, TypeError) as exc:
            record['input_gate'] = _safe_code(str(exc))
        evidence.append(record)
    return {'operational_date_jst':day, 'race_count':count, 'races':evidence, 'omitted':count - len(evidence)}


def report_p3_after_delivery(root, feed):
    """The read-only diagnostic runs only after the normal public readback."""
    try:
        sys.path.insert(0, str(root / '.g11-private'))
        from g11.canonical.official_pipeline import OfficialArtifactStore, FORMAL_GATE
        result = json.loads((root / '.relay-output/result.json').read_bytes())
        day = feed['operational_date_jst']
        if result['OPERATIONAL_DATE_JST'] != day:
            raise ValueError('HEALTH_DAILY_RESULT_BINDING')
        audit = p3_input_holds(OfficialArtifactStore(root / '.relay-state/store'), day, feed['races'],
                               result, FORMAL_GATE.build_canonical_official_predeadline_context)
        print('G11_P3_INPUT_PAIR_WITHOUT_LIVE=' + json.dumps(audit, sort_keys=True), flush=True)
    except Exception as exc:
        print('G11_P3_INPUT_PAIR_AUDIT_UNAVAILABLE=' + type(exc).__name__, flush=True)


def report(artifact, day, acquisition):
    now = datetime.now(JST)
    value = summarize(artifact, day, acquisition, now=now)
    pause = value['host_pause']
    stop = None
    state = None
    try:
        from g11.canonical.official_pipeline import FIRST_403_PAUSE_SECONDS
        state = artifact.ledger.state()
        recovery_policy(state)
        if state['STOPPED'] is True:
            value['status'] = 'STOPPED'
            stop = saved_pipeline_stop(artifact.ledger, day, now, state)
            if 'REQUEST_POLICY' in state:
                pause = value['host_pause'] = None
        else:
            recorded = saved_beforeinfo_pause(artifact.ledger, day, now, FIRST_403_PAUSE_SECONDS, state=state)
            if recorded and (not pause or pause['retry_at_jst'] == recorded['retry_at_jst']):
                pause = recorded
                if datetime.fromisoformat(pause['retry_at_jst']) > now:
                    value['host_pause'] = pause
                    if value['status'] == 'PASS':
                        value['status'] = 'DEFERRED'
    except (ImportError, AttributeError, KeyError, ValueError, OSError, RuntimeError, TypeError) as exc:
        print('G11_HOST_LEDGER_READ_UNVERIFIED=' + type(exc).__name__, flush=True)
        if isinstance(state, dict) and 'REQUEST_POLICY' in state:
            # Never publish a guessed legacy two-refusal STOP or a stale pending
            # receipt when new-policy evidence could not be verified.
            pause = stop = value['host_pause'] = None
            value['status'] = 'UNVERIFIED'
            workspace = os.environ.get('GITHUB_WORKSPACE')
            if workspace:
                (Path(workspace) / '.relay-output' / 'host-pause.json').unlink(missing_ok=True)
        elif value['status'] == 'PASS':
            value['status'] = 'UNVERIFIED'
    value['host_stop'] = stop
    print('G11_PREDEADLINE_INPUT_HEALTH=' + json.dumps(value, ensure_ascii=False, sort_keys=True), flush=True)
    incident = stop or pause
    if value['status'] == 'STOPPED':
        print('::warning title=G11 official input stopped::Daily official acquisition is stopped. Verified saved inputs remain available.', flush=True)
    if incident:
        marker = 'G11_VERIFIED_HOST_STOP=' if stop else 'G11_VERIFIED_HOST_PAUSE='
        print(marker + json.dumps(incident, sort_keys=True), flush=True)
        if value['host_pause']:
            print('::warning title=G11 official input paused::Next eligible request at ' + pause['retry_at_jst'], flush=True)
        workspace = os.environ.get('GITHUB_WORKSPACE')
        if workspace and os.environ.get('GITHUB_RUN_ID'):
            path = Path(workspace) / '.relay-output' / 'host-pause.json'
            path.write_text(json.dumps({**incident, 'run_id': os.environ['GITHUB_RUN_ID']}))
    if value['status'] == 'PARTIAL':
        print('::warning title=G11 predeadline input gaps::' +
              str(value['race_issue_count']) + ' races have missing or unavailable official inputs; checkpoint success is not full operation success.', flush=True)
    return value


def send_pause_to_site(origin, token, pause):
    with tempfile.TemporaryDirectory(prefix='g11-pause-') as directory:
        body = Path(directory) / 'pause.json'
        body.write_text(json.dumps(pause))
        headers = 'Authorization: Bearer ' + token + '\nContent-Type: application/json\n'
        sent = subprocess.run([
            'curl', '--silent', '--show-error', '--max-time', '8', '--request', 'POST',
            origin + '/api/g11-input-pause', '--header', '@-', '--data-binary', '@' + str(body),
            '--write-out', '\n%{http_code}',
        ], input=headers, capture_output=True, text=True)
    response, _, status = sent.stdout.rpartition('\n')
    if sent.returncode or status != '200':
        print('G11_INPUT_PAUSE_HTTP=' + (status if re.fullmatch(r'[0-9]{3}', status) else 'UNVERIFIED'), flush=True)
        raise RuntimeError('INPUT_HEALTH_PUBLICATION_HTTP')
    return json.loads(response)


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
    saved = send_pause_to_site(ORIGIN, token, pause)
    terminal = pause['reason'] == 'PIPELINE_STOPPED'
    if ((terminal and (saved.get('reason') != 'PIPELINE_STOPPED' or 'retry_at_jst' in saved))
            or (not terminal and saved.get('retry_at_jst') != pause['retry_at_jst']) or saved.get('date') != day
            or not isinstance(saved.get('observed_at_jst'), str)
            or saved['observed_at_jst'][:10] != day or saved['observed_at_jst'] > pause['observed_at_jst']):
        raise ValueError('INPUT_HEALTH_PUBLICATION_READBACK')
    print('G11_INPUT_STOP_PUBLISHED=' + saved['observed_at_jst'] if terminal
          else 'G11_INPUT_PAUSE_PUBLISHED=' + pause['retry_at_jst'], flush=True)


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

