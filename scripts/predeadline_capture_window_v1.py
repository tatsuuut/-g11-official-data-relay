"""Start current LIVE acquisition at T-10 without rewriting a saved plan.

Only acquisition scheduling and the matching verified odds time window change.
The retained T-7 plan, real clocks, request ledger, parsers, predictors, model
identity, immutable saves and cutoff guards keep their original authority.
"""
from contextlib import contextmanager
from datetime import date, timedelta
import hashlib
import inspect

EFFECTIVE_FROM = date(2026, 11, 14)
CAPTURE_MINUTES = 10
ODDS_WINDOW_SHA = '7c018b903107ce8ca44eac3224dfc756c12f230ebecd7d41dfc1362bbe799344'


def effective_due(due, deadline):
    if (due.utcoffset() != timedelta(hours=9)
            or deadline.utcoffset() != timedelta(hours=9)
            or due.date() != deadline.date()
            or deadline-due != timedelta(minutes=7)):
        raise ValueError('EARLY_CAPTURE_PLAN_BINDING')
    return deadline-timedelta(minutes=CAPTURE_MINUTES) if deadline.date() >= EFFECTIVE_FROM else due


def enabled(now):
    return now.utcoffset() == timedelta(hours=9) and now.date() >= EFFECTIVE_FROM


@contextmanager
def verified_odds_window(official):
    """Admit real T-10 receipts only; RAW, URL, race and exact-120 checks remain."""
    runner = official.CanonicalRunner
    original = getattr(runner,'_validated_odds_only_market',None)
    if original is None:
        yield False
        return
    if hashlib.sha256(inspect.getsource(original).encode()).hexdigest() != ODDS_WINDOW_SHA:
        # A new retained implementation keeps its own admission rules. The
        # acquisition adapter also checks this compatibility before advancing.
        yield False
        return
    from morning_race_isolation_v1 import replace_pinned
    runner._validated_odds_only_market = replace_pinned(original, ODDS_WINDOW_SHA, [(
        '_require(due <= captured < deadline, "ODDS_ONLY_CAPTURE_WINDOW")',
        '_require(_capture_due(due, deadline) <= captured < deadline, "ODDS_ONLY_CAPTURE_WINDOW")',
    )], {'_capture_due': effective_due})
    runner._g11_ten_minute_odds_window = True
    try:
        yield True
    finally:
        runner._validated_odds_only_market = original
        del runner._g11_ten_minute_odds_window
