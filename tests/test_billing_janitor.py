"""tests/test_billing_janitor.py — lib.billing.wallet_janitor sweep tests.

Proves the orphaned-reserve reclaim (the money-correctness gap the wallet
docstring promised) behaves correctly:

  (a) a stale orphaned reserve IS reclaimed,
  (b) a still-fresh reserve is NOT touched,
  (c) an already-settled reserve is left alone,
  (d) running the sweep twice does NOT double-release (idempotency).
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest


def _backdate_reserve(user_id: str, ref_id: str, seconds_ago: int) -> None:
    """Push the ``reserve`` ledger row for this ref into the past so the
    sweep's TTL cutoff sees it as orphaned. Mutates ts directly — the only
    place in the suite we touch the append-only ledger, and only to simulate
    the passage of time a real crash-then-wait would produce."""
    from lib.database import DOMAIN_SYSTEM, get_thread_db
    db = get_thread_db(DOMAIN_SYSTEM)
    db.execute(
        "UPDATE billing_ledger SET ts = ? "
        " WHERE user_id = ? AND ref_id = ? AND kind = 'reserve'",
        (int(time.time()) - seconds_ago, user_id, ref_id))
    db.commit()


class _JanitorTestBase(unittest.TestCase):
    """Isolated fresh SQLite DB per class (mirrors test_billing.py)."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        from lib.database import reset_sqlite_for_tests
        cls._db_snapshot = reset_sqlite_for_tests(
            os.path.join(cls._tmp.name, 'tofu.db'))

    @classmethod
    def tearDownClass(cls):
        from lib.database import restore_db_state
        restore_db_state(getattr(cls, '_db_snapshot', None))
        cls._tmp.cleanup()


class StaleReserveSweepTest(_JanitorTestBase):

    def test_stale_orphan_is_reclaimed(self):
        from lib.billing import deposit, reserve, get_balance
        from lib.billing.wallet_janitor import sweep_stale_reserves
        uid = 'usr_jan_stale'
        deposit(uid, 10000, kind='topup', ref_id='boot_stale')
        reserve(uid, 1500, ref_id='crashed_task')
        # Hold subtracts from usable balance.
        self.assertEqual(get_balance(uid), 8500)
        # Simulate the request having crashed 1h ago (well past the 30m TTL).
        _backdate_reserve(uid, 'crashed_task', 3600)

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
        reserve(uid, 1500, ref_id='done_task')
        settle(uid, reserved_micro=1500, actual_micro=900, ref_id='done_task')
        self.assertEqual(get_balance(uid), 9100)
        # Even though the reserve row is old, settle already released it.
        _backdate_reserve(uid, 'done_task', 3600)

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
        reserve(uid, 1500, ref_id='crashed_twice')
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
        reserve(uid, 1000, ref_id='aged_task')
        _backdate_reserve(uid, 'aged_task', 120)  # 2 minutes old

        # 30m default would skip it; a 60s TTL reclaims it.
        skipped = sweep_stale_reserves(ttl_seconds=1800)
        self.assertEqual(skipped['reclaimed'], 0)
        self.assertEqual(get_balance(uid), 9000)

        reclaimed = sweep_stale_reserves(ttl_seconds=60)
        self.assertEqual(reclaimed['reclaimed'], 1)
        self.assertEqual(get_balance(uid), 10000)


if __name__ == '__main__':
    unittest.main()
