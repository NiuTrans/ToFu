"""Cross-thread proofs for payment and ledger idempotency constraints."""

from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from lib.billing.payments._common import (
    mark_payment_settled,
    record_payment,
)
from lib.database import DOMAIN_SYSTEM, close_thread_db, pooled_db


pytestmark = pytest.mark.unit


def _concurrent_record(barrier, kwargs):
    try:
        barrier.wait()
        return record_payment(**kwargs)
    finally:
        close_thread_db()


def _concurrent_settle(barrier, payment_id):
    try:
        barrier.wait()
        mark_payment_settled(payment_id)
    finally:
        close_thread_db()


def test_provider_id_unique_constraint_selects_one_concurrent_winner(flask_app):
    nonce = str(time.time_ns())
    kwargs = dict(
        user_id=f'pay-user-{nonce}', provider='test',
        provider_id=f'provider-{nonce}', amount_minor=37, currency='USD')
    barrier = threading.Barrier(8)
    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(pool.map(
            lambda _: _concurrent_record(barrier, kwargs), range(8)))
    assert len({record.id for record in records}) == 1
    with pooled_db(DOMAIN_SYSTEM) as db:
        row = db.execute(
            'SELECT COUNT(*) AS n FROM billing_payments '
            'WHERE provider=? AND provider_id=?',
            ('test', kwargs['provider_id'])).fetchone()
    assert int(row['n']) == 1


def test_concurrent_settlement_credits_wallet_and_ledger_once(flask_app):
    nonce = str(time.time_ns())
    user_id = f'settle-user-{nonce}'
    provider_id = f'settle-provider-{nonce}'
    payment = record_payment(
        user_id=user_id, provider='test', provider_id=provider_id,
        amount_minor=53, currency='USD')
    barrier = threading.Barrier(8)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(
            lambda _: _concurrent_settle(barrier, payment.id), range(8)))

    with pooled_db(DOMAIN_SYSTEM) as db:
        payment_row = db.execute(
            'SELECT status FROM billing_payments WHERE id=?',
            (payment.id,)).fetchone()
        wallet_row = db.execute(
            'SELECT balance_micro FROM billing_wallets WHERE user_id=?',
            (user_id,)).fetchone()
        ledger_row = db.execute(
            "SELECT COUNT(*) AS n FROM billing_ledger WHERE user_id=? "
            "AND kind='topup' AND ref_type='payment' AND ref_id=?",
            (user_id, provider_id)).fetchone()
    assert payment_row['status'] == 'settled'
    assert int(wallet_row['balance_micro']) == payment.credit_micro
    assert int(ledger_row['n']) == 1
