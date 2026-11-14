"""Future activation, immutable state, failed delivery and cycle routing."""
import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace, ModuleType
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


class ResearchBudgetTests(TestCase):
    def task(self, source='odds', due=NOW, race='formal'):
        return {'SOURCE_ID': source, 'RACE_KEY': race, 'DUE_AT_JST': due.isoformat(),
                'MUST_FINISH_BEFORE_JST': (due + timedelta(minutes=7)).isoformat()}

    def test_overdue_capture_and_five_minute_boundary_defer_tail(self):
        for seconds in (-360, 0, 299, 300):
            with self.subTest(seconds=seconds):
                result = fast.research_budget([self.task(due=NOW+timedelta(seconds=seconds))], set(), NOW)
                self.assertTrue(result['deferred'])
                self.assertEqual('CAPTURE_DUE_SOON', result['reason'])
        self.assertFalse(fast.research_budget([self.task(due=NOW+timedelta(seconds=301))], set(), NOW)['deferred'])

    def test_expired_or_saved_inputs_do_not_starve_idle_research(self):
        tasks = [self.task('expired', NOW-timedelta(minutes=7)), self.task('saved')]
        self.assertFalse(fast.research_budget(tasks, {'saved'}, NOW)['deferred'])
        self.assertFalse(fast.research_budget([], set(), NOW)['deferred'])

    def test_incident_previous_cycle_yields_before_toda_and_biwako_capture(self):
        now = datetime.fromisoformat('2026-11-14T12:54:18+09:00')
        due = now.replace(minute=53, second=0)
        tasks = [self.task('toda-input', due), self.task('biwako-input', due)]
        self.assertTrue(fast.research_budget(tasks, set(), now)['deferred'])

    def test_reads_verified_formal_scope_without_mutating_saved_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshots = root/'store/snapshots/predeadline'
            snapshots.mkdir(parents=True)
            (snapshots/'saved.json').write_text('synthetic immutable evidence')
            morning = {'SNAPSHOT': {'PAYLOAD': {'PREDEADLINE_PLAN': [
                self.task('saved'), self.task('excluded', race='excluded'),
                self.task('later', NOW+timedelta(minutes=6))]}}}
            artifact = Mock(return_value=SimpleNamespace(load_verified_snapshot=Mock(return_value=morning)))
            runner = SimpleNamespace(_formal_prediction_scope=Mock(return_value=(None, ['formal'], None)))
            result = fast.saved_research_budget(root, DAY, now=NOW, artifact_class=artifact, runner=runner)
            self.assertFalse(result['deferred'])
            self.assertEqual(1, result['pending_sources'])
            artifact.return_value.load_verified_snapshot.assert_called_once_with('morning', 'official-day-input')
            self.assertEqual('synthetic immutable evidence', (snapshots/'saved.json').read_text())

    def test_invalid_or_unavailable_plan_defers_optional_work_only(self):
        for change in ({'DUE_AT_JST': 'bad'}, {'SOURCE_ID': ''},
                       {'MUST_FINISH_BEFORE_JST': NOW.isoformat()}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                fast.research_budget([{**self.task(), **change}], set(), NOW)
        result = fast.saved_research_budget(Path('/unused'), DAY, now=NOW,
            artifact_class=Mock(side_effect=ValueError('unverified snapshot')))
        self.assertEqual('BUDGET_UNVERIFIED', result['reason'])
        self.assertTrue(result['deferred'])

    def test_budget_uses_the_same_reviewed_admission_as_critical_capture(self):
        active = []
        @contextmanager
        def compatibility():
            active.append(True)
            try:
                yield
            finally:
                active.pop()
        def scope(morning, **kwargs):
            if not active:
                raise RuntimeError('MORNING_REQUIRES_REVIEWED_ADMISSION')
            return None, ['formal'], None
        morning = {'SNAPSHOT': {'PAYLOAD': {'PREDEADLINE_PLAN': []}}}
        official = ModuleType('g11.canonical.official_pipeline')
        official.OfficialArtifactStore = Mock(return_value=SimpleNamespace(
            load_verified_snapshot=Mock(return_value=morning)))
        official.CanonicalRunner = SimpleNamespace(_formal_prediction_scope=scope)
        with patch.dict(sys.modules, {'g11.canonical.official_pipeline': official}), \
             patch('rescue_night_result_parser_v1.official_input_compatibility', compatibility):
            result = fast.saved_research_budget(Path('/unused'), DAY, now=NOW)
        self.assertEqual('CLEAR_WINDOW', result['reason'])
        self.assertFalse(result['deferred'])
        self.assertEqual([], active)

    def test_workflow_defers_optional_tail_but_keeps_verified_receipt_checkpoint(self):
        text = (ROOT/'.github/workflows/g11-free-runner.yml').read_text()
        def step(name): return text.split('      - name: '+name+'\n', 1)[1].split('      - name:', 1)[0]
        guard = step('Reserve the next capture window before optional research')
        self.assertIn("steps.fast-lane.outputs.enabled=='true'", guard)
        self.assertIn("steps.critical-publish.outcome=='success'", guard)
        self.assertLess(text.index('      - name: Reserve the next capture'), text.index('      - name: Install pinned'))
        for name in ('Install pinned private runtime dependencies',
                     'Retain saved-input research without another official capture', 'P3 research source'):
            self.assertIn("steps.research-budget.outputs.deferred!='true'", step(name))
        # The second encrypted checkpoint persists the successful delivery
        # receipt even when no optional research ran. No durability shortcut.
        self.assertNotIn('research-budget', step('Encrypt changed state before persistence'))
        self.assertIn("steps.research-budget.outputs.deferred=='true'&&steps.critical-publish.outcome=='success'",
                      step('Gate NIGHT publication on fully settled research'))
        self.assertIn("steps.encrypt-state.outputs.ready=='true'&&steps.night-ready.outputs.allowed=='true'",
                      step('Persist complete state before publication'))
        self.assertIn('test "${RESEARCH_DEFERRED}" = true', step('Report short LIVE delivery failures after continuation'))


if __name__ == '__main__': main()

