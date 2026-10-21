"""Isolate identifiable official-input defects before sealing MORNING scope.

No predictions, coefficients, clocks, receipts or existing snapshots are changed.
The old producer/consumer adaptations are pinned to reviewed function bytes.
"""
from datetime import date, timedelta
import hashlib
import inspect
import json
import re
import sys
import textwrap
import types


def isolated_results(text, target, official, parser):
    """Return verified races plus explicit rejected race sections, never defaults."""
    from rescue_night_result_parser_v1 import parse_night_results
    def strict(value):
        return parse_night_results(value, target, official, parser)
    try:
        return strict(text), []
    except official.PipelineError:
        pass
    normalized = official._normalize(text)
    markers = list(re.finditer(r'(?m)^(\d{2})KBGN\s*$', normalized))
    if not markers or len({m[1] for m in markers}) != len(markers):
        raise official.PipelineError('K_ISOLATION_VENUE_IDENTITY')
    results, rejected, keys = [], [], set()
    cancelled_re = re.compile(r'(?m)^\s*(\d{1,2})R\s+中\s*止\s*$')
    for index, marker in enumerate(markers):
        block = normalized[marker.end():markers[index+1].start() if index+1<len(markers) else len(normalized)]
        races = list(official._K_RACE_RE.finditer(block))
        header = block[:races[0].start()] if races else block
        prefix = marker.group(0) + '\n' + cancelled_re.sub('', header)
        # Wrong dates, unknown venues and broken framing remain source defects.
        if strict(prefix):
            raise official.PipelineError('K_ISOLATION_HEADER')
        cancelled = list(cancelled_re.finditer(block))
        numbers = [int(m[1]) for m in races] + [int(m[1]) for m in cancelled]
        if not numbers or len(numbers)!=len(set(numbers)) or any(n<1 or n>12 for n in numbers):
            raise official.PipelineError('K_ISOLATION_RACE_IDENTITY')
        if cancelled:
            results.extend(strict(prefix + '\n' + '\n'.join(m.group(0) for m in cancelled)))
        for i, race in enumerate(races):
            key = f'{target:%Y%m%d}-{marker[1]}-{int(race[1]):02d}'
            if key in keys:
                raise official.PipelineError('K_DUPLICATE_RACE')
            keys.add(key)
            section = block[race.start():races[i+1].start() if i+1<len(races) else len(block)]
            section = cancelled_re.sub('', section)
            try:
                parsed = strict(prefix + '\n' + section)
                if len(parsed)!=1 or parsed[0].race_key!=key:
                    raise official.PipelineError('K_ISOLATION_PARSED_IDENTITY')
                results.extend(parsed)
            except official.PipelineError as exc:
                # Six unambiguous racer identities permit exact dependency
                # isolation. Otherwise keep the affected venue's history closed.
                identities = re.findall(r'(?m)^\s*(?:\d{1,2}|[A-Z][0-9]?)\s+([1-6])\s+(\d{4})\s+', section)
                racers = sorted(int(r) for _, r in identities) if (
                    sorted(int(b) for b, _ in identities)==list(range(1,7))
                    and len({r for _,r in identities})==6
                ) else []
                rejected.append({'RACE_KEY':key,'REASON':str(exc),
                    'SECTION_SHA256':hashlib.sha256(section.encode()).hexdigest(),
                    'RACER_NUMBERS':racers})
    return sorted(results,key=lambda r:r.race_key), sorted(rejected,key=lambda r:r['RACE_KEY'])


def dependency_scope(races, quarantine, rejected):
    healthy, held = [], list(quarantine)
    for race in races:
        affected = [r for r in rejected if r['RACE_KEY'][9:11]==race.venue_code
            and race.meet_start_date.replace('-','')<=r['RACE_KEY'][:8]<race.operational_date.replace('-','')
            and (not r['RACER_NUMBERS'] or set(r['RACER_NUMBERS']) & {e.racer_number for e in race.entries})]
        if affected:
            held.append({'RACE_KEY':race.race_key,
                'REASON':'PRIOR_K_RESULT_QUARANTINED:'+','.join(r['RACE_KEY'] for r in affected)})
        else:
            healthy.append(race)
    return healthy, sorted(held,key=lambda r:r['RACE_KEY'])


def prior_dates_for(races, target, official):
    if not races:
        raise official.PipelineError('MORNING_RACES_EMPTY')
    first = min(date.fromisoformat(r.meet_start_date) for r in races)
    days = [first+timedelta(days=i) for i in range((target-first).days)]
    if len(days)>6:
        raise official.PipelineError('MORNING_PRIOR_K_BUDGET')
    return days


def prepare_scope(runner, target, races, quarantine, official, parser):
    # At most six K reads plus B occur before the first 12-request scope is
    # sealed. Racer recovery still happens afterwards under its existing cap.
    days = prior_dates_for(races,target,official)
    rejected = []
    for day in days:
        _, member = runner._fetch_extract(f'result-k-{day:%Y%m%d}',official.result_url(day))
        _, held = isolated_results(official.decode_cp932(member),day,official,parser)
        rejected.extend(held)
    healthy, held = dependency_scope(races,quarantine,rejected)
    if rejected or held:
        print(json.dumps({'morning_input_isolation':{'healthy_races':len(healthy),
            'held_races':held,'rejected_results':rejected}},sort_keys=True),file=sys.stderr)
    return healthy, held, days, rejected


def replay_scope(store, verified, races, quarantine, extractor, official, parser):
    payload=verified['SNAPSHOT']['PAYLOAD']
    if 'RESULT_QUARANTINE' not in payload:
        return races, quarantine
    rejected=[]
    target=date.fromisoformat(payload['OPERATIONAL_DATE_JST'])
    receipts=verified['SNAPSHOT']['SOURCE_RECEIPTS']
    for day in prior_dates_for(races,target,official):
        source=f'result-k-{day:%Y%m%d}'
        receipt=store.response_receipt(source)
        if receipt not in receipts or receipt.get('URL')!=official.result_url(day):
            raise official.PipelineError('MORNING_RESULT_QUARANTINE_SOURCE_BINDING')
        raw=store.response_bytes(source)
        member=extractor.extract_one(raw,runner_member(official.result_url(day)))
        _,held=isolated_results(official.decode_cp932(member),day,official,parser)
        rejected.extend(held)
    if rejected!=payload['RESULT_QUARANTINE']:
        raise official.PipelineError('MORNING_RESULT_QUARANTINE_REPLAY')
    return dependency_scope(races,quarantine,rejected)


def runner_member(url):
    return url.rsplit('/',1)[1][:-4]+'.TXT'


def valid_quarantine(payload):
    """Only additive exclusions; no invalid record can enter admitted RAW38."""
    if type(payload) is not dict:
        return False
    q=payload.get('QUARANTINED_RACES',[])
    rejected=payload.get('RESULT_QUARANTINE',[])
    if type(q) is not list or len(q)>288 or type(rejected) is not list or len(rejected)>1728:
        return False
    try:
        target=date.fromisoformat(payload['OPERATIONAL_DATE_JST'])
        keys={f"{r['operational_date'].replace('-','')}-{r['venue_code']}-{r['race_no']:02d}" for r in payload['RACES']}
        seen=set();source_keys=set()
        for row in rejected:
            if type(row) is not dict or set(row)!={'RACE_KEY','REASON','SECTION_SHA256','RACER_NUMBERS'}:
                return False
            key=row['RACE_KEY'];racers=row['RACER_NUMBERS']
            if not re.fullmatch(r'\d{8}-(?:0[1-9]|1[0-9]|2[0-4])-(?:0[1-9]|1[0-2])',key):return False
            prior=date(int(key[:4]),int(key[4:6]),int(key[6:8]))
            if not 1<=(target-prior).days<=6 or key in source_keys:return False
            if not re.fullmatch('[a-f0-9]{64}',row['SECTION_SHA256']):return False
            if type(row['REASON']) is not str or not 0<len(row['REASON'])<512:return False
            if type(racers) is not list or (racers and (len(racers)!=6 or len(set(racers))!=6 or any(type(r) is not int or not 1000<=r<=9999 for r in racers))):return False
            source_keys.add(key)
        for row in q:
            if type(row) is not dict or set(row)!={'RACE_KEY','REASON'}:return False
            key=row['RACE_KEY'];reason=row['REASON']
            if not re.fullmatch(target.strftime('%Y%m%d')+r'-(?:0[1-9]|1[0-9]|2[0-4])-(?:0[1-9]|1[0-2])',key):return False
            if key in keys or key in seen:return False
            seen.add(key)
            if type(reason) is not str:return False
            if reason.startswith('PRIOR_K_RESULT_QUARANTINED:'):
                dependencies=reason.split(':',1)[1].split(',')
                if not dependencies or not set(dependencies)<=source_keys:return False
                if any(k[9:11]!=key[9:11] for k in dependencies):return False
            elif not re.fullmatch(f'B_ENTRY_CARDINALITY:{key[9:11]}:{int(key[12:])}:[0-9]+',reason):return False
        return not rejected or 'QUARANTINED_RACES' in payload
    except (KeyError,TypeError,ValueError):
        return False


def replace_pinned(function, digest, substitutions, additions):
    original=inspect.unwrap(function)
    source=inspect.getsource(original)
    if hashlib.sha256(source.encode()).hexdigest()!=digest:
        return function
    for before,after in substitutions:
        if source.count(before)!=1:
            raise ValueError('MORNING_ISOLATION_SOURCE_CONTRACT_CHANGED')
        source=source.replace(before,after,1)
    namespace=dict(original.__globals__);namespace.update(additions)
    exec(compile(textwrap.dedent(source),original.__code__.co_filename,'exec'),namespace)
    replacement=namespace[original.__name__]
    def rewrap(wrapper):
        if wrapper is original:return replacement
        inner=wrapper.__wrapped__
        patched_inner=rewrap(inner)
        cells=wrapper.__closure__ or ()
        if sum(cell.cell_contents is inner for cell in cells)!=1:
            raise ValueError('MORNING_ISOLATION_WRAPPER_CONTRACT_CHANGED')
        def cell_for(value):
            return (lambda:value).__closure__[0]
        # Keep the existing authority/LOCK boundary's exact code, globals and
        # every other closure cell. Only its wrapped input decoder changes.
        copy=types.FunctionType(wrapper.__code__,wrapper.__globals__,wrapper.__name__,
            wrapper.__defaults__,tuple(cell_for(patched_inner) if cell.cell_contents is inner else cell for cell in cells))
        copy.__dict__.update(wrapper.__dict__)
        copy.__wrapped__=patched_inner
        copy.__kwdefaults__=wrapper.__kwdefaults__
        copy.__annotations__=wrapper.__annotations__
        return copy
    return rewrap(function)


def adapted_morning(official, parser):
    return replace_pinned(official.CanonicalRunner.run_morning,
        '0c8a77815489c3e27104c576ca017bda72ff6afa99520c50f83ca4a8c7374997',[
        ('        _require(\n            1 <= len(races)',
         '        races, quarantined_races, prior_dates, result_quarantine = _isolation_prepare(self, target_date, races, quarantined_races)\n        _require(\n            1 <= len(races)'),
        ('len(quarantined_races) <= MAX_RACELIST_RECOVERY_RACES','len(quarantined_races) <= len(VENUES) * 12'),
        ('        earliest_start = min(date.fromisoformat(race.meet_start_date) for race in races)\n        prior_dates = [earliest_start + timedelta(days=offset) for offset in range((target_date - earliest_start).days)]\n',''),
        ('prior_results.extend(parse_result_text(decode_cp932(member), prior_date))','prior_results.extend(_isolation_parse(decode_cp932(member), prior_date))'),
        ('            "QUARANTINED_RACES": quarantined_races,','            "QUARANTINED_RACES": quarantined_races,\n            **({"RESULT_QUARANTINE": result_quarantine} if result_quarantine else {}),')],
        {'_isolation_prepare':lambda runner,target,races,q:prepare_scope(runner,target,races,q,official,parser),
         '_isolation_parse':lambda text,day:isolated_results(text,day,official,parser)[0]})


def adapted_night_binding(official, parser):
    return replace_pinned(official._night_sg_race_keys_from_saved_b,
        '3b2914e3be59b610d355feea6d0c87f15fa694108c183ea0c1a0b1dfdb2b6130',[
        ('    _require(quarantined == payload.get("QUARANTINED_RACES", []),',
         '    races, quarantined = _isolation_replay(store, verified, races, quarantined, extractor)\n    _require(quarantined == payload.get("QUARANTINED_RACES", []),')],
        {'_isolation_replay':lambda store,verified,races,q,extractor:replay_scope(store,verified,races,q,extractor,official,parser)})
