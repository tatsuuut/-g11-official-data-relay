"""Display dates never rewrite P3 materials, tickets or prediction authority."""
from copy import deepcopy
from datetime import datetime
import importlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import hot_delta_transport_v1 as hot
import p3_motor_comment_display_v1 as display
import p3_comment_transport as transport

DAY = "2026-11-27"
KEY = "20261127-08-11"
HASH = "a" * 64
SOURCE = "b" * 64
URL = "https://www.boatrace-tokoname.jp/news/comment"
SEALED = DAY + "T02:15:00+09:00"
NOW = datetime.fromisoformat(DAY + "T15:30:00+09:00")


def packet_verifier(packet, *, race_id):
    value = dict(packet)
    sha = value.pop("CONTENT_SHA256")
    if sha != display.digest(value) or packet["RACE_ID"] != race_id:
        raise ValueError("INVALID_PACKET")


def fixture(facts=None):
    row = {"race": 11, "boat": 1, "racer": 4501, "motor": 28,
           "observed_on": DAY, "captured_at": DAY + "T02:00:00+09:00",
           "published_at": DAY + "T01:55:00+09:00", "kind": "RACER",
           "url": URL, "excerpt": "伸びはいい。", "axis": "伸び", "state": "良好",
           "observation_date_basis": "VERIFIED_SOURCE_DATE",
           "observation_date_evidence": {"date": DAY, "field": "RACER_STATEMENT_DATE",
                                         "source_url": URL, "source_sha256": SOURCE}}
    packet = {"RACE_ID": KEY, "TARGET_DATE": DAY, "SEALED_AT_JST": SEALED,
              "BOATS": [{"BOAT_NUMBER": n, "RACER_NUMBER": 4500 + n} for n in range(1, 7)],
              "MOTORS_BY_BOAT": {str(n): 27 + n for n in range(1, 7)},
              "FOOTWORK_FACTS": deepcopy(facts if facts is not None else [row]),
              "SOURCES": [{"SOURCE_ID": "comment", "URL": URL, "SHA256": SOURCE,
                           "CAPTURED_AT_JST": DAY + "T02:00:00+09:00"}]}
    packet["CONTENT_SHA256"] = display.digest(packet)
    payload = {"SCHEMA": "G11_CURRENT_GROWTH_P3_RUNTIME_V1",
               "STATUS": "CURRENT_GROWTH_P3_MORNING_FORMAL_OUTPUT", "RACE_ID": KEY,
               "SNAPSHOT": "MORNING", "LOGIC_ID": "CURRENT_GROWTH_P3",
               "P3_MATERIALS_PACKET": packet, "PRODUCTION_BETS": ["1-2-3"],
               "P3_VENUE_COMMENT_TICKETS": {"SIGNS": {"1": 1}}}
    payload["CONTENT_SHA256"] = display.digest(payload)
    verified = {"SNAPSHOT_SHA256": HASH, "SNAPSHOT": {"STAGE": "MORNING",
                "NAME": "current-growth-p3-" + KEY, "PAYLOAD": payload}}
    race = {"key": KEY, "date_jst": DAY, "venue_code": "08", "venue": "常滑",
            "race": 11, "deadline_jst": DAY + "T15:32:00+09:00", "formal_status": "FORMAL",
            "p3_morning_snapshot_sha256": HASH, "p3_snapshot_sha256": HASH,
            "practical_bets": ["1-2-3"], "production_points": 1}
    feed = {"stage": "PREDEADLINE", "operational_date_jst": DAY, "races": [race],
            "source": {"run_id": 1}, "generated_at_jst": NOW.isoformat()}
    return row, feed, verified


def projected(feed, verified, verify=packet_verifier):
    return display.project_snapshot(feed["races"][0], verified, verify_packet=verify,
                                    source_ok=lambda url, venue: url == URL and venue == "08", now=NOW)


class ProjectionTests(unittest.TestCase):
    def test_source_attested_same_day_and_snapshot_bind_without_mutation(self):
        row, feed, verified = fixture()
        before = deepcopy((feed, verified))
        result = projected(feed, verified)
        self.assertEqual(result["source_snapshot_sha256"], HASH)
        self.assertEqual(result["comments"][0]["assessment"], "POSITIVE")
        self.assertEqual(result["comments"][0]["observation_date_evidence"], row["observation_date_evidence"])
        self.assertEqual((feed, verified), before)

    def test_previous_or_unknown_date_never_borrow_publication_day(self):
        row, _, _ = fixture()
        for changes in ({"observed_on": "2026-11-26"}, {"observation_date_basis": "UNKNOWN"},
                        {"observation_date_basis": "INFERRED"},
                        {"observation_date_evidence": {**row["observation_date_evidence"], "field": "PUBLISHED_DATE"}},
                        {"observation_date_evidence": {**row["observation_date_evidence"], "source_sha256": "c" * 64}}):
            with self.subTest(changes=changes):
                _, feed, verified = fixture([{**row, **changes}])
                self.assertIsNone(projected(feed, verified))
                self.assertEqual(verified["SNAPSHOT"]["PAYLOAD"]["P3_VENUE_COMMENT_TICKETS"]["SIGNS"], {"1": 1})
        # The same sentence on the prior day must not be assigned today's date.
        _, feed, verified = fixture([{**row, "observed_on": "2026-11-26"}, row])
        self.assertEqual(len(projected(feed, verified)["comments"]), 1)

    def test_sentiment_uses_sealed_state_not_good_keyword_or_generated_text(self):
        row, _, _ = fixture()
        for states, expected in ((["良好"], "POSITIVE"), (["良好", "普通"], "POSITIVE"),
                                 (["弱め"], "NEGATIVE"), (["良好", "弱め"], "MIXED"),
                                 ([None], "NEUTRAL"), (["寄り"], "NEUTRAL")):
            _, feed, verified = fixture([{**row, "state": state} for state in states])
            self.assertEqual(projected(feed, verified)["comments"][0]["assessment"], expected)

    def test_bad_race_actor_motor_seal_or_packet_cannot_show(self):
        row, _, _ = fixture()
        for change in ({"boat": 2}, {"racer": 4502}, {"motor": 29},
                       {"race": 12}, {"captured_at": DAY + "T03:00:00+09:00"},
                       {"kind": "REPORTER"}):
            _, feed, verified = fixture([{**row, **change}])
            self.assertIsNone(projected(feed, verified))
        _, feed, verified = fixture()
        verified["SNAPSHOT_SHA256"] = "c" * 64
        with self.assertRaisesRegex(ValueError, "COMMENT_SNAPSHOT"):
            projected(feed, verified)
        _, feed, verified = fixture()
        verified["SNAPSHOT"]["PAYLOAD"]["P3_MATERIALS_PACKET"]["CONTENT_SHA256"] = "f" * 64
        with self.assertRaises(ValueError):
            projected(feed, verified)

    def test_verified_store_read_only_all_stages_and_missing_proof_optional(self):
        _, feed, verified = fixture()
        calls = []
        class Store:
            def load_verified_snapshot(self, stage, name):
                calls.append((stage, name))
                return deepcopy(verified)
        before = deepcopy((feed, verified))
        for stage in ("MORNING", "PREDEADLINE", "NIGHT"):
            source = {**feed, "stage": stage}
            result = display.enrich_feed(source, store=Store(), verify_packet=packet_verifier,
                                         source_ok=lambda *a: True, now=NOW)
            self.assertIn(display.FIELD, result["races"][0])
            self.assertEqual(display.without_comments(result), source)
        self.assertEqual((feed, verified), before)
        self.assertEqual(calls, [("morning", "current-growth-p3-" + KEY)] * 3)
        class Missing:
            def load_verified_snapshot(self, *args):
                raise FileNotFoundError()
        self.assertEqual(display.enrich_feed(feed, store=Missing(), verify_packet=packet_verifier,
                                            source_ok=lambda *a: True), feed)

    def test_optional_elapsed_budget_prevents_further_snapshot_io_and_unverified_metadata_is_removed(self):
        _, feed, _ = fixture()
        feed["races"][0][display.FIELD] = {"forged": "not verified"}
        calls = []
        class Store:
            def load_verified_snapshot(self, *args):
                calls.append(args)
                raise AssertionError("elapsed display task must not read another snapshot")
        ticks = iter([0.0, 1.5])
        result = display.enrich_feed(feed, store=Store(), verify_packet=packet_verifier,
                                    source_ok=lambda *a: True, monotonic=lambda: next(ticks))
        self.assertEqual(calls, [])
        self.assertNotIn(display.FIELD, result["races"][0])
        self.assertIn(display.FIELD, feed["races"][0])

    def test_native_material_verifier_rejects_bad_packet_without_reclassifying_input(self):
        candidates = [Path(os.environ.get("G11_PRIVATE_ROOT", ".g11-private")),
                      Path(__file__).resolve().parents[2] / "exhibition-frozen-source"]
        root = next((p.resolve() for p in candidates if (p / "g11/growth_p3/v1_1/morning_materials_v1.py").is_file()), None)
        if root is None:
            self.skipTest("retained private source not present")
        sys.path.insert(0, str(root))
        try:
            materials = importlib.import_module("g11.growth_p3.v1_1.morning_materials_v1")
            row, feed, verified = fixture()
            packet = materials.build_packet(KEY, SEALED, {
                "RACERS_BY_BOAT": {n: 4500 + n for n in range(1, 7)},
                "MOTORS_BY_BOAT": {n: 27 + n for n in range(1, 7)},
                "VENUE_FOOTWORK": [row], "SOURCES": verified["SNAPSHOT"]["PAYLOAD"]["P3_MATERIALS_PACKET"]["SOURCES"]},
                base_rows=[{"BOAT_NUMBER": n} for n in range(1, 7)], day1=False)
            payload = verified["SNAPSHOT"]["PAYLOAD"]
            payload["P3_MATERIALS_PACKET"] = packet
            payload["CONTENT_SHA256"] = display.digest({k: v for k, v in payload.items() if k != "CONTENT_SHA256"})
            before = deepcopy(payload)
            self.assertEqual(projected(feed, verified, materials.verify_packet)["comments"][0]["assessment"], "POSITIVE")
            self.assertEqual(payload, before)
        finally:
            sys.path.pop(0)


class TransportTests(unittest.TestCase):
    def test_night_uses_original_canonical_bytes_and_native_helpers_without_enrichment(self):
        _, feed, _ = fixture()
        feed["stage"] = "NIGHT"
        before = deepcopy(feed)
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "feed.json"
            raw = hot.compact(feed); path.write_bytes(raw)
            with patch.object(transport, "enrich_feed", side_effect=AssertionError("NIGHT enrichment prohibited")), \
                    patch.object(hot, "publish", return_value="native") as publisher:
                self.assertEqual(transport.publish(path, "https://example.invalid"), "native")
                self.assertIs(publisher.call_args.args[0], path)
            self.assertEqual(path.read_bytes(), raw)
        with patch.object(transport, "enrich_feed", side_effect=AssertionError("NIGHT enrichment prohibited")), \
                patch.object(hot, "create_delta", return_value={"native": True}) as create:
            self.assertEqual(transport.create_delta(feed, {}, HASH), {"native": True})
            self.assertIs(create.call_args.args[0], feed)
        self.assertEqual(feed, before)

    def test_existing_hot_projection_carries_only_new_metadata(self):
        _, accepted, verified = fixture()
        incoming = deepcopy(accepted)
        packet = projected(incoming, verified)
        incoming["races"][0][display.FIELD] = packet
        native = (hot.BASE_RACE_FIELDS, hot.PRE_FIELDS, hot.NIGHT_FIELDS, hot.create_delta)
        with patch.object(transport, "enrich_feed", side_effect=lambda value: value):
            delta = transport.create_delta(incoming, accepted, "c" * 64)
        self.assertEqual(delta["RACES"][0]["CHANGED_FIELDS"], {display.FIELD: packet})
        self.assertEqual(delta["RACES"][0]["EXPECTED_RACE_VERSION"], HASH)
        self.assertEqual(display.without_comments(incoming), accepted)
        self.assertEqual(native, (hot.BASE_RACE_FIELDS, hot.PRE_FIELDS, hot.NIGHT_FIELDS, hot.create_delta))

    def test_morning_optional_budget_drops_metadata_and_preserves_core(self):
        _, feed, _ = fixture()
        feed["stage"] = "MORNING"
        augmented = deepcopy(feed)
        augmented["races"][0][display.FIELD] = {"oversized": "x" * 5000}
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "feed.json"
            raw = hot.compact(feed)
            path.write_bytes(raw)
            with patch.object(transport, "enrich_feed", return_value=augmented), patch.object(hot, "BASE_BUDGET", 2000):
                transport.project_morning_base(path)
            result = json.loads(path.read_bytes())
            reference = result.pop("hot_reference")
            self.assertEqual(reference["source_feed_sha256"], hot.hash_bytes(raw))
            self.assertEqual(result, feed)

    def test_delta_optional_budget_drops_metadata_before_network_post(self):
        _, feed, _ = fixture()
        augmented = deepcopy(feed)
        augmented["races"][0][display.FIELD] = {"oversized": "x" * 5000}
        seen = []
        def make_delta(incoming, *args, **kwargs):
            seen.append(deepcopy(incoming))
            return {"RACES": [{"CHANGED_FIELDS": {display.FIELD: incoming["races"][0][display.FIELD]}}], "TOP_LEVEL": {}} if display.FIELD in incoming["races"][0] else {"RACES": [], "TOP_LEVEL": {}}
        original = Path.cwd()
        with TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                Path(".relay-output").mkdir()
                path = Path("feed.json"); path.write_bytes(hot.compact(feed))
                with patch.object(transport, "enrich_feed", return_value=augmented), patch.object(hot, "create_delta", side_effect=make_delta), patch.object(hot, "BUDGET", 2000), patch.object(hot, "fetch", return_value=(feed, {"x-g11-source-feed-sha256": HASH}, b"{}")), patch.dict(os.environ, {"GITHUB_OUTPUT": str(Path(tmp) / "output")}):
                    self.assertEqual(transport.publish(path, "https://example.invalid")[0], feed)
                self.assertIn(display.FIELD, seen[0]["races"][0])
                self.assertEqual(seen[1], feed)
                self.assertEqual(json.loads(path.read_bytes()), feed)
            finally:
                os.chdir(original)


if __name__ == "__main__":
    unittest.main()
