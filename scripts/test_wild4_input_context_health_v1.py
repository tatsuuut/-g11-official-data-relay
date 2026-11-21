"""Detect missing formal LOCKs without changing the frozen eligibility gate."""
import ast
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import textwrap
import unittest


WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/g11-wild4-shadow-lock-only.yml'


class InputContextHealthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = WORKFLOW.read_text()
        start = cls.source.index('          def input_context_health(')
        end = cls.source.index('          now=datetime.now(timezone.utc)', start)
        ns = {'datetime': datetime}
        exec(compile(ast.parse(textwrap.dedent(cls.source[start:end])), str(WORKFLOW), 'exec'), ns)
        cls.health = staticmethod(ns['input_context_health'])

    def setUp(self):
        self.now = datetime.fromisoformat('2026-11-21T11:01:28+09:00')
        self.binding = {'EVALUATION_CONTEXT_PACK_ID': 'a' * 64,
                        'MODEL_SHA256': 'b' * 64, 'TRAINING_INPUT_SHA256': 'c' * 64}

    def row(self, key='20261121-09-02', context=None, deadline='2026-11-21T11:09:00+09:00'):
        return {'STANDARD_RACE_KEY': key,
                'UPSTREAM_PACKET': {'RACE_DEADLINE_AT': deadline,
                                    'EVALUATION_CONTEXT_PACK_ID': context or self.binding['EVALUATION_CONTEXT_PACK_ID'],
                                    'INFERENCE_PIPELINE_HASH': 'd' * 64}}

    def test_observed_fresh_future_mismatch_is_an_outage(self):
        report = self.health([self.row(context='e' * 64)], self.binding, self.now, False)
        self.assertEqual(report['status'], 'CONTEXT_MISMATCH')
        self.assertEqual(report['new_lock_context_blocked'], 1)
        self.assertEqual(report['expired'], 0)
        self.assertEqual(report['expected_context_id'], 'a' * 64)
        self.assertEqual(report['future_contexts'][0]['context_id'], 'e' * 64)
        self.assertEqual(report['future_contexts'][0]['component_hashes']['INFERENCE_PIPELINE_HASH'], 'd' * 64)

    def test_no_input_and_expired_input_remain_different_non_outages(self):
        missing = self.health([], self.binding, self.now, False)
        expired = self.health([self.row(deadline=self.now.isoformat())], self.binding, self.now, False)
        self.assertEqual(missing['status'], 'NO_INPUT')
        self.assertEqual(expired['status'], 'ALL_EXPIRED')
        self.assertEqual(expired['expired'], 1)
        self.assertEqual(missing['new_lock_context_blocked'], 0)
        self.assertEqual(expired['new_lock_context_blocked'], 0)

    def test_matching_future_remains_ready(self):
        report = self.health([self.row()], self.binding, self.now, False)
        self.assertEqual(report['status'], 'READY')
        self.assertEqual(report['matching_future'], 1)
        self.assertEqual(report['new_lock_context_blocked'], 0)

    def test_partial_match_reports_the_missing_part(self):
        rows = [self.row(), self.row(key='20261121-13-03', context='e' * 64)]
        report = self.health(rows, self.binding, self.now, False)
        self.assertEqual(report['status'], 'PARTIAL_CONTEXT_MISMATCH')
        self.assertEqual(report['matching_future'], 1)
        self.assertEqual(report['new_lock_context_blocked'], 1)

    def test_saved_immutable_lock_does_not_require_new_input(self):
        row = self.row(context='e' * 64)
        report = self.health([row], self.binding, self.now, False, [row['STANDARD_RACE_KEY']])
        self.assertEqual(report['status'], 'RETAINED_LOCKS_ONLY')
        self.assertEqual(report['new_lock_context_blocked'], 0)
        self.assertEqual(report['retained_lock_context_mismatch'], 1)

    def test_settlement_does_not_request_new_prediction(self):
        report = self.health([self.row(context='e' * 64)], self.binding, self.now, True)
        self.assertEqual(report['status'], 'SETTLEMENT_ONLY')
        self.assertEqual(report['new_lock_context_blocked'], 0)
        self.assertEqual(report['mismatching_future'], 1)

    def test_non_hash_values_and_arbitrary_fields_never_reach_logs(self):
        row = self.row(context='secret-example-token')
        row['UPSTREAM_PACKET']['INFERENCE_PIPELINE_HASH'] = 'https://secret.invalid/value'
        row['UPSTREAM_PACKET']['HEAD'] = {'private': 0.75}
        row['UPSTREAM_PACKET']['credentials'] = 'never-print'
        row['ODDS_120'] = {'1-2-3': 12345}
        report = self.health([row], self.binding, self.now, False, registry={
            'CONTRACT_SHA256': 'e' * 64, 'CALIBRATION_SHA256': 'never-print',
            'FINAL_HOLDOUT_SELECTION_COUNT': 12345})
        text = json.dumps(report)
        for excluded in ('secret-example-token', 'secret.invalid', 'never-print', 'ODDS_120', 'private', '12345'):
            self.assertNotIn(excluded, text)
        self.assertEqual(report['future_contexts'][0]['context_id'], 'INVALID_SHA256')
        self.assertEqual(report['candidate_registry_hashes'], {'CONTRACT_SHA256': 'e' * 64,
                                                               'CALIBRATION_SHA256': 'INVALID_SHA256'})

    def test_context_report_is_bounded(self):
        rows = [self.row(key=f'20261121-09-{i + 1:02d}', context=f'{i + 1:064x}') for i in range(12)]
        report = self.health(rows, self.binding, self.now, False)
        self.assertEqual(len(report['future_contexts']), 8)
        self.assertEqual(report['omitted_contexts'], 4)
        self.assertEqual(report['new_lock_context_blocked'], 12)

    def test_diagnostics_do_not_mutate_packets_binding_or_saved_lock_keys(self):
        rows = [self.row(), self.row(context='e' * 64)]
        saved = {'20261121-02-01'}
        before = deepcopy((rows, self.binding, saved))
        self.health(rows, self.binding, self.now, False, saved)
        self.assertEqual((rows, self.binding, saved), before)

    def test_original_eligibility_gate_and_delayed_failure_are_present(self):
        guard = "due=[r for r in records if datetime.fromisoformat(r['UPSTREAM_PACKET']['RACE_DEADLINE_AT'])>now\n               and r['UPSTREAM_PACKET']['EVALUATION_CONTEXT_PACK_ID']==binding['EVALUATION_CONTEXT_PACK_ID']]"
        step = self.source.split('      - name: Check for a matching unexpired official odds packet', 1)[1].split('      - name:', 1)[0]
        self.assertIn(guard, step)
        health_position = self.source.index("print('WILD4_INPUT_CONTEXT_HEALTH='")
        readback_position = self.source.index('      - name: Verify exact Site GET after priority notification input')
        finish_position = self.source.index('      - name: Report future input context failures after preserving independent work')
        self.assertLess(health_position, readback_position)
        self.assertLess(readback_position, finish_position)
        self.assertIn("if: always() && !cancelled() && steps.availability.outputs.context_blocked == 'true'", self.source)
        self.assertIn('.github/actions/g11-public-python@4a538d01976361e469568a946fed15a6def034e8', self.source)



class RetainedVenueEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source=WORKFLOW.read_text()
        start=cls.source.index('          def retained_venue_receipt(')
        end=cls.source.index('          stadium_path=',start)
        ns={}
        exec(compile(ast.parse(textwrap.dedent(cls.source[start:end])),str(WORKFLOW),'exec'),ns)
        cls.receipt=staticmethod(ns['retained_venue_receipt'])

    @staticmethod
    def sha(value):
        import hashlib
        return hashlib.sha256((json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode()).hexdigest()

    def seal(self,value):
        result={k:v for k,v in value.items() if k!='CONTENT_SHA256'}
        return {**result,'CONTENT_SHA256':self.sha(result)}

    def setUp(self):
        self.day='2026-11-21'
        self.candidate=self.seal({'SCHEMA':'G11_WILD_BET_PREDICTION_TIME_ODDS_DAY_V1',
            'OPERATIONAL_DATE_JST':self.day,'RESULT_DATA_PRESENT':False,'PREDICTION_STAGE':'MORNING',
            'RECORDS':[{'UPSTREAM_PACKET':{'EVALUATION_CONTEXT_PACK_ID':'a'*64}}]})
        self.envelope=self.seal({'SCHEMA':'G11_RETAINED_VENUE_ODDS_EVIDENCE_V1',
            'STATUS':'HELD_PENDING_INPUT_CONTRACT','FORMAL_INPUT_ELIGIBLE':False,
            'OPERATIONAL_DATE_JST':self.day,'CANDIDATE_ODDS':self.candidate,
            'RESULT_DATA_PRESENT':False,'PRODUCTION_MUTATED':False})
        self.verified=[]

    def verify(self,prior,value,day):
        self.assertIsNone(prior)
        self.assertEqual(day,self.day)
        if value.get('CONTENT_SHA256')!=self.sha({k:v for k,v in value.items() if k!='CONTENT_SHA256'}):
            raise ValueError('MERGE_SOURCE_SHA')
        self.assertEqual(value['SCHEMA'],'G11_WILD_BET_PREDICTION_TIME_ODDS_DAY_V1')
        self.verified.append(deepcopy(value))
        return deepcopy(value)

    def test_old_raw_source_with_matching_context_remains_quarantined(self):
        before=deepcopy(self.candidate)
        receipt=self.receipt(self.candidate,self.day,self.verify,self.sha)
        self.assertEqual(receipt,{'records':1,'source_sha256':self.candidate['CONTENT_SHA256']})
        self.assertNotIn('RECORDS',receipt)
        self.assertEqual(self.candidate,before)
        self.assertEqual(self.verified,[before])

    def test_new_explicitly_ineligible_envelope_keeps_only_evidence(self):
        before=deepcopy(self.envelope)
        receipt=self.receipt(self.envelope,self.day,self.verify,self.sha)
        self.assertEqual(receipt,{'records':1,'source_sha256':self.envelope['CONTENT_SHA256']})
        self.assertEqual(self.envelope,before)
        self.assertEqual(self.verified,[self.candidate])

    def test_false_eligibility_status_day_result_and_mutation_claims_are_rejected(self):
        for key,value in (('FORMAL_INPUT_ELIGIBLE',True),('STATUS','READY'),
            ('OPERATIONAL_DATE_JST','2026-11-22'),('RESULT_DATA_PRESENT',True),('PRODUCTION_MUTATED',True)):
            with self.subTest(key=key):
                envelope=self.seal({**self.envelope,key:value})
                with self.assertRaisesRegex(ValueError,'VENUE_EVIDENCE_BOUNDARY'):
                    self.receipt(envelope,self.day,self.verify,self.sha)
        self.assertEqual(self.verified,[])

    def test_changed_envelope_or_candidate_bytes_are_rejected(self):
        envelope=deepcopy(self.envelope); envelope['CANDIDATE_ODDS']['RECORDS']=[]
        with self.assertRaisesRegex(ValueError,'VENUE_EVIDENCE_BOUNDARY'):
            self.receipt(envelope,self.day,self.verify,self.sha)
        envelope=self.seal(envelope)
        with self.assertRaisesRegex(ValueError,'MERGE_SOURCE_SHA'):
            self.receipt(envelope,self.day,self.verify,self.sha)

    def test_formal_union_uses_only_persisted_and_direct_national_input(self):
        step=self.source.split('      - name: Check for a matching unexpired official odds packet',1)[1].split('      - name:',1)[0]
        self.assertIn('chosen=merge_verified_odds(odds, direct if direct_records else None, day)',step)
        self.assertNotIn('merge_verified_odds(chosen, stadium',step)
        self.assertNotIn('chosen=retained_venue_receipt',step)
        self.assertEqual(step.count('chosen='),1)
        self.assertIn("print('WILD4_VENUE_INPUT_QUARANTINED='+json.dumps(receipt,sort_keys=True))",step)


if __name__ == '__main__':
    unittest.main()
