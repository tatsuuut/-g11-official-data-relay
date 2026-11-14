"""Separate persistence, immutable forecasts, rollback and NIGHT-only handover."""
import copy
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import research_state_isolation_v1 as data
import isolated_research_runner_v1 as runner
from critical_saved_publication_v1 import newest_checkpoint_key

DAY='2026-11-14'
NOW=datetime.fromisoformat(DAY+'T12:00:00+09:00')
KEY='20261114-02-06'
BINDING={'authority_sha256':'a'*64,'runtime_source_sha':'b'*40}
SOURCE='g11-state-v4-critical-'+DAY+'-42'
PACKET=f'store/g11/wild-pack-oos/pre_race/{DAY}/collection-packet-{KEY}.json'
CONFIDENCE=f'store/snapshots/morning/current-growth-p3-confidence-{KEY}.json'


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.state=self.root/'canonical';self.state.mkdir()
        self.work=self.root/'research';self.bundle=self.root/'bundle'
        plan=[{'RACE_KEY':KEY,'DUE_AT_JST':NOW.isoformat(),
               'MUST_FINISH_BEFORE_JST':(NOW+timedelta(minutes=7)).isoformat()}]
        self.write(self.state,'store/snapshots/morning/official-day-input.json',
            {'STAGE':'MORNING','NAME':'official-day-input','PAYLOAD':{'PREDEADLINE_PLAN':plan}})
        self.write(self.state,f'store/snapshots/predeadline/current-growth-p3-live-{KEY}.json',{'bets':['1-2-3']})
        self.write(self.state,'model/immutable.bin',{'model':'frozen'})
        self.write(self.state,data.DEME,{'first':'preserved'})
        data.activate(self.state,DAY,now=NOW)
        shutil.copytree(self.state,self.work)
        self.sources=data.core_sources(self.work,DAY)
        self.seeds={n:data.digest(p) for n,p in data.research_files(self.work,DAY).items()}
        self.write(self.work,data.DEME,{'first':'preserved','second':'new observation'})
        self.write(self.work,PACKET,{'RACE_ID':KEY,'PREDICTION_AT':(NOW+timedelta(seconds=30)).isoformat(),
            'RESULT_USED_AS_PREDICTION_FEATURE':False})
        self.output=self.root/'outputs';self.environment=patch.dict(os.environ,{'GITHUB_OUTPUT':str(self.output)})
        self.environment.start();self.addCleanup(self.environment.stop)

    def write(self,root,name,value,at=None):
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(data.canonical(value))
        instant=(at or (NOW+timedelta(minutes=1))).timestamp()
        os.utime(path,(instant,instant));return path

    def make(self,closed=False):
        data.export_bundle(self.work,self.bundle,DAY,BINDING,SOURCE,self.seeds,self.sources,
            closed=closed,now=NOW+timedelta(minutes=10 if closed else 2))
        value=data.read_bundle(self.bundle,DAY);value['_root']=self.bundle;return value

    def test_research_has_its_own_allowlist_and_cannot_export_core_or_models(self):
        for name in ('../escape','store/snapshots/predeadline/current-growth-p3-live-'+KEY+'.json',
                     'store/snapshots/morning/official-day-input.json','relay-state.json','model/immutable.bin'):
            self.assertFalse(data.allowed(name,DAY))
        value=self.make();self.assertEqual({PACKET,data.DEME},set(value['members']))
        self.assertFalse(value['canonical_state_written'])
        self.assertEqual(self.sources,data.core_sources(self.state,DAY))

    def test_modified_bytes_foreign_day_and_hidden_paths_are_rejected(self):
        self.make()
        with self.assertRaises(ValueError):data.read_bundle(self.bundle,'2026-11-15')
        (self.bundle/'files'/PACKET).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'READBACK'):data.read_bundle(self.bundle,DAY)

    def test_traversal_and_symlink_never_target_canonical_paths(self):
        with self.assertRaises(ValueError):data.safe_file(self.state,'store/../../escape')
        (self.state/'linked').symlink_to(self.root)
        with self.assertRaises(ValueError):data.safe_file(self.state,'linked/escape')

    def test_copy_failure_removes_temporary_file_and_rolls_back(self):
        value=self.make()
        original=shutil.copy2
        def fail(source,target,*args,**kwargs):
            if str(target).endswith('.research-import'):
                Path(target).write_bytes(b'partial')
                raise OSError('injected short copy')
            return original(source,target,*args,**kwargs)
        with patch.object(data.shutil,'copy2',fail):
            with self.assertRaises(OSError):data.apply_files(value,self.state,DAY)
        self.assertFalse(list(self.state.rglob('*.research-import')))
        self.assertEqual({'first':'preserved'},json.loads((self.state/data.DEME).read_bytes()))

    def test_old_source_and_seed_conflicts_fail_before_any_copy(self):
        value=self.make();data.validate_source(value,self.state,DAY,BINDING)
        self.write(self.state,data.DEME,{'changed':'canonical configuration'})
        with self.assertRaisesRegex(ValueError,'SEED_CONFLICT'):data.apply_files(value,self.state,DAY)
        self.assertFalse((self.state/PACKET).exists())
        self.write(self.state,'store/snapshots/morning/official-day-input.json',{'changed':'input'})
        with self.assertRaisesRegex(ValueError,'SOURCE_CHANGED'):data.validate_source(value,self.state,DAY,BINDING)

    def test_late_research_is_held_and_never_promoted(self):
        path=self.work/PACKET;instant=(NOW+timedelta(minutes=7,seconds=1)).timestamp()
        os.utime(path,(instant,instant))
        value=self.make();self.assertEqual(1,value['held_late_count']);self.assertNotIn(PACKET,value['members'])
        other=self.root/'false-close'
        with self.assertRaisesRegex(ValueError,'BEFORE_LAST_DEADLINE'):
            data.export_bundle(self.work,other,DAY,BINDING,SOURCE,self.seeds,self.sources,
                closed=True,now=NOW+timedelta(minutes=2))

    def auth(self,fail=False):
        def seal(state,day,phase):
            path=state/'daily-runtime'/day/'bindings/PREDEADLINE/new.json'
            path.parent.mkdir(parents=True,exist_ok=True);path.write_text('sealed')
            if fail:raise ValueError('injected seal failure')
        return SimpleNamespace(verify_runtime=Mock(return_value=BINDING),verify_locks=Mock(),seal=seal)

    def test_night_import_requires_close_keeps_core_and_ignores_morning_sidecars(self):
        self.write(self.work,CONFIDENCE,{'PAYLOAD':{'RACE_ID':KEY,'RESULT_INPUT_USED':False}})
        self.make(closed=True);before=data.core_sources(self.state,DAY)
        with self.assertRaisesRegex(ValueError,'BEFORE_LAST_DEADLINE'):
            runner.import_night(self.state,self.root,self.bundle,DAY,authority=self.auth(),now=NOW)
        runner.import_night(self.state,self.root,self.bundle,DAY,authority=self.auth(),now=NOW+timedelta(minutes=11))
        self.assertTrue((self.state/PACKET).is_file())
        self.assertFalse((self.state/CONFIDENCE).exists())
        self.assertEqual(before,data.core_sources(self.state,DAY))
        self.assertEqual('new observation',json.loads((self.state/data.DEME).read_bytes())['second'])
        marker=json.loads((self.state/data.MARKER.format(day=DAY)).read_bytes())
        self.assertTrue(marker['night_imported_payload_sha256'])

    def test_failed_seal_rolls_back_research_and_binding_without_touching_locks(self):
        self.make(closed=True)
        before={p.relative_to(self.state).as_posix():p.read_bytes() for p in self.state.rglob('*') if p.is_file()}
        with self.assertRaisesRegex(ValueError,'injected'):
            runner.import_night(self.state,self.root,self.bundle,DAY,authority=self.auth(True),now=NOW+timedelta(minutes=11))
        self.assertEqual(before,{p.relative_to(self.state).as_posix():p.read_bytes() for p in self.state.rglob('*') if p.is_file()})

    def test_unclosed_bundle_waits_without_touching_canonical(self):
        self.make();before=data.core_sources(self.state,DAY)
        runner.import_night(self.state,self.root,self.bundle,DAY,authority=self.auth(),now=NOW+timedelta(minutes=11))
        self.assertIn('waiting=true',self.output.read_text())
        self.assertFalse((self.state/PACKET).exists());self.assertEqual(before,data.core_sources(self.state,DAY))


class RoutingTests(unittest.TestCase):
    def test_only_owned_main_live_and_night_events_are_selected(self):
        parent={'id':42,'head_branch':'main','head_repository':{'full_name':runner.REPOSITORY},
                'display_title':'LIVE-predeadline-'+DAY}
        self.assertEqual('predeadline',runner.event_selection({'workflow_run':parent},NOW)['phase'])
        for change in ({'head_branch':'feature'},{'head_repository':{'full_name':'other/repo'}},
                       {'display_title':'LIVE-morning-'+DAY}):
            self.assertIsNone(runner.event_selection({'workflow_run':{**parent,**change}},NOW))

    def test_research_cache_never_becomes_canonical_restore_candidate(self):
        rows=[{'key':'g11-research-v1-'+DAY+'-99-1','ref':'refs/heads/main','id':2,
               'created_at':'2026-11-14T04:00:00Z'},
              {'key':SOURCE,'ref':'refs/heads/main','id':1,'created_at':'2026-11-14T03:00:00Z'}]
        self.assertEqual(SOURCE,newest_checkpoint_key(DAY,rows))

    def test_independent_workflow_has_no_canonical_publish_or_cache_writer(self):
        root=Path(__file__).resolve().parents[1]
        text=(root/'.github/workflows/g11-isolated-live-research.yml').read_text()
        self.assertIn('group: g11-isolated-saved-input-research',text)
        self.assertNotIn('g11-free-official-data-runner',text)
        self.assertIn('cache-key: g11-research-v1-',text)
        self.assertNotIn('daily_runtime_delivery_v1.py',text)
        self.assertNotIn('critical_saved_publication_v1.py',text)
        primary=(root/'.github/workflows/g11-free-runner.yml').read_text()
        self.assertNotIn('needs: saved-research',primary)
        self.assertIn("steps.research-isolation.outputs.active!='true'",primary)
        self.assertIn("steps.research-night.outputs.waiting!='true'",primary)


if __name__=='__main__':unittest.main()
