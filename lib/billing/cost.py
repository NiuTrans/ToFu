"""Convert canonical dynamic request prices to integer wallet charges.

compute_request_cost delegates rates, cache conventions and currency conversion
to lib.cost.compute_cost, converts each USD component, and applies the relay
margin from billing.pricing. Estimates and display helpers share the existing
micro-credit unit. This module performs no database or wallet mutation.

Frozen native fees belong to native_cost and must never enter dynamic pricing.
cost_types owns the shared CostBreakdown result, re-exported here for callers.
"""

from __future__ import annotations

from lib.billing.cost_types import CostBreakdown

from lib.billing.pricing import get_default_margin
from lib.cost import compute_cost

# Micro-credits per US dollar. EMPIRICALLY PINNED so existing wallet balances
# and the legacy pricing.json scale are preserved: the old table priced
# gpt-4o-mini input at 150_000 micro/Mtok for a real $0.15/Mtok list price →
# 1_000_000 micro per dollar. Verified by tests/test_billing.test_simple_compute
# (gpt-4o-mini 1000in+500out, margin 0 → exactly 450 µ). Do NOT change without
# migrating every stored balance.
MICRO_PER_USD = 1_000_000

# Back-compat alias (1 credit = 1e6 µ); kept for any external importer.
MICRO_PER_CREDIT = 1_000_000


def _apply_margin(base_micro: int, margin: float) -> int:
    return int(base_micro * margin)


def _usd_to_micro(usd: float) -> int:
    """Convert a USD sub-cost to integer micro-credits.

    Rounds at the final step only. The caller MUST pass a per-component USD
    figure (``inputCostUsd`` etc., 9-dp precise) — NOT the top-level
    ``costUsd`` (rounded to 4 dp, which would lose sub-cent components, e.g.
    $0.00045 → $0.0004 → 400µ instead of 450µ).
    """
    return int(round((usd or 0.0) * MICRO_PER_USD))


def compute_request_cost(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    reasoning_tokens: int = 0,
    provider_id: str | None = None,
    margin: float = -1.0,
    raw_usage: dict | None = None,
) -> CostBreakdown:
    """Compute the cost of a single completed LLM request, in micro-credits.

    Delegates rate math to :func:`lib.cost.compute_cost` (the single cost
    engine) so the debited amount and the displayed ¥/$ are computed by the
    SAME code over the SAME price table — they can never drift. Cache-read and
    cache-write tokens ARE billed (the engine knows each model's cache
    multipliers and detects the Anthropic-vs-OpenAI cache-token convention).

    Args:
        model: Model name; priced via the rich ``lib/pricing.py`` table.
        input_tokens: Prompt tokens (uncached portion for Anthropic, total for
            OpenAI — the engine auto-detects and never double-charges cache).
        output_tokens: Completion tokens.
        cache_read_tokens: Cache-hit tokens (billed at the model's read mul).
        cache_write_tokens: Cache-creation tokens (billed at the write mul).
        reasoning_tokens: Thinking tokens; billed as output when the model
            reports no separate completion tokens.
        provider_id: Optional provider for provider-scoped price overrides.
        margin: Override the relay margin. -1 = use pricing.json default;
            0.0 = no margin.

    Returns:
        :class:`CostBreakdown` with the integer micro-credit total.
    """
    if margin < 0:
        margin = get_default_margin()

    # Spell the scalars so the cost engine reads them under the RIGHT
    #   convention. Hardcoding the Anthropic key names here used to force an
    #   OpenAI TOTAL to be read as an uncached RESIDUAL, so the cache was
    #   added on top of a figure that already contained it and the wallet
    #   over-charged (gpt-4o 10000 total / 6000 cached billed 10000 uncached
    #   instead of 4000 — 52500µ vs the displayed 37500µ). synthesize_usage
    #   picks the spelling that preserves the scalars' meaning.
    from lib.cost import synthesize_usage
    usage = dict(raw_usage) if isinstance(raw_usage, dict) else synthesize_usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=reasoning_tokens,
    )
    cc = compute_cost(usage, model_id=model or '', provider_id=provider_id)

    if not cc:
        components = {'input': 0, 'output': 0, 'cache_read': 0, 'cache_write': 0}
    else:
        components = {
            'input':       _usd_to_micro(cc.get('inputCostUsd')),
            'output':      _usd_to_micro(cc.get('outputCostUsd')),
            'cache_read':  _usd_to_micro(cc.get('cacheReadCostUsd')),
            'cache_write': _usd_to_micro(cc.get('cacheWriteCostUsd')),
        }

    base = sum(components.values())
    margin_micro = _apply_margin(base, margin)
    return CostBreakdown(
        micro=base + margin_micro,
        base_micro=base,
        margin_micro=margin_micro,
        matched_model=model or '',
        components=components,
        snapshot={
            **dict((cc or {}).get('pricingSnapshot') or {}),
            'componentsMicro': dict(components),
            'baseMicro': base, 'marginMicro': margin_micro,
            'totalMicro': base + margin_micro,
        },
    )


def estimate_request_cost(
    model: str,
    *,
    prompt_tokens: int,
    max_completion_tokens: int = 1024,
    headroom: float = 1.5,
) -> int:
    """Pessimistic estimate for pre-flight reservation.

    The relay reserves ``estimate_request_cost(...)`` micro-credits BEFORE
    sending the upstream request; the gap between estimate and actual is
    refunded in :func:`lib.billing.wallet.settle`. ``headroom`` widens the
    estimate so a request whose completion is longer than expected still
    succeeds — at the cost of temporarily holding more of the user's
    balance during the call.

    Args:
        model: Model name.
        prompt_tokens: Counted prompt tokens (relay should compute these
                       cheaply via the local tokenizer before dispatch).
        max_completion_tokens: Caller's ``max_tokens`` cap, or a sane
                               default.
        headroom: Multiplier applied to the WHOLE estimate (1.5 = 50%
                  cushion). Set to 1.0 for "exact" reservation.
    """
    breakdown = compute_request_cost(
        model,
        input_tokens=prompt_tokens,
        output_tokens=max_completion_tokens,
    )
    return int(breakdown.micro * headroom)


# ── Conversion helpers (display only) ─────────────────────────────────

def micro_to_credits(micro: int) -> float:
    """Convert micro-credits → human-readable credit float."""
    return micro / MICRO_PER_CREDIT


def credits_to_micro(credits: float) -> int:
    """Convert credit float → micro-credits (rounded to integer)."""
    return int(round(credits * MICRO_PER_CREDIT))


def format_credits(micro: int, *, precision: int = 4) -> str:
    """Pretty-print a micro-credit amount as a credit string."""
    return f'{micro / MICRO_PER_CREDIT:.{precision}f}'


__all__ = [
    'CostBreakdown',
    'MICRO_PER_CREDIT',
    'compute_request_cost', 'estimate_request_cost',
    'micro_to_credits', 'credits_to_micro', 'format_credits',
]
