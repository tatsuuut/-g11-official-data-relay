"""Read-only comment provenance from the retained, verified MORNING snapshot.

This optional display adapter never fetches an input, writes a snapshot, runs
inference or changes a P3 material/weight. Legacy dates without an explicit
source-date attestation are deliberately not promoted into display evidence.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import importlib
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit

FIELD = "p3_motor_comments"
SCHEMA = "G11_P3_MOTOR_COMMENT_DISPLAY_V1"
MAX_PACKET_BYTES = 16_000
MAX_COMMENTS = 24
MAX_TOTAL_BYTES = 160_000
MAX_RACES = 288
SOFT_SECONDS = 1.25
JST = timezone(timedelta(hours=9))
SHA = re.compile(r"[a-f0-9]{64}")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def require(ok, code):
    if not ok:
        raise ValueError(code)


def clock(value):
    require(isinstance(value, str), "COMMENT_TIME")
    result = datetime.fromisoformat(value)
    require(result.utcoffset() == timedelta(hours=9), "COMMENT_TIMEZONE")
    return result


def source_date(row):
    # Copy source meaning; the page publication day is not the statement day.
    return row.get("observation_date_basis", row.get("OBSERVATION_DATE_BASIS", "UNKNOWN"))


def assessment(states):
    if "良好" in states and "弱め" in states:
        return "MIXED"
    if "良好" in states and states <= {"良好", "普通"}:
        return "POSITIVE"
    if "弱め" in states and states <= {"弱め", "普通"}:
        return "NEGATIVE"
    return "NEUTRAL"


def project_snapshot(race, verified, *, verify_packet, source_ok,
                     now=None):
    """Project only already authenticated source facts; do not alter them."""
    now = now or datetime.now(JST)
    key, day = race.get("key"), race.get("date_jst")
    require(isinstance(day, str) and date.fromisoformat(day).isoformat() == day,
            "COMMENT_DAY")
    require(day >= "2026-11-27" and key == day.replace("-", "") + "-" +
            race["venue_code"] + "-" + str(race["race"]).zfill(2), "COMMENT_RACE")
    deadline = clock(race["deadline_jst"])
    require(deadline.date().isoformat() == day, "COMMENT_DEADLINE")
    anchor = race.get("p3_morning_snapshot_sha256") or race.get("p3_snapshot_sha256")
    require(isinstance(anchor, str) and SHA.fullmatch(anchor)
            and verified.get("SNAPSHOT_SHA256") == anchor, "COMMENT_SNAPSHOT")
    envelope = verified["SNAPSHOT"]
    require(envelope.get("STAGE") == "MORNING"
            and envelope.get("NAME") == "current-growth-p3-" + key,
            "COMMENT_ENVELOPE")
    payload = envelope["PAYLOAD"]
    body = dict(payload)
    payload_sha = body.pop("CONTENT_SHA256", None)
    require(isinstance(payload_sha, str) and SHA.fullmatch(payload_sha)
            and digest(body) == payload_sha, "COMMENT_PAYLOAD_HASH")
    require(payload.get("SCHEMA") == "G11_CURRENT_GROWTH_P3_RUNTIME_V1"
            and payload.get("STATUS") == "CURRENT_GROWTH_P3_MORNING_FORMAL_OUTPUT"
            and payload.get("RACE_ID") == key and payload.get("SNAPSHOT") == "MORNING"
            and payload.get("LOGIC_ID") == "CURRENT_GROWTH_P3", "COMMENT_PAYLOAD")
    packet = payload.get("P3_MATERIALS_PACKET")
    if not isinstance(packet, dict):
        return None
    verify_packet(packet, race_id=key)
    require(packet.get("TARGET_DATE") == day and packet.get("RACE_ID") == key,
            "COMMENT_PACKET_RACE")
    sealed = clock(packet["SEALED_AT_JST"])
    require(sealed.date().isoformat() == day and sealed <= now and sealed < deadline,
            "COMMENT_SEAL")
    actors = {r["BOAT_NUMBER"]: r["RACER_NUMBER"] for r in packet["BOATS"]}
    motors = packet["MOTORS_BY_BOAT"]
    facts = packet.get("FOOTWORK_FACTS", [])
    require(isinstance(facts, list) and len(facts) <= 288, "COMMENT_FACTS_BOUND")
    groups = {}
    for fact in facts:
        try:
            require(isinstance(fact, dict), "COMMENT_FACT")
            boat, racer, motor = (fact[k] for k in ("boat", "racer", "motor"))
            require(all(type(n) is int and n > 0 for n in (boat, racer, motor))
                    and boat in range(1, 7) and actors[boat] == racer
                    and motors[str(boat)] == motor and fact["race"] == race["race"],
                    "COMMENT_ACTOR")
            observed = fact["observed_on"]
            require(observed == day, "COMMENT_NOT_SAME_DAY")
            basis = source_date(fact)
            require(basis == "VERIFIED_SOURCE_DATE", "COMMENT_DATE_BASIS_UNKNOWN")
            proof = fact.get("observation_date_evidence")
            require(isinstance(proof, dict) and len(canonical(proof)) <= 2048,
                    "COMMENT_DATE_PROOF")
            # The attestation must itself be sealed; no page-day reconstruction.
            require(proof.get("date") == observed
                    and proof.get("field") in ("RACER_STATEMENT_DATE", "REPORTER_OBSERVATION_DATE",
                                               "COMMENT_OBSERVATION_DATE")
                    and proof.get("source_url") == fact.get("url")
                    and isinstance(proof.get("source_sha256"), str)
                    and SHA.fullmatch(proof["source_sha256"]), "COMMENT_DATE_PROOF")
            require(any(source.get("URL") == proof["source_url"]
                        and source.get("SHA256") == proof["source_sha256"]
                        for source in packet.get("SOURCES", [])), "COMMENT_DATE_SOURCE_HASH")
            captured = clock(fact["captured_at"])
            published = clock(fact["published_at"]) if fact.get("published_at") else captured
            require(date.fromisoformat(observed) <= published.date()
                    and published <= captured <= sealed, "COMMENT_SOURCE_TIME")
            kind, url, excerpt = (fact[k] for k in ("kind", "url", "excerpt"))
            require(kind in ("RACER", "REPORTER") and isinstance(excerpt, str)
                    and 1 <= len(excerpt.strip()) <= 160 and isinstance(url, str)
                    and source_ok(url, race["venue_code"]), "COMMENT_SOURCE")
            require(proof["field"] == "COMMENT_OBSERVATION_DATE"
                    or (kind == "RACER" and proof["field"] == "RACER_STATEMENT_DATE")
                    or (kind == "REPORTER" and proof["field"] == "REPORTER_OBSERVATION_DATE"),
                    "COMMENT_DATE_KIND")
            parts = urlsplit(url)
            require(parts.scheme == "https" and not parts.username and not parts.password
                    and not parts.fragment, "COMMENT_SOURCE_URL")
            # Preserve date/proof in the dedup key: identical words on two days
            # are distinct evidence and never inherit the newer date.
            item = {k: fact[k] for k in ("boat", "racer", "motor", "kind", "url", "excerpt",
                                        "observed_on", "captured_at")}
            if fact.get("published_at"):
                item["published_at"] = fact["published_at"]
            item["observation_date_basis"] = basis
            item["observation_date_evidence"] = deepcopy(proof)
            identity = canonical(item)
            group = groups.setdefault(identity, [item, set()])
            state = fact.get("state")
            group[1].add(state if state in ("良好", "弱め", "普通", "寄り") else "UNKNOWN")
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    result = {"schema": SCHEMA, "race_id": key, "target_date": day,
              "source_snapshot_sha256": anchor, "packet_sha256": packet["CONTENT_SHA256"],
              "sealed_at": packet["SEALED_AT_JST"], "comments": []}
    for identity in sorted(groups):
        item, states = groups[identity]
        candidate = {**item, "assessment": assessment(states)}
        if len(result["comments"]) >= MAX_COMMENTS:
            break
        if len(canonical({**result, "comments": [*result["comments"], candidate]})) <= MAX_PACKET_BYTES:
            result["comments"].append(candidate)
    return result if result["comments"] else None


def enrich_feed(feed, *, store=None, verify_packet=None, source_ok=None,
                state_root=None, source_root=None, now=None,
                budget_seconds=SOFT_SECONDS, monotonic=time.monotonic):
    """Optional metadata only. Any unavailable proof leaves the source unchanged."""
    feed = without_comments(feed)
    started = monotonic()
    day = feed.get("operational_date_jst")
    if (not isinstance(day, str) or day < "2026-11-27"
            or feed.get("stage") not in ("MORNING", "PREDEADLINE", "NIGHT")
            or not isinstance(feed.get("races"), list)):
        return feed
    if store is None:
        state = Path(state_root or ".relay-state")
        source = Path(source_root or ".g11-private").resolve()
        if not (state / "daily-runtime" / day / "authority.json").is_file() or not source.is_dir():
            return feed
        old_path = list(sys.path)
        try:
            from daily_runtime_bootstrap_v1 import retained_module
            authority = retained_module(state, day).verify_runtime(state, day, source)
            binding = feed.get("daily_runtime") or {}
            require(binding.get("authority_sha256") == authority["authority_sha256"]
                    and binding.get("runtime_source_sha") == authority["runtime_source_sha"],
                    "COMMENT_AUTHORITY")
            sys.path.insert(0, str(source))
            official = importlib.import_module("g11.canonical.official_pipeline")
            materials = importlib.import_module("g11.growth_p3.v1_1.morning_materials_v1")
            require(Path(official.__file__).resolve().is_relative_to(source)
                    and Path(materials.__file__).resolve().is_relative_to(source),
                    "COMMENT_RETAINED_MODULE")
            store = official.OfficialArtifactStore(state / "store")
            verify_packet, source_ok = materials.verify_packet, materials.venue_comments._source_ok
        except Exception:
            print("G11_P3_COMMENT_DISPLAY=HELD_AUTHORITY_OR_SOURCE")
            return feed
        finally:
            sys.path[:] = old_path
    output = deepcopy(feed)
    total = 0
    for row in output["races"][:MAX_RACES]:
        if monotonic() - started >= budget_seconds:
            print("G11_P3_COMMENT_DISPLAY=HELD_SOFT_TIME_BUDGET")
            break
        try:
            verified = store.load_verified_snapshot("morning", "current-growth-p3-" + row["key"])
            projected = project_snapshot(row, verified, verify_packet=verify_packet,
                                         source_ok=source_ok, now=now)
            size = len(canonical(projected)) if projected else 0
            if projected and total + size <= MAX_TOTAL_BYTES:
                row[FIELD] = projected
                total += size
        except Exception:
            # Missing/old/invalid optional evidence must not stop formal output.
            continue
    return output


def without_comments(feed):
    if not any(FIELD in row for row in feed.get("races", [])):
        return feed
    output = deepcopy(feed)
    for row in output["races"]:
        row.pop(FIELD, None)
    return output
