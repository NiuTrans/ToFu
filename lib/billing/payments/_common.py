"""Payment row lifecycle implemented as atomic semantic storage commands."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import List, Optional

from lib.config_dir import config_path
from lib.json_store import read_json
from lib.log import audit_log, get_logger
from lib.storage import get_storage_client


logger = get_logger(__name__)
_DEFAULT_CREDIT_PER_MINOR_UNIT = 1.0


def _payments_settings() -> dict:
    raw = read_json(config_path('payments.json'), default={
        'stripe': {}, 'alipay': {},
        'credit_per_minor_unit': _DEFAULT_CREDIT_PER_MINOR_UNIT,
    })
    return raw if isinstance(raw, dict) else {}


def credit_per_minor_unit() -> float:
    settings = _payments_settings()
    try:
        return float(settings.get('credit_per_minor_unit')
                     or _DEFAULT_CREDIT_PER_MINOR_UNIT)
    except (TypeError, ValueError) as exc:
        logger.debug(
            '[Payments] Bad credit_per_minor_unit, using default: %s', exc)
        return _DEFAULT_CREDIT_PER_MINOR_UNIT


def minor_to_micro(amount_minor: int) -> int:
    return int(round(float(amount_minor) * credit_per_minor_unit() * 1_000_000))


@dataclass(frozen=True)
class PaymentRecord:
    id: str
    user_id: str
    provider: str
    provider_id: str
    amount_minor: int
    currency: str
    credit_micro: int
    status: str
    created_at: int
    settled_at: int
    raw: dict

    @classmethod
    def from_row(cls, row) -> 'PaymentRecord':
        raw = row.get('raw') or {}
        if not isinstance(raw, dict):
            try:
                raw = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                raw = {}
        return cls(
            id=str(row['id']), user_id=str(row['user_id']),
            provider=str(row['provider']),
            provider_id=str(row.get('provider_id') or ''),
            amount_minor=int(row['amount_minor']), currency=str(row['currency']),
            credit_micro=int(row['credit_micro']), status=str(row['status']),
            created_at=int(row['created_at']), settled_at=int(row['settled_at']),
            raw=raw,
        )


def _stable_id(prefix: str, *parts: object) -> str:
    digest = hashlib.sha256('\0'.join(map(str, parts)).encode('utf-8')).hexdigest()
    return f'{prefix}{digest[:32]}'


def _command_id(operation: str, payload: dict) -> str:
    """Identify one concrete request while business keys handle deduplication.

    A provider payment ID is a business idempotency key: the first accepted
    payment wins even if a later webhook contains different fields.  Receipt
    IDs, however, identify an exact command replay and therefore include the
    canonical request body.
    """
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
    ).encode('utf-8')
    return _stable_id(
        'cmd_', operation, hashlib.sha256(encoded).hexdigest())


def find_by_provider_id(provider: str, provider_id: str) -> Optional[PaymentRecord]:
    if not provider_id:
        return None
    value = get_storage_client().query(
        'billing.payment.find', {
            'provider': provider, 'provider_id': provider_id,
        }, deadline=2.0)
    return PaymentRecord.from_row(value) if value is not None else None


def record_payment(
    *,
    user_id: str,
    provider: str,
    provider_id: str,
    amount_minor: int,
    currency: str,
    raw: Optional[dict] = None,
    status: str = 'pending',
) -> PaymentRecord:
    if not provider_id:
        raise ValueError('provider_id is required for an idempotent payment')
    if status not in {'pending', 'settled', 'failed'}:
        raise ValueError('invalid payment status')
    credit_micro = minor_to_micro(amount_minor)
    identity = (provider, provider_id)
    payload = {
        'id': _stable_id('pay_', *identity), 'user_id': user_id,
        'provider': provider, 'provider_id': provider_id,
        'amount_minor': int(amount_minor), 'currency': currency,
        'credit_micro': credit_micro, 'status': status,
        'raw': dict(raw or {}),
    }
    result = get_storage_client(write=True).command(
        'billing.payment.record', payload,
        _command_id('billing.payment.record', payload), deadline=5.0,
    )
    payment = PaymentRecord.from_row(result['payment'])
    if result.get('created'):
        audit_log(
            'payment_recorded', payment_id=payment.id, user_id=user_id,
            provider=provider, provider_id=provider_id,
            amount_minor=amount_minor, status=status)
    return payment


def mark_payment_settled(payment_id: str, *, raw: Optional[dict] = None) -> None:
    if not payment_id:
        raise ValueError('payment_id required')
    raw_document = dict(raw) if raw is not None else None
    raw_digest = hashlib.sha256(
        json.dumps(raw_document, ensure_ascii=False, sort_keys=True,
                   separators=(',', ':')).encode('utf-8')
        if raw_document is not None else b'').hexdigest()[:16]
    result = get_storage_client(write=True).command(
        'billing.payment.settle', {
            'payment_id': payment_id, 'raw': raw_document,
            'ledger_id': _stable_id('led_', 'payment', payment_id),
        },
        _stable_id('cmd_', 'billing.payment.settle', payment_id, raw_digest),
        deadline=5.0,
    )
    if not result.get('found'):
        logger.warning('[Payments] settle: unknown payment %s', payment_id)
        return
    if result.get('settled'):
        payment = PaymentRecord.from_row(result['payment'])
        audit_log(
            'payment_settled', payment_id=payment_id,
            user_id=payment.user_id, provider=payment.provider,
            credit_micro=payment.credit_micro)


def list_payments(
    *,
    user_id: str = '',
    provider: str = '',
    status: str = '',
    limit: int = 100,
    offset: int = 0,
) -> List[PaymentRecord]:
    rows = get_storage_client().query(
        'billing.payment.list', {
            'user_id': user_id, 'provider': provider, 'status': status,
            'limit': int(limit), 'offset': int(offset),
        }, deadline=5.0)
    return [PaymentRecord.from_row(row) for row in rows]


__all__ = [
    'PaymentRecord', '_payments_settings', 'credit_per_minor_unit',
    'find_by_provider_id', 'list_payments', 'mark_payment_settled',
    'minor_to_micro', 'record_payment',
]
