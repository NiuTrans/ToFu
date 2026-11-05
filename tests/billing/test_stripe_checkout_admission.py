"""Checkout admission rejects unfulfillable requests before payment/provider IO."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from lib.billing.payments import _common, stripe
from lib.error_envelope import BadRequest

pytestmark = pytest.mark.unit


@pytest.fixture
def configuration(monkeypatch):
    value = {'stripe': {'secret_key': 'sk_test_synthetic', 'webhook_secret': 'whsec_synthetic'},
             'credit_per_minor_unit': '1'}
    monkeypatch.setattr(_common, '_payments_settings', lambda: value)
    monkeypatch.setattr(_common, 'get_storage_client',
                        lambda **kw: pytest.fail('invalid request reached payment storage'))
    return value


def create(**changes):
    arguments = dict(user_id='usr_local', amount_minor=200, currency='usd',
                     success_url='https://merchant.example.test/paid')
    arguments.update(changes)
    return asyncio.run(stripe.create_stripe_checkout(**arguments))


@pytest.mark.parametrize(('field', 'value'), [
    ('secret_key', ''), ('secret_key', True), ('webhook_secret', ''),
    ('webhook_secret', None), ('webhook_secret', True),
])
def test_both_creation_and_notification_credentials_are_required(configuration, field, value):
    configuration['stripe'][field] = value
    with pytest.raises(BadRequest, match='configured'):
        create()


@pytest.mark.parametrize('amount', [True, 0, -1, 1.5, '200', None])
def test_invalid_amount_cannot_start_checkout(configuration, amount):
    with pytest.raises(ValueError):
        create(amount_minor=amount)


@pytest.mark.parametrize('currency', [None, True, 'us', '美元元', 'u$d'])
def test_invalid_currency_cannot_start_checkout(configuration, currency):
    with pytest.raises(BadRequest) as caught:
        create(currency=currency)
    assert caught.value.field == 'currency'


@pytest.mark.parametrize('rate', [None, 0, False, 'bad', 'NaN', 'Infinity'])
def test_invalid_rate_cannot_start_checkout(configuration, rate):
    configuration['credit_per_minor_unit'] = rate
    with pytest.raises(ValueError):
        create()


@pytest.mark.parametrize('body', [None, [], {'id': 123, 'url': 'https://checkout.stripe.com/pay'},
    {'id': '', 'url': 'https://checkout.stripe.com/pay'}, {'id': 'cs_ok', 'url': []},
    {'id': 'cs_ok', 'url': 'javascript:invalid'}, {'id': 'cs_ok'}])
def test_invalid_provider_response_never_exposes_a_redirect(configuration, monkeypatch, body):
    quote = SimpleNamespace(id='pay_local', user_id='usr_local', amount_minor=200, currency='USD')
    monkeypatch.setattr(_common, 'prepare_payment', Mock(return_value=quote))
    provider = AsyncMock(return_value=SimpleNamespace(status_code=200, json=lambda: body))
    monkeypatch.setattr('lib.http_transport.client.async_http_post', provider)
    with pytest.raises(RuntimeError, match='invalid checkout'):
        create()
    assert provider.call_count == 1
