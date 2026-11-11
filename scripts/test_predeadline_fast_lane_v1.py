"""Future activation, immutable state, failed delivery and cycle routing."""
import json
import os
from datetime import datetime
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import Mock, patch

import predeadline_fast_lane_v1 as fast
import critical_saved_publication_v1 as publication

DAY = '2026-11-08'
NOW = datetime.fromisoformat(DAY + 'T12:00:00+09:00')
ROOT = Path(__file__).resolve().parents[1]


class FastCycleTests(TestCase):
    def test_only_current_normal_frozen_live_from_nov8(self):
        self.assertTrue(fast.enabled(DAY, 'predeadline', 'true', '', NOW))
        for day, phase, frozen, rescue, now in [
            ('2026-11-07', 'predeadline', 'true', '', NOW),
            ('2026-11-09', 'predeadline', 'true', '', NOW),
            (DAY, 'morning', 'true', '', NOW), (DAY, 'night', 'true', '', NOW),
            (DAY, 'dry-run', 'true', '', NOW), (DAY, 'predeadline', 'false', '', NOW),
            (DAY, 'predeadline', 'true', 'true', NOW)]:
            self.assertFalse(fast.enabled(day, phase, frozen, rescue, now))

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.metadata = {'OPERATIONAL_DATE_JST': DAY, 'MORNING_COMPLETE': True,
            'CRITICAL': {'OPERATIONAL_DATE': DAY, 'CRITICAL_CHECKPOINT_STATUS': 'PASS',
                'LAST_CRITICAL_RUN_ID': '42', 'AUTHORITY_SHA256': 'a'*64,
                'RUNTIME_SOURCE_SHA': 'b'*40, 'LAST_TIMELY_P3_LIVE_COUNT': 0,
                'LAST_RACE_DEADLINE_JST': DAY+'T20:30:00+09:00',
                'LAST_TIMELY_V53_COUNT': 0}}
        (self.root/'relay-state.json').write_text(json.dumps(self.metadata))
        self.auth = SimpleNamespace(verify_runtime=Mock(return_value={
            'authority_sha256': 'a'*64, 'runtime_source_sha': 'b'*40}),
            verify_locks=Mock(), seal=Mock(), feed_binding=Mock(return_value={}))
        self.wild = SimpleNamespace(run_predeadline=Mock(return_value=True))
        self.publisher = SimpleNamespace(publish_future=Mock(return_value={'STATUS': 'PASS'}),
            load_locked_publication=Mock(return_value={'READ_ONLY': True}))
        self.feed = {'stage': 'PREDEADLINE', 'status': 'PASS', 'operational_date_jst': DAY,
            'races': [{'key': 'saved', 'predeadline': {'practical_bets': ['1-2-3']}}]}
        self.relay = SimpleNamespace(_morning_projection=Mock(return_value={}),
            build_feed=Mock(return_value=self.feed))

    def sidecars(self):
        with patch.dict(os.environ, {'GITHUB_RUN_ID': '42'}):
            return fast.saved_sidecars(self.root, DAY, self.root/'source', authority=self.auth,
                wild=self.wild, publisher=self.publisher, relay=self.relay, now=NOW)

    def test_preserves_prospective_research_and_seals_immutable_locks(self):
        self.assertEqual('PASS', self.sidecars()['p3_status'])
        self.assertEqual({'include_live': True, 'include_research': True, 'strict_deadline': True},
                         self.publisher.publish_future.call_args.kwargs)
        self.auth.seal.assert_called_once_with(self.root, DAY, 'PREDEADLINE')
        self.assertEqual(4, self.auth.verify_locks.call_count)

    def test_one_research_failure_does_not_drop_other_or_skip_integrity(self):
        self.wild.run_predeadline.side_effect = ValueError('bad input')
        with self.assertRaisesRegex(RuntimeError, 'PARTIAL'):
            self.sidecars()
        self.publisher.publish_future.assert_called_once()
        self.auth.seal.assert_called_once()

    def test_wrong_critical_run_or_changed_lock_stops_before_research(self):
        self.metadata['CRITICAL']['LAST_CRITICAL_RUN_ID'] = '41'
        (self.root/'relay-state.json').write_text(json.dumps(self.metadata))
        with self.assertRaisesRegex(RuntimeError, 'CRITICAL_BINDING'): self.sidecars()
        self.wild.run_predeadline.assert_not_called()
        self.metadata['CRITICAL']['LAST_CRITICAL_RUN_ID'] = '42'
        (self.root/'relay-state.json').write_text(json.dumps(self.metadata))
        self.auth.verify_locks.side_effect = RuntimeError('LOCK_CHANGED')
        with self.assertRaisesRegex(RuntimeError, 'LOCK_CHANGED'): self.sidecars()
        self.wild.run_predeadline.assert_not_called()

    def test_no_new_lock_still_retries_saved_delivery_without_reprediction(self):
        out = self.root/'feed.json'
        for _ in range(2):
            self.assertTrue(publication.project_saved(self.root, DAY, '42', self.root/'source', out,
                authority_module=self.auth, relay=self.relay, publisher=self.publisher, now=NOW))
            self.assertEqual(self.feed['races'], json.loads(out.read_text())['races'])
        self.publisher.publish_future.assert_not_called()
        self.assertEqual(8, self.auth.verify_locks.call_count)

    def test_workflow_releases_slot_without_duplicate_runtime_or_final_archive(self):
        text = (ROOT/'.github/workflows/g11-free-runner.yml').read_text()
        def step(name): return text.split('      - name: '+name, 1)[1].split('      - name:', 1)[0]
        self.assertIn("if: steps.fast-lane.outputs.enabled!='true'", step('Run the private fail-closed relay'))
        self.assertIn("steps.fast-lane.outputs.enabled=='true'", step('Gate NIGHT publication on fully settled research'))
        self.assertIn("steps.publication-state-verified.outputs.ready=='true'", step('Handoff additional timely odds after verified state save'))
        self.assertIn("steps.critical-publish.outcome=='failure'", step('Retry saved publication after an early delivery failure'))
        self.assertIn("steps.runtime.outcome=='success'", step('Publish a newly verified feed through OIDC'))
        self.assertIn("steps.publish.outcome == 'success'", step('Persist delivery receipts'))
        self.assertIn('cancel-in-progress: false', text)
        # Only one source of official acquisition remains in the fast path.
        source = (ROOT/'scripts/predeadline_fast_lane_v1.py').read_text()
        self.assertNotIn('run_predeadline_cycle(', source)
        self.assertNotIn('run_due_predeadline(', source)
        self.assertNotIn('prepare_prediction_models(', source)

    def test_idle_morning_and_night_handoff_do_not_invent_live_predictions(self):
        self.feed['stage'] = 'MORNING'
        self.assertFalse(publication.project_saved(self.root, DAY, '42', self.root/'source',
            self.root/'feed.json', authority_module=self.auth, relay=self.relay,
            publisher=self.publisher, now=NOW))
        self.metadata['CRITICAL']['LAST_RACE_DEADLINE_JST'] = NOW.isoformat()
        (self.root/'relay-state.json').write_text(json.dumps(self.metadata))
        self.assertEqual('NIGHT_HANDOFF', self.sidecars()['p3_status'])
        self.publisher.publish_future.assert_not_called()
        self.wild.run_predeadline.assert_not_called()

    def test_saved_live_research_is_included_in_one_complete_checkpoint_with_failure_fallback(self):
        text = (ROOT/'.github/workflows/g11-free-runner.yml').read_text()
        def step(name): return text.split('      - name: '+name+'\n',1)[1].split('      - name:',1)[0]
        ordered = ['Publish persisted critical LOCKs',
            'Capture saved LIVE research before the complete short-cycle checkpoint',
            'Encrypt changed state before persistence','Persist complete state before publication',
            'Publish coalesced LIVE research from the verified complete checkpoint']
        self.assertEqual(sorted(text.index('      - name: '+n+'\n') for n in ordered),
            [text.index('      - name: '+n+'\n') for n in ordered])
        capture = step(ordered[1])
        for condition in ("steps.fast-lane.outputs.enabled=='true'", "steps.critical-publish.outcome=='success'",
                          "steps.fast-sidecars.outcome=='success'"):
            self.assertIn(condition,capture)
        self.assertIn('saved_deme_recovery_v1.py',capture)
        self.assertIn('G11_PRIVATE_REPO_TOKEN: ""',capture)
        self.assertIn("steps.fast-deme-live.outcome!='success'",step('P3 research update after LIVE publication'))
        delivery = step(ordered[-1])
        self.assertIn("steps.fast-deme-live.outcome=='success'",delivery)
        self.assertIn("steps.publication-state-verified.outputs.ready=='true'",delivery)
        self.assertIn('RESEARCH_CHECKPOINT_VERIFIED: ${{steps.publication-state-verified.outputs.ready}}',delivery)
        # Existing receipt persistence and a verified fallback checkpoint remain
        # authoritative whenever the pre-checkpoint saved-only attempt fails.
        self.assertIn("steps.deme-live.outcome == 'success'",step('Persist delivery receipts'))
        self.assertIn("steps.delivery-checkpoint.outputs.ready=='true'",step('Publish persisted LIVE research after predictions'))


if __name__ == '__main__': main()

