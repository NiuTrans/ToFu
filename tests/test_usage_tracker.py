"""tests/test_usage_tracker.py — usage tracker unit tests."""

import datetime
import json
import os
import tempfile
import unittest
from unittest.mock import patch


class UsageTrackerTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = patch('lib.usage_tracker._STORE_PATH',
                             os.path.join(self._tmp.name, 'usage.json'))
        self._patch.start()
        from lib import usage_tracker as ut
        ut._state.clear()
        ut._loaded = False
        ut._dirty = False
        ut._last_flush = 0.0

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_record_and_retrieve(self):
        from lib.usage_tracker import record, usage_for_key
        record('k_test', n_tokens=100, model='gpt-x')
        record('k_test', n_tokens=50, model='gpt-x')
        record('k_test', n_tokens=25, model='claude-y')
        view = usage_for_key('k_test', days=1)
        self.assertEqual(view['key_id'], 'k_test')
        self.assertEqual(len(view['days']), 1)
        today = view['days'][0]
        self.assertEqual(today['requests'], 3)
        self.assertEqual(today['tokens'], 175)
        self.assertEqual(today['by_model'], {'gpt-x': 150, 'claude-y': 25})
        self.assertEqual(view['total']['requests'], 3)
        self.assertEqual(view['total']['tokens'], 175)

    def test_request_count_zero_means_token_only(self):
        from lib.usage_tracker import record, usage_for_key
        # request was already counted by middleware; route only adds tokens
        record('k_x', request_count=1)
        record('k_x', n_tokens=42, model='m', request_count=0)
        view = usage_for_key('k_x', days=1)
        self.assertEqual(view['days'][0]['requests'], 1)
        self.assertEqual(view['days'][0]['tokens'], 42)

    def test_anon_bucket_for_empty_key(self):
        from lib.usage_tracker import record, usage_for_key
        record('', n_tokens=10)
        view = usage_for_key('_anon', days=1)
        self.assertEqual(view['days'][0]['requests'], 1)

    def test_persistence_round_trip(self):
        from lib import usage_tracker as ut
        from lib.usage_tracker import record, flush, usage_for_key
        record('k_persist', n_tokens=99, model='m')
        flush()
        # Drop the cache and reload from disk.
        ut._state.clear()
        ut._loaded = False
        view = usage_for_key('k_persist', days=1)
        self.assertEqual(view['days'][0]['tokens'], 99)

    def test_summary_aggregates_keys(self):
        from lib.usage_tracker import record, usage_summary
        record('k_a', n_tokens=10, model='m')
        record('k_b', n_tokens=20, model='m')
        record('k_a', n_tokens=5, model='m')
        s = usage_summary(days=1)
        self.assertIn('k_a', s['per_key'])
        self.assertEqual(s['per_key']['k_a']['tokens'], 15)
        self.assertEqual(s['per_key']['k_b']['tokens'], 20)
        self.assertEqual(s['daily'][-1]['tokens'], 35)
        self.assertEqual(s['daily'][-1]['requests'], 3)

    def test_prune_drops_old_buckets(self):
        from lib import usage_tracker as ut
        from lib.usage_tracker import record, flush
        # Inject a bucket with an old date.
        old_day = (datetime.datetime.now(datetime.timezone.utc)
                    - datetime.timedelta(days=200)).strftime('%Y-%m-%d')
        ut._state[old_day] = {'k_old': {'requests': 1, 'tokens': 1,
                                          'by_model': {}}}
        record('k_new', n_tokens=1)  # marks dirty so flush runs
        flush()
        self.assertNotIn(old_day, ut._state)

    def test_window_includes_zero_days(self):
        from lib.usage_tracker import record, usage_for_key
        record('k_w', n_tokens=10)
        view = usage_for_key('k_w', days=7)
        self.assertEqual(len(view['days']), 7)
        non_zero = [d for d in view['days'] if d['tokens'] > 0]
        self.assertEqual(len(non_zero), 1)

    def test_all_keys_with_activity(self):
        from lib.usage_tracker import record, all_keys_with_activity
        record('k_a', n_tokens=1)
        record('k_b', n_tokens=1)
        keys = all_keys_with_activity()
        self.assertIn('k_a', keys)
        self.assertIn('k_b', keys)

    def test_negative_tokens_ignored(self):
        from lib.usage_tracker import record, usage_for_key
        record('k_neg', n_tokens=-100)
        view = usage_for_key('k_neg', days=1)
        self.assertEqual(view['days'][0]['tokens'], 0)

    def test_key_and_model_fanout_aggregate_into_bounded_buckets(self):
        from lib import usage_tracker as ut
        from lib.usage_tracker import record
        with patch.object(ut, '_MAX_KEYS_PER_DAY', 3), \
                patch.object(ut, '_MAX_MODELS_PER_KEY', 3):
            record('k_a', n_tokens=1, model='m_a')
            record('k_a', n_tokens=2, model='m_b')
            record('k_a', n_tokens=4, model='m_c')
            record('k_b', n_tokens=8, model='m_a')
            record('k_c', n_tokens=16, model='m_a')

            bucket = ut._state[ut._today()]
            self.assertEqual(set(bucket), {'k_a', 'k_b', '_overflow'})
            self.assertEqual(bucket['_overflow']['tokens'], 16)
            self.assertEqual(
                bucket['k_a']['by_model'],
                {'m_a': 1, 'm_b': 2, '_other': 4},
            )

    def test_loaded_state_is_bounded_and_repaired(self):
        from lib import usage_tracker as ut
        today = ut._today()
        raw = {
            'version': 1,
            'days': {
                today: {
                    'k_a': {
                        'requests': 1,
                        'tokens': 3,
                        'by_model': {'m_a': 1, 'm_b': 2},
                    },
                    'k_b': {
                        'requests': 2,
                        'tokens': 5,
                        'by_model': {'m_c': 5},
                    },
                },
                'not-a-day': {'ignored': {'requests': 999}},
            },
        }
        with open(ut._STORE_PATH, 'w', encoding='utf-8') as handle:
            json.dump(raw, handle)

        with patch.object(ut, '_MAX_KEYS_PER_DAY', 2), \
                patch.object(ut, '_MAX_MODELS_PER_KEY', 2):
            ut._ensure_loaded()
            bucket = ut._state[today]
            self.assertEqual(set(bucket), {'k_a', '_overflow'})
            self.assertEqual(bucket['_overflow']['tokens'], 5)
            self.assertEqual(
                bucket['k_a']['by_model'], {'m_a': 1, '_other': 2})

            with open(ut._STORE_PATH, encoding='utf-8') as handle:
                repaired = json.load(handle)
            self.assertEqual(repaired['days'], ut._state)
            self.assertNotIn('not-a-day', repaired['days'])


if __name__ == '__main__':
    unittest.main()
