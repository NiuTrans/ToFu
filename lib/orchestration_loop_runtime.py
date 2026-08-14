"""Verifier-loop coordination boundary for orchestration graph runs.

The runtime owns one loop's iteration lifecycle and convergence guards while
the graph interpreter injects navigation-sensitive operations (body walking
and planner re-entry).  This keeps loop policy directly testable without
coupling it to leaf execution or the whole ``FlowExecutor``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from lib.log import get_logger
from lib.orchestration._role_axes import VERIFIER_ROLES
from lib.orchestration._runtime_params import resolve_node_runtime_param
from lib.orchestration.loop_policy import (
    MAX_REPLANS,
    MAX_ZERO_DELIVERABLE_TURNS,
    advance_zero_deliverable_streak,
    should_inject_zero_deliverable,
)
from lib.orchestration_graph import GraphNavigator
from lib.orchestration_feedback import OrchestrationFeedbackState
from lib.orchestration.outcome_ledger import OrchestrationOutcomeLedger
from lib.orchestration_progress import OrchestrationProgressLedger
from lib.orchestration_transcript import OrchestrationTranscript

logger = get_logger(__name__)

ZERO_DELIVERABLE_DIRECTIVE = (
    'STOP ANALYZING — START EXECUTING. Your last attempts produced ZERO '
    'state-changing actions (no file writes, edits, or commands). Your very '
    'next step MUST be a concrete state-changing tool call that advances the '
    'plan. Do not just read, search, or describe.'
)


class OrchestrationLoopAborted(Exception):
    """Signal translated to the graph interpreter's internal abort unwind."""


class OrchestrationLoopRuntime:
    """Coordinate one bounded producer/verifier loop."""

    def __init__(
        self,
        *,
        navigator: GraphNavigator,
        nodes: Mapping[str, dict],
        max_iterations: int,
        feedback: OrchestrationFeedbackState,
        progress: OrchestrationProgressLedger,
        outcomes: OrchestrationOutcomeLedger,
        transcript: OrchestrationTranscript,
        emit: Callable[[dict], None],
        abort_check: Callable[[], bool],
        walk: Callable[..., str],
        run_replan: Callable[[str, str, str | None, int], str],
        classify_verdict: Callable[..., tuple],
        progress_parser: Callable[[str], tuple],
        on_iteration_change: Callable[[int], None],
    ) -> None:
        self._navigator = navigator
        self._nodes = nodes
        self._max_iterations = max(1, int(max_iterations))
        self._feedback = feedback
        self._progress = progress
        self._outcomes = outcomes
        self._transcript = transcript
        self._emit = emit
        self._abort_check = abort_check
        self._walk = walk
        self._run_replan = run_replan
        self._classify_verdict = classify_verdict
        self._progress_parser = progress_parser
        self._on_iteration_change = on_iteration_change

    def run(self, loop_id: str, context: str) -> tuple[str, str | None]:
        """Run a loop body until convergence or a canonical bounded exit."""
        body_entry, exit_node = self._navigator.loop_parts(loop_id)
        planner_id = self._navigator.find_loop_planner(loop_id, body_entry)
        node = self._nodes[loop_id]
        configured_cap = int(resolve_node_runtime_param(
            node, 'max_iterations'))
        cap = max(1, min(self._max_iterations, configured_cap))
        self._emit({
            'type': 'loop_start',
            'node_id': loop_id,
            'max_iterations': cap,
            'planner': planner_id,
        })
        logger.info(
            '[FlowEngine] loop %s body=%s exit=%s planner=%s cap=%d',
            loop_id,
            body_entry,
            exit_node,
            planner_id,
            cap,
        )

        self._feedback.reset_loop()
        zero_streak = 0
        replans = 0
        exit_reason = 'max_iterations'
        completed_iterations = 0
        replan_exhausted = False
        for index in range(cap):
            if self._abort_check():
                raise OrchestrationLoopAborted()
            iteration = index + 1
            self._on_iteration_change(iteration)
            completed_iterations = iteration
            self._emit({
                'type': 'loop_iteration',
                'node_id': loop_id,
                'iteration': iteration,
                'max': cap,
            })
            self._progress.reset_iteration()
            context = self._walk(
                body_entry,
                context,
                stop_at=loop_id,
            )

            producer = self._progress.aggregate_iteration()
            zero_streak = advance_zero_deliverable_streak(
                zero_streak,
                reported=bool(producer and producer.get('reported')),
                state_changing=(producer or {}).get('sc_count', 0),
            )

            if should_inject_zero_deliverable(zero_streak) and iteration < cap:
                self._feedback.set_directive(ZERO_DELIVERABLE_DIRECTIVE)
                self._emit({
                    'type': 'zero_deliverable_guard',
                    'node_id': loop_id,
                    'iteration': iteration,
                    'streak': zero_streak,
                })
                logger.info(
                    '[FlowEngine] loop %s zero-deliverable guard fired '
                    '(streak=%d) — forcing CONTINUE with directive',
                    loop_id,
                    zero_streak,
                )
                zero_streak = 0
                continue

            verifier_output = self._transcript.last_verifier_output(
                VERIFIER_ROLES,
            )
            verifier_role = self._transcript.last_verifier_role(
                VERIFIER_ROLES,
            )
            self._feedback.append_verifier_feedback(verifier_output)
            phase, defect = self._classify_verdict(
                verifier_output,
                verifier_role=verifier_role,
            )

            if verifier_role == 'virtual_user':
                self._feedback.record_virtual_user_progress(
                    verifier_output,
                    self._progress.aggregate_iteration(),
                    progress_parser=self._progress_parser,
                )

            if phase == 'stop':
                logger.info(
                    '[FlowEngine] loop %s STOP after iteration %d',
                    loop_id,
                    iteration,
                )
                exit_reason = 'stop'
                break

            if (
                self._feedback.detects_stuck(verifier_role=verifier_role)
                and iteration < cap
            ):
                self._emit({
                    'type': 'stuck_detected',
                    'node_id': loop_id,
                    'iteration': iteration,
                })
                logger.info(
                    '[FlowEngine] loop %s STUCK (repeating feedback) — '
                    'breaking after iteration %d',
                    loop_id,
                    iteration,
                )
                exit_reason = 'stuck'
                break

            if verifier_role == 'virtual_user' and iteration < cap:
                progress_window = self._feedback.no_progress_window()
                if progress_window:
                    self._emit({
                        'type': 'no_progress',
                        'node_id': loop_id,
                        'iteration': iteration,
                        'window': progress_window,
                    })
                    logger.info(
                        '[FlowEngine] loop %s NO-PROGRESS (churn without net '
                        'resolved items over %d turns) — breaking after '
                        'iteration %d',
                        loop_id,
                        progress_window,
                        iteration,
                    )
                    exit_reason = 'no_progress'
                    break

            if (
                phase == 'planner'
                and planner_id
                and replans < MAX_REPLANS
                and iteration < cap
            ):
                replans += 1
                self._emit({
                    'type': 'replan',
                    'node_id': loop_id,
                    'planner': planner_id,
                    'replan': replans,
                    'defect': (defect or '')[:200],
                })
                logger.info(
                    '[FlowEngine] loop %s REPLAN #%d (defect=%r) → '
                    're-running planner %s',
                    loop_id,
                    replans,
                    defect,
                    planner_id,
                )
                context = self._run_replan(
                    planner_id,
                    context,
                    defect,
                    replans,
                )
                continue

            if phase == 'planner' and replans >= MAX_REPLANS:
                replan_exhausted = True
        else:
            if replan_exhausted:
                exit_reason = 'replan_exhausted'
            logger.info(
                '[FlowEngine] loop %s hit cap %d (no STOP verdict, reason=%s)',
                loop_id,
                cap,
                exit_reason,
            )

        self._outcomes.record_loop_exit(
            node_id=loop_id,
            reason=exit_reason,
            iterations=completed_iterations,
        )
        self._on_iteration_change(0)
        return context, exit_node


__all__ = [
    'MAX_REPLANS',
    'MAX_ZERO_DELIVERABLE_TURNS',
    'OrchestrationLoopAborted',
    'OrchestrationLoopRuntime',
    'ZERO_DELIVERABLE_DIRECTIVE',
]
