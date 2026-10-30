import copy
import unittest
from deme_research_delivery_v1 import verify_thirty_readback


def section(name,start,count):
    return dict(selection=dict(selection_id=name,activated_at='2026-10-30T20:00:00+09:00',requested_start_date_jst=start),observations={str(i):{} for i in range(count)})
def view(s):
    return dict(selection=s['selection'],registration=dict(status='SAVED_READBACK_VERIFIED',registered_at=s['selection']['activated_at'],requested_start_date_jst=s['selection']['requested_start_date_jst']),observation=dict(saved_records=len(s['observations'])))

class ReadbackTests(unittest.TestCase):
    def setUp(self):
        self.old=section('old','2026-10-30',3)
        self.old['replacement']=section('hinata','2026-10-31',0)
        self.old['nikoshi_a']=section('nikoshi','2026-10-31',0)
        self.canonical={'thirty_focus_v1':self.old};self.packet={'sha256':'a'*64}
    def test_registration_before_start_and_after_switch(self):
        before=dict(view(self.old),upcoming=view(self.old['replacement']),additional=view(self.old['nikoshi_a']),transport_sha256='a'*64)
        verify_thirty_readback(self.canonical,before,self.packet)
        self.old['replacement']['observations']={'h':{}}
        self.old['nikoshi_a']['observations']={'d':{},'n':{}}
        after=dict(view(self.old['replacement']),previous=view(self.old),additional=view(self.old['nikoshi_a']),transport_sha256='a'*64)
        verify_thirty_readback(self.canonical,after,self.packet)
        for mutate in (lambda v:v.pop('additional'),lambda v:v['previous']['observation'].update(saved_records=0),lambda v:v.update(transport_sha256='b'*64),lambda v:v['additional']['registration'].update(requested_start_date_jst='2026-10-30')):
            bad=copy.deepcopy(after);mutate(bad)
            with self.assertRaises(ValueError):verify_thirty_readback(self.canonical,bad,self.packet)

if __name__=='__main__':unittest.main()
