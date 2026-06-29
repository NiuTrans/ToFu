"""Tests for lib.translate.inflight — the per-(conv, message) in-flight guard
that prevents two translate paths from spawning duplicate work for one message
(the Phase-2 pre-spawn dedup).

The guard is keyed by stable ``_msgId`` (insert-drift-proof), with an
index-derived fallback key when no id exists, and self-expires after a TTL so a
crashed worker can't wedge a message forever.
"""

import time
import unittest

import lib.translate.inflight as ifl


class TestInflightGuard(unittest.TestCase):

    def setUp(self):
        # Each test starts from a clean registry.
        with ifl._lock:
            ifl._inflight.clear()

    def test_msg_key_prefers_id(self):
        self.assertEqual(ifl.msg_key('abc123', 5), 'abc123')
        self.assertEqual(ifl.msg_key('', 5), '#idx:5')
        self.assertEqual(ifl.msg_key(None, 0), '#idx:0')
        self.assertEqual(ifl.msg_key('', None), '')
        self.assertEqual(ifl.msg_key(None, 'notanint'), '')

    def test_first_claim_wins_second_stands_down(self):
        self.assertTrue(ifl.claim_inflight('c1', 'mid-1', 3))
        # Same id → second claim refused.
        self.assertFalse(ifl.claim_inflight('c1', 'mid-1', 3))
        # Even if the index drifted, the ID still dedups.
        self.assertFalse(ifl.claim_inflight('c1', 'mid-1', 99))

    def test_release_allows_reclaim(self):
        self.assertTrue(ifl.claim_inflight('c1', 'mid-1', 3))
        ifl.release_inflight('c1', 'mid-1', 3)
        # After release a legitimate re-translate can claim again.
        self.assertTrue(ifl.claim_inflight('c1', 'mid-1', 3))

    def test_different_messages_independent(self):
        self.assertTrue(ifl.claim_inflight('c1', 'mid-1', 1))
        self.assertTrue(ifl.claim_inflight('c1', 'mid-2', 2))   # different id
        self.assertTrue(ifl.claim_inflight('c2', 'mid-1', 1))   # different conv

    def test_index_fallback_key_when_no_id(self):
        self.assertTrue(ifl.claim_inflight('c1', '', 7))
        self.assertFalse(ifl.claim_inflight('c1', '', 7))       # same idx key
        self.assertTrue(ifl.claim_inflight('c1', '', 8))        # different idx

    def test_no_key_is_noop_always_true(self):
        # No id AND no index → guard can't key it; degrade to always-allow.
        self.assertTrue(ifl.claim_inflight('c1', '', None))
        self.assertTrue(ifl.claim_inflight('c1', '', None))

    def test_missing_conv_is_noop(self):
        self.assertTrue(ifl.claim_inflight('', 'mid-1', 1))
        self.assertFalse(ifl.is_inflight('', 'mid-1', 1))

    def test_stale_claim_is_taken_over(self):
        self.assertTrue(ifl.claim_inflight('c1', 'mid-1', 1))
        # Force the claim to look ancient.
        with ifl._lock:
            ifl._inflight[('c1', 'mid-1')] = time.time() - (ifl._INFLIGHT_TTL + 10)
        # A stale claim must NOT block a fresh one.
        self.assertTrue(ifl.claim_inflight('c1', 'mid-1', 1))

    def test_is_inflight_probe(self):
        self.assertFalse(ifl.is_inflight('c1', 'mid-1', 1))
        ifl.claim_inflight('c1', 'mid-1', 1)
        self.assertTrue(ifl.is_inflight('c1', 'mid-1', 1))
        ifl.release_inflight('c1', 'mid-1', 1)
        self.assertFalse(ifl.is_inflight('c1', 'mid-1', 1))


if __name__ == '__main__':
    unittest.main()
