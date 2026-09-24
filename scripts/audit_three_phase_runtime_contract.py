#!/usr/bin/env python3
"""Fail-closed static contract audit for G11 MORNING/PREDEADLINE/NIGHT runtime.

This audit is intentionally independent from the production runner.  Its job is
not to predict races or mutate runtime state.  It proves that the three
scheduler/phase routes, publication/readback path, and the private relay phase
hooks are still present before a change is accepted.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
FREE = ROOT / ".github" / "workflows" / "g11-free-runner.yml"
NIGHT = ROOT / ".github" / "workflows" / "g11-explicit-night-dispatcher.yml"


class ContractError(RuntimeError):
    pass


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ContractError(code)


def require_text(text: str, needle: str, code: str) -> None:
    require(needle in text, code)


def audit_public() -> dict[str, object]:
    free = FREE.read_text(encoding="utf-8")
    night = NIGHT.read_text(encoding="utf-8")

    # MORNING scheduler and resolver must stay coupled exactly.
    morning_crons = (
        'cron: "15-55/5 17 * * *"',
        'cron: "*/5 18-21 * * *"',
        'cron: "0-5/5 22 * * *"',
    )
    for cron in morning_crons:
        require_text(free, cron, f"MORNING_CRON_MISSING:{cron}")
    require_text(
        free,
        '"15-55/5 17 * * *"|"*/5 18-21 * * *"|"0-5/5 22 * * *")',
        "MORNING_RESOLVER_SET",
    )
    require_text(free, 'phase="morning"', "MORNING_RESOLVER_PHASE")

    # PREDEADLINE scheduler and resolver must stay coupled exactly.
    pre_crons = (
        'cron: "*/5 23 * * *"',
        'cron: "*/5 0-11 * * *"',
    )
    for cron in pre_crons:
        require_text(free, cron, f"PREDEADLINE_CRON_MISSING:{cron}")
    require_text(
        free,
        '"*/5 23 * * *"|"*/5 0-11 * * *")',
        "PREDEADLINE_RESOLVER_SET",
    )
    require_text(free, 'phase="predeadline"', "PREDEADLINE_RESOLVER_PHASE")

    # NIGHT must be dispatched only by the explicit dispatcher.
    require('cron: "*/5 13-16 * * *"' not in free, "NIGHT_CRON_LEAK_IN_FREE_RUNNER")
    require_text(night, 'cron: "*/5 13-16 * * *"', "NIGHT_CRON_MISSING")
    require_text(
        night,
        'actions/workflows/g11-free-runner.yml/dispatches',
        "NIGHT_DISPATCH_TARGET",
    )
    require_text(night, 'phase:"night"', "NIGHT_DISPATCH_PHASE")

    # Main runner cannot silently lose its safety/readback/publication chain.
    required_steps = (
        "Audit public surface",
        "Resolve the bounded phase and race day",
        "Capture due PREDEADLINE official inputs before regression checks",
        "Run the private fail-closed relay",
        "Audit WILD4 training-food capture",
        "Gate NIGHT publication on fully settled research",
        "Publish a newly verified feed through OIDC",
        "Verify source identity and projected app readback",
        "Keep the predeadline relay alive between delayed cron starts",
        "Propagate private runtime failure after state checkpoint",
    )
    for name in required_steps:
        require_text(free, f"- name: {name}", f"RUNNER_STEP_MISSING:{name}")

    require_text(
        free,
        "vars.G11_RELAY_ENABLED == 'true'",
        "RUNNER_ENABLE_GATE_MISSING",
    )
    require_text(free, "cancel-in-progress: false", "RUNNER_CONCURRENCY_POLICY")
    require_text(free, "G11_APP_ORIGIN:", "APP_ORIGIN_CONTRACT")
    require_text(free, "G11_RUNTIME_BRANCH:", "RUNTIME_BRANCH_CONTRACT")

    # PREDEADLINE reliability contract: cron delay must not suppress the
    # self-dispatch heartbeat.  Only another workflow_dispatch continuation
    # may block dispatch; TRUE-AI artifacts are not an availability gate.
    continuation_start = free.index(
        "- name: Keep the predeadline relay alive between delayed cron starts"
    )
    continuation_end = free.index(
        "- name: Propagate private runtime failure after state checkpoint"
    )
    continuation = free[continuation_start:continuation_end]
    require_text(
        continuation,
        '.event == "workflow_dispatch"',
        "PREDEADLINE_CHAIN_DISPATCH_ONLY_DEDUPE",
    )
    require_text(
        continuation,
        "sleep 45",
        "PREDEADLINE_CHAIN_SPACING",
    )
    require_text(
        continuation,
        'actions/workflows/g11-free-runner.yml/dispatches',
        "PREDEADLINE_CHAIN_DISPATCH_TARGET",
    )
    require(
        "morning-predictions.manifest.json" not in continuation,
        "PREDEADLINE_CHAIN_TRUE_AI_GATE",
    )
    require(
        '.event == "schedule"' not in continuation,
        "PREDEADLINE_CHAIN_CRON_MUST_NOT_BLOCK",
    )

    return {
        "MORNING": "PASS",
        "PREDEADLINE": "PASS",
        "NIGHT": "PASS",
        "PUBLICATION_READBACK_CHAIN": "PASS",
    }


def audit_private(private_root: Path) -> dict[str, object]:
    relay = private_root / "g11" / "relay" / "v1.py"
    require(relay.is_file(), "PRIVATE_RELAY_MISSING")
    text = relay.read_text(encoding="utf-8")

    # Morning canonical/recovery windows must remain explicit and bounded.
    require_text(
        text,
        "canonical_deadline = datetime.combine(target, datetime_time(7, 10), JST)",
        "MORNING_CANONICAL_DEADLINE",
    )
    require_text(
        text,
        "recovery_deadline = datetime.combine(target, datetime_time(8, 0), JST)",
        "MORNING_RECOVERY_DEADLINE",
    )
    require_text(text, "MORNING_RECOVERY_WINDOW_CLOSED", "MORNING_RECOVERY_FAIL_CLOSED")

    # Three independent phase hooks must remain present.
    require_text(
        text,
        'if phase in {"morning", "predeadline"}:',
        "PRIVATE_MORNING_PREDEADLINE_PHASE_HOOK",
    )
    require_text(text, "wild_runtime.run_morning(store, target, now)", "WILD_MORNING_HOOK")
    require_text(text, "wild_runtime.run_predeadline(store, target)", "WILD_PREDEADLINE_HOOK")
    require_text(text, 'if phase == "night":', "PRIVATE_NIGHT_PHASE_HOOK")
    require_text(text, "wild_runtime.run_night(store, target)", "WILD_NIGHT_HOOK")
    require_text(text, "current_growth_p3.publish_future(", "GROWTH_P3_PUBLISH_HOOK")
    require_text(text, "FEED_EMITTED", "PRIVATE_FEED_EMISSION_CONTRACT")

    # A PREDEADLINE invocation must not masquerade as MORNING.
    require(
        re.search(r'if phase in \{"morning", "predeadline"\}:', text) is not None,
        "PHASE_SET_PARSE",
    )

    return {
        "PRIVATE_RELAY": "PASS",
        "MORNING_WINDOW": "PASS",
        "WILD_PHASE_HOOKS": "PASS",
        "GROWTH_P3_HOOK": "PASS",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-root", type=Path)
    args = parser.parse_args()
    result = {"PUBLIC": audit_public()}
    if args.private_root is not None:
        result["PRIVATE"] = audit_private(args.private_root.resolve())
    print("G11_THREE_PHASE_RUNTIME_CONTRACT=" + json.dumps(
        result, ensure_ascii=False, sort_keys=True
    ))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ContractError as exc:
        print(f"G11_THREE_PHASE_RUNTIME_CONTRACT_FAIL:{exc}", file=sys.stderr)
        raise SystemExit(1)
