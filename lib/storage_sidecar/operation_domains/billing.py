"""Money-critical wallet, ledger, reserve, and payment operations."""

from __future__ import annotations

from collections.abc import Mapping
import time
from typing import Any

import orjson

from lib.storage.errors import StorageError
from lib.storage_sidecar import operations as ops
from lib.storage_sidecar.adapters.base import Session
from lib.storage_sidecar.faults import inject_once


LEDGER_KINDS = frozenset({
    'topup', 'redeem', 'bonus', 'refund', 'adjust_credit', 'reserve',
    'reserve_release', 'debit', 'adjust_debit',
})


def _text(payload: Mapping[str, Any], key: str, maximum: int = 512,
          *, required: bool = True) -> str:
    value = payload.get(key, '')
    if not isinstance(value, str) or len(value) > maximum:
        raise StorageError(
            'database_protocol_error', f'Invalid {key} in billing request')
    if required and not value:
        raise StorageError(
            'database_protocol_error', f'Missing {key} in billing request')
    return value


def _integer(payload: Mapping[str, Any], key: str, *, minimum: int | None = None,
             maximum: int = 9_223_372_036_854_775_807) -> int:
    value = payload.get(key)
    if (not isinstance(value, int) or isinstance(value, bool)
            or value > maximum or (minimum is not None and value < minimum)):
        raise StorageError(
            'database_protocol_error', f'Invalid {key} in billing request')
    return value


def _decode_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        decoded = orjson.loads(value or '{}')
    except (TypeError, orjson.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _wallet_document(row: Mapping[str, Any] | None, user_id: str) -> dict[str, Any]:
    if row is None:
        return {
            'user_id': user_id, 'balance_micro': 0, 'currency': 'CREDIT',
            'low_balance_alert_micro': 0, 'updated_at': 0,
        }
    return {
        'user_id': row['user_id'],
        'balance_micro': int(row['balance_micro']),
        'currency': row['currency'],
        'low_balance_alert_micro': int(row['low_balance_alert_micro']),
        'updated_at': int(row['updated_at']),
    }


def _wallet_get_row(session: Session, user_id: str):
    return session.fetch_one(
        'SELECT user_id, balance_micro, currency, low_balance_alert_micro, '
        'updated_at FROM billing_wallets WHERE user_id = ?', (user_id,))


def _wallet_get(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _text(payload, 'user_id', 200)
    return _wallet_document(_wallet_get_row(session, user_id), user_id)


def _ledger_document(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        'id': row['id'], 'user_id': row['user_id'], 'ts': int(row['ts']),
        'amount_micro': int(row['amount_micro']), 'kind': row['kind'],
        'ref_type': row['ref_type'] or '', 'ref_id': row['ref_id'] or '',
        'balance_after_micro': int(row['balance_after_micro']),
        'note': row['note'] or '',
    }


def _ledger_find_row(
    session: Session, user_id: str, kind: str, ref_type: str, ref_id: str,
):
    if not ref_type or not ref_id:
        return None
    return session.fetch_one(
        'SELECT id, user_id, ts, amount_micro, kind, ref_type, ref_id, '
        'balance_after_micro, note FROM billing_ledger '
        'WHERE user_id = ? AND kind = ? AND ref_type = ? AND ref_id = ? '
        'LIMIT 1', (user_id, kind, ref_type, ref_id))


def _ledger_find(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _text(payload, 'user_id', 200)
    kind = _text(payload, 'kind', 64)
    ref_type = _text(payload, 'ref_type', 100, required=False)
    ref_id = _text(payload, 'ref_id', 300, required=False)
    row = _ledger_find_row(session, user_id, kind, ref_type, ref_id)
    return _ledger_document(row) if row is not None else None


def _insert_ledger(
    session: Session, *, ledger_id: str, user_id: str, ts: int,
    amount_micro: int, kind: str, ref_type: str, ref_id: str,
    balance_after_micro: int, note: str,
) -> dict[str, Any]:
    session.execute(
        'INSERT INTO billing_ledger(id, user_id, ts, amount_micro, kind, '
        'ref_type, ref_id, balance_after_micro, note) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (ledger_id, user_id, ts, amount_micro, kind, ref_type, ref_id,
         balance_after_micro, note))
    return {
        'id': ledger_id, 'user_id': user_id, 'ts': ts,
        'amount_micro': amount_micro, 'kind': kind, 'ref_type': ref_type,
        'ref_id': ref_id, 'balance_after_micro': balance_after_micro,
        'note': note,
    }


def _ledger_append(session: Session, payload: Mapping[str, Any]) -> Any:
    ledger_id = _text(payload, 'id', 200)
    user_id = _text(payload, 'user_id', 200)
    kind = _text(payload, 'kind', 64)
    if kind not in LEDGER_KINDS:
        raise StorageError('database_protocol_error', 'Invalid billing ledger kind')
    amount = _integer(payload, 'amount_micro')
    balance = _integer(payload, 'balance_after_micro')
    ts = _integer(payload, 'ts', minimum=0)
    ref_type = _text(payload, 'ref_type', 100, required=False)
    ref_id = _text(payload, 'ref_id', 300, required=False)
    note = _text(payload, 'note', 4000, required=False)
    session.lock_key('billing.ledger', user_id)
    existing = _ledger_find_row(session, user_id, kind, ref_type, ref_id)
    if existing is not None:
        return _ledger_document(existing)
    return _insert_ledger(
        session, ledger_id=ledger_id, user_id=user_id, ts=ts,
        amount_micro=amount, kind=kind, ref_type=ref_type, ref_id=ref_id,
        balance_after_micro=balance, note=note)


def _ledger_list(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _text(payload, 'user_id', 200)
    limit = _integer(payload, 'limit', minimum=1, maximum=1000)
    offset = _integer(payload, 'offset', minimum=0, maximum=10_000_000)
    kinds = payload.get('kinds') or []
    if (not isinstance(kinds, list) or len(kinds) > len(LEDGER_KINDS)
            or any(kind not in LEDGER_KINDS for kind in kinds)):
        raise StorageError('database_protocol_error', 'Invalid billing ledger kinds')
    sql = (
        'SELECT id, user_id, ts, amount_micro, kind, ref_type, ref_id, '
        'balance_after_micro, note FROM billing_ledger WHERE user_id = ?')
    params: list[Any] = [user_id]
    if kinds:
        sql += ' AND kind IN (' + ','.join('?' for _ in kinds) + ')'
        params.extend(kinds)
    if payload.get('since_ts') is not None:
        sql += ' AND ts >= ?'
        params.append(_integer(payload, 'since_ts', minimum=0))
    sql += ' ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?'
    params.extend((limit, offset))
    return [_ledger_document(row) for row in session.fetch_all(sql, tuple(params))]


def _ledger_recompute(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _text(payload, 'user_id', 200)
    row = session.fetch_one(
        'SELECT COALESCE(SUM(amount_micro), 0) AS total FROM billing_ledger '
        'WHERE user_id = ?', (user_id,))
    return {'balance_micro': int(row['total'] if row else 0)}


def _upsert_wallet(session: Session, user_id: str, balance: int, now: int) -> None:
    session.execute(
        'INSERT INTO billing_wallets(user_id, balance_micro, currency, '
        'low_balance_alert_micro, updated_at) VALUES (?, ?, ?, 0, ?) '
        'ON CONFLICT(user_id) DO UPDATE SET '
        'balance_micro = excluded.balance_micro, updated_at = excluded.updated_at',
        (user_id, balance, 'CREDIT', now))


def _wallet_apply(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _text(payload, 'user_id', 200)
    kind = _text(payload, 'kind', 64)
    if kind not in LEDGER_KINDS:
        raise StorageError('database_protocol_error', 'Invalid billing ledger kind')
    amount = _integer(payload, 'amount_micro')
    ref_type = _text(payload, 'ref_type', 100, required=False)
    ref_id = _text(payload, 'ref_id', 300, required=False)
    note = _text(payload, 'note', 4000, required=False)
    ledger_id = _text(payload, 'ledger_id', 200)
    ts = (
        _integer(payload, 'occurred_at', minimum=0)
        if payload.get('occurred_at') is not None else int(time.time())
    )
    allow_negative = payload.get('allow_negative') is True
    session.lock_key('billing.wallet', user_id)
    existing = _ledger_find_row(session, user_id, kind, ref_type, ref_id)
    if existing is not None:
        return {
            'applied': False, 'duplicate': True, 'insufficient': False,
            'wallet': _wallet_document(_wallet_get_row(session, user_id), user_id),
            'entry': _ledger_document(existing),
        }
    wallet = _wallet_document(_wallet_get_row(session, user_id), user_id)
    balance = int(wallet['balance_micro'])
    updated = balance + amount
    if updated < 0 and not allow_negative:
        return {
            'applied': False, 'duplicate': False, 'insufficient': True,
            'balance_micro': balance, 'needed_micro': -amount,
            'wallet': wallet,
        }
    _upsert_wallet(session, user_id, updated, ts)
    entry = _insert_ledger(
        session, ledger_id=ledger_id, user_id=user_id, ts=ts,
        amount_micro=amount, kind=kind, ref_type=ref_type, ref_id=ref_id,
        balance_after_micro=updated, note=note)
    return {
        'applied': True, 'duplicate': False, 'insufficient': False,
        'wallet': _wallet_document(_wallet_get_row(session, user_id), user_id),
        'entry': entry,
    }


def _wallet_settle(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _text(payload, 'user_id', 200)
    ref_id = _text(payload, 'ref_id', 300)
    reserved = _integer(payload, 'reserved_micro', minimum=0)
    actual = _integer(payload, 'actual_micro', minimum=0)
    note = _text(payload, 'note', 4000, required=False)
    ts = int(time.time())
    release_id = _text(payload, 'release_id', 200)
    debit_id = _text(payload, 'debit_id', 200)
    session.lock_key('billing.wallet', user_id)
    prior_debit = _ledger_find_row(session, user_id, 'debit', 'task', ref_id)
    if prior_debit is not None:
        return {
            'applied': False, 'wallet': _wallet_document(
                _wallet_get_row(session, user_id), user_id),
        }
    wallet = _wallet_document(_wallet_get_row(session, user_id), user_id)
    balance = int(wallet['balance_micro'])
    prior_release = _ledger_find_row(
        session, user_id, 'reserve_release', 'reserve', ref_id)
    if prior_release is None:
        after_release = balance + reserved
        _insert_ledger(
            session, ledger_id=release_id, user_id=user_id, ts=ts,
            amount_micro=reserved, kind='reserve_release', ref_type='reserve',
            ref_id=ref_id, balance_after_micro=after_release, note=note)
    else:
        after_release = balance
    after_debit = after_release - actual
    _insert_ledger(
        session, ledger_id=debit_id, user_id=user_id, ts=ts,
        amount_micro=-actual, kind='debit', ref_type='task', ref_id=ref_id,
        balance_after_micro=after_debit, note=note)
    _upsert_wallet(session, user_id, after_debit, ts)
    return {
        'applied': True,
        'wallet': _wallet_document(_wallet_get_row(session, user_id), user_id),
    }


def _stale_reserves(session: Session, payload: Mapping[str, Any]) -> Any:
    cutoff = _integer(payload, 'cutoff_ts', minimum=0)
    limit = _integer(payload, 'limit', minimum=1, maximum=10_000)
    rows = session.fetch_all(
        'SELECT user_id, ref_id, held_micro FROM ('
        ' SELECT user_id, ref_id, '
        " -COALESCE(SUM(CASE WHEN kind = 'reserve' THEN amount_micro ELSE 0 END), 0) "
        " -COALESCE(SUM(CASE WHEN kind = 'reserve_release' THEN amount_micro ELSE 0 END), 0) "
        ' AS held_micro, '
        " MAX(CASE WHEN kind = 'reserve' THEN ts ELSE 0 END) AS last_reserve_ts "
        " FROM billing_ledger WHERE ref_type = 'reserve' AND ref_id <> '' "
        ' GROUP BY user_id, ref_id) AS aggregate_reserves '
        'WHERE held_micro > 0 AND last_reserve_ts > 0 '
        'AND last_reserve_ts <= ? ORDER BY last_reserve_ts LIMIT ?',
        (cutoff, limit))
    return [{
        'user_id': row['user_id'], 'ref_id': row['ref_id'],
        'held_micro': int(row['held_micro']),
    } for row in rows]


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
    provider_id = _text(payload, 'provider_id', 300)
    session.lock_key('billing.payment.provider', f'{provider}:{provider_id}')
    existing = session.fetch_one(
        _PAYMENT_SELECT + ' WHERE provider = ? AND provider_id = ? LIMIT 1',
        (provider, provider_id))
    if existing is not None:
        return {'created': False, 'payment': _payment_document(existing)}
    payment_id = _text(payload, 'id', 200)
    user_id = _text(payload, 'user_id', 200)
    status = _text(payload, 'status', 32)
    if status not in {'pending', 'settled', 'failed'}:
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


def _payment_settle(session: Session, payload: Mapping[str, Any]) -> Any:
    payment_id = _text(payload, 'payment_id', 200)
    session.lock_key('billing.payment', payment_id)
    row = session.fetch_one(_PAYMENT_SELECT + ' WHERE id = ?', (payment_id,))
    if row is None:
        return {'found': False, 'settled': False, 'payment': None}
    payment = _payment_document(row)
    if payment['status'] == 'settled':
        return {'found': True, 'settled': False, 'payment': payment}
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


OPERATIONS = {
    'billing.wallet.get': ops.OperationSpec('query', False, _wallet_get),
    'billing.wallet.apply': ops.OperationSpec('command', True, _wallet_apply),
    'billing.wallet.settle': ops.OperationSpec('command', True, _wallet_settle),
    'billing.ledger.find': ops.OperationSpec('query', False, _ledger_find),
    'billing.ledger.append': ops.OperationSpec('command', True, _ledger_append),
    'billing.ledger.list': ops.OperationSpec('query', False, _ledger_list),
    'billing.ledger.recompute': ops.OperationSpec('query', False, _ledger_recompute),
    'billing.reserve.stale': ops.OperationSpec('query', False, _stale_reserves),
    'billing.redeem_codes.mint': ops.OperationSpec(
        'command', True, _redeem_codes_mint),
    'billing.redeem_code.apply': ops.OperationSpec(
        'command', True, _redeem_code_apply),
    'billing.redeem_codes.list': ops.OperationSpec(
        'query', False, _redeem_codes_list),
    'billing.payment.find': ops.OperationSpec('query', False, _payment_find),
    'billing.payment.record': ops.OperationSpec('command', True, _payment_record),
    'billing.payment.settle': ops.OperationSpec('command', True, _payment_settle),
    'billing.payment.list': ops.OperationSpec('query', False, _payment_list),
}

__all__ = ['LEDGER_KINDS', 'OPERATIONS']
