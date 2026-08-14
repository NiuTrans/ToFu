"""Structural persistence port for durable orchestration runs.

The DB-backed module, focused test stores and future persistence adapters all
bind through this one capability set.  Application services therefore do not
probe optional methods or carry storage-version branches in business logic.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol, cast


ORCHESTRATION_RUN_EVENT_PAGE_LIMIT = 2000


@dataclass(frozen=True)
class RunEventPage:
    """Authoritative durable-log page returned by every run store.

    ``caught_up`` is storage evidence, not an application-layer guess based
    on page length. Iteration retains the historical three-value unpacking
    surface for the low-level compatibility facade; new code uses fields.
    """

    events: list[dict]
    next_cursor: int
    cursor_reset: bool = False
    caught_up: bool = True

    def __iter__(self) -> Iterator[list[dict] | int | bool]:
        yield self.events
        yield self.next_cursor
        yield self.cursor_reset


class OrchestrationRunStoreError(RuntimeError):
    """The durable-run store is unavailable or a read could not complete."""


class OrchestrationRunStorePort(Protocol):
    """Complete storage interface consumed by ``OrchestrationRunService``."""

    def new_run_id(self) -> str: ...

    def create_run(
        self,
        run_id: str,
        *,
        definition: dict,
        input_text: str = '',
        orch_id: str = '',
        name: str = '',
        created_by: str = '',
    ) -> bool: ...

    def get_run(self, run_id: str) -> dict | None: ...

    def list_runs(
        self,
        *,
        status: str = '',
        orch_id: str = '',
        limit: int = 50,
    ) -> list[dict]: ...

    def append_event(self, run_id: str, seq: int, event: dict) -> bool: ...

    def project_event(
        self,
        run_id: str,
        seq: int,
        event: dict,
        status: str = '',
    ) -> bool: ...

    def get_event_page(
        self,
        run_id: str,
        cursor: int = 0,
    ) -> RunEventPage: ...

    def update_status(
        self,
        run_id: str,
        status: str,
        *,
        final: str | None = None,
        error: dict | str | None = None,
    ) -> bool: ...

    def retire_interrupted_runs(self, error: dict | str) -> int | None: ...

    def delete_run(self, run_id: str) -> bool: ...


_REQUIRED_METHODS = (
    'new_run_id',
    'create_run',
    'get_run',
    'list_runs',
    'append_event',
    'project_event',
    'get_event_page',
    'update_status',
    'retire_interrupted_runs',
    'delete_run',
)


def bind_orchestration_run_store(candidate: object) \
        -> OrchestrationRunStorePort:
    """Validate a concrete store once at the composition boundary.

    Protocols keep implementations structurally interchangeable; this small
    runtime guard turns an incomplete adapter into an immediate, actionable
    configuration error instead of a late AttributeError during polling.
    """
    missing = [
        name for name in _REQUIRED_METHODS
        if not callable(getattr(candidate, name, None))
    ]
    if missing:
        raise TypeError(
            'invalid orchestration run store; missing callable(s): '
            + ', '.join(missing)
        )
    return cast(OrchestrationRunStorePort, candidate)


__all__ = [
    'ORCHESTRATION_RUN_EVENT_PAGE_LIMIT',
    'RunEventPage',
    'OrchestrationRunStoreError',
    'OrchestrationRunStorePort',
    'bind_orchestration_run_store',
]
