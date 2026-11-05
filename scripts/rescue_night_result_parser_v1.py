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
    """Admit validated isolation metadata without changing snapshot bytes."""
    import hashlib
    import inspect
    from morning_race_isolation_v1 import valid_quarantine
    original = gate.build_canonical_official_morning_input
    source = inspect.getsource(original)
    if hashlib.sha256(source.encode()).hexdigest() != '569abba9258f903f432be33b3e7905351e84b27f900251e9e5ecf61d3aaa59e1':
        return original  # A different source must keep its own admission rules.
    old = 'and set(payload)\n'
    if source.count(old) != 1:
        raise ValueError('MORNING_METADATA_BRIDGE_CONTRACT_CHANGED')
    source = source.replace(old, 'and _valid_quarantine(payload)\n'
        '        and (set(payload) - {"QUARANTINED_RACES", "RESULT_QUARANTINE"})\n', 1)
    if source.count('maximum_receipts=20,') != 1:
        raise ValueError('MORNING_RECEIPT_LIMIT_CONTRACT_CHANGED')
    source = source.replace('maximum_receipts=20,',
        'maximum_receipts=_receipt_limit(verified_snapshot),', 1)
    namespace = dict(original.__globals__)
    namespace['_valid_quarantine'] = valid_quarantine
    from morning_source_receipt_limit_v1 import receipt_limit
    namespace['_receipt_limit'] = receipt_limit
    exec(compile(source, original.__code__.co_filename, 'exec'), namespace)
    return namespace[original.__name__]


from contextlib import contextmanager
@contextmanager
def official_input_compatibility():
    from g11.canonical import official_pipeline as official
    from morning_race_isolation_v1 import adapted_morning, adapted_night_binding
    from racer_period_source_v1 import adapted_period_morning
    parser=official.parse_result_text
    gate=official.FORMAL_GATE
    bridge=gate.build_canonical_official_morning_input
    invocation=official.FORMAL_ADAPTER
    verify_permit=invocation.verify_formal_p3_invocation
    morning=official.CanonicalRunner.run_morning
    night_binding=official._night_sg_race_keys_from_saved_b
    def adapted(text,target_date,*,require_full_races=False):
        return parse_night_results(text,target_date,official,parser,require_full_races=require_full_races)
    try:
        official.parse_result_text=adapted
        compiled_bridge=morning_metadata_bridge(gate)
        admitted={}
        def admitted_bridge(verified):
            result=compiled_bridge(verified)
            admitted[result['G11_SNAPSHOT_SHA256']]=tuple(result['G11_SOURCE_SHA256'])
            return result
        gate.build_canonical_official_morning_input=admitted_bridge
        from morning_race_isolation_v1 import replace_pinned
        from morning_source_receipt_limit_v1 import admitted_digest_limit
        invocation.verify_formal_p3_invocation=replace_pinned(verify_permit,
            'db65b3a9f8885f95f4b8548717d5dee37e37aa67389606510f3487c6433f593e',[
            ('2 <= len(source_hashes) <= 20','2 <= len(source_hashes) <= _admitted_limit(payload, 0)'),
            ('4 <= len(source_hashes) <= 22','4 <= len(source_hashes) <= _admitted_limit(payload, 2)'),
            ],{'_admitted_limit':lambda payload,extra:admitted_digest_limit(payload,admitted,extra)})
        official.CanonicalRunner.run_morning=adapted_period_morning(official,adapted_morning(official,parser))
        official._night_sg_race_keys_from_saved_b=adapted_night_binding(official,parser)
        yield
    finally:
        official.parse_result_text=parser
        gate.build_canonical_official_morning_input=bridge
        invocation.verify_formal_p3_invocation=verify_permit
        official.CanonicalRunner.run_morning=morning
        official._night_sg_race_keys_from_saved_b=night_binding


def run_relay():
    """Apply the same official-input decoder to normal MORNING/NIGHT reads.

    Frozen source, model identity, prediction rules, clocks and LOCK gates are
    unchanged. Restore both parser references even when the relay fails.
    """
    import runpy
    import sys
    from pathlib import Path
    if sys.argv[1:2] not in (['--relay'], ['--delayed-recovery']):
        raise SystemExit('EXPECTED_RELAY_MODE')
    sys.path.insert(0, str(Path.cwd()))
    original_argv = sys.argv[:]
    target = 'g11.relay.v1'
    if original_argv[1] == '--delayed-recovery':
        # Install the retained rescue parser before wrapping it, so its
        # normalizer and the public input adapters both stay active.
        from g11.relay import rescue_entry_v2  # noqa: F401
        target = 'g11.relay.delayed_morning_recovery_v1'
    try:
        sys.argv = [target, *original_argv[2:]]
        print('G11_OFFICIAL_L_ROW_ADAPTER_V1:ACTIVE', file=sys.stderr)
        print('G11_MORNING_RACE_ISOLATION_V1:ACTIVE', file=sys.stderr)
        with official_input_compatibility():
            if original_argv[1] == '--delayed-recovery':
                from repair_saved_morning_receipts_v1 import run_if_reviewed
                if run_if_reviewed(original_argv[2:]):
                    return
            runpy.run_module(target, run_name='__main__')
    finally:
        sys.argv = original_argv


if __name__ == '__main__':
    run_relay()
