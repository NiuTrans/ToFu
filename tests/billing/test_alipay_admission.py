"""Authenticated Alipay facts cannot replace the merchant's durable order."""

from dataclasses import replace
import asyncio

import pytest

from lib.billing.payments import _common, alipay
from tests.support import alipay_payments as provider

pytestmark = pytest.mark.unit
alipay_keys = provider.alipay_keys


@pytest.fixture
def admission(monkeypatch, alipay_keys):
    config = provider.settings(alipay_keys)
    row = _common.PaymentRecord(id='pay_local', user_id='usr_local_with_underscores',
        provider='alipay', provider_id='order_local', amount_minor=999,
        currency='CNY', credit_micro=999_000_000, status='pending',
        created_at=0, settled_at=0,
        raw={'app_id': provider.APP_ID, 'seller_id': provider.SELLER_ID})
    state = {'row': row, 'settled': [], 'config': config}
    monkeypatch.setattr(alipay, '_alipay_settings', lambda: config)
    monkeypatch.setattr(_common, 'find_by_provider_id',
        lambda name, identity: state['row'] if name == 'alipay' and identity == row.provider_id else None)
    monkeypatch.setattr(_common, 'record_payment',
        lambda **kw: pytest.fail('callback attempted to invent a local order'))
    monkeypatch.setattr(_common, 'mark_payment_settled',
        lambda identity, **kw: state['settled'].append(identity))
    return state


@pytest.mark.parametrize('passback', ['', '{bad json', '[]', '{"user_id":"usr_other"}'])
def test_only_local_order_selects_wallet(admission, alipay_keys, passback):
    form = provider.notify(alipay_keys, passback_params=passback)
    assert alipay.handle_alipay_notify(form) == (200, 'success')
    assert admission['settled'] == ['pay_local']


@pytest.mark.parametrize(('field', 'value'), [
    ('app_id', 'other-app'), ('app_id', ''), ('seller_id', 'other-seller'),
    ('seller_id', ''), ('sign_type', 'RSA'), ('sign_type', ''),
    ('out_trade_no', 'unknown'), ('out_trade_no', ''),
    ('out_trade_no', 'tofu_usr_other_1793145600000'),
    ('trade_no', ''), ('total_amount', '9.98'), ('total_amount', '10.00'),
    ('total_amount', '9.991'), ('total_amount', 'nan'), ('total_amount', 'Infinity'),
    ('total_amount', '-9.99'), ('total_amount', '0'), ('total_amount', 'invalid'),
    ('total_amount', ''), ('total_amount', True), ('total_amount', []),
    ('total_amount', '9.99e0'), ('total_amount', ' 9.99'),
])
def test_signed_invalid_facts_never_settle(admission, alipay_keys, field, value):
    form = provider.notify(alipay_keys, **{field: value})
    assert alipay.handle_alipay_notify(form) == (400, 'fail')
    assert admission['settled'] == []


@pytest.mark.parametrize('changes', [
    {'raw': {}}, {'raw': {'app_id': 'other', 'seller_id': provider.SELLER_ID}},
    {'raw': {'app_id': provider.APP_ID, 'seller_id': 'other'}},
    {'currency': 'USD'}, {'status': 'failed'},
])
def test_order_binding_must_agree_before_settlement(admission, alipay_keys, changes):
    admission['row'] = replace(admission['row'], **changes)
    assert alipay.handle_alipay_notify(provider.notify(alipay_keys)) == (400, 'fail')
    assert admission['settled'] == []


@pytest.mark.parametrize('field', ['app_id', 'seller_id', 'alipay_public_key_pem'])
def test_missing_merchant_configuration_fails_closed(admission, alipay_keys, field):
    admission['config'].pop(field)
    assert alipay.handle_alipay_notify(provider.notify(alipay_keys)) == (400, 'fail')
    assert admission['settled'] == []


@pytest.mark.parametrize('change', ['tamper', 'base64'])
def test_actual_rsa_signature_is_required(admission, alipay_keys, change):
    form = provider.notify(alipay_keys)
    if change == 'tamper':
        form['total_amount'] = '10.00'
    else:
        form['sign'] += '!'
    assert alipay.handle_alipay_notify(form) == (400, 'fail')
    assert admission['settled'] == []


@pytest.mark.parametrize('form', [None, [], {'sign': []}, {1: 'x'}])
def test_non_form_payloads_fail_without_exceptions(admission, form):
    assert alipay.handle_alipay_notify(form) == (400, 'fail')


@pytest.mark.parametrize('status', ['WAIT_BUYER_PAY', 'TRADE_CLOSED'])
def test_nonpaid_notification_does_not_settle(admission, alipay_keys, status):
    assert alipay.handle_alipay_notify(provider.notify(
        alipay_keys, trade_status=status)) == (200, 'success')
    assert admission['settled'] == []


@pytest.mark.parametrize('boundary', ['find_by_provider_id', 'mark_payment_settled'])
def test_storage_failure_never_acknowledges_success(admission, alipay_keys, monkeypatch, boundary):
    def unavailable(*args, **kwargs):
        raise ConnectionError('synthetic storage unavailable')
    monkeypatch.setattr(_common, boundary, unavailable)
    with pytest.raises(ConnectionError, match='storage unavailable'):
        alipay.handle_alipay_notify(provider.notify(alipay_keys))


@pytest.mark.parametrize('amount', [True, None, 0, -1, 'NaN', 'Infinity', '9.991',
                                   float('nan'), float('inf')])
def test_bad_checkout_amount_never_produces_a_redirect(admission, amount):
    with pytest.raises(ValueError):
        alipay.create_alipay_order(user_id='usr_local', amount_yuan=amount,
                                  notify_url='https://merchant.example.test/notify')


@pytest.mark.parametrize('field', ['app_id', 'seller_id', 'private_key_pem', 'alipay_public_key_pem'])
def test_incomplete_configuration_never_starts_checkout(admission, field):
    from lib.error_envelope import BadRequest

    admission['config'].pop(field)
    with pytest.raises(BadRequest, match='configured'):
        alipay.create_alipay_order(user_id='usr_local', amount_yuan='9.99',
                                  notify_url='https://merchant.example.test/notify')


@pytest.mark.parametrize('provider', ['alipay', 'stripe'])
def test_checkout_route_preserves_typed_storage_failure(monkeypatch, provider):
    from quart import Quart
    from lib.billing import payments
    from lib.http_transport.error_handlers import register_http_error_handlers
    from lib.storage.errors import StorageError
    from routes.api_v1 import billing
    app = Quart(__name__, static_folder=None)
    app.config['TESTING'] = True
    register_http_error_handlers(app)
    app.register_blueprint(billing.api_v1_billing_bp)
    monkeypatch.setattr(billing, '_billing_disabled_response', lambda: None)
    monkeypatch.setattr(billing, '_resolve_target_user', lambda: 'usr_local')
    def unavailable(**kwargs):
        raise StorageError('database_unavailable', 'synthetic unavailable', retryable=True)
    entry = 'create_alipay_order' if provider == 'alipay' else 'create_stripe_checkout'
    monkeypatch.setattr(payments, entry, unavailable)
    async def exercise():
        response = await app.test_client().post(
            '/api/v1/billing/checkout', json={'provider': provider, 'amount_minor': 999})
        return response.status_code, await response.get_data(as_text=True)
    status, body = asyncio.run(exercise())
    assert status == 503 and 'database_unavailable' in body
