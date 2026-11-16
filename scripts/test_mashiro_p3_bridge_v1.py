from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import research_state_isolation_v1 as I
import isolated_research_runner_v1 as runner
import mashiro_p3_bridge_v1 as bridge

class IsolationTests(unittest.TestCase):
    def test_report_is_day_scoped_research_only_and_excluded_from_canonical_night(self):
        name='store/research/mashiro-p3-bridge-v1/2026-11-17/report.json'
        self.assertTrue(I.allowed(name,'2026-11-17'))
        self.assertFalse(I.allowed(name,'2026-11-17',night=True))
        self.assertFalse(I.allowed(name,'2026-11-18'))
        self.assertFalse(I.allowed(name.replace('report.json','../../model.json'),'2026-11-17'))
    def test_absent_optional_study_does_not_import_or_infer_or_mutate(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            with patch.object(runner.importlib.util,'spec_from_file_location',side_effect=AssertionError('imported')):
                result=runner.paired_mashiro_research(p,p,p,p,'2026-11-17','checkpoint')
            self.assertFalse(result['CHANGED']);self.assertEqual([],list(p.iterdir()))
    def test_bridge_publisher_requires_checkpoint_before_credentials(self):
        with patch.dict('os.environ',{'RESEARCH_CHECKPOINT_VERIFIED':'false'}):
            with self.assertRaisesRegex(ValueError,'PERSISTENCE_REQUIRED'):bridge.publish(Path('/not-read'),Path('/not-read'),'2026-11-17')
    def test_scoped_parent_selection_does_not_replace_unrelated_mutable_research(self):
        day='2026-11-17';keep='store/snapshots/predeadline/mashiro-v4-shadow-20261117-01-01.json'
        report='store/research/mashiro-p3-bridge-v1/'+day+'/report.json'
        value={'members':{keep:{'sha':'a'},report:{'sha':'b'},I.DEME:{'sha':'c'}},'_root':Path('/verified')}
        with patch.object(I,'apply_files',return_value=2) as apply:
            self.assertEqual(2,runner.apply_paired_saved_inputs(value,Path('/copy'),day))
        selected=apply.call_args.args[0]
        self.assertEqual({keep,report},set(selected['members']));self.assertIn(I.DEME,value['members'])
    def test_scoped_parent_import_preserves_the_same_seed_conflict_guard(self):
        day='2026-11-17';key='store/snapshots/predeadline/mashiro-v4-shadow-20261117-01-01.json'
        with patch.object(I,'apply_files',side_effect=ValueError('RESEARCH_SEED_CONFLICT')):
            with self.assertRaisesRegex(ValueError,'SEED_CONFLICT'):
                runner.apply_paired_saved_inputs({'members':{key:{}},'_root':Path('/verified')},Path('/copy'),day)

if __name__=='__main__':unittest.main()
