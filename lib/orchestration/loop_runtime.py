"""Verifier-loop coordination boundary for orchestration graph runs.

The runtime owns one loop's iteration lifecycle and convergence guards while
the graph interpreter injects navigation-sensitive operations (body walking
and planner re-entry).  This keeps loop policy directly testable without
coupling it to leaf execution or the whole ``FlowExecutor``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from lib.agent_verdict import strip_machine_tokens as _strip_machine_tokens
from lib.log import get_logger
from lib.orchestration._role_axes import VERIFIER_ROLES
from lib.orchestration._runtime_params import resolve_node_runtime_param
from lib.orchestration.loop_policy import (
    BUDGET_POLICY_BOUNDED,
    BUDGET_POLICY_CHECKPOINT_EXTEND,
    LOOP_BUDGET_POLICIES,
    MAX_REPLANS,
    MAX_ZERO_DELIVERABLE_TURNS,
    advance_zero_deliverable_streak,
    should_inject_zero_deliverable,
)
from lib.orchestration.graph import GraphNavigator
from lib.orchestration.feedback import OrchestrationFeedbackState
from lib.orchestration.outcome_ledger import OrchestrationOutcomeLedger
from lib.orchestration.progress import OrchestrationProgressLedger
from lib.orchestration.transcript import OrchestrationTranscript

logger = get_logger(__name__)

ZERO_DELIVERABLE_DIRECTIVE = (
    'Your last attempts produced no durable deliverable. Reassess the next '
    'step: if the exploration already answered the important questions, move '
    'to a concrete deliverable now. If more read-only investigation is '
    'genuinely required, state the specific unanswered question and inspect '
    'new evidence that can answer it. Do not mutate state merely to satisfy '
    'this guard.'
)

REPEATED_FEEDBACK_DIRECTIVE = (
    'The reviewer feedback is worded similarly to an earlier turn. This is '
    'only a strategy-change hint, not proof that the work is stuck. Compare '
    'the feedback with the latest attempt: verify whether the issue still '
    'exists, then either address the concrete remaining gap with a different '
    'approach or show the reviewer the evidence that it is resolved.'
)

DIMINISHING_RETURNS_DIRECTIVE = (
    'Recent turns edited overlapping targets without increasing the '
    'reviewer-reported count of resolved criteria. This can still be valid '
    'incremental work, so it is not a stop condition. Reassess the approach: '
    'verify whether the edits are converging, choose a different strategy if '
    'they are not, and keep the structured progress line accurate.'
)

GOAL_COMPLETION_EVIDENCE_DIRECTIVE = (
    'Do not declare the objective complete without the required machine '
    'evidence line. Verify the acceptance criteria, then end the reply with '
    '[PROGRESS: resolved=X remaining=Y]. TASK_DONE is accepted only when '
    'remaining=0.'
)

GOAL_REMAINING_DIRECTIVE = (
    'You declared the objective complete, but your own progress line says '
    'remaining={remaining}. Completion requires remaining=0: drive the '
    'remaining acceptance criteria to done (or explicitly justify why each '
    'is out of scope), then re-issue your verdict with an updated '
    '[PROGRESS: resolved=X remaining=0] line.'
)

GOAL_STOP_VERIFY_CHALLENGE_DIRECTIVE = (
    'You declared the objective complete without verifying anything '
    'yourself this run — the assistant changed real state, and an owner '
    'sign-off requires independent evidence, not the assistant\'s '
    'self-report. Before declaring completion again, use your tools to '
    'check the most consequential claims (read the changed files, run or '
    'inspect the tests/build), then re-issue your verdict with the '
    '[PROGRESS: resolved=X remaining=Y] line. If verification exposes a '
    'gap, give the assistant the gap instead of declaring completion.'
)

GOAL_STOP_VACUOUS_CHALLENGE_DIRECTIVE = (
    'You declared the objective complete with resolved=0 and zero '
    'state-changing work anywhere in this run — an empty close-out. Either '
    'the objective genuinely needs no tool work (a subjective/advisory '
    'question already answered): then restate completion with a one-line '
    'justification of WHY no work was needed; or work remains: then name '
    'the concrete next step. End with the [PROGRESS: resolved=X '
    'remaining=Y] line.'
)

GOAL_BUDGET_CHECKPOINT_DIRECTIVE = (
    'The iteration budget window closed and was extended automatically '
    'because the work is still advancing — budget is never a reason to '
    'stop, rush, or shrink the objective. Continue with the same standard: '
    'the full original scope, verified. If genuinely every remaining item '
    'is blocked on the human or an unreachable external system, state that '
    'blocking condition and its evidence explicitly in your reply so the '
    'reviewer can judge it.'
)

GOAL_BLOCKED_REMAINING_DIRECTIVE = (
    'You declared the work blocked, but your own progress line says '
    'remaining=0 — with nothing remaining that is a completion claim, not '
    'a blockage. Either verify the acceptance criteria and issue '
    '[VU: TASK_DONE], or name the concrete remaining blocker and re-issue '
    'with an accurate [PROGRESS: resolved=X remaining=Y] line.'
)

GOAL_BLOCKED_JUSTIFICATION_DIRECTIVE = (
    'Your blocked claim carried no concrete justification. A blocked '
    'verdict requires the specific blocking condition, the evidence that '
    'it is genuine and has repeated across consecutive turns, and exactly '
    'what is needed to unblock. Re-issue with that evidence, or continue '
    'the work.'
)

GOAL_BLOCKED_CHALLENGE_DIRECTIVE = (
    'Your blocked claim was challenged. Declare [VERDICT: BLOCKED] again '
    'only if the same genuine blocker persisted across consecutive turns '
    'and no safe action remains: restate the blocking condition, why no '
    'agent-reachable step can resolve it, and what the human must provide. '
    'Otherwise continue the work toward the objective.'
)

# A blocked verdict must carry more prose than the control tokens themselves
# to be evaluated — below this the claim is vacuous.
GOAL_BLOCKED_MIN_JUSTIFICATION_CHARS = 80


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
        budget_policy: str = BUDGET_POLICY_BOUNDED,
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
        if budget_policy not in LOOP_BUDGET_POLICIES:
            raise ValueError(
                f'unsupported loop budget policy: {budget_policy!r}')
        self._budget_policy = budget_policy

    def run(self, loop_id: str, context: str) -> tuple[str, str | None]:
        """Run a loop body until convergence or a canonical bounded exit."""
        body_entry, exit_node = self._navigator.loop_parts(loop_id)
        planner_id = self._navigator.find_loop_planner(loop_id, body_entry)
        node = self._nodes[loop_id]
        configured_cap = int(resolve_node_runtime_param(
            node, 'max_iterations'))
        # Under ``checkpoint_extend`` this is the WINDOW granted per
        # checkpoint, not a hard total: a closing window that produced
        # durable state changes or verified progress is extended by one
        # more window, so budget alone never ends a goal run.
        window = max(1, min(self._max_iterations, configured_cap))
        cap = window
        self._emit({
            'type': 'loop_start',
            'node_id': loop_id,
            'max_iterations': cap,
            'planner': planner_id,
        })
        logger.info(
            '[FlowEngine] loop %s body=%s exit=%s planner=%s cap=%d policy=%s',
            loop_id,
            body_entry,
            exit_node,
            planner_id,
            cap,
            self._budget_policy,
        )

        self._feedback.reset_loop()
        zero_streak = 0
        replans = 0
        exit_reason = 'max_iterations'
        completed_iterations = 0
        replan_exhausted = False
        producer_sc_total = 0
        stop_challenged = False
        blocked_challenged = False
        strategy_nudged = False
        checkpoints = 0
        checkpoint_sc_mark = 0
        checkpoint_resolved_mark: int | None = None
        iteration = 0
        while iteration < cap:
            if self._abort_check():
                raise OrchestrationLoopAborted()
            iteration += 1
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
            producer_sc_total += int((producer or {}).get('sc_count') or 0)
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
                progress_entry = self._feedback.record_virtual_user_progress(
                    verifier_output,
                    producer,
                    progress_parser=self._progress_parser,
                )
                _resolved, remaining = self._progress_parser(verifier_output)
                if phase == 'stop':
                    vu_tool_rounds = self._feedback.verifier_tool_rounds()
                    cum_resolved = progress_entry.get('cum_resolved')
                    if remaining is None:
                        # GoalRun policy requires concrete completion evidence.
                        # The compatibility classifier intentionally fails open
                        # for old standalone carriers; the sole new Goal Mode
                        # owner fails closed here at its Flow lifecycle boundary.
                        phase = 'worker'
                        self._feedback.set_directive(
                            GOAL_COMPLETION_EVIDENCE_DIRECTIVE)
                        self._emit({
                            'type': 'goal_completion_evidence_missing',
                            'node_id': loop_id,
                            'iteration': iteration,
                        })
                        logger.warning(
                            '[FlowEngine] loop %s refused VU completion without '
                            'a parseable remaining=0 progress receipt',
                            loop_id,
                        )
                    elif remaining > 0:
                        # Ledger reconciliation: the VU's own progress line
                        # contradicts the completion claim.
                        phase = 'worker'
                        self._feedback.set_directive(
                            GOAL_REMAINING_DIRECTIVE.format(
                                remaining=remaining))
                        self._emit({
                            'type': 'goal_stop_rejected',
                            'node_id': loop_id,
                            'iteration': iteration,
                            'reason': 'remaining',
                            'remaining': remaining,
                        })
                        logger.warning(
                            '[FlowEngine] loop %s refused VU completion: '
                            'self-reported remaining=%d',
                            loop_id,
                            remaining,
                        )
                    elif (
                        not stop_challenged
                        and producer_sc_total > 0
                        and vu_tool_rounds == 0
                    ):
                        # The producer changed real state but the VU never
                        # verified anything with its own tools — challenge
                        # the first such stop instead of accepting it.
                        stop_challenged = True
                        phase = 'worker'
                        self._feedback.set_directive(
                            GOAL_STOP_VERIFY_CHALLENGE_DIRECTIVE)
                        self._emit({
                            'type': 'goal_stop_rejected',
                            'node_id': loop_id,
                            'iteration': iteration,
                            'reason': 'unverified',
                        })
                        logger.warning(
                            '[FlowEngine] loop %s challenged VU completion: '
                            'producer made %d state-changing call(s) but '
                            'the VU used 0 tool rounds — forcing verification',
                            loop_id,
                            producer_sc_total,
                        )
                    elif (
                        not stop_challenged
                        and not cum_resolved
                        and producer_sc_total == 0
                        and vu_tool_rounds == 0
                    ):
                        # Vacuous close-out: nothing resolved, nothing built,
                        # nothing checked. Challenge once; a genuinely
                        # no-work objective passes on the justified re-issue.
                        stop_challenged = True
                        phase = 'worker'
                        self._feedback.set_directive(
                            GOAL_STOP_VACUOUS_CHALLENGE_DIRECTIVE)
                        self._emit({
                            'type': 'goal_stop_rejected',
                            'node_id': loop_id,
                            'iteration': iteration,
                            'reason': 'vacuous',
                        })
                        logger.warning(
                            '[FlowEngine] loop %s challenged vacuous VU '
                            'completion (resolved=0, no state changes, no '
                            'verification)',
                            loop_id,
                        )
                    elif producer_sc_total > 0 and vu_tool_rounds == 0:
                        logger.warning(
                            '[FlowEngine] loop %s accepting VU completion '
                            'without VU tool verification — challenge was '
                            'already issued once this loop',
                            loop_id,
                        )
                elif phase == 'blocked':
                    # VU-declared impasse ([VERDICT: BLOCKED]).  Acceptance
                    # is gated like completion: the claim needs its machine
                    # receipt, must not contradict itself (remaining=0 is a
                    # completion claim), must carry a concrete justification,
                    # and is challenged once so only a persistent, evidenced
                    # impasse ends the run as blocked.
                    justification = _strip_machine_tokens(
                        verifier_output or '').strip()
                    if remaining is None:
                        phase = 'worker'
                        self._feedback.set_directive(
                            GOAL_COMPLETION_EVIDENCE_DIRECTIVE)
                        self._emit({
                            'type': 'goal_blocked_rejected',
                            'node_id': loop_id,
                            'iteration': iteration,
                            'reason': 'evidence_missing',
                        })
                        logger.warning(
                            '[FlowEngine] loop %s refused VU blocked without '
                            'a parseable progress receipt',
                            loop_id,
                        )
                    elif remaining <= 0:
                        phase = 'worker'
                        self._feedback.set_directive(
                            GOAL_BLOCKED_REMAINING_DIRECTIVE)
                        self._emit({
                            'type': 'goal_blocked_rejected',
                            'node_id': loop_id,
                            'iteration': iteration,
                            'reason': 'remaining_zero',
                        })
                        logger.warning(
                            '[FlowEngine] loop %s refused VU blocked: '
                            'remaining=0 is a completion claim',
                            loop_id,
                        )
                    elif (
                        len(justification)
                        < GOAL_BLOCKED_MIN_JUSTIFICATION_CHARS
                    ):
                        phase = 'worker'
                        self._feedback.set_directive(
                            GOAL_BLOCKED_JUSTIFICATION_DIRECTIVE)
                        self._emit({
                            'type': 'goal_blocked_rejected',
                            'node_id': loop_id,
                            'iteration': iteration,
                            'reason': 'vacuous',
                        })
                        logger.warning(
                            '[FlowEngine] loop %s refused vacuous VU blocked '
                            '(%d justification chars)',
                            loop_id,
                            len(justification),
                        )
                    elif not blocked_challenged:
                        blocked_challenged = True
                        phase = 'worker'
                        self._feedback.set_directive(
                            GOAL_BLOCKED_CHALLENGE_DIRECTIVE)
                        self._emit({
                            'type': 'goal_blocked_rejected',
                            'node_id': loop_id,
                            'iteration': iteration,
                            'reason': 'challenged',
                        })
                        logger.warning(
                            '[FlowEngine] loop %s challenged first VU blocked '
                            'claim — re-issue requires repeated-impasse '
                            'evidence',
                            loop_id,
                        )
                    else:
                        logger.info(
                            '[FlowEngine] loop %s accepting VU blocked after '
                            'iteration %d (repeated impasse, challenged once)',
                            loop_id,
                            iteration,
                        )
                        exit_reason = 'vu_blocked'

            if phase == 'stop':
                logger.info(
                    '[FlowEngine] loop %s STOP after iteration %d',
                    loop_id,
                    iteration,
                )
                exit_reason = 'stop'
                break

            if exit_reason == 'vu_blocked':
                break

            if (
                self._feedback.detects_stuck(verifier_role=verifier_role)
                and not strategy_nudged
                and iteration < cap
            ):
                # Similar reviewer prose is useful as a weak strategy signal,
                # but it is not evidence that the worker or world state failed
                # to advance.  The old branch terminated here, which could
                # kill a valid retry simply because a verifier restated the
                # same acceptance criterion.  Nudge once and keep the finite
                # iteration cap as the safety boundary.
                strategy_nudged = True
                self._feedback.set_directive(REPEATED_FEEDBACK_DIRECTIVE)
                self._emit({
                    'type': 'stuck_detected',
                    'node_id': loop_id,
                    'iteration': iteration,
                    'action': 'strategy_nudge',
                })
                logger.info(
                    '[FlowEngine] loop %s repeating verifier feedback — '
                    'injecting one strategy nudge after iteration %d',
                    loop_id,
                    iteration,
                )

            if (
                verifier_role == 'virtual_user'
                and not strategy_nudged
                and iteration < cap
            ):
                progress_window = self._feedback.no_progress_window()
                if progress_window:
                    strategy_nudged = True
                    self._feedback.set_directive(
                        DIMINISHING_RETURNS_DIRECTIVE)
                    self._emit({
                        'type': 'no_progress',
                        'node_id': loop_id,
                        'iteration': iteration,
                        'window': progress_window,
                        'action': 'strategy_nudge',
                    })
                    logger.info(
                        '[FlowEngine] loop %s possible diminishing returns '
                        'over %d turns — injecting one strategy nudge after '
                        'iteration %d',
                        loop_id,
                        progress_window,
                        iteration,
                    )

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

            if (
                self._budget_policy == BUDGET_POLICY_CHECKPOINT_EXTEND
                and iteration == cap
            ):
                # Budget checkpoint: closing a window never ends a goal run
                # by itself.  A window that produced durable state changes
                # or verified progress earns one more window; only a window
                # with ZERO of both is the evidence-based stall breaker.
                sc_delta = producer_sc_total - checkpoint_sc_mark
                resolved_now = self._latest_cum_resolved()
                resolved_delta = (
                    None if resolved_now is None
                    else resolved_now - (checkpoint_resolved_mark or 0)
                )
                if sc_delta > 0 or (resolved_delta or 0) > 0:
                    checkpoints += 1
                    checkpoint_sc_mark = producer_sc_total
                    # Missing/regressing receipts cannot erase previously
                    # credited progress and re-credit it in a later window.
                    if resolved_now is not None:
                        checkpoint_resolved_mark = max(
                            checkpoint_resolved_mark or 0, resolved_now)
                    cap += window
                    self._feedback.set_directive(
                        GOAL_BUDGET_CHECKPOINT_DIRECTIVE)
                    self._emit({
                        'type': 'goal_budget_checkpoint',
                        'node_id': loop_id,
                        'iteration': iteration,
                        'action': 'extend',
                        'window': window,
                        'new_max': cap,
                        'state_changing_delta': sc_delta,
                        'resolved_delta': resolved_delta,
                        'checkpoint': checkpoints,
                    })
                    logger.info(
                        '[FlowEngine] loop %s budget checkpoint #%d at '
                        'iteration %d: window advanced (sc+%d, resolved+%s) '
                        '— extending to %d',
                        loop_id,
                        checkpoints,
                        iteration,
                        sc_delta,
                        str(resolved_delta),
                        cap,
                    )
                else:
                    self._emit({
                        'type': 'goal_budget_checkpoint',
                        'node_id': loop_id,
                        'iteration': iteration,
                        'action': 'stalled',
                        'window': window,
                        'state_changing_delta': sc_delta,
                        'resolved_delta': resolved_delta,
                    })
                    logger.warning(
                        '[FlowEngine] loop %s budget checkpoint at iteration '
                        '%d: full window with zero state changes and zero '
                        'verified progress — STALLED (evidence-based stop, '
                        'not budget exhaustion)',
                        loop_id,
                        iteration,
                    )
                    exit_reason = 'stalled'
                    break

        if exit_reason == 'max_iterations':
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

    def _latest_cum_resolved(self) -> int | None:
        """Return the newest cumulative verified-resolved count, if any."""
        for entry in reversed(self._feedback.vu_progress_snapshot()):
            value = entry.get('cum_resolved')
            if value is not None:
                return int(value)
        return None


__all__ = [
    'DIMINISHING_RETURNS_DIRECTIVE',
    'GOAL_BLOCKED_CHALLENGE_DIRECTIVE',
    'GOAL_BLOCKED_JUSTIFICATION_DIRECTIVE',
    'GOAL_BLOCKED_MIN_JUSTIFICATION_CHARS',
    'GOAL_BLOCKED_REMAINING_DIRECTIVE',
    'GOAL_BUDGET_CHECKPOINT_DIRECTIVE',
    'MAX_REPLANS',
    'MAX_ZERO_DELIVERABLE_TURNS',
    'REPEATED_FEEDBACK_DIRECTIVE',
    'OrchestrationLoopAborted',
    'OrchestrationLoopRuntime',
    'ZERO_DELIVERABLE_DIRECTIVE',
]
