"""Append-only billing ledger repository over semantic Sidecar RPC."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import List, Optional

from lib.ids import short_id
from lib.storage import get_storage_client


LEDGER_KINDS = frozenset({
    'topup', 'redeem', 'bonus', 'refund', 'adjust_credit', 'reserve',
    'reserve_release', 'debit', 'adjust_debit',
})


@dataclass(frozen=True)
class LedgerEntry:
    id: str
    user_id: str
    ts: int
    amount_micro: int
    kind: str
    ref_type: str
    ref_id: str
    balance_after_micro: int
    note: str = ''

    @classmethod
    def from_row(cls, row) -> 'LedgerEntry':
        return cls(
            id=str(row['id']), user_id=str(row['user_id']), ts=int(row['ts']),
            amount_micro=int(row['amount_micro']), kind=str(row['kind']),
            ref_type=str(row.get('ref_type') or ''),
            ref_id=str(row.get('ref_id') or ''),
            balance_after_micro=int(row['balance_after_micro']),
            note=str(row.get('note') or ''),
        )


def find_existing(
    user_id: str, kind: str, ref_type: str, ref_id: str,
) -> Optional[LedgerEntry]:
    if not (ref_type and ref_id):
        return None
    value = get_storage_client().query(
        'billing.ledger.find', {
            'user_id': user_id, 'kind': kind,
            'ref_type': ref_type, 'ref_id': ref_id,
        }, deadline=2.0)
    return LedgerEntry.from_row(value) if value is not None else None


def append_entry(
    *,
    user_id: str,
    amount_micro: int,
    kind: str,
    balance_after_micro: int,
    ref_type: str = '',
    ref_id: str = '',
    note: str = '',
    ts: Optional[int] = None,
) -> LedgerEntry:
    if kind not in LEDGER_KINDS:
        raise ValueError(f'Unknown ledger kind: {kind!r}')
    if not user_id:
        raise ValueError('user_id required')
    ledger_id = short_id('led_')
    value = get_storage_client(write=True).command(
        'billing.ledger.append', {
            'id': ledger_id, 'user_id': user_id,
            'ts': int(time.time()) if ts is None else int(ts),
            'amount_micro': int(amount_micro), 'kind': kind,
            'ref_type': ref_type, 'ref_id': ref_id,
            'balance_after_micro': int(balance_after_micro), 'note': note,
        },
        f'billing:ledger:append:{ledger_id}',
        deadline=5.0,
    )
    return LedgerEntry.from_row(value)


def list_entries(
    user_id: str,
    *,
    limit: int = 100,
    offset: int = 0,
    kinds: Optional[List[str]] = None,
    since_ts: Optional[int] = None,
) -> List[LedgerEntry]:
    payload = {
        'user_id': user_id, 'limit': int(limit), 'offset': int(offset),
        'kinds': list(kinds or []),
    }
    if since_ts is not None:
        payload['since_ts'] = int(since_ts)
    rows = get_storage_client().query(
        'billing.ledger.list', payload, deadline=5.0)
    return [LedgerEntry.from_row(row) for row in rows]


def recompute_balance(user_id: str) -> int:
    value = get_storage_client().query(
        'billing.ledger.recompute', {'user_id': user_id}, deadline=5.0)
    return int(value['balance_micro'])


__all__ = [
    'LEDGER_KINDS', 'LedgerEntry', 'append_entry', 'find_existing',
    'list_entries', 'recompute_balance',
]
