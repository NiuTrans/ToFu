"""Atomic SQL wallet, ledger and reservation operations.

Entry points are registered by operation_domains.billing; native checkpoint
settlement reuses the same wallet owner. Depends on shared scalar validators."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lib.storage.errors import StorageError

import time

from lib.storage_authority_sql.adapters.base import Session
from lib.storage_authority_sql.generated_billing_contract import (
    LEDGER_KINDS, NATIVE_ADMISSION_KIND, REQUEST_ADMISSION_KIND, REQUEST_INVOICE_KIND,
)
from ._billing_values import _integer, _text


def _guard_native_reservation(session, user_id, kind, ref_type, ref_id, amount, *, admission):
    """Serialize provider admission with every standalone reservation release."""
    if kind in {NATIVE_ADMISSION_KIND, REQUEST_ADMISSION_KIND, REQUEST_INVOICE_KIND}:
        if not admission or amount != 0 or ref_type != 'reserve' or not ref_id:
            raise StorageError('database_protocol_error', 'Native admission requires its wallet barrier')
        held = _ledger_find_row(session, user_id, 'reserve', 'reserve', ref_id)
        if (held is None or int(held['amount_micro']) >= 0
                or _ledger_find_row(session, user_id, 'reserve_release', 'reserve', ref_id) is not None
                or _ledger_find_row(session, user_id, 'debit', 'task', ref_id) is not None):
            raise StorageError('database_conflict', 'Native admission requires an unreleased reservation')
        if kind == REQUEST_INVOICE_KIND and _ledger_find_row(session, user_id, REQUEST_ADMISSION_KIND, 'reserve', ref_id) is None:
            raise StorageError('database_conflict', 'Request invoice requires acknowledged admission')
    elif kind == 'reserve_release' and ref_type == 'reserve' and ref_id:
        if any(_ledger_find_row(session, user_id, marker, 'reserve', ref_id) is not None
               for marker in (NATIVE_ADMISSION_KIND, REQUEST_ADMISSION_KIND)):
            raise StorageError('database_conflict', 'Native reservation requires exact terminal settlement')


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
    if (kind in {NATIVE_ADMISSION_KIND, REQUEST_ADMISSION_KIND, REQUEST_INVOICE_KIND}
            or ref_type == 'reserve' and kind in {'reserve', 'reserve_release'}
            or ref_type == 'task' and kind == 'debit'):
        # Admission reads these exact ledger claims. Their raw append path must
        # share the wallet lock too, including on PostgreSQL READ COMMITTED.
        session.lock_key('billing.wallet', user_id)
        _guard_native_reservation(session, user_id, kind, ref_type, ref_id, amount, admission=False)
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
    _guard_native_reservation(session, user_id, kind, ref_type, ref_id, amount, admission=True)
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
