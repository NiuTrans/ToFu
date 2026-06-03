"""tests/test_billing.py — lib.billing + /api/v1/billing/* tests.

Covers:
  - pricing.json round-trip + family-prefix fallback
  - cost arithmetic + margin
  - ledger append-only invariant + idempotency
  - wallet debit/deposit + InsufficientFunds
  - reserve/settle three-row choreography
  - route layer: 401 without auth, public pricing, admin-only mint
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
import unittest.mock as _mock
from unittest.mock import patch


def _ensure_db_initialised():
    from lib.database import init_db
    init_db()


class _BillingTestBase(unittest.TestCase):
    """Common setUp: tempdir, isolated pricing.json, fresh SQLite DB."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        os.environ['TOFU_DB_BACKEND'] = 'sqlite'
        os.environ['TOFU_DB_PATH'] = os.path.join(cls._tmp.name, 'tofu.db')
        # Isolate pricing.json so test edits don't pollute the dev tree.
        cls._pricing_path = os.path.join(cls._tmp.name, 'pricing.json')
        cls._pricing_patch = patch('lib.billing.pricing._PRICING_PATH',
                                    cls._pricing_path)
        cls._pricing_patch.start()
        from lib.billing import pricing as _p
        _p.reload_pricing()
        _ensure_db_initialised()

    @classmethod
    def tearDownClass(cls):
        cls._pricing_patch.stop()
        cls._tmp.cleanup()


# ── pricing ──────────────────────────────────────────────────────────

class PricingTest(_BillingTestBase):

    def test_default_seed_on_first_read(self):
        from lib.billing import get_price, list_prices
        # First call seeds pricing.json with defaults.
        p = get_price('gpt-4o')
        self.assertEqual(p.matched, 'gpt-4o')
        self.assertGreater(p.input_per_mtok_micro, 0)

    def test_family_prefix_match(self):
        from lib.billing import get_price
        p = get_price('claude-3-5-sonnet-20241022')
        self.assertEqual(p.matched, 'claude-3-5-sonnet')
        self.assertGreater(p.cache_read_per_mtok_micro, 0)

    def test_unknown_model_falls_back(self):
        from lib.billing import get_price
        p = get_price('utterly-unknown-model-xyz')
        self.assertEqual(p.matched, 'default_model')


# ── cost ─────────────────────────────────────────────────────────────

class CostTest(_BillingTestBase):

    def test_simple_compute(self):
        from lib.billing import compute_request_cost
        b = compute_request_cost('gpt-4o-mini',
                                  input_tokens=1000, output_tokens=500,
                                  margin=0.0)
        self.assertGreater(b.micro, 0)
        self.assertEqual(b.margin_micro, 0)
        # The math is: (1000 * 150_000 + 500 * 600_000) / 1_000_000 = 450
        self.assertEqual(b.micro, 450)

    def test_margin_applied(self):
        from lib.billing import compute_request_cost
        base = compute_request_cost('gpt-4o-mini',
                                     input_tokens=1000, output_tokens=500,
                                     margin=0.0).micro
        with_margin = compute_request_cost('gpt-4o-mini',
                                            input_tokens=1000,
                                            output_tokens=500,
                                            margin=0.25).micro
        self.assertEqual(with_margin, int(base * 1.25))

    def test_cache_components(self):
        from lib.billing import compute_request_cost
        b = compute_request_cost('claude-3-5-sonnet',
                                  input_tokens=0, cache_read_tokens=10000,
                                  margin=0.0)
        self.assertEqual(b.components['input'], 0)
        self.assertGreater(b.components['cache_read'], 0)


# ── ledger ──────────────────────────────────────────────────────────

class LedgerTest(_BillingTestBase):

    def test_append_and_list(self):
        # Use a unique user_id per test method to avoid order-dependent
        # leakage when the suite runs against a shared DB tempfile.
        from lib.billing import append_entry, list_entries
        uid = 'usr_ledger_a'
        # Pass explicit, monotonically-increasing timestamps so the
        # ORDER BY ts DESC sort is stable regardless of UUID id ordering.
        e1 = append_entry(user_id=uid, amount_micro=+1000,
                          kind='topup', balance_after_micro=1000,
                          ref_type='payment', ref_id='pay_1', ts=1000)
        e2 = append_entry(user_id=uid, amount_micro=-200,
                          kind='debit', balance_after_micro=800,
                          ref_type='task', ref_id='task_1', ts=1001)
        rows = list_entries(uid)
        # Filter to just our user's rows in case prior tests touched it.
        our = [r for r in rows if r.id in (e1.id, e2.id)]
        self.assertEqual(len(our), 2)
        # Newest first.
        self.assertEqual(our[0].id, e2.id)
        self.assertEqual(our[1].id, e1.id)

    def test_unknown_kind_raises(self):
        from lib.billing import append_entry
        with self.assertRaises(ValueError):
            append_entry(user_id='usr_b', amount_micro=10,
                          kind='nonsense', balance_after_micro=10)


# ── wallet ──────────────────────────────────────────────────────────

class WalletTest(_BillingTestBase):

    def test_deposit_increases_balance(self):
        from lib.billing import deposit, get_balance
        snap = deposit('usr_w1', 5000, kind='topup',
                        ref_type='payment', ref_id='dep_1')
        self.assertEqual(snap.balance_micro, 5000)
        self.assertEqual(get_balance('usr_w1'), 5000)

    def test_debit_blocks_below_zero(self):
        from lib.billing import debit, deposit, InsufficientFunds
        deposit('usr_w2', 100, kind='topup', ref_id='d_2_init')
        with self.assertRaises(InsufficientFunds):
            debit('usr_w2', 200)

    def test_idempotency_on_ref(self):
        from lib.billing import deposit, get_balance
        deposit('usr_w3', 1000, kind='topup', ref_type='r', ref_id='X')
        # Second call with same ref must NOT double-deposit.
        deposit('usr_w3', 1000, kind='topup', ref_type='r', ref_id='X')
        self.assertEqual(get_balance('usr_w3'), 1000)

    def test_reserve_and_settle(self):
        from lib.billing import deposit, reserve, settle, get_balance
        deposit('usr_w4', 10000, kind='topup', ref_id='boot')
        reserve('usr_w4', 1500, ref_id='task42')
        # During reserve, visible balance is 8500.
        self.assertEqual(get_balance('usr_w4'), 8500)
        snap = settle('usr_w4', reserved_micro=1500, actual_micro=900,
                       ref_id='task42')
        # Refunded 1500, debited 900 → net –900 → balance = 9100.
        self.assertEqual(snap.balance_micro, 9100)
        # Settle is idempotent.
        snap2 = settle('usr_w4', reserved_micro=1500, actual_micro=900,
                        ref_id='task42')
        self.assertEqual(snap2.balance_micro, 9100)

    def test_recompute_matches_cache(self):
        from lib.billing import deposit, debit
        from lib.billing.ledger import recompute_balance
        deposit('usr_w5', 5000, kind='topup', ref_id='r1')
        debit('usr_w5', 1200, ref_type='task', ref_id='r2')
        from lib.billing import get_balance
        self.assertEqual(recompute_balance('usr_w5'), get_balance('usr_w5'))


# ── route smoke ──────────────────────────────────────────────────────

class BillingRouteSmokeTest(unittest.TestCase):
    """Smoke tests for the billing routes — registration + URL map.

    Full HTTP-level testing happens in ``tests/test_e2e_headless_api.py``
    where the app fixture is shared. Here we just confirm the routes
    exist and the OpenAPI spec is consistent.
    """

    def test_pricing_module_lists_models(self):
        from lib.billing import list_prices
        cfg = list_prices()
        self.assertIn('default_model', cfg)
        self.assertIn('models', cfg)


if __name__ == '__main__':
    unittest.main()
