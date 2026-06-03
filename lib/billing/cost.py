"""lib.billing.cost — Pure tokens-to-credits arithmetic.

No I/O, no DB. Inputs are token counts + a :class:`ModelPrice`; output
is an integer micro-credit amount including the configured margin.
"""

from __future__ import annotations

from dataclasses import dataclass

from lib.billing.pricing import ModelPrice, get_default_margin, get_price

# 1 credit = 1,000,000 micro-credits. 1 dollar ≈ 1000 credits at the
# canonical conversion. Both values are quoted in pricing.json's
# documentation but never used at runtime — math here is pure µ.
MICRO_PER_CREDIT = 1_000_000


@dataclass(frozen=True)
class CostBreakdown:
    """Result of :func:`compute_request_cost`."""
    micro: int                  # total micro-credits to debit
    base_micro: int             # before margin
    margin_micro: int           # added on top of base
    matched_model: str          # which row in pricing.json was used
    components: dict            # {'input': µ, 'output': µ, 'cache_read': µ, ...}


def _apply_margin(base_micro: int, margin: float) -> int:
    return int(base_micro * margin)


def compute_request_cost(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    margin: float = -1.0,
) -> CostBreakdown:
    """Compute the cost of a single completed LLM request.

    Args:
        model: Model name; resolved via :func:`lib.billing.pricing.get_price`.
        input_tokens: Prompt tokens NOT counted as cache reads.
        output_tokens: Completion tokens.
        cache_read_tokens: Anthropic cache hit tokens (charged ~10% input).
        cache_write_tokens: Anthropic cache write tokens (charged ~125% input).
        margin: Override the default margin from pricing.json. -1 means
                "use default". 0.0 explicitly disables margin.

    Returns:
        :class:`CostBreakdown` with the integer micro-credit total.
    """
    price: ModelPrice = get_price(model)
    if margin < 0:
        margin = get_default_margin()
    components = {
        'input':       (input_tokens       * price.input_per_mtok_micro)       // 1_000_000,
        'output':      (output_tokens      * price.output_per_mtok_micro)      // 1_000_000,
        'cache_read':  (cache_read_tokens  * price.cache_read_per_mtok_micro)  // 1_000_000,
        'cache_write': (cache_write_tokens * price.cache_write_per_mtok_micro) // 1_000_000,
    }
    base = sum(components.values())
    margin_micro = _apply_margin(base, margin)
    return CostBreakdown(
        micro=base + margin_micro,
        base_micro=base,
        margin_micro=margin_micro,
        matched_model=price.matched,
        components=components,
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
