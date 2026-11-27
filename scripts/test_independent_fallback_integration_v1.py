"""Offline independent fallback over the actual retained November 27 runtime.

National refusals, requests and source pages are explicit synthetic fixtures.
The filesystem-backed refusal ledger, parsers, source receipts and P3/V53
forecast methods are real frozen code. The WILD check observes its existing
PRE_RACE input boundary; it does not claim a normal Gori formal LOCK or any
production acquisition/publication.
"""
from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta
import inspect
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit


def require(ok, reason):
    if not ok:
        raise AssertionError(reason)


def exercise():
    frozen = Path(os.environ['G11_EXHIBITION_FROZEN_DIR']).resolve()
    candidate = Path(os.environ['G11_EXHIBITION_CANDIDATE_DIR']).resolve()
    scripts = Path(__file__).resolve().parent
    sys.path[:] = [str(frozen), str(scripts), *[p for p in sys.path if p not in (str(candidate), str(frozen))]]
    os.chdir(frozen)
    import exhibition_input_compatibility_v1 as exhibition
    import independent_fallback_v1 as independent
    import independent_original_display_v1 as independent_original
    import test_exhibition_today_frozen_consumers_v1 as fixture
    from g11.canonical import live_source_failover_v1 as failover
    from g11.canonical.test_official_pipeline import odds_html
    from g11.canonical.test_live_refusal_recovery_v1 import TestLiveRefusalRecovery
    from g11.canonical.test_published_preview_v1 import published_card
    from g11.wild_pack.v1 import runtime_orchestrator_v1 as wild
    from g11.relay import predeadline_critical_v1 as critical
    from rescue_night_result_parser_v1 import official_input_compatibility
    from predeadline_content_retry_v1 import bounded_content_retry
    from critical_formal_lock_scope_v1 import formal_lock_scope
    import predeadline_capture_window_v1 as capture_window

    checks, route_calls = [], {}
    with TemporaryDirectory(prefix='independent-native-integration-') as temporary, ExitStack() as all_scopes:
        def no_live_network(*_args, **_kwargs):
            raise AssertionError('live network prohibited; only explicit independent response fixtures are allowed')
        all_scopes.enter_context(patch.object(socket, 'create_connection', side_effect=no_live_network))
        all_scopes.enter_context(patch.object(socket.socket, 'connect', side_effect=no_live_network))
        all_scopes.enter_context(patch.object(socket.socket, 'connect_ex', side_effect=no_live_network))
        root = Path(temporary)
        baseline = root / 'baseline'; baseline.mkdir()
        official, preview, current, daily, original_store, _, clock, morning = fixture.prepare_state(baseline, frozen)
        plan = {row['SOURCE_ID']: row for row in morning['PREDEADLINE_PLAN']}
        primary_sid = 'beforeinfo-' + fixture.KEY
        primary_url = plan[primary_sid]['URL']

        # Build a genuine versioned terminal STOP through the native append-only
        # ledger. The trusted-service grant is a native test fixture, not an API.
        events = [('beforeinfo-' + fixture.PRIOR_KEY, '08:00:00', 'HTTP_403', 403),
                  (primary_sid, '10:00:00', 'HTTP_403', 403),
                  ('odds3t-' + fixture.PRIOR_KEY, '12:00:00', 'HTTP_200', 200),
                  ('beforeinfo-' + fixture.PRIOR_KEY, '12:01:00', 'HTTP_403', 403)]
        def grant(ledger, request, at, recovery):
            value = TestLiveRefusalRecovery.grant(None, ledger, request, at, recovery)
            value['authority_sha256'] = daily.load(baseline, fixture.DAY)['authority_sha256']
            value['deadline_jst'] = plan[request['SOURCE_ID']]['MUST_FINISH_BEFORE_JST']
            return value
        with patch.object(official, 'LIVE_REFUSAL_RECOVERY_CLAIM_PROVIDER', grant):
            for sid, stamp, outcome, status in events:
                at = datetime.fromisoformat(fixture.DAY + 'T' + stamp + '+09:00')
                request = original_store.ledger.begin(sid, plan[sid]['URL'], at)
                original_store.ledger.finish(request, outcome, at, http_status=status)
        stopped = original_store.ledger.state()
        require(stopped['STOPPED'] is True and stopped['GENERAL_HTTP_403_COUNT'] == 3
                and stopped['REFUSAL_RECOVERY']['STATUS'] == 'EXHAUSTED'
                and stopped['REFUSAL_RECOVERY']['ATTEMPT_COUNT'] == 1,
                'native refusal fixture did not reach terminal STOP')
        checks.append('ACTUAL_FILESYSTEM_LEDGER_VERSIONED_STOP_WITH_RECOVERED_THEN_REFUSED_HISTORY')

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                value = clock['now']
                return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)
        all_scopes.enter_context(patch.object(capture_window, 'datetime', Clock))
        if os.environ.get('G11_EXHIBITION_ALLOW_API_MATERIALIZATION_TEST') == '1':
            all_scopes.enter_context(patch.object(exhibition, '_candidate_head', return_value=exhibition.CANDIDATE_SHA))

        for case in ('second', 'third', 'provider-hold', 'expired'):
            state = root / case
            shutil.copytree(baseline, state)
            clock['now'] = fixture.CUTOFF if case == 'expired' else fixture.ACTIVATED
            store = official.OfficialArtifactStore(state / 'store')
            client = official.OfficialHttpClient(store, clock=lambda: clock['now'],
                opener=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError('national HTTP was reopened')))
            runner = official.CanonicalRunner(store, client=client)
            roster = preview.race_roster(morning, fixture.KEY)
            fixture.put_odds(store, official, fixture.KEY, fixture.CUTOFF - timedelta(minutes=3), odds_html())
            old_ledger = fixture.file_bytes(store.ledger.root)
            old_state = deepcopy(store.ledger.state())
            old_locks = fixture.file_bytes(store.root / 'snapshots/predeadline')
            old_models = daily.model_inventory(store.root)
            old_source = daily.source_inventory(frozen)
            old_authority = fixture.file_bytes(state / 'daily-runtime')
            calls = []

            def assert_national_still_stopped():
                require(store.ledger.state()['STOPPED'] is True, 'independent read cleared native STOP')
                try:
                    store.ledger.begin(primary_sid, primary_url, clock['now'])
                except official.PipelineError as error:
                    require(str(error) == 'PIPELINE_STOPPED', 'national request rejected for the wrong reason')
                else:
                    raise AssertionError('national request began inside independent capture')

            acquisition = {'ACQUIRED': [], 'REJECTED': [], 'RETRYABLE_FAILURES': [],
                'UNAVAILABLE': [], 'TASK_DEFERRED': [{'SOURCE_ID': primary_sid,
                    'RACE_KEY': fixture.KEY, 'REASON': 'PIPELINE_STOPPED'}]}
            unadapted = failover.capture(runner, acquisition)
            require(unadapted['LIVE_SOURCE_FAILOVER']['STATUS'] == 'GLOBAL_STOPPED',
                    'unadapted frozen fallback unexpectedly bypassed national STOP')
            with official_input_compatibility(), bounded_content_retry(early_capture=True), \
                    exhibition.context(state, fixture.DAY, candidate, nowfunc=lambda: clock['now']), \
                    independent.context(fixture.DAY), independent_original.context(fixture.DAY):
                from g11.canonical import boatcast_preview_v1 as third, shared_original_display_v1 as shared
                with formal_lock_scope(fixture.DAY):
                    card = json.dumps(published_card(roster), ensure_ascii=False).replace(
                        '2026-09-29', fixture.DAY).replace('09:00:00', '23:59:00').encode()
                    files = candidate / 'g11/canonical/fixtures'
                    raw = {kind: (files / ('boatcast_preview_20261127_18_03_' + kind + '.txt')).read_bytes()
                           for kind in third.KINDS}
                    tables = {kind: [line.split('\t') for line in raw[kind].decode().splitlines()]
                              for kind in ('str3', 'tkz', 'stt')}
                    for boat, (racer, motor, name) in roster.items():
                        tables['str3'][boat + 1][0] = str(racer)
                        tables['str3'][boat + 1][1] = name
                        tables['str3'][boat + 1][17] = str(motor)
                        tables['tkz'][boat + 1][0] = name
                    for row in tables['stt'][2:]:
                        row[2] = roster[int(row[1])][2]
                    for kind, rows in tables.items():
                        raw[kind] = ('\n'.join('\t'.join(row) for row in rows) + '\n').encode()
                    weather = raw['sui'].decode().strip().split('\t'); weather[0] = '2355'
                    raw['sui'] = ('\t'.join(weather) + '\n').encode()

                    def second_read(url, observed_clock):
                        require(urlsplit(url).hostname == preview.HOST, 'unexpected second provider host')
                        assert_national_still_stopped(); calls.append(('second', url))
                        if case != 'second':
                            raise ValueError('SYNTHETIC_DAILY_PREVIEW_NOT_YET_PUBLISHED')
                        return card, observed_clock(), observed_clock() - timedelta(minutes=1)

                    def third_read(url, _cutoff, observed_clock, _seconds):
                        require(urlsplit(url).hostname == third.HOST, 'unexpected third provider host')
                        if not any(call[0] == 'third' for call in calls):
                            # These calls happen while the independent adapter's
                            # cloned transport is active, so they also prove its
                            # outer exhibition scope guards survived cloning.
                            for wrong_store, wrong_clock, reason in (
                                (official.OfficialArtifactStore(state / 'wrong-store'), observed_clock, 'NEW_SOURCE_STORE'),
                                (store, lambda: observed_clock() + timedelta(seconds=1), 'INPUT_BEFORE_ACTIVATION_OR_FUTURE')):
                                try:
                                    third.due_observation(wrong_store, fixture.KEY, fixture.CUTOFF, roster, wrong_clock)
                                except ValueError as error:
                                    require(str(error) == 'EXHIBITION_INPUT_COMPAT:' + reason,
                                            'outer exhibition guard was lost: ' + str(error))
                                else:
                                    raise AssertionError('wrong-store or future-clock independent capture was admitted')
                        assert_national_still_stopped(); calls.append(('third', url))
                        kind = next(kind for kind, expected in third.urls_for(fixture.KEY).items() if url == expected)
                        clock['now'] += timedelta(milliseconds=1)
                        return raw[kind], observed_clock(), observed_clock() - timedelta(minutes=1)

                    if case == 'provider-hold':
                        hold = store.root / 'g11/published-preview-state' / shared.BOATCAST.HOST / 'host.json'
                        hold.parent.mkdir(parents=True, exist_ok=True)
                        hold.write_text(json.dumps({'host': shared.BOATCAST.HOST,
                            'next': (clock['now'] + timedelta(minutes=2)).isoformat(), 'status': 403}))
                    with patch.object(preview, '_read', side_effect=second_read), \
                            patch.object(third, '_read', side_effect=third_read):
                        fallback = failover.capture(runner, acquisition)
                    assert_national_still_stopped()
                    require(store.ledger.state() == old_state and fixture.file_bytes(store.ledger.root) == old_ledger,
                            'independent acquisition changed native refusal ledger')
                    saved = store.root / 'snapshots/predeadline' / ('published-exhibition-' + fixture.KEY + '.json')
                    if case in ('second', 'third'):
                        require(saved.is_file(), 'independent route did not save its verified input: ' + repr(fallback))
                        proof = store.load_verified_snapshot('predeadline', 'published-exhibition-' + fixture.KEY)
                        expected_host = preview.HOST if case == 'second' else third.HOST
                        require(proof['SNAPSHOT']['PAYLOAD']['PUBLISHER'] == expected_host,
                                'saved publisher was relabelled')
                        require(len([call for call in calls if call[0] == 'second']) == 1,
                                'daily publisher was polled repeatedly')
                        require(len([call for call in calls if call[0] == 'third']) == (0 if case == 'second' else 4),
                                'third source packet was skipped or repeatedly acquired')
                        result = runner.run_due_predeadline_predictions()
                        require(fixture.KEY in result['COMPLETED'], 'native formal prediction did not complete')
                        v53 = store.load_verified_snapshot('predeadline', runner._abeken_v53_live_name(fixture.KEY))
                        require(v53['SNAPSHOT']['PAYLOAD']['BEFOREINFO_SNAPSHOT_SHA256'] == proof['SNAPSHOT_SHA256'],
                                'V53 formal LOCK lost independent input')
                        packet = fixture.collect_wild(store, proof, wild, clock)
                        require(packet.is_file(), 'WILD existing PRE_RACE input boundary was not connected')
                        if case == 'third':
                            wrong = deepcopy(proof['SNAPSHOT'])
                            wrong['SOURCE_RECEIPTS'][0]['HOST'] = 'unreviewed.invalid'
                            try:
                                third.validate_snapshot(wrong, fixture.KEY)
                            except ValueError:
                                pass
                            else:
                                raise AssertionError('wrong source host was accepted')
                        compiled = inspect.unwrap(critical.run).__globals__['_tree_sha']
                        require(compiled is critical._tree_sha,
                                'critical entry retained a pre-adapter source identity')
                        complete = compiled(store.root)
                        require(isinstance(complete, str) and len(complete) == 64,
                                'native critical checkpoint did not finish after fallback restored its guards')
                        checks.extend([case.upper() + '_PARSED_ORIGINAL_RECEIPTS_SAVED',
                                       case.upper() + '_NATIVE_FORMAL_P3_V53_AND_WILD_PRERACE_INPUT_BOUNDARY',
                                       case.upper() + '_REAL_SCOPED_CRITICAL_CHECKPOINT'])
                    else:
                        require(not saved.exists(), 'held or expired race produced a new input')
                        require(not any(call[0] == 'third' for call in calls), 'held or expired third provider was requested')
                        if case == 'expired':
                            require(not calls, 'expired fallback issued HTTP')
                        checks.append(case.upper().replace('-', '_') + '_NO_NEW_INPUT')
            require(daily.model_inventory(store.root) == old_models and daily.source_inventory(frozen) == old_source,
                    'fallback changed frozen models or source')
            require(all((store.root / 'snapshots/predeadline' / name).read_bytes() == raw
                        for name, raw in old_locks.items()), 'fallback rewrote a previously saved input or LOCK')
            require(all((state / 'daily-runtime' / name).read_bytes() == raw
                        for name, raw in old_authority.items()), 'fallback rewrote immutable authority or binding')
            require(fixture.file_bytes(store.ledger.root) == old_ledger and store.ledger.state() == old_state,
                    'native national refusal changed after inference')
            route_calls[case] = calls
        checks.extend(['NATIONAL_CLIENT_STOPPED_INSIDE_EVERY_INDEPENDENT_READ',
                       'LEDGER_MODELS_SOURCE_AUTHORITY_AND_PRIOR_LOCKS_UNCHANGED',
                       'THIRD_STORE_AND_FUTURE_CLOCK_SCOPE_GUARDS_PRESERVED',
                       'WRONG_THIRD_RECEIPT_HOST_REJECTED'])
    return {'STATUS': 'PASS', 'CHECKS': checks, 'ROUTE_CALLS': route_calls,
            'LIVE_HTTP_REQUESTS': 0, 'NORMAL_GORI_FORMAL_LOCK_PROVEN': False,
            'NATIVE_WILD_CHECK': 'SAVED_PRE_RACE_INPUT_BOUNDARY_ONLY'}


class IndependentFrozenIntegrationTests(unittest.TestCase):
    def test_recorded_national_stop_allows_only_bounded_independent_acquisition(self):
        if not os.environ.get('G11_EXHIBITION_FROZEN_DIR') or not os.environ.get('G11_EXHIBITION_CANDIDATE_DIR'):
            self.skipTest('requires exact frozen and candidate source checkouts')
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--exercise'],
            env=dict(os.environ), capture_output=True, text=True, timeout=150)
        self.assertEqual(result.returncode, 0, result.stdout[-6000:] + '\n' + result.stderr[-9000:])
        report = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(report['STATUS'], 'PASS')
        self.assertEqual(report['LIVE_HTTP_REQUESTS'], 0)
        self.assertFalse(report['NORMAL_GORI_FORMAL_LOCK_PROVEN'])
        self.assertIn('NATIONAL_CLIENT_STOPPED_INSIDE_EVERY_INDEPENDENT_READ', report['CHECKS'])
        self.assertIn('THIRD_PARSED_ORIGINAL_RECEIPTS_SAVED', report['CHECKS'])


if __name__ == '__main__':
    if sys.argv[1:] == ['--exercise']:
        print(json.dumps(exercise(), sort_keys=True))
    else:
        unittest.main()
