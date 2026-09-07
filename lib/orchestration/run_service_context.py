"""Shared persistence, status and error boundary for durable-run services."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from lib.error_envelope import normalize_envelope
from lib.orchestration.errors import RunServiceError
from lib.orchestration.run_status import is_run_status
from lib.orchestration.run_store_port import OrchestrationRunStorePort
from lib.orchestration.service_call import orchestration_dependency_call


class DurableRunServiceContext:
    """One bound store and one failure vocabulary shared by collaborators."""

    def __init__(self, persistence: OrchestrationRunStorePort):
        self.persistence = persistence

    def persistence_call(
        self,
        message: str,
        callback: Callable[[], Any],
    ) -> Any:
        return orchestration_dependency_call(
            callback,
            error_type=RunServiceError,
            message=message,
        )

    @staticmethod
    def run_error(
        error: dict | str | None,
        *,
        context: str,
    ) -> dict | None:
        return normalize_envelope(
            error,
            context=context,
            source='orchestration:run-service',
            require_complete=True,
        )

    @staticmethod
    def require_status(status: str, *, allow_empty: bool = False) -> None:
        if allow_empty and not status:
            return
        if not is_run_status(status):
            raise RunServiceError(
                f'invalid orchestration run status: {status!r}')


__all__ = ['DurableRunServiceContext']
