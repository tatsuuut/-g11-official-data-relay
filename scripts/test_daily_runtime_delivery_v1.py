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


# Rescue fixtures are synthetic. They do not read production state or execute models.
from datetime import datetime as _rescue_datetime
import hashlib as _rescue_hashlib

RESCUE_DAY = '2026-10-06'
RESCUE_KEY = '20261006-01-01'  # Synthetic fixture, not a saved prediction.
RESCUE_ORIGIN = 'https://example.invalid'

def _rescue_compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()

def _rescue_digest(value):
    return _rescue_hashlib.sha256(value).hexdigest()

class RescueFixedClock(_rescue_datetime):
    @classmethod
    def now(cls, tz=None):
        return _rescue_datetime.fromisoformat('2026-10-06T11:55:00+09:00').astimezone(tz)

class RescueTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        oldcwd = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, oldcwd)
        self.state = self.root/'state'
        (self.state/'rescue').mkdir(parents=True)
        Path('.relay-output').mkdir()
        self.feed_path = self.root/'incoming.json'
        cert = {'schema':'G11_SAME_DAY_RESCUE_BOUNDARY_V1', 'operational_date_jst':RESCUE_DAY,
                'canonical_morning_promoted':False, 'research_eligible':False}
        cert['authority_sha256'] = _rescue_digest(json.dumps(cert, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode())
        (self.state/'rescue/boundary.json').write_bytes(_rescue_compact(cert))
        self.p3 = {'snapshot_sha256':'1'*64, 'captured_at_jst':'2026-10-06T11:54:00+09:00',
                   'odds_captured_at_jst':'2026-10-06T11:54:01+09:00',
                   'practical_bets':['1-2-3'], 'production_points':1,
                   'best_ev':None, 'best_ev_bet':None, 'value_bets':[], 'legacy_odds_merit':'TEST'}
        self.morning = {'snapshot_sha256':'2'*64, 'practical_bets':['1-3-2'],
                        'locked_at_jst':'2026-10-06T09:00:00+09:00'}
        self.live = {'snapshot_sha256':'3'*64, 'practical_bets':['1-2-3'],
                     'locked_at_jst':'2026-10-06T11:54:03+09:00', 'buy_count':1}
        row = {'key':RESCUE_KEY, 'deadline_jst':'2026-10-06T12:00:00+09:00',
               'research_eligible':False, 'formal_status':'EXCLUDED',
               'same_day_rescue_lock':{'p3_snapshot_sha256':'4'*64},
               'p3_snapshot_sha256':'4'*64, 'practical_bets':['1-3-2'], 'production_points':1,
               'abeken_shadow':{'v53':{'morning':copy.deepcopy(self.morning), 'live':None}}}
        self.accepted = {'operational_date_jst':RESCUE_DAY, 'SNAPSHOT_CLASS':'SAME_DAY_RESCUE_BOUNDARY',
                         'same_day_rescue':{'authority_sha256':cert['authority_sha256']},
                         'races':[row], 'source':{}, 'generated_at_jst':'2026-10-06T09:00:00+09:00'}
        self.incoming = {'operational_date_jst':RESCUE_DAY, 'SNAPSHOT_CLASS':'TODAY_ONLY_RESCUE_PREDEADLINE',
                         'CANONICAL_MORNING_PROMOTED':False, 'RESEARCH_ELIGIBLE_AS_MORNING':False,
                         'source':{}, 'generated_at_jst':'2026-10-06T11:54:05+09:00',
                         'races':[copy.deepcopy(row)]}
        self.newrow = self.incoming['races'][0]
        self.newrow['today_rescue'] = {'status':'LOCKED','p3_snapshot_sha256':'1'*64}
        self.newrow['predeadline'] = copy.deepcopy(self.p3)
        self.newrow['abeken_shadow']['v53']['live'] = copy.deepcopy(self.live)
        self.site = copy.deepcopy(self.accepted)
        self.posted = []
        self.readback_corrupt = None
        self.clock_patch = patch.object(delivery, 'datetime', RescueFixedClock)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.fetch_patch = patch.object(delivery.hot, 'fetch', side_effect=self.fetch)
        self.fetch_patch.start()
        self.addCleanup(self.fetch_patch.stop)
        self.run_patch = patch.object(subprocess,'run',side_effect=self.send)
        self.run_patch.start()
        self.addCleanup(self.run_patch.stop)
        self.env = patch.dict(os.environ,{'G11_SYNC_OIDC':'synthetic-token-only','GITHUB_OUTPUT':''})
        self.env.start()
        self.addCleanup(self.env.stop)

    def fetch(self, url):
        value = copy.deepcopy(self.site)
        if self.posted and self.readback_corrupt:
            self.readback_corrupt(value)
        raw = _rescue_compact(value)
        source_sha = _rescue_digest(_rescue_compact(self.posted[-1])) if self.posted else _rescue_digest(_rescue_compact(self.site))
        return value, {'x-g11-source-feed-sha256':source_sha, 'x-g11-feed-sha256':_rescue_digest(raw)}, raw

    def send(self, args, **kwargs):
        path = Path(args[args.index('--data-binary')+1][1:])
        data = path.read_bytes()
        self.site = json.loads(data)
        self.posted.append(copy.deepcopy(self.site))
        Path(args[args.index('--output')+1]).write_bytes(_rescue_compact({'status':'PASS','payload_sha256':_rescue_digest(data)}))
        return subprocess.CompletedProcess(args,0,'200','')

    def invoke(self):
        self.feed_path.write_bytes(_rescue_compact(self.incoming))
        return delivery.deliver_rescue_predeadline(self.state,self.feed_path,RESCUE_ORIGIN)

    def prior_p3(self):
        self.site['races'][0]['predeadline'] = copy.deepcopy(self.p3)
        self.accepted = copy.deepcopy(self.site)

    def test_p3_and_v4_can_arrive_together(self):
        self.invoke()
        self.assertEqual(self.site['races'][0]['predeadline'],self.p3)
        self.assertEqual(self.site['races'][0]['abeken_shadow']['v53']['live'],self.live)

    def test_v4_can_arrive_after_already_published_p3(self):
        self.prior_p3()
        self.invoke()
        self.assertEqual(self.site['races'][0]['abeken_shadow']['v53']['live'],self.live)
        self.assertEqual(self.site['races'][0]['predeadline'],self.p3)

    def test_existing_v4_must_not_change(self):
        self.prior_p3()
        self.site['races'][0]['abeken_shadow']['v53']['live'] = copy.deepcopy(self.live)
        self.newrow['abeken_shadow']['v53']['live']['practical_bets'] = ['1-3-2']
        with self.assertRaisesRegex(RuntimeError,'RESCUE_V4_LIVE_LOCK_CHANGED'):
            self.invoke()
        self.assertEqual(self.posted,[])

    def test_missing_v4_on_readback_is_not_pass(self):
        def remove(value):
            value['races'][0]['abeken_shadow']['v53']['live'] = None
        self.readback_corrupt = remove
        with self.assertRaisesRegex(RuntimeError,'RESCUE_V4_READBACK_LOCK'):
            self.invoke()
        self.assertFalse((self.state/'rescue/predeadline-delivery.json').exists())

    def test_new_v4_after_deadline_rejected(self):
        self.prior_p3()
        self.site['races'][0]['deadline_jst'] = '2026-10-06T11:54:59+09:00'
        self.newrow['deadline_jst'] = self.site['races'][0]['deadline_jst']
        with self.assertRaisesRegex(RuntimeError,'RESCUE_LIVE_PROOF_INVALID'):
            self.invoke()
        self.assertEqual(self.posted,[])

    def test_new_v4_requires_same_morning(self):
        self.prior_p3()
        self.newrow['abeken_shadow']['v53']['morning']['snapshot_sha256'] = '9'*64
        with self.assertRaisesRegex(RuntimeError,'RESCUE_V4_MORNING_CHANGED'):
            self.invoke()
        self.assertEqual(self.posted,[])

    def test_missing_p3_proof_cannot_publish_late_v4(self):
        self.prior_p3()
        self.newrow['today_rescue']['p3_snapshot_sha256'] = '9'*64
        with self.assertRaisesRegex(RuntimeError,'RESCUE_LIVE_PROOF_INVALID'):
            self.invoke()
        self.assertEqual(self.posted,[])

    def test_existing_p3_must_not_change(self):
        self.prior_p3()
        self.newrow['predeadline']['practical_bets'] = ['1-3-2']
        with self.assertRaisesRegex(RuntimeError,'RESCUE_LIVE_LOCK_CHANGED'):
            self.invoke()
        self.assertEqual(self.posted,[])

    def test_morning_survives_append(self):
        self.invoke()
        result = self.site['races'][0]
        original = self.accepted['races'][0]
        for field in ('practical_bets','p3_snapshot_sha256','same_day_rescue_lock','formal_status','research_eligible','production_points'):
            self.assertEqual(result[field],original[field])
        self.assertEqual(result['abeken_shadow']['v53']['morning'], self.morning)
        self.assertFalse((self.state/'daily-runtime').exists())

    def test_morning_corruption_on_readback_is_not_pass(self):
        self.readback_corrupt = lambda v: v['races'][0].update(p3_snapshot_sha256='9'*64)
        with self.assertRaisesRegex(RuntimeError,'RESCUE_MORNING_READBACK_LOCK'):
            self.invoke()

    def test_retained_live_and_no_new_are_explicit(self):
        self.prior_p3()
        self.site['races'][0]['abeken_shadow']['v53']['live'] = copy.deepcopy(self.live)
        self.site['races'][0]['deadline_jst'] = '2026-10-06T11:54:59+09:00'
        self.newrow['deadline_jst'] = self.site['races'][0]['deadline_jst']
        result = self.invoke()
        self.assertEqual(result['delivery_state'],'NO_NEW_LOCKS')
        self.assertEqual(result['new_live_locks'],[])
        self.assertEqual(result['new_v4_live_locks'],[])
        self.assertEqual(result['verified_p3_live_count'],1)
        self.assertEqual(result['verified_v4_live_count'],1)


if __name__ == "__main__":
    unittest.main()
