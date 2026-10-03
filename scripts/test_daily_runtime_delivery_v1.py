"""Delivery fault injection uses isolated fixture state and a staged Site double."""
from __future__ import annotations
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import daily_runtime_bootstrap_v1 as bootstrap
import daily_runtime_delivery_v1 as delivery
import hot_delta_transport_v1 as hot
from g11.relay import daily_runtime_authority_v1 as d
from g11.relay.test_daily_runtime_authority_v1 import Scenario, DAY


class StagedSite:
    def __init__(self, scenario, *, failure=None):
        self.scenario = scenario
        self.feed = copy.deepcopy(scenario.settlement())
        self.feed["stage"] = "PREDEADLINE"
        self.feed["daily_runtime"] = d.feed_binding(scenario.authority, "PREDEADLINE")
        self.feed["counts"].update(results=0, pending=1)
        for row in self.feed["races"]:
            for field in ("result_trifecta", "payout", "research_finance", "result_meta"):
                row.pop(field, None)
        self.sha = "f"*64
        self.manifest = None
        self.chunks = {}
        self.actions = []
        self.failure = failure
        self.failed = False
        self.commits = 0

    def fetch(self, url):
        data = hot.compact(self.feed)
        headers = {"x-g11-source-feed-sha256": self.sha, "x-g11-feed-sha256": hot.hash_bytes(data),
                   "x-g11-daily-authority-sha256": self.scenario.authority["authority_sha256"]}
        if self.feed.get("transport"):
            headers["x-g11-canonical-feed-sha256"] = self.feed["transport"]["canonical_feed_sha256"]
        return copy.deepcopy(self.feed), headers, data

    def post(self, origin, token, payload, endpoint):
        self.actions.append(copy.deepcopy(payload))
        action = payload["ACTION"]
        if self.failure == "chunk" and action == "CHUNK" and not self.failed:
            self.failed = True
            # A staged chunk failure never publishes any NIGHT content.
            assert self.feed["stage"] == "PREDEADLINE"
            raise RuntimeError("INJECTED_DELIVERY_FAILURE")
        if action == "MANIFEST":
            if self.manifest is not None:
                assert payload == self.manifest
            self.manifest = payload
        elif action == "CHUNK":
            assert payload["MANIFEST_SHA256"] == self.manifest["MANIFEST_SHA256"]
            self.chunks[payload["CHUNK_INDEX"]] = payload
        elif action == "COMMIT":
            assert payload["MANIFEST_SHA256"] == self.manifest["MANIFEST_SHA256"]
            if self.feed["stage"] != "NIGHT":
                assert len(self.chunks) == self.manifest["CHUNK_COUNT"]
                feed = self.scenario.settlement()
                manifest = self.manifest
                feed["transport"] = {"night_transaction_id": manifest["NIGHT_TRANSACTION_ID"],
                    "night_manifest_sha256": manifest["MANIFEST_SHA256"],
                    "canonical_feed_sha256": manifest["CANONICAL_FEED_SHA256"],
                    "night_checkpoint_sha256": manifest["CHECKPOINT_SHA256"],
                    "daily_authority_sha256": manifest["DAILY_AUTHORITY_SHA256"]}
                self.feed = feed
                self.sha = hot.hash_bytes(hot.compact(feed))
                self.commits += 1
            if self.failure == "commit-ack" and not self.failed:
                self.failed = True
                raise RuntimeError("INJECTED_COMMIT_ACK_LOSS")
            return {"status": "PASS", "payload_sha256": self.sha}
        return {"status": "PASS"}


class DailyDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.s = Scenario(self.root / "fixture")
        self.checkpoint = self.s.checkpoint()
        self.original = self.s.feed_path.read_bytes()
        self.work = self.root / "delivery"
        self.work.mkdir()
        self.cwd = Path.cwd()
        os.chdir(self.work)
        self.addCleanup(os.chdir, self.cwd)
        self.env = patch.dict(os.environ, {"G11_SYNC_OIDC": "fixture-token"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_C_chunk_failure_retries_checkpoint_transport_only(self):
        site = StagedSite(self.s, failure="chunk")
        # Any attempt to settle after checkpoint creation is a regression.
        site.scenario.settlement = lambda: json.loads(self.original)
        with patch.object(hot, "fetch", site.fetch), patch.object(hot, "post", site.post):
            result = delivery.deliver(self.s.state, self.s.feed_path, delivery.ORIGIN, pause=lambda _: None)
        self.assertEqual(site.commits, 1)
        manifests = [x for x in site.actions if x["ACTION"] == "MANIFEST"]
        self.assertEqual(len(manifests), 2)
        self.assertEqual(manifests[0], manifests[1])
        self.assertEqual(result["daily_operation_complete"], "PASS")
        self.assertEqual(self.s.feed_path.read_bytes(), self.original)
        self.assertEqual(d.read_checkpoint(self.s.state, DAY), self.checkpoint)

    def test_committed_ack_loss_repeats_only_commit_and_get(self):
        site = StagedSite(self.s, failure="commit-ack")
        site.scenario.settlement = lambda: json.loads(self.original)
        with patch.object(hot, "fetch", site.fetch), patch.object(hot, "post", site.post):
            delivery.deliver(self.s.state, self.s.feed_path, delivery.ORIGIN, pause=lambda _: None)
        self.assertEqual(site.commits, 1)
        self.assertEqual([x["ACTION"] for x in site.actions].count("MANIFEST"), 1)
        self.assertEqual([x["ACTION"] for x in site.actions].count("COMMIT"), 2)

    def test_delivery_retry_is_bounded_and_never_rewrites_checkpoint(self):
        site = StagedSite(self.s)
        with patch.object(hot, "fetch", site.fetch), patch.object(hot, "post", side_effect=RuntimeError("HTTP_503")) as send:
            with self.assertRaisesRegex(RuntimeError, "HTTP_503"):
                delivery.deliver(self.s.state, self.s.feed_path, delivery.ORIGIN, pause=lambda _: None)
        self.assertEqual(send.call_count, 3)
        self.assertEqual(d.read_checkpoint(self.s.state, DAY), self.checkpoint)
        self.assertFalse((d.day_root(self.s.state, DAY) / "daily-operation-complete.json").exists())

    def test_html_response_retries_without_checkpoint_mutation(self):
        site = StagedSite(self.s)
        responses = [json.JSONDecodeError("HTML gateway response", "<html>", 0)]
        def fetch(url):
            if responses:
                raise responses.pop()
            return site.fetch(url)
        with patch.object(hot, "fetch", fetch), patch.object(hot, "post", site.post):
            delivery.deliver(self.s.state, self.s.feed_path, delivery.ORIGIN, pause=lambda _: None)
        self.assertEqual(site.commits, 1)
        self.assertEqual(d.read_checkpoint(self.s.state, DAY), self.checkpoint)

    def test_process_failure_never_exposes_transport_token(self):
        site = StagedSite(self.s)
        error = subprocess.CalledProcessError(28, ["curl", "Authorization: Bearer fixture-token"])
        with patch.object(hot, "fetch", site.fetch), patch.object(hot, "post", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "TRANSPORT_PROCESS_FAILURE:28") as captured:
                delivery.deliver(self.s.state, self.s.feed_path, delivery.ORIGIN, pause=lambda _: None)
        self.assertNotIn("fixture-token", str(captured.exception))
        self.assertTrue(captured.exception.__suppress_context__)

    def test_authority_and_checkpoint_tampering_stop_before_network(self):
        (d.day_root(self.s.state, DAY) / "night-feed.json").write_bytes(b"tampered")
        with patch.object(hot, "post") as send:
            with self.assertRaisesRegex(RuntimeError, "HASH_MISMATCH"):
                delivery.deliver(self.s.state, self.s.feed_path, delivery.ORIGIN, pause=lambda _: None)
        send.assert_not_called()

    def test_site_wrong_authority_and_canonical_hash_fail_closed(self):
        site = StagedSite(self.s)
        site.feed["daily_runtime"]["authority_sha256"] = "0"*64
        with patch.object(hot, "fetch", site.fetch), patch.object(hot, "post") as send:
            with self.assertRaisesRegex(RuntimeError, "AUTHORITY_MISMATCH"):
                delivery.deliver(self.s.state, self.s.feed_path, delivery.ORIGIN, pause=lambda _: None)
        send.assert_not_called()

    def test_research_main_unavailable_restore_never_requests_checkout(self):
        shutil.rmtree(self.s.main)
        result = bootstrap.prepare(self.s.state, DAY, "night", self.root / "frozen-private")
        self.assertEqual(result["checkout_candidate"], "false")
        self.assertEqual(result["authority_sha256"], self.s.authority["authority_sha256"])
        self.assertTrue((self.root / "frozen-private/g11/relay/daily_runtime_authority_v1.py").is_file())

    def test_no_authority_cannot_fallback_to_main_at_night(self):
        with self.assertRaisesRegex(RuntimeError, "DAILY_AUTHORITY_UNKNOWN"):
            bootstrap.prepare(self.root / "unknown", DAY, "night", self.root / "fallback")
        self.assertFalse((self.root / "fallback").exists())

    def test_phase_binding_is_preserved_by_bounded_morning_projection(self):
        feed = json.loads(self.original)
        feed["stage"] = "MORNING"
        feed["daily_runtime"] = d.feed_binding(self.s.authority, "MORNING")
        path = self.work / "morning.json"
        path.write_bytes(hot.compact(feed))
        hot.project_morning_base(path)
        self.assertEqual(json.loads(path.read_bytes())["daily_runtime"], feed["daily_runtime"])

    def test_manifest_binds_full_canonical_checkpoint_sha(self):
        site = StagedSite(self.s)
        delta = hot.create_delta(json.loads(self.original), site.feed, site.sha)
        manifest, chunks = hot.night_chunks(delta, json.loads(self.original), self.checkpoint)
        self.assertEqual(manifest["CANONICAL_FEED_SHA256"], hot.hash_bytes(self.original))
        self.assertEqual(manifest["DAILY_AUTHORITY_SHA256"], self.s.authority["authority_sha256"])
        self.assertEqual(manifest["CHECKPOINT_SHA256"], self.checkpoint["checkpoint_sha256"])
        self.assertTrue(all(len(hot.compact(chunk)) <= hot.NIGHT_HARD_CHUNK_BYTES for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
