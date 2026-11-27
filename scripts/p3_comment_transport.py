"""Optional P3 display envelope around the unchanged retained HTTP client."""
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import tempfile

import hot_delta_transport_v1 as _hot
from p3_motor_comment_display_v1 import FIELD, enrich_feed, without_comments


def __getattr__(name):
    # All unmodified hashing, HTTP, receipt and NIGHT helpers remain native.
    return getattr(_hot, name)


@contextmanager
def _display_scope():
    original = (_hot.BASE_RACE_FIELDS, _hot.PRE_FIELDS, _hot.NIGHT_FIELDS, _hot.create_delta)
    native_delta = _hot.create_delta
    def bounded_delta(incoming, accepted, base_sha, research_projection=None, *, sequence_offset=1):
        delta = native_delta(incoming, accepted, base_sha, research_projection,
                             sequence_offset=sequence_offset)
        core = without_comments(incoming)
        if (incoming["stage"] != "NIGHT" and len(_hot.compact(delta)) > _hot.BUDGET
                and core is not incoming):
            delta = native_delta(core, accepted, base_sha, research_projection,
                                 sequence_offset=sequence_offset)
            print("G11_P3_COMMENT_DISPLAY=HELD_DELTA_BUDGET")
        return delta
    try:
        _hot.BASE_RACE_FIELDS = _hot.BASE_RACE_FIELDS | {FIELD}
        _hot.PRE_FIELDS = (*_hot.PRE_FIELDS, FIELD)
        _hot.NIGHT_FIELDS = (*_hot.NIGHT_FIELDS, FIELD)
        _hot.create_delta = bounded_delta
        yield
    finally:
        _hot.BASE_RACE_FIELDS, _hot.PRE_FIELDS, _hot.NIGHT_FIELDS, _hot.create_delta = original


def project_morning_base(path, research_projection=None):
    original = Path(path).read_bytes()
    source = json.loads(original)
    enriched = enrich_feed(source)
    with tempfile.TemporaryDirectory(prefix="g11-comment-display-") as temp, _display_scope():
        derived = Path(temp) / "feed.json"
        derived.write_bytes(_hot.compact(enriched))
        try:
            _hot.project_morning_base(derived, research_projection)
        except RuntimeError as error:
            core = without_comments(enriched)
            if not str(error).startswith("HOT_BASE_OVER_BUDGET:") or core is enriched:
                raise
            derived.write_bytes(_hot.compact(core))
            _hot.project_morning_base(derived, research_projection)
            print("G11_P3_COMMENT_DISPLAY=HELD_MORNING_BUDGET")
        result = json.loads(derived.read_bytes())
        # This remains a projection of the original canonical feed, not a new
        # prediction or an independently authoritative source.
        result["hot_reference"]["source_feed_sha256"] = _hot.hash_bytes(original)
        Path(path).write_bytes(_hot.compact(result) + b"\n")


def create_delta(incoming, accepted, base_sha, research_projection=None, *, sequence_offset=1):
    if incoming.get("stage") == "NIGHT":
        # NIGHT transaction/source hashes must refer to the unchanged canonical
        # bytes. Already accepted display metadata survives an unrelated patch.
        return _hot.create_delta(incoming, accepted, base_sha, research_projection,
                                 sequence_offset=sequence_offset)
    with _display_scope():
        return _hot.create_delta(enrich_feed(incoming), accepted, base_sha, research_projection,
                                 sequence_offset=sequence_offset)


def publish(feed_path, origin, research_projection=None, *, sequence_offset=1):
    source = json.loads(Path(feed_path).read_bytes())
    if source.get("stage") == "NIGHT":
        return _hot.publish(feed_path, origin, research_projection, sequence_offset=sequence_offset)
    enriched = enrich_feed(source)
    with tempfile.TemporaryDirectory(prefix="g11-comment-display-") as temp, _display_scope():
        derived = Path(temp) / "feed.json"
        derived.write_bytes(_hot.compact(enriched))
        return _hot.publish(derived, origin, research_projection, sequence_offset=sequence_offset)


if __name__ == "__main__":
    try:
        if sys.argv[1] == "--morning-base":
            project_morning_base(Path(sys.argv[2]))
        elif sys.argv[1] == "--night-projection":
            # Immutable NIGHT projection itself remains the original helper.
            _hot.reproject_completed_night(Path(sys.argv[2]), Path(sys.argv[3]),
                                           sys.argv[4], sys.argv[5], sys.argv[6])
        else:
            publish(Path(sys.argv[1]), sys.argv[2].rstrip("/"))
    except Exception as error:
        print("G11_P3_COMMENT_TRANSPORT_FAIL=" + str(error), file=sys.stderr)
        raise SystemExit(1)
