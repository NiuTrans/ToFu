"""SQL payment quotes, provider binding and atomic settlement.

Entry points are registered by operation_domains.billing. Quote identity and
provider claims lock before wallet application; receipts belong to the executor."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lib.storage.errors import StorageError

import time

import orjson

from lib.storage_authority_sql.adapters.base import Session
from lib.storage_authority_sql.faults import inject_once
from ._billing_values import _decode_json, _integer, _text
from ._billing_wallets import _wallet_apply


def _payment_document(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        'id': row['id'], 'user_id': row['user_id'],
        'provider': row['provider'], 'provider_id': row['provider_id'] or '',
        'amount_minor': int(row['amount_minor']), 'currency': row['currency'],
        'credit_micro': int(row['credit_micro']), 'status': row['status'],
        'created_at': int(row['created_at']), 'settled_at': int(row['settled_at']),
        'raw': _decode_json(row['raw']),
    }


_PAYMENT_SELECT = (
    'SELECT id, user_id, provider, provider_id, amount_minor, currency, '
    'credit_micro, status, created_at, settled_at, raw FROM billing_payments')


def _payment_find(session: Session, payload: Mapping[str, Any]) -> Any:
    provider = _text(payload, 'provider', 100)
    provider_id = _text(payload, 'provider_id', 300)
    row = session.fetch_one(
        _PAYMENT_SELECT + ' WHERE provider = ? AND provider_id = ? LIMIT 1',
        (provider, provider_id))
    return _payment_document(row) if row is not None else None


def _payment_record(session: Session, payload: Mapping[str, Any]) -> Any:
    provider = _text(payload, 'provider', 100)
    provider_id = _text(payload, 'provider_id', 300, required=False)
    if provider_id:
        session.lock_key('billing.payment.provider', f'{provider}:{provider_id}')
        existing = session.fetch_one(
            _PAYMENT_SELECT + ' WHERE provider = ? AND provider_id = ? LIMIT 1',
            (provider, provider_id))
        if existing is not None:
            return {'created': False, 'payment': _payment_document(existing)}
    payment_id = _text(payload, 'id', 200)
    user_id = _text(payload, 'user_id', 200)
    session.lock_key('billing.payment', payment_id)
    if not provider_id:
        existing = session.fetch_one(_PAYMENT_SELECT + ' WHERE id = ?', (payment_id,))
        if existing is not None:
            if existing['provider'] != provider or existing['user_id'] != user_id:
                raise StorageError('database_conflict', 'Payment identity already claimed')
            return {'created': False, 'payment': _payment_document(existing)}
    status = _text(payload, 'status', 32)
    if status not in {'pending', 'settled', 'failed'} or (not provider_id and status != 'pending'):
        raise StorageError('database_protocol_error', 'Invalid payment status')
    raw = payload.get('raw') or {}
    if not isinstance(raw, Mapping):
        raise StorageError('database_protocol_error', 'Invalid payment raw document')
    values = (
        payment_id, user_id, provider, provider_id,
        _integer(payload, 'amount_minor', minimum=0),
        _text(payload, 'currency', 16),
        _integer(payload, 'credit_micro', minimum=0), status,
        int(time.time()),
        orjson.dumps(dict(raw), option=orjson.OPT_SORT_KEYS).decode('utf-8'),
    )
    session.execute(
        'INSERT INTO billing_payments(id, user_id, provider, provider_id, '
        'amount_minor, currency, credit_micro, status, created_at, settled_at, raw) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)', values)
    row = session.fetch_one(_PAYMENT_SELECT + ' WHERE id = ?', (payment_id,))
    return {'created': True, 'payment': _payment_document(row)}


def _payment_bind(session: Session, payload: Mapping[str, Any]) -> Any:
    payment_id = _text(payload, 'payment_id', 200)
    provider = _text(payload, 'provider', 100)
    provider_id = _text(payload, 'provider_id', 300)
    user_id = _text(payload, 'user_id', 200)
    amount_minor = _integer(payload, 'amount_minor', minimum=0)
    currency = _text(payload, 'currency', 16)
    legacy_session_id = _text(payload, 'legacy_session_id', 300, required=False)
    # Match record's provider-before-payment lock order for PostgreSQL too.
    session.lock_key('billing.payment.provider', f'{provider}:{provider_id}')
    session.lock_key('billing.payment', payment_id)
    row = session.fetch_one(_PAYMENT_SELECT + ' WHERE id = ?', (payment_id,))
    rejected = {'accepted': False, 'payment': None}
    if row is None:
        return rejected
    payment = _payment_document(row)
    if (payment['provider'] != provider or payment['user_id'] != user_id
            or payment['amount_minor'] != amount_minor or payment['currency'] != currency
            or payment['status'] not in {'pending', 'settled'}):
        return rejected
    if payment['provider_id']:
        if payment['provider_id'] == provider_id:
            return {'accepted': True, 'payment': payment}
        legacy_checkout = (provider == 'stripe' and payment['status'] == 'pending'
            and payment['provider_id'] == legacy_session_id
            and legacy_session_id.startswith('cs_') and len(legacy_session_id) > 3
            and provider_id.startswith('pi_') and len(provider_id) > 3
            and payment['raw'].get('checkout_session') == legacy_session_id)
        if not legacy_checkout:
            return rejected
    if payment['status'] != 'pending':
        return rejected
    claimed = session.fetch_one(
        _PAYMENT_SELECT + ' WHERE provider = ? AND provider_id = ?', (provider, provider_id))
    if claimed is not None:
        return rejected
    session.execute('UPDATE billing_payments SET provider_id = ? WHERE id = ?',
                    (provider_id, payment_id))
    inject_once('billing.payment.bind.before_commit')
    payment['provider_id'] = provider_id
    return {'accepted': True, 'payment': payment}


def _payment_settle(session: Session, payload: Mapping[str, Any]) -> Any:
    payment_id = _text(payload, 'payment_id', 200)
    session.lock_key('billing.payment', payment_id)
    row = session.fetch_one(_PAYMENT_SELECT + ' WHERE id = ?', (payment_id,))
    if row is None:
        return {'found': False, 'settled': False, 'payment': None}
    payment = _payment_document(row)
    if payment['status'] == 'settled':
        return {'found': True, 'settled': False, 'payment': payment}
    if not payment['provider_id'] or payment['status'] != 'pending':
        raise StorageError('database_conflict', 'Only bound pending payments can settle')
    now = int(time.time())
    credit = int(payment['credit_micro'])
    if credit > 0:
        wallet_result = _wallet_apply(session, {
            'user_id': payment['user_id'], 'amount_micro': credit,
            'kind': 'topup', 'ref_type': 'payment',
            'ref_id': payment['provider_id'] or payment_id,
            'note': f"{payment['provider']} payment settled",
            'ledger_id': _text(payload, 'ledger_id', 200),
            'allow_negative': False,
        })
        if wallet_result.get('insufficient'):
            raise StorageError('database_integrity', 'Payment credit was rejected')
    inject_once('billing.payment.settle.before_status')
    raw = payload.get('raw')
    if raw is not None and not isinstance(raw, Mapping):
        raise StorageError('database_protocol_error', 'Invalid payment raw document')
    if raw is None:
        session.execute(
            "UPDATE billing_payments SET status = ?, settled_at = ? "
            "WHERE id = ? AND status <> 'settled'",
            ('settled', now, payment_id))
    else:
        session.execute(
            "UPDATE billing_payments SET status = ?, settled_at = ?, raw = ? "
            "WHERE id = ? AND status <> 'settled'",
            ('settled', now, orjson.dumps(
                dict(raw), option=orjson.OPT_SORT_KEYS).decode('utf-8'), payment_id))
    updated = session.fetch_one(_PAYMENT_SELECT + ' WHERE id = ?', (payment_id,))
    return {'found': True, 'settled': True, 'payment': _payment_document(updated)}


def _payment_list(session: Session, payload: Mapping[str, Any]) -> Any:
    clauses = []
    params: list[Any] = []
    for key in ('user_id', 'provider', 'status'):
        value = _text(payload, key, 200, required=False)
        if value:
            clauses.append(f'{key} = ?')
            params.append(value)
    sql = _PAYMENT_SELECT
    if clauses:
        sql += ' WHERE ' + ' AND '.join(clauses)
    sql += ' ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?'
    params.extend((
        _integer(payload, 'limit', minimum=1, maximum=1000),
        _integer(payload, 'offset', minimum=0, maximum=10_000_000),
    ))
    return [_payment_document(row) for row in session.fetch_all(sql, tuple(params))]
