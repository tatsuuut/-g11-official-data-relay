import contextlib
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from optional_display_isolation_v1 import isolate_optional_display


class DisplayIsolationTests(unittest.TestCase):
    def test_healthy_projection_is_exactly_preserved(self):
        result = ({'available': True}, {'a': {'status': 'MORNING_AUDITED'}})
        reader = Mock(return_value=result)
        relay = SimpleNamespace(_display_research=reader)
        with isolate_optional_display(relay):
            self.assertIs(relay._display_research(Path('/unused'), {'a'}), result)
        self.assertIs(relay._display_research, reader)

    def test_partial_stale_malformed_and_unreadable_research_cannot_stop_feed(self):
        for error in [ValueError('DISPLAY_RACE_PARTITION'), KeyError('COMPLETED_RACE_KEYS'),
                      ValueError('bad JSON'), OSError('secret/path/not/for/logging')]:
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                retained = root / 'manifest.json'
                retained.write_bytes(b'original partial evidence')
                reader = Mock(side_effect=error)
                relay = SimpleNamespace(_display_research=reader)
                with contextlib.redirect_stderr(io.StringIO()) as log, isolate_optional_display(relay):
                    summary, rows = relay._display_research(root, {'a', 'b'})
                self.assertEqual(retained.read_bytes(), b'original partial evidence')
                self.assertFalse(summary['available'])
                self.assertEqual(summary['status'], 'TECHNICAL_HOLD')
                self.assertEqual(set(rows), {'a', 'b'})
                self.assertTrue(all(x['status'] == 'NOT_YET_ISSUED' for x in rows.values()))
                self.assertEqual(rows['a']['boats'], [])
                self.assertIsNone(summary['daily_aggregate'])
                self.assertIs(relay._display_research, reader)
                self.assertNotIn('secret/path', log.getvalue())

    def test_core_errors_and_process_cancellation_still_propagate(self):
        relay = SimpleNamespace(_display_research=Mock(return_value=({}, {})))
        original = relay._display_research
        for reason in ['SOURCE_HASH', 'LOCK_HASH', 'FEED_COUNT_PARTITION', 'RESULT_BINDING']:
            with self.assertRaisesRegex(ValueError, reason):
                with isolate_optional_display(relay):
                    raise ValueError(reason)
            self.assertIs(relay._display_research, original)
        for error in (KeyboardInterrupt(), SystemExit(9)):
            relay._display_research = Mock(side_effect=error)
            with self.assertRaises(type(error)), isolate_optional_display(relay):
                relay._display_research(None, {'a'})

    def test_nested_scopes_restore_original_and_do_not_double_read(self):
        reader = Mock(side_effect=ValueError('DISPLAY_RACE_PARTITION'))
        relay = SimpleNamespace(_display_research=reader)
        with isolate_optional_display(relay):
            first = relay._display_research
            with isolate_optional_display(relay), contextlib.redirect_stderr(io.StringIO()):
                relay._display_research(None, {'a'})
            self.assertIs(relay._display_research, first)
        self.assertIs(relay._display_research, reader)
        self.assertEqual(reader.call_count, 1)


if __name__ == '__main__':
    unittest.main()
