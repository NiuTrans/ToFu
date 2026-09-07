"""Stable error envelope shared by storage clients and the sidecar."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


ERROR_CODES = frozenset({
    'database_not_found',
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


def http_status_for_storage_error(error: StorageError) -> int:
    """Map the stable storage taxonomy to the public HTTP contract."""
    if error.code == 'database_not_found':
        return 404
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
    'ERROR_CODES', 'StorageError', 'http_status_for_storage_error',
]
