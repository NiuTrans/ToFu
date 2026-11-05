"""Alipay protocol admission and RSA2 signing, without configuration or storage IO.

sign_page_pay signs requests; admit_notification authenticates scalar provider
fields and returns immutable paid facts (or None for an authenticated nonpaid
state). The adapter alone joins these facts with the durable local order.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import re

from lib.log import get_logger
from .amounts import yuan_to_minor

logger = get_logger(__name__)


def _signing_content(params: dict, *, notification: bool = True) -> str:
    """Page-pay signs sign_type; notifications exclude it as well as sign."""
    excluded = {'sign', 'sign_type'} if notification else {'sign'}
    items = sorted((k, v) for k, v in params.items()
                   if v not in ('', None) and k not in excluded)
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
        sig = base64.b64decode(sign_b64, validate=True)
        pub = serialization.load_pem_public_key(public_key_pem.encode('utf-8'))
        pub.verify(
            sig, signed_str.encode('utf-8'),
            padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception as e:
        logger.warning('[Alipay] signature verify failed: %s', e)
        return False


def sign_page_pay(params: dict, private_key_pem: str) -> str:
    signed_str = _signing_content(params, notification=False)
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


@dataclass(frozen=True)
class AlipayPayment:
    provider_id: str
    amount_minor: int
    app_id: str
    seller_id: str


def admit_notification(form: dict, *, app_id: str, seller_id: str,
                       public_key_pem: str) -> AlipayPayment | None:
    if not isinstance(form, dict) or any(not isinstance(key, str) or
            not isinstance(value, str) for key, value in form.items()):
        raise ValueError('notification fields must be scalar strings')
    sign = form.get('sign', '')
    if not sign or form.get('sign_type') != 'RSA2':
        raise ValueError('RSA2 signature required')
    if not _verify_rsa2(sign, _signing_content(form), public_key_pem):
        raise ValueError('invalid notification signature')
    if form.get('app_id') != app_id or form.get('seller_id') != seller_id:
        raise ValueError('notification merchant mismatch')
    if form.get('trade_status') not in ('TRADE_SUCCESS', 'TRADE_FINISHED'):
        return None
    out_trade_no = form.get('out_trade_no', '')
    trade_no = form.get('trade_no', '')
    total_amount = form.get('total_amount', '')
    if not (0 < len(out_trade_no) <= 64 and 0 < len(trade_no) <= 64):
        raise ValueError('paid notification requires order and trade IDs')
    if not re.fullmatch(r'[0-9]+(?:\.[0-9]{1,2})?', total_amount):
        raise ValueError('total_amount must contain whole CNY cents')
    return AlipayPayment(out_trade_no, yuan_to_minor(total_amount), app_id, seller_id)
