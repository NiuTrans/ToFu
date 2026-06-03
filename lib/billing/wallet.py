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
A janitor sweep (TODO: separate file) cancels orphaned reserves
older than ``stale_reserve_ttl`` (default 30 min).
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass

from lib.database import DOMAIN_SYSTEM, _BACKEND, get_thread_db as get_db
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


# Per-user lock keeps the SQLite path race-free without IMMEDIATE
# transactions (which would block the whole DB). Negligible cost; the
# real concurrency primitive is the DB-level transaction.
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
    if _BACKEND == 'postgresql':
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
    now = int(time.time())
    if _BACKEND == 'postgresql':
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


def _begin(db) -> None:
    # SQLite default DEFERRED transactions can deadlock under concurrent
    # writers; IMMEDIATE acquires the write lock up front.
    if _BACKEND != 'postgresql':
        try:
            db.execute('BEGIN IMMEDIATE')
        except Exception as e:
            # Some wrappers auto-begin; tolerate at debug level.
            logger.debug('[Billing] BEGIN IMMEDIATE skipped: %s', e)


def _commit(db) -> None:
    try:
        db.commit()
    except Exception as e:
        logger.error('[Billing] commit failed: %s', e, exc_info=True)
        raise


def _rollback(db) -> None:
    try:
        db.rollback()
    except Exception as e:
        # Rollback during error handling — log but never escalate.
        logger.debug('[Billing] rollback failed: %s', e)


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
        _begin(db)
        try:
            # Idempotency: if we already settled this ref, return current.
            existing = _ledger.find_existing(
                user_id, 'debit', 'task', ref_id)
            if existing is not None:
                _commit(db)
                return get_wallet(user_id)
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
            _commit(db)
            audit_log('billing_settle', user_id=user_id,
                      ref_id=ref_id, reserved_micro=reserved_micro,
                      actual_micro=actual_micro,
                      balance_after_micro=new_after_debit)
            return get_wallet(user_id)
        except Exception:
            _rollback(db)
            raise


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
    """Internal: one signed ledger entry + wallet upsert in one tx."""
    if not user_id:
        raise ValueError('user_id required')
    lock = _lock_for(user_id)
    with lock:
        db = get_db(DOMAIN_SYSTEM)
        _begin(db)
        try:
            # Idempotency.
            if ref_type and ref_id:
                existing = _ledger.find_existing(
                    user_id, kind, ref_type, ref_id)
                if existing is not None:
                    _commit(db)
                    return get_wallet(user_id)
            balance = _read_balance(db, user_id)
            new_balance = balance + amount_micro
            if new_balance < 0 and not allow_negative:
                _rollback(db)
                raise InsufficientFunds(
                    user_id, balance, -amount_micro)
            _ledger.append_entry(
                user_id=user_id, amount_micro=amount_micro,
                kind=kind, ref_type=ref_type, ref_id=ref_id,
                balance_after_micro=new_balance, note=note)
            _upsert_wallet(db, user_id, new_balance)
            _commit(db)
            audit_log('billing_' + kind, user_id=user_id,
                      ref_id=ref_id, amount_micro=amount_micro,
                      balance_after_micro=new_balance)
            return get_wallet(user_id)
        except InsufficientFunds:
            raise
        except Exception:
            _rollback(db)
            raise


def new_ref_id(prefix: str = 'ref') -> str:
    """Generate an id suitable for ref_id (caller-side helper)."""
    return f'{prefix}_{uuid.uuid4().hex[:24]}'


__all__ = [
    'BillingError', 'InsufficientFunds', 'WalletSnapshot',
    'get_wallet', 'get_balance',
    'deposit', 'debit', 'reserve', 'reserve_release', 'settle',
    'new_ref_id',
]
