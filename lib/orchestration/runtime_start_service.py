"""Application facade for ephemeral and durable orchestration starts.

HTTP adapters prepare canonical definitions, then hand execution ownership to
this service.  It is the only place that assembles TaskRuntime metadata,
late-bound subflow lookup and durable-run identity.  Keeping both start modes
here prevents their worker wiring and pre-worker failure handling from
drifting apart.
"""

from __future__ import annotations

from lib.orchestration.errors import RuntimeStartError
from lib.orchestration.runtime_ports import (
    OrchestrationDefinitionProvider,
    OrchestrationDurableRunProvider,
    OrchestrationTaskRuntimePort,
)
from lib.orchestration.runtime_service import spawn_runtime_flow
from lib.orchestration.runtime_start_recovery import (
    recover_failed_durable_start,
)


class OrchestrationRuntimeStartService:
    """Start live or durable flows through one injected runtime boundary."""

    def __init__(
        self,
        runtime: OrchestrationTaskRuntimePort,
        *,
        definition_service: OrchestrationDefinitionProvider,
        run_service: OrchestrationDurableRunProvider | None = None,
    ):
        self._runtime = runtime
        self._definition_service = definition_service
        self._run_service = run_service

    def _subflow_resolver_provider(self):
        return lambda: self._definition_service().get_definition

    @staticmethod
    def _name(definition: dict) -> str:
        return str(definition.get('name') or '')

    def _start_ephemeral(
        self,
        definition: dict,
        *,
        input_text: str = '',
    ) -> str:
        """Create and spawn a transient Studio run."""
        try:
            return spawn_runtime_flow(
                self._runtime,
                definition,
                meta={'name': self._name(definition)},
                initial_context=input_text,
                subflow_resolver_provider=self._subflow_resolver_provider(),
            )
        except Exception as error:
            raise RuntimeStartError(
                'Failed to start orchestration run') from error

    def _start_durable(
        self,
        definition: dict,
        *,
        input_text: str = '',
        orchestration_id: str = '',
        created_by: str = '',
    ) -> str:
        """Persist, create and spawn one Task Mode run as an atomic handoff.

        Persistence must win before TaskRuntime becomes visible.  If runtime
        creation/spawn then fails, both projections are closed as ``error`` so
        neither a durable row nor an in-memory task remains permanently
        pending.
        """
        if self._run_service is None:
            raise RuntimeStartError('Durable run service is unavailable')
        runs = self._run_service()
        run_id = str(runs.create_new(
            definition=definition,
            input_text=input_text,
            orch_id=orchestration_id,
            name=self._name(definition),
            created_by=created_by,
        ) or '')
        if not run_id:
            raise RuntimeStartError(
                'Failed to create durable orchestration run')

        try:
            runtime_id = spawn_runtime_flow(
                self._runtime,
                definition,
                task_id=run_id,
                meta={'name': self._name(definition), 'run_id': run_id},
                initial_context=input_text,
                subflow_resolver_provider=self._subflow_resolver_provider(),
                durable_runs=runs,
            )
        except Exception as error:
            recover_failed_durable_start(
                self._runtime, runs, run_id, error)
            raise RuntimeStartError(
                'Failed to start durable orchestration run',
                run_id=run_id,
            ) from error

        if runtime_id != run_id:
            mismatch = RuntimeError(
                'runtime did not preserve durable orchestration run id')
            recover_failed_durable_start(
                self._runtime, runs, run_id, mismatch)
            raise RuntimeStartError(
                'Failed to start durable orchestration run',
                run_id=run_id,
            ) from mismatch
        return run_id

    def start(
        self,
        kind: str,
        definition: dict,
        *,
        input_text: str = '',
        orchestration_id: str = '',
        created_by: str = '',
    ) -> str:
        """Execute either start mode through one delivery-layer command."""
        if kind == 'ephemeral':
            return self._start_ephemeral(
                definition,
                input_text=input_text,
            )
        if kind == 'durable':
            return self._start_durable(
                definition,
                input_text=input_text,
                orchestration_id=orchestration_id,
                created_by=created_by,
            )
        raise RuntimeStartError(
            f'Unsupported orchestration runtime start kind: {kind!r}')

    def start_ephemeral(
        self,
        definition: dict,
        *,
        input_text: str = '',
    ) -> str:
        """Compatibility wrapper over the canonical start command."""
        return self.start(
            'ephemeral', definition, input_text=input_text)

    def start_durable(
        self,
        definition: dict,
        *,
        input_text: str = '',
        orchestration_id: str = '',
        created_by: str = '',
    ) -> str:
        """Compatibility wrapper over the canonical start command."""
        return self.start(
            'durable',
            definition,
            input_text=input_text,
            orchestration_id=orchestration_id,
            created_by=created_by,
        )


__all__ = [
    'RuntimeStartError',
    'OrchestrationRuntimeStartService',
]
