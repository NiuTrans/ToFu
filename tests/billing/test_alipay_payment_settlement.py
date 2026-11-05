"""Real Alipay order/ledger parity through SQLite and both native transports."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
import threading
from types import SimpleNamespace
import uuid
from urllib.parse import urlencode

import pytest
from quart import Quart

from lib.billing import get_balance, users
from lib.billing.ledger import list_entries
from lib.billing.payments import _common, alipay
from tests.storage import test_storage_application_contract as storage_contract
from tests.support import alipay_payments as provider

pytestmark = [pytest.mark.api, pytest.mark.auth_mode('private')]
application_storage = storage_contract.application_storage
alipay_keys = provider.alipay_keys


@pytest.fixture
def application_storage_owner_id():
    return 2


@pytest.fixture
def account(application_storage, alipay_keys, monkeypatch):
    config = {'alipay': provider.settings(alipay_keys), 'credit_per_minor_unit': '1'}
    monkeypatch.setattr(_common, '_payments_settings', lambda: config)
    user = users.create_user(uuid.uuid4().hex + '@alipay.example.test')
    return user, config


def order(user):
    return alipay.create_alipay_order(user_id=user.id, amount_yuan=Decimal('9.99'),
        notify_url='https://merchant.example.test/notify', subject='豆腐充值')


def assert_paid_once(user, identity):
    assert get_balance(user.id) == 999_000_000
    entries = list_entries(user.id, kinds=['topup'])
    assert len(entries) == 1 and entries[0].ref_id == identity
    rows = _common.list_payments(user_id=user.id)
    assert len(rows) == 1 and rows[0].status == 'settled'
    assert rows[0].amount_minor == 999 and rows[0].credit_micro == 999_000_000


def test_redirect_is_valid_and_exposes_only_a_durable_order(account, alipay_keys):
    user, _ = account
    identity, url = order(user)
    params = provider.verify_redirect(alipay_keys, url)
    business = json.loads(params['biz_content'])
    assert params['app_id'] == provider.APP_ID
    assert business['out_trade_no'] == identity
    assert business['total_amount'] == '9.99'
    assert business['seller_id'] == provider.SELLER_ID
    row = _common.find_by_provider_id('alipay', identity)
    assert row and row.user_id == user.id and row.status == 'pending'
    assert row.amount_minor == 999 and row.credit_micro == 999_000_000
    assert row.raw == {'app_id': provider.APP_ID, 'seller_id': provider.SELLER_ID}
    assert get_balance(user.id) == 0 and list_entries(user.id) == []


def test_pending_order_and_frozen_rate_survive_storage_restart(account, alipay_keys):
    from lib.storage import service
    user, config = account
    identity, _ = order(user)
    runtime = service.storage_runtime()
    runtime.stop()
    runtime.start()
    config['credit_per_minor_unit'] = 'invalid-after-checkout'
    for status in ['TRADE_SUCCESS', 'TRADE_FINISHED', 'TRADE_SUCCESS']:
        assert alipay.handle_alipay_notify(provider.notify(
            alipay_keys, identity, trade_status=status)) == (200, 'success')
    assert_paid_once(user, identity)


def test_same_millisecond_orders_are_distinct(account, monkeypatch):
    user, _ = account
    # Freeze only the adapter clock; storage deadlines must use the real clock.
    monkeypatch.setattr(alipay, 'time', SimpleNamespace(
        time=lambda: 1_793_145_600.0, strftime=alipay.time.strftime))
    identities = {order(user)[0] for _ in range(6)}
    assert len(identities) == 6
    assert len(_common.list_payments(user_id=user.id)) == 6
    assert get_balance(user.id) == 0


def test_concurrent_success_notifications_credit_once(account, alipay_keys):
    user, _ = account
    identity, _ = order(user)
    forms = [provider.notify(alipay_keys, identity, notify_id=str(index),
             trade_status='TRADE_SUCCESS' if index % 2 else 'TRADE_FINISHED') for index in range(6)]
    barrier = threading.Barrier(len(forms))
    def send(form):
        barrier.wait(timeout=10)
        return alipay.handle_alipay_notify(form)
    with ThreadPoolExecutor(max_workers=len(forms)) as executor:
        assert list(executor.map(send, forms)) == [(200, 'success')] * len(forms)
    assert_paid_once(user, identity)


@pytest.mark.parametrize('operation', ['billing.payment.record', 'billing.payment.settle'])
def test_lost_committed_ack_keeps_durable_evidence_for_retry(
        account, application_storage, alipay_keys, monkeypatch, operation):
    user, _ = account
    if operation.endswith('settle'):
        identity, _ = order(user)
    command = application_storage.command
    def lost_ack(name, *args, **kwargs):
        result = command(name, *args, **kwargs)
        if name == operation:
            raise ConnectionError('synthetic committed ACK lost')
        return result
    with monkeypatch.context() as fault:
        fault.setattr(application_storage, 'command', lost_ack)
        with pytest.raises(ConnectionError, match='committed ACK lost'):
            if operation.endswith('record'):
                order(user)
            else:
                alipay.handle_alipay_notify(provider.notify(alipay_keys, identity))
    rows = _common.list_payments(user_id=user.id)
    assert len(rows) == 1
    identity = rows[0].provider_id
    assert get_balance(user.id) == (0 if operation.endswith('record') else 999_000_000)
    assert alipay.handle_alipay_notify(provider.notify(alipay_keys, identity)) == (200, 'success')
    assert_paid_once(user, identity)


def test_public_http_rejects_ambiguous_form_and_returns_plain_ack(account, alipay_keys):
    from routes.api_v1.auth import bearer_auth_before_request
    from routes.api_v1.billing import api_v1_billing_bp
    user, _ = account
    identity, _ = order(user)
    app = Quart(__name__, static_folder=None)
    app.config['TESTING'] = True
    app.before_request(bearer_auth_before_request)
    app.register_blueprint(api_v1_billing_bp)
    async def exercise():
        client = app.test_client()
        payload = urlencode(provider.notify(alipay_keys, identity))
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        rejected = await client.post('/api/v1/billing/webhooks/alipay',
            data=payload + '&total_amount=1.00', headers=headers)
        assert rejected.status_code == 400 and await rejected.get_data() == b'fail'
        assert get_balance(user.id) == 0
        accepted = await client.post('/api/v1/billing/webhooks/alipay',
            data=payload, headers=headers)
        assert accepted.status_code == 200 and await accepted.get_data() == b'success'
        assert accepted.content_type.startswith('text/plain')
    asyncio.run(exercise())
    assert_paid_once(user, identity)
