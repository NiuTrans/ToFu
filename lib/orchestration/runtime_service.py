"""Shared orchestration execution and runtime-projection service.

Composes the interpreter, event sinks, journal, terminal fences and TaskRuntime.
Definition authoring and repository CRUD stay in their application services.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)
from typing import Callable

from lib.orchestration.durable_projection import DurableRunProjection
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
    from lib.orchestration.engine import FlowExecutor
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
        from lib.orchestration.graph import FlowExecutionError, FlowEventDeliveryError
        failure_kind = ('structural'
                        if isinstance(exc, FlowExecutionError)
                        else 'persistence'
                        if isinstance(exc, FlowEventDeliveryError)
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
    recovery_records: dict | None = None,
    recovery_parent_id: str = '',
    continuation: bool = False,
    sequence_offset: int = 0,
    resolved_request: dict | None = None,
) -> FlowRunOutcome:
    """Execute either surface through one event, lifecycle and completion pipeline."""
    durable = durable_runs is not None and bool(durable_run_id)
    projection = (DurableRunProjection(durable_runs, durable_run_id)
                  if durable else None)

    sink = FlowEventSink(
        lambda event: runtime.append_event(task_id, event),
        durable_project=(lambda seq, event, status: projection.project_event(
            seq + sequence_offset, event, status)) if projection else None,
    )
    executor_options = {
        # TaskRuntime owns the live lifecycle; leaf invocations need an explicit
        # owner even when there is no conversation-backed parent task.
        'parent_task': {'id': task_id, '_userId': owner_user_id,
                        '_transientRuntime': True, 'config': {}},
        'human_gate_scope': task_id,
        'human_gate_owner_user_id': owner_user_id,
    }
    journal = None
    if durable:
        from lib.orchestration.execution_journal import ExecutionJournal
        journal = ExecutionJournal(sink, records=recovery_records, suspend_run_id=durable_run_id)
        try:
            if resolved_request is not None:
                sink(resolved_request)
            journal.begin(definition, initial_context, parent_run_id=recovery_parent_id, continuation=continuation)
        except Exception as exc:
            outcome = _failure_outcome(exc, 'persistence')
            projection.record_error(outcome.error_envelope)
            finish_runtime(runtime, task_id, outcome)
            return outcome
        executor_options['execution_journal'] = journal
    if subflow_resolver is not None:
        executor_options['subflow_resolver'] = subflow_resolver
    from lib.orchestration.continuation import FlowSuspended, suspend
    try:
        outcome = execute_flow(
            definition,
            initial_context=initial_context,
            on_event=sink,
            abort_check=abort_check,
            executor_options=executor_options,
        )
    except FlowSuspended:
        try:
            if abort_check is not None and abort_check():
                raise RuntimeError('Flow cancelled before suspension')
            suspend(durable_runs, durable_run_id, journal, runtime, task_id)
            return FlowRunOutcome({'suspended': True, 'status': 'paused', 'ok': False})
        except Exception as exc:
            logger.debug('execute runtime flow failed: %s', exc)
            outcome = _failure_outcome(exc, 'persistence')
    if journal is not None and outcome.result.get('ok'):
        try:
            journal.verify_consumed()
        except Exception as exc:
            logger.debug('execute runtime flow failed: %s', exc)
            outcome = _failure_outcome(exc, 'persistence', executor=outcome.executor)
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
    recovery_records: dict | None = None,
    recovery_parent_id: str = '',
    durable_run_id: str = '',
    continuation: bool = False,
    sequence_offset: int = 0,
    resolved_request: dict | None = None,
) -> str:
    """Spawn a generation; resumed generations retain the original durable ID."""
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
        if continuation:
            current = durable_runs.get_execution_snapshot(durable_run_id)
            if current is None or current.get('status') in ('aborted', 'done', 'error'):
                runtime.finish(runtime_task_id, result={'aborted': True})
                return
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
            durable_run_id=((durable_run_id or runtime_task_id)
                            if durable_runs is not None else ''),
            continuation=continuation, sequence_offset=sequence_offset,
            resolved_request=resolved_request,
            recovery_records=recovery_records,
            recovery_parent_id=recovery_parent_id,
        )

    runtime.spawn(runtime_task_id, _worker)
    return runtime_task_id


__all__ = [
    'create_flow_executor', 'execute_flow', 'finish_runtime',
    'execute_runtime_flow', 'spawn_runtime_flow',
]
