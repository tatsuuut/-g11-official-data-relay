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

def parse_night_results(text,target,official,parser):
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
        results=parser(normalized,target,require_full_races=False)
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
