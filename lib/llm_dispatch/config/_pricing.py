"""lib/llm_dispatch/config/_pricing.py — Pricing-tier tables & resolution.

The ``PRICING_TIERS`` table defines named price brackets (currently just
``'cheap'``).  A model earns a tier tag when its input AND output prices
fall strictly below the tier's input/output thresholds.  The tags in
``MANAGED_TIER_TAGS`` are **fully owned by this module** — any function
that calls :func:`reevaluate_pricing_tags` will add missing tags and
remove stale ones based on live pricing data.

Adding a new tier is a one-line change: append a row to ``PRICING_TIERS``
and every code path re-evaluates automatically.
"""

from lib.log import get_logger

logger = get_logger(__name__)

# ══════════════════════════════════════════════════════════════
#  Pricing tier table — single source of truth
# ══════════════════════════════════════════════════════════════
# Each row: (tag, input_max_per_1m, output_max_per_1m).
# A model earns the tag when its input price < input_max AND its output
# price < output_max (both strict).  When only a blended $/1K cost is
# available, we compare blended_1m <= (input_max + output_max) / 2
# (assumes symmetric pricing — matches the legacy CHEAP_BLENDED_THRESHOLD
# of 9.0 which is (3.0 + 15.0) / 2).
#
# Reference for 'cheap' bracket: Claude Sonnet 4.6 — input $3/1M,
# output $15/1M.  A model strictly cheaper on both axes is "cheap".
PRICING_TIERS: list[tuple[str, float, float]] = [
    ('cheap', 3.0, 15.0),
    # Future tiers go here, e.g.:
    # ('ultra_cheap', 0.5, 2.0),
    # ('mid',         10.0, 40.0),
]

# Tier tags whose presence/absence is managed by reevaluate_pricing_tags.
# Never put operational capability tags (text / vision / thinking / …) here.
MANAGED_TIER_TAGS: frozenset[str] = frozenset(tag for tag, *_ in PRICING_TIERS)

# Legacy constants (kept for backward compat with external imports).
CHEAP_INPUT_THRESHOLD = 3.0
CHEAP_OUTPUT_THRESHOLD = 15.0
CHEAP_BLENDED_THRESHOLD = 9.0

# Caps that indicate a non-chat model — pricing tier tags never apply.
# Must list EVERY non-chat cap: a stray managed tag (e.g. 'cheap') on a non-chat
# slot makes {transcription, cheap} no longer a subset of the dispatcher's
# _NON_CHAT_CAPS, so the slot leaks into the chat picker (and 404s). Keep this in
# sync with LLMDispatcher._NON_CHAT_CAPS and the debug/reeval_pricing_tags.py
# skip-sets.
_NON_CHAT_CAPS = frozenset({'image_gen', 'embedding', 'transcription', 'audio_chat'})


def _resolve_prices(model_id: str,
                    input_price: float = None,
                    output_price: float = None) -> tuple[float | None, float | None]:
    """Return (input, output) per-1M USD for *model_id*.

    Explicit args win over the MODEL_PRICING table lookup.  Returns
    ``(None, None)`` when neither source yields both values.
    """
    inp, out = input_price, output_price
    if inp is None or out is None:
        try:
            from lib import MODEL_PRICING
        except Exception as e:
            logger.debug('[PricingTiers] MODEL_PRICING unavailable: %s', e)
            return inp, out
        pricing = MODEL_PRICING.get(model_id)
        if pricing:
            if inp is None:
                inp = pricing.get('input')
            if out is None:
                out = pricing.get('output')
    return inp, out


def _tier_matches(tier: tuple[str, float, float],
                  inp: float | None,
                  out: float | None,
                  fallback_cost_per_1k: float | None) -> bool:
    """Return True if the given prices place the model inside *tier*."""
    _tag, in_max, out_max = tier
    if inp is not None and out is not None:
        return inp < in_max and out < out_max
    if fallback_cost_per_1k is not None and fallback_cost_per_1k > 0:
        blended_1m = fallback_cost_per_1k * 1000.0
        # Symmetric-pricing fallback: midpoint threshold.
        return blended_1m <= (in_max + out_max) / 2.0
    return False


def get_pricing_tiers(model_id: str,
                      fallback_cost_per_1k: float = None,
                      input_price: float = None,
                      output_price: float = None) -> set[str]:
    """Return the set of tier tags that apply to *model_id*.

    Returns the empty set for models with no pricing data, or when the
    model is strictly more expensive than every tier's thresholds.
    Callers MUST skip non-chat models (image_gen / embedding) themselves
    — this function does not know a model's other capabilities.
    """
    inp, out = _resolve_prices(model_id, input_price, output_price)
    tags: set[str] = set()
    for tier in PRICING_TIERS:
        if _tier_matches(tier, inp, out, fallback_cost_per_1k):
            tags.add(tier[0])
    return tags


def is_model_cheap(model_id: str, fallback_cost_per_1k: float = None,
                   input_price: float = None, output_price: float = None) -> bool:
    """Backward-compat shim: True iff 'cheap' ∈ :func:`get_pricing_tiers`.

    Prefer :func:`get_pricing_tiers` for new code — it naturally extends
    when more tiers are added to ``PRICING_TIERS``.
    """
    return 'cheap' in get_pricing_tiers(
        model_id,
        fallback_cost_per_1k=fallback_cost_per_1k,
        input_price=input_price,
        output_price=output_price,
    )


def reevaluate_pricing_tags(models: list[dict], *, log_prefix: str = '') -> dict:
    """Re-evaluate all managed pricing-tier tags on *models* in place.

    For each model dict, computes the desired tier-tag set from live
    pricing data (``input_price`` / ``output_price`` → ``MODEL_PRICING``
    → ``cost`` blended fallback) and rewrites the model's
    ``capabilities`` list so every tag in :data:`MANAGED_TIER_TAGS`
    matches the desired set.  Non-tier capabilities (text / vision /
    thinking / image_gen / embedding / …) are left untouched.

    Skips non-chat models (image_gen / embedding in caps) entirely —
    their caps lists stay as-is.

    Args:
        models: List of model dicts with at least ``model_id`` and
            ``capabilities`` keys.  May also carry ``input_price``,
            ``output_price``, ``cost``.  Mutated in place.
        log_prefix: Optional prefix for log messages (e.g. a provider
            id) — shown as ``[PricingTags] <prefix> …`` in logs.

    Returns:
        ``{'added': {tag: n, …}, 'removed': {tag: n, …}, 'changed': n,
          'total': n}`` — per-tag counts of models that gained / lost
        each managed tag, plus totals.
    """
    added: dict[str, int] = {t: 0 for t in MANAGED_TIER_TAGS}
    removed: dict[str, int] = {t: 0 for t in MANAGED_TIER_TAGS}
    changed = 0

    for m in models:
        mid = m.get('model_id', '')
        if not mid:
            continue
        caps_raw = m.get('capabilities') or []
        caps = set(caps_raw) if isinstance(caps_raw, (list, set, tuple)) else {'text'}

        # Skip non-chat models — tier tags don't apply.
        if caps & _NON_CHAT_CAPS:
            continue

        desired = get_pricing_tiers(
            mid,
            fallback_cost_per_1k=m.get('cost'),
            input_price=m.get('input_price'),
            output_price=m.get('output_price'),
        )

        current_tier_tags = caps & MANAGED_TIER_TAGS
        if current_tier_tags == desired:
            continue

        # Apply diff: strip any managed tag not desired, add any desired tag missing.
        for tag in MANAGED_TIER_TAGS - desired:
            if tag in caps:
                caps.discard(tag)
                removed[tag] += 1
        for tag in desired - current_tier_tags:
            caps.add(tag)
            added[tag] += 1

        m['capabilities'] = sorted(caps)
        changed += 1

    if changed:
        parts = []
        for tag in sorted(MANAGED_TIER_TAGS):
            a, r = added.get(tag, 0), removed.get(tag, 0)
            if a or r:
                parts.append('%s +%d/-%d' % (tag, a, r))
        summary = ', '.join(parts) or 'no net change'
        prefix = ('%s ' % log_prefix) if log_prefix else ''
        logger.info('[PricingTags] %sre-evaluated %d/%d models: %s',
                    prefix, changed, len(models), summary)

    return {
        'added': added,
        'removed': removed,
        'changed': changed,
        'total': len(models),
    }
