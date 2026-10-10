"""lib.billing.payments.alipay — Alipay async-notify handler.

For CN deployments. Handles the ``alipay.trade.page.pay`` ↔ async-
notify round-trip. Skips the order-creation half because that requires
a synchronous call to Alipay's gateway with the operator's RSA key —
:func:`create_alipay_order` returns the signed redirect URL that the
frontend points the customer at; the async-notify handler then drops
credits into the wallet when Alipay confirms the trade.

Signature scheme: RSA2 (SHA-256 with RSA, PKCS#1 v1.5). The verify
path uses the standard ``cryptography`` library if installed; if not,
we log loud and reject so the operator knows their dependency is
missing.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.parse
from typing import Tuple

from lib.log import get_logger

from . import _common

logger = get_logger(__name__)


def _alipay_settings() -> dict:
    return _common._payments_settings().get('alipay') or {}


def _build_sign_string(params: dict) -> str:
    """Alipay's canonical sign string: alpha-sorted ``k=v&...`` of all
    non-empty fields except ``sign`` and ``sign_type``."""
    items = sorted((k, v) for k, v in params.items()
                   if v not in ('', None) and k not in ('sign', 'sign_type'))
    return '&'.join(f'{k}={v}' for k, v in items)


def _verify_rsa2(sign_b64: str, signed_str: str, public_key_pem: str) -> bool:
    """Verify Alipay's RSA2 signature. Returns False if the
    cryptography lib is unavailable or anything else goes wrong."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError:
        logger.error('[Alipay] cryptography lib missing — cannot verify')
        return False
    try:
        sig = base64.b64decode(sign_b64)
        pub = serialization.load_pem_public_key(public_key_pem.encode('utf-8'))
        pub.verify(
            sig, signed_str.encode('utf-8'),
            padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception as e:
        logger.warning('[Alipay] signature verify failed: %s', e)
        return False


def _sign_rsa2(signed_str: str, private_key_pem: str) -> str:
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError:
        raise RuntimeError(
            'cryptography lib required for Alipay signing — '
            'pip install cryptography')
    priv = serialization.load_pem_private_key(
        private_key_pem.encode('utf-8'), password=None)
    sig = priv.sign(signed_str.encode('utf-8'),
                    padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode('utf-8')


# ── Order creation ──────────────────────────────────────────────────

def create_alipay_order(
    *,
    user_id: str,
    amount_yuan: float,
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
    app_id = settings.get('app_id')
    private_key_pem = settings.get('private_key_pem')
    gateway = settings.get(
        'gateway', 'https://openapi.alipay.com/gateway.do')
    if not (app_id and private_key_pem):
        raise RuntimeError('Alipay not configured — populate '
                           'data/config/payments.json:alipay.{app_id,'
                           'private_key_pem}')
    out_trade_no = f'tofu_{user_id}_{int(time.time() * 1000)}'
    biz_content = json.dumps({
        'out_trade_no': out_trade_no,
        'product_code': 'FAST_INSTANT_TRADE_PAY',
        'total_amount': f'{amount_yuan:.2f}',
        'subject': subject[:80],
        'passback_params': urllib.parse.quote(
            json.dumps({'user_id': user_id})),
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
    sign_str = _build_sign_string(params)
    params['sign'] = _sign_rsa2(sign_str, private_key_pem)
    qs = urllib.parse.urlencode(params)
    return out_trade_no, f'{gateway}?{qs}'


# ── Async-notify handler ────────────────────────────────────────────

def handle_alipay_notify(form: dict) -> Tuple[int, str]:
    """Handle an async-notify POST. Returns ``(status_code, body_text)``.

    Alipay expects the body literal ``"success"`` (lowercase, no JSON)
    on accepted notifications, anything else triggers a retry.
    """
    settings = _alipay_settings()
    pub = settings.get('alipay_public_key_pem') or ''
    if not pub:
        logger.error('[Alipay] alipay_public_key_pem not configured')
        return 400, 'fail'
    sign = form.get('sign') or ''
    if not sign:
        return 400, 'fail'
    sign_str = _build_sign_string(form)
    if not _verify_rsa2(sign, sign_str, pub):
        logger.warning('[Alipay] notify signature invalid')
        return 400, 'fail'
    trade_status = form.get('trade_status') or ''
    if trade_status not in ('TRADE_SUCCESS', 'TRADE_FINISHED'):
        # Acknowledged but not credited; e.g. WAIT_BUYER_PAY
        logger.info('[Alipay] notify trade_status=%s — not crediting',
                    trade_status)
        return 200, 'success'
    out_trade_no = form.get('out_trade_no') or ''
    total_amount = form.get('total_amount') or '0'
    try:
        amount_yuan = float(total_amount)
    except ValueError as e:
        logger.debug('[Alipay] Non-numeric total_amount %r: %s', total_amount, e)
        amount_yuan = 0.0
    amount_minor = int(round(amount_yuan * 100))  # 元 → 分
    user_id = ''
    pp = form.get('passback_params') or ''
    if pp:
        try:
            decoded = json.loads(urllib.parse.unquote(pp))
            user_id = str(decoded.get('user_id') or '')
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning('[Alipay] passback_params parse failed: %s', e)
    if not user_id:
        # Fall back to extracting from out_trade_no shape (`tofu_<uid>_<ms>`).
        # The user_id itself contains underscores (it is `usr_<hex>`), so a
        # naive split('_')[1] yields the literal 'usr' and credits a bogus
        # account. Strip the known `tofu_` prefix and the trailing `_<ms>`
        # epoch instead, so the full `usr_<hex>` id is recovered intact.
        if out_trade_no.startswith('tofu_'):
            _mid = out_trade_no[len('tofu_'):]
            _cut = _mid.rfind('_')
            user_id = _mid[:_cut] if _cut > 0 else ''
            if not user_id:
                logger.debug('[Alipay] out_trade_no parse yielded empty uid: %s',
                             out_trade_no)
    if not user_id:
        logger.error('[Alipay] notify with no user_id (out_trade_no=%s)',
                     out_trade_no)
        return 200, 'success'  # don't retry forever; manual review needed
    rec = _common.record_payment(
        user_id=user_id, provider='alipay',
        provider_id=out_trade_no, amount_minor=amount_minor,
        currency='CNY', raw=form, status='pending')
    _common.mark_payment_settled(rec.id, raw=form)
    return 200, 'success'


__all__ = ['create_alipay_order', 'handle_alipay_notify']
