"""Shared HTTP error projection for conversation-turn route versions."""

from __future__ import annotations

from lib.api_response import (
    api_conflict,
    api_error,
    api_service_unavailable,
)
from lib.conversation_sync.validation import ContractViolation
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
            detail=(
                "数据库暂时无法完成请求，请稍后重试。 / "
                "Storage temporarily unavailable; retry shortly.\n"
                + exc.message
            ),
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
    if status == 413:
        logger.warning(
            "Conversation turn exceeds storage payload bound op=%s code=%s",
            operation,
            exc.code,
        )
        return api_error(
            "This turn is too large to save. Remove a large inline attachment and retry.",
            status=413,
            **extras,
        )
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
    return api_error(
        envelope, status=status, public_500_detail=exc.code, **extras,
    )


def contract_violation_response(exc: ContractViolation, *, operation: str):
    """Surface a durable-data contract drift as a typed, informative 500.

    The fail-closed decode boundary proving stored Turns no longer match the
    generated schema is a server-side defect class of its own — collapsing it
    into the redacted uncaught boundary left the browser with a bare
    "内部错误 / Internal error" toast and no recovery signal (2026-09-20, conv
    mu5p3vn9x32wcd). The violation strings are machine-generated schema paths
    and fixed phrases, never user content, credentials, or filesystem paths,
    so they are safe to carry in the occurrence detail.
    """
    violations = [str(item) for item in exc.violations[:8]]
    summary = "; ".join(violations[:3])
    if len(violations) > 3:
        summary += f"; … (+{len(exc.violations) - 3} more)"
    envelope = make_envelope(
        "internal",
        detail=f"{exc.schema_name}: {summary}",
        context=operation,
        source="conversation-sync",
        retryable=False,
        extensions={
            "contractSchema": exc.schema_name,
            "contractViolations": violations,
        },
    )
    logger.error(
        "Conversation contract violation op=%s schema=%s: %s",
        operation, exc.schema_name, summary)
    return api_error(
        envelope, status=500,
        public_500_detail=f"{exc.schema_name}: {summary}",
    )


__all__ = [
    "contract_violation_response",
    "lifecycle_conflict_response",
    "storage_failure_response",
]
