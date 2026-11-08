"""Keep a read-only research projection failure outside operational delivery.

Only the optional original-display reader is isolated. Core feed construction,
official inputs, LOCK/source verification, results and transport still fail
closed. No research files are removed, repaired or declared complete.
"""
from contextlib import contextmanager
from functools import wraps
import json
import re
import sys


def unavailable(race_keys, error):
    reason = str(error)
    reason = reason if re.fullmatch(r'[A-Z][A-Z0-9_]{0,100}', reason) else type(error).__name__
    summary = {
        'available': False, 'status': 'TECHNICAL_HOLD',
        'source': 'BOAT_RACE_OFFICIAL_BOATCAST', 'boaters_used': False,
        'morning_audits': 0, 'captures': 0, 'night_settlements': 0,
        'profiles': ['A', 'P', 'B', 'C', 'D', 'E', 'F', 'G'],
        'promotion_gate': None, 'proposal_gate_300r': None,
        'daily_aggregate': None, 'production_mutated': False,
        'hold_reason': reason,
    }
    rows = {key: {
        'status': 'NOT_YET_ISSUED', 'source': 'BOAT_RACE_OFFICIAL_BOATCAST',
        'captured_at_jst': None, 'top_display_boat': None,
        'rising_boats': [], 'falling_boats': [], 'boats': [],
        'last_minute_inputs': None, 'profile_projections': None,
        'night_execution': None, 'production_mutated': False,
    } for key in race_keys}
    print('G11_OPTIONAL_DISPLAY_HOLD=' + json.dumps({
        'reason': reason, 'race_count': len(rows), 'production_mutated': False,
    }, sort_keys=True), file=sys.stderr)
    return summary, rows


@contextmanager
def isolate_optional_display(relay=None):
    if relay is None:
        from g11.relay import v1 as relay
    original = getattr(relay, '_display_research', None)
    if original is None:
        yield
        return
    if getattr(original, '_g11_optional_display_isolated', False) is True:
        yield
        return

    @wraps(original)
    def read(store, race_keys):
        # The catch ends at this read-only sidecar boundary. A failure in
        # build_feed, authority verification or publication is never caught.
        try:
            return original(store, race_keys)
        except Exception as error:
            return unavailable(race_keys, error)

    read._g11_optional_display_isolated = True
    relay._display_research = read
    try:
        yield
    finally:
        relay._display_research = original
