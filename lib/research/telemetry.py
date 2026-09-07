"""Auto-research usage aggregation over the shared Paper paid-call policy.

Research is a multi-call product: survey synthesis may take several tool
rounds and ideation adds one rubric call for every structurally valid idea.
Treating only the last response's usage as "the cost" therefore understates
the work substantially.  This module records every dispatch through the
project's canonical usage/cost helpers and exposes one JSON-safe snapshot.

The open-ended agent-loop accounting is owned once by
``lib.paper.agent_usage``. This module keeps the historical
``ResearchUsageMeter`` import and folds survey/ideate/evaluate snapshots into
the durable auto-research aggregate.
"""

from __future__ import annotations

from typing import Optional

from lib.cost import compute_cost
from lib.paper.agent_usage import (
    PaperAgentUsageMeter,
    resolve_paper_agent_budget,
)

__all__ = ['ResearchUsageMeter', 'research_token_budget', 'aggregate_research_usage']


def research_token_budget(env_name: str, default: int) -> int:
    """Resolve a finite legacy-named research token envelope."""
    from lib.paper.agent_usage import (
        PAPER_AGENT_TOKEN_BUDGET_HARD_MAX,
        PAPER_AGENT_TOKEN_BUDGET_MIN,
    )
    return resolve_paper_agent_budget(
        env_name, default,
        minimum=PAPER_AGENT_TOKEN_BUDGET_MIN,
        maximum=PAPER_AGENT_TOKEN_BUDGET_HARD_MAX,
    )


def aggregate_research_usage(survey_usage: dict, ideate_usage: dict,
                             evaluate_usage: Optional[dict] = None, *,
                             harvest_usage: Optional[dict] = None) -> dict:
    """Fold persisted/live stage snapshots into one transparent run total."""
    stages = {'survey': survey_usage or {}, 'ideate': ideate_usage or {}}
    # Search translation is a paid harvest call for non-English directions.
    # Omit the stage for historical and seeded runs that did not need it.
    if harvest_usage:
        stages = {'harvest': harvest_usage, **stages}
    # Keep the old two-stage shape when reading historical rows. New runs
    # always pass an evaluation snapshot, including an explicit empty one.
    if evaluate_usage is not None:
        stages['evaluate'] = evaluate_usage or {}
    additive = (
        'calls', 'priced_calls', 'unmetered_calls', 'prompt_tokens',
        'completion_tokens', 'cache_read_tokens', 'cache_write_tokens',
        'reasoning_tokens', 'total_tokens', 'agent_tokens',
        'agent_token_budget',
        'agent_dispatches', 'agent_dispatch_budget',
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
    total['budget_ignored'] = any(bool((stage or {}).get('budget_ignored'))
                                  for stage in stages.values())
    total['models'] = sorted({str(model) for stage in stages.values()
                              for model in ((stage or {}).get('models') or [])})
    return {'total': total, 'stages': stages}


class ResearchUsageMeter(PaperAgentUsageMeter):
    """Compatibility owner for survey/ideate plus finite rubric calls."""

    def __init__(self, stage: str, *, token_budget: int = 240_000,
                 dispatch_budget: int = 10, repeat_limit: int = 2,
                 fallback_model: str = ''):
        super().__init__(
            stage, token_budget=token_budget,
            dispatch_budget=dispatch_budget, repeat_limit=repeat_limit,
            fallback_model=fallback_model)

    def _compute_cost(self, usage, *, model_id: str, provider_id: str | None):
        # Keep tests and downstream callers that monkeypatch this module's
        # historical pricing seam working after accounting moved to Paper.
        return compute_cost(
            usage, model_id=model_id, provider_id=provider_id)
