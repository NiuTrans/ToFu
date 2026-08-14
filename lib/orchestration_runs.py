"""Compatibility facade for the Sidecar-backed orchestration run store.

New application code depends on :class:`OrchestrationRunStorePort` through
``OrchestrationRunService``. These module functions remain for older callers
and direct persistence tests while delegating to the same composed database
adapter used by the service.
"""

from __future__ import annotations

from lib.orchestration.sidecar_run_store import SidecarOrchestrationRunStore
from lib.orchestration.run_store_port import (
    OrchestrationRunStoreError,
    OrchestrationRunStorePort,
    RunEventPage,
)
def database_run_store() -> OrchestrationRunStorePort:
    """Build the canonical semantic Sidecar adapter."""
    return SidecarOrchestrationRunStore()


def new_run_id() -> str:
    return database_run_store().new_run_id()


def create_run(run_id: str, *, definition: dict, input_text: str = '',
               orch_id: str = '', name: str = '', created_by: str = '') -> bool:
    return database_run_store().create_run(
        run_id,
        definition=definition,
        input_text=input_text,
        orch_id=orch_id,
        name=name,
        created_by=created_by,
    )


def update_status(run_id: str, status: str, *, final: str | None = None,
                  error: dict | str | None = None) -> bool:
    return database_run_store().update_status(
        run_id, status, final=final, error=error)


def retire_interrupted_runs(error: dict | str) -> int | None:
    return database_run_store().retire_interrupted_runs(error)


def get_run(run_id: str) -> dict | None:
    return database_run_store().get_run(run_id)


def list_runs(*, status: str = '', orch_id: str = '',
              limit: int = 50) -> list[dict]:
    return database_run_store().list_runs(
        status=status, orch_id=orch_id, limit=limit)


def append_event(run_id: str, seq: int, event: dict) -> bool:
    return database_run_store().append_event(run_id, seq, event)


def project_event(
    run_id: str,
    seq: int,
    event: dict,
    status: str = '',
) -> bool:
    return database_run_store().project_event(run_id, seq, event, status)


def get_event_page(run_id: str, cursor: int = 0) -> RunEventPage:
    return database_run_store().get_event_page(run_id, cursor)


def get_events(run_id: str, cursor: int = 0) -> list[dict]:
    return database_run_store().get_events(run_id, cursor)


def delete_run(run_id: str) -> bool:
    return database_run_store().delete_run(run_id)


__all__ = [
    'OrchestrationRunStoreError', 'SidecarOrchestrationRunStore',
    'database_run_store', 'new_run_id', 'create_run', 'update_status',
    'retire_interrupted_runs', 'get_run', 'list_runs', 'append_event',
    'project_event', 'get_event_page', 'get_events', 'delete_run',
]
