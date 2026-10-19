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
        # Capture the owner-bound repository while the authenticated request
        # context still exists. The worker receives only a plain lookup
        # callable and never reaches back into framework globals.
        definitions = self._definition_service()
        return lambda: definitions.get_definition

    @staticmethod
    def _name(definition: dict) -> str:
        return str(definition.get('name') or '')

    def _start_ephemeral(
        self,
        definition: dict,
        *,
        owner_user_id: int,
        input_text: str = '',
    ) -> str:
        """Create and spawn a transient Studio run."""
        try:
            return spawn_runtime_flow(
                self._runtime,
                definition,
                owner_user_id=owner_user_id,
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
        owner_user_id: int,
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
                owner_user_id=owner_user_id,
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

    def resolve_human_decision(self, request_id, answer, *, owner_user_id, mode):
        """Bind owner repositories before handing a durable decision to a worker."""
        from lib.orchestration.continuation import resolve
        if self._run_service is None:
            raise RuntimeStartError('Durable run service is unavailable')
        return resolve(self._run_service(), self._runtime,
                       self._subflow_resolver_provider(), request_id, answer,
                       owner_user_id=owner_user_id, mode=mode)

    def resume(self, source_run_id: str, *, owner_user_id: int) -> str:
        """Fork a restart-interrupted run, reusing committed effect receipts."""
        from lib.orchestration.run_recovery import (
            RunRecoveryError, load_recovery_records, recovery_run_id, record_recovery_claim,
        )
        if self._run_service is None:
            raise RuntimeStartError('Durable run service is unavailable')
        runs = self._run_service()
        # Resolve source through the authenticated owner's repository even on
        # an idempotent retry; no cross-owner existence/identity disclosure.
        source = runs.get_execution_snapshot(source_run_id)
        if source is None:
            raise RunRecoveryError('Run not found', not_found=True)
        from lib.goal_runs.contract import GOAL_RUN_CREATED_BY
        if source.get('created_by') == GOAL_RUN_CREATED_BY:
            from lib.goal_runs.recovery import resume_chat_goal
            from lib.orchestration.execution_journal import RecoveryDeferred, RecoveryRefused
            try:
                return resume_chat_goal(
                    runs, source_run_id, owner_user_id=owner_user_id,
                    tenant_id=str(source.get('tenant_id') or ''),
                )
            except (RecoveryDeferred, RecoveryRefused) as error:
                # The Goal owner enforces Stop, lane-tail, receipt and claim
                # fences. Never fall back to a generic Studio executor.
                raise RunRecoveryError(str(error)) from error
        run_id = recovery_run_id(source_run_id)
        if runs.get(run_id) is not None:
            return run_id
        try:
            source, records = load_recovery_records(runs, source_run_id,
                                                    owner_user_id=owner_user_id)
        except RunRecoveryError:
            # Another request may have won creation while this request read
            # the immutable source and its newly appended launch claim.
            if runs.get(run_id) is not None:
                return run_id
            raise
        created = runs.create(
            run_id, definition=source['definition'],
            input_text=source.get('input', ''), orch_id=source.get('orch_id', ''),
            name=source.get('name', ''), created_by=source.get('created_by', ''),
        )
        if not created:
            if runs.get(run_id) is not None:
                return run_id
            raise RuntimeStartError('Failed to claim durable Flow recovery')
        try:
            record_recovery_claim(runs, source_run_id, run_id)
            actual_id = spawn_runtime_flow(
                self._runtime, source['definition'], owner_user_id=owner_user_id,
                task_id=run_id, meta={'name': source.get('name', ''), 'run_id': run_id},
                initial_context=source.get('input', ''),
                subflow_resolver_provider=self._subflow_resolver_provider(),
                durable_runs=runs, recovery_records=records,
                recovery_parent_id=source_run_id,
            )
            if actual_id != run_id:
                raise RuntimeError('runtime did not preserve recovery run id')
        except Exception as exc:
            recover_failed_durable_start(self._runtime, runs, run_id, exc)
            raise RuntimeStartError('Failed to start recovered Flow', run_id=run_id) from exc
        return run_id

    def start(
        self,
        kind: str,
        definition: dict,
        *,
        owner_user_id: int,
        input_text: str = '',
        orchestration_id: str = '',
        created_by: str = '',
    ) -> str:
        """Execute either start mode through one delivery-layer command."""
        if kind == 'ephemeral':
            return self._start_ephemeral(
                definition,
                owner_user_id=owner_user_id,
                input_text=input_text,
            )
        if kind == 'durable':
            return self._start_durable(
                definition,
                owner_user_id=owner_user_id,
                input_text=input_text,
                orchestration_id=orchestration_id,
                created_by=created_by,
            )
        raise RuntimeStartError(
            f'Unsupported orchestration runtime start kind: {kind!r}')

__all__ = [
    'RuntimeStartError',
    'OrchestrationRuntimeStartService',
]
