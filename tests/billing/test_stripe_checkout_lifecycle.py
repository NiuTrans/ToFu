"""Checkout quotes and provider identities share one durable payment lifecycle."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import itertools
import json
import threading
import time
from types import SimpleNamespace
import uuid

import pytest

from lib.billing import get_balance, users
from lib.billing.ledger import list_entries
from lib.billing.payments import _common, stripe
from lib.storage.errors import StorageError
from tests.support import billing_storage
from tests.support.stripe_payments import SECRET, signature

pytestmark = pytest.mark.api

application_storage = billing_storage.application_storage
application_storage_owner_id = billing_storage.application_storage_owner_id




@pytest.fixture
def checkout_account(application_storage, monkeypatch):
    config = {'stripe': {'secret_key': 'sk_test_synthetic', 'webhook_secret': SECRET},
              'credit_per_minor_unit': '1'}
    monkeypatch.setattr(_common, '_payments_settings', lambda: config)
    user = users.create_user(uuid.uuid4().hex + '@stripe-checkout.example.test')
    requests = []
    async def provider_post(url, **kwargs):
        request = dict(kwargs, before=_common.list_payments(user_id=user.id),
                       session_id='cs_' + uuid.uuid4().hex)
        requests.append(request)
        return SimpleNamespace(status_code=200, json=lambda: {
            'id': request['session_id'],
            'url': 'https://checkout.stripe.com/c/pay/' + request['session_id'],
        })
    monkeypatch.setattr('lib.http_transport.client.async_http_post', provider_post)
    return SimpleNamespace(user=user, config=config, requests=requests)


def create(account):
    return asyncio.run(stripe.create_stripe_checkout(user_id=account.user.id,
        amount_minor=200, currency='usd', success_url='https://merchant.example.test/paid'))


def paid_event(request, *, kind='payment_intent.succeeded', intent_id='pi_local', **changes):
    prefix = 'payment_intent_data[metadata][' if kind.startswith('payment_intent') else 'metadata['
    metadata = {key[len(prefix):-1]: value for key, value in request['data'].items()
                if key.startswith(prefix)}
    obj = {'id': intent_id, 'amount_received': 200, 'currency': 'usd', 'metadata': metadata}
    if kind.startswith('checkout'):
        obj.update(id=request['session_id'], payment_intent=intent_id,
                   mode='payment', payment_status='paid', amount_total=200)
    obj.update(changes)
    return {'type': kind, 'data': {'object': obj}}


def deliver(event):
    payload = json.dumps(event).encode()
    return stripe.handle_stripe_webhook(payload, signature(payload, timestamp=int(time.time())))


def assert_one_payment(account, intent_id='pi_local'):
    rows = _common.list_payments(user_id=account.user.id)
    assert len(rows) == 1 and rows[0].status == 'settled'
    assert rows[0].provider_id == intent_id and rows[0].credit_micro == 200_000_000
    assert get_balance(account.user.id) == 200_000_000
    ledger = list_entries(account.user.id, kinds=['topup'])
    assert len(ledger) == 1 and ledger[0].ref_id == intent_id


def test_quote_is_durable_before_provider_io_and_binds_both_metadata_objects(checkout_account):
    account = checkout_account
    create(account)
    request = account.requests[0]
    assert len(request['before']) == 1
    quote = request['before'][0]
    assert quote.user_id == account.user.id and quote.provider_id == ''
    assert quote.amount_minor == 200 and quote.credit_micro == 200_000_000
    assert quote.status == 'pending'
    assert request['data']['metadata[tofu_payment_id]'] == quote.id
    assert request['data']['payment_intent_data[metadata][tofu_payment_id]'] == quote.id
    assert request['headers']['Idempotency-Key'] == quote.id
    assert get_balance(account.user.id) == 0


@pytest.mark.parametrize('changed_rate', ['2', 'invalid-after-quote'])
def test_cold_settlement_uses_frozen_quote(checkout_account, changed_rate):
    from lib.storage import service
    account = checkout_account
    create(account)
    account.config['credit_per_minor_unit'] = changed_rate
    runtime = service.storage_runtime()
    runtime.stop()
    runtime.start()
    assert deliver(paid_event(account.requests[0]))[0] == 200
    assert_one_payment(account)


def test_local_storage_failure_prevents_provider_creation(checkout_account, application_storage, monkeypatch):
    def unavailable(*args, **kwargs):
        raise StorageError('database_unavailable', 'synthetic quote unavailable', retryable=True)
    monkeypatch.setattr(application_storage, 'command', unavailable)
    with pytest.raises(StorageError, match='quote unavailable'):
        create(checkout_account)
    assert checkout_account.requests == []


EVENTS = ('payment_intent.succeeded', 'checkout.session.completed',
          'checkout.session.async_payment_succeeded')


@pytest.mark.parametrize('ordering', list(itertools.permutations(EVENTS)))
def test_success_events_converge_on_one_original_quote(checkout_account, ordering):
    account = checkout_account
    create(account)
    original = _common.list_payments(user_id=account.user.id)[0].id
    for kind in [*ordering, *reversed(ordering)]:
        status, body = deliver(paid_event(account.requests[0], kind=kind))
        assert status == 200 and body['payment_id'] == original
    assert_one_payment(account)


@pytest.mark.parametrize('changes', [
    {'amount_received': 201}, {'currency': 'eur'},
    {'metadata': {'user_id': 'usr_foreign', 'tofu_payment_id': 'copied'}},
])
def test_signed_mismatched_facts_do_not_change_a_quote(checkout_account, changes):
    account = checkout_account
    create(account)
    event = paid_event(account.requests[0])
    if 'metadata' in changes:
        event['data']['object']['metadata']['user_id'] = changes['metadata']['user_id']
    else:
        event['data']['object'].update(changes)
    assert deliver(event)[0] == 400
    rows = _common.list_payments(user_id=account.user.id)
    assert len(rows) == 1 and rows[0].status == 'pending' and rows[0].provider_id == ''
    assert get_balance(account.user.id) == 0


def test_one_quote_cannot_bind_two_payment_intents(checkout_account):
    account = checkout_account
    create(account)
    assert deliver(paid_event(account.requests[0]))[0] == 200
    assert deliver(paid_event(account.requests[0], intent_id='pi_different'))[0] == 400
    assert_one_payment(account)


def test_concurrent_quotes_cannot_claim_the_same_payment_intent(checkout_account):
    account = checkout_account
    create(account)
    create(account)
    barrier = threading.Barrier(2)
    def send(request):
        barrier.wait(timeout=10)
        return deliver(paid_event(request))[0]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(send, account.requests))
    assert sorted(results) == [200, 400]
    assert get_balance(account.user.id) == 200_000_000
    rows = _common.list_payments(user_id=account.user.id)
    assert len(rows) == 2 and sum(row.status == 'settled' for row in rows) == 1
    assert len(list_entries(account.user.id, kinds=['topup'])) == 1


@pytest.mark.parametrize('operation', ['billing.payment.bind', 'billing.payment.settle'])
def test_lost_commit_ack_recovers_without_repricing_or_reminting(
        checkout_account, application_storage, monkeypatch, operation):
    from lib.storage import service
    account = checkout_account
    create(account)
    command = application_storage.command
    def lost_ack(name, *args, **kwargs):
        result = command(name, *args, **kwargs)
        if name == operation:
            raise ConnectionError('synthetic committed ACK lost')
        return result
    with monkeypatch.context() as fault:
        fault.setattr(application_storage, 'command', lost_ack)
        with pytest.raises(ConnectionError, match='committed ACK lost'):
            deliver(paid_event(account.requests[0]))
    assert get_balance(account.user.id) == (0 if operation.endswith('bind') else 200_000_000)
    runtime = service.storage_runtime()
    runtime.stop()
    runtime.start()
    account.config['credit_per_minor_unit'] = 'invalid-after-quote'
    assert deliver(paid_event(account.requests[0], kind=EVENTS[2]))[0] == 200
    assert_one_payment(account)


def test_legacy_session_quote_promotes_once_and_retains_its_evidence(checkout_account):
    account = checkout_account
    session_id = 'cs_legacy_local'
    quote = _common.record_payment(user_id=account.user.id, provider='stripe',
        provider_id=session_id, amount_minor=200, currency='USD',
        raw={'checkout_session': session_id})
    request = dict(session_id=session_id, data={'metadata[user_id]': account.user.id,
        'payment_intent_data[metadata][user_id]': account.user.id})
    account.config['credit_per_minor_unit'] = 'invalid-after-quote'
    assert deliver(paid_event(request))[0] == 400
    status, body = deliver(paid_event(request, kind=EVENTS[1]))
    assert status == 200 and body['payment_id'] == quote.id
    assert _common.find_by_provider_id('stripe', session_id) is None
    for kind in EVENTS:
        assert deliver(paid_event(request, kind=kind))[0] == 200
    assert_one_payment(account)
    assert _common.list_payments(user_id=account.user.id)[0].raw['checkout_session'] == session_id


def test_existing_canonical_intent_replays_its_original_quote(checkout_account):
    account = checkout_account
    _common.record_payment(user_id=account.user.id, provider='stripe',
        provider_id='pi_local', amount_minor=200, currency='USD')
    account.config['credit_per_minor_unit'] = 'invalid-after-quote'
    request = dict(data={'payment_intent_data[metadata][user_id]': account.user.id})
    assert deliver(paid_event(request))[0] == 200
    assert_one_payment(account)


def test_signed_callback_cannot_create_a_quote_from_user_metadata(checkout_account):
    account = checkout_account
    request = dict(data={'payment_intent_data[metadata][user_id]': account.user.id})
    assert deliver(paid_event(request))[0] == 400
    assert _common.list_payments(user_id=account.user.id) == []
    assert get_balance(account.user.id) == 0
