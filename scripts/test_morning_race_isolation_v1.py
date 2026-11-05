"""Inject local faults through the actual saved-input and admission route."""
from dataclasses import asdict
from datetime import date, timedelta
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from g11.canonical import official_pipeline as official
from g11.canonical import test_official_pipeline as fixtures
from morning_race_isolation_v1 import isolated_results, valid_quarantine
from rescue_night_result_parser_v1 import official_input_compatibility


DAY = fixtures.TARGET


def schedule_fixture():
    value=fixtures.schedule_text()
    # Parser fixtures reuse motor numbers; the real admission gate correctly
    # rejects that across different racers. Give every test racer one motor.
    for race in range(1,13):
        for boat in range(1,7):
            old=fixtures.schedule_entry_line(boat,4000+race*10+boat)
            new=old.replace(f'{boat+10} 40.{boat:02d}',f'{(race-1)*6+boat} 40.{boat:02d}')
            value=value.replace(old,new,1)
    return value


def second_venue(value):
    import re
    return re.sub(r'(?<!\d)4(\d{3})(?!\d)',r'5\1',value.replace('19KBGN','20KBGN').replace('19BBGN','20BBGN').replace('下関','若松'))


def prior_k(day, damage=None):
    first=fixtures.full_result_text().replace('2026/ 8/27',f'2026/ 8/{day.day}')
    if damage=='row':
        first=first.replace('6.06 6 0.06 1.50.0','BROKEN 6 0.06 1.50.0',1)
    elif damage=='identity':
        first=first.replace(' 06 6 4016 ', ' 06 6 ???? ',1)
    second=second_venue(fixtures.full_result_text()).replace('2026/ 8/27',f'2026/ 8/{day.day}')
    return first+'\n'+second


class SavedSourceClient:
    def __init__(self,store,*,damage=None,bad_b=False,missing_racers=(),six_prior_days=False):
        self.store=store;self.damage=damage;self.bad_b=bad_b;self.reads=[]
        self.missing_racers=set(missing_racers)
        self.six_prior_days=six_prior_days
        self.clock=lambda:fixtures.at(60)
    def fetch_once(self,source,url):
        old=self.store.response_receipt(source)
        if old is not None:return old
        self.reads.append(source)
        if source.startswith('schedule-b-'):
            b=schedule_fixture()
            if self.bad_b:
                line=next(line for line in b.splitlines() if line.startswith('6 4016'))
                b=b.replace(line,'DAMAGED',1)
            combined=b+'\n'+second_venue(schedule_fixture())
            if self.six_prior_days:
                combined=combined.replace('第 4日','第 7日')
            raw=combined.encode('cp932')
        elif source.startswith('racer-period-'):
            raw=b'\r\n'.join(fixtures.racer_record(base+race*10+boat)
                for base in (4000,5000) for race in range(1,13) for boat in range(1,7)
                if base+race*10+boat not in self.missing_racers)
        elif source.startswith('racelist-recovery-'):
            venue,race=source.rsplit('-',2)[-2:]
            base=4000 if venue=='19' else 5000
            raw=fixtures.racelist_status_html()
            for boat in range(1,7):
                raw=raw.replace(str(4000+boat).encode(),str(base+int(race)*10+boat).encode())
        else:
            compact=source[-8:];day=date(int(compact[:4]),int(compact[4:6]),int(compact[6:]))
            raw=prior_k(day,self.damage if day==DAY-timedelta(days=1) else None).encode('cp932')
        return self.store.commit_response(source,url,fixtures.at(len(self.reads)),raw,
            content_type='application/x-lzh-compressed',final_url=url)


class MorningIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
    def run_morning(self,**kwargs):
        store=official.OfficialArtifactStore.initialize(Path(self.tmp.name)/'store',DAY.isoformat(),None,fixtures.at(0).isoformat())
        client=SavedSourceClient(store,**kwargs)
        runner=official.CanonicalRunner(store,client=client,extractor=fixtures.IdentityArchiveExtractor())
        runner.run_morning(DAY,'https://www.boatrace.jp/owpc/pc/extra/data/fan2604.lzh',fixtures.at(0).isoformat())
        return store,runner,client

    def test_one_k_row_keeps_23_races_through_snapshot_gate_prediction_and_projection(self):
        with official_input_compatibility():
            store,runner,client=self.run_morning(damage='row')
            verified=store.load_verified_snapshot('morning','official-day-input')
            p=verified['SNAPSHOT']['PAYLOAD'];held='20260827-19-01'
            self.assertEqual(p['SCOPE']['EXPECTED_RACE_COUNT'],23)
            self.assertEqual(len(p['RAW38_ROWS']),23*6)
            self.assertEqual([r['RACE_KEY'] for r in p['QUARANTINED_RACES']],[held])
            self.assertEqual([r['RACE_KEY'] for r in p['RESULT_QUARANTINE']],['20260826-19-01'])
            self.assertTrue(valid_quarantine(p))
            day,eligible,excluded=runner._formal_prediction_scope(verified,failure_prefix='TEST')
            self.assertEqual(len(eligible),23);self.assertNotIn(held,eligible)
            self.assertNotIn(held,{r['RACE_KEY'] for r in p['PREDEADLINE_PLAN']})
            with self.assertRaises(official.FORMAL_ADAPTER.FormalP3InvocationError):
                official.FORMAL_ADAPTER.prepare_canonical_official_morning_invocation(verified,held)
            runner.run_morning_predictions()
            result=runner.run_morning_workbook_projection(fixtures.workbook_profiles())
            projection=store.load_verified_snapshot('morning','workbook-projection')['SNAPSHOT']['PAYLOAD']
            self.assertEqual(projection['COUNTS']['RACES'],23)
            self.assertEqual(projection['COUNTS']['FORMAL'],23)
            # The next phase independently replays B plus K evidence; it must
            # accept the same healthy subset instead of rejecting the metadata.
            official._night_sg_race_keys_from_saved_b(store,DAY,set(eligible),fixtures.IdentityArchiveExtractor())
            self.assertEqual(len(client.reads),5)
            self.assertEqual(len(client.reads),len(set(client.reads)))
            self.assertEqual(verified,store.load_verified_snapshot('morning','official-day-input'))

    def test_b_quarantine_is_admitted_by_actual_consumer(self):
        with official_input_compatibility():
            store,runner,_=self.run_morning(bad_b=True)
            verified=store.load_verified_snapshot('morning','official-day-input')
            p=verified['SNAPSHOT']['PAYLOAD']
            self.assertNotIn('RESULT_QUARANTINE',p)
            self.assertEqual(len(p['QUARANTINED_RACES']),1)
            _,eligible,_=runner._formal_prediction_scope(verified,failure_prefix='TEST')
            self.assertEqual(len(eligible),23)
            official._night_sg_race_keys_from_saved_b(store,DAY,set(eligible),fixtures.IdentityArchiveExtractor())

    def test_fifteen_recovery_races_keep_own_receipts_and_complete_saved_scope(self):
        missing={4000+race*10+1 for race in range(1,13)} | {5000+race*10+1 for race in range(1,4)}
        with official_input_compatibility():
            store,runner,client=self.run_morning(missing_racers=missing)
            verified=store.load_verified_snapshot('morning','official-day-input')
            payload=verified['SNAPSHOT']['PAYLOAD']
            recovered=[s for s in client.reads if s.startswith('racelist-recovery-')]
            self.assertEqual(15,len(recovered)); self.assertEqual(15,len(set(recovered)))
            self.assertEqual(24,payload['SCOPE']['EXPECTED_RACE_COUNT'])
            self.assertEqual(24*6,len(payload['RAW38_ROWS']))
            index={name:i for i,name in enumerate(payload['RAW38_FIELDS'])}
            for row in payload['RAW38_ROWS']:
                if row[index['登録番号']] not in missing: continue
                key=row[index['標準レースキー']]
                receipt=store.response_receipt('racelist-recovery-'+key.lower())
                self.assertIn(receipt,verified['SNAPSHOT']['SOURCE_RECEIPTS'])
                self.assertEqual(receipt['URL'],row[index['公式URL']])
                self.assertEqual(official.racelist_url(DAY,key[9:11],int(key[-2:])),receipt['URL'])
            _,eligible,_=runner._formal_prediction_scope(verified,failure_prefix='TEST')
            self.assertEqual(24,len(eligible))
            self.assertEqual(official.daily_request_cap(24),store.ledger.effective_scope()['DYNAMIC_REQUEST_CAP'])
            self.assertEqual(588,official.HARD_MAX_REQUESTS)
            official._night_sg_race_keys_from_saved_b(store,DAY,set(eligible),fixtures.IdentityArchiveExtractor())
            before=store.load_verified_snapshot('morning','official-day-input')
            # Saved responses are reused on retry; no second network request.
            runner.run_morning(DAY,'https://www.boatrace.jp/owpc/pc/extra/data/fan2604.lzh',fixtures.at(0).isoformat())
            self.assertEqual(15,len([s for s in client.reads if s.startswith('racelist-recovery-')]))
            self.assertEqual(before,store.load_verified_snapshot('morning','official-day-input'))

    def test_delayed_recovery_cli_uses_same_adapters_and_restores_arguments(self):
        import contextlib
        import subprocess
        import sys
        import textwrap
        # Fresh process isolates the retained rescue module's import adapters.
        code=textwrap.dedent('''
            import contextlib,sys
            from unittest.mock import patch
            import rescue_night_result_parser_v1 as entry
            argv=['entry','--delayed-recovery','--date','2026-11-06']
            sys.argv=argv[:]
            entered=[]
            @contextlib.contextmanager
            def scope():
                entered.append(True)
                yield
                entered.pop()
            def invoke(name,run_name):
                assert entered and name=='g11.relay.delayed_morning_recovery_v1'
                assert sys.argv==[name,'--date','2026-11-06']
                assert run_name=='__main__'
            with patch.object(entry,'official_input_compatibility',scope), patch('runpy.run_module',side_effect=invoke) as run, patch('repair_saved_morning_receipts_v1.run_if_reviewed',return_value=False):
                entry.run_relay()
                assert run.call_count==1
            assert sys.argv==argv and not entered
        ''')
        result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True)
        self.assertEqual(0,result.returncode,result.stderr)

    def test_twenty_three_receipts_reach_rescue_and_saved_prediction_projection(self):
        from g11.relay import rescue_v1
        missing={4000+race*10+1 for race in range(1,13)} | {5000+race*10+1 for race in range(1,4)}
        with official_input_compatibility():
            store,runner,client=self.run_morning(missing_racers=missing,six_prior_days=True)
            verified=store.load_verified_snapshot('morning','official-day-input')
            self.assertEqual(23,len(verified['SNAPSHOT']['SOURCE_RECEIPTS']))
            future,deadlines=rescue_v1._future_keys(verified,fixtures.at(0))
            self.assertEqual(24,len(future));self.assertEqual(24,len(deadlines))
            runner.run_morning_predictions()
            runner.run_morning_workbook_projection(fixtures.workbook_profiles())
            projection=store.load_verified_snapshot('morning','workbook-projection')['SNAPSHOT']['PAYLOAD']
            self.assertEqual(24,projection['COUNTS']['FORMAL'])
            self.assertEqual(verified,store.load_verified_snapshot('morning','official-day-input'))
            self.assertEqual(23,len(client.reads))

    def test_unknown_racer_identity_holds_only_dependent_venue(self):
        with official_input_compatibility():
            store,runner,_=self.run_morning(damage='identity')
            verified=store.load_verified_snapshot('morning','official-day-input')
            _,eligible,_=runner._formal_prediction_scope(verified,failure_prefix='TEST')
            self.assertEqual(len(eligible),12)
            self.assertTrue(all(k[9:11]=='20' for k in eligible))

    def test_other_results_remain_byte_equivalent(self):
        day=DAY-timedelta(days=1)
        complete=official.parse_result_text(prior_k(day),day)
        good,held=isolated_results(prior_k(day,'row'),day,official,official.parse_result_text)
        self.assertEqual(len(good),23);self.assertEqual(len(held),1)
        self.assertEqual([asdict(r) for r in good],[asdict(r) for r in complete if r.race_key!='20260826-19-01'])

    def test_wrong_day_and_duplicate_identity_are_not_silently_skipped(self):
        with self.assertRaisesRegex(official.PipelineError,'K_DATE_MISMATCH'):
            isolated_results(prior_k(DAY),DAY-timedelta(days=1),official,official.parse_result_text)
        with self.assertRaises(official.PipelineError):
            isolated_results(prior_k(DAY,'row').replace('2R 一般','1R 一般'),DAY,official,official.parse_result_text)

    def test_quarantine_cannot_overlap_healthy_scope_or_omit_evidence(self):
        with official_input_compatibility():
            store,_,_=self.run_morning(damage='row')
            p=store.load_verified_snapshot('morning','official-day-input')['SNAPSHOT']['PAYLOAD']
            p['QUARANTINED_RACES'][0]['RACE_KEY']='20260827-19-02'
            self.assertFalse(valid_quarantine(p))
            p['QUARANTINED_RACES'][0]['RACE_KEY']='20260827-19-01'
            p.pop('RESULT_QUARANTINE')
            self.assertFalse(valid_quarantine(p))

    def test_compatibility_restores_all_original_functions_on_error(self):
        originals=(official.parse_result_text,official.CanonicalRunner.run_morning,
            official.FORMAL_GATE.build_canonical_official_morning_input,official._night_sg_race_keys_from_saved_b)
        with self.assertRaisesRegex(RuntimeError,'injected'):
            with official_input_compatibility():raise RuntimeError('injected')
        self.assertEqual(originals,(official.parse_result_text,official.CanonicalRunner.run_morning,
            official.FORMAL_GATE.build_canonical_official_morning_input,official._night_sg_race_keys_from_saved_b))

    def test_daily_authority_boundary_still_runs_before_any_acquisition(self):
        original=official.CanonicalRunner.run_morning
        with official_input_compatibility():
            current=official.CanonicalRunner.run_morning
            self.assertIs(current.__code__,original.__code__)
            self.assertTrue(current._g11_daily_authority_boundary)
            store=official.OfficialArtifactStore.initialize(Path(self.tmp.name)/'guard',DAY.isoformat(),None,fixtures.at(0).isoformat())
            client=SavedSourceClient(store)
            runner=official.CanonicalRunner(store,client=client,extractor=fixtures.IdentityArchiveExtractor())
            with patch('g11.relay.daily_runtime_authority_v1.verify_runtime',side_effect=RuntimeError('authority-check')) as guard:
                with self.assertRaisesRegex(RuntimeError,'authority-check'):
                    runner.run_morning(date(2026,10,22),'unused','unused')
                guard.assert_called_once()
            self.assertEqual(client.reads,[])


if __name__=='__main__':unittest.main()
