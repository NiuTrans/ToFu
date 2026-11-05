"""Payment conversion must preserve exact credit units and reject invalid inputs."""

from decimal import localcontext

import pytest

from lib.billing.payments import _common

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(('amount', 'rate', 'expected'), [
    (999, '1', 999_000_000),
    (9_007_199_254_740_993, '0.000001', 9_007_199_254_740_993),
    (10_000_000, '0.123456789123456789', 1_234_567_891_235),
    (3, '0.0000005', 2),
    (5, '0.0000005', 2),
    (7, '0.0000005', 4),
    (9_223_372_036_854_775_807, '0.000001', 9_223_372_036_854_775_807),
])
def test_conversion_is_exact_and_independent_of_caller_decimal_context(
        monkeypatch, amount, rate, expected):
    monkeypatch.setattr(_common, '_payments_settings',
                        lambda: {'credit_per_minor_unit': rate})
    with localcontext() as context:
        context.prec = 3
        assert _common.minor_to_micro(amount) == expected


def test_only_absent_rate_uses_the_default(monkeypatch):
    monkeypatch.setattr(_common, '_payments_settings', lambda: {})
    assert _common.minor_to_micro(123) == 123_000_000


@pytest.mark.parametrize('rate', [None, True, False, '', 'invalid', 'NaN',
    'Infinity', '-Infinity', 0, -1, [], {}, '1e1000000', '1e-1000000'])
def test_invalid_rate_cannot_issue_a_payment(monkeypatch, rate):
    monkeypatch.setattr(_common, '_payments_settings',
                        lambda: {'credit_per_minor_unit': rate})
    monkeypatch.setattr(_common, 'get_storage_client',
                        lambda **kw: pytest.fail('invalid payment reached storage'))
    with pytest.raises(ValueError):
        _common.record_payment(user_id='usr_invalid', provider='stripe',
            provider_id='pi_invalid', amount_minor=100, currency='USD')


@pytest.mark.parametrize('amount', [True, False, None, '10', 1.5, 0, -1,
    float('nan'), float('inf'), 9_223_372_036_854_775_808])
def test_amount_requires_positive_storable_integer_minor_units(monkeypatch, amount):
    monkeypatch.setattr(_common, '_payments_settings',
                        lambda: {'credit_per_minor_unit': '0.000001'})
    with pytest.raises(ValueError):
        _common.minor_to_micro(amount)


@pytest.mark.parametrize(('amount', 'rate'), [
    (9_223_372_036_854_775_807, 1), (1, '0.0000001'),
])
def test_credit_overflow_and_rounding_to_zero_are_rejected(monkeypatch, amount, rate):
    monkeypatch.setattr(_common, '_payments_settings',
                        lambda: {'credit_per_minor_unit': rate})
    with pytest.raises(ValueError):
        _common.minor_to_micro(amount)
