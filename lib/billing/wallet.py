"""Atomic wallet operations backed exclusively by ``storage.v1``."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib

from lib.ids import short_id
from lib.log import audit_log
from lib.storage import get_storage_client


class BillingError(Exception):
    """Base class for billing-layer failures."""


class InsufficientFunds(BillingError):
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

    @classmethod
    def from_document(cls, value: dict) -> 'WalletSnapshot':
        return cls(
            user_id=str(value['user_id']),
            balance_micro=int(value['balance_micro']),
            currency=str(value['currency']),
            low_balance_alert_micro=int(value['low_balance_alert_micro']),
            updated_at=int(value['updated_at']),
        )


def _stable_id(prefix: str, *parts: object) -> str:
    digest = hashlib.sha256('\0'.join(map(str, parts)).encode('utf-8')).hexdigest()
    return f'{prefix}{digest[:32]}'


def get_wallet(user_id: str) -> WalletSnapshot:
    if not user_id:
        raise ValueError('user_id required')
    value = get_storage_client().query(
        'billing.wallet.get', {'user_id': user_id}, deadline=2.0)
    return WalletSnapshot.from_document(value)


def get_balance(user_id: str) -> int:
    return get_wallet(user_id).balance_micro


def deposit(
    user_id: str,
    amount_micro: int,
    *,
    kind: str = 'topup',
    ref_type: str = '',
    ref_id: str = '',
    note: str = '',
) -> WalletSnapshot:
    if amount_micro <= 0:
        raise ValueError('amount_micro must be positive for deposit')
    if kind not in {
            'topup', 'redeem', 'bonus', 'refund', 'adjust_credit',
            'reserve_release'}:
        raise ValueError(f'Invalid deposit kind: {kind!r}')
    return _apply_signed(
        user_id, amount_micro, kind=kind, ref_type=ref_type,
        ref_id=ref_id, note=note)


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
    if amount_micro <= 0:
        raise ValueError('amount_micro must be positive for debit')
    if kind not in {'debit', 'reserve', 'adjust_debit'}:
        raise ValueError(f'Invalid debit kind: {kind!r}')
    return _apply_signed(
        user_id, -amount_micro, kind=kind, ref_type=ref_type,
        ref_id=ref_id, note=note, allow_negative=allow_negative)


def reserve(
    user_id: str, amount_micro: int, *, ref_id: str, note: str = '',
) -> WalletSnapshot:
    return debit(
        user_id, amount_micro, kind='reserve', ref_type='reserve',
        ref_id=ref_id, note=note)


def reserve_release(
    user_id: str, amount_micro: int, *, ref_id: str, note: str = '',
) -> WalletSnapshot:
    return deposit(
        user_id, amount_micro, kind='reserve_release', ref_type='reserve',
        ref_id=ref_id, note=note)


def settle(
    user_id: str,
    *,
    reserved_micro: int,
    actual_micro: int,
    ref_id: str,
    note: str = '',
) -> WalletSnapshot:
    if not user_id or not ref_id:
        raise ValueError('user_id and ref_id are required')
    if reserved_micro < 0 or actual_micro < 0:
        raise ValueError('amounts must be non-negative')
    identity = (user_id, ref_id)
    result = get_storage_client(write=True).command(
        'billing.wallet.settle',
        {
            'user_id': user_id, 'reserved_micro': reserved_micro,
            'actual_micro': actual_micro, 'ref_id': ref_id, 'note': note,
            'release_id': _stable_id('led_', 'release', *identity),
            'debit_id': _stable_id('led_', 'debit', *identity),
        },
        _stable_id('cmd_', 'billing.settle', *identity),
        deadline=5.0,
    )
    wallet = WalletSnapshot.from_document(result['wallet'])
    if result.get('applied'):
        audit_log(
            'billing_settle', user_id=user_id, ref_id=ref_id,
            reserved_micro=reserved_micro, actual_micro=actual_micro,
            balance_after_micro=wallet.balance_micro)
    return wallet


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
    if not user_id:
        raise ValueError('user_id required')
    if ref_type and ref_id:
        identity = (user_id, kind, ref_type, ref_id)
        ledger_id = _stable_id('led_', *identity)
        command_id = _stable_id('cmd_', 'billing.apply', *identity)
    else:
        ledger_id = short_id('led_')
        command_id = _stable_id('cmd_', 'billing.apply', ledger_id)
    result = get_storage_client(write=True).command(
        'billing.wallet.apply',
        {
            'user_id': user_id, 'amount_micro': amount_micro, 'kind': kind,
            'ref_type': ref_type, 'ref_id': ref_id, 'note': note,
            'allow_negative': bool(allow_negative), 'ledger_id': ledger_id,
        },
        command_id,
        deadline=5.0,
    )
    if result.get('insufficient'):
        raise InsufficientFunds(
            user_id, int(result['balance_micro']), int(result['needed_micro']))
    wallet = WalletSnapshot.from_document(result['wallet'])
    if result.get('applied'):
        audit_log(
            'billing_' + kind, user_id=user_id, ref_id=ref_id,
            amount_micro=amount_micro,
            balance_after_micro=wallet.balance_micro)
    return wallet


def new_ref_id(prefix: str = 'ref') -> str:
    return short_id(f'{prefix}_')


__all__ = [
    'BillingError', 'InsufficientFunds', 'WalletSnapshot', 'debit', 'deposit',
    'get_balance', 'get_wallet', 'new_ref_id', 'reserve', 'reserve_release',
    'settle',
]
