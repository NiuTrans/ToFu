"""Application boundary for transient orchestration runtime mutations."""

from __future__ import annotations

from lib.orchestration.errors import RuntimeMutationError
from lib.orchestration.service_call import orchestration_dependency_call
from lib.orchestration.mutation_operations import runtime_abort_mutation
from lib.orchestration.mutation_result import OrchestrationMutationResult
from lib.task_runtime_ports import TaskAbortRuntimePort


class OrchestrationRuntimeMutationService:
    """Classify TaskRuntime mutations behind the shared service error seam."""

    def __init__(self, runtime: TaskAbortRuntimePort):
        self._runtime = runtime

    def abort(self, task_id: str) -> OrchestrationMutationResult:
        return orchestration_dependency_call(
            lambda: runtime_abort_mutation(self._runtime, task_id),
            error_type=RuntimeMutationError,
            message='failed to abort transient orchestration run',
        )


__all__ = [
    'RuntimeMutationError',
    'OrchestrationRuntimeMutationService',
]
