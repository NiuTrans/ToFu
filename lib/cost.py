"""lib/cost.py — Per-message cost calculation (THE single cost engine).

★ Single source of truth (2026-06-24): this is the ONE place per-token cost
arithmetic happens. Both surfaces delegate here:

  * **Display** — the headless ``/api/v1/messages/cost`` endpoint + the SSE
    done-event / persisted ``cost`` stamps; the JS ``calcCostCny`` is a thin
    fetch wrapper to that endpoint (it does NO client-side pricing math).
  * **Billing** — ``lib/billing/cost.compute_request_cost`` calls
    ``compute_cost`` and converts the per-component USD sub-costs
    (``inputCostUsd`` / ``outputCostUsd`` / ``cacheWriteCostUsd`` /
    ``cacheReadCostUsd``, 9-dp precise) into micro-credits, then layers the
    relay margin. So the wallet debit and the displayed ¥/$ can never drift.

Historically a partial port of the old ``static/js/core.js:calcCostCny``; the
JS pricing tables have since been deleted, leaving this as the sole engine.
Encapsulates:

* Anthropic-vs-OpenAI cache-token convention detection.
* Cache-write / cache-read multiplier handling per provider.
* Qwen tiered pricing (CNY-native, billed by total tokens at the
  applicable tier).
* Provider-scoped pricing override resolution.
* USD → CNY conversion using the live exchange rate.

Centralised so:

* The headless ``/api/v1/messages/cost`` endpoint produces the same
  numbers the UI shows in its finish-info bar.
* Adding a pricing convention (e.g. a new tiered-billing provider)
  means editing ``lib/pricing.py`` once and the math here picks it up.
* SDK callers building cost dashboards over the conversation log don't
  need to re-implement the dual-convention cache detection.

Public API
----------

  compute_cost(usage, model_id=None, provider_id=None) -> dict | None
      Returns ``{costUsd, costCny, inputTokens, outputTokens, ...}`` —
      same shape as the JS ``calcCostCny`` output. ``None`` when there
      is nothing to charge for (zeros across the board).
"""

from __future__ import annotations

from typing import Optional

from lib.log import get_logger
from lib.pricing import (
    DEFAULT_USD_CNY_RATE, QWEN_PRICING_CNY,
    get_pricing_data, lookup_pricing,
)

logger = get_logger(__name__)


# ── Usage-dict key normalisation (pure aliasing, NO cache-convention math) ──

# A usage dict arrives in one of two vendor spellings and carries ONE
# convention only (OpenAI keys XOR Anthropic keys with meaningful values),
# so ``a or b`` and ``b or a`` are equivalent — the fallback order is
# immaterial. This helper is DELIBERATELY just the key aliasing: it does NOT
# apply the Anthropic-vs-OpenAI cache-token convention (`inp <= cw+cr`), which
# stays where the arithmetic lives (``compute_cost`` / ``cost_estimator``).
#
# Key aliases (OpenAI ⇄ Anthropic):
#   input   : prompt_tokens              ⇄ input_tokens
#   output  : completion_tokens          ⇄ output_tokens
#   cache_w : cache_write_tokens         ⇄ cache_creation_input_tokens
#   cache_r : cache_read_tokens          ⇄ cache_read_input_tokens
#   think   : reasoning_tokens           ⇄ thinking_tokens
_USAGE_KEY_ALIASES = {
    'input': ('prompt_tokens', 'input_tokens'),
    'output': ('completion_tokens', 'output_tokens'),
    'cache_write': ('cache_write_tokens', 'cache_creation_input_tokens'),
    'cache_read': ('cache_read_tokens', 'cache_read_input_tokens'),
    'thinking': ('reasoning_tokens', 'thinking_tokens'),
}


def normalize_usage(usage: Optional[dict]) -> dict:
    """Read a usage dict's token counts under either vendor spelling.

    Returns a dict with the five canonical integer keys — ``input``,
    ``output``, ``cache_write``, ``cache_read``, ``thinking`` — each resolved
    from the OpenAI key OR the Anthropic key (see ``_USAGE_KEY_ALIASES``),
    coerced to ``int`` with a 0 default. All-zero on a null/non-dict input.

    This is PURE key-aliasing: it replaces the ~7 copies of
    ``int(usage.get('prompt_tokens') or usage.get('input_tokens') or 0)``
    scattered across cost/paper/route code. It deliberately does NOT apply the
    cache-token convention detection — callers that need "uncached input" run
    that math themselves on ``input`` / ``cache_write`` / ``cache_read``.
    """
    if not usage or not isinstance(usage, dict):
        return {k: 0 for k in _USAGE_KEY_ALIASES}
    out = {}
    for canon, keys in _USAGE_KEY_ALIASES.items():
        val = 0
        for k in keys:
            v = usage.get(k)
            if v:
                val = v
                break
        try:
            out[canon] = int(val or 0)
        except (TypeError, ValueError) as e:
            logger.debug('[Cost] non-numeric usage value for %s (->0): %s', canon, e)
            out[canon] = 0
    return out


def _legacy_preset_to_model(model_id: str) -> str:
    """Resolve a legacy preset id (e.g. 'opus') to its canonical model_id.

    Mirrors the JS ``_LEGACY_PRESET_TO_MODEL`` mapping. If the input is
    already a canonical model_id, returned unchanged. The mapping lives
    on the JS side; we keep this as identity since the API surface
    accepts canonical ids — preset → model resolution happens client-
    side before the cost lookup runs.
    """
    return model_id or ''


def _qwen_cny(tokens: int, side: str, model_id: str) -> float:
    """Compute Qwen tiered cost in CNY for one direction.

    ``side`` is ``'input'`` or ``'output'``. Returns CNY for
    ``tokens`` charged at the tier whose threshold (max context window
    size) is the smallest one >= ``tokens``. The cheapest tier is the
    "low context" rate; fallback to the largest tier if everything is
    over-threshold.
    """
    if tokens <= 0:
        return 0.0
    table = QWEN_PRICING_CNY.get(model_id) or QWEN_PRICING_CNY.get('_default')
    if not table:
        return 0.0
    tiers = table.get(side) or []
    if not tiers:
        return 0.0
    # Pick the first tier whose threshold >= tokens; otherwise last tier.
    chosen_rate = tiers[-1][1]
    for threshold, rate in tiers:
        if tokens <= threshold:
            chosen_rate = rate
            break
    return tokens * chosen_rate / 1_000_000


def _round(value: float, places: int = 4) -> float:
    factor = 10 ** places
    return round(value * factor) / factor


def compute_cost(
    usage: Optional[dict],
    model_id: str = '',
    provider_id: Optional[str] = None,
) -> Optional[dict]:
    """Compute USD + CNY cost from a usage dict.

    Returns ``None`` when the usage is empty/null or all token counts
    are zero. Otherwise returns the same keys the JS ``calcCostCny``
    produces so callers can swap freely.
    """
    if not usage or not isinstance(usage, dict):
        return None

    model_id = _legacy_preset_to_model(model_id or '')

    _u = normalize_usage(usage)
    inp = _u['input']
    out = _u['output']
    cache_write = _u['cache_write']
    cache_read = _u['cache_read']
    think_tok = _u['thinking']

    # Some providers report thinking in `reasoning_tokens` but `out=0`
    # when the model emitted only thinking; treat thinking as output then.
    if think_tok > 0 and out == 0:
        out = think_tok

    if inp == 0 and out == 0 and cache_write == 0 and cache_read == 0:
        return None

    pricing_data = get_pricing_data()
    rate = pricing_data.get('usdToCny') or DEFAULT_USD_CNY_RATE

    # ── Qwen tiered (CNY-native) — Qwen models bill in CNY, not USD ──
    if 'qwen' in model_id.lower():
        inp_cny = _qwen_cny(inp, 'input', model_id)
        out_cny = _qwen_cny(out, 'output', model_id)
        total_cny = inp_cny + out_cny
        return {
            'costUsd': _round(total_cny / rate),
            'costCny': _round(total_cny),
            'inputTokens': inp,
            'outputTokens': out,
            'cacheWriteTokens': cache_write,
            'cacheReadTokens': cache_read,
            'thinkingTokens': think_tok,
            'inputCostCny': _round(inp_cny, 6),
            'outputCostCny': _round(out_cny, 6),
            'cacheWriteCostCny': 0.0,
            'cacheReadCostCny': 0.0,
            # USD per-component sub-costs — consumed by the billing adapter
            # (lib/billing/cost.compute_request_cost) so the wallet debit and
            # the displayed cost share ONE arithmetic core. Qwen bills in CNY,
            # so the USD figures are the CNY costs divided by the live rate.
            'inputCostUsd': _round(inp_cny / rate, 9),
            'outputCostUsd': _round(out_cny / rate, 9),
            'cacheWriteCostUsd': 0.0,
            'cacheReadCostUsd': 0.0,
            'cacheSavingsCny': 0.0,
            'cacheSavingsUsd': 0.0,
        }

    # ── Generic USD pricing — provider override beats global ──
    mp = lookup_pricing(model_id, provider_id) if model_id else None
    if mp:
        base_in = float(mp.get('input') or 0)
        out_p = float(mp.get('output') or 0)
        cw_mul = float(mp.get('cacheWriteMul', 1.25))
        cr_mul = float(mp.get('cacheReadMul', 0.10))
    else:
        base_in = float(pricing_data.get('inputPrice') or 0)
        out_p = float(pricing_data.get('outputPrice') or 0)
        cw_mul = float(pricing_data.get('cacheWriteMul') or 1.25)
        cr_mul = float(pricing_data.get('cacheReadMul') or 0.10)

    output_cost_usd = (out * out_p) / 1e6

    # ── Cache convention detection ──
    # OpenAI: prompt_tokens = total (inp >= cw + cr).
    # Anthropic: prompt_tokens = uncached residual (inp << cw + cr).
    if cache_write > 0 or cache_read > 0:
        if inp <= cache_write + cache_read:
            # Anthropic: inp IS the uncached portion.
            uncached = inp
            total_input = inp + cache_write + cache_read
        else:
            # OpenAI: inp is the total — derive uncached.
            uncached = inp - cache_write - cache_read
            total_input = inp
        input_cost_usd = (uncached * base_in) / 1e6
        cw_cost_usd = (cache_write * base_in * cw_mul) / 1e6
        cr_cost_usd = (cache_read * base_in * cr_mul) / 1e6
    else:
        uncached = inp
        total_input = inp
        input_cost_usd = (inp * base_in) / 1e6
        cw_cost_usd = 0.0
        cr_cost_usd = 0.0

    cost_usd = input_cost_usd + cw_cost_usd + cr_cost_usd + output_cost_usd
    no_cache_input_usd = (total_input * base_in) / 1e6
    cache_savings_usd = no_cache_input_usd - (
        input_cost_usd + cw_cost_usd + cr_cost_usd)

    return {
        'costUsd': _round(cost_usd),
        'costCny': _round(cost_usd * rate),
        'inputTokens': uncached,
        'outputTokens': out,
        'totalInputTokens': total_input,
        'cacheWriteTokens': cache_write,
        'cacheReadTokens': cache_read,
        'thinkingTokens': think_tok,
        'inputCostCny': _round(input_cost_usd * rate, 6),
        'outputCostCny': _round(output_cost_usd * rate, 6),
        'cacheWriteCostCny': _round(cw_cost_usd * rate, 6),
        'cacheReadCostCny': _round(cr_cost_usd * rate, 6),
        # USD per-component sub-costs — consumed by the billing adapter
        # (lib/billing/cost.compute_request_cost) so the wallet debit and the
        # displayed cost share ONE arithmetic core and can never drift.
        'inputCostUsd': _round(input_cost_usd, 9),
        'outputCostUsd': _round(output_cost_usd, 9),
        'cacheWriteCostUsd': _round(cw_cost_usd, 9),
        'cacheReadCostUsd': _round(cr_cost_usd, 9),
        'cacheSavingsCny': _round(
            cache_savings_usd * rate if cache_savings_usd > 0 else 0, 6),
        'cacheSavingsUsd': _round(
            cache_savings_usd if cache_savings_usd > 0 else 0, 6),
    }


__all__ = ['compute_cost', 'normalize_usage']
