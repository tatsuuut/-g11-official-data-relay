"""User-authorized independent transport after a proved national HTTP403 STOP.

The actual national ledger, provider holds, native windows/parsers and saved
morning/models/LOCKs remain authoritative and unchanged.
"""
from __future__ import annotations
import ast
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from functools import wraps
from hashlib import sha256
import importlib
import inspect
import json
import textwrap
import types
from urllib.parse import urlsplit
from predeadline_input_health_v1 import recorded_incident

SCHEMA = 'G11_USER_AUTHORIZED_INDEPENDENT_FALLBACK_V1'
NATIONAL = 'www.boatrace.jp'
BOATCAST = 'race.boatcast.jp'
HOSTS = frozenset({'boatraceopenapi.github.io', BOATCAST, 'www.omurakyotei.jp'})
# Reviewed candidate 614905a4001d6f4392ece21f49a30b3b4991581c function bytes.
FUNCTIONS = {
 'live_source_failover_v1': ('capture', '56de8c9acaae9402084b34420ec2a228c000e04663adac3a12ce78a932f291b4', 1),
 'published_preview_v1': ('capture_missing', '614f1c2151a286555a54b3bbaab8b176b61e216ee39838718ec347b1671aa886', 1),
 'venue_market_v1': ('capture_due', '7b0118c440f5fd25a3835d43a9e2c73f37e41dc150c670f6895d6113bbcd1234', 2),
 'boatcast_preview_v1': ('due_observation', '4d2c8b18fc7fe2f105caf0f8cea2645c922b7057a20b92c72c23907f789e5941', 4),
}


def require(ok, reason):
    if not ok:
        raise ValueError('INDEPENDENT_FALLBACK:' + reason)


def stop_scope(store, day, now):
    """Read-only recognition; an old incident does not open an old target race."""
    require(now.utcoffset() == timedelta(hours=9) and now.date().isoformat() == day
            and now.date() >= date(2026, 11, 27), 'DAY')
    state = store.ledger.state()
    require(state.get('REQUEST_POLICY') == 'G11_LIVE_REFUSAL_RECOVERY_V1'
            and state.get('STOPPED') is True and state.get('ACTIVE_REQUEST') is None, 'NATIVE_STOP')
    at, request, outcome = recorded_incident(store.ledger, day, now, state['STOPPED_AT_JST'])
    source = request['SOURCE_ID']
    key = source.removeprefix('beforeinfo-')
    require(source.startswith('beforeinfo-') and key[:8] == day.replace('-', '')
            and request['HOST'] == NATIONAL and outcome.get('OUTCOME') == 'HTTP_403'
            and outcome.get('HTTP_STATUS') == 403, 'NATIONAL_BEFOREINFO_403')
    morning = store.load_verified_snapshot('morning', 'official-day-input')['SNAPSHOT']['PAYLOAD']
    require(morning['OPERATIONAL_DATE_JST'] == day, 'MORNING_DAY')
    tasks = [t for t in morning['PREDEADLINE_PLAN'] if t['SOURCE_ID'] == source]
    require(len(tasks) == 1, 'STOP_PLAN_SOURCE')
    task = tasks[0]
    parsed = urlsplit(task['URL'])
    require(task['KIND'] == 'BEFOREINFO' and task['RACE_KEY'] == key
            and parsed.scheme == 'https' and parsed.netloc == NATIONAL
            and parsed.path == '/owpc/pc/race/beforeinfo'
            and request['URL_SHA256'] == sha256(task['URL'].encode()).hexdigest()
            and at < datetime.fromisoformat(task['MUST_FINISH_BEFORE_JST']), 'STOP_PLAN_URL')
    recovery = state['REFUSAL_RECOVERY']
    require(type(recovery['ATTEMPT_COUNT']) is int and recovery['ATTEMPT_COUNT'] == 1
            and recovery['STATUS'] == 'EXHAUSTED', 'RECOVERY_NOT_EXHAUSTED')
    events = store.ledger.events()
    completed = [e for e in events if e['TYPE'] == 'OUTCOME_RECORDED'
                 and e['PAYLOAD'].get('REQUEST_ID') == recovery['REQUEST_ID']]
    started = [e for e in events if e['TYPE'] == 'REQUEST_STARTED'
               and e['PAYLOAD'].get('REQUEST_ID') == recovery['REQUEST_ID']]
    require(len(completed) == len(started) == 1
            and completed[0]['PAYLOAD'].get('OUTCOME') == 'HTTP_200'
            and completed[0]['PAYLOAD'].get('HTTP_STATUS') == 200
            and started[0]['PAYLOAD'].get('HOST') == NATIONAL
            and datetime.fromisoformat(completed[0]['AT_JST']) <= at, 'RECOVERY_NOT_SUCCESSFUL')
    return {'schema': SCHEMA, 'day': day, 'native_stopped': True,
            'stop_at_jst': state['STOPPED_AT_JST'], 'stop_source_id': source,
            'stop_request_id': request['REQUEST_ID'], 'stop_url_sha256': request['URL_SHA256'],
            'recovery_request_id': recovery['REQUEST_ID'], 'independent_hosts': sorted(HOSTS),
            'market_attempt_limit': 1}


class _Scoped(ast.NodeTransformer):
    def __init__(self):
        self.count = 0

    def visit_Subscript(self, node):
        if isinstance(node.slice, ast.Constant) and node.slice.value == 'STOPPED':
            self.count += 1
            return ast.Call(ast.Name('_independent_blocked', ast.Load()), [], [])
        return self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.level == 1 and any(a.name == 'boatcast_preview_v1' for a in node.names):
            require(len(node.names) == 1 and node.names[0].asname == 'boatcast', 'BOATCAST_IMPORT')
            return ast.Assign([ast.Name('boatcast', ast.Store())], ast.Name('_independent_boatcast', ast.Load()))
        return node


def _clone(module, additions, *, original=None):
    from exhibition_input_compatibility_v1 import _keep_wrappers
    short = module.__name__.rsplit('.', 1)[-1]
    name, digest, expected = FUNCTIONS[short]
    if original is None:
        original = getattr(module, name)
    source = textwrap.dedent(inspect.getsource(inspect.unwrap(original)))
    tree = ast.parse(source)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef))
    require(sha256((ast.get_source_segment(source, node) + '\n').encode()).hexdigest() == digest,
            'REVIEWED_SOURCE:' + short)
    transform = _Scoped()
    transform.visit(tree)
    require(transform.count == expected, 'STOP_SITE_COUNT:' + short)
    if short == 'live_source_failover_v1':
        matches = [n for n in ast.walk(tree) if isinstance(n, ast.If)
                   and isinstance(n.test, ast.Name) and n.test.id == 'paused']
        require(len(matches) == 1, 'NATIONAL_PAUSE_SITE')
        matches[0].body = ast.parse("reasons.add('NATIONAL_HOST_STOPPED')").body
        matches[0].test = ast.Constant(True)
        first = next(i for i,n in enumerate(node.body) if isinstance(n,ast.If)
                     and any(isinstance(a,ast.Attribute) and a.attr == 'capture_missing' for a in ast.walk(n)))
        second = next(i for i,n in enumerate(node.body) if isinstance(n,ast.If)
                      and any(isinstance(a,ast.Attribute) and a.attr == 'capture_due' for a in ast.walk(n)))
        node.body[first], node.body[second] = node.body[second], node.body[first]
    ast.fix_missing_locations(tree)
    namespace = {**inspect.unwrap(original).__globals__, **additions}
    exec(compile(tree, inspect.unwrap(original).__code__.co_filename, 'exec'), namespace)
    return _keep_wrappers(original, namespace[name])


def _module(module, **changes):
    return types.SimpleNamespace(**{**module.__dict__, **changes})


def _wait_for_packet(runner, result, modules, proof):
    """Re-evaluate once after a successful same-race market's existing hold."""
    report = result['LIVE_SOURCE_FAILOVER']
    eligible = set(report['ELIGIBLE_BEFOREINFO'])
    store, clock = runner.store, runner.client.clock
    for key,item in result.get('STADIUM_MARKETS', {}).items():
        if key not in eligible or item.get('source') != BOATCAST or 'status' in item:
            continue
        target = store.root / 'snapshots/predeadline' / ('published-exhibition-' + key + '.json')
        if target.exists() or result.get('INDEPENDENT_PREVIEWS', {}).get(key, {}).get('sha256'):
            continue
        morning = store.load_verified_snapshot('morning','official-day-input')['SNAPSHOT']['PAYLOAD']
        task = next(t for t in morning['PREDEADLINE_PLAN'] if t['KIND']=='BEFOREINFO' and t['RACE_KEY']==key)
        cutoff = datetime.fromisoformat(task['MUST_FINISH_BEFORE_JST'])
        checked = modules['venue_market_v1'].recheck(store,key,cutoff,modules['published_preview_v1'].race_roster(morning,key))
        hostpath = store.root / 'g11/published-preview-state' / BOATCAST / 'host.json'
        host = json.loads(hostpath.read_bytes())
        until = datetime.fromisoformat(host['next'])
        if not (host.get('status') == 200 and host.get('race') == key
                and host.get('source') in {r['SOURCE_ID'] for r in checked['SNAPSHOT']['SOURCE_RECEIPTS']}
                and until.utcoffset() == timedelta(hours=9)
                and timedelta(0) < until-clock() <= timedelta(seconds=90)
                and cutoff-until >= timedelta(seconds=180)):
            report['PACKET_WAIT'] = 'NO_SAFE_EXISTING_HOLD_WINDOW'
            continue
        waited = 0.0
        while clock() < until:
            require(stop_scope(store,proof['day'],clock()) == proof, 'STOP_SCOPE_CHANGED')
            require(json.loads(hostpath.read_bytes()) == host, 'PROVIDER_HOLD_CHANGED')
            seconds = min(60.0,(until-clock()).total_seconds())
            before = clock()
            runner.sleeper(seconds)
            require(clock() > before, 'WAIT_CLOCK_DID_NOT_ADVANCE')
            waited += seconds
            require(waited <= 90, 'WAIT_BUDGET')
        require(stop_scope(store,proof['day'],clock()) == proof, 'STOP_SCOPE_CHANGED')
        retry = modules['scoped_preview'].capture_missing(runner,race_keys={key},allow_missing=True)
        result.setdefault('INDEPENDENT_PREVIEWS',{}).update(retry)
        report['PACKET_WAIT'] = {'race_key':key,'seconds':waited,'status':'ONE_REEVALUATION_AFTER_EXISTING_HOLD'}
        break


@contextmanager
def context(day):
    if date.fromisoformat(day) < date(2026, 11, 27):
        yield
        return
    try:
        modules = {name:importlib.import_module('g11.canonical.'+name) for name in FUNCTIONS}
    except ImportError:
        print(SCHEMA + ' DISABLED_REVIEWED_INPUT_MODULE_UNAVAILABLE')
        yield
        return
    live = modules['live_source_failover_v1']
    try:
        original = live.capture
    except AttributeError:
        print(SCHEMA + ' DISABLED_REVIEWED_CAPTURE_UNAVAILABLE')
        yield
        return

    @wraps(original)
    def capture(runner, acquisition):
        try:
            proof = stop_scope(runner.store, day, runner.client.clock())
        except (OSError, ValueError, KeyError, TypeError):
            return original(runner, acquisition)
        changes = []
        def blocked():
            try:
                return stop_scope(runner.store,day,runner.client.clock()) != proof
            except (OSError,ValueError,KeyError,TypeError):
                return True
        try:
            require(set(modules['venue_market_v1'].PROVIDERS) == {BOATCAST,'www.omurakyotei.jp'}, 'PROVIDER_ALLOWLIST')
            readers = [(modules['published_preview_v1'],'_read'),(modules['boatcast_preview_v1'],'_read'),
                       (importlib.import_module('g11.canonical.boatcast_market_v1'),'read'),
                       (importlib.import_module('g11.canonical.omura_market_v1'),'read')]
            readers = [(owner,name,getattr(owner,name)) for owner,name in readers]
            for short,(name,_,_) in FUNCTIONS.items():
                require(callable(getattr(modules[short],name)), 'FUNCTION_UNAVAILABLE:' + short)
        except (ImportError,AttributeError,OSError,ValueError,TypeError,KeyError):
            print(SCHEMA + ' DISABLED_REVIEWED_TRANSPORT_UNAVAILABLE')
            return original(runner,acquisition)
        try:
            for owner,name,before in readers:
                def guarded(url,*args,_read=before,**kwargs):
                    parsed = urlsplit(url)
                    require(not blocked() and parsed.scheme=='https' and parsed.netloc in HOSTS, 'INDEPENDENT_READ_SCOPE')
                    return _read(url,*args,**kwargs)
                changes.append((owner,name,before))
                setattr(owner,name,guarded)
            try:
                additions = {'_independent_blocked':blocked}
                boatcast = _module(modules['boatcast_preview_v1'],due_observation=_clone(modules['boatcast_preview_v1'],additions))
                preview = _module(modules['published_preview_v1'],capture_missing=_clone(modules['published_preview_v1'],
                           {**additions,'_independent_boatcast':boatcast}))
                markets = _module(modules['venue_market_v1'],capture_due=_clone(modules['venue_market_v1'],additions))
                scoped = _clone(live,{**additions,'preview':preview,'markets':markets},original=original)
            except (AttributeError, OSError, ValueError, TypeError, KeyError, StopIteration) as exc:
                print(SCHEMA + ' DISABLED_REVIEWED_SOURCE ' + type(exc).__name__)
                return original(runner, acquisition)
            result = scoped(runner,acquisition)
            result['LIVE_SOURCE_FAILOVER']['INDEPENDENT_SCOPE'] = proof
            try:
                _wait_for_packet(runner,result,{**modules,'scoped_preview':preview},proof)
            except (OSError,ValueError,KeyError,TypeError) as exc:
                result['LIVE_SOURCE_FAILOVER']['PACKET_WAIT'] = {'status':'DEFERRED','reason':str(exc)[:160]}
            print(SCHEMA+' '+json.dumps(result['LIVE_SOURCE_FAILOVER'],sort_keys=True))
            return result
        finally:
            for owner,name,before in reversed(changes):
                setattr(owner,name,before)
    live.capture = capture
    try:
        yield
    finally:
        live.capture = original
