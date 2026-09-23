#!/usr/bin/env python3
"""Project a verified G11 feed onto the race-field set already accepted by the Site.

This is transport-only compatibility logic. It never mutates canonical artifacts,
prediction snapshots, morning locks, production bets, or research ledgers.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit("G11_SITE_COMPAT_JSON_OBJECT")
    return value


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: g11_site_race_field_compat.py FEED ACCEPTED_REFERENCE")

    feed_path = Path(sys.argv[1])
    reference_path = Path(sys.argv[2])
    feed = _load(feed_path)
    accepted = _load(reference_path)

    accepted_races = [row for row in accepted.get("races", []) if isinstance(row, dict)]
    incoming = feed.get("races")
    if not accepted_races or not isinstance(incoming, list):
        raise SystemExit("G11_SITE_COMPAT_REFERENCE_INVALID")
    if any(not isinstance(row, dict) for row in incoming):
        raise SystemExit("G11_SITE_COMPAT_INCOMING_RACE_INVALID")

    allowed = set().union(*(row.keys() for row in accepted_races))
    incoming_fields = set().union(*(row.keys() for row in incoming)) if incoming else set()
    dropped = sorted(incoming_fields - allowed)
    missing = sorted(allowed - incoming_fields)

    print("G11_ACCEPTED_RACE_FIELDS=" + json.dumps(sorted(allowed), ensure_ascii=False))
    print("G11_INCOMING_RACE_FIELDS_DROP=" + json.dumps(dropped, ensure_ascii=False))
    print("G11_INCOMING_RACE_FIELDS_MISSING=" + json.dumps(missing, ensure_ascii=False))

    feed["races"] = [
        {key: value for key, value in row.items() if key in allowed}
        for row in incoming
    ]
    encoded = json.dumps(
        feed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ) + "\n"
    feed_path.write_text(encoded, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
