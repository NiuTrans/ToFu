"""Composed database adapter for :class:`OrchestrationRunStorePort`.

The application sees one complete durable-run store. Header lifecycle and
append-only replay remain separate repositories below that port so their SQL,
serialization and tests can evolve without growing another god module.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from typing import Any

from lib.log import get_logger
from lib.orchestration.run_event_repository import (
    OrchestrationRunEventRepository,
)
from lib.orchestration.run_deletion_repository import (
    OrchestrationRunDeletionRepository,
)
from lib.orchestration.run_header_repository import (
    OrchestrationRunHeaderRepository,
)
from lib.orchestration.run_projection_repository import (
    OrchestrationRunProjectionRepository,
)
from lib.orchestration.run_store_port import RunEventPage
from lib.timeutil import now_ms


logger = get_logger(__name__)


class DatabaseOrchestrationRunStore:
    """Complete DB-backed store composed from header and event repositories."""

    def __init__(self, database: Callable[[], Any],
                 clock: Callable[[], int] = now_ms):
        self._headers = OrchestrationRunHeaderRepository(database, clock)
        self._events = OrchestrationRunEventRepository(database, clock)
        self._projection = OrchestrationRunProjectionRepository(
            database, clock)
        self._deletion = OrchestrationRunDeletionRepository(database)

    def new_run_id(self) -> str:
        return 'run_' + hex(int(time.time() * 1000))[2:] + secrets.token_hex(2)

    def create_run(self, run_id: str, *, definition: dict,
                   input_text: str = '', orch_id: str = '', name: str = '',
                   created_by: str = '') -> bool:
        return self._headers.create(
            run_id,
            definition=definition,
            input_text=input_text,
            orch_id=orch_id,
            name=name,
            created_by=created_by,
        )

    def update_status(self, run_id: str, status: str, *,
                      final: str | None = None,
                      error: dict | str | None = None) -> bool:
        return self._headers.update_status(
            run_id, status, final=final, error=error)

    def retire_interrupted_runs(self, error: dict | str) -> int | None:
        return self._headers.retire_interrupted(error)

    def get_run(self, run_id: str) -> dict | None:
        return self._headers.get(run_id)

    def list_runs(self, *, status: str = '', orch_id: str = '',
                  limit: int = 50) -> list[dict]:
        return self._headers.list(
            status=status, orch_id=orch_id, limit=limit)

    def append_event(self, run_id: str, seq: int, event: dict) -> bool:
        return self._events.append(run_id, seq, event)

    def project_event(
        self, run_id: str, seq: int, event: dict, status: str = '',
    ) -> bool:
        return self._projection.project(run_id, seq, event, status)

    def get_event_page(self, run_id: str, cursor: int = 0) -> RunEventPage:
        return self._events.page(run_id, cursor)

    def get_events(self, run_id: str, cursor: int = 0) -> list[dict]:
        requested = max(0, int(cursor or 0))
        collected: list[dict] = []
        while True:
            page = self.get_event_page(run_id, requested)
            if page.cursor_reset:
                return collected
            collected.extend(page.events)
            if page.caught_up or page.next_cursor <= requested:
                return collected
            requested = page.next_cursor

    def delete_run(self, run_id: str) -> bool:
        deleted = self._deletion.delete(run_id)
        if deleted:
            logger.debug('[OrchRuns] deleted run %s', run_id)
        return deleted


__all__ = ['DatabaseOrchestrationRunStore']
