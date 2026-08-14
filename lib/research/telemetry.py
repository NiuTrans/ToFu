"""Usage accounting and adaptive guards for auto-research model calls.

Research is a multi-call product: survey synthesis may take several tool
rounds and ideation adds one rubric call for every structurally valid idea.
Treating only the last response's usage as "the cost" therefore understates
the work substantially.  This module records every dispatch through the
project's canonical usage/cost helpers and exposes one JSON-safe snapshot.

The guard is deliberately scoped to auto-research.  It does not impose a
global round ceiling on :mod:`lib.agent_loop`; once the configured token
envelope or a repeated-call pattern is reached, the *next* research dispatch
simply receives no tools and must synthesize a terminal answer from the
evidence already collected.
"""

from __future__ import annotations

import os
from typing import Optional

from lib.cost import compute_cost, normalize_usage, split_input_tokens
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['ResearchUsageMeter', 'research_token_budget', 'aggregate_research_usage']


def research_token_budget(env_name: str, default: int) -> int:
    """Read a research-only token envelope, falling back safely.

    ``0`` explicitly disables the envelope. Invalid or negative values use the
    documented default instead of accidentally making every run tool-less.
    """
    raw = os.environ.get(env_name)
    if raw is None or not raw.strip():
        return max(0, int(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning('[Research:Usage] invalid %s=%r; using %d',
                       env_name, raw, default)
        return max(0, int(default))
    if value < 0:
        logger.warning('[Research:Usage] negative %s=%r; using %d',
                       env_name, raw, default)
        return max(0, int(default))
    return value


def aggregate_research_usage(survey_usage: dict, ideate_usage: dict,
                             evaluate_usage: Optional[dict] = None) -> dict:
    """Fold persisted/live stage snapshots into one transparent run total."""
    stages = {'survey': survey_usage or {}, 'ideate': ideate_usage or {}}
    # Keep the old two-stage shape when reading historical rows. New runs
    # always pass an evaluation snapshot, including an explicit empty one.
    if evaluate_usage is not None:
        stages['evaluate'] = evaluate_usage or {}
    additive = (
        'calls', 'priced_calls', 'unmetered_calls', 'prompt_tokens',
        'completion_tokens', 'cache_read_tokens', 'cache_write_tokens',
        'reasoning_tokens', 'total_tokens', 'agent_tokens',
        'agent_token_budget',
    )
    total = {key: sum(int((stage or {}).get(key) or 0)
                      for stage in stages.values()) for key in additive}
    total['cost_usd'] = round(sum(float((stage or {}).get('cost_usd') or 0)
                                  for stage in stages.values()), 4)
    total['cost_cny'] = round(sum(float((stage or {}).get('cost_cny') or 0)
                                  for stage in stages.values()), 4)
    total['cost_estimated'] = any(bool((stage or {}).get('cost_estimated'))
                                  for stage in stages.values())
    total['forced_final'] = any(bool((stage or {}).get('forced_final'))
                                for stage in stages.values())
    total['models'] = sorted({str(model) for stage in stages.values()
                              for model in ((stage or {}).get('models') or [])})
    return {'total': total, 'stages': stages}


class ResearchUsageMeter:
    """Accumulate all model calls in one research stage.

    ``token_budget`` applies only to the open-ended agentic portion. Finite
    rubric calls are still recorded in the same stage snapshot but cannot
    create a runaway loop, so they are not cut off mid-batch.
    """

    def __init__(self, stage: str, *, token_budget: int = 0,
                 repeat_limit: int = 2, fallback_model: str = ''):
        self.stage = stage or 'research'
        self.token_budget = max(0, int(token_budget or 0))
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
        # Cache-aware logical work. For OpenAI payloads prompt_tokens already
        # includes cache hits; for Anthropic payloads split_input_tokens adds
        # the separately reported cache tokens once. ``agent_tokens`` is the
        # subset from the open-ended loop and is what the envelope watches;
        # ``total_tokens`` also includes finite rubric calls.
        self.total_tokens = 0
        self.agent_tokens = 0
        self.cost_usd = 0.0
        self.cost_cny = 0.0
        self.cost_estimated = False
        self.models: set[str] = set()
        self.force_final_reason = ''
        self._previous_fingerprint: Optional[str] = None
        self._repeat_streak = 0

    def record(self, usage) -> None:
        """Record one billed dispatch. Never lets telemetry fail the run."""
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
            priced = compute_cost(usage, model_id=str(model or ''),
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
            logger.debug('[Research:Usage] record failed (non-fatal): %s', exc)

    def observe_agent_round(self, usage, message) -> None:
        """Record a tool-loop dispatch and update adaptive finalisation state."""
        before = self.total_tokens
        self.record(usage)
        self.agent_tokens += max(0, self.total_tokens - before)
        if self.token_budget and self.agent_tokens >= self.token_budget \
                and not self.force_final_reason:
            self.force_final_reason = 'token_budget'
            logger.warning('[Research:%s] token envelope reached (%d >= %d); '
                           'next round will synthesize without tools',
                           self.stage, self.agent_tokens, self.token_budget)

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
            if self._repeat_streak >= self.repeat_limit \
                    and not self.force_final_reason:
                self.force_final_reason = 'repeated_tool_calls'
                logger.warning('[Research:%s] repeated tool-call pattern; '
                               'next round will synthesize without tools', self.stage)
        else:
            self._repeat_streak = 0
        self._previous_fingerprint = fingerprint

    def allowed_tools(self, requested_tools):
        """Return the requested schema until adaptive finalisation is needed."""
        return None if self.force_final_reason else requested_tools

    def snapshot(self) -> dict:
        """Return the stable, JSON-safe frontend/persistence contract."""
        return {
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
            'forced_final': bool(self.force_final_reason),
            'forced_final_reason': self.force_final_reason,
            'cost_usd': round(self.cost_usd, 4),
            'cost_cny': round(self.cost_cny, 4),
            'cost_estimated': self.cost_estimated,
            'models': sorted(self.models),
        }
