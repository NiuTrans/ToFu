"""Bounded paid-call accounting for every agentic Paper workflow.

Responsibility: define one stage budget contract, account each upstream model
dispatch, and provide the policy hooks consumed by ``agent_loop_policy``.
Prompt construction and task/event presentation remain with each workflow.

The dispatch envelope is deliberately independent from the generic agent-loop
repeat breaker.  Exact repeated calls are only one failure mode; a wandering
model can change arguments forever while an unmetered provider reports no
tokens.  Every Paper stage therefore gets both a token envelope and a finite
dispatch envelope.  The last admitted dispatch is tool-less synthesis, so the
budget includes the call needed to turn gathered evidence into a deliverable.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
import threading
from typing import Any, Optional

from lib.agent_loop import LoopDirective
from lib.cost import compute_cost, normalize_usage, split_input_tokens
from lib.log import get_logger

logger = get_logger(__name__)

PAPER_AGENT_USAGE_VERSION = 'paper-agent-usage/v1'
PAPER_AGENT_TOKEN_BUDGET_HARD_MAX = 2_000_000
PAPER_AGENT_DISPATCH_BUDGET_HARD_MAX = 32
PAPER_AGENT_TOKEN_BUDGET_MIN = 16_000
PAPER_AGENT_DISPATCH_BUDGET_MIN = 2


@dataclass(frozen=True)
class PaperAgentBudgetSpec:
    """One task-local API-cost envelope; no user state is retained globally."""

    stage: str
    token_env: str
    token_default: int
    dispatch_env: str
    dispatch_default: int


PAPER_AGENT_BUDGET_SPECS = {
    'report': PaperAgentBudgetSpec(
        'report', 'TOFU_PAPER_REPORT_AGENT_TOKEN_BUDGET', 480_000,
        'TOFU_PAPER_REPORT_AGENT_DISPATCH_BUDGET', 10),
    'qa': PaperAgentBudgetSpec(
        'qa', 'TOFU_PAPER_QA_AGENT_TOKEN_BUDGET', 240_000,
        'TOFU_PAPER_QA_AGENT_DISPATCH_BUDGET', 8),
    'deepen': PaperAgentBudgetSpec(
        'deepen', 'TOFU_PAPER_DEEPEN_AGENT_TOKEN_BUDGET', 320_000,
        'TOFU_PAPER_DEEPEN_AGENT_DISPATCH_BUDGET', 8),
    'insight': PaperAgentBudgetSpec(
        'insight', 'TOFU_PAPER_INSIGHT_AGENT_TOKEN_BUDGET', 240_000,
        'TOFU_PAPER_INSIGHT_AGENT_DISPATCH_BUDGET', 8),
    'recommend': PaperAgentBudgetSpec(
        'recommend', 'TOFU_PAPER_RECOMMEND_AGENT_TOKEN_BUDGET', 160_000,
        'TOFU_PAPER_RECOMMEND_AGENT_DISPATCH_BUDGET', 8),
    # Preserve the established research token environment names. Dispatch
    # limits are additive and use the same research-stage namespace.
    'survey': PaperAgentBudgetSpec(
        'survey', 'TOFU_RESEARCH_SURVEY_TOKEN_BUDGET', 240_000,
        'TOFU_RESEARCH_SURVEY_DISPATCH_BUDGET', 10),
    'ideate': PaperAgentBudgetSpec(
        'ideate', 'TOFU_RESEARCH_IDEATE_TOKEN_BUDGET', 160_000,
        'TOFU_RESEARCH_IDEATE_DISPATCH_BUDGET', 10),
}


def resolve_paper_agent_budget(
    env_name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
    environment: Mapping[str, str] | None = None,
) -> int:
    """Resolve one finite override; zero/malformed values fail back bounded."""
    env = os.environ if environment is None else environment
    bounded_default = max(minimum, min(maximum, int(default)))
    raw = env.get(env_name)
    if raw is None or not str(raw).strip():
        return bounded_default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError, OverflowError):
        logger.warning('[Paper:Usage] invalid %s=%r; using %d',
                       env_name, raw, bounded_default)
        return bounded_default
    if value < minimum:
        logger.warning('[Paper:Usage] %s=%r is below finite minimum %d; using %d',
                       env_name, raw, minimum, bounded_default)
        return bounded_default
    return min(maximum, value)


def paper_agent_token_budget(
    stage: str,
    environment: Mapping[str, str] | None = None,
) -> int:
    """Return the finite logical-token envelope for one named Paper stage."""
    try:
        spec = PAPER_AGENT_BUDGET_SPECS[stage]
    except KeyError as exc:
        raise ValueError(f'unknown Paper agent stage: {stage!r}') from exc
    return resolve_paper_agent_budget(
        spec.token_env, spec.token_default,
        minimum=PAPER_AGENT_TOKEN_BUDGET_MIN,
        maximum=PAPER_AGENT_TOKEN_BUDGET_HARD_MAX,
        environment=environment,
    )


def paper_agent_dispatch_budget(
    stage: str,
    environment: Mapping[str, str] | None = None,
) -> int:
    """Return dispatches including the reserved final synthesis attempt."""
    try:
        spec = PAPER_AGENT_BUDGET_SPECS[stage]
    except KeyError as exc:
        raise ValueError(f'unknown Paper agent stage: {stage!r}') from exc
    return resolve_paper_agent_budget(
        spec.dispatch_env, spec.dispatch_default,
        minimum=PAPER_AGENT_DISPATCH_BUDGET_MIN,
        maximum=PAPER_AGENT_DISPATCH_BUDGET_HARD_MAX,
        environment=environment,
    )


class PaperAgentUsageMeter:
    """Account model calls and force finite evidence-to-answer convergence.

    ``token_budget`` watches only calls made through the open-ended agent loop.
    Direct finite calls may use :meth:`record`; this lets auto-research keep
    rubric calls in the same cost snapshot without letting them affect loop
    admission. ``dispatch_budget`` counts actual agent dispatch attempts, including
    unmetered responses and the final tool-less synthesis call.
    """

    def __init__(self, stage: str, *, token_budget: int,
                 dispatch_budget: int, repeat_limit: int = 2,
                 fallback_model: str = ''):
        self.stage = str(stage or 'paper')
        self.token_budget = max(0, int(token_budget or 0))
        self.dispatch_budget = max(0, int(dispatch_budget or 0))
        self.repeat_limit = max(0, int(repeat_limit or 0))
        self.fallback_model = fallback_model or ''
        self.calls = 0
        self.priced_calls = 0
        self.unmetered_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0
        self.reasoning_tokens = 0
        self.total_tokens = 0
        self.agent_tokens = 0
        self.agent_dispatches = 0
        self.cost_usd = 0.0
        self.cost_cny = 0.0
        self.cost_estimated = False
        self.models: set[str] = set()
        self.force_final_reason = ''
        self.budget_ignored = False
        self._forced_rounds: set[int] = set()
        self._previous_fingerprint: Optional[str] = None
        self._repeat_streak = 0
        self._lock = threading.RLock()

    @classmethod
    def for_stage(
        cls,
        stage: str,
        *,
        fallback_model: str = '',
        environment: Mapping[str, str] | None = None,
    ) -> 'PaperAgentUsageMeter':
        """Build the canonical finite meter for a named workflow."""
        return cls(
            stage,
            token_budget=paper_agent_token_budget(stage, environment),
            dispatch_budget=paper_agent_dispatch_budget(stage, environment),
            # Exact call+world repetition is owned by the guarded chassis. The
            # compatibility detector remains available to standalone meters,
            # but production owners must preserve the canonical no_progress
            # reason and threshold.
            repeat_limit=0,
            fallback_model=fallback_model,
        )

    def _compute_cost(self, usage, *, model_id: str, provider_id: str | None):
        """Pricing seam retained for the research compatibility subclass."""
        return compute_cost(
            usage, model_id=model_id, provider_id=provider_id)

    def record(self, usage) -> None:
        """Record one billed dispatch; telemetry can never fail the task."""
        with self._lock:
            self._record_unlocked(usage)

    def _record_unlocked(self, usage) -> None:
        self.calls += 1
        if not isinstance(usage, dict):
            self.unmetered_calls += 1
            return
        try:
            normal = normalize_usage(usage)
            self.prompt_tokens += normal['input']
            self.completion_tokens += normal['output']
            self.cache_read_tokens += normal['cache_read']
            self.cache_write_tokens += normal['cache_write']
            self.reasoning_tokens += normal['thinking']
            _uncached, total_input = split_input_tokens(usage)
            self.total_tokens += int(total_input or 0) + int(normal['output'] or 0)

            dispatch = usage.get('_dispatch') or {}
            model = dispatch.get('model') or usage.get('model') or self.fallback_model
            provider = dispatch.get('provider_id') or usage.get('provider_id')
            if model:
                self.models.add(str(model))
            priced = self._compute_cost(
                usage, model_id=str(model or ''),
                provider_id=str(provider) if provider else None)
            if priced:
                self.priced_calls += 1
                self.cost_usd += float(priced.get('costUsd') or 0)
                self.cost_cny += float(priced.get('costCny') or 0)
                if priced.get('pricingSource') == 'default_estimate':
                    self.cost_estimated = True
            else:
                self.unmetered_calls += 1
        except Exception as exc:
            self.unmetered_calls += 1
            logger.debug('[Paper:Usage] record failed (non-fatal): %s', exc)

    def _force_final_unlocked(self, reason: str) -> None:
        if self.force_final_reason:
            return
        self.force_final_reason = reason
        logger.warning(
            '[Paper:%s] %s reached; next/current dispatch synthesizes without tools',
            self.stage, reason)

    def tools_for_round(self, requested_tools: Any, round_index: int):
        """Count one agent dispatch and reserve the last call for synthesis."""
        with self._lock:
            self.agent_dispatches += 1
            if (not self.force_final_reason and self.dispatch_budget
                    and self.agent_dispatches >= self.dispatch_budget):
                self._force_final_unlocked('dispatch_budget')
            if self.force_final_reason:
                self._forced_rounds.add(int(round_index))
                return None
            return requested_tools

    def observe_agent_round(self, usage, message) -> None:
        """Record a loop dispatch and update token/repetition finalisation."""
        with self._lock:
            before = self.total_tokens
            self._record_unlocked(usage)
            self.agent_tokens += max(0, self.total_tokens - before)
            if (self.token_budget and self.agent_tokens >= self.token_budget
                    and not self.force_final_reason):
                self._force_final_unlocked('token_budget')

            calls = message.get('tool_calls') if isinstance(message, dict) else None
            if not calls or self.repeat_limit <= 0:
                self._previous_fingerprint = None
                self._repeat_streak = 0
                return
            fingerprint = repr([
                (tc.get('function', {}).get('name', ''),
                 tc.get('function', {}).get('arguments', ''))
                for tc in calls if isinstance(tc, dict)
            ])
            if fingerprint == self._previous_fingerprint:
                self._repeat_streak += 1
                if self._repeat_streak >= self.repeat_limit:
                    self._force_final_unlocked('repeated_tool_calls')
            else:
                self._repeat_streak = 0
            self._previous_fingerprint = fingerprint

    def allowed_tools(self, requested_tools):
        """Compatibility reader; new loops use :meth:`tools_for_round`."""
        with self._lock:
            return None if self.force_final_reason else requested_tools

    def forced_round(self, round_index: int) -> bool:
        """Whether this exact upstream attempt was admitted without tools."""
        with self._lock:
            return int(round_index) in self._forced_rounds

    def decide_round(self, round_index: int, message) -> LoopDirective | None:
        """Reject provider tool calls emitted after tool authority was removed."""
        with self._lock:
            forced = int(round_index) in self._forced_rounds
            calls = message.get('tool_calls') if isinstance(message, dict) else None
            if forced and calls:
                self.budget_ignored = True
                logger.error(
                    '[Paper:%s] provider emitted tool calls after tools were removed',
                    self.stage)
                return LoopDirective.halt('agent_budget_ignored')
        return None

    def snapshot(self) -> dict:
        """Return the stable JSON-safe accounting and admission contract."""
        with self._lock:
            return {
                'version': PAPER_AGENT_USAGE_VERSION,
                'stage': self.stage,
                'calls': self.calls,
                'priced_calls': self.priced_calls,
                'unmetered_calls': self.unmetered_calls,
                'prompt_tokens': self.prompt_tokens,
                'completion_tokens': self.completion_tokens,
                'cache_read_tokens': self.cache_read_tokens,
                'cache_write_tokens': self.cache_write_tokens,
                'reasoning_tokens': self.reasoning_tokens,
                'total_tokens': self.total_tokens,
                'agent_tokens': self.agent_tokens,
                'agent_token_budget': self.token_budget,
                'agent_dispatches': self.agent_dispatches,
                'agent_dispatch_budget': self.dispatch_budget,
                'forced_final': bool(self.force_final_reason),
                'forced_final_reason': self.force_final_reason,
                'budget_ignored': self.budget_ignored,
                'cost_usd': round(self.cost_usd, 4),
                'cost_cny': round(self.cost_cny, 4),
                'cost_estimated': self.cost_estimated,
                'models': sorted(self.models),
            }


__all__ = [
    'PAPER_AGENT_BUDGET_SPECS',
    'PAPER_AGENT_DISPATCH_BUDGET_HARD_MAX',
    'PAPER_AGENT_TOKEN_BUDGET_HARD_MAX',
    'PAPER_AGENT_USAGE_VERSION',
    'PaperAgentBudgetSpec',
    'PaperAgentUsageMeter',
    'paper_agent_dispatch_budget',
    'paper_agent_token_budget',
    'resolve_paper_agent_budget',
]
