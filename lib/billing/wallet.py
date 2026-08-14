"""lib.billing.wallet — Atomic credit/debit operations.

Every public function here wraps "INSERT ledger + UPSERT wallet" in a
single transaction, with the ledger as the source of truth. Callers
get back the new balance; rejected operations raise.

Concurrency
-----------
SQLite serializes writers, so a transaction starting with
``BEGIN IMMEDIATE`` is enough. For PostgreSQL the wallet row is locked
via ``SELECT … FOR UPDATE`` inside the transaction so two debits never
race past the balance check.

Reservations
------------
A typical billable LLM request goes through THREE ledger rows:

  1. ``reserve(-estimate)``           — pre-flight, blocks insufficient funds
  2. ``reserve_release(+estimate)``   — refunds the hold on completion
  3. ``debit(-actual)``               — final usage charge

Steps (2) and (3) live in :func:`settle`; they are committed in the
same transaction so the user never observes a transient over-debit.
Reservations orphaned by a crash/abort before settle are reclaimed by
:func:`lib.billing.wallet_janitor.sweep_stale_reserves` (run on a timer
by the scheduler) once they are older than ``TOFU_BILLING_RESERVE_TTL``
(default 30 min).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from lib.database import (
    DOMAIN_SYSTEM,
    _BACKEND,
    assert_write_transaction,
    get_thread_db as get_db,
    write_transaction,
)
from lib.ids import short_id
from lib.log import audit_log, get_logger

from . import ledger as _ledger

logger = get_logger(__name__)


class BillingError(Exception):
    """Base class for billing-layer failures."""


class InsufficientFunds(BillingError):
    """Raised by :func:`debit` and :func:`reserve` when balance is too low."""

    def __init__(self, user_id: str, balance_micro: int, needed_micro: int):
        super().__init__(
            f'Insufficient funds for user={user_id}: '
            f'balance={balance_micro} µ, needed={needed_micro} µ')
        self.user_id = user_id
        self.balance_micro = balance_micro
        self.needed_micro = needed_micro


@dataclass(frozen=True)
class WalletSnapshot:
    user_id: str
    balance_micro: int
    currency: str
    low_balance_alert_micro: int
    updated_at: int


# Per-user lock avoids avoidable same-user queueing inside this process.  The
# real cross-thread/process primitive is write_transaction(): BEGIN IMMEDIATE
# on SQLite and row/CAS locking on PostgreSQL.
_user_locks: dict = {}
_user_locks_guard = threading.Lock()


def _lock_for(user_id: str) -> threading.Lock:
    with _user_locks_guard:
        lk = _user_locks.get(user_id)
        if lk is None:
            lk = threading.Lock()
            _user_locks[user_id] = lk
        return lk


def _read_balance(db, user_id: str) -> int:
    """Read the cached wallet balance, locking the row in PG."""
    if _BACKEND == 'pg':
        row = db.execute(
            'SELECT balance_micro FROM billing_wallets '
            ' WHERE user_id = ? FOR UPDATE',
            (user_id,)).fetchone()
    else:
        row = db.execute(
            'SELECT balance_micro FROM billing_wallets WHERE user_id = ?',
            (user_id,)).fetchone()
    if row is None:
        return 0
    return int(row[0] if not hasattr(row, 'keys') else row['balance_micro'])


def _upsert_wallet(db, user_id: str, balance_micro: int) -> None:
    assert_write_transaction(db, label='wallet balance upsert')
    now = int(time.time())
    if _BACKEND == 'pg':
        db.execute(
            'INSERT INTO billing_wallets '
            '  (user_id, balance_micro, currency, '
            '   low_balance_alert_micro, updated_at) '
            'VALUES (?, ?, ?, 0, ?) '
            'ON CONFLICT (user_id) DO UPDATE SET '
            '  balance_micro = EXCLUDED.balance_micro, '
            '  updated_at = EXCLUDED.updated_at',
            (user_id, balance_micro, 'CREDIT', now))
    else:
        db.execute(
            'INSERT INTO billing_wallets '
            '  (user_id, balance_micro, currency, '
            '   low_balance_alert_micro, updated_at) '
            'VALUES (?, ?, ?, 0, ?) '
            'ON CONFLICT (user_id) DO UPDATE SET '
            '  balance_micro = excluded.balance_micro, '
            '  updated_at    = excluded.updated_at',
            (user_id, balance_micro, 'CREDIT', now))


# ── Read-only helpers ────────────────────────────────────────────────

def get_wallet(user_id: str) -> WalletSnapshot:
    """Return the cached wallet state. Creates an empty wallet on miss."""
    db = get_db(DOMAIN_SYSTEM)
    row = db.execute(
        'SELECT user_id, balance_micro, currency, '
        '       low_balance_alert_micro, updated_at '
        '  FROM billing_wallets WHERE user_id = ?',
        (user_id,)).fetchone()
    if row is None:
        return WalletSnapshot(
            user_id=user_id, balance_micro=0,
            currency='CREDIT', low_balance_alert_micro=0,
            updated_at=0,
        )
    if hasattr(row, 'keys'):
        return WalletSnapshot(
            user_id=row['user_id'],
            balance_micro=int(row['balance_micro']),
            currency=row['currency'],
            low_balance_alert_micro=int(row['low_balance_alert_micro']),
            updated_at=int(row['updated_at']),
        )
    return WalletSnapshot(
        user_id=row[0], balance_micro=int(row[1]),
        currency=row[2], low_balance_alert_micro=int(row[3]),
        updated_at=int(row[4]),
    )


def get_balance(user_id: str) -> int:
    """Shortcut for ``get_wallet(...).balance_micro``."""
    return get_wallet(user_id).balance_micro


# ── Mutations ────────────────────────────────────────────────────────

def deposit(
    user_id: str,
    amount_micro: int,
    *,
    kind: str = 'topup',
    ref_type: str = '',
    ref_id: str = '',
    note: str = '',
) -> WalletSnapshot:
    """Add credits. Idempotent on (kind, ref_type, ref_id)."""
    if amount_micro <= 0:
        raise ValueError('amount_micro must be positive for deposit')
    if kind not in {'topup', 'redeem', 'bonus', 'refund', 'adjust_credit',
                    'reserve_release'}:
        raise ValueError(f'Invalid deposit kind: {kind!r}')
    return _apply_signed(user_id, +amount_micro, kind=kind,
                         ref_type=ref_type, ref_id=ref_id, note=note)


def debit(
    user_id: str,
    amount_micro: int,
    *,
    kind: str = 'debit',
    ref_type: str = '',
    ref_id: str = '',
    note: str = '',
    allow_negative: bool = False,
) -> WalletSnapshot:
    """Subtract credits. Raises :class:`InsufficientFunds` if balance too low."""
    if amount_micro <= 0:
        raise ValueError('amount_micro must be positive for debit')
    if kind not in {'debit', 'reserve', 'adjust_debit'}:
        raise ValueError(f'Invalid debit kind: {kind!r}')
    return _apply_signed(user_id, -amount_micro, kind=kind,
                         ref_type=ref_type, ref_id=ref_id, note=note,
                         allow_negative=allow_negative)


def reserve(
    user_id: str,
    amount_micro: int,
    *,
    ref_id: str,
    note: str = '',
) -> WalletSnapshot:
    """Pre-flight hold for an in-flight request. Idempotent on ref_id."""
    return debit(user_id, amount_micro, kind='reserve',
                 ref_type='reserve', ref_id=ref_id, note=note)


def reserve_release(
    user_id: str,
    amount_micro: int,
    *,
    ref_id: str,
    note: str = '',
) -> WalletSnapshot:
    """Refund a previously placed reserve. Idempotent on ref_id."""
    return deposit(user_id, amount_micro, kind='reserve_release',
                   ref_type='reserve', ref_id=ref_id, note=note)


def settle(
    user_id: str,
    *,
    reserved_micro: int,
    actual_micro: int,
    ref_id: str,
    note: str = '',
) -> WalletSnapshot:
    """Convert a reserve into the actual debit, refunding the difference.

    Atomically posts ``reserve_release(+reserved)`` and ``debit(-actual)``
    so the visible balance never dips below ``post_request_balance``.
    Idempotent on ``ref_id``.
    """
    if reserved_micro < 0 or actual_micro < 0:
        raise ValueError('amounts must be non-negative')
    lock = _lock_for(user_id)
    with lock:
        db = get_db(DOMAIN_SYSTEM)
        changed = False
        with write_transaction(db, label='settle billing reserve'):
            # Idempotency: if we already settled this ref, return current.
            existing = _ledger.find_existing(
                user_id, 'debit', 'task', ref_id)
            if existing is None:
                balance = _read_balance(db, user_id)
                new_after_release = balance + reserved_micro
                new_after_debit = new_after_release - actual_micro
                _ledger.append_entry(
                    user_id=user_id, amount_micro=+reserved_micro,
                    kind='reserve_release',
                    ref_type='reserve', ref_id=ref_id,
                    balance_after_micro=new_after_release, note=note)
                _ledger.append_entry(
                    user_id=user_id, amount_micro=-actual_micro,
                    kind='debit',
                    ref_type='task', ref_id=ref_id,
                    balance_after_micro=new_after_debit, note=note)
                _upsert_wallet(db, user_id, new_after_debit)
                changed = True
        if changed:
            audit_log('billing_settle', user_id=user_id,
                      ref_id=ref_id, reserved_micro=reserved_micro,
                      actual_micro=actual_micro,
                      balance_after_micro=new_after_debit)
        return get_wallet(user_id)


def _plain_balance(db, user_id: str) -> int:
    """Read balance WITHOUT a row lock (used post-UPDATE within the same tx)."""
    row = db.execute(
        'SELECT balance_micro FROM billing_wallets WHERE user_id = ?',
        (user_id,)).fetchone()
    if row is None:
        return 0
    return int(row[0] if not hasattr(row, 'keys') else row['balance_micro'])


def _conditional_apply(db, user_id: str, amount_micro: int,
                       allow_negative: bool):
    """Atomically apply a signed delta to the wallet balance (CAS).

    Runs a single conditional ``UPDATE billing_wallets SET
    balance_micro = balance_micro + ? WHERE user_id = ? [AND
    balance_micro + ? >= 0]``. The WHERE clause IS the funds check, evaluated
    against the CURRENT row value under the row lock, and the balance moves
    RELATIVELY — so a debit can neither overdraw nor clobber a concurrent
    writer's change, even across worker processes and without relying on the
    in-process lock. Same TOCTOU-closing shape as the board-lease CAS.

    Returns ``(status, balance)``:
      * ``('applied', new_balance)``  — the delta landed.
      * ``('insufficient', current)`` — a debit failed the funds check; the row
        is unchanged and ``current`` is its balance (for the error message).
      * ``('absent', 0)``             — no wallet row exists yet; the caller
        must INSERT the first movement.
    """
    assert_write_transaction(db, label='conditional wallet balance update')
    now = int(time.time())
    if allow_negative:
        res = db.execute(
            'UPDATE billing_wallets '
            '   SET balance_micro = balance_micro + ?, updated_at = ? '
            ' WHERE user_id = ?',
            (amount_micro, now, user_id))
    else:
        res = db.execute(
            'UPDATE billing_wallets '
            '   SET balance_micro = balance_micro + ?, updated_at = ? '
            ' WHERE user_id = ? AND balance_micro + ? >= 0',
            (amount_micro, now, user_id, amount_micro))
    # rowcount is 1 when the conditional matched+updated, 0 when it did not.
    # (Both shipped backends expose it; default 1 mirrors the board-lease CAS.)
    if getattr(res, 'rowcount', 1) != 0:
        return ('applied', _plain_balance(db, user_id))
    # 0 rows changed — distinguish "no wallet row yet" from "insufficient".
    row = db.execute(
        'SELECT balance_micro FROM billing_wallets WHERE user_id = ?',
        (user_id,)).fetchone()
    if row is None:
        return ('absent', 0)
    cur = int(row[0] if not hasattr(row, 'keys') else row['balance_micro'])
    return ('insufficient', cur)


def _apply_signed(
    user_id: str,
    amount_micro: int,
    *,
    kind: str,
    ref_type: str,
    ref_id: str,
    note: str,
    allow_negative: bool = False,
) -> WalletSnapshot:
    """Internal: one signed ledger entry + atomic wallet mutation in one tx.

    The wallet balance moves via an ATOMIC conditional UPDATE
    (:func:`_conditional_apply`) — the funds check lives in the SQL WHERE
    clause and the balance moves RELATIVELY — so it replaces the previous
    read-modify-write that computed an ABSOLUTE new balance in Python from a
    possibly-stale read (a cross-process TOCTOU, the same class the board-lease
    CAS closed). The in-process ``_lock_for`` is now belt-and-braces, no longer
    the sole guard.
    """
    if not user_id:
        raise ValueError('user_id required')
    lock = _lock_for(user_id)
    with lock:
        db = get_db(DOMAIN_SYSTEM)
        changed = False
        with write_transaction(db, label=f'billing {kind}'):
            # Idempotency.
            existing = None
            if ref_type and ref_id:
                existing = _ledger.find_existing(
                    user_id, kind, ref_type, ref_id)
            if existing is None:
                status, new_balance = _conditional_apply(
                    db, user_id, amount_micro, allow_negative)
                if status == 'insufficient':
                    raise InsufficientFunds(
                        user_id, new_balance, -amount_micro)
                if status == 'absent':
                    # First movement for this user — the conditional UPDATE
                    # matched no row. INSERT the opening balance.
                    new_balance = amount_micro
                    if new_balance < 0 and not allow_negative:
                        raise InsufficientFunds(user_id, 0, -amount_micro)
                    _upsert_wallet(db, user_id, new_balance)
                _ledger.append_entry(
                    user_id=user_id, amount_micro=amount_micro,
                    kind=kind, ref_type=ref_type, ref_id=ref_id,
                    balance_after_micro=new_balance, note=note)
                changed = True
        if changed:
            audit_log('billing_' + kind, user_id=user_id,
                      ref_id=ref_id, amount_micro=amount_micro,
                      balance_after_micro=new_balance)
        return get_wallet(user_id)


def new_ref_id(prefix: str = 'ref') -> str:
    """Generate an id suitable for ref_id (caller-side helper)."""
    return short_id(f'{prefix}_')


__all__ = [
    'BillingError', 'InsufficientFunds', 'WalletSnapshot',
    'get_wallet', 'get_balance',
    'deposit', 'debit', 'reserve', 'reserve_release', 'settle',
    'new_ref_id',
]
