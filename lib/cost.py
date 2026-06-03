"""lib/cost.py — Per-message cost calculation.

Pure-function port of ``static/js/core.js:calcCostCny``. Encapsulates:

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

    inp = int(usage.get('prompt_tokens') or usage.get('input_tokens') or 0)
    out = int(usage.get('completion_tokens') or usage.get('output_tokens') or 0)
    cache_write = int(usage.get('cache_write_tokens')
                       or usage.get('cache_creation_input_tokens') or 0)
    cache_read = int(usage.get('cache_read_tokens')
                      or usage.get('cache_read_input_tokens') or 0)
    think_tok = int(usage.get('reasoning_tokens')
                     or usage.get('thinking_tokens') or 0)

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
        'cacheSavingsCny': _round(
            cache_savings_usd * rate if cache_savings_usd > 0 else 0, 6),
        'cacheSavingsUsd': _round(
            cache_savings_usd if cache_savings_usd > 0 else 0, 6),
    }


__all__ = ['compute_cost']
