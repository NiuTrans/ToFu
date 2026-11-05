"""Real HTTP/SQLite payment proofs: delayed funds, duplicate events and lost ACKs."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import itertools
import json
import threading
from types import SimpleNamespace
import uuid

import pytest
from quart import Quart

from lib.billing import get_balance
from lib.billing.ledger import list_entries
from lib.billing.payments import _common, stripe
from tests.support.stripe_payments import NOW, SECRET, checkout, intent, signature

pytest_plugins = ('tests._billing_user_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.auth_mode('private')]


@pytest.fixture
def account(monkeypatch):
    from lib.billing.users import create_user

    monkeypatch.setattr('time.time', lambda: NOW)
    monkeypatch.setattr(_common, '_payments_settings', lambda: {
        'stripe': {'webhook_secret': SECRET}, 'credit_per_minor_unit': 1.0,
    })
    user = create_user(f'{uuid.uuid4().hex}@example.test', password='synthetic-test-password')
    quote = _common.prepare_payment(user_id=user.id, provider='stripe', amount_minor=200, currency='USD')
    return SimpleNamespace(id=user.id, payment_id=quote.id)


def messages(account):
    identity = 'pi_' + account.id
    metadata = {'user_id': account.id, 'tofu_payment_id': account.payment_id}
    return [
        intent(id=identity, metadata=metadata),
        checkout(payment_intent=identity, metadata=metadata),
        checkout(event_type='checkout.session.async_payment_succeeded',
                 payment_intent={'id': identity}, metadata=metadata),
    ]


def deliver(event):
    payload = json.dumps(event).encode()
    return stripe.handle_stripe_webhook(payload, signature(payload))


def assert_paid_once(account):
    assert get_balance(account.id) == 200_000_000
    entries = list_entries(account.id, kinds=['topup'])
    assert len(entries) == 1
    assert entries[0].ref_id == 'pi_' + account.id
    rows = _common.list_payments(user_id=account.id)
    assert len(rows) == 1
    assert rows[0].status == 'settled'
    assert rows[0].amount_minor == 200


@pytest.mark.parametrize('order', list(itertools.permutations(range(3))))
def test_all_success_event_orders_share_one_payment_and_ledger(account, order):
    events = messages(account)
    ids = []
    for index in [*order, *reversed(order)]:
        status, body = deliver(events[index])
        assert status == 200
        ids.append(body['payment_id'])
    assert len(set(ids)) == 1
    assert_paid_once(account)


def test_delayed_payment_waits_for_success_and_failed_event_never_credits(account):
    event = messages(account)[1]
    event['data']['object']['payment_status'] = 'unpaid'
    assert deliver(event)[0] == 200
    event['type'] = 'checkout.session.async_payment_failed'
    assert deliver(event)[0] == 200
    assert get_balance(account.id) == 0
    rows = _common.list_payments(user_id=account.id)
    assert len(rows) == 1 and rows[0].status == 'pending' and rows[0].provider_id == ''
    assert list_entries(account.id, kinds=['topup']) == []
    assert deliver(messages(account)[2])[0] == 200
    assert_paid_once(account)


def test_concurrent_different_success_events_settle_once(account):
    events = messages(account) * 2
    barrier = threading.Barrier(len(events))

    def send(event):
        barrier.wait(timeout=10)
        return deliver(event)

    with ThreadPoolExecutor(max_workers=len(events)) as executor:
        outcomes = list(executor.map(send, events))
    assert all(status == 200 for status, _ in outcomes)
    assert len({body['payment_id'] for _, body in outcomes}) == 1
    assert_paid_once(account)


@pytest.mark.parametrize('operation', ['billing.payment.bind', 'billing.payment.settle'])
def test_lost_ack_replays_committed_payment_without_extra_credit(account, monkeypatch, operation):
    real_client = _common.get_storage_client
    lost = False

    class LostAcknowledgement:
        def __init__(self, client):
            self.client = client

        def command(self, name, *args, **kwargs):
            nonlocal lost
            result = self.client.command(name, *args, **kwargs)
            if name == operation and not lost:
                lost = True
                raise ConnectionError('synthetic lost committed acknowledgement')
            return result

    with monkeypatch.context() as patches:
        patches.setattr(_common, 'get_storage_client',
                        lambda **kwargs: LostAcknowledgement(real_client(**kwargs)))
        with pytest.raises(ConnectionError, match='lost committed acknowledgement'):
            deliver(messages(account)[0])
    expected_balance = 0 if operation.endswith('bind') else 200_000_000
    assert get_balance(account.id) == expected_balance
    assert deliver(messages(account)[0])[0] == 200
    assert deliver(messages(account)[1])[0] == 200
    assert_paid_once(account)


def test_public_http_authenticates_bytes_and_waits_for_actual_payment(account):
    from routes.api_v1.auth import bearer_auth_before_request
    from routes.api_v1.billing import api_v1_billing_bp

    app = Quart(__name__, static_folder=None)
    app.config['TESTING'] = True
    app.before_request(bearer_auth_before_request)
    app.register_blueprint(api_v1_billing_bp)

    async def exercise():
        client = app.test_client()
        event = messages(account)[1]
        payload = json.dumps(event, indent=2).encode()
        rejected = await client.post('/api/v1/billing/webhooks/stripe', data=payload)
        assert rejected.status_code == 400
        event['data']['object']['payment_status'] = 'unpaid'
        unpaid = json.dumps(event).encode()
        pending = await client.post('/api/v1/billing/webhooks/stripe', data=unpaid,
                                    headers={'Stripe-Signature': signature(unpaid)})
        assert pending.status_code == 200
        assert get_balance(account.id) == 0
        paid = await client.post('/api/v1/billing/webhooks/stripe', data=payload,
                                 headers={'Stripe-Signature': signature(payload)})
        assert paid.status_code == 200
        assert (await paid.get_json())['payment_id']

    asyncio.run(exercise())
    assert_paid_once(account)
