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
#
# Vendor cache-read spellings beyond the two canonical ones (probed live):
#   cached_tokens                — Moonshot/Kimi top-level (also what the
#                                  sankuai gateway emits for kimi-k3; it
#                                  reports the auto-cache hit HERE while
#                                  pinning cache_read_tokens=0 — probed
#                                  2026-07-24, 3328/3367 tok = 98.8% hit)
#   effectiveCachedTokens        — sankuai gateway camelCase variant
#   prompt_cache_hit_tokens      — DeepSeek direct API
#   cached_content_token_count   — Gemini (usageMetadata shape)
# First TRUTHY value wins, so an explicit 0 on a canonical key (the gateway's
# pinned cache_read_tokens=0) never shadows the real hit on a vendor key.
_USAGE_KEY_ALIASES = {
    'input': ('prompt_tokens', 'input_tokens'),
    'output': ('completion_tokens', 'output_tokens'),
    'cache_write': ('cache_write_tokens', 'cache_creation_input_tokens'),
    'cache_read': ('cache_read_tokens', 'cache_read_input_tokens',
                   'cached_tokens', 'effectiveCachedTokens',
                   'prompt_cache_hit_tokens', 'cached_content_token_count'),
    'thinking': ('reasoning_tokens', 'thinking_tokens'),
}

# NESTED vendor spellings — ``(parent_key, child_key)`` pairs consulted ONLY
# when every flat alias above resolved to 0 (first truthy wins):
#   prompt_tokens_details.cached_tokens        — the OpenAI-standard spelling
#                                                (o-series, Azure, Moonshot
#                                                direct, OpenRouter)
#   completion_tokens_details.reasoning_tokens — o-series / kimi thinking
_USAGE_NESTED_ALIASES = {
    'cache_read': (('prompt_tokens_details', 'cached_tokens'),),
    'thinking': (('completion_tokens_details', 'reasoning_tokens'),),
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
    # Nested vendor spellings — only when every flat alias resolved to 0.
    for canon, paths in _USAGE_NESTED_ALIASES.items():
        if out.get(canon):
            continue
        for parent_key, child_key in paths:
            parent = usage.get(parent_key)
            if not isinstance(parent, dict):
                continue
            v = parent.get(child_key)
            if not v:
                continue
            try:
                out[canon] = int(v)
            except (TypeError, ValueError) as e:
                logger.debug('[Cost] non-numeric nested usage value for %s (->0): %s',
                             canon, e)
            break
    return out


def canonicalize_usage_cache_keys(usage: Optional[dict]) -> Optional[dict]:
    """Stamp the canonical ``cache_read_tokens`` / ``cache_write_tokens`` keys
    onto a raw usage dict, filled from vendor-specific spellings, IN PLACE.

    Called once at each ingestion point (SSE stream finalize + non-stream
    ``chat()``) so every downstream raw-dict consumer — the SSE ``usage``
    payload, ``api_rounds``, persisted task metadata, and the frontend cost
    popover / context gauge (which read ``cache_read_tokens ||
    cache_read_input_tokens`` verbatim) — sees cache hits regardless of which
    spelling the provider/gateway used. Compute paths should prefer
    :func:`normalize_usage` (read-only, no mutation); this helper exists for
    dicts that are FORWARDED raw to other consumers.

    No-op when the canonical keys already carry a nonzero value (Anthropic /
    already-canonical OpenAI traffic is untouched). Returns the same dict.
    """
    if not isinstance(usage, dict):
        return usage
    _u = normalize_usage(usage)
    if _u['cache_read'] and not (usage.get('cache_read_tokens')
                                 or usage.get('cache_read_input_tokens')):
        usage['cache_read_tokens'] = _u['cache_read']
    if _u['cache_write'] and not (usage.get('cache_write_tokens')
                                  or usage.get('cache_creation_input_tokens')):
        usage['cache_write_tokens'] = _u['cache_write']
    return usage


# ── Cache-token CONVENTION detection (structural, not magnitude-based) ──

# Keys that only ever appear on an OpenAI-compat payload. Their defining
# property is that ``prompt_tokens`` is the TOTAL prompt — the cached tokens
# are ALREADY INCLUDED in it.
_OPENAI_TOTAL_KEYS = ('prompt_tokens', 'completion_tokens')
# Keys that only ever appear on an Anthropic-native payload, where
# ``input_tokens`` is the UNCACHED RESIDUAL and the cache counts sit beside it.
_ANTHROPIC_RESIDUAL_KEYS = ('cache_read_input_tokens',
                            'cache_creation_input_tokens')


def usage_cache_convention(usage: Optional[dict]) -> str:
    """Return ``'openai'`` or ``'anthropic'`` — how to read ``usage``'s input.

    The two vendors disagree on what the prompt-token field MEANS:

      * OpenAI-compat — ``prompt_tokens`` is the TOTAL; cached tokens are a
        SUBSET of it (reported in ``cached_tokens`` /
        ``prompt_tokens_details.cached_tokens``).
      * Anthropic-native — ``input_tokens`` is only the UNCACHED residual;
        the cached tokens are counted SEPARATELY and must be added on.

    This decision used to be made by comparing magnitudes
    (``input <= cache_write + cache_read`` ⇒ Anthropic). That is a latent
    10x BILLING BUG: a round that reads its ENTIRE prompt back from cache has
    ``cached == prompt_tokens``, satisfies ``<=``, and is misclassified as
    Anthropic — so the cache is added ON TOP of a total that already contained
    it and the whole prefix is re-priced at the full uncached rate. Live data
    reached a margin of ONE token (prompt=428603, cached=428602) before this
    was found, and a fully-cached 82843-token round mis-prices 10.4x.

    So we decide STRUCTURALLY, on which key spellings the payload carries —
    a property of the wire format, not of the numbers:

      1. An Anthropic-only cache key present  → ``'anthropic'``.
      2. ``prompt_tokens`` present (the OpenAI total field) → ``'openai'``.
      3. ``input_tokens`` present without OpenAI keys → ``'anthropic'``.
      4. Nothing conclusive → ``'openai'`` (cache-inclusive) — the SAFE
         default: it never double-counts, so it can only under-report a total,
         never over-charge.
    """
    if not isinstance(usage, dict):
        return 'openai'
    if any(usage.get(k) is not None for k in _ANTHROPIC_RESIDUAL_KEYS):
        return 'anthropic'
    if usage.get('prompt_tokens') is not None:
        # ── Arithmetic-impossibility guard (NOT a magnitude heuristic) ──
        # Some providers emit HYBRID payloads: the OpenAI ``prompt_tokens``
        # spelling carrying Anthropic RESIDUAL semantics (e.g.
        # prompt_tokens=100 with cache_read=15000). Under the OpenAI
        # convention the cache is a SUBSET of prompt_tokens, so
        # ``cache > prompt_tokens`` is physically impossible — when we see it,
        # prompt_tokens cannot be the total and must be the residual.
        #
        # This uses STRICT ``>``, so the equality case (cached ==
        # prompt_tokens, a round that read its whole prompt back) still reads
        # as OpenAI. That equality IS the 10x mis-pricing cliff this function
        # exists to fix, so the guard must never swallow it.
        _cached = int(usage.get('cache_read_tokens') or 0) or _nested_cached(usage)
        _written = int(usage.get('cache_write_tokens') or 0)
        _prompt = int(usage.get('prompt_tokens') or 0)
        if _cached + _written > _prompt:
            return 'anthropic'
        return 'openai'
    if usage.get('input_tokens') is not None:
        return 'anthropic'
    return 'openai'


def _nested_cached(usage: dict) -> int:
    """Cached-token count from any vendor spelling (flat or nested)."""
    for k in ('cached_tokens', 'effectiveCachedTokens',
              'prompt_cache_hit_tokens', 'cached_content_token_count'):
        v = usage.get(k)
        if v:
            try:
                return int(v)
            except (TypeError, ValueError) as e:
                # A vendor cache-spelling that won't int() falls through to the
                # next spelling and ultimately 0 — undercounting the cache hit
                # and overstating the price. Surface it (mirrors the debug
                # logging normalize_usage already does for the flat aliases).
                logger.debug('[Cost] non-numeric cached value for %s (->skip): %s', k, e)
    details = usage.get('prompt_tokens_details')
    if isinstance(details, dict):
        try:
            return int(details.get('cached_tokens') or 0)
        except (TypeError, ValueError) as e:
            logger.debug('[Cost] non-numeric nested cached_tokens (->0): %s', e)
    return 0


def _anthropic_residual_input(usage: Optional[dict], normalized_input: int) -> int:
    """Uncached RESIDUAL input for a payload already judged ``'anthropic'``.

    ★ Exists because :func:`normalize_usage` and :func:`usage_cache_convention`
    can disagree about WHICH KEY carries the input on a HYBRID payload — one
    that spells BOTH conventions at once. ``normalize_usage`` documents the
    assumption that a payload "carries ONE convention only (OpenAI keys XOR
    Anthropic keys)", under which its alias order is explicitly "immaterial".
    The sankuai_anthropic gateway violates that XOR: it emits ``prompt_tokens``
    (the cache-INCLUSIVE total) *beside* ``cache_*_input_tokens`` (residual
    semantics). The alias order then resolves ``input`` to the TOTAL while the
    structural detector says ``'anthropic'`` — so the whole prefix was re-priced
    at the uncached rate and ``totalInputTokens`` double-counted the cache.

    Measured on conversation ms5i5ydigs9j9w (28/28 rounds satisfied
    ``input_tokens + cache_read + cache_write == prompt_tokens``, i.e.
    prompt_tokens really is the inclusive total): true uncached input was
    162,854 tokens but 5,562,791 were billed — ¥201.37 instead of ¥5.90 on the
    input component alone. Fleet-wide: 2,740 affected rounds across 27
    conversations, displayed cost overstated by 72%.

    So when the convention is Anthropic, the residual MUST come from the
    Anthropic-native key. ``prompt_tokens`` is only accepted as a fallback for
    the pure-hybrid case where the native key is absent entirely (the
    ``prompt_tokens``-carries-residual-semantics shape pinned by
    ``test_hybrid_payload_with_impossible_cache_reads_as_residual``).
    """
    if not isinstance(usage, dict):
        return normalized_input
    native = usage.get('input_tokens')
    if native is not None:
        try:
            return int(native or 0)
        except (TypeError, ValueError) as e:
            logger.debug('[Cost] non-numeric input_tokens (->normalized): %s', e)
    return normalized_input


def split_input_tokens(usage: Optional[dict]) -> tuple[int, int]:
    """Return ``(uncached_input_tokens, total_input_tokens)`` for ``usage``.

    THE single place the cache-token convention is applied. Both the cost
    engine and the cache-hit-rate telemetry consume this, so a hit-rate figure
    and the price charged for the same round can never disagree about what the
    prompt size was.

    ``uncached`` is what gets billed at the base input rate; ``total`` is the
    full prompt the provider processed (the correct denominator for a cache
    hit ratio).
    """
    _u = normalize_usage(usage)
    inp, cw, cr = _u['input'], _u['cache_write'], _u['cache_read']
    if not (cw or cr):
        return inp, inp
    if usage_cache_convention(usage) == 'anthropic':
        # input_tokens is the uncached residual; cache sits beside it.
        return _anthropic_residual_input(usage, inp), \
            _anthropic_residual_input(usage, inp) + cw + cr
    # OpenAI-compat: prompt_tokens is the total; cache is a subset of it.
    return max(0, inp - cw - cr), inp


def synthesize_usage(*, input_tokens: int = 0, output_tokens: int = 0,
                     cache_read_tokens: int = 0, cache_write_tokens: int = 0,
                     reasoning_tokens: int = 0) -> dict:
    """Build a usage dict from SCALARS, spelled so its convention survives.

    Callers that hold loose token counts rather than a provider payload (the
    billing adapter, pre-flight estimators) must still hand ``compute_cost``
    something it can read correctly. Re-encoding scalars under the WRONG
    spelling silently changes their meaning: writing an OpenAI TOTAL into
    ``input_tokens`` makes the engine treat it as an uncached RESIDUAL and add
    the cache on top — a real over-charge (a gpt-4o round of 10000 total /
    6000 cached bills 10000 uncached instead of 4000).

    Scalars carry no key-spelling signal, so the convention is inferred by the
    one rule that is arithmetic rather than heuristic: under the OpenAI
    convention the cache is a SUBSET of the input, so ``cache > input`` is
    impossible and proves the input is a residual. Strict ``>`` keeps the
    equality case (a round that read its ENTIRE prompt back) on the
    cache-inclusive reading — that equality is precisely the 10x mis-pricing
    cliff, so it must not be swallowed here either.
    """
    inp = int(input_tokens or 0)
    cr = int(cache_read_tokens or 0)
    cw = int(cache_write_tokens or 0)
    base = {'reasoning_tokens': int(reasoning_tokens or 0)}
    if cr + cw > inp:
        # Residual semantics — Anthropic spelling.
        base.update({
            'input_tokens': inp,
            'output_tokens': int(output_tokens or 0),
            'cache_read_input_tokens': cr,
            'cache_creation_input_tokens': cw,
        })
    else:
        # Cache-inclusive total — OpenAI spelling.
        base.update({
            'prompt_tokens': inp,
            'completion_tokens': int(output_tokens or 0),
            'cache_read_tokens': cr,
            'cache_write_tokens': cw,
        })
    return base


def merge_usage_totals(base: Optional[dict], extra: Optional[dict]) -> dict:
    """Add ``extra``'s token counts onto ``base``, returning a NEW dict.

    The one place "two billed requests become one line on the bill" is
    expressed. Used wherever a request that the gateway charged for has to be
    folded into a total it is not naturally part of:

      * a whole-turn auto-retry's discarded attempt
        (``orchestrator/_finalize._maybe_auto_retry_turn``);
      * the per-turn memory-prefetch rerank, including the call the deadline
        ABANDONED — abandoning stops us waiting, not the gateway billing
        (``orchestrator/_memory_prefetch.make_prefetch_usage_sink``).

    Only genuinely numeric fields are summed. Usage dicts also carry
    ``trace_id`` (str), ``_dispatch`` (dict) and assorted nested detail
    objects; adding those is meaningless, so they are skipped rather than
    crashing the caller. ``bool`` is excluded explicitly — it is an ``int``
    subclass in Python and ``stream=True`` must not become ``2``.

    Neither input is mutated.
    """
    out = dict(base or {})
    for k, v in (extra or {}).items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        prev = out.get(k)
        out[k] = (prev + v) if isinstance(prev, (int, float)) and not isinstance(prev, bool) else v
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
    peak_mul = None
    if mp:
        base_in = float(mp.get('input') or 0)
        out_p = float(mp.get('output') or 0)
        cw_mul = float(mp.get('cacheWriteMul', 1.25))
        cr_mul = float(mp.get('cacheReadMul', 0.10))
        peak_mul = mp.get('peakMul')  # stamped by lookup_pricing at peak hours
    else:
        base_in = float(pricing_data.get('inputPrice') or 0)
        out_p = float(pricing_data.get('outputPrice') or 0)
        cw_mul = float(pricing_data.get('cacheWriteMul') or 1.25)
        cr_mul = float(pricing_data.get('cacheReadMul') or 0.10)

    output_cost_usd = (out * out_p) / 1e6

    # ── Cache convention detection ──
    # Delegated to split_input_tokens (the SINGLE source of this decision, also
    # used by the cache-hit telemetry) so a reported hit-rate and the price
    # charged for the same round can never disagree. It keys on the payload's
    # KEY SPELLINGS, not on token magnitudes — see usage_cache_convention for
    # the 10x mis-pricing that the old ``inp <= cw + cr`` comparison caused
    # when a round read its entire prompt back from cache.
    if cache_write > 0 or cache_read > 0:
        uncached, total_input = split_input_tokens(usage)
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
        # Present ONLY when the resolved pricing was peak-scaled (all four
        # components already carry the multiplier) — transparency for
        # "why did this round cost 2x" without re-deriving the schedule.
        **({'peakMultiplier': peak_mul} if peak_mul else {}),
    }


__all__ = ['compute_cost', 'normalize_usage', 'canonicalize_usage_cache_keys',
           'split_input_tokens', 'usage_cache_convention', 'synthesize_usage',
           'merge_usage_totals']
