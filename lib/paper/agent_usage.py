"""Paid-call accounting for every agentic Paper workflow — no implicit envelopes.

Responsibility: account each upstream model dispatch, price it, and expose one
JSON-safe snapshot. Entry point: :class:`PaperAgentUsageMeter`.

A Paper stage has **no** spend or dispatch limit of its own. Every run is
bounded by the shared chassis' no-progress breaker, the user's Stop, and — only
when a caller explicitly declares one — its own envelope. There is deliberately
no stage default: a fixed token/dispatch number cannot be derived from the
paper, the model, or the machine, so imposing one silently truncates evidence.

When a caller *does* declare an envelope (research actions freeze one in their
task identity), exhausting it fails the dispatch with
:class:`PaperAgentBudgetExceeded`. It never removes the model's tools: a
tool-less round invites the model to print a tool call as prose, which then
looks like a finished answer.

The legacy snapshot keys (``agent_token_budget``, ``agent_dispatch_budget``,
``forced_final``, ``forced_final_reason``, ``budget_ignored``) remain because
that shape is the durable checkpoint and observation contract. A stage without
a declared envelope reports zero, which the contract already defines as
unbounded, and the terminal flags can no longer be set.
"""

from __future__ import annotations

from collections.abc import Mapping
import math
import threading
from typing import Any

from lib.cost import compute_cost, normalize_usage, split_input_tokens
from lib.log import get_logger

logger = get_logger(__name__)

PAPER_AGENT_USAGE_VERSION = 'paper-agent-usage/v1'


class PaperAgentBudgetExceeded(RuntimeError):
    """A caller-declared envelope is spent; the task fails instead of degrading."""

    def __init__(self, stage: str, reason: str, *, token_budget: int = 0,
                 dispatch_budget: int = 0, agent_tokens: int = 0,
                 agent_dispatches: int = 0):
        self.stage = stage
        self.reason = reason
        self.token_budget = token_budget
        self.dispatch_budget = dispatch_budget
        self.agent_tokens = agent_tokens
        self.agent_dispatches = agent_dispatches
        super().__init__(
            f'Paper {stage} paid-call budget exhausted ({reason}): '
            f'tokens {agent_tokens}/{token_budget or "unbounded"}, '
            f'dispatches {agent_dispatches}/{dispatch_budget or "unbounded"}')


class PaperAgentUsageMeter:
    """Account model calls and price them; impose no envelope by default.

    ``token_budget`` / ``dispatch_budget`` are explicit caller declarations.
    Zero (the default for every Paper stage) means unbounded. ``agent_tokens``
    counts only open-ended agent-loop dispatches, matching the checkpoint
    contract that a declared envelope was originally frozen against.
    """

    def __init__(self, stage: str, *, token_budget: int = 0,
                 dispatch_budget: int = 0, fallback_model: str = ''):
        self.stage = str(stage or 'paper')
        self.token_budget = max(0, int(token_budget or 0))
        self.dispatch_budget = max(0, int(dispatch_budget or 0))
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
        # Terminal finalisation state is restore-only: nothing sets it any more,
        # but a checkpoint written before this change must round-trip unchanged.
        self.force_final_reason = ''
        self.budget_ignored = False
        self._lock = threading.RLock()

    @classmethod
    def for_stage(cls, stage: str, *,
                  fallback_model: str = '') -> 'PaperAgentUsageMeter':
        """Build the meter for a named Paper workflow; no stage has a default cap."""
        return cls(stage, fallback_model=fallback_model)

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, Any], *, fallback_model: str = '') -> 'PaperAgentUsageMeter':
        """Restore complete accounting; loop/tool replay authority is not restored.

        Missing counters are refused rather than synthesized from current
        defaults.
        """
        meter = cls('restoring', fallback_model=fallback_model)
        template = meter.snapshot()
        if (not isinstance(snapshot, Mapping) or set(snapshot) != set(template)
                or snapshot.get('version') != PAPER_AGENT_USAGE_VERSION):
            raise ValueError('Incomplete or unsupported Paper usage checkpoint')
        for key, default in template.items():
            value = snapshot[key]
            if type(default) is bool:
                valid = type(value) is bool
            elif type(default) is int:
                valid = type(value) is int and value >= 0
            elif type(default) is float:
                valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
            elif type(default) is str:
                valid = isinstance(value, str)
            else:
                valid = (isinstance(value, list) and all(isinstance(item, str) for item in value)
                         and len(value) == len(set(value)))
            if not valid:
                raise ValueError(f'Invalid Paper usage checkpoint field: {key}')
        if not snapshot['stage'] or snapshot['forced_final'] != bool(snapshot['forced_final_reason']):
            raise ValueError('Paper usage checkpoint finalisation is inconsistent')
        if snapshot['agent_tokens'] > snapshot['total_tokens']:
            raise ValueError('Paper usage checkpoint agent tokens exceed total tokens')
        aliases = {'agent_token_budget': 'token_budget', 'agent_dispatch_budget': 'dispatch_budget'}
        for key in template:
            if key in ('version', 'forced_final', 'models'):
                continue
            setattr(meter, aliases.get(key, key), snapshot[key])
        meter.models = set(snapshot['models'])
        return meter

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

    def begin_dispatch(self) -> None:
        """Count one agent dispatch; refuse only a caller-declared exhausted envelope.

        Raises :class:`PaperAgentBudgetExceeded` *before* the paid request is
        sent, so an over-budget run fails with a typed error instead of
        continuing with degraded authority.
        """
        with self._lock:
            self.agent_dispatches += 1
            if self.dispatch_budget and self.agent_dispatches > self.dispatch_budget:
                raise PaperAgentBudgetExceeded(
                    self.stage, 'dispatch_budget',
                    token_budget=self.token_budget,
                    dispatch_budget=self.dispatch_budget,
                    agent_tokens=self.agent_tokens,
                    agent_dispatches=self.agent_dispatches)
            if self.token_budget and self.agent_tokens >= self.token_budget:
                raise PaperAgentBudgetExceeded(
                    self.stage, 'token_budget',
                    token_budget=self.token_budget,
                    dispatch_budget=self.dispatch_budget,
                    agent_tokens=self.agent_tokens,
                    agent_dispatches=self.agent_dispatches)

    def observe_agent_round(self, usage, message) -> None:
        """Record one loop dispatch and add its tokens to the agent total."""
        with self._lock:
            before = self.total_tokens
            self._record_unlocked(usage)
            self.agent_tokens += max(0, self.total_tokens - before)

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
                # Terminal flags retained for the durable contract; a Paper run
                # no longer degrades its own authority, so new runs leave them
                # unset and restored old checkpoints keep their original value.
                'forced_final': bool(self.force_final_reason),
                'forced_final_reason': self.force_final_reason,
                'budget_ignored': self.budget_ignored,
                'cost_usd': round(self.cost_usd, 4),
                'cost_cny': round(self.cost_cny, 4),
                'cost_estimated': self.cost_estimated,
                'models': sorted(self.models),
            }


__all__ = [
    'PAPER_AGENT_USAGE_VERSION',
    'PaperAgentBudgetExceeded',
    'PaperAgentUsageMeter',
]
