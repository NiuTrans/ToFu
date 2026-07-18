"""lib/cost_estimator.py — Per-round cost estimation + budget gate.

Two responsibilities:

1. ``estimate_usage_cost(usage, model, provider_id=None)`` converts a
   single usage dict (Anthropic OR OpenAI shape) into a USD figure using
   ``lib.pricing.lookup_pricing``.  Used by the orchestrator to evaluate
   ``max_budget_usd`` after every round.

2. ``check_budget(task, accumulated_usage, model, max_budget_usd, ...)``
   short-circuits the agent loop when the running total exceeds the cap.

Inspired by Claude Agent SDK's ``max_budget_usd`` option — a hard $-cap
that halts the loop deterministically (vs token caps which are per-call,
not per-task).

Design rules
------------
* Return zero-cost on missing pricing data, NEVER raise.  Cost estimation
  is best-effort: an unknown model should not break the loop.
* Token-shape tolerant — accept both Anthropic and OpenAI keys.
* Cache discounts respected — ``cacheWriteMul`` / ``cacheReadMul`` from
  ``MODEL_PRICING`` are applied.  Free / unknown cache fields default to
  the standard input price.
"""

from __future__ import annotations

from lib.log import audit_log, get_logger

logger = get_logger(__name__)


def _split_tokens(usage: dict | None) -> tuple[int, int, int, int]:
    """Return (uncached_input, cache_write, cache_read, output) tokens.

    Tolerates both Anthropic and OpenAI usage shapes:

      * Anthropic — ``input_tokens``, ``cache_creation_input_tokens``,
        ``cache_read_input_tokens``, ``output_tokens``.
        Convention: ``input_tokens`` excludes cache hits.

      * OpenAI — ``prompt_tokens``, ``completion_tokens``, plus
        ``prompt_tokens_details: {cached_tokens}`` (some providers).
        Convention: ``prompt_tokens`` already includes cached tokens.

    The returned ``uncached_input`` is the count we pay full input price for.
    """
    if not isinstance(usage, dict):
        return 0, 0, 0, 0

    from lib.cost import normalize_usage
    _u = normalize_usage(usage)
    inp_raw = _u['input']
    cw = _u['cache_write']
    cr = _u['cache_read']
    out = _u['output']

    # OpenAI convention: prompt_tokens may already include cached tokens.
    # Heuristic: if input <= (cw+cr), Anthropic convention (input excludes
    # cache).  Otherwise OpenAI — subtract cache from input.
    if cw + cr > 0 and inp_raw > cw + cr:
        uncached = max(0, inp_raw - cw - cr)
    else:
        uncached = inp_raw

    return uncached, cw, cr, out


def estimate_usage_cost(usage: dict | None, model: str,
                        provider_id: str = '') -> float:
    """Estimate the USD cost of a single round's ``usage`` dict.

    Returns 0.0 when pricing data is unavailable or usage is empty.  Never
    raises — callers can compare against budgets unconditionally.
    """
    if not usage or not model:
        return 0.0
    try:
        from lib.pricing import lookup_pricing
        pricing = lookup_pricing(model, provider_id or None)
    except Exception as e:
        logger.debug('[Cost] lookup_pricing failed for model=%s: %s', model, e)
        return 0.0
    if not pricing:
        return 0.0

    uncached, cw, cr, out = _split_tokens(usage)
    inp_per_m = float(pricing.get('input', 0) or 0)   # USD per 1M tokens
    out_per_m = float(pricing.get('output', 0) or 0)
    cw_mul = float(pricing.get('cacheWriteMul', 1.0) or 1.0)
    cr_mul = float(pricing.get('cacheReadMul', 0.0) or 0.0)

    cost = (
        uncached * inp_per_m / 1_000_000.0
        + cw * inp_per_m * cw_mul / 1_000_000.0
        + cr * inp_per_m * cr_mul / 1_000_000.0
        + out * out_per_m / 1_000_000.0
    )
    return cost


def check_budget(task: dict, accumulated_usage: dict | None,
                  model: str, max_budget_usd: float,
                  *, provider_id: str = '',
                  round_num: int = 0) -> tuple[bool, float, str]:
    """Evaluate the running cost against ``max_budget_usd``.

    Returns ``(exceeded, cost_so_far, reason)``.  The orchestrator should
    break out of the round loop when ``exceeded`` is True.

    ``max_budget_usd <= 0`` disables the gate (returns ``(False, 0.0, '')``).
    """
    if not max_budget_usd or max_budget_usd <= 0:
        return False, 0.0, ''

    cost = estimate_usage_cost(accumulated_usage, model, provider_id)
    if cost <= max_budget_usd:
        return False, cost, ''

    reason = (f'max_budget_usd exceeded: '
              f'${cost:.4f} > ${max_budget_usd:.4f} '
              f'(model={model}, round={round_num})')
    logger.warning('[Task %s] [BudgetGate] %s',
                   (task.get('id') or '??')[:8], reason)
    audit_log('budget_exceeded',
              task_id=task.get('id') or '',
              model=model, cost_usd=round(cost, 6),
              budget_usd=max_budget_usd,
              round_num=round_num)
    return True, cost, reason


__all__ = ['estimate_usage_cost', 'check_budget']
