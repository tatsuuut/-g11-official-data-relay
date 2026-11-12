from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import tarfile
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import morning_footprint_persistence_v1 as P

DAY = '2026-11-12'


def feed():
    return {'status': 'PASS', 'operational_date_jst': DAY,
            'races': [{'key': f'20261112-01-0{r}', 'deadline_jst': f'{DAY}T09:00:00+09:00',
                       'research_eligible': True, 'formal_status': 'FORMAL'} for r in (1, 2)]}


def capture(race):
    odds = {f'{a}-{b}-{c}': float(10 * a + b + c / 10)
            for a in range(1, 7) for b in range(1, 7) for c in range(1, 7) if len({a, b, c}) == 3}
    return {'SCHEMA': 'G11_MORNING_ODDS_FOOTPRINT_CAPTURE_V1', 'PROJECT': P.PROJECT,
            'RACE_ID': race['key'], 'DEADLINE_JST': race['deadline_jst'],
            'STATUS': 'MORNING_ODDS_LOCKED_EXACT_120',
            'REQUEST_STARTED_AT_JST': DAY + 'T07:10:00+09:00',
            'CAPTURED_AT_JST': DAY + 'T07:10:01+09:00', 'RESPONSE_SHA256': 'a' * 64,
            'ODDS_ROWS': [{'COMBINATION': k, 'MARKET_ODDS': v} for k, v in odds.items()],
            'ODDS_MAPPING_SHA256': P.sha(P.encoded(odds)), **dict.fromkeys(P.FLAGS, False)}


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'history'
        self.work = Path(self.temp.name) / 'work'
        self.input = Path(self.temp.name) / 'feed.json'
        self.day = self.root / '2026/11/12'
        self.feed = feed()
        P.write(self.input, self.feed)

    def scope(self):
        P.write(self.day / 'scope.json', {'OPERATIONAL_DATE_JST': DAY, 'RACE_COUNT': 2,
                                        'RACES': [{'RACE_ID': r['key']} for r in self.feed['races']]})
        P.write(self.day / 'calibration-lock.json', {'AS_OF_BEFORE_DATE_JST': DAY})

    def sample(self, i=0, detected=False):
        r = self.feed['races'][i]
        c = capture(r)
        a = {'RACE_ID': r['key'], 'MORNING_SNAPSHOT_SHA256': P.sha(P.encoded(c)),
             'DETECTED_FOOTPRINT_COUNT': int(detected),
             'DETECTED_FOOTPRINTS': [{'CANONICAL_SIGNATURE': 'test'}] if detected else [],
             **dict.fromkeys(P.FLAGS, False)}
        P.write(self.day / 'captures' / (r['key'] + '.json'), c)
        P.write(self.day / 'analysis' / (r['key'] + '.json'), a)
        return a

    def inspect(self, mode='settle'):
        return P.inspect(self.root, self.input, DAY, mode)

    def test_absent_feed_is_unknown_not_zero(self):
        self.input.unlink()
        r = self.inspect()
        self.assertEqual(r['status'], 'FEED_UNAVAILABLE')
        self.assertIsNone(r['expected'])
        self.assertEqual(P.finish(r, 'settle'), 1)

    def test_absent_morning_scope_is_not_successful_noop(self):
        r = self.inspect()
        self.assertEqual((r['status'], r['expected'], r['saved']), ('MISSING_MORNING_DATA', 2, 0))
        self.assertEqual(len(r['missing']), 2)
        self.assertEqual(P.finish(r, 'settle'), 1)

    def test_partial_morning_retained_but_night_incomplete(self):
        self.scope()
        self.sample()
        r = self.inspect()
        self.assertEqual((r['status'], r['expected'], r['saved'], r['analyzed']),
                         ('PARTIAL_MORNING_DATA', 2, 1, 1))
        self.assertEqual(P.finish(r, 'capture'), 0)
        self.assertEqual(P.finish(r, 'settle'), 1)
        self.assertEqual(P.finish(r, 'audit'), 1)

    def test_all_raw_and_analysis_no_detection_is_a_valid_zero(self):
        self.scope()
        self.sample(0)
        self.sample(1)
        self.assertEqual(self.inspect()['status'], 'COMPLETE_RESEARCH_ONLY')

    def test_incomplete_analysis_cannot_pass(self):
        self.scope()
        self.sample(0)
        self.sample(1)
        (self.day / 'analysis/20261112-01-02.json').unlink()
        self.assertEqual(self.inspect()['status'], 'PARTIAL_MORNING_DATA')

    def test_detected_unsettled_and_wrong_source_are_pending(self):
        self.scope()
        a = self.sample(0, True)
        self.sample(1)
        self.assertEqual(self.inspect()['status'], 'WAITING_OFFICIAL_SETTLEMENT')
        path = self.day / 'settlement/20261112-01-01.json'
        payload = {'SOURCE_ANALYSIS_SHA256': 'b' * 64,
                   'FOOTPRINT_SETTLEMENTS': [{'STATUS': 'SETTLED'}]}
        P.write(path, payload)
        self.assertEqual(self.inspect()['status'], 'WAITING_OFFICIAL_SETTLEMENT')
        payload['SOURCE_ANALYSIS_SHA256'] = P.sha(P.encoded(a))
        P.write(path, payload)
        self.assertEqual(self.inspect()['status'], 'COMPLETE_RESEARCH_ONLY')
        payload['FOOTPRINT_SETTLEMENTS'][0]['STATUS'] = 'SETTLEMENT_EXCLUDED_REFUND'
        P.write(path, payload)
        self.assertEqual(self.inspect()['status'], 'COMPLETE_RESEARCH_ONLY')

    def test_rescue_not_applicable_only_with_explicit_exclusions(self):
        for r in self.feed['races']:
            r.update(research_eligible=False, formal_status='EXCLUDED')
        P.write(self.input, self.feed)
        self.assertEqual(self.inspect()['status'], 'NOT_APPLICABLE_EXCLUDED')
        self.feed['races'][0]['formal_status'] = 'FORMAL'
        P.write(self.input, self.feed)
        with self.assertRaisesRegex(ValueError, 'EMPTY_SCOPE_UNPROVEN'):
            self.inspect()

    def test_scope_mismatch_and_calibration_missing_cannot_pass(self):
        self.scope()
        scope = P.read(self.day / 'scope.json')
        scope['RACE_COUNT'] = 3
        P.write(self.day / 'scope.json', scope)
        self.assertEqual(self.inspect()['status'], 'SCOPE_MISMATCH')
        self.scope()
        (self.day / 'calibration-lock.json').unlink()
        self.assertEqual(self.inspect()['status'], 'CALIBRATION_LOCK_MISSING')

    def test_late_capture_or_modified_odds_invalid(self):
        self.scope()
        self.sample(0)
        self.sample(1)
        path = self.day / 'captures/20261112-01-01.json'
        c = P.read(path)
        c['CAPTURED_AT_JST'] = DAY + 'T11:58:00+09:00'
        P.write(path, c)
        self.assertEqual(self.inspect()['status'], 'INVALID_MORNING_DATA')
        self.sample(0)
        c = P.read(path)
        c['ODDS_ROWS'][0]['MARKET_ODDS'] += 1
        P.write(path, c)
        self.assertEqual(self.inspect()['status'], 'INVALID_MORNING_DATA')

    def test_capture_boundary_before_request_and_deadline(self):
        P.capture_boundary(datetime.fromisoformat(DAY + 'T07:10:00+09:00'), DAY)
        for clock in ('07:04:59', '08:55:00', '11:51:00', '22:00:00'):
            with self.assertRaisesRegex(ValueError, 'MORNING_CAPTURE_WINDOW_CLOSED'):
                P.capture_boundary(datetime.fromisoformat(DAY + 'T' + clock + '+09:00'), DAY)
        with self.assertRaises(ValueError):
            P.capture_boundary(datetime.fromisoformat(DAY + 'T08:30:00+09:00'), DAY,
                               DAY + 'T08:30:00+09:00')
        with self.assertRaises(ValueError):
            P.capture_boundary(datetime(2026, 11, 12, 7, 10, tzinfo=timezone.utc), DAY)

    def test_encrypted_cumulative_checkpoint_roundtrip_and_tampering(self):
        self.scope()
        self.sample()
        report = self.inspect()
        with patch.dict(os.environ, {'STATE_PASSPHRASE': 'synthetic-unit-test-only', 'GITHUB_RUN_ID': '123'}):
            raw = P.pack(self.root, report, DAY, 'capture')
            try:
                cipher = P.crypt(raw)
            except subprocess.CalledProcessError as exc:
                if os.environ.get('GITHUB_ACTIONS') != 'true' and b'No agent running' in exc.stderr:
                    self.skipTest('Local sandbox disallows gpg-agent sockets; real GPG is mandatory in Actions CI')
                raise
            self.assertNotIn(b'MORNING_ODDS_LOCKED', cipher)
            restored = P.unpack(P.crypt(cipher, decrypt=True), '123')
            self.assertEqual(restored, P.history_files(self.root))
            with self.assertRaises(Exception):
                P.crypt(cipher[:50] + b'broken' + cipher[70:], decrypt=True)
            with self.assertRaisesRegex(ValueError, 'CHECKPOINT_RUN'):
                P.unpack(raw, '999')

    def test_append_only_merge_is_atomic_and_other_paths_untouched(self):
        self.scope()
        outside = self.root.parent / 'p3.txt'
        outside.write_text('unchanged')
        original = P.history_files(self.root)
        with self.assertRaisesRegex(ValueError, 'IMMUTABLE_HISTORY_CONFLICT'):
            P.merge(self.root, {'2026/11/12/scope.json': b'changed',
                                '2026/11/12/captures/20261112-01-01.json': b'new'})
        self.assertEqual(P.history_files(self.root), original)
        P.merge(self.root, {'2026/11/12/daily-summary.json': b'{}'})
        P.merge(self.root, {'2026/11/12/daily-summary.json': b'{"new":true}'})
        self.assertEqual(outside.read_text(), 'unchanged')

    def test_manifest_detects_changed_payload_and_unindexed_extra_member(self):
        self.scope()
        raw = P.pack(self.root, self.inspect(), DAY, 'capture')
        for extra in (False, True):
            edited = io.BytesIO()
            with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as source:
                with tarfile.open(fileobj=edited, mode='w:gz') as target:
                    for member in source:
                        data = source.extractfile(member).read()
                        if not extra and member.name.endswith('/scope.json'):
                            data += b' '
                        member.size = len(data)
                        target.addfile(member, io.BytesIO(data))
                    if extra:
                        member = tarfile.TarInfo('history/2026/11/12/daily-summary.json')
                        target.addfile(member, io.BytesIO(b''))
            with self.assertRaisesRegex(ValueError, 'CHECKPOINT_DIGESTS'):
                P.unpack(edited.getvalue())

    def test_archive_rejects_traversal_symlinks_duplicates_and_unindexed_files(self):
        for path in ('../secret.json', '/secret.json', '2026/11/12/../../p3.json',
                     '2026/11/12/captures/20261111-01-01.json', '2026/11/12/model.json'):
            with self.assertRaises(ValueError):
                P.history_name(path)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'link').symlink_to('/tmp')
            with self.assertRaises(ValueError):
                P.history_files(root)
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode='w:gz') as archive:
            member = tarfile.TarInfo('history/../secret')
            archive.addfile(member, io.BytesIO(b''))
        with self.assertRaises(ValueError):
            P.unpack(raw.getvalue())

    def test_restore_accepts_failed_main_run_but_not_foreign_workflow(self):
        row = {'id': 9, 'name': P.ARTIFACT, 'expired': False,
               'workflow_run': {'id': 4, 'head_branch': 'main', 'head_repository_id': 1367703708}}
        origin = {'path': P.WORKFLOW, 'head_branch': 'main', 'event': 'schedule', 'conclusion': 'failure'}
        self.assertEqual(P.select_checkpoint([row], '8', lambda _: origin), row)
        self.assertIsNone(P.select_checkpoint([row], '4', lambda _: origin))
        self.assertIsNone(P.select_checkpoint([row], '8', lambda _: dict(origin, path='other.yml')))
        row['expired'] = True
        with self.assertRaisesRegex(ValueError, 'LATEST_CHECKPOINT_EXPIRED'):
            P.select_checkpoint([row], '8', lambda _: origin)

    def test_finish_requires_remote_readback_receipt(self):
        args = ['test', 'finish', '--history', str(self.root), '--work', str(self.work),
                '--day', DAY, '--mode', 'settle']
        P.write(self.work / 'research-status.json', {'status': 'COMPLETE_RESEARCH_ONLY'})
        with patch('sys.argv', args):
            with self.assertRaisesRegex(ValueError, 'REMOTE_SAVE_NOT_VERIFIED'):
                P.main()
        P.write(self.work / 'remote-save-verified.json', {'day': DAY, 'mode': 'settle'})
        with patch('sys.argv', args):
            self.assertEqual(P.main(), 0)

    def test_workflow_saves_failures_and_only_ciphertext_before_completeness_gate(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / P.WORKFLOW).read_text()
        normal = workflow.split('      # One-time 2026-10-13 recovery', 1)[0]
        self.assertNotIn('git push', normal)
        self.assertNotIn('contents: write', workflow)
        names = ['Restore verified encrypted MORNING research history',
                 'Capture earliest complete MORNING exact-120 odds',
                 'Encrypt research history before the runner disappears',
                 'Retain only the encrypted research checkpoint',
                 "Read back this run's remote encrypted checkpoint",
                 'Verify exact remote bytes and every retained research file',
                 'Require persisted and complete research before reporting completion']
        self.assertEqual([normal.index(n) for n in names], sorted(normal.index(n) for n in names))
        upload = normal.split('      - name: Retain only the encrypted research checkpoint')[1].split('      - name:')[0]
        self.assertIn('always() && !cancelled()', upload)
        self.assertIn('path: .shadow-work/history.tgz.gpg', upload)
        self.assertNotIn('overwrite:', upload)


if __name__ == '__main__':
    unittest.main()
