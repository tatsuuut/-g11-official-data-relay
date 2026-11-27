"""Scope original-display transport to approved hosts after a proved national STOP.

Only a private clone of the reviewed acquire function sees the scoped predicate
and reader. The native predicate, real refusal ledger and provider state stay
unchanged; model consumers and their saved input validation are untouched.
"""
from contextlib import contextmanager
from datetime import date
from functools import wraps, update_wrapper
from hashlib import sha256
import importlib
import inspect
import json
from pathlib import Path
import textwrap
import types
from urllib.parse import urlsplit
from urllib.error import HTTPError

from independent_fallback_v1 import stop_scope

ACQUIRE_SHA = '4f567c716f7fecfbca902f1fd5edcf61693551ddabf6d233231675896c2561f1'
HOSTS = frozenset({'race.boatcast.jp', 'www.boatrace-fukuoka.com', 'boatrace-amagasaki.jp',
                  'www.boatrace-tokuyama.jp', 'www.wmb.jp', 'boatrace-fun.net'})


def require(ok, reason):
    if not ok:
        raise ValueError('INDEPENDENT_ORIGINAL:' + reason)


def summary(key, result):
    fields = {'race_key':key, 'status':str(result.get('STATUS','UNKNOWN'))[:80]}
    if result.get('STATUS') == 'CAPTURED':
        for name in ('PROVIDER','HOST','SHA256','CAPTURED_AT'):
            if isinstance(result.get(name),str):
                fields[name.lower()] = result[name][:96]
    print('G11_INDEPENDENT_ORIGINAL_DISPLAY_V1 ' + json.dumps(fields,sort_keys=True),flush=True)


@contextmanager
def context(day):
    if date.fromisoformat(day) < date(2026, 11, 27):
        yield
        return
    try:
        shared = importlib.import_module('g11.canonical.shared_original_display_v1')
        official = importlib.import_module('g11.canonical.official_pipeline')
        original = shared.acquire
        native = inspect.unwrap(original)
        source = textwrap.dedent(inspect.getsource(native))
        native_blocked = native.__globals__['_blocked']
        native_reader = native.__globals__['urlopen']
        require(all(callable(native.__globals__[name]) for name in
                    ('_blocked','urlopen','_legacy_urlopen','providers_for')), 'SOURCE_INTERFACE')
    except (ImportError, OSError, TypeError, AttributeError, KeyError, ValueError):
        print('INDEPENDENT_ORIGINAL:DISABLED_SOURCE_UNAVAILABLE', flush=True)
        yield
        return
    from exhibition_input_compatibility_v1 import _keep_wrappers, _quiet_original_futures
    if sha256(source.encode()).hexdigest() != ACQUIRE_SHA:
        print('INDEPENDENT_ORIGINAL:DISABLED_UNREVIEWED_ACQUIRE', flush=True)
        yield
        return
    critical = importlib.import_module('g11.relay.predeadline_critical_v1')
    original_tree = critical._tree_sha
    _quiet_original_futures()

    @wraps(original)
    def acquire(root, key, deadline, *, clock=shared.clock, opener=None, total_seconds=2.0):
        store = official.OfficialArtifactStore(root)
        try:
            proof = stop_scope(store, day, clock())
        except (OSError, ValueError, KeyError, TypeError):
            return original(root, key, deadline, clock=clock, opener=opener, total_seconds=total_seconds)
        for provider in native.__globals__['providers_for'](key):
            url = urlsplit(provider.url_for(key))
            require(provider.HOST in HOSTS and url.scheme == 'https' and url.netloc == provider.HOST,
                    'PROVIDER_ALLOWLIST')

        def blocked(target, race):
            reason = native_blocked(target, race)
            if reason != 'PIPELINE_STOPPED':
                return reason
            # Native STOP is checked first and can conceal race unavailability.
            if (Path(target)/'snapshots/predeadline'/('availability-'+race+'.json')).is_file():
                return 'RACE_UNAVAILABLE'
            try:
                current = official.OfficialArtifactStore(target)
                if stop_scope(current,day,clock()) != proof:
                    return reason
                packet = importlib.import_module('g11.canonical.boatcast_preview_v1')
                if packet.odds_priority(current,clock()).get('STATUS') != 'ODDS_PRIORITY_CLEAR':
                    return 'DEFERRED_ODDS_PRIORITY'
                return None
            except (OSError,ValueError,KeyError,TypeError,ImportError,AttributeError):
                return reason

        def guard(reader):
            @wraps(reader)
            def read(request, *args, **kwargs):
                url = request.full_url if hasattr(request,'full_url') else str(request)
                parsed = urlsplit(url)
                require(parsed.scheme == 'https' and parsed.netloc in HOSTS
                        and stop_scope(store,day,clock()) == proof, 'READ_SCOPE')
                fields={'race_key':key,'host':parsed.netloc,'url_sha256':sha256(url.encode()).hexdigest()}
                print('G11_INDEPENDENT_ORIGINAL_REQUEST ' + json.dumps(fields,sort_keys=True),flush=True)
                try:
                    response=reader(request,*args,**kwargs)
                except HTTPError as error:
                    print('G11_INDEPENDENT_ORIGINAL_RESPONSE ' + json.dumps({**fields,'status':error.code},sort_keys=True),flush=True)
                    raise
                print('G11_INDEPENDENT_ORIGINAL_RESPONSE ' + json.dumps({**fields,'status':response.status},sort_keys=True),flush=True)
                return response
            return read

        providers = native.__globals__['providers_for'](key)
        require(providers and all(provider.HOST in HOSTS for provider in providers), 'PROVIDER_ALLOWLIST')
        guarded_default = guard(native_reader)
        namespace = {**native.__globals__, '_blocked':blocked, 'urlopen':guarded_default}
        clone = types.FunctionType(native.__code__,namespace,native.__name__,native.__defaults__,native.__closure__)
        clone.__kwdefaults__ = native.__kwdefaults__
        update_wrapper(clone,native)
        retained = _keep_wrappers(original,clone)
        guarded_opener = (None if opener is None or opener is native_reader
                          or opener is native.__globals__['_legacy_urlopen'] else guard(opener))
        result = retained(root,key,deadline,clock=clock,opener=guarded_opener,total_seconds=total_seconds)
        summary(key,result)
        return result

    @wraps(original_tree)
    def tree(root):
        _quiet_original_futures()
        return original_tree(root)

    shared.acquire = acquire
    critical._tree_sha = tree
    try:
        yield
    finally:
        try:
            _quiet_original_futures()
        finally:
            shared.acquire = original
            critical._tree_sha = original_tree
