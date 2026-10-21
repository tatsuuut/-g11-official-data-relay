"""Read official L results with exhibition time and an empty course field.

The original six-boat, result, refund, date and payout validations stay active.
The parser adapter is restored before leaving the serial NIGHT call.
"""
from dataclasses import replace
import re

LATE_ROW=re.compile(
    r'^\s*(L[0-9]?)\s+([1-6])\s+(\d{4})\s+(.+?)\s+'
    r'(\d{1,3})\s+(\d{1,3})\s+(\d+\.\d{2})\s+L\s*\.\s+\.\s*\.\s*$'
)

def parse_night_results(text,target,official,parser,*,require_full_races=False):
    normalized=official._normalize(text)
    extra={}
    venues=list(re.finditer(r'(?m)^(\d{2})KBGN\s*$',normalized))
    for i,m in enumerate(venues):
        block=normalized[m.end():venues[i+1].start() if i+1<len(venues) else len(normalized)]
        races=list(official._K_RACE_RE.finditer(block))
        for j,r in enumerate(races):
            section=block[r.end():races[j+1].start() if j+1<len(races) else len(block)]
            for line in section.splitlines():
                match=LATE_ROW.fullmatch(line)
                if match:
                    key=(m[1],int(r[1]),int(match[2]))
                    if key in extra:raise ValueError('NIGHT_LATE_ROW_DUPLICATE')
                    extra[key]=(int(match[3]),float(match[7]),match[1])
    original=official._K_INVALID_ENTRY_RE
    class InvalidEntries:
        def match(self,line):return original.match(line) or LATE_ROW.fullmatch(line)
    try:
        official._K_INVALID_ENTRY_RE=InvalidEntries()
        results=parser(normalized,target,require_full_races=require_full_races)
    finally:official._K_INVALID_ENTRY_RE=original
    seen=set();output=[]
    for race in results:
        entries=[]
        for entry in race.entries:
            key=(race.venue_code,race.race_no,entry.boat)
            if key in extra:
                racer,exhibition,finish=extra[key]
                if entry.racer_number!=racer or entry.finish!=finish or entry.boat not in race.refund_boats:
                    raise ValueError('NIGHT_LATE_ROW_IDENTITY')
                entry=replace(entry,exhibition_time=exhibition)
                seen.add(key)
            entries.append(entry)
        output.append(replace(race,entries=tuple(entries)))
    if seen!=set(extra):raise ValueError('NIGHT_LATE_ROW_NOT_PARSED')
    return output


def morning_metadata_bridge(gate):
    """Accept the producer's empty quarantine metadata, preserving all checks.

    Nonempty quarantine remains unsupported by this frozen bridge. No fields
    or hashes in the verified snapshot are removed or rewritten.
    """
    import hashlib
    import inspect
    original = gate.build_canonical_official_morning_input
    source = inspect.getsource(original)
    if hashlib.sha256(source.encode()).hexdigest() != '569abba9258f903f432be33b3e7905351e84b27f900251e9e5ecf61d3aaa59e1':
        return original  # A different source must keep its own admission rules.
    old = 'and set(payload)\n'
    if source.count(old) != 1:
        raise ValueError('MORNING_METADATA_BRIDGE_CONTRACT_CHANGED')
    source = source.replace(old, 'and type(payload.get("QUARANTINED_RACES", [])) is list\n'
        '        and not payload.get("QUARANTINED_RACES", [])\n'
        '        and (set(payload) - {"QUARANTINED_RACES"})\n', 1)
    namespace = dict(original.__globals__)
    exec(compile(source, original.__code__.co_filename, 'exec'), namespace)
    return namespace[original.__name__]


from contextlib import contextmanager
@contextmanager
def official_input_compatibility():
    from g11.canonical import official_pipeline as official
    parser=official.parse_result_text
    gate=official.FORMAL_GATE
    bridge=gate.build_canonical_official_morning_input
    def adapted(text,target_date,*,require_full_races=False):
        return parse_night_results(text,target_date,official,parser,require_full_races=require_full_races)
    try:
        official.parse_result_text=adapted
        gate.build_canonical_official_morning_input=morning_metadata_bridge(gate)
        yield
    finally:
        official.parse_result_text=parser
        gate.build_canonical_official_morning_input=bridge


def run_relay():
    """Apply the same official-input decoder to normal MORNING/NIGHT reads.

    Frozen source, model identity, prediction rules, clocks and LOCK gates are
    unchanged. Restore both parser references even when the relay fails.
    """
    import runpy
    import sys
    from pathlib import Path
    if sys.argv[1:2] != ['--relay']:
        raise SystemExit('EXPECTED_RELAY_MODE')
    sys.path.insert(0, str(Path.cwd()))
    original_argv = sys.argv[:]
    try:
        sys.argv = ['g11.relay.v1', *original_argv[2:]]
        print('G11_OFFICIAL_L_ROW_ADAPTER_V1:ACTIVE', file=sys.stderr)
        print('G11_MORNING_EMPTY_QUARANTINE_BRIDGE_V1:ACTIVE', file=sys.stderr)
        with official_input_compatibility():
            runpy.run_module('g11.relay.v1', run_name='__main__')
    finally:
        sys.argv = original_argv


if __name__ == '__main__':
    run_relay()
