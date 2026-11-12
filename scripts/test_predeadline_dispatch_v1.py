"""Mocked scheduling tests: never request official data or run a prediction."""
import unittest
from urllib.parse import parse_qs, urlsplit
from predeadline_dispatch_v1 import ACTIVE, ApiError, DispatchError, dispatch

DAY = '2026-11-06'


def row(run_id=101, number=101, status='queued', day=DAY, **kw):
    return dict(id=run_id, run_number=number, status=status, head_branch='main',
                event='workflow_dispatch', display_title='LIVE-predeadline-' + day,
                created_at='2026-11-05T23:00:00Z', **kw)


class FakeClient:
    def __init__(self, posts=(), before=(), after=()):
        self.posts = list(posts)
        self.before, self.after = list(before), list(after)
        self.calls = []
        self.post_count = 0
        self.get_error = None
        self.invalid = None
        self.total = None

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == 'POST':
            self.post_count += 1
            outcome = self.posts.pop(0) if self.posts else None
            if outcome is not None:
                raise ApiError(outcome)
            return None
        if self.get_error:
            raise ApiError(self.get_error)
        if self.invalid is not None:
            return self.invalid
        args = parse_qs(urlsplit(path).query)
        status, page = args.get('status', [None])[0], int(args['page'][0])
        data = self.after if self.post_count else self.before
        data = [x for x in data if status is None or x['status'] == status]
        return {'workflow_runs': data[(page-1)*100:page*100],
                'total_count': len(data) if self.total is None else self.total}


class DispatchTests(unittest.TestCase):
    def test_moving_run_index_is_reread_before_a_single_dispatch(self):
        class MovingIndex(FakeClient):
            def __init__(self):
                super().__init__(); self.inconsistent = 2
            def request(self, method, path, body=None):
                result = super().request(method,path,body)
                if method == 'GET' and self.inconsistent:
                    self.inconsistent -= 1
                    return {'total_count':1,'workflow_runs':[]}
                return result
        c=MovingIndex(); delays=[]
        result=dispatch(c,DAY,100,100,'continuation',sleep=delays.append)
        self.assertEqual(result['status'],'DISPATCH_ACCEPTED')
        self.assertEqual(c.post_count,1)
        self.assertEqual(delays,[1,2])

    def test_successor_that_appears_during_list_recheck_prevents_duplicate_dispatch(self):
        class MovingIndex(FakeClient):
            def __init__(self):
                super().__init__(before=[row()]); self.first=True
            def request(self, method, path, body=None):
                result=super().request(method,path,body)
                if method=='GET' and self.first:
                    self.first=False
                    return {'total_count':1,'workflow_runs':[]}
                return result
        c=MovingIndex()
        self.assertEqual(self.run_dispatch(c)['status'],'EXISTING_ACTIVE')
        self.assertEqual(c.post_count,0)

    def test_persistently_incomplete_index_is_bounded_and_never_dispatches(self):
        c=FakeClient();c.invalid={'total_count':1,'workflow_runs':[]};delays=[]
        with self.assertRaisesRegex(DispatchError,'RUN_LIST_INCOMPLETE'):
            dispatch(c,DAY,100,100,'continuation',sleep=delays.append)
        self.assertEqual(c.post_count,0)
        self.assertEqual(len(c.calls),6)
        self.assertEqual(delays,[1,2,1,2])

    def run_dispatch(self, client, mode='continuation'):
        return dispatch(client, DAY, 100, 100, mode, sleep=lambda seconds: None)

    def test_first_success_posts_once_with_explicit_day(self):
        c = FakeClient()
        self.assertEqual(self.run_dispatch(c)['status'], 'DISPATCH_ACCEPTED')
        self.assertEqual(c.post_count, 1)
        self.assertEqual(c.calls[-1][2], {'ref':'main', 'inputs':{'phase':'predeadline','operational_date':DAY}})

    def test_existing_successor_all_active_states_prevents_post(self):
        for status in ACTIVE:
            c = FakeClient(before=[row(status=status)])
            self.assertEqual(self.run_dispatch(c)['status'], 'EXISTING_ACTIVE')
            self.assertEqual(c.post_count, 0)

    def test_stale_predecessor_does_not_block_continuation(self):
        c = FakeClient(before=[row(99,99)])
        self.assertEqual(self.run_dispatch(c)['status'], 'DISPATCH_ACCEPTED')

    def test_bootstrap_and_rerun_respect_older_active_same_day_run(self):
        for mode in ('bootstrap','rerun'):
            c = FakeClient(before=[row(99,99)])
            self.assertEqual(self.run_dispatch(c, mode)['status'], 'EXISTING_ACTIVE')
            self.assertEqual(c.post_count, 0)

    def test_current_run_is_not_its_own_successor(self):
        c = FakeClient(before=[row(100,100)])
        self.assertEqual(self.run_dispatch(c,'rerun')['status'], 'DISPATCH_ACCEPTED')

    def test_ambiguous_500_accepted_does_not_replay_post(self):
        c = FakeClient(posts=[500],after=[row()])
        self.assertEqual(self.run_dispatch(c)['status'], 'ACCEPTED_AFTER_AMBIGUOUS_RESPONSE')
        self.assertEqual(c.post_count, 1)

    def test_timeout_accepted_does_not_replay_post(self):
        c = FakeClient(posts=[0],after=[row()])
        self.assertEqual(self.run_dispatch(c)['status'], 'ACCEPTED_AFTER_AMBIGUOUS_RESPONSE')
        self.assertEqual(c.post_count, 1)

    def test_500_without_run_reconciles_before_retry(self):
        c = FakeClient(posts=[500,None])
        self.assertEqual(self.run_dispatch(c)['attempts'], 2)
        indices = [i for i,x in enumerate(c.calls) if x[0]=='POST']
        self.assertTrue(all(x[0]=='GET' for x in c.calls[indices[0]+1:indices[1]]))
        self.assertGreater(indices[1]-indices[0],1)

    def test_persistent_failure_is_bounded_and_not_success(self):
        c = FakeClient(posts=[500,502,503])
        with self.assertRaisesRegex(DispatchError, 'RETRY_EXHAUSTED'):
            self.run_dispatch(c)
        self.assertEqual(c.post_count,3)

    def test_auth_and_validation_errors_are_not_retried(self):
        for status in (400,401,403,404,422):
            c = FakeClient(posts=[status])
            with self.assertRaises(ApiError): self.run_dispatch(c)
            self.assertEqual(c.post_count,1)

    def test_get_error_fails_closed_without_post(self):
        c=FakeClient(); c.get_error=503
        with self.assertRaises(ApiError): self.run_dispatch(c)
        self.assertEqual(c.post_count,0)

    def test_incomplete_and_malformed_lists_fail_closed(self):
        for value in ({}, {'total_count':1,'workflow_runs':[]}, {'total_count':0,'workflow_runs':None},
                      {'total_count':2,'workflow_runs':[row(99,99)]},
                      {'total_count':0,'workflow_runs':[row(99,99)]}):
            c=FakeClient(); c.invalid=value
            with self.assertRaises(DispatchError): self.run_dispatch(c)
            self.assertEqual(c.post_count,0)

    def test_other_day_branch_and_phase_are_ignored(self):
        for changed in ({'display_title':'LIVE-predeadline-2026-11-05'}, {'head_branch':'test'},
                        {'display_title':'LIVE-night-'+DAY}, {'event':'push'}):
            r=row(); r.update(changed); c=FakeClient(before=[r])
            self.assertEqual(self.run_dispatch(c)['status'],'DISPATCH_ACCEPTED')

    def test_legacy_blank_day_title_blocks_only_same_local_day(self):
        r=row(); r['display_title']='LIVE-predeadline-'
        c=FakeClient(before=[r])
        self.assertEqual(self.run_dispatch(c)['status'],'EXISTING_ACTIVE')
        r['created_at']='2026-11-04T23:00:00Z'
        self.assertEqual(self.run_dispatch(FakeClient(before=[r]))['status'],'DISPATCH_ACCEPTED')

    def test_pagination_does_not_miss_active_successor(self):
        rows=[row(i,i,day='2026-11-05') for i in range(1,101)]+[row(101,101)]
        c=FakeClient(before=rows)
        self.assertEqual(self.run_dispatch(c)['status'],'EXISTING_ACTIVE')
        self.assertEqual(c.post_count,0)

    def test_invalid_scope_never_calls_api(self):
        for day in ('20261106','2026-02-30','2026-11-06\n'):
            c=FakeClient()
            with self.assertRaises(DispatchError): dispatch(c,day,100,100,'continuation')
            self.assertEqual(c.calls,[])


class DriftingStatusClient(FakeClient):
    """Observed Nov 12 count mismatch, plus an independent complete index."""
    def __init__(self, snapshot=None, **kw):
        super().__init__(**kw)
        self.snapshot = snapshot

    def request(self, method, path, body=None):
        result = super().request(method, path, body)
        if method == 'GET':
            args = parse_qs(urlsplit(path).query)
            if 'status' in args:
                return {'total_count': 2, 'workflow_runs': [row(100, 100, status='in_progress')]}
            if self.snapshot is not None:
                return self.snapshot
        return result


class CompletePrefixTests(unittest.TestCase):
    def run_dispatch(self, client, mode='continuation'):
        return dispatch(client, DAY, 100, 100, mode, sleep=lambda _: None)

    def test_observed_two_reported_one_returned_requires_independent_complete_proof(self):
        client = DriftingStatusClient(before=[row(100, 100, status='in_progress')])
        self.assertEqual(self.run_dispatch(client)['status'], 'DISPATCH_ACCEPTED')
        self.assertEqual(client.post_count, 1)
        self.assertEqual(len([x for x in client.calls if x[0] == 'GET']), 4)

    def test_every_active_successor_in_complete_prefix_blocks_post(self):
        for status in ACTIVE:
            with self.subTest(status=status):
                client = DriftingStatusClient(before=[row(status=status), row(100, 100)])
                self.assertEqual(self.run_dispatch(client)['status'], 'EXISTING_ACTIVE')
                self.assertEqual(client.post_count, 0)

    def test_complete_proof_is_required_even_when_a_candidate_is_visible(self):
        values = [
            {'total_count': 2, 'workflow_runs': [row()]},
            {'total_count': 1, 'workflow_runs': [row()]},
            {'total_count': 2, 'workflow_runs': [row(), row(99, 100)]},
            {'total_count': 2, 'workflow_runs': [row(), row(100, 99)]},
            {'total_count': 3, 'workflow_runs': [row(), row(), row(100, 100)]},
            {'total_count': 3, 'workflow_runs': [row(102, 101), row(101, 102), row(100, 100)]},
            {'total_count': 1, 'workflow_runs': [dict(row(100, 100), head_branch='fork')]},
            {'total_count': 1, 'workflow_runs': [dict(row(100, 100), status='unknown')]},
            {'total_count': True, 'workflow_runs': []},
        ]
        for value in values:
            with self.subTest(value=value):
                client = DriftingStatusClient(snapshot=value)
                with self.assertRaises(DispatchError):
                    self.run_dispatch(client)
                self.assertEqual(client.post_count, 0)

    def test_anchor_can_be_a_scheduled_run_but_never_blocks_itself(self):
        anchor = dict(row(100, 100, status='in_progress'), event='schedule', display_title='runtime sync')
        client = DriftingStatusClient(before=[anchor])
        self.assertEqual(self.run_dispatch(client)['status'], 'DISPATCH_ACCEPTED')

    def test_complete_second_page_finds_exact_anchor_and_newer_successor(self):
        rows = [row(i, i, day='2026-11-05') for i in range(205, 100, -1)]
        rows[0]['display_title'] = 'LIVE-predeadline-' + DAY
        client = DriftingStatusClient(before=rows + [row(100, 100)])
        self.assertEqual(self.run_dispatch(client)['status'], 'EXISTING_ACTIVE')
        self.assertEqual(client.post_count, 0)
        self.assertEqual(len([x for x in client.calls if x[0] == 'GET']), 5)

    def test_bootstrap_and_rerun_do_not_use_a_newer_only_proof(self):
        for mode in ('bootstrap', 'rerun'):
            client = DriftingStatusClient(before=[row(100, 100)])
            with self.assertRaisesRegex(DispatchError, 'RUN_LIST_INCOMPLETE'):
                self.run_dispatch(client, mode)
            self.assertEqual(client.post_count, 0)
            self.assertEqual(len(client.calls), 3)

    def test_ambiguous_post_reconciles_complete_index_before_any_replay(self):
        client = DriftingStatusClient(posts=[0], before=[row(100, 100)],
                                      after=[row(), row(100, 100)])
        self.assertEqual(self.run_dispatch(client)['status'], 'ACCEPTED_AFTER_AMBIGUOUS_RESPONSE')
        self.assertEqual(client.post_count, 1)

    def test_missing_anchor_and_page_limit_never_authorize_post(self):
        client = DriftingStatusClient(before=[row(i, i) for i in range(601, 100, -1)])
        with self.assertRaisesRegex(DispatchError, 'RUN_LIST_PAGE_LIMIT'):
            self.run_dispatch(client)
        self.assertEqual(client.post_count, 0)

    def test_three_phase_audit_preserves_the_shared_duplicate_and_completeness_guards(self):
        from pathlib import Path
        import tempfile
        from unittest import mock
        import audit_three_phase_runtime_contract as audit
        self.assertEqual(audit.audit_public()['PREDEADLINE'], 'PASS')
        source = (audit.ROOT / 'scripts/predeadline_dispatch_v1.py').read_text()
        mutations = (
            ("row.get('event') != 'workflow_dispatch' or row.get('status') not in ACTIVE",
             'PREDEADLINE_CHAIN_DISPATCH_ONLY_DEDUPE'),
            ("mode == 'continuation' and number <= current_number", 'PREDEADLINE_CHAIN_NEWER_RUN_DEDUPE'),
            ("len(rows) == min(100, max(0, total - 100 * (page - 1)))", 'PREDEADLINE_PREFIX_COMPLETENESS'),
            ("if number != current_number:", 'PREDEADLINE_PREFIX_BOUNDARY_IDENTITY'),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'scripts').mkdir()
            target = root / 'scripts/predeadline_dispatch_v1.py'
            for needle, code in mutations:
                target.write_text(source.replace(needle, 'REMOVED_GUARD'))
                with mock.patch.object(audit, 'ROOT', root):
                    with self.assertRaisesRegex(audit.ContractError, code):
                        audit.audit_public()


if __name__=='__main__':
    unittest.main()

