"""lib.billing.payments._common — Helpers shared by all providers.

Owns the ``billing_payments`` row lifecycle:
  pending → settled  (success path)
  pending → failed   (provider-rejected / cancelled)

Plus the conversion from provider minor units (cents / 分) to micro-
credits, gated by an operator-tunable ratio in payments.json.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import List, Optional

from lib.config_dir import config_path
from lib.database import DOMAIN_SYSTEM, get_thread_db
from lib.ids import short_id
from lib.json_store import read_json
from lib.log import audit_log, get_logger

logger = get_logger(__name__)

_DEFAULT_CREDIT_PER_MINOR_UNIT = 1.0


def _payments_settings() -> dict:
    raw = read_json(config_path('payments.json'),
                    default={
                        'stripe': {},
                        'alipay': {},
                        'credit_per_minor_unit':
                            _DEFAULT_CREDIT_PER_MINOR_UNIT,
                    })
    if not isinstance(raw, dict):
        return {}
    return raw


def credit_per_minor_unit() -> float:
    """How many credits one minor unit of the provider currency buys."""
    s = _payments_settings()
    try:
        return float(s.get('credit_per_minor_unit')
                      or _DEFAULT_CREDIT_PER_MINOR_UNIT)
    except (TypeError, ValueError) as e:
        logger.debug('[Payments] Bad credit_per_minor_unit, using default: %s', e)
        return _DEFAULT_CREDIT_PER_MINOR_UNIT


def minor_to_micro(amount_minor: int) -> int:
    """Convert provider minor units → integer micro-credits."""
    credits = float(amount_minor) * credit_per_minor_unit()
    return int(round(credits * 1_000_000))


@dataclass(frozen=True)
class PaymentRecord:
    id: str
    user_id: str
    provider: str
    provider_id: str
    amount_minor: int
    currency: str
    credit_micro: int
    status: str  # pending | settled | failed
    created_at: int
    settled_at: int
    raw: dict

    @classmethod
    def from_row(cls, row) -> 'PaymentRecord':
        if hasattr(row, 'keys'):
            raw = row['raw']
        else:
            raw = row[10]
        try:
            raw_d = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug('[Payments] Malformed raw payload, defaulting: %s', e)
            raw_d = {}
        if hasattr(row, 'keys'):
            return cls(
                id=row['id'], user_id=row['user_id'],
                provider=row['provider'],
                provider_id=row['provider_id'] or '',
                amount_minor=int(row['amount_minor']),
                currency=row['currency'],
                credit_micro=int(row['credit_micro']),
                status=row['status'],
                created_at=int(row['created_at']),
                settled_at=int(row['settled_at']),
                raw=raw_d,
            )
        return cls(
            id=row[0], user_id=row[1], provider=row[2],
            provider_id=row[3] or '', amount_minor=int(row[4]),
            currency=row[5], credit_micro=int(row[6]),
            status=row[7], created_at=int(row[8]),
            settled_at=int(row[9]), raw=raw_d,
        )


def _new_payment_id() -> str:
    return short_id('pay_')


def find_by_provider_id(provider: str, provider_id: str) -> Optional[PaymentRecord]:
    """Idempotency lookup."""
    if not provider_id:
        return None
    db = get_thread_db(DOMAIN_SYSTEM)
    row = db.execute(
        'SELECT id, user_id, provider, provider_id, amount_minor, '
        '       currency, credit_micro, status, created_at, settled_at, raw '
        '  FROM billing_payments '
        ' WHERE provider = ? AND provider_id = ? '
        ' LIMIT 1',
        (provider, provider_id)).fetchone()
    return PaymentRecord.from_row(row) if row is not None else None


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
    """Insert a payment row. Idempotent on (provider, provider_id):
    re-call with the same id returns the existing record.
    """
    existing = find_by_provider_id(provider, provider_id)
    if existing is not None:
        return existing
    pid = _new_payment_id()
    now = int(time.time())
    credit_micro = minor_to_micro(amount_minor)
    raw_str = json.dumps(raw or {}, ensure_ascii=False, sort_keys=True)
    db = get_thread_db(DOMAIN_SYSTEM)
    db.execute(
        'INSERT INTO billing_payments '
        '  (id, user_id, provider, provider_id, amount_minor, currency, '
        '   credit_micro, status, created_at, settled_at, raw) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)',
        (pid, user_id, provider, provider_id, amount_minor, currency,
         credit_micro, status, now, raw_str))
    db.commit()
    audit_log('payment_recorded', payment_id=pid, user_id=user_id,
              provider=provider, provider_id=provider_id,
              amount_minor=amount_minor, status=status)
    return PaymentRecord(
        id=pid, user_id=user_id, provider=provider,
        provider_id=provider_id, amount_minor=amount_minor,
        currency=currency, credit_micro=credit_micro,
        status=status, created_at=now, settled_at=0,
        raw=raw or {})


def mark_payment_settled(payment_id: str, *, raw: Optional[dict] = None) -> None:
    """Flip a payment to status=settled and (idempotently) deposit the
    credits into the user's wallet. Caller is responsible for calling
    this AFTER the provider confirms successful settlement.
    """
    db = get_thread_db(DOMAIN_SYSTEM)
    row = db.execute(
        'SELECT id, user_id, provider, provider_id, amount_minor, '
        '       currency, credit_micro, status '
        '  FROM billing_payments WHERE id = ?',
        (payment_id,)).fetchone()
    if row is None:
        logger.warning('[Payments] settle: unknown payment %s', payment_id)
        return
    if hasattr(row, 'keys'):
        user_id = row['user_id']
        provider = row['provider']
        provider_id = row['provider_id'] or ''
        credit_micro = int(row['credit_micro'])
        status = row['status']
    else:
        user_id = row[1]
        provider = row[2]
        provider_id = row[3] or ''
        credit_micro = int(row[6])
        status = row[7]
    if status == 'settled':
        return  # already done; idempotent
    now = int(time.time())
    # ★ Ordering fix (lost-top-up window): deposit BEFORE flipping status.
    #   The old order (flip status=settled + commit, THEN deposit in a separate
    #   txn) had a crash window — a crash between the two lost the credit
    #   FOREVER, because on webhook redelivery the `status == 'settled'`
    #   short-circuit above returns BEFORE re-attempting the deposit. The
    #   deposit is idempotent on (user_id, kind=topup, ref_type=payment,
    #   ref_id), so doing it FIRST is safe to repeat, and the status flip only
    #   happens once the credit is durably in the wallet. New crash windows:
    #     • crash after deposit, before flip → redelivery: status≠settled →
    #       deposit repeats (idempotent no-op) → flip. Credit preserved.
    #     • crash before deposit → redelivery: status≠settled → deposit + flip.
    #   Either way the top-up is never lost.
    if credit_micro > 0:
        from lib.billing import deposit
        deposit(user_id, credit_micro, kind='topup',
                ref_type='payment', ref_id=provider_id or payment_id,
                note=f'{provider} payment settled')
    raw_update = ''
    if raw is not None:
        raw_update = json.dumps(raw, ensure_ascii=False, sort_keys=True)
        db.execute(
            'UPDATE billing_payments '
            '   SET status = ?, settled_at = ?, raw = ? '
            ' WHERE id = ?',
            ('settled', now, raw_update, payment_id))
    else:
        db.execute(
            'UPDATE billing_payments '
            '   SET status = ?, settled_at = ? '
            ' WHERE id = ?',
            ('settled', now, payment_id))
    db.commit()
    audit_log('payment_settled', payment_id=payment_id,
              user_id=user_id, provider=provider,
              credit_micro=credit_micro)


def list_payments(
    *,
    user_id: str = '',
    provider: str = '',
    status: str = '',
    limit: int = 100,
    offset: int = 0,
) -> List[PaymentRecord]:
    db = get_thread_db(DOMAIN_SYSTEM)
    where = []
    params: list = []
    if user_id:
        where.append('user_id = ?'); params.append(user_id)
    if provider:
        where.append('provider = ?'); params.append(provider)
    if status:
        where.append('status = ?'); params.append(status)
    sql = ('SELECT id, user_id, provider, provider_id, amount_minor, '
           '       currency, credit_micro, status, created_at, '
           '       settled_at, raw '
           '  FROM billing_payments')
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY created_at DESC LIMIT ? OFFSET ?'
    params.extend([int(limit), int(offset)])
    rows = db.execute(sql, tuple(params)).fetchall()
    return [PaymentRecord.from_row(r) for r in rows]


__all__ = [
    'PaymentRecord', '_payments_settings',
    'credit_per_minor_unit', 'minor_to_micro',
    'find_by_provider_id', 'record_payment',
    'mark_payment_settled', 'list_payments',
]
