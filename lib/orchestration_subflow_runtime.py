"""Isolated-subflow lifecycle boundary for orchestration graph runs.

The graph interpreter supplies a child-executor factory; this module owns the
black-box membrane around that child without importing ``FlowExecutor``.  It
resolves referenced definitions, enforces recursive/budget bounds, projects
the child deliverable, and publishes the parent-visible transcript, trace,
dataflow and terminal facts.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Protocol

from lib.log import get_logger
from lib.orchestration._execution_projection import render_role_brief
from lib.orchestration.events import event_preview
from lib.orchestration._role_axes import VERIFIER_ROLES
from lib.orchestration._runtime_params import resolve_node_runtime_param
from lib.orchestration._subflow_contract import MAX_SUBFLOW_DEPTH
from lib.orchestration_budget import OrchestrationAgentBudget
from lib.orchestration_dataflow import OrchestrationDataflow
from lib.orchestration_graph import FlowExecutionError
from lib.orchestration.outcome_ledger import OrchestrationOutcomeLedger
from lib.orchestration.outcome_domain import outcome_from_result
from lib.orchestration_trace import (
    OrchestrationTraceRecorder,
    trace_activity_snapshot,
)
from lib.orchestration_transcript import (
    OrchestrationTranscript,
    append_role_context,
    subflow_deliverable,
)

logger = get_logger(__name__)


class OrchestrationChildExecutorPort(Protocol):
    """Minimal nested-executor surface consumed by the subflow runtime."""

    @property
    def agents_run(self) -> int: ...

    def run(self, *, initial_context: str = '') -> dict: ...


class OrchestrationSubflowAborted(Exception):
    """Signal translated to the graph interpreter's internal abort unwind."""


class OrchestrationSubflowRuntime:
    """Execute one isolated subflow through an explicit child factory port."""

    def __init__(
        self,
        *,
        budget: OrchestrationAgentBudget,
        depth: int,
        resolver: Callable[[str], dict] | None,
        child_executor_factory: Callable[
            [dict], OrchestrationChildExecutorPort
        ],
        dataflow: OrchestrationDataflow,
        outcomes: OrchestrationOutcomeLedger,
        trace_recorder: OrchestrationTraceRecorder,
        transcript: OrchestrationTranscript,
        emit: Callable[[dict], None],
        on_child_agents: Callable[[int], None],
    ) -> None:
        self._budget = budget
        self._depth = max(0, int(depth))
        self._resolver = resolver
        self._child_executor_factory = child_executor_factory
        self._dataflow = dataflow
        self._outcomes = outcomes
        self._trace_recorder = trace_recorder
        self._transcript = transcript
        self._emit = emit
        self._on_child_agents = on_child_agents

    def run(self, node: dict, context: str, *, iteration: int) -> str:
        """Run a nested graph and return only its producer deliverable."""
        if self._budget.remaining() <= 0:
            raise FlowExecutionError(
                f'agent budget exhausted ({self._budget.limit})')

        node_id = node.get('id')
        role = node.get('role') or 'general'
        emits = resolve_node_runtime_param(node, 'emits')
        child_definition = self._resolve_definition(node)

        self._emit({
            'type': 'step_start',
            'node_id': node_id,
            'role': role,
            'name': node.get('name') or role,
            'emits': emits,
            'isolation': 'isolated',
            'subflow': True,
        })
        started = time.monotonic()
        logger.info(
            '[FlowEngine] isolated subflow %s START role=%s depth=%d',
            node_id,
            role,
            self._depth + 1,
        )

        child_executor = self._child_executor_factory(child_definition)
        try:
            raw_result = child_executor.run(initial_context=context)
        except Exception as exc:
            logger.error(
                '[FlowEngine] isolated subflow %s crashed: %s',
                node_id,
                exc,
                exc_info=True,
            )
            raw_result = {
                'ok': False,
                'status': 'failed',
                'final': '',
                'error': str(exc),
            }
        self._on_child_agents(child_executor.agents_run)

        if isinstance(raw_result, Mapping):
            result = dict(raw_result)
        else:
            result = {
                'ok': False,
                'status': 'failed',
                'final': '',
                'error': 'invalid child executor result: expected a mapping, '
                         f'got {type(raw_result).__name__}',
            }
        output = subflow_deliverable(
            result,
            verifier_roles=VERIFIER_ROLES,
        )
        status = str(result.get('status') or 'completed')
        error = str(result.get('error') or '')
        if status == 'aborted':
            raise OrchestrationSubflowAborted()

        child_outcome = outcome_from_result(result)
        if child_outcome.category == 'incomplete':
            self._outcomes.record_loop_exit(
                node_id=node_id,
                reason=child_outcome.stop_reason,
                iterations=0,
            )
        elif child_outcome.category == 'failure' and status != 'failed':
            self._outcomes.record_node_failure(
                node_id=node_id,
                role=role,
                error=child_outcome.runtime_error,
            )

        elapsed = time.monotonic() - started
        self._transcript.record(
            node_id,
            role,
            output,
            status,
            error,
            elapsed,
        )
        self._trace_recorder.capture(
            node,
            iteration=iteration,
            brief=render_role_brief(node),
            input_context=context,
            output=output,
            status=status,
            error=error,
            elapsed=elapsed,
            emits=emits,
            isolation='isolated',
            subflow=True,
        )
        self._dataflow.publish_outputs(node, output, [], 0)
        self._emit({
            'type': 'step_complete',
            'node_id': node_id,
            'role': role,
            'status': status,
            'preview': event_preview(output),
            'output': output,
            'emits': emits,
            'subflow': True,
            **trace_activity_snapshot(),
        })
        logger.info(
            '[FlowEngine] isolated subflow %s DONE status=%s',
            node_id,
            status,
        )
        if status == 'failed':
            raise FlowExecutionError(
                f'isolated subflow {node_id!r} failed: '
                f'{error or "no detail"}')
        return append_role_context(context, role, output)

    def _resolve_definition(self, node: dict) -> dict:
        node_id = node.get('id')
        if self._depth + 1 > MAX_SUBFLOW_DEPTH:
            raise FlowExecutionError(
                f'isolated subflow {node_id!r} nesting exceeds '
                f'MAX_SUBFLOW_DEPTH ({MAX_SUBFLOW_DEPTH})')

        params = node.get('params') or {}
        child = params.get('definition')
        if child is not None:
            return child
        reference = params.get('ref')
        if not (self._resolver and reference):
            raise FlowExecutionError(
                f'isolated subflow {node_id!r} has ref {reference!r} but no '
                'resolver was supplied')
        child = self._resolver(reference)
        if not isinstance(child, dict):
            raise FlowExecutionError(
                f'isolated subflow {node_id!r} ref {reference!r} did not '
                'resolve to a definition')
        return child


__all__ = [
    'OrchestrationChildExecutorPort',
    'OrchestrationSubflowAborted',
    'OrchestrationSubflowRuntime',
]
