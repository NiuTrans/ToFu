"""lib.billing.payments.stripe — Stripe webhook handler.

Wires Stripe's ``payment_intent.succeeded`` event to the wallet
``deposit(kind='topup')`` call. Verifies the webhook signature against
``stripe.webhook_secret`` from ``data/config/payments.json`` so a
random POST to ``/api/v1/billing/webhooks/stripe`` cannot mint credits.

Customer flow
-------------

  1. Customer clicks "Top up" on the dashboard.
  2. Frontend calls ``POST /api/v1/billing/checkout`` (TODO Phase 4)
     which creates a Stripe Checkout Session with::

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


__all__ = ['handle_stripe_webhook']
