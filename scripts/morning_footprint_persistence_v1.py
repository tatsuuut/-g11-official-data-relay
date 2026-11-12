"""Encrypted research checkpoints and completeness checks; no forecast writes."""
from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile
import zipfile

JST = timezone(timedelta(hours=9))
ARTIFACT = 'g11-morning-footprint-history-encrypted'
WORKFLOW = '.github/workflows/g11-morning-odds-footprint-4bet-shadow-v1.yml'
REPOSITORY = 'tatsuuut/-g11-official-data-relay'
PROJECT = 'G11_MORNING_ODDS_FOOTPRINT_4BET_SHADOW_V1'
FLAGS = ('P3_MUTATED', 'WILD_MUTATED', 'ABEKEN_MUTATED',
         'PRODUCTION_BETS_MUTATED', 'NOTIFICATION_MUTATED', 'PURCHASE_EXECUTED',
         'RESULT_INPUT_USED_FOR_DETECTION', 'PAYOUT_INPUT_USED_FOR_DETECTION')
MAX_BYTES = 512 * 1024 * 1024


def require(ok, code):
    if not ok:
        raise ValueError(code)


def encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False,
                       separators=(',', ':'), allow_nan=False) + '\n').encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def read(path):
    return json.loads(Path(path).read_bytes())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded(value))


def history_name(name):
    parts = PurePosixPath(name).parts
    require(str(PurePosixPath(name)) == name and len(parts) in (4, 5), 'HISTORY_PATH')
    require(re.fullmatch(r'\d{4}/\d{2}/\d{2}/.+', name), 'HISTORY_DATE')
    day = date.fromisoformat('-'.join(parts[:3]))
    if len(parts) == 4:
        require(parts[3] in ('scope.json', 'calibration-lock.json', 'daily-summary.json'), 'HISTORY_FILE')
    else:
        require(parts[3] in ('captures', 'analysis', 'calibration', 'settlement'), 'HISTORY_KIND')
        suffix = '.json.gz' if parts[3] == 'calibration' else '.json'
        require(re.fullmatch(day.strftime('%Y%m%d') + r'-\d{2}-(?:0[1-9]|1[0-2])'
                             + re.escape(suffix), parts[4]), 'HISTORY_RACE')
    return name


def history_files(root):
    root = Path(root)
    output = {}
    for path in sorted(root.rglob('*')):
        require(not path.is_symlink(), 'HISTORY_SYMLINK')
        if path.is_file():
            name = history_name(path.relative_to(root).as_posix())
            output[name] = path.read_bytes()
    require(len(output) <= 100000 and sum(map(len, output.values())) <= MAX_BYTES, 'HISTORY_SIZE')
    return output


def pack(root, report, day, mode):
    files = {'history/' + name: raw for name, raw in history_files(root).items()}
    files['research-status.json'] = encoded(report)
    manifest = {'schema': 'G11_FOOTPRINT_ENCRYPTED_CHECKPOINT_V1',
                'repository': REPOSITORY, 'operational_date_jst': day, 'mode': mode,
                'run_id': os.environ.get('GITHUB_RUN_ID'),
                'run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT', '1'),
                'files': {name: {'sha256': sha(raw), 'bytes': len(raw)}
                          for name, raw in files.items()}}
    files['manifest.json'] = encoded(manifest)
    target = io.BytesIO()
    with tarfile.open(fileobj=target, mode='w:gz') as archive:
        for name, raw in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(raw))
    return target.getvalue()


def unpack(raw, expected_run=None):
    files = {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
        for member in archive:
            require(member.isfile() and member.name not in files, 'CHECKPOINT_MEMBER')
            require(member.name in ('manifest.json', 'research-status.json')
                    or member.name.startswith('history/'), 'CHECKPOINT_PATH')
            if member.name.startswith('history/'):
                history_name(member.name[len('history/'):])
            total += member.size
            require(0 <= member.size <= 16 * 1024 * 1024 and total <= MAX_BYTES
                    and len(files) < 100002, 'CHECKPOINT_SIZE')
            files[member.name] = archive.extractfile(member).read()
    manifest = json.loads(files.pop('manifest.json'))
    require(manifest.get('schema') == 'G11_FOOTPRINT_ENCRYPTED_CHECKPOINT_V1'
            and manifest.get('repository') == REPOSITORY, 'CHECKPOINT_IDENTITY')
    if expected_run is not None:
        require(str(manifest.get('run_id')) == str(expected_run), 'CHECKPOINT_RUN')
    require(manifest.get('files') == {name: {'sha256': sha(value), 'bytes': len(value)}
                                    for name, value in files.items()}, 'CHECKPOINT_DIGESTS')
    require(type(json.loads(files['research-status.json'])) is dict, 'CHECKPOINT_REPORT')
    return {name[len('history/'):]: value for name, value in files.items()
            if name.startswith('history/')}


def merge(root, files):
    root = Path(root)
    existing = history_files(root)
    for name, raw in files.items():
        history_name(name)
        require(name not in existing or name.endswith('/daily-summary.json')
                or existing[name] == raw, 'IMMUTABLE_HISTORY_CONFLICT')
    for name, raw in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)


def api(path):
    response = subprocess.run(['gh', 'api', 'repos/' + REPOSITORY + '/' + path],
                              check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return json.loads(response.stdout)


def select_checkpoint(rows, current_run, lookup):
    # A failed job can still carry a valid retained checkpoint. Never require success.
    for item in sorted(rows, key=lambda x: x['id'], reverse=True):
        if item.get('name') != ARTIFACT:
            continue
        run = item.get('workflow_run') or {}
        if (run.get('head_branch') != 'main' or run.get('head_repository_id') != 1367703708
                or str(run.get('id')) == str(current_run)):
            continue
        origin = lookup(run['id'])
        if (origin.get('path') != WORKFLOW or origin.get('head_branch') != 'main'
                or origin.get('event') not in ('schedule', 'workflow_dispatch', 'push')):
            continue
        require(not item.get('expired'), 'LATEST_CHECKPOINT_EXPIRED')
        return item
    return None


def crypt(raw, decrypt=False):
    key = os.environ.get('STATE_PASSPHRASE', '')
    require(bool(key), 'CHECKPOINT_KEY_MISSING')
    # Private bytes and the existing key never appear in argv, logs or public artifacts.
    with tempfile.TemporaryDirectory() as work:
        source = Path(work) / 'input'
        output = Path(work) / 'output'
        source.write_bytes(raw)
        command = ['gpg', '--batch', '--yes', '--pinentry-mode', 'loopback',
                   '--passphrase-fd', '0', '--output', str(output)]
        command += ['--decrypt'] if decrypt else ['--symmetric', '--cipher-algo', 'AES256']
        subprocess.run(command + [str(source)], input=(key + '\n').encode(),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return output.read_bytes()


def restore(root):
    require(os.environ.get('GITHUB_REPOSITORY') == REPOSITORY
            and os.environ.get('GITHUB_REF') == 'refs/heads/main', 'CHECKPOINT_REPOSITORY')
    require(os.environ.get('GITHUB_RUN_ATTEMPT', '1') == '1', 'RESEARCH_RETRY_REQUIRES_NEW_RUN')
    response = api('actions/artifacts?name=' + ARTIFACT + '&per_page=100')
    selected = select_checkpoint(response['artifacts'], os.environ['GITHUB_RUN_ID'],
                                 lambda run_id: api(f'actions/runs/{run_id}'))
    Path(root).mkdir(parents=True, exist_ok=True)
    if selected is None:
        require(len(response['artifacts']) < 100, 'CHECKPOINT_SEARCH_INCOMPLETE')
        history_files(root)
        print('G11_FOOTPRINT_RESTORE=NO_PRIOR_ENCRYPTED_CHECKPOINT')
        return
    zipped = subprocess.run(['gh', 'api', 'repos/' + REPOSITORY
                             + f"/actions/artifacts/{selected['id']}/zip"], check=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
    require(len(zipped) <= MAX_BYTES, 'CHECKPOINT_ZIP_SIZE')
    with zipfile.ZipFile(io.BytesIO(zipped)) as archive:
        require(archive.namelist() == ['history.tgz.gpg'], 'CHECKPOINT_ZIP_MEMBER')
        require(archive.getinfo('history.tgz.gpg').file_size <= MAX_BYTES, 'CHECKPOINT_ZIP_SIZE')
        raw = archive.read('history.tgz.gpg')
    files = unpack(crypt(raw, decrypt=True), selected['workflow_run']['id'])
    merge(root, files)
    print(f"G11_FOOTPRINT_RESTORE=VERIFIED artifact={selected['id']} files={len(files)}")


def morning_time(value, day):
    moment = datetime.fromisoformat(value)
    require(moment.utcoffset() == timedelta(hours=9) and moment.date().isoformat() == day,
            'CAPTURE_DATE_OR_TIMEZONE')
    return moment


def capture_valid(value, race, day):
    try:
        captured = morning_time(value['CAPTURED_AT_JST'], day)
        started = morning_time(value['REQUEST_STARTED_AT_JST'], day)
        deadline = morning_time(race['deadline_jst'], day)
        rows = value['ODDS_ROWS']
        combinations = {f'{a}-{b}-{c}' for a in range(1, 7) for b in range(1, 7)
                        for c in range(1, 7) if len({a, b, c}) == 3}
        odds = {r['COMBINATION']: r['MARKET_ODDS'] for r in rows}
        require(value.get('SCHEMA') == 'G11_MORNING_ODDS_FOOTPRINT_CAPTURE_V1'
                and value.get('PROJECT') == PROJECT and value.get('RACE_ID') == race['key']
                and value.get('DEADLINE_JST') == race['deadline_jst']
                and value.get('STATUS') == 'MORNING_ODDS_LOCKED_EXACT_120'
                and all(value.get(flag) is False for flag in FLAGS)
                and started <= captured < deadline and captured.time() <= time(8, 55)
                and len(rows) == 120 and set(odds) == combinations
                and all(type(v) in (int, float) and math.isfinite(v) and v > 0 for v in odds.values())
                and value.get('ODDS_MAPPING_SHA256') == sha(encoded({k: float(v) for k, v in odds.items()}))
                and re.fullmatch('[0-9a-f]{64}', value.get('RESPONSE_SHA256', '')),
                'CAPTURE_PROOF')
        return True
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def inspect(root, feed_path, day, mode):
    report = {'schema': 'G11_FOOTPRINT_PERSISTENCE_STATUS_V1', 'operational_date_jst': day,
              'mode': mode, 'status': 'FEED_UNAVAILABLE', 'expected': None,
              'saved': None, 'analyzed': None, 'missing': None,
              'production_changed': False}
    if not Path(feed_path).is_file():
        return report
    feed = read(feed_path)
    require(feed.get('status') == 'PASS' and feed.get('operational_date_jst') == day
            and type(feed.get('races')) is list and bool(feed['races']), 'FEED_IDENTITY')
    races = {r['key']: r for r in feed['races'] if r.get('research_eligible') is True}
    require(len(races) == sum(r.get('research_eligible') is True for r in feed['races']), 'FEED_DUPLICATE')
    if not races:
        require(all(r.get('research_eligible') is False and r.get('formal_status') == 'EXCLUDED'
                    for r in feed['races']), 'EMPTY_SCOPE_UNPROVEN')
        report.update(status='NOT_APPLICABLE_EXCLUDED', expected=0, saved=0, analyzed=0, missing=[])
        return report
    day_root = Path(root) / day.replace('-', '/')
    scope_path = day_root / 'scope.json'
    captured, analyzed, invalid, pending = [], [], [], []
    scope_ok = False
    if scope_path.is_file():
        scope = read(scope_path)
        scope_ok = (scope.get('OPERATIONAL_DATE_JST') == day
                    and scope.get('RACE_COUNT') == len(races)
                    and {r['RACE_ID'] for r in scope.get('RACES', [])} == set(races))
    for key, race in races.items():
        path = day_root / 'captures' / (key + '.json')
        if not path.is_file():
            continue
        value = read(path)
        if not capture_valid(value, race, day):
            invalid.append(key)
            continue
        captured.append(key)
        analysis_path = day_root / 'analysis' / (key + '.json')
        if not analysis_path.is_file():
            continue
        analysis = read(analysis_path)
        if (analysis.get('RACE_ID') != key or analysis.get('MORNING_SNAPSHOT_SHA256') != sha(encoded(value))
                or not all(analysis.get(flag) is False for flag in FLAGS)):
            invalid.append(key)
            continue
        analyzed.append(key)
        detected = analysis.get('DETECTED_FOOTPRINTS')
        require(type(detected) is list and analysis.get('DETECTED_FOOTPRINT_COUNT') == len(detected),
                'ANALYSIS_COUNT')
        if detected:
            settlement_path = day_root / 'settlement' / (key + '.json')
            settlement = read(settlement_path) if settlement_path.is_file() else {}
            rows = settlement.get('FOOTPRINT_SETTLEMENTS', [])
            if (settlement.get('SOURCE_ANALYSIS_SHA256') != sha(encoded(analysis))
                    or len(rows) != len(detected)
                    or any(r.get('STATUS') not in ('SETTLED', 'SETTLEMENT_EXCLUDED_REFUND') for r in rows)):
                pending.append(key)
    missing = sorted(set(races) - set(captured))
    calibration_path = day_root / 'calibration-lock.json'
    calibration_ok = (calibration_path.is_file()
                      and read(calibration_path).get('AS_OF_BEFORE_DATE_JST') == day)
    status = ('MISSING_MORNING_DATA' if not scope_path.is_file() else
              'SCOPE_MISMATCH' if not scope_ok else 'INVALID_MORNING_DATA' if invalid else
              'CALIBRATION_LOCK_MISSING' if not calibration_ok else
              'PARTIAL_MORNING_DATA' if missing or len(analyzed) != len(races) else
              'WAITING_OFFICIAL_SETTLEMENT' if mode in ('settle', 'audit') and pending else
              'COMPLETE_RESEARCH_ONLY')
    report.update(status=status, expected=len(races), saved=len(captured), analyzed=len(analyzed),
                  missing=missing, invalid=sorted(invalid), pending_settlement=sorted(pending))
    return report


def capture_boundary(now, day, deadline=None):
    require(now.utcoffset() == timedelta(hours=9) and now.date().isoformat() == day
            and time(7, 5) <= now.time() < time(8, 55)
            and (deadline is None or now < datetime.fromisoformat(deadline)),
            'MORNING_CAPTURE_WINDOW_CLOSED')


def capture(args):
    capture_boundary(datetime.now(JST), args.day)
    from g11.morning_odds_footprint.v1 import runtime_v1 as runtime
    original = runtime._capture_snapshot

    def guarded(key, deadline):
        before = datetime.now(JST)
        try:
            capture_boundary(before, args.day, deadline)
        except ValueError:
            raise runtime.MorningOddsFootprintError('MORNING_CAPTURE_WINDOW_CLOSED')
        value = original(key, deadline)
        if not capture_valid(value, {'key': key, 'deadline_jst': deadline}, args.day):
            raise runtime.MorningOddsFootprintError('CAPTURE_PROOF')
        return value

    runtime._capture_snapshot = guarded
    try:
        result = runtime.capture_day(feed_path=Path(args.feed), history_root=Path(args.history),
                                     operational_date=args.day, max_requests=24, interval_seconds=10)
        write(Path(args.work) / 'runtime-summary.json', result)
    finally:
        runtime._capture_snapshot = original


def finish(report, mode):
    status = report['status']
    if status in ('COMPLETE_RESEARCH_ONLY', 'NOT_APPLICABLE_EXCLUDED'):
        return 0
    # Partial MORNING runs can retry within the window. NIGHT and audits must be red.
    return 0 if mode == 'capture' and status == 'PARTIAL_MORNING_DATA' else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('restore', 'capture', 'preflight', 'report', 'seal', 'verify', 'finish'))
    parser.add_argument('--history', required=True)
    parser.add_argument('--work', required=True)
    parser.add_argument('--day', required=True)
    parser.add_argument('--mode', choices=('capture', 'settle', 'audit'), required=True)
    parser.add_argument('--feed', default='.shadow-work/feed.json')
    parser.add_argument('--cipher')
    args = parser.parse_args()
    date.fromisoformat(args.day)
    work = Path(args.work)
    report_path = work / 'research-status.json'
    if args.command == 'restore':
        restore(args.history)
    elif args.command == 'capture':
        capture(args)
    elif args.command in ('preflight', 'report'):
        try:
            report = inspect(args.history, args.feed, args.day, args.mode)
        except (ValueError, TypeError, KeyError, OSError):
            if args.command == 'preflight':
                raise
            report = {'schema': 'G11_FOOTPRINT_PERSISTENCE_STATUS_V1',
                      'operational_date_jst': args.day, 'mode': args.mode,
                      'status': 'INVALID_RESEARCH_DATA', 'expected': None,
                      'saved': None, 'analyzed': None, 'production_changed': False}
        write(report_path, report)
        if args.command == 'preflight':
            require(report['status'] in ('COMPLETE_RESEARCH_ONLY', 'WAITING_OFFICIAL_SETTLEMENT',
                                          'PARTIAL_MORNING_DATA', 'NOT_APPLICABLE_EXCLUDED'),
                    'MORNING_RESEARCH_INCOMPLETE')
        else:
            print(json.dumps(report, ensure_ascii=False, separators=(',', ':')))
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
                summary.write(f"\n### 朝オッズ研究：{report['status']}\n"
                              f"予定 {report['expected']}R ／ 原本確認 {report['saved']}R ／ 分析 {report['analyzed']}R\n"
                              "遠隔保存は、この後の暗号化・読み戻しで別途確認します。\n"
                              "欠損・未照合を0件の研究成果や完了に置き換えません。\n")
    elif args.command == 'seal':
        raw = pack(args.history, read(report_path), args.day, args.mode)
        cipher = crypt(raw)
        work.mkdir(parents=True, exist_ok=True)
        (work / 'history.tgz.gpg').write_bytes(cipher)
        write(work / 'cipher-receipt.json', {'cipher_sha256': sha(cipher), 'plain_sha256': sha(raw)})
    elif args.command == 'verify':
        cipher = Path(args.cipher).read_bytes()
        receipt = read(work / 'cipher-receipt.json')
        require(sha(cipher) == receipt['cipher_sha256'], 'SAVED_CIPHER_MISMATCH')
        raw = crypt(cipher, decrypt=True)
        require(sha(raw) == receipt['plain_sha256'], 'SAVED_PLAINTEXT_MISMATCH')
        require(unpack(raw, os.environ['GITHUB_RUN_ID']) == history_files(args.history), 'SAVED_HISTORY_MISMATCH')
        write(work / 'remote-save-verified.json', {'day': args.day, 'mode': args.mode,
                                                 'cipher_sha256': sha(cipher)})
        print('G11_FOOTPRINT_REMOTE_SAVE_AND_READBACK=VERIFIED')
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
            summary.write('\n遠隔保存・暗号化原本の読み戻し：確認済み。\n')
    else:
        require((work / 'remote-save-verified.json').is_file(), 'REMOTE_SAVE_NOT_VERIFIED')
        proof = read(work / 'remote-save-verified.json')
        require(proof['day'] == args.day and proof['mode'] == args.mode, 'REMOTE_SAVE_IDENTITY')
        return finish(read(report_path), args.mode)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
