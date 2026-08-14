"""Create, event and lifecycle-write collaborator for durable runs."""

from __future__ import annotations

from lib.orchestration.errors import RunServiceError
from lib.orchestration.run_service_context import DurableRunServiceContext


class DurableRunCommandService:
    def __init__(self, context: DurableRunServiceContext):
        self._context = context

    def new_id(self) -> str:
        return str(self._context.persistence_call(
            'failed to allocate orchestration run id',
            self._context.persistence.new_run_id,
        ))

    def create(self, run_id: str, *, definition: dict,
               input_text: str = '', orch_id: str = '', name: str = '',
               created_by: str = '') -> bool:
        return bool(self._context.persistence_call(
            f'failed to create run {run_id}',
            lambda: self._context.persistence.create_run(
                run_id,
                definition=definition,
                input_text=input_text,
                orch_id=orch_id,
                name=name,
                created_by=created_by,
            ),
        ))

    def create_new(self, *, definition: dict,
                   input_text: str = '', orch_id: str = '', name: str = '',
                   created_by: str = '') -> str:
        run_id = self.new_id()
        created = self.create(
            run_id,
            definition=definition,
            input_text=input_text,
            orch_id=orch_id,
            name=name,
            created_by=created_by,
        )
        return run_id if created else ''

    def append_event(self, run_id: str, seq: int, event: dict) -> bool:
        return bool(self._context.persistence_call(
            f'failed to append event {run_id}/{seq}',
            lambda: self._context.persistence.append_event(
                run_id, seq, event),
        ))

    def project_event(
        self, run_id: str, seq: int, event: dict, status: str = '',
    ) -> bool:
        if status:
            self._context.require_status(status)
        return bool(self._context.persistence_call(
            f'failed to project event {run_id}/{seq}',
            lambda: self._context.persistence.project_event(
                run_id, seq, event, status),
        ))

    def update_status(self, run_id: str, status: str, *,
                      final: str | None = None,
                      error: dict | str | None = None) -> bool:
        self._context.require_status(status)
        error = self._context.run_error(
            error, context='run status update')
        return bool(self._context.persistence_call(
            f'failed to update run {run_id} to {status}',
            lambda: self._context.persistence.update_status(
                run_id, status, final=final, error=error),
        ))

    def retire_interrupted(self, *, error: dict | str) -> int:
        message = 'failed to retire interrupted orchestration runs'
        error = self._context.run_error(
            error, context='run startup recovery')
        retired = self._context.persistence_call(
            message,
            lambda: self._context.persistence.retire_interrupted_runs(error),
        )
        if retired is None:
            raise RunServiceError(message)
        return max(0, int(retired))


__all__ = ['DurableRunCommandService']
