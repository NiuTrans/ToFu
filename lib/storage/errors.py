"""Storage client facade for the shared, backend-neutral failure contract.

Also owns StorageStartRefused, the supervisor's typed deterministic start
refusal, which StorageRuntime keeps out of its crash breaker.
"""

from lib.agent_core.storage_errors import (
    ERROR_CODES,
    TRANSIENT_STORAGE_CODES,
    is_resource_bound_refusal,
    is_transient_storage_error,
    StorageError,
    ProjectionRebaseRequired,
    describe_storage_failure,
    http_status_for_storage_error,
)


class StorageStartRefused(StorageError):
    """A deterministic start refusal: repeating the same start cannot succeed.

    ``reason`` is a stable machine code and ``remedy`` the operator action.
    Repetition is not crash pressure: StorageRuntime reports it as the
    ``refused`` state and rechecks at a fixed slow cadence instead of feeding
    the restart breaker (2026-10-27: five identical admission refusals inside
    three seconds latched storage for 5 h 23 min).
    """

    def __init__(self, reason: str, detail: str, remedy: str) -> None:
        super().__init__('database_unavailable', f'{detail}; {remedy}', retryable=False)
        self.reason = reason
        self.detail = detail
        self.remedy = remedy


__all__ = [
    'ERROR_CODES', 'StorageError', 'ProjectionRebaseRequired', 'describe_storage_failure',
    'StorageStartRefused', 'TRANSIENT_STORAGE_CODES', 'is_resource_bound_refusal',
    'is_transient_storage_error', 'http_status_for_storage_error',
]
