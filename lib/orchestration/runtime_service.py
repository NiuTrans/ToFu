"""Shared orchestration execution and runtime-projection service.

This module is the application boundary between the pure ``FlowExecutor``
and transport/runtime adapters. It composes focused outcome and event-sink
owners with lifecycle projection, terminal fences and TaskRuntime completion.
Definition authoring and repository CRUD live in their focused application
services; this module owns runtime execution only.
"""

from __future__ import annotations

from typing import Callable

from lib.orchestration.durable_projection import (
    DurableProjectionError,
    DurableRunProjection,
)
from lib.orchestration.runtime_event_sink import FlowEventSink
from lib.orchestration.runtime_outcome import (
    FlowRunOutcome,
    aborted_race_outcome as _aborted_race_outcome,
    failure_outcome as _failure_outcome,
)
from lib.orchestration.runtime_ports import (
    OrchestrationDurableRunPort,
    OrchestrationTaskRuntimePort,
)


def create_flow_executor(definition: dict, **kwargs):
    """Construct the shared interpreter; adapters supply only policy hooks."""
    from lib.orchestration_engine import FlowExecutor
    return FlowExecutor(definition, **kwargs)


def execute_flow(definition: dict, *, initial_context: str = '',
                 on_event: Callable[[dict], None] | None = None,
                 abort_check: Callable[[], bool] | None = None,
                 executor_options: dict | None = None) -> FlowRunOutcome:
    """Validate, construct and run one flow with normalized failure output."""
    options = dict(executor_options or {})
    options['on_event'] = on_event
    options['abort_check'] = abort_check
    try:
        executor = create_flow_executor(definition, **options)
        return FlowRunOutcome(executor.run(initial_context=initial_context),
                              executor)
    except Exception as exc:
        from lib.orchestration_engine import FlowExecutionError
        failure_kind = ('structural'
                        if isinstance(exc, FlowExecutionError)
                        else 'persistence'
                        if isinstance(exc, DurableProjectionError)
                        else 'exception')
        return _failure_outcome(exc, failure_kind)


def finish_runtime(
    runtime: OrchestrationTaskRuntimePort,
    task_id: str,
    outcome: FlowRunOutcome,
) -> None:
    """Project a normalized flow outcome onto TaskRuntime exactly once."""
    runtime.finish(
        task_id,
        result=outcome.result,
        error=outcome.error_envelope,
        error_context='orchestration:execution',
    )


def execute_runtime_flow(
    runtime: OrchestrationTaskRuntimePort,
    task_id: str,
    definition: dict,
    *,
    owner_user_id: int,
    initial_context: str = '',
    abort_check: Callable[[], bool] | None = None,
    subflow_resolver: Callable[[str], dict | None] | None = None,
    durable_runs: OrchestrationDurableRunPort | None = None,
    durable_run_id: str = '',
) -> FlowRunOutcome:
    """Execute and project one flow through the shared runtime pipeline.

    Transient and durable adapters differ only by whether ``durable_runs`` is
    supplied. Event filtering, lifecycle mapping, executor construction and
    TaskRuntime completion therefore cannot drift between the two surfaces.
    """
    durable = durable_runs is not None and bool(durable_run_id)
    projection = (DurableRunProjection(durable_runs, durable_run_id)
                  if durable else None)

    sink = FlowEventSink(
        lambda event: runtime.append_event(task_id, event),
        durable_project=projection.project_event if projection else None,
    )
    executor_options = {
        'human_gate_scope': task_id,
        'human_gate_owner_user_id': owner_user_id,
    }
    if subflow_resolver is not None:
        executor_options['subflow_resolver'] = subflow_resolver
    outcome = execute_flow(
        definition,
        initial_context=initial_context,
        on_event=sink,
        abort_check=abort_check,
        executor_options=executor_options,
    )
    if projection is not None:
        finalization = projection.finalize(
            outcome.lifecycle_status,
            final=(outcome.result.get('final') or ''),
            error=outcome.error_envelope,
        )
        if finalization.abort_won:
            outcome = _aborted_race_outcome(outcome)
        elif finalization.error is not None:
            outcome = _failure_outcome(
                finalization.error,
                'persistence',
                executor=outcome.executor,
            )
            projection.record_error(outcome.error_envelope)
    finish_runtime(runtime, task_id, outcome)
    return outcome


def spawn_runtime_flow(
    runtime: OrchestrationTaskRuntimePort,
    definition: dict,
    *,
    owner_user_id: int,
    task_id: str = '',
    meta: dict | None = None,
    initial_context: str = '',
    subflow_resolver_provider: Callable[
        [], Callable[[str], dict | None] | None
    ] | None = None,
    durable_runs: OrchestrationDurableRunPort | None = None,
) -> str:
    """Create and spawn one live or durable flow through one runtime seam.

    Durable persistence and ``TaskRuntime`` intentionally share the same ID.
    Keeping that invariant here prevents an HTTP adapter from wiring aborts,
    events or terminal projection to a different runtime task by accident.
    """
    if durable_runs is not None and not task_id:
        raise ValueError('durable orchestration flow requires a task_id')

    task = runtime.create(
        user_id=owner_user_id, task_id=task_id, meta=meta)
    runtime_task_id = str(task.get('id') or '')
    if not runtime_task_id:
        raise RuntimeError('orchestration runtime returned an empty task id')
    if task_id and runtime_task_id != task_id:
        raise RuntimeError(
            'orchestration runtime did not preserve the requested task id')

    def _worker():
        subflow_resolver = (
            subflow_resolver_provider()
            if subflow_resolver_provider is not None else None
        )
        execute_runtime_flow(
            runtime,
            runtime_task_id,
            definition,
            owner_user_id=owner_user_id,
            initial_context=initial_context,
            abort_check=task['abort_event'].is_set,
            subflow_resolver=subflow_resolver,
            durable_runs=durable_runs,
            durable_run_id=(runtime_task_id
                            if durable_runs is not None else ''),
        )

    runtime.spawn(runtime_task_id, _worker)
    return runtime_task_id


__all__ = [
    'create_flow_executor', 'execute_flow', 'finish_runtime',
    'execute_runtime_flow', 'spawn_runtime_flow',
]
