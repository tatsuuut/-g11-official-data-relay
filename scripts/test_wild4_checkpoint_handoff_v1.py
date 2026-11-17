"""Early encrypted input delivery; all dispatches and predictions are mocked."""
import ast
import copy
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = Path(os.environ.get('WILD4_WORKFLOW_PATH', '/tmp/g11-wild4-handoff.yml'))
DAY = '2026-10-24'
KEY = 'g11-state-v4-critical-' + DAY + '-123'


def digest_tree(root):
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*')):
        if path.is_file():
            name = path.relative_to(root).as_posix().encode()
            digest.update(len(name).to_bytes(8, 'big'))
            digest.update(name)
            digest.update(path.read_bytes())
    return digest.hexdigest()


class CheckpointBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        store = self.root / 'store'
        store.mkdir()
        (store / 'morning.json').write_text('immutable morning')
        (store / 'odds.json').write_text('verified odds captured before deadline')
        authority = self.root / 'daily-runtime' / DAY / 'authority.json'
        authority.parent.mkdir(parents=True)
        authority.write_text(json.dumps({'operational_date_jst': DAY, 'immutable': True,
                                        'authority_sha256': 'a' * 64, 'runtime_source_sha': 'b' * 40}))
        self.state = {'OPERATIONAL_DATE_JST': DAY, 'MORNING_COMPLETE': True,
                      'CRITICAL': {'SCHEMA': 'G11_PREDEADLINE_CRITICAL_CHECKPOINT_V1',
                                   'CRITICAL_CHECKPOINT_STATUS': 'PASS', 'OPERATIONAL_DATE': DAY,
                                   'LAST_CRITICAL_RUN_ID': '123', 'POST_DEADLINE_GENERATION': 0,
                                   'CRITICAL_CHECKPOINT_SHA': digest_tree(store),
                                   'AUTHORITY_SHA256': 'a' * 64, 'RUNTIME_SOURCE_SHA': 'b' * 40}}
        workflow = WORKFLOW.read_text()
        fragment = workflow[workflow.index('          def verify_early_checkpoint('):
                            workflow.index('          verify_early_checkpoint(pathlib.Path')]
        scope = {'hashlib': hashlib, 'json': json}
        exec(compile(ast.parse(textwrap.dedent(fragment)), '<deployed-checkpoint-guard>', 'exec'), scope)
        self.verify = scope['verify_early_checkpoint']

    def test_same_verified_input_is_read_only_before_publication(self):
        before = {p.relative_to(self.root).as_posix(): p.read_bytes()
                  for p in self.root.rglob('*') if p.is_file()}
        original = copy.deepcopy(self.state)
        self.verify(self.root, self.state, DAY, KEY)
        self.assertEqual(self.state, original)
        self.assertEqual(before, {p.relative_to(self.root).as_posix(): p.read_bytes()
                                 for p in self.root.rglob('*') if p.is_file()})

    def test_wrong_run_day_or_unsafe_state_cannot_fall_back_to_stale_input(self):
        for key in (KEY + '4', KEY.replace(DAY, '2026-10-23'), 'g11-state-v4-published-' + DAY + '-123'):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'BINDING'):
                self.verify(self.root, self.state, DAY, key)
        for field, value in [('CRITICAL_CHECKPOINT_STATUS', 'PARTIAL'), ('POST_DEADLINE_GENERATION', 1),
                             ('OPERATIONAL_DATE', '2026-10-23')]:
            state = copy.deepcopy(self.state); state['CRITICAL'][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'BINDING'):
                self.verify(self.root, state, DAY, KEY)
        self.state['MORNING_COMPLETE'] = False
        with self.assertRaisesRegex(ValueError, 'BINDING'):
            self.verify(self.root, self.state, DAY, KEY)

    def test_modified_saved_input_or_daily_source_is_rejected(self):
        (self.root / 'store/odds.json').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'HASH'):
            self.verify(self.root, self.state, DAY, KEY)
        self.state['CRITICAL']['CRITICAL_CHECKPOINT_SHA'] = digest_tree(self.root / 'store')
        self.state['CRITICAL']['RUNTIME_SOURCE_SHA'] = 'c' * 40
        with self.assertRaisesRegex(ValueError, 'AUTHORITY'):
            self.verify(self.root, self.state, DAY, KEY)

    def test_existing_night_and_rescue_without_early_key_are_unchanged(self):
        self.verify(self.root, {}, DAY, '')


class EarlyDispatchTests(unittest.TestCase):
    def exercise(self, count, dispatch_status=0, early_attempted=None, early_sent=''):
        if early_attempted is None:
            return OverlappedCheckpointDispatchTests().exercise(count=count, dispatch_status=dispatch_status)
        workflow = (ROOT / '.github/workflows/g11-free-runner.yml').read_text()
        step = workflow.split('        id: wild4-early\n', 1)[1].split(
            '      - name:', 1)[0]
        script = textwrap.dedent(step.split('        run: |\n', 1)[1])
        script = script.replace('${{steps.critical-save.outputs.wild4-attempted}}', early_attempted).replace(
            '${{steps.critical-save.outputs.wild4-sent}}', early_sent)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / '.relay-output').mkdir()
            (root / '.relay-output/result.json').write_text(json.dumps({'CRITICAL': {
                'CRITICAL_CHECKPOINT_STATUS': 'PASS', 'LAST_TIMELY_SOURCE_COUNT': count}}))
            output = root / 'output'; calls = root / 'calls'
            env = dict(os.environ, OPERATIONAL_DATE=DAY, GITHUB_RUN_ID='123',
                       GITHUB_OUTPUT=str(output), TEST_CALLS=str(calls),
                       TEST_STATUS=str(dispatch_status))
            stub = 'gh() { printf "%s\\n" "$@" >> "$TEST_CALLS"; return "$TEST_STATUS"; }\n'
            result = subprocess.run(['bash', '-c', stub + script], cwd=root, env=env,
                                    text=True, capture_output=True, timeout=10)
            return result, output.read_text() if output.exists() else '', calls.read_text() if calls.exists() else ''

    def test_new_capture_dispatches_exact_checkpoint_before_optional_work(self):
        result, output, calls = self.exercise(2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('sent=true', output)
        self.assertIn('predeadline_checkpoint_key=' + KEY, calls)
        workflow = (ROOT / '.github/workflows/g11-free-runner.yml').read_text()
        self.assertLess(workflow.index('id: critical-save'), workflow.index('id: wild4-early'))
        self.assertLess(workflow.index('id: wild4-early'), workflow.index('id: predeadline-chain'))

    def test_no_new_input_does_not_create_empty_observer_job(self):
        result, output, calls = self.exercise(0)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls, '')
        self.assertNotIn('sent=true', output)

    def test_failed_dispatch_does_not_claim_handoff_or_retry_ambiguous_post(self):
        result, output, calls = self.exercise(2, 1)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('sent=true', output)
        self.assertEqual(calls.splitlines().count('workflow'), 1)

    def test_published_dispatch_is_skipped_only_after_accepted_early_handoff(self):
        workflow = (ROOT / '.github/workflows/g11-free-runner.yml').read_text()
        line = next(line for line in workflow.splitlines() if 'WILD4_DISPATCH_RETRY' in line)
        for sent, followup, needed, phase, expected in [
                ('true', '', '', 'predeadline', False), ('', '', '', 'predeadline', True),
                ('true', '', 'true', 'predeadline', True),
                ('', '', 'false', 'predeadline', False),
                ('', '', 'true', 'predeadline', True),
                ('true', 'true', 'true', 'predeadline', False),
                ('', 'true', 'true', 'predeadline', False), ('', '', '', 'night', False)]:
            script = line.replace('${{steps.wild4-early.outputs.sent}}', sent).replace(
                '${{steps.wild4-followup.outputs.sent}}', followup).replace(
                '${{steps.wild4-followup.outputs.needed}}', needed).replace(
                '${{steps.due.outputs.phase}}', phase).replace('${{github.token}}', 'test').replace(
                '${{steps.due.outputs.operational_date}}', DAY)
            result = subprocess.run(['bash', '-c', 'gh() { echo DISPATCH; }; ' + script],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual('DISPATCH' in result.stdout, expected)

    def test_normal_and_night_keys_still_use_existing_fallback_contract(self):
        source = WORKFLOW.read_text()
        self.assertIn("inputs.predeadline_checkpoint_key == '' && inputs.night_checkpoint_key == ''", source)
        self.assertIn("inputs.predeadline_checkpoint_key || inputs.night_checkpoint_key || format(", source)
        self.assertIn("steps.canonical-cache.outputs.cache-hit }}' != true", source)


    def test_accepted_overlap_is_propagated_without_another_dispatch(self):
        result, output, calls = self.exercise(2, early_attempted='true', early_sent='true')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('sent=true', output)
        self.assertEqual(calls, '')

    def test_ambiguous_overlap_is_not_reposted_immediately(self):
        result, output, calls = self.exercise(2, early_attempted='true')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('sent=true', output)
        self.assertEqual(calls, '')


class OverlappedCheckpointDispatchTests(unittest.TestCase):
    def exercise(self, count=2, status='PASS', key=KEY, dispatch_status=0, corrupt=False, fallback=False):
        action = (ROOT / '.github/actions/g11-publication-checkpoint/action.yml').read_text()
        name = 'Start WILD4 after verified delayed checkpoint visibility' if fallback else 'Start independently verified WILD4 worker after checkpoint save'
        block = action.split('    - name: '+name+'\n', 1)[1].split('    - name:', 1)[0]
        code = textwrap.dedent(block.split('      run: |\n', 1)[1])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.relay-cache').mkdir(); (root / '.relay-output').mkdir(); (root / 'bin').mkdir()
            archive = root / '.relay-cache/state.tgz.gpg'
            original = b'complete authenticated encrypted state'
            archive.write_bytes(b'corrupt' if corrupt else original)
            (root / '.relay-output/publication-state.sha256').write_text(
                hashlib.sha256(original).hexdigest() + '  .relay-cache/state.tgz.gpg\n')
            (root / '.relay-output/result.json').write_text(json.dumps({'CRITICAL': {
                'CRITICAL_CHECKPOINT_STATUS': status, 'LAST_TIMELY_SOURCE_COUNT': count}}))
            gh = root / 'bin/gh'
            gh.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$TEST_CALLS"\nexit "$TEST_STATUS"\n')
            gh.chmod(0o755)
            output, calls = root / 'output', root / 'calls'
            env = dict(os.environ, PATH=str(root / 'bin') + os.pathsep + os.environ['PATH'],
                OPERATIONAL_DATE=DAY, CHECKPOINT_KEY=key, GITHUB_RUN_ID='123', GITHUB_OUTPUT=str(output),
                TEST_CALLS=str(calls), TEST_STATUS=str(dispatch_status), DISPATCH_STAGE='AFTER_DELAYED_VISIBILITY' if fallback else 'DURING_READBACK')
            result = subprocess.run(['bash', '-c', code], cwd=root, env=env, capture_output=True, text=True, timeout=15)
            return result, output.read_text() if output.exists() else '', calls.read_text() if calls.exists() else ''

    def test_exact_saved_input_can_prepare_child_before_parent_readback_without_opening_publication(self):
        result, output, calls = self.exercise()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, 'attempted=true\nsent=true\n')
        self.assertIn('predeadline_checkpoint_key=' + KEY, calls)
        action = (ROOT / '.github/actions/g11-publication-checkpoint/action.yml').read_text()
        self.assertLess(action.index('name: Save complete encrypted state'), action.index('id: wild4-dispatch'))
        self.assertLess(action.index('id: wild4-dispatch'), action.index('id: readback\n'))
        self.assertIn('value: ${{ steps.verified.outputs.ready }}', action)

    def test_empty_capture_never_launches_an_observer(self):
        result, output, calls = self.exercise(count=0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, ''); self.assertEqual(calls, '')

    def test_bad_checkpoint_or_capture_stops_before_dispatch(self):
        for kwargs in ({'key': KEY+'4'}, {'key': KEY.replace('critical', 'published')},
                       {'status': 'PARTIAL'}, {'corrupt': True}):
            with self.subTest(kwargs=kwargs):
                result, output, calls = self.exercise(**kwargs)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(output, ''); self.assertEqual(calls, '')

    def test_failed_dispatch_retains_attempt_without_claiming_acceptance(self):
        result, output, calls = self.exercise(dispatch_status=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output, 'attempted=true\n')
        self.assertEqual(calls.splitlines().count('workflow'), 1)

    def test_cache_visibility_probe_is_exact_and_does_not_download_or_use_prefix_fallback(self):
        action = (ROOT / '.github/actions/g11-publication-checkpoint/action.yml').read_text()
        probe = action.split('    - name: Check exact saved checkpoint visibility without downloading\n', 1)[1].split('    - name:', 1)[0]
        self.assertIn('lookup-only: true', probe); self.assertNotIn('restore-keys:', probe)
        self.assertIn('key: ${{ inputs.cache-key }}', probe)
        early = action.split('    - name: Start independently verified WILD4 worker after checkpoint save\n', 1)[1].split('    - name:', 1)[0]
        self.assertIn("steps.wild4-visible.outputs.cache-hit == 'true'", early)

    def test_delayed_visibility_falls_back_only_after_successful_full_readback(self):
        action = (ROOT / '.github/actions/g11-publication-checkpoint/action.yml').read_text()
        block = action.split('    - name: Start WILD4 after verified delayed checkpoint visibility\n', 1)[1]
        condition = next(line.split('if: ',1)[1] for line in block.splitlines() if '      if: ' in line)
        for ready, visible, expected in [('', '', False), ('true', 'true', False), ('true', '', True)]:
            expression = condition.replace('inputs.wild4-day',repr(DAY)).replace('steps.verified.outputs.ready',repr(ready)).replace('steps.wild4-visible.outputs.cache-hit',repr(visible))
            self.assertEqual(eval(expression.replace('&&',' and '), {'__builtins__':{}}, {}),expected)
        result, output, calls = self.exercise(fallback=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, 'attempted=true\nsent=true\n')
        self.assertIn('predeadline_checkpoint_key='+KEY,calls)
        self.assertIn('AFTER_DELAYED_VISIBILITY', result.stdout)

    def test_delayed_visibility_preserves_bad_key_and_corrupt_state_rejection(self):
        for kwargs in ({'key':KEY+'4'},{'corrupt':True},{'status':'PARTIAL'}):
            with self.subTest(kwargs=kwargs):
                result, output, calls = self.exercise(fallback=True, **kwargs)
                self.assertNotEqual(result.returncode,0);self.assertEqual(output,'');self.assertEqual(calls,'')


@unittest.skipUnless(os.environ.get('WILD4_READBACK_WORKFLOW_PATH'), 'exact publisher workflow is provided by CI')
class SavedReadbackTransportTests(unittest.TestCase):
    def exercise(self, change=None, scope='WILD4_SAVED_PROJECTION', bad_sha=False):
        workflow = Path(os.environ['WILD4_READBACK_WORKFLOW_PATH']).read_text()
        block = workflow.split('      - name: Verify exact Site GET after priority notification input\n',1)[1].split('      - name:',1)[0]
        code = block.split("          python3 - <<'PY'\n",1)[1].split('\n          PY',1)[0]
        code = textwrap.dedent(code).replace('${{ steps.decrypted.outputs.canonical_ready }}','true')
        lock={'race_key':'20261117-23-04','bets':['1-2-3'],'bet_count':1,'abstain':False,'lock_sha256':'a'*64}
        lifecycle={'active':{'identity':'same-immutable-model'},'source_locks_mutated':False}
        projection={'races':[lock],'observation':{'lifecycle':lifecycle}}
        feed={'races':[{'key':lock['race_key'],'wild_pack':{'wild4':{'goripan':copy.deepcopy(lock)}}}],
              'goripan':{'status':'VERIFIED_CANONICAL_PROJECTION','observation':{'lifecycle':copy.deepcopy(lifecycle)}}}
        if change:change(feed)
        raw=json.dumps(feed,ensure_ascii=False).encode()
        headers={'x-g11-feed-sha256':'f'*64 if bad_sha else hashlib.sha256(raw).hexdigest(),'x-g11-readback-scope':scope}
        def fetch(url):
            parsed=urlparse(url);query=parse_qs(parsed.query)
            self.assertEqual(parsed.path,'/api/g11-feed');self.assertEqual(query,{'view':['wild4-readback'],'date':['2026-11-17']})
            return feed,headers,raw
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=root/'goripan-public-projection.json';p.write_text(json.dumps(projection));before=p.read_bytes()
            prior=Path.cwd();output=io.StringIO()
            try:
                os.chdir(root)
                with patch.dict(os.environ,{'RUNNER_TEMP':str(root),'OPERATIONAL_DATE':'2026-11-17'}),patch.dict(sys.modules,{'hot_delta_transport_v1':type('ReadOnlyTransport',(),{'fetch':staticmethod(fetch)})}),redirect_stdout(output):
                    exec(compile(code,'<exact-saved-readback-verifier>','exec'),{})
                self.assertEqual(p.read_bytes(),before)
                return output.getvalue()
            finally:os.chdir(prior)

    def test_saved_projection_and_lifecycle_verify_without_full_research_view(self):
        self.assertIn('GORIPAN_SITE_GET_READBACK=PASS',self.exercise())

    def test_scope_bytes_locks_and_lifecycle_all_remain_required(self):
        cases=[({'scope':'UNSCOPED'},'SITE_READBACK_SCOPE'),({'bad_sha':True},'SITE_GET_SHA'),
          ({'change':lambda f:f['goripan'].update(status='PROJECTION_UNAVAILABLE')},'SITE_PROJECTION_STATUS'),
          ({'change':lambda f:f['races'][0]['wild_pack']['wild4']['goripan'].update(bets=['6-5-4'])},'SITE_LOCK_READBACK_MISMATCH'),
          ({'change':lambda f:f['goripan']['observation'].update(lifecycle={})},'SITE_LIFECYCLE_READBACK_MISMATCH')]
        for kwargs,reason in cases:
            with self.subTest(reason=reason),self.assertRaisesRegex(SystemExit,reason):self.exercise(**kwargs)


class ProtectedLogicTests(unittest.TestCase):
    def test_model_inference_learning_and_settlement_blocks_are_unchanged(self):
        source = WORKFLOW.read_text()
        expected = {'Carry previous-day model registry without copying previous predictions': 'cdfe9943acc632fc699ed539c4becc85db7d140997c9ce1b356fc412e6969ab5', 'Bind one model for the operational day': '6b21757a328b8f303adf5ed700f8c663dd49e2dfcbfc79ad3769de62a936d67f', 'Verify frozen WILD4 inference runtime': '4495877bd12adea8c5d209ed488a85122892ef0858bf8161cda0d4340c6d0202', 'Check for a matching unexpired official odds packet': 'e3e74afcbc78bcbf78ab82035aab7537b036e466a6ccd819d014f93a4cf110e3', 'Lock future races before deadline without any production write': '297200df10c01e1c6417db3e4f3dbc4a8accb0bfee7c7286c9a86f502eaee8d1', 'Settle only immutable locks from verified final NIGHT': '03695c52becd2dcbd700f7dcf8d763442b280356d1ef17fd619aa574923801a3', 'Continue existing BET learning after completed NIGHT': '797e01d03c64314e9b7a7ba313323e424ed85ffd033e623cb9fefe829534e095'}
        for name, digest in expected.items():
            block = source.split("      - name: " + name + "\n", 1)[1].split("      - name: ", 1)[0]
            with self.subTest(step=name):
                self.assertEqual(hashlib.sha256(block.encode()).hexdigest(), digest)

