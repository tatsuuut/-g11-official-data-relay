"""Transport regression for the October 21 stored/display byte mismatch.

Use the real router, certificate checks, LIVE/NIGHT delta builders, curl response
parser and receipt writer. Only curl's remote side is substituted; Node still
serializes the LIVE target. This is not an integration test of the Site server.
The fixture deliberately makes the public projection differ from stored bytes.
"""
from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
from datetime import datetime
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import daily_predeadline_authority_recovery_v1 as rescue
import daily_runtime_delivery_v1 as delivery
import hot_delta_transport_v1 as hot


class WireSite:
    """A strict protocol fixture with separate source and representation hashes."""

    def __init__(self, feed):
        self.raw = rescue.canonical(feed) + b"\n"
        self.reads = []
        self.posts = []
        self.bad_source_hash = False
        self.bad_wire_hash = False
        self.stale_readback = False
        self.drop_ack = False
        self.run = subprocess.run

    def __call__(self, command, **kwargs):
        if command[0] != "curl":
            return self.run(command, **kwargs)
        urls = [s for s in command if s.startswith(delivery.ORIGIN + "/api/")]
        assert len(urls) == 1, "unexpected remote destination"
        url = urlsplit(urls[0])
        output = Path(command[command.index("--output") + 1])
        headers = [command[i + 1] for i, value in enumerate(command) if value == "-H"]
        if "POST" in command:
            assert url.path == "/api/g11-sync"
            assert "Authorization: Bearer fixture-only" in headers
            wire = Path(command[command.index("--data-binary") + 1][1:]).read_bytes()
            assert "x-g11-feed-sha256: " + hot.hash_bytes(wire) in headers
            delta = json.loads(wire)
            assert delta["SCHEMA"] in {"G11_RESCUE_LIVE_DELTA_V1", "G11_RESCUE_NIGHT_DELTA_V1"}
            assert delta["BASE_FEED_SHA"] == hot.hash_bytes(self.raw)
            target = json.loads(self.raw)
            target.update(delta["TOP_LEVEL"])
            rows = {row["key"]: row for row in target["races"]}
            for change in delta["RACES"]:
                rows[change["RACE_KEY"]].update(change["CHANGED_FIELDS"])
            result = rescue.canonical(target)
            if delta["SCHEMA"] == "G11_RESCUE_NIGHT_DELTA_V1":
                result += b"\n"
            assert hot.hash_bytes(result) == delta["TARGET_FEED_SHA"]
            self.posts.append(delta)
            if not self.stale_readback:
                self.raw = result
            if self.drop_ack:
                self.drop_ack = False
                raise subprocess.CalledProcessError(28, ["curl", "fixture-only"])
            output.write_text(json.dumps({"status": "PASS", "payload_sha256": hot.hash_bytes(result)}))
            return subprocess.CompletedProcess(command, 0, stdout="200", stderr="")

        assert url.path == "/api/g11-feed"
        query = parse_qs(url.query)
        feed = json.loads(self.raw)
        assert query.get("date") == [feed["operational_date_jst"]]
        stored = query.get("view") == ["stored-rescue"]
        self.reads.append("stored" if stored else "display")
        if stored:
            assert "Authorization: Bearer fixture-only" in headers
            body = self.raw
        else:
            # A valid display response has its own body hash and the original
            # source hash. It must never be used as the rescue mutation base.
            feed["display_projection"] = {"derived": True}
            body = hot.compact(feed)
        response_headers = {
            "x-g11-feed-sha256": "0" * 64 if self.bad_wire_hash else hot.hash_bytes(body),
            "x-g11-source-feed-sha256": "0" * 64 if self.bad_source_hash else hot.hash_bytes(self.raw),
        }
        if stored:
            response_headers["x-g11-transport-readback"] = "STORED_RESCUE"
        output.write_bytes(body)
        Path(command[command.index("--dump-header") + 1]).write_text(
            "HTTP/2 200\n" + "\n".join(k + ": " + v for k, v in response_headers.items()) + "\n")
        return subprocess.CompletedProcess(command, 0)


@contextmanager
def scenario(day="2026-10-21", phase="LIVE"):
    with tempfile.TemporaryDirectory() as directory:
        before_cwd = Path.cwd()
        root = Path(directory)
        (root / ".relay-output").mkdir()
        state = root / "state"
        now = datetime.fromisoformat(day + ("T10:00:00+09:00" if phase == "LIVE" else "T23:00:00+09:00"))
        source = "e0e192dd8e34df628439ccdd332748848aaf7725"
        parent = {"schema": "G11_DAILY_RUNTIME_AUTHORITY_V1", "operational_date_jst": day,
                  "immutable": True, "runtime_source_sha": source,
                  "source_files": {"runtime.py": "b" * 64}, "model_files": {"model": "c" * 64}}
        for key in ("p3", "abeken_v53", "wild", "true_ai"):
            parent[key] = {"source_sha": source, "version": "fixture"}
        parent["authority_sha256"] = rescue.sha(rescue.canonical(parent))
        cert = {**deepcopy(parent), "schema": rescue.SCHEMA, "source_delta": [],
                "parent_authority_sha256": parent["authority_sha256"], "safety_margin_seconds": 300,
                "canonical_morning_promoted": False, "research_eligible": False,
                "same_day_results_used": False, "model_set_sha256": rescue.sha(rescue.canonical(parent["model_files"]))}
        cert.pop("authority_sha256")
        cert["authority_sha256"] = rescue.sha(rescue.canonical(cert))
        for relative, value in [("rescue/boundary.json", cert), ("daily-runtime/" + day + "/authority.json", parent),
                                ("relay-state.json", {"OPERATIONAL_DATE_JST": day, "MORNING_COMPLETE": False}),
                                ("rescue/predeadline-audit.json", {}), ("rescue/night-audit.json", {})]:
            path = state / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(rescue.canonical(value))
        feed = {"operational_date_jst": day, "stage": "MORNING", "status": "PASS", "source": {},
                "SNAPSHOT_CLASS": "SAME_DAY_RESCUE_BOUNDARY", "same_day_rescue": {"authority_sha256": cert["authority_sha256"]},
                "generated_at_jst": day + "T08:00:00+09:00", "counts": {"formal": 0, "results": 0, "pending": 0}, "races": []}
        for i in range(156 if day == "2026-10-21" else 2):
            key = day.replace("-", "") + f"-{i // 12 + 1:02d}-{i % 12 + 1:02d}"
            feed["races"].append({"key": key, "deadline_jst": day + "T12:00:00+09:00",
                "same_day_rescue_lock": {"p3_snapshot_sha256": "b" * 64}, "research_eligible": False,
                "formal_status": "EXCLUDED", "practical_bets": ["1-2-3"], "predeadline": None,
                "best_ev": None, "best_ev_bet": None, "value_bets": None,
                "odds_captured_at_jst": None, "odds_merit": None,
                "result_trifecta": None, "payout": None, "result_meta": {},
                "abeken_shadow": {"v53": {"morning": {"snapshot_sha256": "c" * 64}, "live": None}}})
        feed["counts"].update(races=len(feed["races"]), pending=len(feed["races"]))
        incoming = deepcopy(feed)
        incoming["generated_at_jst"] = now.isoformat()
        if phase == "LIVE":
            row = incoming["races"][0]
            row["predeadline"] = {"status": "READY", "snapshot_sha256": "d" * 64,
                "captured_at_jst": now.isoformat(), "best_ev": None, "best_ev_bet": None, "value_bets": None,
                "odds_captured_at_jst": now.isoformat(), "legacy_odds_merit": "－"}
            row["pre_race_v2"] = {"final": {"locked_at_jst": now.isoformat()}, "early": None}
            row["p3_morning_snapshot_sha256"] = "b" * 64
            row["abeken_shadow"]["v53"]["live"] = {"snapshot_sha256": "e" * 64,
                "captured_at_jst": now.isoformat(), "locked_at_jst": now.isoformat()}
        else:
            incoming["stage"] = "NIGHT"
            incoming["counts"].update(results=len(incoming["races"]), pending=0)
            for row in incoming["races"]:
                row.update(result_trifecta="1-2-3", payout=1200, result_meta={"official": True})
            (state / "rescue/night-feed.json").write_bytes(rescue.canonical(incoming) + b"\n")
        payload = root / "feed.json"
        payload.write_bytes(rescue.canonical(incoming) + b"\n")
        wire_site = WireSite(feed)
        saved = {p: p.read_bytes() for p in state.rglob("*") if p.is_file()}
        os.chdir(root)
        try:
            with patch.dict(os.environ, {"G11_SYNC_OIDC": "fixture-only", "GITHUB_OUTPUT": str(root / "outputs")}), \
                 patch.object(rescue, "clock", return_value=now), patch.object(subprocess, "run", side_effect=wire_site), \
                 redirect_stdout(io.StringIO()):
                yield state, payload, wire_site, saved
        finally:
            os.chdir(before_cwd)


class RescueTransportContractTests(unittest.TestCase):
    def test_verified_rescue_readback_drives_only_optional_saved_input_reader(self):
        import selective_alert_input_v1 as inputs
        with scenario() as (state, payload, site, saved), patch.object(inputs,'deliver_optional') as append:
            receipt=delivery.deliver(state,payload,delivery.ORIGIN)
            self.assertEqual(receipt['site_get_readback'],'PASS')
            append.assert_called_once_with(state/'rescue/store',json.loads(site.raw),origin=delivery.ORIGIN)
            self.assert_preserved(saved)
        with scenario() as (state, payload, site, saved), patch.object(inputs,'deliver_optional',side_effect=RuntimeError('fixture')):
            self.assertEqual(delivery.deliver(state,payload,delivery.ORIGIN)['site_get_readback'],'PASS')
            self.assert_preserved(saved)

    def assert_preserved(self, saved):
        for path, raw in saved.items():
            self.assertEqual(path.read_bytes(), raw, path.name)

    def test_live_uses_exact_source_when_public_projection_differs(self):
        for day in ("2026-10-21", "2026-10-22"):
            with self.subTest(day=day), scenario(day) as (state, payload, site, saved):
                _, headers, raw = hot.fetch(delivery.ORIGIN + "/api/g11-feed?date=" + day)
                self.assertNotEqual(hot.hash_bytes(raw), headers["x-g11-source-feed-sha256"])
                receipt = delivery.deliver(state, payload, delivery.ORIGIN)
                self.assertEqual(receipt["site_get_readback"], "PASS")
                self.assertEqual(receipt["verified_p3_live_count"], 1)
                self.assertEqual(receipt["verified_v4_live_count"], 1)
                self.assertEqual(site.reads, ["display", "stored", "stored"])
                self.assertEqual(len(site.posts), 1)
                self.assertEqual(receipt["site_payload_sha256"], hot.hash_bytes(site.raw))
                self.assertNotIn("display_projection", json.loads(site.raw))
                self.assert_preserved(saved)

    def test_night_uses_exact_source_and_preserves_saved_prediction(self):
        for day in ("2026-10-21", "2026-10-22"):
            with self.subTest(day=day), scenario(day, "NIGHT") as (state, payload, site, saved):
                receipt = delivery.deliver(state, payload, delivery.ORIGIN)
                self.assertEqual(receipt["site_get_readback"], "PASS")
                self.assertEqual(site.raw, payload.read_bytes())
                self.assertEqual(site.reads, ["stored", "stored"])
                self.assertEqual(receipt["results"], 156 if day == "2026-10-21" else 2)
                self.assertEqual(receipt["pending"], 0)
                self.assert_preserved(saved)

    def test_invalid_source_or_wire_hash_rejects_before_post(self):
        for phase in ("LIVE", "NIGHT"):
            for defect in ("bad_source_hash", "bad_wire_hash"):
                with self.subTest(phase=phase, defect=defect), scenario(phase=phase) as (state, payload, site, saved):
                    setattr(site, defect, True)
                    with self.assertRaisesRegex(RuntimeError, "STORED_RESCUE_READBACK|HOT_READBACK_SHA"):
                        delivery.deliver(state, payload, delivery.ORIGIN)
                    self.assertEqual(site.posts, [])
                    self.assertFalse(list((state / "rescue").glob("*-delivery.json")))
                    self.assert_preserved(saved)

    def test_http_success_with_stale_readback_never_records_success(self):
        for phase in ("LIVE", "NIGHT"):
            with self.subTest(phase=phase), scenario(phase=phase) as (state, payload, site, saved):
                site.stale_readback = True
                with self.assertRaisesRegex(RuntimeError, "SITE_SHA_READBACK|NIGHT_SITE_READBACK"):
                    delivery.deliver(state, payload, delivery.ORIGIN)
                self.assertEqual(len(site.posts), 1)
                self.assertFalse(list((state / "rescue").glob("*-delivery.json")))
                self.assertFalse(Path(".relay-output/hot-delta-accepted").exists())
                self.assert_preserved(saved)

    def test_retry_after_lost_ack_uses_committed_source_without_changing_locks(self):
        for phase in ("LIVE", "NIGHT"):
            with self.subTest(phase=phase), scenario(phase=phase) as (state, payload, site, saved):
                site.drop_ack = True
                with self.assertRaises(subprocess.CalledProcessError):
                    delivery.deliver(state, payload, delivery.ORIGIN)
                committed = site.raw
                self.assertFalse(list((state / "rescue").glob("*-delivery.json")))
                receipt = delivery.deliver(state, payload, delivery.ORIGIN)
                self.assertEqual(receipt["site_get_readback"], "PASS")
                self.assertEqual(site.raw, committed)
                self.assertEqual(len(site.posts), 2 if phase == "LIVE" else 1)
                self.assert_preserved(saved)


if __name__ == "__main__":
    unittest.main()

