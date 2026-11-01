"""Execute the pinned workflow's original-LOCK recovery and publication gates.

Private project/model/settlement implementations are imported unchanged. No
prediction function is called. The fixtures are synthetic sealed LOCK ledgers;
the production workflow separately authenticates the original encrypted ZIP.
"""
from copy import deepcopy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import textwrap
import unittest
import urllib.error
import zipfile
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = Path(os.environ.get('WILD4_WORKFLOW_PATH',
    ROOT / '.github/workflows/g11-wild4-shadow-lock-only.yml'))
DAY = '2026-11-01'
MISSING = '20261101-19-02'


def block(marker):
    source = WORKFLOW.read_text()
    match = re.search(r"python3 - <<'" + marker + r"'\n(.*?)\n          " + marker, source, re.S)
    if not match:
        raise AssertionError('Pinned workflow block missing: ' + marker)
    scope = {'__name__': 'workflow_contract_test'}
    exec(compile(textwrap.dedent(match.group(1)), str(WORKFLOW), 'exec'), scope)
    return scope


try:
    from g11.wild_pack.v1 import wild4_lifecycle_v1 as life
    from g11.wild_pack.v1.wild4_display_projection_v1 import project
    if os.environ.get('WILD4_SETTLEMENT_SOURCE_PATH'):
        settlement_path = Path(os.environ['WILD4_SETTLEMENT_SOURCE_PATH'])
        spec = importlib.util.spec_from_file_location('pinned_wild4_settlement', settlement_path)
        settlement = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(settlement)
    else:
        from g11.wild_pack.v1 import wild4_official_settlement_v1 as settlement
    PRIVATE_READY = True
except ImportError:
    if os.environ.get('G11_REQUIRE_PRIVATE_WILD4_UNION') == '1':
        raise
    PRIVATE_READY = False


class MetadataAndGateTests(unittest.TestCase):
    def setUp(self):
        self.scope = block('PYORIGINALARCHIVE')

    def metadata(self):
        n = self.scope
        run = {'id': n['RUN_ID'], 'status': 'completed', 'conclusion': 'success',
            'event': 'workflow_dispatch', 'head_branch': 'main', 'head_sha': n['HEAD_SHA'],
            'path': '.github/workflows/g11-morning-odds-footprint-4bet-shadow-v1.yml',
            'repository': {'id': n['REPOSITORY_ID']}, 'head_repository': {'id': n['REPOSITORY_ID']}}
        artifact = {'id': n['ARTIFACT_ID'], 'name': n['ARTIFACT_NAME'], 'expired': False,
            'size_in_bytes': n['ARTIFACT_SIZE'], 'digest': 'sha256:' + n['ARTIFACT_SHA'],
            'workflow_run': {'id': n['RUN_ID'], 'head_branch': 'main', 'head_sha': n['HEAD_SHA'],
                'repository_id': n['REPOSITORY_ID'], 'head_repository_id': n['REPOSITORY_ID']}}
        return run, {'artifacts': [artifact]}

    def test_production_archive_identity_is_exact(self):
        n = self.scope
        self.assertEqual(n['RUN_ID'], 41291307990)
        self.assertEqual(n['ARTIFACT_ID'], 13077092759)
        self.assertEqual(n['ARTIFACT_SIZE'], 1250101)
        self.assertEqual(n['ARTIFACT_SHA'], '8d8456c9bd80d748d8613df94aee4bf9a9b80adc58b4e6d5f678648422f3f791')
        self.assertEqual(block('PYSAVEDLEDGERUNION')['ORIGINAL_LOCK_SHA'],
            '0aa905427444a0c3a8c018bb5055aea3abc9c463cdf068197bf63c16f064df5b')
        run, listing = self.metadata()
        self.assertEqual(n['verify_metadata'](run, listing), listing['artifacts'][0])

    def test_foreign_failed_expired_or_changed_artifact_is_rejected(self):
        for field, wrong in (('conclusion', 'failure'), ('head_sha', 'a'*40),
                             ('head_branch', 'other'), ('event', 'pull_request'),
                             ('repository', {'id': 1}), ('head_repository', {'id': 1})):
            with self.subTest(field=field):
                run, listing = self.metadata(); run[field] = wrong
                with self.assertRaises(ValueError):
                    self.scope['verify_metadata'](run, listing)
        for field, wrong in (('expired', True), ('digest', 'sha256:' + 'f'*64),
                             ('size_in_bytes', 1), ('name', 'other'), ('id', 1)):
            with self.subTest(field=field):
                run, listing = self.metadata(); listing['artifacts'][0][field] = wrong
                with self.assertRaises(ValueError):
                    self.scope['verify_metadata'](run, listing)

    def test_archive_digest_and_signed_request_never_forward_api_authorization(self):
        n = self.scope
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as target:
            target.writestr('future-locks.tgz.gpg', b'encrypted-fixture')
        raw = archive.getvalue()
        n['ARTIFACT_SIZE'], n['ARTIFACT_SHA'] = len(raw), hashlib.sha256(raw).hexdigest()
        run, listing = self.metadata()
        signed = 'https://artifact.example.test/fixture?signature=redacted'
        requests = []
        class Response(io.BytesIO):
            status = 200
        class Opener:
            def open(_, request, timeout):
                requests.append(request)
                if request.full_url.endswith('/zip'):
                    raise urllib.error.HTTPError(request.full_url, 302, '', {'Location': signed}, None)
                if request.full_url == signed:
                    return Response(raw)
                return Response(json.dumps(listing if '/artifacts?' in request.full_url else run).encode())
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ,
                {'GITHUB_REPOSITORY': n['REPO'], 'OPERATIONAL_DATE': DAY}):
            n['download_original'](Path(tmp), 'fixture-token', Opener())
            self.assertEqual((Path(tmp) / 'wild4-original-136.tgz.gpg').read_bytes(), b'encrypted-fixture')
            self.assertEqual(len(requests), 4)
            self.assertTrue(all(r.get_header('Authorization') == 'Bearer fixture-token' for r in requests[:3]))
            self.assertIsNone(requests[-1].get_header('Authorization'))
            self.assertIsNone(n['NoRedirect']().redirect_request(None, None, 302, '', {}, signed))
            n['ARTIFACT_SHA'] = 'f'*64
            listing['artifacts'][0]['digest'] = 'sha256:' + 'f'*64
            with self.assertRaisesRegex(ValueError, 'ZIP_SHA'):
                n['download_original'](Path(tmp), 'fixture-token', Opener())

    def test_attempted_publication_failure_is_fatal_in_every_mode(self):
        source = WORKFLOW.read_text()
        start = source.index('      - name: Require successful attempted public projection and delivery')
        end = source.index('      - name: Require completion for verified NIGHT handoff', start)
        section = source[start:end]
        self.assertIn("steps.public-source.outcome == 'failure'", section)
        self.assertIn("steps.public-projection.outcome == 'failure'", section)
        script = textwrap.dedent(section.split('        run: |\n', 1)[1])
        for source_state, projection_state, ready, delivery, wanted in (
            ('success','success','true','success',0),
            ('success','success','true','failure',1),
            ('success','success','true','skipped',1),
            ('failure','skipped','','skipped',1),
            ('success','failure','','skipped',1),
            ('success','success','','skipped',0),
            ('skipped','skipped','','skipped',0)):
            with self.subTest(source=source_state, projection=projection_state, ready=ready, delivery=delivery):
                body = script
                for key, value in {'public-source.outcome': source_state,
                    'public-projection.outcome': projection_state,
                    'public-projection.outputs.ready': ready, 'public-delivery.outcome': delivery}.items():
                    body = body.replace('${{ steps.' + key + ' }}', value)
                result = subprocess.run(['bash', '-euo', 'pipefail', '-c', body], capture_output=True)
                self.assertEqual(result.returncode, wanted, result.stderr)


@unittest.skipUnless(PRIVATE_READY, 'Pinned private WILD4 verification modules required')
class SavedLedgerUnionTests(unittest.TestCase):
    def setUp(self):
        self.scope = block('PYSAVEDLEDGERUNION')
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base, self.donor = self.root/'base', self.root/'donor'
        self.base.mkdir(); self.donor.mkdir()
        self.canonical, self.sealed = self.scope['canonical'], self.scope['sealed']
        model_raw = b'fixture-frozen-model-bytes'
        model_sha = hashlib.sha256(model_raw).hexdigest()
        registry = {'MODEL_SHA256': model_sha, 'DATASET_SHA256': 'd'*64, 'MODEL_VERSION': 'FIXTURE',
            'STATUS': 'HISTORICAL_SHADOW_CANDIDATE_FROZEN_FUTURE_OOS_REQUIRED', 'WILD_ID': 'WILD_BET',
            'PRODUCTION_PROMOTION_ELIGIBLE': False, 'APP_PUBLISHED': False, 'INITIAL_ELIGIBLE_RACES': 10000}
        registry['REGISTRY_SHA256'] = hashlib.sha256(self.canonical(registry)).hexdigest()
        binding = self.sealed({'SCHEMA': 'G11_WILD_BET_SHADOW_CANDIDATE_BINDING_V1',
            'REGISTRY_SHA256': registry['REGISTRY_SHA256'], 'MODEL_SHA256': model_sha,
            'TRAINING_INPUT_SHA256': registry['DATASET_SHA256'], 'TRAINING_CUTOFF_DATE': '2026-10-01',
            'FUTURE_OOS_START_DATE': '2026-10-02', 'EVALUATION_CONTEXT_PACK_ID': 'fixture-pack',
            'FUTURE_OOS_REQUIRED': True, 'PRODUCTION_PROMOTION_ELIGIBLE': False, 'APP_PUBLISHED': False})
        for root in (self.base, self.donor):
            model = root/'model-registry/models'/model_sha; model.mkdir(parents=True)
            (model/'model.cbm').write_bytes(model_raw)
            (model/'registry-entry.json').write_bytes(self.canonical(registry))
            (model/'wild-bet-shadow-candidate-binding.json').write_bytes(self.canonical(binding))
            info = life.model_info(model)
            (root/'day-model-binding.json').write_bytes(self.canonical(self.sealed({
                'schema': 'G11_WILD4_DAY_MODEL_BINDING_V1', 'date': DAY, 'authority': life.AUTHORITY,
                'model': info, 'bound_at': DAY+'T06:00:00+09:00'})))
            (root/'model-registry/lifecycle.json').write_bytes(self.canonical(self.sealed({
                'schema': 'G11_WILD4_LIFECYCLE_V1', 'authority': life.AUTHORITY, 'active': info,
                'last_attempt': None, 'last_completed_night': '2026-10-31'})))
            (root/'odds-input-original.json').write_bytes(
                b'{"immutable":"current rolling input"}\n' if root == self.base
                else b'{"immutable":"earlier original input; never copied"}\n')
        keys = [f'20261101-{venue:02}-{race:02}' for venue in range(1,25) for race in range(1,13)
                if f'20261101-{venue:02}-{race:02}' != MISSING]
        self.common, self.base_keys = keys[:135], keys[:150]
        self.donor_keys = sorted(self.common + [MISSING])
        for root, selected in ((self.base, self.base_keys), (self.donor, self.donor_keys)):
            for key in selected:
                lock = self.sealed({'SCHEMA': 'G11_WILD_BET_FUTURE_OOS_SHADOW_LOCK_V1',
                    'STATUS': 'LOCKED_BEFORE_RESULT', 'STANDARD_RACE_KEY': key,
                    'CANDIDATE_MODEL_SHA256': model_sha, 'CANDIDATE_REGISTRY_SHA256': registry['REGISTRY_SHA256'],
                    'PRODUCTION_BETS_CHANGED': False, 'OFFICIAL_RESULT_USED': False,
                    'PRODUCTION_PROMOTION_ELIGIBLE': False, 'PRODUCTION_MONETARY_ACTION': 'PROHIBITED',
                    'TOP1_FORCED_AS_BET': False, 'SELECTED_BETS': ['1-2-3'], 'BUY_COUNT': 1,
                    'TOP1_DISPLAY': '1-2-3', 'ODDS_SNAPSHOT_SHA256': 'a'*64, 'INPUT_PACKET_SHA256': 'b'*64,
                    'LOCKED_AT': DAY+'T17:02:00+09:00', 'RACE_DEADLINE_AT': DAY+'T17:08:00+09:00',
                    'private_evidence': {'original': key}})
                (root/(key+'.json')).write_bytes(self.canonical(lock))
            manifest = self.sealed({'SCHEMA': 'G11_WILD_BET_FUTURE_OOS_SHADOW_BATCH_V1', 'DATE': DAY,
                'CANDIDATE_MODEL_SHA256': model_sha, 'CANDIDATE_INPUT_SHA256': 'c'*64,
                'PRODUCTION_MUTATED': False, 'ACTUAL_PURCHASE': False, 'RACE_KEYS': sorted(selected),
                'LOCKED_BEFORE_DEADLINE': len(selected), 'SOURCE_ODDS_SHA256': 'e'*64,
                'NEW_LOCKED_THIS_RUN': 7, 'RETAINED_FROM_PRIOR': len(selected)-7})
            (root/'batch-manifest.json').write_bytes(self.canonical(manifest))
        self.expected_sha = json.loads((self.donor/(MISSING+'.json')).read_bytes())['CONTENT_SHA256']
        self.model_sha = model_sha

    def call(self):
        return self.scope['append_original_lock'](self.base, self.donor, DAY,
            {'run_id': 41291307990, 'artifact_id': 13077092759}, expected_lock_sha=self.expected_sha)

    def inventory(self, root):
        return self.scope['inventory'](root)

    def change_record(self, path, **changes):
        value = json.loads(path.read_bytes()); value.update(changes)
        path.write_bytes(self.canonical(self.sealed(value)))

    def assertFailsUnchanged(self, reason):
        before, original = self.inventory(self.base), self.inventory(self.donor)
        with self.assertRaisesRegex(ValueError, reason):
            self.call()
        self.assertEqual(self.inventory(self.base), before)
        self.assertEqual(self.inventory(self.donor), original)

    def test_actual_project_verifies_135_byte_overlap_and_151_union_without_prediction(self):
        before, donor_before = self.inventory(self.base), self.inventory(self.donor)
        manifest = json.loads((self.base/'batch-manifest.json').read_bytes())
        result = self.call()
        self.assertEqual(result, {'restored': [MISSING], 'locks': 151, 'overlap': 135})
        self.assertEqual((self.base/(MISSING+'.json')).read_bytes(), (self.donor/(MISSING+'.json')).read_bytes())
        after = self.inventory(self.base)
        self.assertTrue(all(after[k] == v for k,v in before.items() if k != 'batch-manifest.json'))
        self.assertEqual(self.inventory(self.donor), donor_before)
        saved = json.loads((self.base/'batch-manifest.json').read_bytes())
        for key, value in manifest.items():
            if key not in ('RACE_KEYS','LOCKED_BEFORE_DEADLINE','CONTENT_SHA256'):
                self.assertEqual(saved[key], value)
        self.assertEqual(len(project(self.base, operational_date=DAY, model_sha256=self.model_sha)['races']), 151)
        receipt = json.loads((self.base/'recovery-provenance/20261101-19-02/receipt.json').read_bytes())
        self.assertEqual(receipt['generation_count'], 0)
        self.assertFalse(receipt['settlement_generated_by_union'])
        snapshot = self.inventory(self.base)
        self.assertEqual(self.call(), {'restored': [], 'locks': 151, 'overlap': 136})
        self.assertEqual(self.inventory(self.base), snapshot)

    def test_any_hidden_overlap_field_or_json_byte_change_is_rejected(self):
        path = self.donor/(self.common[0]+'.json')
        original = path.read_bytes()
        self.change_record(path, private_evidence={'original':'changed'})
        self.assertFailsUnchanged('OVERLAP_BYTES')
        path.write_bytes(original + b' ')
        self.assertFailsUnchanged('OVERLAP_BYTES')

    def test_invalid_manifest_or_lock_hash_is_rejected_by_actual_project(self):
        path = self.donor/'batch-manifest.json'; original = path.read_bytes()
        value = json.loads(original); value['CONTENT_SHA256'] = 'f'*64
        path.write_bytes(self.canonical(value)); self.assertFailsUnchanged('MANIFEST_BINDING')
        path.write_bytes(original)
        lock = self.donor/(MISSING+'.json'); value = json.loads(lock.read_bytes()); value['CONTENT_SHA256']='f'*64
        lock.write_bytes(self.canonical(value)); self.assertFailsUnchanged('LOCK_BINDING')

    def test_wrong_donor_lock_or_model_is_rejected_without_mutation(self):
        self.expected_sha = 'f'*64; self.assertFailsUnchanged('ORIGINAL_LOCK_SHA')
        self.expected_sha = json.loads((self.donor/(MISSING+'.json')).read_bytes())['CONTENT_SHA256']
        (self.donor/'model-registry/models'/self.model_sha/'model.cbm').write_bytes(b'changed')
        self.assertFailsUnchanged('CANDIDATE_BINDING')

    def test_previously_started_night_or_any_settlement_is_not_silently_rebased(self):
        state = self.base/'model-registry/lifecycle.json'; original = state.read_bytes()
        for changes in ({'last_completed_night': DAY}, {'last_attempt': {'date':DAY, 'state':'TECHNICAL_HOLD'}},
                        {'night_coverage_retry': {'binding': {'date':DAY}}}):
            self.change_record(state, **changes); self.assertFailsUnchanged('BASE_ALREADY_PROCESSED_NIGHT')
            state.write_bytes(original)
        for root in (self.base, self.donor):
            directory = root/'settlements'; directory.mkdir()
            path = directory/(self.common[0]+'.json'); path.write_text('{}')
            self.assertFailsUnchanged('ALREADY_SETTLED|BASE_ALREADY_PROCESSED_NIGHT')
            path.unlink(); directory.rmdir()

    def test_symlinks_archive_traversal_duplicates_and_special_members_are_rejected(self):
        before = self.inventory(self.base)
        (self.donor/'unsafe').symlink_to(self.base/'batch-manifest.json')
        with self.assertRaisesRegex(ValueError, 'FILE_TYPE'):
            self.call()
        self.assertEqual(self.inventory(self.base), before)
        self.assertTrue((self.donor/'unsafe').is_symlink())
        (self.donor/'unsafe').unlink()
        for name, kind in (('../escape',tarfile.REGTYPE), ('g11-wild4-future-locks/link',tarfile.SYMTYPE),
                           ('g11-wild4-future-locks/device',tarfile.CHRTYPE)):
            archive = self.root/'invalid.tgz'
            with tarfile.open(archive,'w:gz') as out:
                member = tarfile.TarInfo(name); member.type=kind; out.addfile(member)
            with self.assertRaisesRegex(ValueError,'ARCHIVE_PATH'):
                self.scope['extract_original'](archive,self.root/('extract-'+str(kind)))

    def test_existing_settlement_process_appends_151_and_retry_is_idempotent(self):
        self.call(); before = self.inventory(self.base)
        keys = sorted(self.base_keys+[MISSING]); captured = DAY+'T22:45:00+09:00'
        fields = ['標準レースキー','3連単','払戻','返還有無','レース成立状態','取得時刻','公式URL']
        direct = {'SNAPSHOT_SHA256':'8'*64,'SNAPSHOT':{'STAGE':'NIGHT','NAME':'official-day-results',
            'SOURCE_RECEIPTS':[{'SHA256':'7'*64,'CAPTURED_AT_JST':captured}],
            'PAYLOAD':{'OPERATIONAL_DATE_JST':DAY,'STATUS':'NIGHT_RESULTS_COMPLETE','PENDING_RACE_KEYS':[],
                'RESULTS':[{'operational_date':DAY,'venue_code':key[9:11], 'race_no':int(key[-2:]),
                    'trifecta_combination':'1-2-3','trifecta_payout':500,'refund_boats':[],
                    'race_status':'ESTABLISHED'} for key in keys]}}}
        verified = {'SNAPSHOT_SHA256':'9'*64,'SNAPSHOT':{'STAGE':'NIGHT','NAME':'workbook-ledger-projection',
            'PAYLOAD':{'SCHEMA':'G11_CANONICAL_NIGHT_WORKBOOK_PROJECTION_V2',
                'STATUS':'NIGHT_WORKBOOK_PROJECTION_COMPLETE_LEDGER_TRANSFER_PENDING',
                'AGGREGATION_SCOPE':'ALL_RESULTS_FINAL','PENDING_RACE_KEYS':[],'OPERATIONAL_DATE_JST':DAY,
                'SOURCE_BINDINGS':{'NIGHT_RESULTS_SNAPSHOT_SHA256':'8'*64,'OFFICIAL_RESULT_RESPONSE_SHA256':'7'*64},
                'RESULT_RAW16_FIELDS':fields,'RESULT_RAW16_ROWS':[
                    [key,'1-2-3',500,False,'成立',captured,'https://official.example/results'] for key in keys],
                'RESEARCH_EXCLUSION_POLICY':{'SG_CONFIRMED_RACE_KEYS':[]},
                'OFFICIAL_TRIFECTA_SETTLEMENTS':{'MULTI_SETTLEMENT_RACE_KEYS':[]}}}}
        result = settlement.settle_day(self.base, verified, DAY, direct)
        self.assertEqual(result, {'new':151,'total':151,'excluded':0})
        after = self.inventory(self.base)
        self.assertTrue(all(after[k] == v for k,v in before.items()))
        self.assertEqual(set(after)-set(before), {'settlements/'+key+'.json' for key in keys})
        self.assertEqual(settlement.settle_day(self.base,verified,DAY,direct)['new'],0)
        self.assertEqual(self.inventory(self.base),after)
        self.assertEqual(self.call()['restored'],[])
        self.assertEqual(self.inventory(self.base),after)


class PublicUnionComparisonTests(unittest.TestCase):
    def test_all_saved_fields_and_any_prior_settlement_are_preserved(self):
        check = block('PYUNIONPUBLIC')['verify_published_union']
        rows = [{'race_key':f'20261101-{i//12+1:02}-{i%12+1:02}', 'bets':['1-2-3'],
                 'hidden':{'model':'original'}, 'settlement':None} for i in range(151)]
        prior = {'status':'PASS','operational_date_jst':DAY,
            'goripan':{'status':'VERIFIED_CANONICAL_PROJECTION'},'races':[
            {'wild_pack':{'wild4':{'goripan':deepcopy(row)}}} for row in rows[:136]]}
        projection = {'date':DAY,'races':deepcopy(rows)}
        check(prior,projection)
        projection['races'][0]['hidden']['model']='changed'
        with self.assertRaisesRegex(ValueError,'IMMUTABLE'): check(prior,projection)
        projection['races']=deepcopy(rows); projection['races'].pop(0)
        with self.assertRaisesRegex(ValueError,'COVERAGE'): check(prior,projection)
        projection['races']=deepcopy(rows)
        prior['races'][0]['wild_pack']['wild4']['goripan']['settlement']={'original':True}
        with self.assertRaisesRegex(ValueError,'SETTLEMENT'): check(prior,projection)


if __name__ == '__main__':
    unittest.main()
