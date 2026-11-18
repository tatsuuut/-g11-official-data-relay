from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from night_archive_continuity_v1 import separate_result_host
from previous_night_dispatch_v1 import reconcile, JST

DAY='2026-11-18'
SID='result-k-20261118'
def archive_url(day):
    return 'https://www1.mbrace.or.jp/od2/K/'+day.strftime('%Y%m/k%y%m%d.lzh')
from datetime import date
URL=archive_url(date.fromisoformat(DAY))
LIVE='www.boatrace.jp'
def ledger(rejections):
    events=[]
    for i,(host,source,outcome) in enumerate(rejections):
        events.extend([{'TYPE':'REQUEST_STARTED','PAYLOAD':{'REQUEST_ID':str(i),'HOST':host,'SOURCE_ID':source}},
                       {'TYPE':'OUTCOME_RECORDED','PAYLOAD':{'REQUEST_ID':str(i),'OUTCOME':outcome}}])
    return SimpleNamespace(_manifest=lambda:{'OPERATIONAL_DATE_JST':DAY},events=lambda:events)

class TestContinuity(unittest.TestCase):
    def test_only_unaffected_official_archive_is_admitted(self):
        live=[(LIVE,'beforeinfo-1','HTTP_403')]*2
        self.assertTrue(separate_result_host(ledger(live),SID,URL,archive_url))
        for source,url in [(SID,URL+'?x'),('beforeinfo-1',URL),('result-k-20261117',URL)]:
            self.assertFalse(separate_result_host(ledger(live),source,url,archive_url))
        for events in [live[:1],live+[('www1.mbrace.or.jp',SID,'HTTP_403')],[(LIVE,'odds3t-1','HTTP_429')]*3]:
            self.assertFalse(separate_result_host(ledger(events),SID,URL,archive_url))
        self.assertTrue(separate_result_host(ledger([(LIVE,'beforeinfo-1','HTTP_429')]*3),SID,URL,archive_url))

    def test_previous_day_only_no_competing_run_or_duplicate(self):
        now=datetime(2026,11,19,2,40,tzinfo=JST)
        def run(stage='PREDEADLINE',other=None,prior=None,complete=True):
            client=Mock()
            def request(method,url,*data):
                if method=='POST': return None
                rows=(other if 'status=' in url else prior) or []
                return {'workflow_runs':rows,'total_count':len(rows)}
            client.request.side_effect=request
            status=reconcile(now,'2026-11-19','10',lambda d:{'date':d,'stage':stage,'races':132},client,lambda day: complete)
            return status,client
        status,client=run()
        self.assertEqual(status,'DISPATCHED_'+DAY)
        self.assertEqual(client.request.call_args.args[-1]['inputs'],{'phase':'night','operational_date':DAY})
        self.assertEqual(run('NIGHT')[0],'ALREADY_PUBLISHED')
        self.assertEqual(run('NIGHT',complete=False)[0],'DISPATCHED_'+DAY)
        self.assertEqual(run(other=[{'id':11}])[0],'OTHER_RUN_ACTIVE')
        self.assertEqual(run(prior=[{'display_title':'LIVE-night-'+DAY}])[0],'RECOVERY_ALREADY_REQUESTED')
