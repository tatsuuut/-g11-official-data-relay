from copy import deepcopy
import unittest

from daily_runtime_delivery_v1 import recount_rescue_abeken


class SavedRescuePublicationTests(unittest.TestCase):
    def test_count_retained_morning_and_live_without_counting_v53_twice(self):
        feed = {'abeken': {'predictions_emitted': 9, 'source_content_sha256': 'a' * 64},
            'races': [
                {'key': 'one', 'abeken_shadow': {'morning': {'bets': [1]}, 'live': None,
                    'v53': {'morning': {'bets': [2]}, 'live': {'bets': [3]}}}},
                {'key': 'two', 'abeken_shadow': {'morning': {'bets': [4]}, 'live': {'bets': [5]}}},
                {'key': 'excluded'}]}
        before = deepcopy(feed)
        recount_rescue_abeken(feed)
        self.assertEqual(3, feed['abeken']['predictions_emitted'])
        self.assertEqual(before['races'], feed['races'])
        self.assertEqual('a' * 64, feed['abeken']['source_content_sha256'])
        once = deepcopy(feed)
        recount_rescue_abeken(feed)
        self.assertEqual(once, feed)

    def test_absent_abeken_is_not_created(self):
        feed = {'races': [{'key': 'excluded'}]}
        before = deepcopy(feed)
        recount_rescue_abeken(feed)
        self.assertEqual(before, feed)


if __name__ == '__main__':
    unittest.main()
