"""Best-effort recovery after a durable runtime worker fails to start."""

from __future__ import annotations

from lib.error_envelope import make_envelope
from lib.log import get_logger
from lib.orchestration.durable_projection import DurableRunProjection
from lib.orchestration.runtime_ports import (
    OrchestrationDurableRunPort,
    OrchestrationTaskRuntimePort,
)


logger = get_logger(__name__)


def recover_failed_durable_start(
    runtime: OrchestrationTaskRuntimePort,
    runs: OrchestrationDurableRunPort,
    run_id: str,
    error: Exception,
) -> None:
    """Close both runtime projections without masking the primary failure."""
    try:
        runtime.finish(
            run_id,
            error=error,
            error_context='orchestration:start',
        )
    except Exception:
        logger.error(
            '[OrchestrationStart] failed to close runtime task run=%s',
            run_id,
            exc_info=True,
        )

    try:
        DurableRunProjection(runs, run_id).record_error(
            make_envelope(
                'internal',
                message='Runtime worker could not be started',
                detail=str(error),
                context='durable start failure',
                source='orchestration:runtime-start',
                retryable=False,
            ),
            context='durable start failure',
        )
    except Exception:
        logger.error(
            '[OrchestrationStart] failed to close durable run=%s',
            run_id,
            exc_info=True,
        )


__all__ = ['recover_failed_durable_start']
