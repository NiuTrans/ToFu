"""Read and replay collaborator behind ``OrchestrationRunService``."""

from __future__ import annotations

from collections.abc import Callable

from lib.orchestration.run_replay_result import (
    RunReplayResult,
    normalize_run_replay_cursor,
    project_run_replay_result,
)
from lib.orchestration.run_service_context import DurableRunServiceContext


class DurableRunQueryService:
    def __init__(
        self,
        context: DurableRunServiceContext,
        *,
        project_header: Callable[[dict | None], dict | None],
    ):
        self._context = context
        self._project_header = project_header

    def get(self, run_id: str) -> dict | None:
        run = self._context.persistence_call(
            f'failed to read run {run_id}',
            lambda: self._context.persistence.get_run(run_id),
        )
        return self._project_header(run)

    def list(self, *, status: str = '', orch_id: str = '',
             limit: int = 50) -> list[dict]:
        self._context.require_status(status, allow_empty=True)
        runs = self._context.persistence_call(
            'failed to list runs',
            lambda: self._context.persistence.list_runs(
                status=status, orch_id=orch_id, limit=limit),
        )
        return [self._project_header(run) for run in runs]  # type: ignore[misc]

    def replay(self, run_id: str, cursor: int = 0) \
            -> RunReplayResult | None:
        run = self.get(run_id)
        if run is None:
            return None
        safe_cursor = normalize_run_replay_cursor(cursor)
        page = self._context.persistence_call(
            f'failed to replay run {run_id}',
            lambda: self._context.persistence.get_event_page(
                run_id, safe_cursor),
        )
        return project_run_replay_result(
            run=run,
            requested_cursor=safe_cursor,
            events=page.events,
            next_cursor=page.next_cursor,
            cursor_reset=page.cursor_reset,
            caught_up=page.caught_up,
        )


__all__ = ['DurableRunQueryService']
