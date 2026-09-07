"""Abort, delete and classified transition collaborator for durable runs."""

from __future__ import annotations

from lib.orchestration.application_service_ports import (
    RuntimeMutationServicePort,
)
from lib.orchestration.errors import RunServiceError
from lib.orchestration.mutation_result import RunMutationResult
from lib.orchestration.run_command_service import DurableRunCommandService
from lib.orchestration.run_lifecycle_policy import (
    abort_precondition,
    abort_runtime_conflict,
    classify_abort_transition,
    classify_delete_commit,
    classify_transition,
    delete_precondition,
)
from lib.orchestration.run_query_service import DurableRunQueryService
from lib.orchestration.run_service_context import DurableRunServiceContext


class DurableRunMutationService:
    def __init__(
        self,
        context: DurableRunServiceContext,
        queries: DurableRunQueryService,
        commands: DurableRunCommandService,
        *,
        runtime_mutation: RuntimeMutationServicePort | None = None,
    ):
        self._context = context
        self._queries = queries
        self._commands = commands
        self._runtime_mutation = runtime_mutation

    def transition_status(self, run_id: str, status: str, *,
                          final: str | None = None,
                          error: dict | str | None = None,
                          ) -> RunMutationResult:
        error = self._context.run_error(
            error, context='run status transition')
        committed = self._commands.update_status(
            run_id, status, final=final, error=error)
        current = None if committed else self._queries.get(run_id)
        return classify_transition(
            run_id,
            status,
            committed=committed,
            current=current,
            final=final,
            error=error,
        )

    def abort(self, run_id: str) -> RunMutationResult:
        run = self._queries.get(run_id)
        blocked = abort_precondition(run_id, run)
        if blocked is not None:
            return blocked
        assert run is not None
        if self._runtime_mutation is None:
            raise RunServiceError('runtime abort dependency is unavailable')
        if not self._runtime_mutation.abort(run_id).ok:
            return abort_runtime_conflict(run_id, run)
        transition = self.transition_status(run_id, 'aborted')
        return classify_abort_transition(run_id, run, transition)

    def delete(self, run_id: str) -> RunMutationResult:
        run = self._queries.get(run_id)
        blocked = delete_precondition(run_id, run)
        if blocked is not None:
            return blocked
        assert run is not None
        deleted = self._context.persistence_call(
            f'failed to delete run {run_id}',
            lambda: self._context.persistence.delete_run(run_id),
        )
        return classify_delete_commit(run_id, run, deleted=bool(deleted))


__all__ = ['DurableRunMutationService']
