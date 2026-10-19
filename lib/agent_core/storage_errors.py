"""Backend-neutral failure contract for execution ports and storage adapters.

StorageError, diagnostic formatting and HTTP mapping are shared with the sidecar.
Only standard-library dependencies are allowed; importing this module never
initializes application storage. lib.storage.errors re-exports the same objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


ERROR_CODES = frozenset({
    'database_not_found',
    # Bounded diagnostic queries reject malformed cursors and unsupported
    # legacy representations without downgrading them to internal failures.
    'database_invalid_payload',
    'database_unsupported',
    'database_busy',
    'database_unavailable',
    'database_timeout',
    'database_conflict',
    # The caller supplied an identity that conflicts with the operation's
    # explicit owner boundary. Keep this distinct from malformed payloads and
    # optimistic state conflicts so HTTP adapters can preserve default-deny.
    'database_forbidden',
    # Domain conflicts that callers must handle differently. Keeping them
    # typed avoids brittle parsing of localized storage messages.
    'turn_projection_stale',
    'turn_in_progress',
    'turn_parent_invalid',
    'turn_lane_advanced',
    'turn_superseded_by_human',
    # A normalized conversation already owns its transcript through Turn rows;
    # accepting a legacy message-array replacement would create a second
    # authority.  Keep this distinct from optimistic-CAS conflicts so clients
    # do not GET/rebase/re-PUT an operation that can never become valid.
    'conversation_authority_conflict',
    'database_integrity',
    # storage.v2 semantic admission rejects names outside the operation
    # catalog with this wire code. Clients rely on it to feature-detect
    # daemon control operations (system.health/system.metrics) across mixed
    # binary versions, so it must survive sanitization verbatim.
    'unknown_operation',
    # Durable transport/event payloads have explicit storage budgets. This is
    # a caller-correctable boundary failure, not an internal database fault.
    'storage_payload_too_large',
    'database_protocol_error',
    'database_internal',
    'plugin_storage_incompatible',
})


@dataclass(slots=True)
class StorageError(RuntimeError):
    """A sanitized, transport-stable storage failure."""

    code: str
    message: str
    retryable: bool = False
    retry_after_ms: int | None = None
    operation_id: str = ''
    request_not_dispatched: bool = False

    def __post_init__(self) -> None:
        RuntimeError.__init__(self, self.message)
        if self.code not in ERROR_CODES:
            self.code = 'database_internal'
        if self.retry_after_ms is not None:
            self.retry_after_ms = max(0, int(self.retry_after_ms))
        # This bit unlocks command replay, so truthy strings/integers must not
        # widen authority across a malformed or older wire implementation.
        self.request_not_dispatched = self.request_not_dispatched is True

    def to_payload(self) -> dict[str, Any]:
        payload = {
            'code': self.code,
            'message': self.message,
            'retryable': bool(self.retryable),
            'retry_after_ms': self.retry_after_ms,
            'operation_id': self.operation_id,
        }
        if self.request_not_dispatched:
            payload['request_not_dispatched'] = True
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> 'StorageError':
        return cls(
            code=str(payload.get('code') or 'database_internal'),
            message=str(payload.get('message') or 'Storage request failed'),
            retryable=bool(payload.get('retryable', False)),
            retry_after_ms=payload.get('retry_after_ms'),
            operation_id=str(payload.get('operation_id') or ''),
            request_not_dispatched=(
                payload.get('request_not_dispatched') is True),
        )


#: Storage classifications that are always safe to retry with the identical
#: operation (see lib.storage.retry for the replay-safety argument).
TRANSIENT_STORAGE_CODES = frozenset({
    'database_timeout',
    'database_busy',
    'database_unavailable',
})


def is_transient_storage_error(exc: BaseException | None) -> bool:
    """Classify by code through the cause chain.

    Wrappers such as ``EventDurabilityError`` lose the original
    ``StorageError`` flag surface but keep it as ``__cause__``; walk the
    chain (bounded) so classification happens at the site that catches.
    """
    seen = 0
    current: BaseException | None = exc
    while current is not None and seen < 8:
        if (isinstance(current, StorageError)
                and current.code in TRANSIENT_STORAGE_CODES):
            return True
        current = current.__cause__ or current.__context__
        seen += 1
    return False


def describe_storage_failure(exc: BaseException) -> str:
    """One-line diagnostic surface for a storage failure, for ops logs.

    ``str(StorageError)`` is only its message, which is frequently empty or an
    unclassified restatement of the code (a bare ``database_protocol_error``),
    so failure logs interpolating ``%s`` lose exactly the fields an operator
    needs to tell an over-deadline authority from an unwritable frame. Walk
    the cause chain (bounded, mirroring ``lib.storage.retry``) for the first
    ``StorageError`` and render its full classification; fall back to the
    exception type and message for non-storage failures.
    """
    current: BaseException | None = exc
    seen = 0
    while current is not None and seen < 8:
        if isinstance(current, StorageError):
            detail = (f'code={current.code} message={current.message!r} '
                      f'retryable={current.retryable} '
                      f'retry_after_ms={current.retry_after_ms}')
            if current.operation_id:
                detail += f' operation_id={current.operation_id}'
            return detail
        current = current.__cause__ or current.__context__
        seen += 1
    return f'{type(exc).__name__}: {exc}'


def http_status_for_storage_error(error: StorageError) -> int:
    """Map the stable storage taxonomy to the public HTTP contract."""
    if error.code == 'database_not_found':
        return 404
    if error.code == 'database_invalid_payload':
        return 400
    if error.code == 'database_unsupported':
        return 422
    if error.code == 'storage_payload_too_large':
        return 413
    if error.code == 'database_forbidden':
        return 403
    if error.code in {
        'database_conflict', 'conversation_authority_conflict',
        'turn_projection_stale', 'turn_in_progress', 'turn_parent_invalid',
        'turn_lane_advanced', 'turn_superseded_by_human',
    }:
        return 409
    if error.code in {
        'database_busy', 'database_unavailable', 'database_timeout',
    }:
        return 503
    return 500


__all__ = [
    'ERROR_CODES', 'StorageError', 'describe_storage_failure',
    'TRANSIENT_STORAGE_CODES', 'is_transient_storage_error',
    'http_status_for_storage_error',
]
