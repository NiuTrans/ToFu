"""Unified application facade for durable orchestration runs.

HTTP routes, background workers and non-HTTP consumers keep one stable public
interface. Focused query, command and mutation collaborators share one bound
persistence/error context behind this facade.
"""

from __future__ import annotations

from lib.orchestration.application_service_ports import (
    RuntimeMutationServicePort,
)
from lib.orchestration.errors import RunServiceError
from lib.orchestration.mutation_result import (
    MUTATION_ACTIVE,
    MUTATION_CONFLICT,
    MUTATION_NOT_FOUND,
    MUTATION_PERSISTENCE_FAILED,
    MUTATION_TERMINAL,
    RunMutationResult,
)
from lib.orchestration.outcome_projection import project_run_header_outcome
from lib.orchestration.run_command_service import DurableRunCommandService
from lib.orchestration.run_mutation_service import DurableRunMutationService
from lib.orchestration.run_query_service import DurableRunQueryService
from lib.orchestration.run_replay_result import RunReplayResult
from lib.orchestration.run_service_context import DurableRunServiceContext
from lib.orchestration.run_store_port import (
    OrchestrationRunStorePort,
    bind_orchestration_run_store,
)


RUN_MUTATION_NOT_FOUND = MUTATION_NOT_FOUND
RUN_MUTATION_TERMINAL = MUTATION_TERMINAL
RUN_MUTATION_ACTIVE = MUTATION_ACTIVE
RUN_MUTATION_CONFLICT = MUTATION_CONFLICT
RUN_MUTATION_PERSISTENCE_FAILED = MUTATION_PERSISTENCE_FAILED


class OrchestrationRunService:
    """Single durable-run interface above focused internal collaborators."""

    def __init__(
        self,
        persistence: OrchestrationRunStorePort | None = None,
        *,
        runtime_mutation: RuntimeMutationServicePort | None = None,
    ):
        if persistence is None:
            from lib.orchestration_runs import database_run_store
            persistence = database_run_store()
        self._persistence = bind_orchestration_run_store(persistence)
        context = DurableRunServiceContext(self._persistence)
        self._queries = DurableRunQueryService(
            context, project_header=project_run_header_outcome)
        self._commands = DurableRunCommandService(context)
        self._mutations = DurableRunMutationService(
            context,
            self._queries,
            self._commands,
            runtime_mutation=runtime_mutation,
        )

    def new_id(self) -> str:
        return self._commands.new_id()

    def create(self, run_id: str, *, definition: dict,
               input_text: str = '', orch_id: str = '', name: str = '',
               created_by: str = '') -> bool:
        return self._commands.create(
            run_id,
            definition=definition,
            input_text=input_text,
            orch_id=orch_id,
            name=name,
            created_by=created_by,
        )

    def create_new(self, *, definition: dict,
                   input_text: str = '', orch_id: str = '', name: str = '',
                   created_by: str = '') -> str:
        return self._commands.create_new(
            definition=definition,
            input_text=input_text,
            orch_id=orch_id,
            name=name,
            created_by=created_by,
        )

    def get(self, run_id: str) -> dict | None:
        return self._queries.get(run_id)

    def list(self, *, status: str = '', orch_id: str = '',
             limit: int = 50) -> list[dict]:
        return self._queries.list(status=status, orch_id=orch_id, limit=limit)

    def append_event(self, run_id: str, seq: int, event: dict) -> bool:
        return self._commands.append_event(run_id, seq, event)

    def project_event(
        self, run_id: str, seq: int, event: dict, status: str = '',
    ) -> bool:
        return self._commands.project_event(run_id, seq, event, status)

    def update_status(self, run_id: str, status: str, *,
                      final: str | None = None,
                      error: dict | str | None = None) -> bool:
        return self._commands.update_status(
            run_id, status, final=final, error=error)

    def transition_status(self, run_id: str, status: str, *,
                          final: str | None = None,
                          error: dict | str | None = None,
                          ) -> RunMutationResult:
        return self._mutations.transition_status(
            run_id, status, final=final, error=error)

    def replay(self, run_id: str, cursor: int = 0) \
            -> RunReplayResult | None:
        return self._queries.replay(run_id, cursor)

    def retire_interrupted(self, *, error: dict | str) -> int:
        return self._commands.retire_interrupted(error=error)

    def abort(self, run_id: str) -> RunMutationResult:
        return self._mutations.abort(run_id)

    def delete(self, run_id: str) -> RunMutationResult:
        return self._mutations.delete(run_id)


__all__ = [
    'RUN_MUTATION_NOT_FOUND', 'RUN_MUTATION_TERMINAL', 'RUN_MUTATION_ACTIVE',
    'RUN_MUTATION_CONFLICT', 'RUN_MUTATION_PERSISTENCE_FAILED',
    'RunServiceError', 'RunMutationResult', 'RunReplayResult',
    'OrchestrationRunService',
]
