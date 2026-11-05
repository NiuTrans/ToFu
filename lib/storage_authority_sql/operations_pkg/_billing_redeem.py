"""SQL redemption-code issuance and atomic wallet credit.

Entry points are registered by operation_domains.billing. Reuses wallet apply
and preserves the pre-mark-used rollback fault boundary."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lib.storage.errors import StorageError

from lib.storage_authority_sql.adapters.base import Session
from lib.storage_authority_sql.faults import inject_once
from ._billing_values import _integer, _text
from ._billing_wallets import _wallet_apply


def _redeem_code_document(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        'code': str(row['code']),
        'amount_micro': int(row['amount_micro']),
        'batch': str(row['batch'] or ''),
        'created_by': str(row['created_by'] or ''),
        'created_at': int(row['created_at']),
        'expires_at': int(row['expires_at']),
        'redeemed_by': str(row['redeemed_by'] or ''),
        'redeemed_at': int(row['redeemed_at']),
        'note': str(row['note'] or ''),
    }


def _redeem_codes_mint(session: Session, payload: Mapping[str, Any]) -> Any:
    raw_codes = payload.get('codes')
    if (not isinstance(raw_codes, list) or not raw_codes
            or len(raw_codes) > 10_000):
        raise StorageError(
            'database_protocol_error', 'codes must contain 1..10000 items')
    codes: list[str] = []
    for raw_code in raw_codes:
        if not isinstance(raw_code, str) or not raw_code or len(raw_code) > 64:
            raise StorageError(
                'database_protocol_error', 'Invalid redemption code')
        codes.append(raw_code)
    if len(set(codes)) != len(codes):
        raise StorageError(
            'database_protocol_error', 'Redemption codes must be unique')
    amount_micro = _integer(
        payload, 'amount_micro', minimum=1, maximum=10_000_000_000_000)
    batch = _text(payload, 'batch', 80)
    created_by = _text(payload, 'created_by', 200, required=False)
    created_at = _integer(payload, 'created_at', minimum=0)
    expires_at = _integer(payload, 'expires_at', minimum=0)
    note = _text(payload, 'note', 200, required=False)
    session.lock_key('billing.redeem_codes', batch)
    for code in codes:
        session.execute(
            'INSERT INTO billing_redeem_codes('
            'code, amount_micro, batch, created_by, created_at, expires_at, '
            'redeemed_by, redeemed_at, note) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)',
            (code, amount_micro, batch, created_by, created_at, expires_at,
             '', note))
    return {'created': len(codes), 'codes': codes}


def _redeem_code_apply(session: Session, payload: Mapping[str, Any]) -> Any:
    """Atomically consume one code and credit its owner's wallet."""
    code = _text(payload, 'code', 64)
    user_id = _text(payload, 'user_id', 200)
    redeemed_at = _integer(payload, 'redeemed_at', minimum=0)
    ledger_id = _text(payload, 'ledger_id', 200)
    session.lock_key('billing.redeem_code', code)
    row = session.fetch_one(
        'SELECT code, amount_micro, batch, created_by, created_at, expires_at, '
        'redeemed_by, redeemed_at, note FROM billing_redeem_codes '
        'WHERE code = ?', (code,))
    if row is None:
        return {'status': 'not_found'}
    document = _redeem_code_document(row)
    if document['redeemed_by']:
        return {
            'status': 'already_redeemed',
            'code': document,
        }
    if document['expires_at'] and document['expires_at'] < redeemed_at:
        return {'status': 'expired', 'code': document}

    wallet_result = _wallet_apply(session, {
        'user_id': user_id,
        'amount_micro': document['amount_micro'],
        'kind': 'redeem',
        'ref_type': 'redeem_code',
        'ref_id': code,
        'note': f'redeemed code {code}',
        'ledger_id': ledger_id,
        'allow_negative': False,
        'occurred_at': redeemed_at,
    })
    if wallet_result.get('insufficient'):
        raise StorageError(
            'database_integrity', 'Redemption credit was rejected')
    inject_once('billing.redeem_code.before_mark_used')
    session.execute(
        'UPDATE billing_redeem_codes SET redeemed_by = ?, redeemed_at = ? '
        "WHERE code = ? AND (redeemed_by = '' OR redeemed_by IS NULL)",
        (user_id, redeemed_at, code))
    document['redeemed_by'] = user_id
    document['redeemed_at'] = redeemed_at
    return {
        'status': 'redeemed',
        'code': document,
        'wallet': wallet_result['wallet'],
    }


def _redeem_codes_list(session: Session, payload: Mapping[str, Any]) -> Any:
    batch = _text(payload, 'batch', 80, required=False)
    status = _text(payload, 'status', 32)
    if status not in {'all', 'redeemed', 'unredeemed'}:
        raise StorageError(
            'database_protocol_error', 'Invalid redemption-code status')
    clauses: list[str] = []
    params: list[Any] = []
    if batch:
        clauses.append('batch = ?')
        params.append(batch)
    if status == 'redeemed':
        clauses.append("redeemed_by <> ''")
    elif status == 'unredeemed':
        clauses.append("(redeemed_by = '' OR redeemed_by IS NULL)")
    sql = (
        'SELECT code, amount_micro, batch, created_by, created_at, expires_at, '
        'redeemed_by, redeemed_at, note FROM billing_redeem_codes')
    if clauses:
        sql += ' WHERE ' + ' AND '.join(clauses)
    sql += ' ORDER BY created_at DESC, code LIMIT ? OFFSET ?'
    params.extend((
        _integer(payload, 'limit', minimum=1, maximum=1000),
        _integer(payload, 'offset', minimum=0, maximum=10_000_000),
    ))
    return [
        _redeem_code_document(row)
        for row in session.fetch_all(sql, tuple(params))
    ]
