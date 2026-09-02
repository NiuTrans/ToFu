"""tests/test_rate_limit_api.py — lib.rate_limit_api unit tests."""

import time
import unittest

import pytest

import lib.rate_limit_api as rate_limit_api
from lib.api_keys import AuthContext
from lib.rate_limit_api import (
    RateDecision, apply_headers, check_request, record_tokens,
)


pytestmark = pytest.mark.unit


def _buckets() -> dict:
    """The CURRENT module-level bucket dict.

    Must be dereferenced through the module object: ``test_epic_a_backpressure``
    deliberately ``importlib.reload()``s ``lib.rate_limit_api`` (to re-read its
    env knobs), and a reload RE-BINDS the module-global ``_state`` to a fresh
    dict. A ``from ... import _state`` here would keep pointing at the OLD
    dict — ``setUp`` would clear a dict nobody reads, and per-test isolation
    would silently depend on which files the xdist worker ran first (the
    CI-only ``'rpm' != 'tpd'`` / ``False is not true`` failures).
    """
    return rate_limit_api._state


class RateLimitTest(unittest.TestCase):

    def setUp(self):
        _buckets().clear()

    def _ctx(self, *, rpm=60, tpd=0, key_id='k_test'):
        return AuthContext(key_id=key_id, name='test',
                           scopes=frozenset({'chat'}),
                           rate_limit_rpm=rpm, rate_limit_tpd=tpd)

    def test_unauthenticated_is_unlimited(self):
        d = check_request(None)
        self.assertTrue(d.allowed)

    def test_zero_limits_allow_everything(self):
        ctx = self._ctx(rpm=0, tpd=0)
        for _ in range(1000):
            self.assertTrue(check_request(ctx).allowed)

    def test_rpm_exhaustion(self):
        ctx = self._ctx(rpm=3, tpd=0)
        self.assertTrue(check_request(ctx).allowed)
        self.assertTrue(check_request(ctx).allowed)
        self.assertTrue(check_request(ctx).allowed)
        d = check_request(ctx)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, 'rpm')
        self.assertGreater(d.retry_after_s, 0)
        # Header values are sane.
        self.assertEqual(d.rpm_limit, 3)
        self.assertEqual(d.rpm_remaining, 0)

    def test_rpm_refill(self):
        ctx = self._ctx(rpm=60)  # 1 token/sec
        # Drain the bucket.
        bucket = _buckets().get(ctx.key_id) or {}
        for _ in range(60):
            self.assertTrue(check_request(ctx).allowed)
        self.assertFalse(check_request(ctx).allowed)
        # Manually advance the bucket time.
        _buckets()[ctx.key_id]['rpm'].last_refill -= 5
        # 5 seconds at 1 token/sec → 5 fresh tokens available.
        for _ in range(5):
            self.assertTrue(check_request(ctx).allowed)
        self.assertFalse(check_request(ctx).allowed)

    def test_tpd_decrement_via_record_tokens(self):
        ctx = self._ctx(rpm=60, tpd=1000)
        check_request(ctx)
        record_tokens(ctx.key_id, 200)
        d = check_request(ctx)
        self.assertTrue(d.allowed)
        self.assertLessEqual(d.tpd_remaining, 800)

    def test_tpd_zero_blocks(self):
        ctx = self._ctx(rpm=60, tpd=100)
        # Prime the bucket via a normal request first so rpm is full.
        check_request(ctx)
        record_tokens(ctx.key_id, 100, rpm_limit=60, tpd_limit=100)
        d = check_request(ctx)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, 'tpd')

    def test_apply_headers(self):
        class Resp:
            headers: dict = {}
            def __init__(self):
                self.headers = {}
        d = RateDecision(allowed=True,
                         rpm_limit=60, rpm_remaining=42,
                         tpd_limit=10000, tpd_remaining=9500)
        r = Resp()
        apply_headers(r, d)
        self.assertEqual(r.headers['X-RateLimit-Limit-Requests'], '60')
        self.assertEqual(r.headers['X-RateLimit-Remaining-Requests'], '42')
        self.assertEqual(r.headers['X-RateLimit-Limit-Tokens'], '10000')
        self.assertEqual(r.headers['X-RateLimit-Remaining-Tokens'], '9500')

    def test_apply_headers_retry_after(self):
        class Resp:
            headers: dict = {}
            def __init__(self):
                self.headers = {}
        d = RateDecision(allowed=False, reason='rpm', retry_after_s=2.5,
                         rpm_limit=60, rpm_remaining=0)
        r = Resp()
        apply_headers(r, d)
        self.assertEqual(r.headers['Retry-After'], '3')


if __name__ == '__main__':
    unittest.main()
