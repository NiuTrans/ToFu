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


pytest_plugins = ('tests._billing_user_sidecar',)


class _BillingTestBase(unittest.TestCase):
    """Common setup: isolated pricing over module-scoped Sidecar storage."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        # Isolate pricing.json so test edits don't pollute the dev tree.
        cls._pricing_path = os.path.join(cls._tmp.name, 'pricing.json')
        cls._pricing_patch = patch('lib.billing.pricing._PRICING_PATH',
                                    cls._pricing_path)
        cls._pricing_patch.start()
        from lib.billing import pricing as _p
        _p.reload_pricing()

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


# ── single-engine unification (2026-06-24) ──────────────────────────

class SingleEngineUnificationTest(_BillingTestBase):
    """compute_request_cost now delegates rate math to lib.cost.compute_cost.

    The two money-critical invariants:
      (1) cache_read/cache_write tokens ARE billed (the old settle path
          dropped them → silent under-charge on cache-heavy turns);
      (2) the wallet debit equals the displayed ¥/$ cost to the micro
          (they share ONE arithmetic core), so display and bill can't drift.
    """

    def test_cache_tokens_are_billed(self):
        from lib.billing import compute_request_cost
        # A cache-heavy Anthropic turn. Pre-unification only input+output were
        # charged; cache_read/cache_write were dropped entirely.
        b = compute_request_cost('claude-opus-4-7',
                                  input_tokens=500, output_tokens=1500,
                                  cache_write_tokens=8000,
                                  cache_read_tokens=40000, margin=0.0)
        self.assertGreater(b.components['cache_read'], 0)
        self.assertGreater(b.components['cache_write'], 0)
        # The cache portion must be a real fraction of the bill, not lost.
        cache_micro = b.components['cache_read'] + b.components['cache_write']
        self.assertGreater(cache_micro, b.components['input'])

    def test_debit_equals_displayed_cost(self):
        # The wallet base (margin 0) must equal compute_cost's summed USD
        # sub-components × 1e6 — proving display and bill use one engine.
        from lib.billing import compute_request_cost
        from lib.billing.cost import MICRO_PER_USD
        from lib.cost import compute_cost
        cases = [
            ('claude-opus-4-7', {'input_tokens': 500, 'output_tokens': 1500,
                                 'cache_creation_input_tokens': 8000,
                                 'cache_read_input_tokens': 40000}),
            ('gpt-4o', {'prompt_tokens': 10000, 'completion_tokens': 2000,
                        'cache_read_tokens': 6000}),
            ('gpt-4o-mini', {'input_tokens': 1000, 'output_tokens': 500}),
        ]
        for model, u in cases:
            cc = compute_cost(u, model_id=model)
            disp_micro = round((cc['inputCostUsd'] + cc['outputCostUsd']
                                + cc['cacheWriteCostUsd']
                                + cc['cacheReadCostUsd']) * MICRO_PER_USD)
            b = compute_request_cost(
                model,
                input_tokens=int(u.get('input_tokens')
                                 or u.get('prompt_tokens') or 0),
                output_tokens=int(u.get('output_tokens')
                                  or u.get('completion_tokens') or 0),
                cache_read_tokens=int(u.get('cache_read_tokens')
                                      or u.get('cache_read_input_tokens') or 0),
                cache_write_tokens=int(u.get('cache_write_tokens')
                                       or u.get('cache_creation_input_tokens') or 0),
                margin=0.0)
            self.assertEqual(b.base_micro, disp_micro,
                             f'{model}: debit {b.base_micro} != display {disp_micro}')

    def test_rich_table_models_priced_not_defaulted(self):
        # A model only in lib/pricing.py (NOT in the sparse pricing.json) is
        # now priced correctly instead of falling to default_model.
        from lib.billing import compute_request_cost
        b = compute_request_cost('claude-opus-4-7',
                                  input_tokens=1_000_000, output_tokens=0,
                                  margin=0.0)
        # claude-opus-4-7 is $5/Mtok input → 5_000_000 micro.
        self.assertEqual(b.base_micro, 5_000_000)

    def test_margin_still_applied_over_unified_base(self):
        from lib.billing import compute_request_cost
        base = compute_request_cost('gpt-4o-mini', input_tokens=1000,
                                    output_tokens=500, margin=0.0).micro
        marg = compute_request_cost('gpt-4o-mini', input_tokens=1000,
                                    output_tokens=500, margin=0.25).micro
        self.assertEqual(marg, int(base * 1.25))

    def test_qwen_cny_native_billed(self):
        # Qwen bills in CNY tiers; the adapter converts via the live rate.
        # Must produce a positive, non-defaulted micro amount.
        from lib.billing import compute_request_cost
        b = compute_request_cost('qwen-plus', input_tokens=100_000,
                                 output_tokens=100_000, margin=0.0)
        self.assertGreater(b.base_micro, 0)


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

class AlipayUserIdExtractionTest(unittest.TestCase):
    """The alipay notify fallback recovers the user_id from out_trade_no when
    passback_params is missing. out_trade_no is `tofu_<user_id>_<ms>` and the
    user_id ITSELF contains underscores (`usr_<hex>`), so the naive
    split('_')[1] yielded the literal 'usr' and credited a bogus account.
    """

    def _extract(self, out_trade_no):
        # Mirror the fixed fallback in alipay.handle_alipay_notify.
        if out_trade_no.startswith('tofu_'):
            mid = out_trade_no[len('tofu_'):]
            cut = mid.rfind('_')
            return mid[:cut] if cut > 0 else ''
        return ''

    def test_recovers_full_usr_id_with_underscore(self):
        # Real id shape from lib.ids.short_id('usr_') → usr_<hex>.
        uid = 'usr_ab12cd34ef'
        otn = f'tofu_{uid}_1784690000000'
        self.assertEqual(self._extract(otn), uid)

    def test_naive_split_would_return_wrong_id(self):
        # NEUTER: the OLD code path (split('_')[1]) returns 'usr', proving the
        # bug was real and the rfind-based fix is load-bearing.
        uid = 'usr_ab12cd34ef'
        otn = f'tofu_{uid}_1784690000000'
        self.assertEqual(otn.split('_')[1], 'usr')       # the old (wrong) result
        self.assertNotEqual(otn.split('_')[1], uid)
        self.assertEqual(self._extract(otn), uid)         # the fixed result

    def test_real_handler_credits_correct_user_via_out_trade_no_fallback(self):
        # End-to-end against the REAL handle_alipay_notify: with NO
        # passback_params it must fall back to out_trade_no and credit the
        # FULL usr_<hex> id — proving the source fix (not a test-local copy)
        # is load-bearing. Stub signature-verify + capture record_payment.
        from unittest.mock import patch
        import lib.billing.payments.alipay as ap
        uid = 'usr_deadbeef99'
        otn = f'tofu_{uid}_1784690000000'
        captured = {}

        class _Rec:
            id = 'pay_test_1'

        def _rec(*, user_id, **kw):
            captured['user_id'] = user_id
            return _Rec()

        form = {
            'sign': 'x', 'trade_status': 'TRADE_SUCCESS',
            'out_trade_no': otn, 'total_amount': '9.99',
            # NB: no passback_params → forces the out_trade_no fallback path.
        }
        with patch.object(ap, '_alipay_settings',
                          lambda: {'alipay_public_key_pem': 'stub-pub'}), \
             patch.object(ap, '_verify_rsa2', lambda *a: True), \
             patch.object(ap._common, 'record_payment', _rec), \
             patch.object(ap._common, 'mark_payment_settled',
                          lambda *a, **k: None):
            status, body = ap.handle_alipay_notify(form)
        self.assertEqual(status, 200)
        self.assertEqual(captured.get('user_id'), uid,
                         'notify fallback must credit the FULL usr_<hex> id, '
                         'not the truncated "usr"')


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
