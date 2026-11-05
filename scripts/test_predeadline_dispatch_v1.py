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
        status, page = args['status'][0], int(args['page'][0])
        data = self.after if self.post_count else self.before
        data = [x for x in data if x['status'] == status]
        return {'workflow_runs': data[(page-1)*100:page*100],
                'total_count': len(data) if self.total is None else self.total}


class DispatchTests(unittest.TestCase):
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


if __name__=='__main__':
    unittest.main()
