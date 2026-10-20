"""Finite-progress policy for every agentic Paper workflow.

Responsibility: compose the shared agent-loop chassis with Paper's common
no-progress threshold, account each paid round, and convert non-completion
outcomes into honest control flow. Entry point:
:func:`run_guarded_paper_agent_loop`. Dependencies are the generic agent loop,
the shared Paper resource contract, the usage meter, and LLM abort errors.

A Paper loop is bounded by observable progress, the user's Stop, and — only
when the caller declares one — its own envelope. The chassis never narrows its
own authority to finish: a spent envelope raises
:class:`~lib.paper.agent_usage.PaperAgentBudgetExceeded` before the paid request.
"""

from __future__ import annotations

from typing import Any

from lib.agent_loop.agent_loop import LoopOutcome, run_agent_loop, agent_loop_checkpoint_limits
from lib.llm.llm_errors import AbortedError
from lib.paper.contracts import (
    PAPER_AGENT_MAX_CONSECUTIVE_NO_PROGRESS_ROUNDS,
)
from lib.paper.agent_usage import PaperAgentUsageMeter

# Provider/model catalogs can contain newly discovered offerings whose text
# capability has not yet been proven on the configured gateway.  Paper agents
# carry large, expensive grounded contexts, so one dispatch may rotate across
# a bounded set of deterministic 400/no-route candidates before reaching a
# healthy text model.  Keep this separate from the paid agent-round budget:
# failed admission/route attempts produce no model result and no new round.
PAPER_AGENT_ROUTE_MAX_RETRIES = 12


class PaperAgentLoopHalted(RuntimeError):
    """Raised when a Paper loop stops without completion or user abort."""

    def __init__(self, context: str, outcome: LoopOutcome):
        self.context = context
        self.reason = outcome.exit_reason or 'unknown'
        self.no_progress_streak = outcome.consecutive_no_progress_rounds
        super().__init__(
            f'{context} halted before completion: {self.reason} '
            f'(streak={self.no_progress_streak})')


def paper_agent_checkpoint_limits():
    """Research admission uses exactly the guarded Paper loop's default limits."""
    return agent_loop_checkpoint_limits(
        max_consecutive_no_progress_rounds=PAPER_AGENT_MAX_CONSECUTIVE_NO_PROGRESS_ROUNDS)


def run_guarded_paper_agent_loop(
    *,
    context: str,
    allow_aborted_outcome: bool = False,
    usage_meter: PaperAgentUsageMeter | None = None,
    **loop_kwargs: Any,
) -> LoopOutcome:
    """Run one Paper agent loop with finite progress and paid-call semantics.

    Background report/Q&A/Deepen tasks opt into an aborted outcome because
    they publish a distinct partial/aborted event. Synchronous research stages
    use the default and receive :class:`AbortedError`, which their existing
    outer lifecycle already maps to an honest non-success result.
    """
    normalized_context = str(context or '').strip()
    if not normalized_context:
        raise ValueError('paper agent loop context is required')
    if 'max_consecutive_no_progress_rounds' in loop_kwargs:
        raise TypeError(
            'paper agent no-progress threshold is owned by agent_loop_policy')

    # Compose accounting here so a new Paper owner cannot use the chassis but
    # forget to price its rounds. The tools the loop admits are exactly the
    # tools the caller requested; caller hooks remain additive and see the
    # original response after the meter has accounted the upstream attempt.
    effective_loop_kwargs = dict(loop_kwargs)
    if usage_meter is not None:
        caller_dispatch = effective_loop_kwargs['dispatch']
        caller_on_round_result = effective_loop_kwargs.get('on_round_result')

        def _accounted_dispatch(round_index, requested_tools):
            usage_meter.begin_dispatch()
            return caller_dispatch(round_index, requested_tools)

        def _account_round(round_index, message, finish, usage):
            usage_meter.observe_agent_round(usage, message)
            if caller_on_round_result is not None:
                caller_on_round_result(round_index, message, finish, usage)

        effective_loop_kwargs['dispatch'] = _accounted_dispatch
        effective_loop_kwargs['on_round_result'] = _account_round

    outcome = run_agent_loop(
        max_consecutive_no_progress_rounds=(
            PAPER_AGENT_MAX_CONSECUTIVE_NO_PROGRESS_ROUNDS),
        **effective_loop_kwargs,
    )
    if outcome.halted:
        raise PaperAgentLoopHalted(normalized_context, outcome)
    if outcome.aborted and not allow_aborted_outcome:
        raise AbortedError(
            f'{normalized_context} aborted ({outcome.exit_reason or "unknown"})')
    return outcome


__all__ = [
    'PAPER_AGENT_ROUTE_MAX_RETRIES',
    'PaperAgentLoopHalted',
    'run_guarded_paper_agent_loop',
]
