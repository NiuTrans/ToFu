"""tests/test_conv_affinity.py — Conversation-sticky slot routing.

Anthropic's prompt cache is keyed per API key, so a conversation must keep
landing on the SAME key round-to-round or every flip costs a full
cache_creation write. These tests verify that:
  - the first pick seeds affinity and subsequent picks reuse that key;
  - affinity is a SOFT preference that falls back when the sticky key is
    cooled down, then rebinds to the healthy key;
  - with no conv bound (or the feature disabled) behaviour is the plain
    min-score race (no regression).

See lib/llm_dispatch/conv_affinity.py for the rationale (conv mqjlcopple4o60).
"""

import os
import time
import unittest


class ConvAffinityTest(unittest.TestCase):

    def setUp(self):
        os.environ['TOFU_EPHEMERAL_PREFLIGHT'] = '0'
        os.environ['TOFU_CONV_STICKY_ROUTING'] = '1'
        from lib.llm_dispatch.factory import get_dispatcher
        from lib.llm_dispatch.slot import Slot
        from lib.llm_dispatch import conv_affinity

        conv_affinity.clear_conv_affinity()
        conv_affinity._conv_keys.clear()

        self.d = get_dispatcher()
        self.d.initialize()
        # Two slots for the SAME alias model on DIFFERENT keys — the real
        # opus-4.8 two-key shape that caused the cache thrash. key_A has a
        # slightly better (lower) latency so it wins the naive race; key_B
        # is the second key.
        self._slot_a = Slot(
            key_name='key_A', api_key='sk-a', model='sticky-model',
            capabilities={'text'}, provider_id='sankuai', latency_ema=10.0,
        )
        self._slot_b = Slot(
            key_name='key_B', api_key='sk-b', model='sticky-model',
            capabilities={'text'}, provider_id='sankuai', latency_ema=20.0,
        )
        with self.d._lock:
            self.d.slots.extend([self._slot_a, self._slot_b])

    def tearDown(self):
        from lib.llm_dispatch import conv_affinity
        conv_affinity.clear_conv_affinity()
        conv_affinity._conv_keys.clear()
        with self.d._lock:
            for s in (self._slot_a, self._slot_b):
                if s in self.d.slots:
                    self.d.slots.remove(s)
        os.environ.pop('TOFU_EPHEMERAL_PREFLIGHT', None)
        os.environ.pop('TOFU_CONV_STICKY_ROUTING', None)

    def _pick(self):
        return self.d.pick_slot(capability='text', prefer_model='sticky-model')

    def test_affinity_sticks_across_rounds(self):
        from lib.llm_dispatch import conv_affinity
        conv_affinity.set_conv_affinity('conv-1')
        # First pick seeds affinity — A wins on score.
        first = self._pick()
        self.assertEqual(first.key_name, 'key_A')
        # Even if we now make B score better, the sticky key A must win.
        self._slot_b.latency_ema = 1.0
        for _ in range(8):
            self.assertEqual(self._pick().key_name, 'key_A')

    def test_falls_back_when_sticky_key_cooled_down(self):
        from lib.llm_dispatch import conv_affinity
        conv_affinity.set_conv_affinity('conv-2')
        # Seed affinity to A.
        self.assertEqual(self._pick().key_name, 'key_A')
        # Cool A down → picker must route to B and rebind affinity to B.
        self._slot_a.cooldown_until = time.time() + 1000
        self.assertEqual(self._pick().key_name, 'key_B')
        self.assertEqual(conv_affinity.get_preferred_key('conv-2'), 'key_B')
        # With A still cooled, B remains sticky even though A would otherwise
        # be the configured first choice.
        self.assertEqual(self._pick().key_name, 'key_B')

    def test_no_conv_bound_is_plain_min_score(self):
        from lib.llm_dispatch import conv_affinity
        conv_affinity.clear_conv_affinity()
        # No conv → A wins purely on score; make B better and B wins.
        self.assertEqual(self._pick().key_name, 'key_A')
        self._slot_b.latency_ema = 1.0
        self.assertEqual(self._pick().key_name, 'key_B')

    def test_disabled_flag_ignores_affinity(self):
        from lib.llm_dispatch import conv_affinity
        os.environ['TOFU_CONV_STICKY_ROUTING'] = '0'
        conv_affinity.set_conv_affinity('conv-3')
        self.assertEqual(self._pick().key_name, 'key_A')
        # Feature off → a better B wins despite an existing sticky binding.
        conv_affinity.record_conv_key('conv-3', 'key_A')
        self._slot_b.latency_ema = 1.0
        self.assertEqual(self._pick().key_name, 'key_B')

    def test_auxiliary_model_pick_does_not_overwrite_main_model_key(self):
        """A compaction/translation model must not cause main-key A→B→A churn."""
        from lib.llm_dispatch import conv_affinity
        from lib.llm_dispatch.slot import Slot

        auxiliary = Slot(
            key_name='aux_key', api_key='sk-aux', model='affinity-aux-model',
            capabilities={'text'}, provider_id='sankuai', latency_ema=1.0,
        )
        with self.d._lock:
            self.d.slots.append(auxiliary)
        try:
            conv_affinity.set_conv_affinity('conv-aux-scope')
            self.assertEqual(self._pick().key_name, 'key_A')
            self.assertEqual(self.d.pick_slot(
                capability='text', prefer_model='affinity-aux-model').key_name,
                'aux_key')
            # B would win a fresh score race.  The main model must nevertheless
            # retain A because the auxiliary pick lives in another route scope.
            self._slot_b.latency_ema = 1.0
            self.assertEqual(self._pick().key_name, 'key_A')
        finally:
            with self.d._lock:
                if auxiliary in self.d.slots:
                    self.d.slots.remove(auxiliary)

    def test_recency_map_ttl_expiry(self):
        from lib.llm_dispatch import conv_affinity
        conv_affinity.record_conv_key('conv-4', 'key_B')
        self.assertEqual(conv_affinity.get_preferred_key('conv-4'), 'key_B')
        # Force the stored timestamp past the TTL → treated as absent.
        os.environ['TOFU_CONV_STICKY_TTL'] = '1'
        conv_affinity._conv_keys['conv-4'] = ('key_B', time.time() - 100)
        try:
            self.assertIsNone(conv_affinity.get_preferred_key('conv-4'))
        finally:
            os.environ.pop('TOFU_CONV_STICKY_TTL', None)


if __name__ == '__main__':
    unittest.main()
