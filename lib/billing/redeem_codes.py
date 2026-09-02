"""Redemption-code repository over atomic Sidecar billing operations."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import time
from typing import Iterable

from lib.ids import short_id
from lib.storage import get_storage_client

from .wallet import WalletSnapshot


class RedeemCodeError(ValueError):
    """Base class for a user-correctable redemption failure."""


class RedeemCodeNotFound(RedeemCodeError):
    pass


class RedeemCodeExpired(RedeemCodeError):
    pass


class RedeemCodeAlreadyUsed(RedeemCodeError):
    pass


@dataclass(frozen=True)
class RedeemCode:
    code: str
    amount_micro: int
    batch: str
    created_by: str
    created_at: int
    expires_at: int
    redeemed_by: str
    redeemed_at: int
    note: str

    @classmethod
    def from_document(cls, value: dict) -> 'RedeemCode':
        return cls(
            code=str(value['code']),
            amount_micro=int(value['amount_micro']),
            batch=str(value.get('batch') or ''),
            created_by=str(value.get('created_by') or ''),
            created_at=int(value.get('created_at') or 0),
            expires_at=int(value.get('expires_at') or 0),
            redeemed_by=str(value.get('redeemed_by') or ''),
            redeemed_at=int(value.get('redeemed_at') or 0),
            note=str(value.get('note') or ''),
        )


@dataclass(frozen=True)
class Redemption:
    code: RedeemCode
    wallet: WalletSnapshot


def _stable_id(prefix: str, payload: dict) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return prefix + hashlib.sha256(encoded).hexdigest()


def mint_codes(
    codes: Iterable[str],
    *,
    amount_micro: int,
    batch: str,
    created_by: str = '',
    created_at: int | None = None,
    expires_at: int = 0,
    note: str = '',
) -> list[str]:
    values = list(codes)
    payload = {
        'codes': values,
        'amount_micro': int(amount_micro),
        'batch': batch,
        'created_by': created_by,
        'created_at': int(time.time()) if created_at is None else int(created_at),
        'expires_at': int(expires_at),
        'note': note,
    }
    result = get_storage_client(write=True).command(
        'billing.redeem_codes.mint',
        payload,
        _stable_id('billing:redeem:mint:', payload),
        deadline=10.0,
    )
    return [str(code) for code in result['codes']]


def redeem_code(
    code: str,
    *,
    user_id: str,
    redeemed_at: int | None = None,
) -> Redemption:
    now = int(time.time()) if redeemed_at is None else int(redeemed_at)
    identity = {'code': code, 'user_id': user_id}
    payload = {
        **identity,
        'redeemed_at': now,
        'ledger_id': _stable_id('led_', identity)[:36],
    }
    result = get_storage_client(write=True).command(
        'billing.redeem_code.apply',
        payload,
        short_id('billing_redeem_cmd_'),
        deadline=5.0,
    )
    status = result.get('status')
    if status == 'not_found':
        raise RedeemCodeNotFound('No such redemption code')
    if status == 'expired':
        raise RedeemCodeExpired('Redemption code expired')
    if status == 'already_redeemed':
        raise RedeemCodeAlreadyUsed('Redemption code was already used')
    if status != 'redeemed':
        raise RuntimeError('Unexpected redemption result')
    return Redemption(
        code=RedeemCode.from_document(result['code']),
        wallet=WalletSnapshot.from_document(result['wallet']),
    )


def list_codes(
    *,
    batch: str = '',
    status: str = 'all',
    limit: int = 100,
    offset: int = 0,
) -> list[RedeemCode]:
    rows = get_storage_client().query(
        'billing.redeem_codes.list', {
            'batch': batch,
            'status': status,
            'limit': int(limit),
            'offset': int(offset),
        }, deadline=5.0)
    return [RedeemCode.from_document(dict(row)) for row in rows]


__all__ = [
    'RedeemCode', 'RedeemCodeAlreadyUsed', 'RedeemCodeError',
    'RedeemCodeExpired', 'RedeemCodeNotFound', 'Redemption',
    'list_codes', 'mint_codes', 'redeem_code',
]
