from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from morning_source_receipt_limit_v1 import receipt_limit
from repair_saved_morning_receipts_v1 import inspect_attempts, DAY, SOURCE, ATTEMPTS, REASON


def envelope():
    day = date(2026,11,6)
    races = [{'operational_date':DAY,'venue_code':v,'race_no':r}
             for v in ('19','20') for r in range(1,13)]
    receipts,rows = [],[]
    def add(source,url):
        receipts.append({'SOURCE_ID':source,'URL':url,'FINAL_URL':url})
    add('schedule-b-20261106','https://www1.mbrace.or.jp/od2/B/202611/b261106.lzh')
    add('racer-period-fan2604','https://www.boatrace.jp/static_extra/pc_static/download/data/kibetsu/fan2604.lzh')
    for n in range(1,7):
        d=day-timedelta(days=n)
        add(f'result-k-{d:%Y%m%d}',f'https://www1.mbrace.or.jp/od2/K/{d:%Y%m}/k{d:%y%m%d}.lzh')
    for r in races[:15]:
        key=f"20261106-{r['venue_code']}-{r['race_no']:02d}"
        url=f"https://www.boatrace.jp/owpc/pc/race/racelist?hd=20261106&jcd={r['venue_code']}&rno={r['race_no']}"
        add('racelist-recovery-'+key,url)
        rows.append([key,url])
    return {'SNAPSHOT':{'SOURCE_RECEIPTS':receipts,'PAYLOAD':{
        'OPERATIONAL_DATE_JST':DAY,'SCOPE':{'EXPECTED_RACE_COUNT':24},
        'RACES':races,'RAW38_FIELDS':['標準レースキー','公式URL'],'RAW38_ROWS':rows}}}


class ReceiptLimitTests(unittest.TestCase):
    def test_twenty_three_independent_official_sources_fit_saved_scope(self):
        value=envelope(); before=deepcopy(value)
        self.assertEqual(23,len(value['SNAPSHOT']['SOURCE_RECEIPTS']))
        self.assertEqual(32,receipt_limit(value))
        self.assertEqual(before,value)

    def test_legacy_admission_limit_is_unchanged(self):
        value=envelope(); value['SNAPSHOT']['SOURCE_RECEIPTS']=value['SNAPSHOT']['SOURCE_RECEIPTS'][:20]
        self.assertEqual(20,receipt_limit(value))

    def test_extra_unbound_wrong_day_redirect_duplicate_and_result_sources_are_rejected(self):
        changes = [
            ('SOURCE_ID','unknown-source'),
            ('SOURCE_ID','racelist-recovery-20261106-01-01'),
            ('SOURCE_ID','racelist-recovery-20261107-19-01'),
            ('SOURCE_ID','result-k-20261106'),
            ('SOURCE_ID','schedule-b-20261106'),
            ('URL','https://www.boatrace.jp/owpc/pc/race/raceresult?hd=20261106&jcd=19&rno=1'),
            ('FINAL_URL','https://www.boatrace.jp/other'),
        ]
        for field,value in changes:
            with self.subTest(field=field,value=value):
                data=envelope();data['SNAPSHOT']['SOURCE_RECEIPTS'][-1][field]=value
                self.assertEqual(20,receipt_limit(data))

    def test_recovery_receipt_must_be_used_by_its_own_raw_row(self):
        data=envelope();data['SNAPSHOT']['PAYLOAD']['RAW38_ROWS'].pop()
        self.assertEqual(20,receipt_limit(data))

    def test_scope_hard_limit_and_period_are_enforced(self):
        for count in (0,25,289,True):
            data=envelope();data['SNAPSHOT']['PAYLOAD']['SCOPE']['EXPECTED_RACE_COUNT']=count
            self.assertEqual(20,receipt_limit(data))
        data=envelope();data['SNAPSHOT']['SOURCE_RECEIPTS'][1]['SOURCE_ID']='racer-period-fan2610'
        self.assertEqual(20,receipt_limit(data))


class RepairBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        failure=self.root/'recovery-failures'/DAY;failure.mkdir(parents=True)
        for name in ATTEMPTS:
            (failure/name).write_text(json.dumps({'reason':REASON,'source_sha':SOURCE}))
        snapshots=self.root/'rescue/store/snapshots/morning';snapshots.mkdir(parents=True)
        for name in ('official-day-input','abeken-head-features'):
            (snapshots/(name+'.json')).write_text('{}')
        self.now=datetime(2026,11,6,8,45,tzinfo=timezone(timedelta(hours=9)))

    def inspect(self):
        return inspect_attempts(self.root,DAY,SOURCE,self.now)

    def test_exact_two_failures_preserved_for_one_input_only_repair(self):
        before={p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(set(self.inspect()),ATTEMPTS)
        self.assertEqual(before,{p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_other_error_and_additional_attempt_rejected(self):
        path=self.root/'recovery-failures'/DAY/next(iter(ATTEMPTS))
        path.write_text(json.dumps({'reason':'AUTHORITY_MISMATCH','source_sha':SOURCE}))
        with self.assertRaisesRegex(RuntimeError,'FAILED_REASON'):self.inspect()
        (path.parent/'other.json').write_text('{}')
        with self.assertRaisesRegex(RuntimeError,'EXACT_FAILED'):self.inspect()

    def test_existing_prediction_and_second_repair_rejected(self):
        path=self.root/'rescue/store/snapshots/morning/p3.json';path.write_text('{}')
        with self.assertRaisesRegex(RuntimeError,'INPUT_ONLY'):self.inspect()
        path.unlink()
        marker=self.root/'recovery-repairs'/DAY/'receipt-scope-v1.json';marker.parent.mkdir(parents=True);marker.write_text('{}')
        with self.assertRaisesRegex(RuntimeError,'ALREADY_ATTEMPTED'):self.inspect()

    def test_historical_day_or_changed_source_rejected(self):
        with self.assertRaisesRegex(RuntimeError,'CURRENT_DAY'):
            inspect_attempts(self.root,DAY,SOURCE,self.now+timedelta(days=1))
        with self.assertRaisesRegex(RuntimeError,'FROZEN_SOURCE'):
            inspect_attempts(self.root,DAY,'f'*40,self.now)


if __name__=='__main__':unittest.main()
