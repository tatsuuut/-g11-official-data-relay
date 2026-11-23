import json
from contextlib import nullcontext
from datetime import datetime, timezone, timedelta
from pathlib import Path
import socket
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import saved_morning_transport_v1 as subject


class SavedMorningTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.day = '2026-11-24'
        self.authority = {'runtime_source_sha':'a'*40,'authority_sha256':'b'*64}
        self.implementation = Mock()
        self.implementation.restore_source.return_value = self.authority
        self.implementation.feed_binding.return_value = {'authority_sha256':'b'*64}
        self.relay = ModuleType('g11.relay.v1')
        self.relay._tree_sha = Mock(return_value='unchanged')
        self.relay._now_jst = lambda: datetime(2026,11,24,8,tzinfo=timezone(timedelta(hours=9)))
        self.relay._metadata = Mock(return_value={'MORNING_COMPLETE':True})
        self.relay._morning_projection = Mock(return_value={'LOCK49_ROWS':['saved']})
        self.relay.current_growth_p3 = SimpleNamespace(load_locked_publication=Mock(return_value={'READ_ONLY':True}))
        self.relay.prepare_prediction_models = Mock(side_effect=AssertionError('inference'))
        self.relay.build_feed = Mock(return_value={'stage':'MORNING','status':'PASS','operational_date_jst':self.day,'races':[]})
        self.relay._write_json = lambda p,v: p.write_text(json.dumps(v))
        g11 = ModuleType('g11'); package = ModuleType('g11.relay');package.v1=self.relay;g11.relay=package
        optional = ModuleType('optional_display_isolation_v1');optional.isolate_optional_display=lambda *_:nullcontext()
        compat = ModuleType('rescue_night_result_parser_v1');compat.official_input_compatibility=lambda:nullcontext()
        modules={'g11':g11,'g11.relay':package,'g11.relay.v1':self.relay,
                 'optional_display_isolation_v1':optional,'rescue_night_result_parser_v1':compat}
        self.enterContext(patch.dict(sys.modules,modules))
        self.enterContext(patch.object(subject,'retained_module',return_value=self.implementation))
        old_path=sys.path[:];self.addCleanup(lambda:sys.path.__setitem__(slice(None),old_path))

    def project(self):
        return subject.project(self.root,self.day,self.root/'source',self.root/'out/feed.json',123)

    def test_saved_feed_keeps_authority_and_verifies_both_phases_before_and_after(self):
        feed=self.project()
        self.assertEqual(feed['daily_runtime']['authority_sha256'],'b'*64)
        self.assertEqual(self.implementation.verify_locks.call_count,4)
        self.assertEqual(self.relay.build_feed.call_args.kwargs['github_sha'],'a'*40)
        self.relay.prepare_prediction_models.assert_not_called()

    def test_missing_complete_or_later_phase_never_rebuilds(self):
        self.relay._metadata.return_value={'MORNING_COMPLETE':False}
        with self.assertRaisesRegex(RuntimeError,'NOT_COMPLETE'):self.project()
        self.relay._metadata.return_value={'MORNING_COMPLETE':True}
        folder=self.root/'store/snapshots/predeadline';folder.mkdir(parents=True);(folder/'saved.json').write_text('{}')
        with self.assertRaisesRegex(RuntimeError,'LATER_PHASE'):self.project()
        self.relay.build_feed.assert_not_called()

    def test_projection_cannot_open_network_and_restores_guard(self):
        original=socket.socket.connect
        def network(*_,**__):
            with socket.socket() as connection:connection.connect(('127.0.0.1',1))
        self.relay.build_feed.side_effect=network
        with self.assertRaisesRegex(RuntimeError,'NETWORK_OR_PREDICTION_FORBIDDEN'):self.project()
        self.assertIs(socket.socket.connect,original)
        self.assertFalse((self.root/'out/feed.json').exists())

    def test_store_drift_never_emits_feed(self):
        self.relay._tree_sha.side_effect=['before','changed']
        with self.assertRaisesRegex(RuntimeError,'STORE_CHANGED'):self.project()
        self.assertFalse((self.root/'out/feed.json').exists())


if __name__=='__main__':unittest.main()
