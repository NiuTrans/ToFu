"""Stripe checkout creation and authenticated payment settlement.

Entry points: create_stripe_checkout and handle_stripe_webhook. Provider HTTP
uses the shared transport; payment and wallet transactions stay in _common's
semantic storage commands. All successful event types use one PaymentIntent
identity, including delayed checkout success. stripe_events owns admission;
checkout prepares one durable quote before IO and provider identity binds once.
"""

from __future__ import annotations

import json
from typing import Tuple

from lib.error_envelope import BadRequest
from lib.log import get_logger

from . import _common
from .stripe_events import currency_code, payment_facts, verify_signature

logger = get_logger(__name__)

_STRIPE_API_BASE = 'https://api.stripe.com/v1'


def _stripe_settings() -> dict:
    value = _common._payments_settings().get('stripe')
    return value if isinstance(value, dict) else {}


def handle_stripe_webhook(payload: bytes, sig_header: str) -> Tuple[int, dict]:
    """Verify, admit and settle a received one-time payment idempotently.

    Malformed or unauthenticated input returns 400. Unpaid/irrelevant events
    return 200 without storage writes. Storage failures propagate so the HTTP
    error boundary returns a retryable failure instead of acknowledging a loss.
    """
    settings = _stripe_settings()
    secret = settings.get('webhook_secret') or ''
    if not isinstance(secret, str) or not secret:
        logger.error('[Stripe] webhook_secret not configured — rejecting')
        return 400, {'ok': False, 'error': 'stripe_not_configured'}
    if not verify_signature(payload, sig_header, secret):
        logger.warning('[Stripe] webhook signature invalid')
        return 400, {'ok': False, 'error': 'bad_signature'}
    try:
        event = json.loads(payload.decode('utf-8'))
        payment = payment_facts(event)
    except ValueError as exc:
        logger.warning('[Stripe] Malformed webhook payload: %s', exc)
        return 400, {'ok': False, 'error': f'malformed_payload: {exc}'}
    if isinstance(payment, str):
        return 200, {'ok': True, 'note': payment}
    if payment.payment_id:
        rec = _common.bind_payment(payment.payment_id, user_id=payment.user_id,
            provider='stripe', provider_id=payment.provider_id,
            amount_minor=payment.amount_minor, currency=payment.currency)
    else:
        # Legacy canonical PaymentIntent rows may finish/replay their original
        # quote. A callback never creates a missing quote from remote metadata.
        rec = _common.find_by_provider_id('stripe', payment.provider_id)
        if rec is None and payment.checkout_session_id:
            legacy = _common.find_by_provider_id('stripe', payment.checkout_session_id)
            if legacy is not None:
                rec = _common.bind_payment(legacy.id, user_id=payment.user_id,
                    provider='stripe', provider_id=payment.provider_id,
                    amount_minor=payment.amount_minor, currency=payment.currency,
                    legacy_session_id=payment.checkout_session_id)
        if rec and (rec.user_id != payment.user_id or rec.amount_minor != payment.amount_minor
                    or rec.currency != payment.currency or rec.status not in {'pending', 'settled'}):
            rec = None
    if rec is None:
        return 400, {'ok': False, 'error': 'payment_order_mismatch'}
    raw = event
    if rec.raw.get('checkout_session'):
        # Retain the locally issued legacy Session identity through PI retries.
        raw = {**event, 'checkout_session': rec.raw['checkout_session']}
    _common.mark_payment_settled(rec.id, raw=raw)
    return 200, {'ok': True, 'payment_id': rec.id,
                 'credit_micro': rec.credit_micro}


# ── Checkout-session creation (customer top-up entry point) ─────────

async def create_stripe_checkout(
    *,
    user_id: str,
    amount_minor: int,
    currency: str = 'usd',
    success_url: str,
    cancel_url: str = '',
    product_name: str = 'Tofu Relay credit top-up',
) -> Tuple[str, str]:
    """Create a Stripe Checkout Session and return ``(session_id, url)``.

    Calls Stripe's ``POST /v1/checkout/sessions`` with HTTP Basic auth
    (the secret key as the username). ``metadata.user_id`` is stamped on
    the session so the ``checkout.session.completed`` webhook can credit
    the right wallet. The same owner is stamped on the PaymentIntent.

    Args:
        user_id: Tenant user id to credit on settlement.
        amount_minor: Charge amount in the currency's smallest unit
            (cents for USD).
        currency: ISO currency code (lowercase), e.g. ``'usd'``.
        success_url: URL Stripe redirects to after a successful payment.
            Stripe requires this; ``{CHECKOUT_SESSION_ID}`` is allowed.
        cancel_url: URL for a cancelled/abandoned payment (optional).
        product_name: Line-item label shown on the Stripe-hosted page.

    Returns:
        ``(session_id, checkout_url)`` — point the customer at the URL.

    Raises:
        BadRequest: Stripe is not configured, the currency is not an ISO
            code, or Stripe rejects the request parameters (HTTP 400).
        RuntimeError: the Stripe API call fails or answers otherwise.
    """
    settings = _stripe_settings()
    secret_key = settings.get('secret_key') or ''
    webhook_secret = settings.get('webhook_secret') or ''
    if any(not isinstance(value, str) or not value.strip()
           for value in (secret_key, webhook_secret)):
        raise BadRequest(
            'Stripe not configured — populate '
            'data/config/payments.json:stripe.secret_key and stripe.webhook_secret',
            field='provider')
    if not isinstance(success_url, str) or not success_url:
        raise RuntimeError('success_url is required for Stripe Checkout')
    try:
        currency = currency_code(currency)
    except ValueError as exc:
        raise BadRequest(str(exc), field='currency') from exc
    quote = _common.prepare_payment(user_id=user_id, provider='stripe',
                                    amount_minor=amount_minor, currency=currency)

    # Stripe's API is form-encoded with bracket-nested keys.
    form = {
        'mode': 'payment',
        'success_url': success_url,
        'client_reference_id': quote.user_id,
        'metadata[user_id]': quote.user_id,
        'payment_intent_data[metadata][user_id]': quote.user_id,
        'metadata[tofu_payment_id]': quote.id,
        'payment_intent_data[metadata][tofu_payment_id]': quote.id,
        'line_items[0][quantity]': '1',
        'line_items[0][price_data][currency]': quote.currency.lower(),
        'line_items[0][price_data][unit_amount]': str(quote.amount_minor),
        'line_items[0][price_data][product_data][name]': product_name[:127],
    }
    if cancel_url:
        form['cancel_url'] = cancel_url

    from lib.http_transport.client import async_http_post
    try:
        resp = await async_http_post(
            f'{_STRIPE_API_BASE}/checkout/sessions',
            data=form,
            auth=(secret_key, ''),
            headers={'Content-Type': 'application/x-www-form-urlencoded',
                     'Idempotency-Key': quote.id},
        )
    except Exception as e:
        logger.error('[Stripe] checkout session request failed: %s', e,
                     exc_info=True)
        raise RuntimeError(f'Stripe API request failed: {e}') from e

    if resp.status_code >= 400:
        try:
            err = (resp.json().get('error') or {}).get('message') or resp.text
        except Exception as e:
            logger.debug('[Stripe] could not parse error body: %s', e)
            err = resp.text
        logger.warning('[Stripe] checkout session rejected (%s): %.300s',
                       resp.status_code, err)
        # Stripe answers 400 for unacceptable request parameters, and the
        # variable ones here (amount, currency, redirect URLs) are the customer's.
        if resp.status_code == 400:
            raise BadRequest(f'Stripe rejected checkout session: {err}')
        raise RuntimeError(f'Stripe rejected checkout session: {err}')

    try:
        body = resp.json()
    except ValueError as exc:
        raise RuntimeError('Stripe returned malformed checkout JSON') from exc
    session_id = body.get('id') if isinstance(body, dict) else None
    url = body.get('url') if isinstance(body, dict) else None
    if (not isinstance(session_id, str) or not session_id.startswith('cs_')
            or len(session_id) <= 3 or not isinstance(url, str) or not url.startswith('https://')):
        raise RuntimeError('Stripe returned an invalid checkout identity or URL')

    logger.info('[Stripe] created checkout session %s for user=%s amount=%s%s',
                session_id, quote.user_id, quote.amount_minor, quote.currency)
    return session_id, url


__all__ = ['handle_stripe_webhook', 'create_stripe_checkout']
