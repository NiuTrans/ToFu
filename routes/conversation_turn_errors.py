"""Shared HTTP error projection for conversation-turn route versions."""

from __future__ import annotations

from lib.api_response import (
    api_conflict,
    api_error,
    api_service_unavailable,
)
from lib.error_envelope import make_envelope
from lib.log import get_logger
from lib.storage.errors import StorageError, http_status_for_storage_error
from lib.turn_lifecycle import LifecycleConflict


logger = get_logger(__name__)


def lifecycle_conflict_response(exc: LifecycleConflict):
    return api_conflict(
        {"kind": exc.code, "message": exc.message}, latestTurn=exc.turn
    )


def storage_failure_response(exc: StorageError, *, operation: str):
    """Expose the stable storage taxonomy without laundering it into 500."""
    status = http_status_for_storage_error(exc)
    retry_after_ms = max(0, int(exc.retry_after_ms or 0))
    extras = {
        "storageCode": exc.code,
        "retryAfterMs": retry_after_ms,
        "operationId": exc.operation_id,
    }
    if status == 503:
        retry_after_s = max(1, (retry_after_ms + 999) // 1000)
        envelope = make_envelope(
            "server_busy",
            detail=exc.message,
            context=operation,
            source="storage",
            retryable=bool(exc.retryable),
            extensions={
                "storageCode": exc.code,
                "operationId": exc.operation_id,
            },
        )
        logger.warning(
            "Conversation storage temporarily unavailable op=%s code=%s",
            operation,
            exc.code,
        )
        return api_service_unavailable(
            envelope,
            retry_after=retry_after_s,
            retryAfter=retry_after_s,
            **extras,
        )
    if status in {404, 409}:
        return api_error(exc.message, status=status, **extras)
    envelope = make_envelope(
        "internal",
        detail=exc.message,
        context=operation,
        source="storage",
        retryable=False,
        extensions={
            "storageCode": exc.code,
            "operationId": exc.operation_id,
        },
    )
    logger.error("Conversation storage failure op=%s code=%s", operation, exc.code)
    return api_error(envelope, status=status, **extras)


__all__ = ["lifecycle_conflict_response", "storage_failure_response"]
