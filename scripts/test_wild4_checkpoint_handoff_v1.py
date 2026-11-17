"""Early encrypted input delivery; all dispatches and predictions are mocked."""
import ast
import copy
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
from types import SimpleNamespace
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


class SourcePushRoutingTests(unittest.TestCase):
    def expression(self, event, *, message='', mode='', schedule='', title='LIVE-predeadline-2026-11-17', result='success', group=False):
        source=(ROOT/'.github/workflows/g11-morning-odds-footprint-4bet-shadow-v1.yml').read_text()
        if group:
            value=next(l for l in source.splitlines() if l.startswith('  group: ')).split('${{',1)[1].rsplit('}}',1)[0]
        else:
            block=source.split('  wild4-prospective:\n',1)[1]
            value=block.split('    if: >-\n',1)[1].split('    uses:',1)[0]
        gh=SimpleNamespace(repository_id='1367703708',ref='refs/heads/main',repository='tatsuuut/-g11-official-data-relay',event_name=event,
            event=SimpleNamespace(head_commit=SimpleNamespace(message=message),schedule=schedule,
                workflow_run=SimpleNamespace(display_title=title,conclusion=result,head_branch='main',
                    path='.github/workflows/g11-free-runner.yml',head_repository=SimpleNamespace(full_name='tatsuuut/-g11-official-data-relay'))))
        scope={'github':gh,'inputs':SimpleNamespace(mode=mode),'contains':lambda text,part:part in text,'startsWith':lambda text,prefix:text.startswith(prefix)}
        value=re.sub(r'!(?!=)',' not ',' '.join(value.split()).replace('&&',' and ').replace('||',' or '))
        return eval(value,{'__builtins__':{}},scope)

    def test_source_push_cannot_start_an_unkeyed_worker_or_cancel_a_pending_exact_handoff(self):
        self.assertFalse(self.expression('push',message='Merge ordinary transport fix'))
        self.assertEqual(self.expression('push',message='Merge ordinary transport fix',group=True),'source-only')
        self.assertTrue(self.expression('push',message='[goripan-promotion] retained lineage check'))
        self.assertEqual(self.expression('push',message='[goripan-promotion] retained lineage check',group=True),'wild4')
        for message in ('[footprint-storage-check] retained audit','[footprint-storage-check] [goripan-promotion]'):
            self.assertFalse(self.expression('push',message=message))
            self.assertEqual(self.expression('push',message=message,group=True),'footprint')

    def test_normal_exact_handoff_fallback_cron_and_night_routes_stay_available(self):
        for mode in ('wild4','wild4-settle'):
            self.assertTrue(self.expression('workflow_dispatch',mode=mode))
            self.assertEqual(self.expression('workflow_dispatch',mode=mode,group=True),'wild4')
        for schedule in ('1-56/5 0-12 * * *','25,40,55 13 * * *'):
            self.assertTrue(self.expression('schedule',schedule=schedule))
        self.assertTrue(self.expression('workflow_run'))
        self.assertFalse(self.expression('workflow_run',result='failure'))
        self.assertFalse(self.expression('workflow_dispatch',mode='capture'))


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
            expression = condition.replace('inputs.wild4-day',repr(DAY)).replace('steps.verified.outputs.ready',repr(ready)).replace('steps.wild4-visible.outputs.cache-hit',repr(visible)).replace('steps.wild4-compact.outputs.attempted',repr(''))
            self.assertEqual(eval(expression.replace('&&',' and '), {'__builtins__':{}}, {}),expected)
            self.assertFalse(eval(expression.replace("'' != 'true'", "'true' != 'true'").replace('&&',' and '), {'__builtins__':{}}, {}))
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


class CompactDispatchTests(unittest.TestCase):
    def exercise(self, *, corrupt=False, status='PASS', count=1, dispatch_status=0, prewarm=''):
        import encrypt_runtime_state_v1 as archive
        action=(ROOT/'.github/actions/g11-publication-checkpoint/action.yml').read_text()
        block=action.split('    - name: Start WILD4 from a verified full reconstruction before large backup transfer\n',1)[1].split('    - name:',1)[0]
        script=textwrap.dedent(block.split('      run: |\n',1)[1])
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);out=root/'.relay-output';out.mkdir();(root/'scripts').mkdir()
            (root/'scripts/encrypt_runtime_state_v1.py').write_text((ROOT/'scripts/encrypt_runtime_state_v1.py').read_text())
            cipher=out/'early-delta.tgz.gpg';cipher.write_bytes(b'complete authenticated compact checkpoint')
            delta_key='g11-state-v4-delta-'+DAY+'-123'
            archive.atomic_json(out/'early-delta-ready.json',{'day':DAY,'run_id':'123','checkpoint_key':KEY,
                'delta_key':delta_key,'base_key':'g11-state-v4-published-'+DAY+'-122-1-checkpoint','cipher_sha256':archive.file_sha(cipher)})
            (out/'result.json').write_text(json.dumps({'CRITICAL':{'CRITICAL_CHECKPOINT_STATUS':status,'LAST_TIMELY_SOURCE_COUNT':count}}))
            if corrupt:cipher.write_bytes(b'corrupt remote readback')
            output=root/'output';calls=root/'calls'
            env={**os.environ,'GITHUB_OUTPUT':str(output),'TEST_CALLS':str(calls),'GITHUB_RUN_ID':'123',
                'OPERATIONAL_DATE':DAY,'CHECKPOINT_KEY':KEY,'DELTA_KEY':delta_key,'TEST_STATUS':str(dispatch_status),
                'PREWARM_STATUS':prewarm}
            stub='gh() { printf "%s\\n" "$@" >> "$TEST_CALLS"; return "$TEST_STATUS"; }\n timeout() { shift 3; "$@"; }\n'
            result=subprocess.run(['bash','-c',stub+script],cwd=root,env=env,text=True,capture_output=True,timeout=10)
            return result,output.read_text() if output.exists() else '',calls.read_text() if calls.exists() else ''

    def test_full_reconstruction_can_start_before_large_backup_but_canonical_gate_is_unchanged(self):
        result,output,calls=self.exercise()
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(output,'attempted=true\nsent=true\n')
        self.assertIn('predeadline_checkpoint_key='+KEY,calls);self.assertIn('predeadline_delta_key=g11-state-v4-delta-'+DAY+'-123',calls)
        action=(ROOT/'.github/actions/g11-publication-checkpoint/action.yml').read_text()
        self.assertLess(action.index('id: compact-readback'),action.index('id: wild4-compact'))
        self.assertLess(action.index('id: wild4-compact'),action.index('name: Save complete encrypted state before publication'))
        self.assertIn('value: ${{ steps.verified.outputs.ready }}',action)

    def test_corrupt_empty_and_nonpass_inputs_never_dispatch(self):
        for kwargs in ({'corrupt':True},{'status':'PARTIAL'},{'count':0}):
            with self.subTest(kwargs=kwargs):
                result,output,calls=self.exercise(**kwargs)
                self.assertNotEqual(result.returncode,0);self.assertEqual(output,'');self.assertEqual(calls,'')

    def test_ambiguous_compact_dispatch_is_retained_and_blocks_both_full_fallback_dispatches(self):
        result,output,calls=self.exercise(dispatch_status=1)
        self.assertNotEqual(result.returncode,0);self.assertEqual(output,'attempted=true\n');self.assertEqual(calls.splitlines().count('workflow'),1)
        action=(ROOT/'.github/actions/g11-publication-checkpoint/action.yml').read_text()
        for name in ('Start independently verified WILD4 worker after checkpoint save','Start WILD4 after verified delayed checkpoint visibility'):
            block=action.split('    - name: '+name+'\n',1)[1].split('    - name:',1)[0]
            self.assertIn("steps.wild4-compact.outputs.attempted != 'true'",block)

    def test_prewarmed_compact_handoff_is_not_dispatched_twice_and_still_checks_input_integrity(self):
        for prewarm,expected in [('sent','attempted=true\nsent=true\n'),('attempted','attempted=true\n')]:
            result,output,calls=self.exercise(prewarm=prewarm)
            self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(output,expected);self.assertEqual(calls,'')
            for kwargs in ({'corrupt':True},{'count':0},{'status':'PARTIAL'}):
                result,output,calls=self.exercise(prewarm=prewarm,**kwargs)
                self.assertNotEqual(result.returncode,0);self.assertEqual(output,'');self.assertEqual(calls,'')


@unittest.skipUnless(os.environ.get('WILD4_COMPACT_WORKFLOW_PATH'), 'exact compact child is provided by CI')
class CurrentCompactWorkflowTests(unittest.TestCase):
    def source(self):
        return Path(os.environ['WILD4_COMPACT_WORKFLOW_PATH']).read_text()

    def test_current_deployed_prediction_learning_and_settlement_bytes_are_unchanged(self):
        # Exact deployed 2b6f5719 baseline. Historical guard hashes stay separate.
        expected={'Carry previous-day model registry without copying previous predictions':'08b4d4c9cec584c0d4328fd758cf20c2c1e1758613239bfa836cd5cc94689f02',
            'Bind one model for the operational day':'988e96549a566fa5bcbdfce254381445386d56a5e17a02da61743b62ffb49db6',
            'Verify frozen WILD4 inference runtime':'4495877bd12adea8c5d209ed488a85122892ef0858bf8161cda0d4340c6d0202',
            'Check for a matching unexpired official odds packet':'455bd16a042ba59030bff53f69ad844e74b1e57a43352a55eb9c5e2ff117ed7a',
            'Lock future races before deadline without any production write':'4c4fa5c0dec49e4fb47d4322bee731ab7ad8c503d659ee778db5abd13bffb4a6',
            'Settle only immutable locks from verified final NIGHT':'80f4df9e28fada9f0fa810948d3edc943060da57e0e2a52bdd410c08147b7bd4',
            'Continue existing BET learning after completed NIGHT':'13d0c5f33151da4f614e685da3d962974730dcf62bc03a73a47efe980aa07b6d'}
        for name,digest in expected.items():
            block=self.source().split('      - name: '+name+'\n',1)[1].split('      - name:',1)[0]
            if name == 'Bind one model for the operational day':
                # Only the two saved day reads use the small, source-verified
                # transport. Every model/eligibility statement stays identical.
                self.assertEqual(block.count('?view=wild4-day-binding&date='), 2)
                block=block.replace('?view=wild4-day-binding&date=', '?date=')
                guard="if day>='2026-10-13' and not (temp/'g11-wild4-future-locks/day-model-binding.json').is_file():"
                self.assertEqual(block.count(guard),1)
                block=block.replace(guard,"if day>='2026-10-13':")
            with self.subTest(name=name):self.assertEqual(hashlib.sha256(block.encode()).hexdigest(),digest)

    def test_complete_authenticated_reconstruction_precedes_original_full_store_guard(self):
        source=self.source()
        authenticate=source.split('      - name: Authenticate compact manifest before choosing its exact base\n',1)[1].split('      - name:',1)[0]
        self.assertLess(authenticate.index('--decrypt'),authenticate.index('value=inspect_delta('));self.assertIn('set -euo pipefail',authenticate)
        self.assertIn("steps.compact-cache.outputs.cache-hit == 'true' || steps.compact-cache-retry.outputs.cache-hit == 'true' }}' = true",authenticate)
        restore=source.split('      - name: Restore current encrypted canonical state\n',1)[1].split('      - name:',1)[0]
        self.assertIn('key: ${{ steps.compact.outputs.base_key || inputs.predeadline_checkpoint_key',restore)
        decrypt=source.split('      - name: Decrypt verified state and candidate\n',1)[1].split('      - name:',1)[0]
        self.assertLess(decrypt.index('extract_complete_state('),decrypt.index('apply_delta('))
        self.assertLess(decrypt.index('apply_delta('),decrypt.index("state=json.loads("))
        self.assertIn("base_cipher=pathlib.Path('.relay-cache/state.tgz.gpg')",decrypt)
        self.assertIn("critical.get('CRITICAL_CHECKPOINT_SHA')",decrypt)
        self.assertIn("critical.get('AUTHORITY_SHA256')",decrypt)

    def test_exact_cache_restore_precedes_optional_metadata_wait_and_never_uses_prefix_fallback(self):
        source=self.source()
        self.assertLess(source.index('id: compact-cache\n'),source.index('id: handoff\n'))
        wait=source.split('      - name: Wait only for the exact new saved handoff after preparation\n',1)[1].split('      - name:',1)[0]
        self.assertIn("steps.compact-cache.outputs.cache-hit != 'true'",wait)
        for name in ('Restore exact authenticated complete reconstruction delta','Restore the exact handoff after bounded visibility wait'):
            block=source.split('      - name: '+name+'\n',1)[1].split('      - name:',1)[0]
            self.assertIn('key: ${{ inputs.predeadline_delta_key }}',block)
            self.assertNotIn('restore-keys:',block)

    def test_executable_child_binding_rejects_delta_without_exact_current_capture(self):
        source=self.source();start=source.index('          live_checkpoint=');end=source.index('          with open(os.environ[\'GITHUB_OUTPUT\']',start)
        code=textwrap.dedent(source[start:end])
        for live,delta,ok in [(KEY,KEY.replace('critical-','delta-'),True),(KEY,'',True),
                ('',KEY.replace('critical-','delta-'),False),(KEY,KEY.replace('critical-','delta-')+'4',False),
                (KEY,KEY.replace(DAY,'2026-10-23').replace('critical-','delta-'),False)]:
            env={'PREDEADLINE_CHECKPOINT_KEY':live,'PREDEADLINE_DELTA_KEY':delta,'REQUESTED_MODE':'wild4'}
            with self.subTest(live=live,delta=delta),patch.dict(os.environ,env):
                scope={'os':os,'re':__import__('re'),'day':DAY,'checkpoint':'','readback':''}
                if ok:exec(compile(code,'<exact-compact-binding>','exec'),scope)
                else:
                    with self.assertRaises(SystemExit):exec(compile(code,'<exact-compact-binding>','exec'),scope)

    def test_prepared_base_cannot_open_inference_without_new_authenticated_exact_handoff(self):
        source=self.source()
        block=source.split('      - name: Verify current canonical state availability\n',1)[1].split('      - name:',1)[0]
        script=textwrap.dedent(block.split('        run: |\n',1)[1])
        for warm,base,reuse,hit,ready,ok in [('base','','','',False,True),
                ('base','verified-base','true','',True,True),
                ('base','verified-base','false','',False,False),
                ('','','','',False,False)]:
            values={'inputs.prewarm_base_key':warm,'steps.compact.outputs.base_key':base,
                'steps.compact.outputs.use_prewarm':reuse,'steps.canonical-cache.outputs.cache-hit':hit,
                'inputs.predeadline_checkpoint_key || inputs.night_checkpoint_key':KEY}
            code=script
            for key,value in values.items():code=code.replace('${{ '+key+' }}',value)
            with tempfile.TemporaryDirectory() as tmp:
                output=Path(tmp)/'outputs'
                result=subprocess.run(['bash','-e','-c',code],cwd=tmp,env={**os.environ,'GITHUB_OUTPUT':str(output)},capture_output=True,text=True)
                self.assertEqual(result.returncode==0,ok,result.stderr)
                self.assertEqual('ready=true' in (output.read_text() if output.exists() else ''),ready)
        decrypt=source.split('      - name: Decrypt verified state and candidate\n',1)[1].split('      - name:',1)[0]
        self.assertIn("os.replace(root/'g11-prewarm-cipher.tgz.gpg'",decrypt)
        self.assertLess(decrypt.index("os.rename(root/'g11-prewarm-base'"),decrypt.index('apply_delta('))
        parent=(ROOT/'.github/workflows/g11-free-runner.yml').read_text()
        self.assertLess(parent.index('id: prewarm'),parent.index('name: Decrypt and validate restored state'))
        self.assertIn('prewarm: ${{steps.prewarm.outputs.status}}',parent)


@unittest.skipUnless(os.environ.get('WILD4_READBACK_WORKFLOW_PATH'), 'exact publisher workflow is provided by CI')
class SavedReadbackTransportTests(unittest.TestCase):
    def exercise(self, change=None, scope='WILD4_SAVED_PROJECTION', bad_sha=False, failures=()):
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
        self.fetch_attempts=[];self.retry_sleeps=[]
        def fetch(url):
            parsed=urlparse(url);query=parse_qs(parsed.query)
            self.assertEqual(parsed.path,'/api/g11-feed');self.assertEqual(query,{'view':['wild4-readback'],'date':['2026-11-17']})
            attempt=len(self.fetch_attempts);self.fetch_attempts.append(url)
            if attempt<len(failures):
                Path('.relay-output/hot-get-headers.txt').write_text('HTTP/2 '+str(failures[attempt])+'\n')
                raise subprocess.CalledProcessError(22,['curl'])
            return feed,headers,raw
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=root/'goripan-public-projection.json';p.write_text(json.dumps(projection));before=p.read_bytes()
            prior=Path.cwd();output=io.StringIO()
            try:
                os.chdir(root)
                with patch.dict(os.environ,{'RUNNER_TEMP':str(root),'OPERATIONAL_DATE':'2026-11-17'}),patch.dict(sys.modules,{'hot_delta_transport_v1':type('ReadOnlyTransport',(),{'fetch':staticmethod(fetch)})}),patch('time.sleep',side_effect=self.retry_sleeps.append),redirect_stdout(output):
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

    def test_transient_saved_get_failure_retries_reads_but_still_requires_exact_bytes(self):
        self.assertIn('GORIPAN_SITE_GET_READBACK=PASS',self.exercise(failures=(500,503)))
        self.assertEqual(len(self.fetch_attempts),3);self.assertEqual(self.retry_sleeps,[3,10])
        with self.assertRaisesRegex(SystemExit,'SITE_GET_SHA'):self.exercise(failures=(500,),bad_sha=True)
        self.assertEqual(len(self.fetch_attempts),2)

    def test_auth_failure_or_persistent_overload_cannot_become_success(self):
        with self.assertRaises(subprocess.CalledProcessError):self.exercise(failures=(401,))
        self.assertEqual(len(self.fetch_attempts),1);self.assertEqual(self.retry_sleeps,[])
        with self.assertRaises(subprocess.CalledProcessError):self.exercise(failures=(500,503,500))
        self.assertEqual(len(self.fetch_attempts),3);self.assertEqual(self.retry_sleeps,[3,10])

    def test_slow_first_failure_exhausts_retry_window_without_more_requests(self):
        with patch('time.monotonic',side_effect=[0,25]),self.assertRaises(subprocess.CalledProcessError):
            self.exercise(failures=(500,))
        self.assertEqual(len(self.fetch_attempts),1);self.assertEqual(self.retry_sleeps,[])


class ProtectedLogicTests(unittest.TestCase):
    def test_model_inference_learning_and_settlement_blocks_are_unchanged(self):
        source = WORKFLOW.read_text()
        expected = {'Carry previous-day model registry without copying previous predictions': 'cdfe9943acc632fc699ed539c4becc85db7d140997c9ce1b356fc412e6969ab5', 'Bind one model for the operational day': '6b21757a328b8f303adf5ed700f8c663dd49e2dfcbfc79ad3769de62a936d67f', 'Verify frozen WILD4 inference runtime': '4495877bd12adea8c5d209ed488a85122892ef0858bf8161cda0d4340c6d0202', 'Check for a matching unexpired official odds packet': 'e3e74afcbc78bcbf78ab82035aab7537b036e466a6ccd819d014f93a4cf110e3', 'Lock future races before deadline without any production write': '297200df10c01e1c6417db3e4f3dbc4a8accb0bfee7c7286c9a86f502eaee8d1', 'Settle only immutable locks from verified final NIGHT': '03695c52becd2dcbd700f7dcf8d763442b280356d1ef17fd619aa574923801a3', 'Continue existing BET learning after completed NIGHT': '797e01d03c64314e9b7a7ba313323e424ed85ffd033e623cb9fefe829534e095'}
        for name, digest in expected.items():
            block = source.split("      - name: " + name + "\n", 1)[1].split("      - name: ", 1)[0]
            with self.subTest(step=name):
                self.assertEqual(hashlib.sha256(block.encode()).hexdigest(), digest)

