"""lib.billing.ledger — Append-only ledger of credit movements.

The ledger is the **source of truth** for every credit that ever moved
through the relay. The wallet table is a denormalized cache:
``wallet.balance_micro == SUM(ledger.amount_micro WHERE user_id = ...)``.
If the cache ever drifts (process kill mid-transaction, manual SQL,
disk corruption) we recompute from the ledger.

Design rules
------------
1. **Append-only.** No UPDATE, no DELETE. A "refund" is a positive
   ledger entry, not a deletion of the original debit.
2. **One transaction per movement.** ``INSERT ledger + UPDATE wallet``
   live in the same DB transaction so the cache never lags the truth.
3. **Idempotent by ``ref_type+ref_id``.** Re-posting the same
   reservation/settle twice is a no-op (returns the existing row).
   This is what makes retries safe in :mod:`lib.billing.wallet`.
4. **Signed ``amount_micro``.** Positive = credit (deposit, redeem,
   refund). Negative = debit (usage, manual adjustment).

Public functions live on the wallet module — this file is the raw
storage layer plus the kind enum.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Optional

from lib.database import (
    DOMAIN_SYSTEM,
    get_thread_db as get_db,
    write_transaction,
)
from lib.ids import short_id
from lib.log import get_logger

logger = get_logger(__name__)


# ── Closed enum of ledger kinds ──────────────────────────────────────
LEDGER_KINDS = frozenset({
    'topup',          # +    payment processor settled
    'redeem',         # +    redemption code consumed
    'bonus',          # +    admin bonus / promotional credit
    'refund',         # +    refund of a previous debit
    'adjust_credit',  # +    manual admin credit
    'reserve',        # -    pre-flight hold for an in-flight request
    'reserve_release',# +    refund of a reserve when the request settles
                      #      (so the NET effect of a successful request is
                      #       reserve(-N) + reserve_release(+N) + debit(-actual))
    'debit',          # -    actual usage charge
    'adjust_debit',   # -    manual admin debit
})


@dataclass(frozen=True)
class LedgerEntry:
    """One row in the ledger."""
    id: str
    user_id: str
    ts: int                  # epoch seconds
    amount_micro: int        # signed
    kind: str
    ref_type: str            # '' / 'task' / 'payment' / 'redeem_code' / ...
    ref_id: str
    balance_after_micro: int
    note: str = ''

    @classmethod
    def from_row(cls, row) -> 'LedgerEntry':
        if hasattr(row, 'keys'):
            return cls(
                id=row['id'], user_id=row['user_id'],
                ts=int(row['ts']),
                amount_micro=int(row['amount_micro']),
                kind=row['kind'],
                ref_type=row['ref_type'] or '',
                ref_id=row['ref_id'] or '',
                balance_after_micro=int(row['balance_after_micro']),
                note=row['note'] or '',
            )
        return cls(
            id=row[0], user_id=row[1], ts=int(row[2]),
            amount_micro=int(row[3]), kind=row[4],
            ref_type=row[5] or '', ref_id=row[6] or '',
            balance_after_micro=int(row[7]), note=row[8] or '',
        )


def _new_id() -> str:
    return short_id('led_')


def find_existing(user_id: str, kind: str, ref_type: str,
                  ref_id: str) -> Optional[LedgerEntry]:
    """Return the existing entry matching the (kind, ref_type, ref_id)
    triple, or None. Used for idempotency.
    """
    if not (ref_type and ref_id):
        return None
    db = get_db(DOMAIN_SYSTEM)
    row = db.execute(
        'SELECT id, user_id, ts, amount_micro, kind, ref_type, ref_id, '
        '       balance_after_micro, note '
        '  FROM billing_ledger '
        ' WHERE user_id = ? AND kind = ? AND ref_type = ? AND ref_id = ? '
        ' LIMIT 1',
        (user_id, kind, ref_type, ref_id),
    ).fetchone()
    if row is None:
        return None
    return LedgerEntry.from_row(row)


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
    """Insert one ledger row with an owned/nested transaction boundary.

    Wallet operations already hold an outer atomic transaction, so this nests
    as a savepoint there. Direct maintenance/test callers get a complete
    standalone commit instead of leaving a write pending for an unrelated
    future operation.

    Validation:
      * ``kind`` must be in :data:`LEDGER_KINDS`.
      * Sign of ``amount_micro`` is NOT validated against ``kind`` — the
        ``adjust_*`` kinds let an admin push the balance either way.
    """
    if kind not in LEDGER_KINDS:
        raise ValueError(f'Unknown ledger kind: {kind!r}')
    if not user_id:
        raise ValueError('user_id required')
    led_id = _new_id()
    ts = ts if ts is not None else int(time.time())
    db = get_db(DOMAIN_SYSTEM)
    with write_transaction(db, label='billing ledger append'):
        db.execute(
            'INSERT INTO billing_ledger '
            '  (id, user_id, ts, amount_micro, kind, ref_type, ref_id, '
            '   balance_after_micro, note) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (led_id, user_id, ts, amount_micro, kind,
             ref_type, ref_id, balance_after_micro, note),
        )
    return LedgerEntry(
        id=led_id, user_id=user_id, ts=ts,
        amount_micro=amount_micro, kind=kind,
        ref_type=ref_type, ref_id=ref_id,
        balance_after_micro=balance_after_micro, note=note,
    )


def list_entries(
    user_id: str,
    *,
    limit: int = 100,
    offset: int = 0,
    kinds: Optional[List[str]] = None,
    since_ts: Optional[int] = None,
) -> List[LedgerEntry]:
    """Page through a user's ledger, newest first."""
    db = get_db(DOMAIN_SYSTEM)
    sql = (
        'SELECT id, user_id, ts, amount_micro, kind, ref_type, ref_id, '
        '       balance_after_micro, note '
        '  FROM billing_ledger '
        ' WHERE user_id = ? '
    )
    params: list = [user_id]
    if kinds:
        placeholders = ','.join('?' for _ in kinds)
        sql += f' AND kind IN ({placeholders}) '
        params.extend(kinds)
    if since_ts is not None:
        sql += ' AND ts >= ? '
        params.append(int(since_ts))
    sql += ' ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?'
    params.extend([int(limit), int(offset)])
    rows = db.execute(sql, tuple(params)).fetchall()
    return [LedgerEntry.from_row(r) for r in rows]


def recompute_balance(user_id: str) -> int:
    """Recompute the wallet balance from the ledger. O(N); use sparingly."""
    db = get_db(DOMAIN_SYSTEM)
    row = db.execute(
        'SELECT COALESCE(SUM(amount_micro), 0) AS total '
        '  FROM billing_ledger WHERE user_id = ?',
        (user_id,),
    ).fetchone()
    return int(row[0] if not hasattr(row, 'keys') else row['total'])


__all__ = [
    'LEDGER_KINDS', 'LedgerEntry',
    'append_entry', 'find_existing', 'list_entries', 'recompute_balance',
]
