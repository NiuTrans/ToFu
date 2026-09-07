"""tests/test_billing_janitor.py — lib.billing.wallet_janitor sweep tests.

Proves the orphaned-reserve reclaim (the money-correctness gap the wallet
docstring promised) behaves correctly:

  (a) a stale orphaned reserve IS reclaimed,
  (b) a still-fresh reserve is NOT touched,
  (c) an already-settled reserve is left alone,
  (d) running the sweep twice does NOT double-release (idempotency).
"""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch


pytest_plugins = ('tests._billing_user_sidecar',)


def _backdate_reserve(user_id: str, ref_id: str, seconds_ago: int) -> None:
    """Seed an old hold without mutating the append-only ledger."""
    # The production ledger is append-only; tests seed an old reservation via
    # the semantic command instead of mutating its timestamp after the fact.
    # Callers invoke this helper before reserve() and it creates that hold.
    from lib.storage import get_storage_client
    from lib.billing.wallet import _stable_id
    get_storage_client(write=True).command(
        'billing.wallet.apply', {
            'user_id': user_id, 'amount_micro': -1500, 'kind': 'reserve',
            'ref_type': 'reserve', 'ref_id': ref_id, 'note': 'crash fixture',
            'allow_negative': False,
            'ledger_id': _stable_id('led_', user_id, 'reserve', ref_id),
            'occurred_at': int(time.time()) - seconds_ago,
        }, f'test:old-reserve:{user_id}:{ref_id}', deadline=5.0)


class _JanitorTestBase(unittest.TestCase):
    """Runs against the module-scoped billing Sidecar."""


class StaleReserveSweepTest(_JanitorTestBase):

    def test_stale_orphan_is_reclaimed(self):
        from lib.billing import deposit, reserve, get_balance
        from lib.billing.wallet_janitor import sweep_stale_reserves
        uid = 'usr_jan_stale'
        deposit(uid, 10000, kind='topup', ref_id='boot_stale')
        # Seed the hold at its original time; append-only ledgers are never
        # backdated with UPDATE.
        _backdate_reserve(uid, 'crashed_task', 3600)
        # Hold subtracts from usable balance.
        self.assertEqual(get_balance(uid), 8500)
        # Simulate the request having crashed 1h ago (well past the 30m TTL).

        summary = sweep_stale_reserves()
        self.assertTrue(summary['ok'])
        self.assertEqual(summary['reclaimed'], 1)
        self.assertEqual(summary['reclaimed_micro'], 1500)
        self.assertEqual(summary['errors'], 0)
        # Hold released → balance restored.
        self.assertEqual(get_balance(uid), 10000)

    def test_fresh_reserve_is_not_touched(self):
        from lib.billing import deposit, reserve, get_balance
        from lib.billing.wallet_janitor import sweep_stale_reserves
        uid = 'usr_jan_fresh'
        deposit(uid, 10000, kind='topup', ref_id='boot_fresh')
        reserve(uid, 2000, ref_id='inflight_task')  # ts = now, fresh
        self.assertEqual(get_balance(uid), 8000)

        summary = sweep_stale_reserves()  # default 30m TTL
        self.assertTrue(summary['ok'])
        self.assertEqual(summary['reclaimed'], 0)
        # The in-flight hold must survive — releasing it would let a live
        # request over-spend.
        self.assertEqual(get_balance(uid), 8000)

    def test_settled_reserve_is_left_alone(self):
        from lib.billing import deposit, reserve, settle, get_balance
        from lib.billing.wallet_janitor import sweep_stale_reserves
        uid = 'usr_jan_settled'
        deposit(uid, 10000, kind='topup', ref_id='boot_settled')
        _backdate_reserve(uid, 'done_task', 3600)
        settle(uid, reserved_micro=1500, actual_micro=900, ref_id='done_task')
        self.assertEqual(get_balance(uid), 9100)
        # Even though the reserve row is old, settle already released it.

        summary = sweep_stale_reserves()
        self.assertTrue(summary['ok'])
        self.assertEqual(summary['reclaimed'], 0)
        # No spurious second release → balance unchanged.
        self.assertEqual(get_balance(uid), 9100)

    def test_double_sweep_does_not_double_release(self):
        from lib.billing import deposit, reserve, get_balance
        from lib.billing.wallet_janitor import sweep_stale_reserves
        uid = 'usr_jan_double'
        deposit(uid, 10000, kind='topup', ref_id='boot_double')
        _backdate_reserve(uid, 'crashed_twice', 3600)

        first = sweep_stale_reserves()
        self.assertEqual(first['reclaimed'], 1)
        self.assertEqual(get_balance(uid), 10000)

        # Second sweep: the release row now exists, so the ref is no longer
        # orphaned. Nothing reclaimed, balance unchanged.
        second = sweep_stale_reserves()
        self.assertEqual(second['reclaimed'], 0)
        self.assertEqual(second['candidates'], 0)
        self.assertEqual(get_balance(uid), 10000)

    def test_explicit_ttl_arg_overrides_default(self):
        from lib.billing import deposit, reserve, get_balance
        from lib.billing.wallet_janitor import sweep_stale_reserves
        uid = 'usr_jan_ttl'
        deposit(uid, 10000, kind='topup', ref_id='boot_ttl')
        from lib.storage import get_storage_client
        from lib.billing.wallet import _stable_id
        get_storage_client(write=True).command(
            'billing.wallet.apply', {
                'user_id': uid, 'amount_micro': -1000, 'kind': 'reserve',
                'ref_type': 'reserve', 'ref_id': 'aged_task',
                'note': 'aged fixture', 'allow_negative': False,
                'ledger_id': _stable_id('led_', uid, 'reserve', 'aged_task'),
                'occurred_at': int(time.time()) - 120,
            }, f'test:old-reserve:{uid}:aged_task', deadline=5.0)

        # 30m default would skip it; a 60s TTL reclaims it.
        skipped = sweep_stale_reserves(ttl_seconds=1800)
        self.assertEqual(skipped['reclaimed'], 0)
        self.assertEqual(get_balance(uid), 9000)

        reclaimed = sweep_stale_reserves(ttl_seconds=60)
        self.assertEqual(reclaimed['reclaimed'], 1)
        self.assertEqual(get_balance(uid), 10000)

    def test_stale_reserve_for_running_task_is_not_reclaimed(self):
        from lib.billing import deposit, get_balance
        from lib.billing.wallet_janitor import sweep_stale_reserves
        uid = 'usr_jan_running'
        deposit(uid, 10000, kind='topup', ref_id='boot_running')
        _backdate_reserve(uid, 'long_task', 3600)

        with patch(
                'lib.billing.wallet_janitor._is_task_still_running',
                return_value=True):
            summary = sweep_stale_reserves()

        self.assertEqual(summary['reclaimed'], 0)
        self.assertEqual(summary['skipped_running'], 1)
        self.assertEqual(get_balance(uid), 8500)


class ReserveReclaimPolicyTest(unittest.TestCase):

    def test_only_active_multi_user_billing_enables_periodic_reclaim(self):
        from lib.billing.wallet_janitor import reserve_reclaim_enabled

        with patch('lib.auth_mode.is_multi_user', return_value=True), \
             patch('lib.relay_config.billing_enabled', return_value=True), \
             patch.dict('os.environ', {'TOFU_BILLING_JANITOR': '1'}):
            self.assertTrue(reserve_reclaim_enabled())
        with patch('lib.auth_mode.is_multi_user', return_value=False), \
             patch('lib.relay_config.billing_enabled', return_value=True):
            self.assertFalse(reserve_reclaim_enabled())
        with patch('lib.auth_mode.is_multi_user', return_value=True), \
             patch('lib.relay_config.billing_enabled', return_value=False):
            self.assertFalse(reserve_reclaim_enabled())
        with patch('lib.auth_mode.is_multi_user', return_value=True), \
             patch('lib.relay_config.billing_enabled', return_value=True), \
             patch.dict('os.environ', {'TOFU_BILLING_JANITOR': '0'}):
            self.assertFalse(reserve_reclaim_enabled())


if __name__ == '__main__':
    unittest.main()
