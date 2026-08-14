"""Stable error envelope shared by storage clients and the sidecar."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


ERROR_CODES = frozenset({
    'database_busy',
    'database_unavailable',
    'database_timeout',
    'database_conflict',
    'database_integrity',
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

    def __post_init__(self) -> None:
        RuntimeError.__init__(self, self.message)
        if self.code not in ERROR_CODES:
            self.code = 'database_internal'
        if self.retry_after_ms is not None:
            self.retry_after_ms = max(0, int(self.retry_after_ms))

    def to_payload(self) -> dict[str, Any]:
        return {
            'code': self.code,
            'message': self.message,
            'retryable': bool(self.retryable),
            'retry_after_ms': self.retry_after_ms,
            'operation_id': self.operation_id,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> 'StorageError':
        return cls(
            code=str(payload.get('code') or 'database_internal'),
            message=str(payload.get('message') or 'Storage request failed'),
            retryable=bool(payload.get('retryable', False)),
            retry_after_ms=payload.get('retry_after_ms'),
            operation_id=str(payload.get('operation_id') or ''),
        )


def http_status_for_storage_error(error: StorageError) -> int:
    """Map the stable storage taxonomy to the public HTTP contract."""
    if error.code == 'database_conflict':
        return 409
    if error.code in {
        'database_busy', 'database_unavailable', 'database_timeout',
    }:
        return 503
    return 500


def coerce_legacy_storage_error(error: BaseException) -> StorageError | None:
    """Classify migration-era driver exceptions without importing a driver.

    This bridge uses numeric SQLite result codes / PostgreSQL SQLSTATE, never
    localized exception text.  It can disappear with the last in-process
    repository; keeping it here prevents HTTP and scheduler code from learning
    driver types or parsing messages during that migration.
    """
    module = type(error).__module__.split('.')[0]
    if module == 'sqlite3':
        raw = int(getattr(error, 'sqlite_errorcode', 0) or 0) & 0xFF
        if raw in {5, 6} or (not raw and type(error).__name__ == 'OperationalError'):
            return StorageError('database_busy', 'Database temporarily busy', True, 50)
        if raw == 9:
            return StorageError('database_timeout', 'Database request timed out', True, 50)
        if raw in {10, 13, 14}:
            return StorageError('database_unavailable', 'Database unavailable', True, 100)
        if raw in {11, 19, 26}:
            return StorageError('database_integrity', 'Database integrity failure')
        return None
    state = str(getattr(error, 'pgcode', '') or '')
    if state in {'40001', '40P01', '55P03'}:
        return StorageError('database_busy', 'Database temporarily busy', True, 50)
    if state == '57014':
        return StorageError('database_timeout', 'Database request timed out', True, 50)
    if state.startswith('08'):
        return StorageError('database_unavailable', 'Database unavailable', True, 100)
    if state in {'23505', '23P01'}:
        return StorageError('database_conflict', 'Database conflict')
    if state.startswith('23'):
        return StorageError('database_integrity', 'Database integrity failure')
    return None


__all__ = [
    'ERROR_CODES', 'StorageError', 'coerce_legacy_storage_error',
    'http_status_for_storage_error',
]
