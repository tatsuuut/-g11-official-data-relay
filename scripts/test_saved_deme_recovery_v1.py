import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from saved_deme_recovery_v1 import recover, JST, datetime


class SavedResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.day=datetime.now(JST).date().isoformat()
        self.feed=dict(status='PASS',stage='MORNING',operational_date_jst=self.day,races=[{'key':'original'}])
        self.request=Mock(return_value=(self.feed,{'x-g11-source-feed-sha256':'a'*64}))
        self.runner=Mock(return_value={'readback':'PASS','capture':{'status':'PASS'}})
        self.env=patch.dict(os.environ,{'G11_APP_ORIGIN':'https://saved.example'});self.env.start();self.addCleanup(self.env.stop)

    def run_recovery(self):
        return recover(self.root,self.root,self.day,self.root/'display/feed.json',request=self.request,runner=self.runner)

    def test_idle_morning_and_live_retry_use_same_saved_input_entry(self):
        for phase in ('MORNING','PREDEADLINE'):
            self.feed['stage']=phase;self.run_recovery()
            args=self.runner.call_args
            self.assertEqual(args.kwargs,{'fast':True})
            self.assertEqual(args.args[0],self.root/'store')
            self.assertEqual(json.loads(args.args[1].read_bytes()),self.feed)

    def test_wrong_day_bad_source_rescue_and_unaccepted_feed_do_not_run(self):
        for field,value in [('operational_date_jst','2000-01-01'),('status','FAIL'),('stage','NIGHT'),('same_day_rescue',{'proof':True}),('races',[])]:
            old=dict(self.feed);self.feed[field]=value
            with self.assertRaises(ValueError):self.run_recovery()
            self.feed.clear();self.feed.update(old)
        self.request.return_value=(self.feed,{'x-g11-source-feed-sha256':'bad'})
        with self.assertRaises(ValueError):self.run_recovery()
        self.runner.assert_not_called()

    def test_projection_failure_is_not_reported_as_saved(self):
        self.runner.return_value={'readback':'HELD'}
        with self.assertRaises(ValueError):self.run_recovery()


if __name__=='__main__':unittest.main()
