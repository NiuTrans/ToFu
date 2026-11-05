"""Signed Stripe input must prove a received payment before any storage call."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lib.billing.payments import stripe
from tests.support.stripe_payments import NOW, SECRET, checkout, intent, signature

pytestmark = pytest.mark.unit
@pytest.fixture
def settlement(monkeypatch):
    monkeypatch.setattr('time.time', lambda: NOW)
    monkeypatch.setattr(stripe._common, '_payments_settings', lambda: {
        'stripe': {'webhook_secret': SECRET}, 'credit_per_minor_unit': 1.0,
    })
    record = Mock(return_value=SimpleNamespace(id='pay_synthetic', credit_micro=200_000_000,
        user_id='usr_synthetic', amount_minor=200, currency='USD', status='pending', raw={}))
    settle = Mock()
    monkeypatch.setattr(stripe._common, 'find_by_provider_id', record)
    monkeypatch.setattr(stripe._common, 'record_payment',
                        Mock(side_effect=AssertionError('callback must not invent a quote')))
    monkeypatch.setattr(stripe._common, 'mark_payment_settled', settle)
    return record, settle


def deliver(event):
    payload = json.dumps(event, ensure_ascii=False).encode()
    return stripe.handle_stripe_webhook(payload, signature(payload))


@pytest.mark.parametrize('valid_first', [True, False])
def test_rotating_secret_accepts_any_v1_signature(settlement, valid_first):
    payload = json.dumps(intent()).encode()
    valid = signature(payload).split(',', 1)[1]
    invalid = 'v1=' + '0' * 64
    candidates = [valid, invalid] if valid_first else [invalid, valid]
    status, body = stripe.handle_stripe_webhook(payload, f't={NOW},' + ','.join(candidates))
    assert status == 200
    assert body['payment_id'] == 'pay_synthetic'
    assert settlement[1].call_count == 1


@pytest.mark.parametrize('header', [
    '', 'v1=missing-time', 't=not-a-number,v1=no', f't={NOW},v0=legacy',
    f't={NOW},v1=非ASCII', f't={NOW},v1=0,broken',
])
def test_invalid_signature_is_rejected_without_storage(settlement, header):
    status, body = stripe.handle_stripe_webhook(b'{}', header)
    assert status == 400
    assert body['error'] == 'bad_signature'
    for call in settlement:
        call.assert_not_called()


@pytest.mark.parametrize('offset', [-301, 301])
def test_timestamp_outside_existing_tolerance_is_rejected(settlement, offset):
    payload = json.dumps(intent()).encode()
    status, _ = stripe.handle_stripe_webhook(payload, signature(payload, timestamp=NOW + offset))
    assert status == 400
    settlement[0].assert_not_called()


def test_duplicate_timestamps_are_ambiguous(settlement):
    payload = json.dumps(intent()).encode()
    status, _ = stripe.handle_stripe_webhook(payload, f't={NOW-500},' + signature(payload))
    assert status == 400
    settlement[0].assert_not_called()


@pytest.mark.parametrize('payload', [b'\xff', b'{', b'null', b'[]', b'1', b'"text"'])
def test_authenticated_malformed_payload_is_a_400(settlement, payload):
    status, body = stripe.handle_stripe_webhook(payload, signature(payload))
    assert status == 400
    assert body['error'].startswith('malformed_payload')
    for call in settlement:
        call.assert_not_called()


def test_signature_uses_original_bytes_not_reserialized_json(settlement):
    event = intent(metadata={'user_id': 'usr_synthetic', 'label': '豆腐'})
    payload = json.dumps(event, ensure_ascii=False, indent=2).encode()
    status, _ = stripe.handle_stripe_webhook(payload, signature(payload))
    assert status == 200
    altered = payload.replace(b'200', b'900')
    status, _ = stripe.handle_stripe_webhook(altered, signature(payload))
    assert status == 400
    assert settlement[0].call_count == 1


@pytest.mark.parametrize('payment_status', ['unpaid', 'no_payment_required', None, 'unknown'])
def test_checkout_without_received_payment_never_credits(settlement, payment_status):
    status, body = deliver(checkout(payment_status=payment_status))
    assert status == 200
    assert body['note'] == 'payment_not_paid'
    for call in settlement:
        call.assert_not_called()


@pytest.mark.parametrize('mode', ['subscription', 'setup', None])
def test_checkout_only_credits_one_time_payment_mode(settlement, mode):
    status, body = deliver(checkout(mode=mode))
    assert status == 200
    assert body['note'] == 'unsupported_checkout_mode'
    settlement[0].assert_not_called()


def test_delayed_payment_success_uses_original_intent_identity(settlement):
    event = checkout(event_type='checkout.session.async_payment_succeeded')
    status, body = deliver(event)
    assert status == 200
    assert body['payment_id'] == 'pay_synthetic'
    settlement[0].assert_called_once_with('stripe', 'pi_synthetic')
    settlement[1].assert_called_once_with('pay_synthetic', raw=event)


def test_expanded_intent_uses_same_canonical_payment_identity(settlement):
    status, _ = deliver(checkout(payment_intent={'id': 'pi_synthetic', 'object': 'payment_intent'}))
    assert status == 200
    settlement[0].assert_called_once_with('stripe', 'pi_synthetic')


@pytest.mark.parametrize('payment_intent', [None, '', 'cs_synthetic', {}, ['pi_synthetic']])
def test_missing_intent_does_not_fall_back_to_session_id(settlement, payment_intent):
    status, _ = deliver(checkout(payment_intent=payment_intent))
    assert status == 400
    for call in settlement:
        call.assert_not_called()


@pytest.mark.parametrize('amount', [None, -1, True, 12.5, '200'])
def test_invalid_received_amount_is_not_replaced_by_requested_amount(settlement, amount):
    status, _ = deliver(intent(amount_received=amount))
    assert status == 400
    settlement[0].assert_not_called()


def test_zero_received_does_not_credit_requested_amount(settlement):
    status, body = deliver(intent(amount_received=0))
    assert status == 200
    assert body['note'] == 'no_payment_received'
    settlement[0].assert_not_called()


@pytest.mark.parametrize('event', [
    {'type': []}, {'type': 'payment_intent.succeeded', 'data': []},
    {'type': 'payment_intent.succeeded', 'data': {'object': []}},
    intent(metadata=['bad']), intent(metadata={'user_id': ['usr_synthetic']}),
    intent(metadata={'user_id': 'usr_synthetic', 'tofu_payment_id': True}),
    intent(currency=None), intent(currency=[]), intent(currency='US'),
    intent(currency='美元元'), intent(id=None),
])
def test_malformed_paid_facts_are_rejected_before_storage(settlement, event):
    status, _ = deliver(event)
    assert status == 400
    for call in settlement:
        call.assert_not_called()


def test_unknown_events_and_unowned_payments_remain_acknowledged(settlement):
    status, body = deliver({'type': 'charge.refunded', 'data': {}})
    assert (status, body['note']) == (200, 'unhandled_event')
    status, body = deliver(intent(metadata={}))
    assert (status, body['note']) == (200, 'no_user_id')
    settlement[0].assert_not_called()


def test_storage_failure_is_not_misreported_as_invalid_provider_data(settlement):
    settlement[1].side_effect = RuntimeError('synthetic unavailable storage')
    with pytest.raises(RuntimeError, match='synthetic unavailable storage'):
        deliver(intent())
    settlement[0].assert_called_once()


def test_authenticated_unknown_intent_cannot_invent_a_quote(settlement):
    settlement[0].return_value = None
    assert deliver(intent())[0] == 400
    settlement[1].assert_not_called()
