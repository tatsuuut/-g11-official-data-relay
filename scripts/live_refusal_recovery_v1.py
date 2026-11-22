"""Durable admission for the new native LIVE refusal policy only.

The Site consumes one daily claim before any recovery HTTP. A lost reply,
process death or lost encrypted checkpoint never restores that allowance.
Legacy ledgers do not call this provider. No STOP or saved prediction is edited.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
import signal
import threading
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

POLICY = 'G11_LIVE_REFUSAL_RECOVERY_V1'
CLAIM_SCHEMA = 'G11_LIVE_REFUSAL_RECOVERY_CLAIM_V1'
GRANT_SCHEMA = 'G11_LIVE_REFUSAL_RECOVERY_GRANT_V1'
ORIGIN = 'https://g11-race-lab.higatatsunori2.chatgpt.site'
JST = timezone(timedelta(hours=9))
PROBE_WALL_SECONDS = 30


class ClaimUnavailable(RuntimeError):
    def __init__(self, spent=False):
        self.spent = spent
        super().__init__('LIVE_RECOVERY_CLAIM_SPENT' if spent else 'LIVE_RECOVERY_CLAIM_DEFERRED')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post_claim(body):
    """Use the existing Actions OIDC identity, with no retry or credential logging."""
    if os.environ.get('G11_APP_ORIGIN') != ORIGIN:
        raise ClaimUnavailable()
    request = Request(os.environ['ACTIONS_ID_TOKEN_REQUEST_URL'] + '&' + urlencode({
        'audience': os.environ['G11_OIDC_AUDIENCE']}), headers={
        'Authorization': 'bearer ' + os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']})
    with urlopen(request, timeout=5) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ClaimUnavailable()
    token = json.loads(raw).get('value')
    if not isinstance(token, str) or not 1 <= len(token) <= 16000:
        raise ClaimUnavailable()
    request = Request(ORIGIN + '/api/g11-input-recovery', method='POST',
        data=json.dumps(body, separators=(',', ':')).encode(), headers={
            'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    try:
        with build_opener(NoRedirect()).open(request, timeout=8) as response:
            if response.status != 200:
                raise ClaimUnavailable()
            raw = response.read(4097)
    except HTTPError as exc:
        # An identical request is never replayed as an allowed grant.
        try:
            value = json.loads(exc.read(4096))
        except (ValueError, OSError):
            value = {}
        raise ClaimUnavailable(exc.code == 409 and value.get('error') == 'RECOVERY_CLAIM_SPENT') from None
    if len(raw) > 4096:
        raise ClaimUnavailable()
    return json.loads(raw)


def load_authority(root, day):
    from g11.relay.daily_runtime_authority_v1 import load
    return load(root, day)


def jst(value):
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() != timedelta(hours=9):
        raise ValueError('RECOVERY_TIMEZONE')
    return parsed


class ClaimProvider:
    def __init__(self, runner, official, transport=post_claim, authority_loader=load_authority):
        self.runner, self.official = runner, official
        self.transport, self.authority_loader = transport, authority_loader
        self.pending = None
        self.blocked_until = None
        self.blocked_reason = None

    def defer(self, spent=False):
        now = self.runner.client.clock()
        until = (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
                 if spent else now + timedelta(seconds=60))
        self.blocked_until = until
        self.blocked_reason = 'LIVE_RECOVERY_CLAIM_SPENT' if spent else 'LIVE_RECOVERY_CLAIM_DEFERRED'
        print('G11_LIVE_REFUSAL_RECOVERY=' + json.dumps({'status': self.blocked_reason,
              'official_request_started': False}), flush=True)
        raise self.official.AcquisitionDeferred(until, self.blocked_reason)

    def __call__(self, ledger, payload, at, recovery):
        if self.blocked_until is not None:
            until = max(self.blocked_until, self.runner.client.clock() + timedelta(seconds=60))
            raise self.official.AcquisitionDeferred(until, self.blocked_reason)
        try:
            day = ledger._manifest()['OPERATIONAL_DATE_JST']
            source = payload['SOURCE_ID']
            now = self.runner.client.clock()
            if (recovery['POLICY'] != POLICY or recovery['STATUS'] != 'COOLDOWN'
                    or now.utcoffset() != timedelta(hours=9) or now.date().isoformat() != day
                    or day < '2026-11-23' or now < jst(recovery['RETRY_AT_JST'])
                    or ledger.root != self.runner.store.ledger.root
                    or not re.fullmatch(r'request-[a-f0-9]{32}', payload['REQUEST_ID'])):
                raise ValueError('RECOVERY_SOURCE_BINDING')
            # Verify the unchanged saved schedule and exact request URL again.
            morning = self.runner.store.load_verified_snapshot('morning', 'official-day-input')
            plan = morning['SNAPSHOT']['PAYLOAD']
            tasks = [row for row in plan['PREDEADLINE_PLAN'] if row['SOURCE_ID'] == source]
            if plan['OPERATIONAL_DATE_JST'] != day or len(tasks) != 1:
                raise ValueError('RECOVERY_PLAN_BINDING')
            task = tasks[0]
            cutoff = jst(task['MUST_FINISH_BEFORE_JST'])
            if (task['KIND'] not in ('BEFOREINFO', 'ODDS3T')
                    or source != task['KIND'].lower() + '-' + task['RACE_KEY']
                    or not re.fullmatch(r'(?:beforeinfo|odds3t)-' + day.replace('-', '')
                        + r'-(?:0[1-9]|1[0-9]|2[0-4])-(?:0[1-9]|1[0-2])', source)
                    or payload['HOST'] != 'www.boatrace.jp'
                    or payload['URL_SHA256'] != hashlib.sha256(task['URL'].encode()).hexdigest()
                    or cutoff.date().isoformat() != day or (cutoff - now).total_seconds() < 50):
                raise ValueError('RECOVERY_DEADLINE_OR_URL')
            events = ledger.events()
            triggers = [event for event in events if event['TYPE'] == 'OUTCOME_RECORDED'
                        and event['AT_JST'] == recovery['ARMED_AT_JST']
                        and event['PAYLOAD']['OUTCOME'] == 'HTTP_403']
            if len(triggers) != 1:
                raise ValueError('RECOVERY_TRIGGER_BINDING')
            trigger = triggers[0]
            trigger_sha = hashlib.sha256((ledger.root / 'events' /
                f'{trigger["INDEX"]:06d}.json').read_bytes()).hexdigest()
            authority = self.authority_loader(self.runner.store.root.parent, day)
            sha = authority['authority_sha256']
            run = os.environ.get('GITHUB_RUN_ID', '')
            if not re.fullmatch('[a-f0-9]{64}', sha) or not re.fullmatch('[0-9]+', run):
                raise ValueError('RECOVERY_AUTHORITY_BINDING')
            body = {'schema': CLAIM_SCHEMA, 'policy': POLICY, 'date': day,
                'authority_sha256': sha, 'run_id': run, 'source_id': source,
                'request_id': payload['REQUEST_ID'], 'trigger_sha256': trigger_sha,
                'observed_at_jst': recovery['ARMED_AT_JST'],
                'retry_at_jst': recovery['RETRY_AT_JST'],
                'deadline_jst': cutoff.isoformat(timespec='seconds')}
            grant = self.transport(body)
            expected = {**body, 'schema': GRANT_SCHEMA}
            if (not isinstance(grant, dict) or set(grant) != set(expected) | {
                    'claim_id', 'granted_at_jst', 'lease_until_jst'}
                    or any(grant.get(key) != value for key, value in expected.items())
                    or not re.fullmatch('[a-f0-9]{32}', str(grant.get('claim_id', '')))):
                raise ValueError('RECOVERY_GRANT_BINDING')
            granted, lease = jst(grant['granted_at_jst']), jst(grant['lease_until_jst'])
            after = self.runner.client.clock()
            if (granted < jst(recovery['RETRY_AT_JST']) or granted > after
                    or after.date().isoformat() != day or after < now
                    or lease - granted != timedelta(seconds=90)
                    or (lease - after).total_seconds() < PROBE_WALL_SECONDS + 2
                    or (cutoff - after).total_seconds() < PROBE_WALL_SECONDS + 2):
                raise ValueError('RECOVERY_GRANT_EXPIRED')
            self.pending = {'grant': grant, 'url': task['URL'], 'cutoff': cutoff, 'lease': lease}
            print('G11_LIVE_REFUSAL_RECOVERY=' + json.dumps({'status': 'DURABLE_CLAIM_GRANTED',
                  'date': day, 'source_id': source, 'official_request_started': False}), flush=True)
            return grant
        except self.official.AcquisitionDeferred:
            raise
        except ClaimUnavailable as exc:
            return self.defer(exc.spent)
        except Exception:
            # Transport loss may have consumed the claim. Never retry it here.
            return self.defer()


class TimedProbeResponse:
    """Bound the complete opening/body read, not merely each socket operation."""
    def __init__(self, opener, request, kwargs, provider):
        self.closed_timer = True
        self.response = None
        permit = provider.pending
        provider.pending = None  # This in-process handoff is also one-use.
        now = provider.runner.client.clock()
        if (permit is None or request.full_url != permit['url'] or request.get_method() != 'GET'
                or threading.current_thread() is not threading.main_thread()
                or not hasattr(signal, 'setitimer') or signal.getitimer(signal.ITIMER_REAL)[0] != 0
                or min((permit['cutoff'] - now).total_seconds(),
                       (permit['lease'] - now).total_seconds()) < PROBE_WALL_SECONDS + 1):
            # begin() already appended REQUEST_STARTED. The canonical client's
            # timeout handler settles it and closes the failed recovery; a
            # scheduler deferral here would leave ACTIVE_REQUEST stranded.
            raise TimeoutError('LIVE_RECOVERY_PREOPEN_REJECTED')
        self.old_handler = signal.getsignal(signal.SIGALRM)
        def timeout(_signum, _frame):
            raise TimeoutError('LIVE_RECOVERY_TOTAL_WALL_TIMEOUT')
        signal.signal(signal.SIGALRM, timeout)
        self.closed_timer = False
        signal.setitimer(signal.ITIMER_REAL, PROBE_WALL_SECONDS)
        try:
            self.response = opener(request, **kwargs)
        except BaseException:
            self.stop_timer()
            raise

    def stop_timer(self):
        if not self.closed_timer:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, self.old_handler)
            self.closed_timer = True

    def __getattr__(self, key):
        return getattr(self.response, key)

    def __enter__(self):
        try:
            self.response.__enter__()
            return self
        except BaseException:
            self.stop_timer()
            raise

    def read(self, *args, **kwargs):
        try:
            return self.response.read(*args, **kwargs)
        finally:
            self.stop_timer()

    def __exit__(self, *args):
        try:
            return self.response.__exit__(*args)
        finally:
            self.stop_timer()


@contextmanager
def durable_refusal_admission(runner):
    from g11.canonical import official_pipeline as official
    # The hook exists only in the reviewed new native policy. Today's frozen
    # source is left byte-for-byte and behavior-for-behavior unchanged.
    if not hasattr(official, 'LIVE_REFUSAL_RECOVERY_CLAIM_PROVIDER'):
        yield
        return
    previous = official.LIVE_REFUSAL_RECOVERY_CLAIM_PROVIDER
    if previous is not None:
        raise ValueError('LIVE_RECOVERY_NESTED_PROVIDER')
    provider = ClaimProvider(runner, official)
    original_opener = runner.client.opener
    def opener(request, **kwargs):
        if provider.pending is None:
            return original_opener(request, **kwargs)
        return TimedProbeResponse(original_opener, request, kwargs, provider)
    official.LIVE_REFUSAL_RECOVERY_CLAIM_PROVIDER = provider
    runner.client.opener = opener
    try:
        yield
    finally:
        runner.client.opener = original_opener
        official.LIVE_REFUSAL_RECOVERY_CLAIM_PROVIDER = previous
