"""Leaf role execution boundary for orchestration graph runs.

This module owns the complete lifecycle of one agent-backed role node: budget
claim, effective input projection, runner normalization, execution accounting,
trace/transcript publication and output-context projection.  It deliberately
has no graph navigation, loop verdict or scheduling dependency.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from lib.log import get_logger
from lib.orchestration._execution_projection import render_role_brief
from lib.orchestration.events import event_preview
from lib.orchestration._role_axes import VERIFIER_ROLES
from lib.orchestration._runtime_params import resolve_node_runtime_param
from lib.orchestration_budget import OrchestrationAgentBudget
from lib.orchestration_dataflow import OrchestrationDataflow
from lib.orchestration_feedback import OrchestrationFeedbackState
from lib.orchestration_graph import FlowExecutionError
from lib.orchestration.outcome_ledger import OrchestrationOutcomeLedger
from lib.orchestration_progress import OrchestrationProgressLedger
from lib.orchestration_runner_result import (
    OrchestrationAgentResult,
    OrchestrationAgentRunnerPort,
    normalize_orchestration_agent_result,
)
from lib.orchestration_trace import (
    OrchestrationTraceRecorder,
    trace_activity_snapshot,
)
from lib.orchestration_transcript import (
    OrchestrationTranscript,
    append_role_context,
)

logger = get_logger(__name__)


class OrchestrationRoleRuntime:
    """Execute one role node through the shared orchestration state ports."""

    def __init__(
        self,
        *,
        budget: OrchestrationAgentBudget,
        runner: OrchestrationAgentRunnerPort,
        dataflow: OrchestrationDataflow,
        feedback: OrchestrationFeedbackState,
        progress: OrchestrationProgressLedger,
        outcomes: OrchestrationOutcomeLedger,
        trace_recorder: OrchestrationTraceRecorder,
        transcript: OrchestrationTranscript,
        emit: Callable[[dict], None],
        on_agent_claimed: Callable[[], None],
    ) -> None:
        self._budget = budget
        self._runner = runner
        self._dataflow = dataflow
        self._feedback = feedback
        self._progress = progress
        self._outcomes = outcomes
        self._trace_recorder = trace_recorder
        self._transcript = transcript
        self._emit = emit
        self._on_agent_claimed = on_agent_claimed

    def run(self, node: dict, context: str, *, iteration: int) -> str:
        """Run one role and return the accumulated context projection."""
        output = self.run_output(node, context, iteration=iteration)
        return append_role_context(context, node.get('role', 'general'), output)

    def run_output(self, node: dict, context: str, *, iteration: int) -> str:
        """Run one role and return only its output across a control membrane."""
        if not self._budget.claim():
            raise FlowExecutionError(
                f'agent budget exhausted ({self._budget.limit})')
        self._on_agent_claimed()

        node_id = node.get('id')
        role = node.get('role', 'general')
        shared = (
            resolve_node_runtime_param(node, 'isolation') == 'shared-context'
        )
        verifier = role in VERIFIER_ROLES
        emits = resolve_node_runtime_param(node, 'emits')

        effective_context = context
        typed_input = self._dataflow.compose_inputs(node)
        if typed_input is not None:
            effective_context = typed_input
        if shared:
            effective_context = self._feedback.compose_shared_context(
                node_id,
                effective_context if typed_input is not None else context,
            )
        if verifier:
            effective_context = self._progress.append_deliverables_snapshot(
                effective_context,
                in_loop=iteration > 0,
            )

        isolation = 'shared' if shared else 'fresh'
        self._emit({
            'type': 'step_start',
            'node_id': node_id,
            'role': role,
            'name': node.get('name') or role,
            'emits': emits,
            'isolation': isolation,
        })
        started = time.monotonic()
        try:
            raw_result = self._runner(node, effective_context, 0)
        except Exception as exc:
            logger.error(
                '[FlowEngine] agent runner crashed on %s: %s',
                node_id,
                exc,
                exc_info=True,
            )
            raw_result = OrchestrationAgentResult(
                status='failed',
                error=str(exc),
            )
        result = normalize_orchestration_agent_result(raw_result)
        usage = result.tool_usage
        state_changing, exploratory, names, reported = usage.engine_tuple()
        activity = trace_activity_snapshot(
            state_changing=state_changing,
            exploratory=exploratory,
            state_changing_tools=names,
        )
        elapsed = time.monotonic() - started

        self._transcript.record(
            node_id,
            role,
            result.output,
            result.status,
            result.error,
            elapsed,
            state_changing=state_changing,
            exploratory=exploratory,
        )
        if result.status == 'failed':
            self._outcomes.record_node_failure(
                node_id=node_id,
                role=role,
                error=result.error or 'failed',
            )
        self._trace_recorder.capture(
            node,
            iteration=iteration,
            brief=render_role_brief(node),
            input_context=effective_context,
            output=result.output,
            status=result.status,
            error=result.error,
            elapsed=elapsed,
            emits=emits,
            isolation=isolation,
            state_changing=state_changing,
            exploratory=exploratory,
            state_changing_tools=names,
            thinking=result.thinking,
        )
        self._dataflow.publish_outputs(
            node,
            result.output,
            names,
            exploratory,
        )
        self._feedback.complete_role(
            node_id,
            result.output,
            shared=shared,
            verifier=verifier,
        )
        if not verifier:
            self._progress.record_producer({
                'node_id': node_id,
                'role': role,
                'sc_count': state_changing,
                'explore_count': exploratory,
                'names': names,
                'reported': reported,
            })

        self._emit({
            'type': 'step_complete',
            'node_id': node_id,
            'role': role,
            'status': result.status,
            'preview': event_preview(result.output),
            'output': result.output,
            'thinking': result.thinking,
            'emits': emits,
            **activity,
        })
        return result.output


__all__ = ['OrchestrationRoleRuntime']
