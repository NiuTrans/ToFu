"""Read-current bearer validation with a bounded best-effort audit touch."""

from __future__ import annotations

from collections import OrderedDict
import os
import threading
import time
from typing import Optional

from lib.log import get_logger
from lib.storage import StorageError, get_storage_client

from ._context import AuthContext
from ._crud import _hash_token


logger = get_logger(__name__)


def _touch_interval_seconds() -> float:
    raw = os.environ.get('TOFU_CREDENTIAL_TOUCH_INTERVAL_S', '').strip()
    try:
        configured = float(raw) if raw else 60.0
    except ValueError:
        configured = 60.0
    return min(3600.0, max(15.0, configured))


_TOUCH_INTERVAL_S = _touch_interval_seconds()
_TOUCH_RETRY_S = min(10.0, _TOUCH_INTERVAL_S)
_TOUCH_DEADLINE_S = 0.25
_TOUCH_CACHE_MAX = 2048
_touch_lock = threading.Lock()
_touch_due: OrderedDict[str, float] = OrderedDict()
_last_touch_warning_at = 0.0


def _reserve_audit_touch(row: dict, now: float, monotonic_now: float) -> bool:
    credential_id = str(row['id'])
    with _touch_lock:
        due_at = _touch_due.get(credential_id, 0.0)
        if due_at > monotonic_now:
            _touch_due.move_to_end(credential_id)
            return False
        last_used_at = row.get('last_used_at')
        elapsed = (
            _TOUCH_INTERVAL_S
            if last_used_at is None
            else max(0.0, now - float(last_used_at))
        )
        remaining = max(0.0, _TOUCH_INTERVAL_S - elapsed)
        _touch_due[credential_id] = monotonic_now + (
            remaining if remaining > 0 else _TOUCH_INTERVAL_S)
        _touch_due.move_to_end(credential_id)
        while len(_touch_due) > _TOUCH_CACHE_MAX:
            _touch_due.popitem(last=False)
        return remaining == 0


def _defer_failed_audit_touch(credential_id: str, monotonic_now: float) -> None:
    global _last_touch_warning_at
    with _touch_lock:
        _touch_due[credential_id] = monotonic_now + _TOUCH_RETRY_S
        if monotonic_now - _last_touch_warning_at < 60.0:
            return
        _last_touch_warning_at = monotonic_now
    logger.warning(
        '[ApiKeys] credential last-used audit touch deferred; '
        'request authority was already validated read-only')


def _touch_audit_metadata(row: dict, now: float, monotonic_now: float) -> None:
    if not _reserve_audit_touch(row, now, monotonic_now):
        return
    credential_id = str(row['id'])
    try:
        get_storage_client(write=True).command(
            'credential.touch', {
                'credential_id': credential_id,
                'owner_user_id': int(row['owner_user_id']),
                'tenant_id': str(row['tenant_id'] or ''),
                'used_at': now,
                'touch_if_before': max(0.0, now - _TOUCH_INTERVAL_S),
            },
            None,
            priority='maintenance',
            deadline=_TOUCH_DEADLINE_S,
        )
    except (StorageError, RuntimeError):
        # ``last_used_at`` is audit metadata, never an authority grant.  A
        # congested writer must not reject a principal that the read authority
        # just validated, and the retry reservation prevents request storms.
        _defer_failed_audit_touch(credential_id, monotonic_now)


def _reset_touch_budget_for_test() -> None:
    global _last_touch_warning_at
    with _touch_lock:
        _touch_due.clear()
        _last_touch_warning_at = 0.0


def validate_token(token: str) -> Optional[AuthContext]:
    """Validate current credential authority, or return ``None`` fail-closed."""
    if not isinstance(token, str):
        return None
    normalized = token.strip()
    if not normalized.startswith(('tofu_live_', 'tofu_admin_')):
        return None
    now = time.time()
    row = get_storage_client().query(
        'credential.validate', {
            'secret_hash': _hash_token(normalized),
            'now': now,
        },
    )
    if row is None:
        return None
    _touch_audit_metadata(row, now, time.monotonic())
    return AuthContext(
        key_id=row['id'],
        name=row['name'],
        scopes=frozenset(row['scopes']),
        rate_limit_rpm=int(row['rate_limit_rpm']),
        rate_limit_tpd=int(row['rate_limit_tpd']),
        owner_user_id=int(row['owner_user_id']),
        account_user_id=str(row['account_user_id'] or ''),
        tenant_id=str(row['tenant_id'] or '') or None,
    )


def identify_known_token(token: str) -> Optional[dict]:
    """Identify a persisted token without authenticating or touching it.

    Disabled, expired, revoked, and account-suspended credentials may be
    returned. Callers must use this only to scope recovery information; the
    result grants no authority.
    """
    if not isinstance(token, str):
        return None
    normalized = token.strip()
    if not normalized.startswith(('tofu_live_', 'tofu_admin_')):
        return None
    return get_storage_client().query(
        'credential.identify', {'secret_hash': _hash_token(normalized)})


__all__ = ['identify_known_token', 'validate_token']
