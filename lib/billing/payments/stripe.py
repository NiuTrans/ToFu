"""lib.billing.payments.stripe — Stripe webhook handler.

Wires Stripe's ``payment_intent.succeeded`` event to the wallet
``deposit(kind='topup')`` call. Verifies the webhook signature against
``stripe.webhook_secret`` from ``data/config/payments.json`` so a
random POST to ``/api/v1/billing/webhooks/stripe`` cannot mint credits.

Customer flow
-------------

  1. Customer clicks "Top up" on the dashboard.
  2. Frontend calls ``POST /api/v1/billing/checkout`` (handled by
     :func:`create_stripe_checkout`) which creates a Stripe Checkout
     Session with::

         metadata.user_id = <tenant user id>

  3. Customer completes payment on Stripe-hosted page.
  4. Stripe POSTs to ``/api/v1/billing/webhooks/stripe`` with
     ``payment_intent.succeeded``.
  5. We verify, idempotently record, and credit the user's wallet.

The whole pipeline goes through ``record_payment`` +
``mark_payment_settled`` so a duplicate webhook is a no-op.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Tuple

from lib.log import get_logger

from . import _common

logger = get_logger(__name__)

_STRIPE_API_BASE = 'https://api.stripe.com/v1'


def _stripe_settings() -> dict:
    return _common._payments_settings().get('stripe') or {}


def _verify_signature(payload: bytes, sig_header: str, secret: str,
                       *, tolerance: int = 300) -> bool:
    """Verify Stripe's ``Stripe-Signature`` header.

    Stripe's header format::

        t=<timestamp>,v1=<hex sha256>[,v0=<legacy hex sha256>]

    We compute HMAC-SHA256 over ``"<timestamp>.<payload>"`` keyed by
    the webhook secret and constant-time compare against the v1 value.
    Replays older than ``tolerance`` seconds are rejected.
    """
    if not (sig_header and secret):
        return False
    try:
        parts = dict(item.split('=', 1) for item in sig_header.split(','))
    except ValueError as e:
        logger.debug('[Stripe] Malformed signature header: %s', e)
        return False
    ts = parts.get('t')
    v1 = parts.get('v1')
    if not (ts and v1):
        return False
    try:
        if abs(int(ts) - int(time.time())) > tolerance:
            logger.warning('[Stripe] webhook timestamp out of tolerance')
            return False
    except ValueError as e:
        logger.debug('[Stripe] Non-numeric timestamp in signature: %s', e)
        return False
    signed_payload = f'{ts}.{payload.decode("utf-8")}'.encode('utf-8')
    expected = hmac.new(secret.encode('utf-8'), signed_payload,
                         hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v1)


def handle_stripe_webhook(
    payload: bytes, sig_header: str,
) -> Tuple[int, dict]:
    """Process a Stripe webhook. Returns ``(status_code, body_dict)``.

    Status codes follow Stripe's expectations:
      * 200 — accepted (we always 200 for known + signed events,
              even if we already processed them, so Stripe stops
              retrying).
      * 400 — bad signature / malformed payload.
      * 500 — internal error during settlement.
    """
    settings = _stripe_settings()
    secret = settings.get('webhook_secret') or ''
    if not secret:
        logger.error('[Stripe] webhook_secret not configured — rejecting')
        return 400, {'ok': False, 'error': 'stripe_not_configured'}
    if not _verify_signature(payload, sig_header, secret):
        logger.warning('[Stripe] webhook signature invalid')
        return 400, {'ok': False, 'error': 'bad_signature'}
    try:
        event = json.loads(payload.decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning('[Stripe] Malformed webhook payload: %s', e)
        return 400, {'ok': False, 'error': f'malformed_payload: {e}'}
    etype = event.get('type', '')
    obj = (event.get('data') or {}).get('object') or {}
    if etype == 'payment_intent.succeeded':
        return _handle_payment_intent_succeeded(event, obj)
    if etype == 'checkout.session.completed':
        return _handle_checkout_session_completed(event, obj)
    # Unknown event types: 200 so Stripe doesn't retry forever.
    logger.info('[Stripe] unhandled event type: %s', etype)
    return 200, {'ok': True, 'note': 'unhandled_event'}


def _user_id_from_metadata(obj: dict) -> str:
    md = obj.get('metadata') or {}
    return str(md.get('user_id') or md.get('tofu_user_id') or '')


def _handle_payment_intent_succeeded(event, obj):
    user_id = _user_id_from_metadata(obj)
    if not user_id:
        logger.warning('[Stripe] payment_intent without user_id metadata: %s',
                       obj.get('id'))
        return 200, {'ok': True, 'note': 'no_user_id'}
    amount = int(obj.get('amount_received') or obj.get('amount') or 0)
    currency = (obj.get('currency') or 'usd').upper()
    pid = obj.get('id') or ''
    rec = _common.record_payment(
        user_id=user_id, provider='stripe', provider_id=pid,
        amount_minor=amount, currency=currency,
        raw=event, status='pending')
    _common.mark_payment_settled(rec.id, raw=event)
    return 200, {'ok': True, 'payment_id': rec.id,
                  'credit_micro': rec.credit_micro}


def _handle_checkout_session_completed(event, obj):
    """Some integrations finalize on session.completed instead of
    payment_intent.succeeded. Treat them equivalently."""
    user_id = _user_id_from_metadata(obj)
    if not user_id:
        return 200, {'ok': True, 'note': 'no_user_id'}
    amount = int(obj.get('amount_total') or 0)
    currency = (obj.get('currency') or 'usd').upper()
    pid = obj.get('payment_intent') or obj.get('id') or ''
    rec = _common.record_payment(
        user_id=user_id, provider='stripe', provider_id=pid,
        amount_minor=amount, currency=currency,
        raw=event, status='pending')
    _common.mark_payment_settled(rec.id, raw=event)
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
    the right wallet, mirroring the Alipay ``passback_params`` flow.

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
        RuntimeError: if Stripe is not configured, or the Stripe API
            rejects the request.
    """
    settings = _stripe_settings()
    secret_key = settings.get('secret_key') or ''
    if not secret_key:
        raise RuntimeError(
            'Stripe not configured — populate '
            'data/config/payments.json:stripe.secret_key')
    if not success_url:
        raise RuntimeError('success_url is required for Stripe Checkout')

    # Stripe's API is form-encoded with bracket-nested keys.
    form = {
        'mode': 'payment',
        'success_url': success_url,
        'client_reference_id': user_id,
        'metadata[user_id]': user_id,
        'payment_intent_data[metadata][user_id]': user_id,
        'line_items[0][quantity]': '1',
        'line_items[0][price_data][currency]': currency.lower(),
        'line_items[0][price_data][unit_amount]': str(int(amount_minor)),
        'line_items[0][price_data][product_data][name]': product_name[:127],
    }
    if cancel_url:
        form['cancel_url'] = cancel_url

    from lib.http_client import async_http_post
    try:
        resp = await async_http_post(
            f'{_STRIPE_API_BASE}/checkout/sessions',
            data=form,
            auth=(secret_key, ''),
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
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
        raise RuntimeError(f'Stripe rejected checkout session: {err}')

    body = resp.json()
    session_id = str(body.get('id') or '')
    url = str(body.get('url') or '')
    if not url:
        raise RuntimeError('Stripe returned a session with no checkout URL')

    # Pre-record as pending so the payment is visible before the webhook
    # lands. The webhook's record_payment is idempotent on
    # (provider, provider_id), and uses the payment_intent id, so this
    # pending row keyed on the session id is a distinct breadcrumb that
    # does not collide with the settlement row.
    try:
        _common.record_payment(
            user_id=user_id, provider='stripe',
            provider_id=session_id, amount_minor=int(amount_minor),
            currency=currency.upper(),
            raw={'checkout_session': session_id}, status='pending')
    except Exception as e:
        logger.warning('[Stripe] pre-record of checkout session failed '
                       '(non-fatal): %s', e)

    logger.info('[Stripe] created checkout session %s for user=%s amount=%s%s',
                session_id, user_id, amount_minor, currency.upper())
    return session_id, url


__all__ = ['handle_stripe_webhook', 'create_stripe_checkout']
