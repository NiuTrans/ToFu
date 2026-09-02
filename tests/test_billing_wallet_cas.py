#!/usr/bin/env python3
"""Regression: wallet debit uses an ATOMIC conditional UPDATE (CAS).

Board epic pt_a4c9d33e, owner answer "B — Go". The debit path previously did
read-modify-write: read balance in Python, compute an ABSOLUTE new balance,
UPSERT it — guarded only by an in-process ``threading.Lock``. Across worker
PROCESSES that lock is meaningless, so two debits could each read the same
balance and both write, overdrawing (the same TOCTOU class the board lease
CAS closed).

``_apply_signed`` now moves the balance via ``_conditional_apply``: a single
``UPDATE billing_wallets SET balance_micro = balance_micro + ?
WHERE user_id = ? AND balance_micro + ? >= 0``. The funds check lives in the
SQL WHERE clause (evaluated against the CURRENT row under the row lock) and the
delta is RELATIVE, so a debit can neither overdraw nor clobber a concurrent
writer. rowcount==0 → the check failed → InsufficientFunds.

These tests pin that behavior. NEUTER: weakening the WHERE ``>= 0`` guard lets
a debit overdraw → the concurrency test goes red.
"""
from __future__ import annotations

import threading
import unittest

import pytest

pytestmark = pytest.mark.unit
pytest_plugins = ('tests._billing_user_sidecar',)


class WalletCASTest(unittest.TestCase):

    def test_many_debits_never_overdraw(self):
        """30 debits of 10 against a balance of 100: exactly 10 land, the rest
        are refused, and the balance is EXACTLY 0 — never negative. Under the
        atomic WHERE-clause funds check the balance can never be overdrawn even
        as concurrent workers race. (NEUTER: weakening the >=0 guard makes all
        30 'succeed' and the balance goes to -200.)"""
        from lib.billing import deposit, debit, get_balance, InsufficientFunds
        deposit('usr_cas3', 100, kind='topup', ref_id='seed3')
        results = []
        rlock = threading.Lock()

        def worker(i):
            try:
                debit('usr_cas3', 10, ref_type='task', ref_id=f'd{i}')
                ok = True
            except InsufficientFunds:
                ok = False
            with rlock:
                results.append(ok)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(results), 10,
                         'exactly 10 debits of 10 fit in a balance of 100')
        self.assertEqual(get_balance('usr_cas3'), 0)
        self.assertGreaterEqual(get_balance('usr_cas3'), 0,
                                'balance must never go negative')

    def test_debit_raises_insufficient_and_preserves_balance(self):
        from lib.billing import deposit, debit, get_balance, InsufficientFunds
        deposit('usr_cas4', 50, kind='topup', ref_id='seed4')
        with self.assertRaises(InsufficientFunds):
            debit('usr_cas4', 200, ref_type='task', ref_id='big')
        self.assertEqual(get_balance('usr_cas4'), 50)


if __name__ == '__main__':
    unittest.main()
