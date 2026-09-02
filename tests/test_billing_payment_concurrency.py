"""Cross-thread proofs for payment and ledger idempotency constraints."""

from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from lib.billing.payments._common import (
    mark_payment_settled,
    record_payment,
)
from lib.billing import get_balance
from lib.billing.ledger import list_entries


pytestmark = pytest.mark.unit


def _concurrent_record(barrier, kwargs):
    barrier.wait()
    return record_payment(**kwargs)


def _concurrent_settle(barrier, payment_id):
    barrier.wait()
    mark_payment_settled(payment_id)


pytest_plugins = ('tests._billing_user_sidecar',)


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
    from lib.billing.payments._common import list_payments
    rows = list_payments(provider='test')
    assert sum(row.provider_id == kwargs['provider_id'] for row in rows) == 1


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

    from lib.billing.payments._common import list_payments
    payment_row = next(row for row in list_payments(user_id=user_id)
                       if row.id == payment.id)
    ledger_rows = list_entries(user_id, kinds=['topup'])
    assert payment_row.status == 'settled'
    assert get_balance(user_id) == payment.credit_micro
    assert sum(
        row.ref_type == 'payment' and row.ref_id == provider_id
        for row in ledger_rows) == 1
