"""Stripe signature authentication and received-payment fact projection.

Entry points: verify_signature and payment_facts. Depends only on the standard
library; it reads no settings, contacts no provider and performs no storage IO.
The Stripe adapter owns decoding, responses and the common settlement commands.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import time


def verify_signature(payload: bytes, sig_header: str, secret: str,
                      *, tolerance: int = 300) -> bool:
    """Authenticate unchanged bytes against every v1 signature during rotation."""
    if not (sig_header and secret):
        return False
    timestamps, signatures = [], []
    for item in sig_header.split(','):
        name, separator, value = item.strip().partition('=')
        if not separator:
            return False
        if name == 't':
            timestamps.append(value)
        elif name == 'v1':
            signatures.append(value)
    if len(timestamps) != 1 or not signatures:
        return False
    timestamp = timestamps[0]
    if not timestamp.isascii() or not timestamp.isdecimal():
        return False
    try:
        if abs(int(timestamp) - int(time.time())) > tolerance:
            return False
    except ValueError:
        return False
    signed_payload = timestamp.encode('ascii') + b'.' + payload
    expected = hmac.new(secret.encode('utf-8'), signed_payload,
                        hashlib.sha256).hexdigest()
    return any(value.isascii() and hmac.compare_digest(expected, value)
               for value in signatures)


@dataclass(frozen=True)
class StripePayment:
    user_id: str
    provider_id: str
    amount_minor: int
    currency: str
    payment_id: str = ''
    checkout_session_id: str = ''


def currency_code(value: object) -> str:
    if (not isinstance(value, str) or len(value) != 3
            or not value.isascii() or not value.isalpha()):
        raise ValueError('expected currency code')
    return value.upper()


def payment_facts(event: dict) -> StripePayment | str:
    """Project signed provider facts, or an acknowledged no-credit reason."""
    if not isinstance(event, dict) or not isinstance(event.get('type'), str):
        raise ValueError('expected an event object with a string type')
    event_type = event['type']
    checkout = event_type in {
        'checkout.session.completed', 'checkout.session.async_payment_succeeded',
    }
    if event_type != 'payment_intent.succeeded' and not checkout:
        return 'unhandled_event'
    data = event.get('data')
    obj = data.get('object') if isinstance(data, dict) else None
    if not isinstance(obj, dict):
        raise ValueError('expected a payment object')
    if checkout:
        if obj.get('mode') != 'payment':
            return 'unsupported_checkout_mode'
        if obj.get('payment_status') != 'paid':
            return 'payment_not_paid'
    session_id = obj.get('id') if checkout else ''
    if checkout and (not isinstance(session_id, str)
                     or not session_id.startswith('cs_') or len(session_id) <= 3):
        raise ValueError('expected Checkout Session identity')
    metadata = obj.get('metadata')
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise ValueError('expected metadata object')
    user_id = metadata.get('user_id') or metadata.get('tofu_user_id') or ''
    if not isinstance(user_id, str):
        raise ValueError('expected string user identity')
    if not user_id:
        return 'no_user_id'
    payment_id = metadata.get('tofu_payment_id', '')
    if not isinstance(payment_id, str) or len(payment_id) > 200:
        raise ValueError('expected local payment identity')
    provider_id = obj.get('payment_intent') if checkout else obj.get('id')
    if checkout and isinstance(provider_id, dict):
        provider_id = provider_id.get('id')
    if (not isinstance(provider_id, str)
            or not provider_id.startswith('pi_') or len(provider_id) <= 3):
        raise ValueError('expected PaymentIntent identity')
    amount = obj.get('amount_total' if checkout else 'amount_received')
    if type(amount) is not int or amount < 0:
        raise ValueError('expected nonnegative integer received amount')
    if amount == 0:
        return 'no_payment_received'
    return StripePayment(user_id, provider_id, amount, currency_code(obj.get('currency')),
                         payment_id, session_id)
