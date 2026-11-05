"""Alipay page-pay orders and authenticated callbacks over shared settlement.

Create stores the merchant/order/credit binding before returning a signed URL.
Notify proves provider facts against that order; only _common owns storage and
atomic wallet/ledger settlement. Missing cryptography refuses payment handling.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import uuid
from decimal import Decimal
from typing import Tuple

from lib.error_envelope import BadRequest
from lib.log import get_logger

from . import _common, alipay_protocol
from .amounts import yuan_to_minor

logger = get_logger(__name__)


def _alipay_settings() -> dict:
    value = _common._payments_settings().get('alipay')
    return value if isinstance(value, dict) else {}


# ── Order creation ──────────────────────────────────────────────────

def create_alipay_order(
    *,
    user_id: str,
    amount_yuan: Decimal | str | int | float,
    subject: str = 'Tofu Relay credit top-up',
    notify_url: str,
    return_url: str = '',
) -> Tuple[str, str]:
    """Build a signed Alipay page-pay redirect URL.

    Returns ``(out_trade_no, redirect_url)``. The frontend should send
    the customer to ``redirect_url``; on completion Alipay POSTs the
    async-notify to ``notify_url`` (which routes to
    :func:`handle_alipay_notify`).
    """
    settings = _alipay_settings()
    required = ('app_id', 'seller_id', 'private_key_pem', 'alipay_public_key_pem')
    if any(not isinstance(settings.get(key), str) or not settings[key].strip()
           for key in required):
        raise BadRequest('Alipay not configured — populate '
                         'data/config/payments.json:alipay.{' + ','.join(required) + '}',
                         field='provider')
    app_id, seller_id = settings['app_id'], settings['seller_id']
    gateway = settings.get(
        'gateway', 'https://openapi.alipay.com/gateway.do')
    amount_minor = yuan_to_minor(amount_yuan)
    out_trade_no = 'tofu_' + uuid.uuid4().hex
    biz_content = json.dumps({
        'out_trade_no': out_trade_no,
        'product_code': 'FAST_INSTANT_TRADE_PAY',
        'total_amount': f'{amount_minor // 100}.{amount_minor % 100:02d}',
        'seller_id': seller_id,
        'subject': subject[:80],
    }, ensure_ascii=False, sort_keys=True)
    params = {
        'app_id': app_id,
        'method': 'alipay.trade.page.pay',
        'format': 'JSON',
        'charset': 'utf-8',
        'sign_type': 'RSA2',
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
        'version': '1.0',
        'notify_url': notify_url,
        'biz_content': biz_content,
    }
    if return_url:
        params['return_url'] = return_url
    params['sign'] = alipay_protocol.sign_page_pay(params, settings['private_key_pem'])
    qs = urllib.parse.urlencode(params)
    _common.record_payment(user_id=user_id, provider='alipay',
        provider_id=out_trade_no, amount_minor=amount_minor, currency='CNY',
        raw={'app_id': app_id, 'seller_id': seller_id}, status='pending')
    return out_trade_no, f'{gateway}?{qs}'


# ── Async-notify handler ────────────────────────────────────────────

def handle_alipay_notify(form: dict) -> Tuple[int, str]:
    """Handle an async-notify POST. Returns ``(status_code, body_text)``.

    Alipay expects the body literal ``"success"`` (lowercase, no JSON)
    on accepted notifications, anything else triggers a retry.
    """
    settings = _alipay_settings()
    required = ('app_id', 'seller_id', 'alipay_public_key_pem')
    if any(not isinstance(settings.get(key), str) or not settings[key].strip()
           for key in required):
        logger.error('[Alipay] app_id, seller_id and alipay_public_key_pem must be configured')
        return 400, 'fail'
    try:
        payment = alipay_protocol.admit_notification(form, app_id=settings['app_id'],
            seller_id=settings['seller_id'], public_key_pem=settings['alipay_public_key_pem'])
    except ValueError as exc:
        logger.warning('[Alipay] notify rejected: %s', exc)
        return 400, 'fail'
    if payment is None:
        return 200, 'success'
    rec = _common.find_by_provider_id('alipay', payment.provider_id)
    if (rec is None or rec.currency != 'CNY' or rec.amount_minor != payment.amount_minor
            or rec.status not in {'pending', 'settled'}
            or rec.raw.get('app_id') != payment.app_id
            or rec.raw.get('seller_id') != payment.seller_id):
        logger.warning('[Alipay] notify does not match a local order')
        return 400, 'fail'
    _common.mark_payment_settled(rec.id, raw=form)
    return 200, 'success'


__all__ = ['create_alipay_order', 'handle_alipay_notify']
