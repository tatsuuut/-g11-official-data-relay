"""Start current LIVE acquisition at T-10 without rewriting a saved plan.

Only acquisition scheduling and the matching verified odds time window change.
The retained T-7 plan, real clocks, request ledger, parsers, predictors, model
identity, immutable saves and cutoff guards keep their original authority.
"""
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from functools import wraps
import hashlib
import inspect

EFFECTIVE_FROM = date(2026, 11, 14)
CAPTURE_MINUTES = 10
ODDS_WINDOW_SHA = '7c018b903107ce8ca44eac3224dfc756c12f230ebecd7d41dfc1362bbe799344'
FORMAL_PLAN_SHA = '8c9d2dc0ffd441dc530bbe7832511f47f274e36778b5be1361e9d87a2eac5344'
STADIUM_FORMAL_PLAN_SHA = '4fae2103ef5b229ffaec22c32629822281d373f120a1d8d75c9cad8d9863fdac'
ALTERNATE_FORMAL_PLAN_SHA = '0a9f9f9b650292893a2499ee47b8d7eac9557f0ae9b00c93e775ed69940a8b73'
BOATCAST_FORMAL_PLAN_SHA = '80dff1a35f8a83275d388d36632c1d38ed7dd8106a0170bad40100113bc209bd'
AVAILABILITY_WINDOW_SHA = '4d36e7826f48db78fd4dd2b47aa445d3bae24f4b87bff4a02a15538e8cccba4d'


def effective_due(due, deadline):
    if (due.utcoffset() != timedelta(hours=9)
            or deadline.utcoffset() != timedelta(hours=9)
            or due.date() != deadline.date()
            or deadline-due != timedelta(minutes=7)):
        raise ValueError('EARLY_CAPTURE_PLAN_BINDING')
    return deadline-timedelta(minutes=CAPTURE_MINUTES) if deadline.date() >= EFFECTIVE_FROM else due


def enabled(now):
    return now.utcoffset() == timedelta(hours=9) and now.date() >= EFFECTIVE_FROM


def captured_in_window(due, deadline, captured):
    now = datetime.now(timezone(timedelta(hours=9)))
    return (captured.utcoffset() == timedelta(hours=9)
            and captured.date() == deadline.date()
            and effective_due(due, deadline) <= captured < deadline
            and captured <= now)


@contextmanager
def verified_odds_window(official):
    """Align all retained receipt gates before T-10 acquisition; no LOCK writes."""
    runner = official.CanonicalRunner
    original = getattr(runner,'_validated_odds_only_market',None)
    invocation = getattr(official,'FORMAL_ADAPTER',None)
    original_plan = getattr(invocation,'_bind_predeadline_plan',None)
    availability = getattr(official,'PREDEADLINE_AVAILABILITY',None)
    original_availability = getattr(availability,'_payload',None)
    if original is None or original_plan is None:
        yield False
        return
    plan_sha = hashlib.sha256(inspect.getsource(original_plan).encode()).hexdigest()
    if (hashlib.sha256(inspect.getsource(original).encode()).hexdigest() != ODDS_WINDOW_SHA
            or plan_sha not in (FORMAL_PLAN_SHA, STADIUM_FORMAL_PLAN_SHA,
                                ALTERNATE_FORMAL_PLAN_SHA, BOATCAST_FORMAL_PLAN_SHA)
            or (availability is not None and (original_availability is None
                or hashlib.sha256(inspect.getsource(original_availability).encode()).hexdigest()
                    != AVAILABILITY_WINDOW_SHA))):
        # A new retained implementation keeps its own admission rules. The
        # acquisition adapter also checks this compatibility before advancing.
        yield False
        return
    from morning_race_isolation_v1 import replace_pinned
    adapted_market = replace_pinned(original, ODDS_WINDOW_SHA, [(
        '_require(due <= captured < deadline, "ODDS_ONLY_CAPTURE_WINDOW")',
        '_require(_captured_in_window(due, deadline, captured), "ODDS_ONLY_CAPTURE_WINDOW")',
    )], {'_captured_in_window': captured_in_window})
    def compile_plan(function):
        source_sha = hashlib.sha256(inspect.getsource(function).encode()).hexdigest()
        if source_sha not in (FORMAL_PLAN_SHA, STADIUM_FORMAL_PLAN_SHA,
                              ALTERNATE_FORMAL_PLAN_SHA, BOATCAST_FORMAL_PLAN_SHA):
            raise ValueError('EARLY_CAPTURE_FORMAL_PLAN_SOURCE_CHANGED')
        return replace_pinned(function, source_sha, [(
            'and due <= captured < deadline,',
            'and _captured_in_window(due, deadline, captured),',
        )], {'_captured_in_window': captured_in_window})
    compiled_plans = {}
    @wraps(original_plan)
    def adapted_plan(*args, **kwargs):
        # Retain the T10 guard when the approved input overlay transplants
        # the reviewed plan decoder beneath this existing wrapper.
        if original_plan not in compiled_plans:
            compiled_plans[original_plan] = compile_plan(original_plan)
        return compiled_plans[original_plan](*args, **kwargs)
    adapted_availability = None
    if original_availability is not None:
        adapted_availability = replace_pinned(original_availability, AVAILABILITY_WINDOW_SHA, [(
            'and due <= captured < deadline',
            'and _captured_in_window(due, deadline, captured)',
        )], {'_captured_in_window': captured_in_window})
    runner._validated_odds_only_market = adapted_market
    invocation._bind_predeadline_plan = adapted_plan
    if adapted_availability is not None:
        availability._payload = adapted_availability
    runner._g11_ten_minute_odds_window = True
    try:
        yield True
    finally:
        runner._validated_odds_only_market = original
        invocation._bind_predeadline_plan = original_plan
        if original_availability is not None:
            availability._payload = original_availability
        del runner._g11_ten_minute_odds_window
