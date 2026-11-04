"""Run the actual pinned date gate; only the authenticated Nov 3 NIGHT may recover."""
from datetime import datetime as RealDatetime
import os
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest.mock import patch

WORKFLOW = Path(os.environ.get('WILD4_WORKFLOW_PATH', Path(__file__).with_name('g11-wild4-shadow-lock-only.yml')))
CHECKPOINT = 'g11-state-v4-published-2026-11-03-41648721330-1-checkpoint'

class DateGateTests(unittest.TestCase):
    def run_gate(self, day='2026-11-03', now='2026-11-04T23:50:00+09:00', mode='wild4-settle', checkpoint=CHECKPOINT, **extra):
        source = WORKFLOW.read_text()
        section = source.split('      - name: Bind the current operational date\n', 1)[1].split('      - name:', 1)[0]
        code = textwrap.dedent(section.split("          python3 - <<'PY'\n", 1)[1].split('          PY\n', 1)[0])
        fixed = RealDatetime.fromisoformat(now)
        class Clock(RealDatetime):
            @classmethod
            def now(cls, tz=None):
                return fixed.astimezone(tz) if tz else fixed
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'output'
            env = {'UPSTREAM_RUN_TITLE':'', 'REQUESTED_DAY':day, 'REQUESTED_MODE':mode,
                'LINEAGE_READBACK_DATE':'', 'NIGHT_CHECKPOINT_KEY':checkpoint, 'PREDEADLINE_CHECKPOINT_KEY':'',
                'GITHUB_EVENT_NAME':'workflow_dispatch', 'GITHUB_OUTPUT':str(output), **extra}
            with patch.dict(os.environ, env), patch('datetime.datetime', Clock):
                exec(compile(code, str(WORKFLOW), 'exec'), {})
            return dict(line.split('=',1) for line in output.read_text().splitlines())

    def test_exact_verified_night_is_settlement_only_for_two_recovery_dates(self):
        for now in ['2026-11-04T23:50:00+09:00','2026-11-05T03:00:00+09:00']:
            result = self.run_gate(now=now)
            self.assertEqual(result['day'],'2026-11-03')
            self.assertEqual(result['night_rollover'],'true')
            self.assertEqual(result['artifact_name'],'g11-wild4-20261103-future-oos-locks-encrypted')

    def test_other_dates_checkpoints_modes_and_live_input_are_rejected(self):
        for changes in ({'day':'2026-11-02'}, {'checkpoint':''}, {'checkpoint':CHECKPOINT.replace('41648721330','41647723048')},
                        {'mode':'wild4'}, {'PREDEADLINE_CHECKPOINT_KEY':'g11-state-v4-critical-2026-11-03-1'},
                        {'now':'2026-11-06T01:00:00+09:00'}, {'GITHUB_EVENT_NAME':'workflow_run'}):
            with self.subTest(changes=changes), self.assertRaises(SystemExit):
                self.run_gate(**changes)

    def test_normal_current_day_and_existing_rollover_still_work(self):
        current = self.run_gate(day='2026-11-04',checkpoint='g11-state-v4-published-2026-11-04-41842302772-1-checkpoint')
        self.assertEqual(current['night_rollover'],'false')
        early = self.run_gate(now='2026-11-04T01:59:59+09:00',checkpoint='')
        self.assertEqual(early['night_rollover'],'true')
        with self.assertRaises(SystemExit):
            self.run_gate(now='2026-11-04T02:00:00+09:00',checkpoint='')

    def test_historical_recovery_cannot_create_predictions(self):
        source = WORKFLOW.read_text()
        for name in ('Lock future races before deadline without any production write', 'Read back frozen holdout evidence without retraining'):
            section = source.split('      - name: '+name+'\n', 1)[1].split('      - name:',1)[0]
            self.assertIn("steps.day.outputs.night_rollover != 'true'", section)
        for proof in ('WILD4_REQUESTED_CHECKPOINT_MISSING','WILD4_SETTLEMENT_STATUS','WILD4_SAME_RUN_ARTIFACT_RERUN_FORBIDDEN'):
            self.assertIn(proof,source)

if __name__ == '__main__':
    unittest.main()
