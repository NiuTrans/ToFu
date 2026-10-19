"""tests/rate_limit_api/test_rate_limit_api.py — lib.rate_limit.rate_limit_api unit tests."""

import time
import unittest

import pytest

import lib.rate_limit.rate_limit_api as rate_limit_api
from lib.api_keys import AuthContext
from lib.rate_limit.rate_limit_api import (
    RateDecision, apply_headers, check_request, record_tokens,
)


pytestmark = pytest.mark.unit


def _buckets() -> dict:
    """The CURRENT module-level bucket dict.

    Must be dereferenced through the module object: ``test_epic_a_backpressure``
    deliberately ``importlib.reload()``s ``lib.rate_limit.rate_limit_api`` (to re-read its
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

    def test_historical_rpm_does_not_refuse_work(self):
        from unittest.mock import patch
        ctx = self._ctx(rpm=1)
        with patch.object(rate_limit_api, '_shared_bucket_store',
                          side_effect=AssertionError('request-rate backend entered')):
            for _ in range(4097):
                decision = check_request(ctx, request_cost=1000)
                self.assertTrue(decision.allowed)
                self.assertEqual(decision.rpm_limit, 0)
        self.assertEqual(_buckets(), {})

    def test_spending_peek_refills_before_deciding(self):
        from unittest.mock import patch
        ctx = self._ctx(rpm=1, tpd=86400)
        with patch.object(rate_limit_api.time, 'time', return_value=1000):
            self.assertTrue(check_request(ctx).allowed)
            record_tokens(ctx.key_id, 86410, tpd_limit=86400)
            self.assertFalse(check_request(ctx).allowed)
        with patch.object(rate_limit_api.time, 'time', return_value=1011):
            decision = check_request(ctx)
            self.assertTrue(decision.allowed)
            self.assertEqual(decision.tpd_remaining, 1)

    def test_tpd_decrement_via_record_tokens(self):
        ctx = self._ctx(rpm=60, tpd=1000)
        check_request(ctx)
        record_tokens(ctx.key_id, 200)
        d = check_request(ctx)
        self.assertTrue(d.allowed)
        self.assertLessEqual(d.tpd_remaining, 800)

    def test_tpd_zero_blocks(self):
        from unittest.mock import patch
        ctx = self._ctx(rpm=60, tpd=100)
        with patch.object(rate_limit_api.time, 'time', return_value=1000):
            check_request(ctx)
            record_tokens(ctx.key_id, 100, rpm_limit=60, tpd_limit=100)
            decision = check_request(ctx)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, 'tpd')

    def test_spending_debt_survives_unrelated_credential_churn(self):
        from unittest.mock import patch
        first = self._ctx(key_id='original-debt', tpd=100)
        with patch.object(rate_limit_api.time, 'time', return_value=1000):
            self.assertTrue(check_request(first).allowed)
            record_tokens(first.key_id, 150, tpd_limit=100)
            for index in range(4097):
                self.assertTrue(check_request(self._ctx(key_id=f'other-{index}', tpd=100)).allowed)
            decision = check_request(first)
            self.assertFalse(decision.allowed)
            self.assertEqual(decision.reason, 'tpd')
            self.assertEqual(_buckets()[first.key_id]['tpd'].tokens, -50)
            self.assertEqual(rate_limit_api.api_rate_limit_stats(), {'entries': 4098})

    def test_posthoc_spending_creates_debt_without_preflight_state(self):
        from unittest.mock import patch
        with patch.object(rate_limit_api.time, 'time', return_value=1000):
            record_tokens('posthoc', 150, tpd_limit=100)
            self.assertEqual(_buckets()['posthoc']['tpd'].tokens, -50)
            self.assertFalse(check_request(self._ctx(key_id='posthoc', tpd=100)).allowed)

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
        self.assertNotIn('X-RateLimit-Limit-Requests', r.headers)
        self.assertNotIn('X-RateLimit-Remaining-Requests', r.headers)
        self.assertEqual(r.headers['X-RateLimit-Limit-Tokens'], '10000')
        self.assertEqual(r.headers['X-RateLimit-Remaining-Tokens'], '9500')

    def test_apply_headers_retry_after(self):
        class Resp:
            headers: dict = {}
            def __init__(self):
                self.headers = {}
        d = RateDecision(allowed=False, reason='tpd', retry_after_s=2.5,
                         tpd_limit=100, tpd_remaining=0)
        r = Resp()
        apply_headers(r, d)
        self.assertEqual(r.headers['Retry-After'], '3')


if __name__ == '__main__':
    unittest.main()
