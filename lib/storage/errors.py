"""Storage client facade for the shared, backend-neutral failure contract."""

from lib.agent_core.storage_errors import (
    ERROR_CODES,
    TRANSIENT_STORAGE_CODES,
    is_transient_storage_error,
    StorageError,
    describe_storage_failure,
    http_status_for_storage_error,
)

__all__ = [
    'ERROR_CODES', 'StorageError', 'describe_storage_failure',
    'TRANSIENT_STORAGE_CODES', 'is_transient_storage_error',
    'http_status_for_storage_error',
]
