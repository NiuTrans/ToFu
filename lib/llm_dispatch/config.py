"""lib/llm_dispatch/config.py — Default slot configurations and model aliases.

Contains the static configuration tables that seed the slot pool before
benchmark data is loaded.  These are **reference tables** — they describe
known model capabilities / RPM / cost metadata so that *any* configured
model benefits from pre-seeded data.  They do NOT control which models
are "active" — that is driven entirely by the Settings UI providers
(server_config.json) or legacy env-var config.

Pricing-tier tagging
====================
The ``PRICING_TIERS`` table defines named price brackets (currently just
``'cheap'``).  A model earns a tier tag when its input AND output prices
fall strictly below the tier's input/output thresholds.  The tags in
``MANAGED_TIER_TAGS`` are **fully owned by this module** — any function
that calls :func:`reevaluate_pricing_tags` will add missing tags and
remove stale ones based on live pricing data.

Adding a new tier is a one-line change: append a row to
``PRICING_TIERS`` and every code path (/api/server-config,
/api/provider-templates, /api/discover-models, /api/probe-provider, the
debug/reeval_pricing_tags.py static rewriter) re-evaluates automatically.
"""

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'DEFAULT_SLOT_CONFIGS',
    'MODEL_ALIASES',
    'MODEL_ALIAS_GROUPS',
    'PRICING_TIERS',
    'MANAGED_TIER_TAGS',
    'CHEAP_INPUT_THRESHOLD',
    'CHEAP_OUTPUT_THRESHOLD',
    'CHEAP_BLENDED_THRESHOLD',
    'is_model_cheap',
    'get_pricing_tiers',
    'reevaluate_pricing_tags',
]

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
_NON_CHAT_CAPS = frozenset({'image_gen', 'embedding'})


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


# ══════════════════════════════════════════════════════════════
#  Default slot configs: model → {caps, rpm, latency, cost}
#  Comprehensive reference table — any model that might be configured
#  via the Settings UI benefits from having a pre-seeded entry here.
#  These are overridden by benchmark data at runtime.
# ══════════════════════════════════════════════════════════════
DEFAULT_SLOT_CONFIGS = {
    # ── Claude (Anthropic — current gen: 4.8 flagship, May 2026) ──
    'claude-opus-4-8':               {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 5000, 'cost': 0.015},
    # ── Claude (Anthropic — 4.7 family, Apr 2026) ──
    'claude-opus-4-7':               {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 5000, 'cost': 0.015},
    # ── Claude (Anthropic — 4.6 family, Feb 2026) ──
    'claude-opus-4-6':               {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 5000, 'cost': 0.015},
    'claude-sonnet-4-6':             {'caps': {'text', 'vision', 'thinking'},      'rpm': 50,  'latency': 2000, 'cost': 0.015},
    'claude-haiku-4-5':              {'caps': {'text', 'vision', 'cheap'},         'rpm': 100, 'latency': 1500, 'cost': 0.005},
    'claude-haiku-4-5-20251001':     {'caps': {'text', 'vision', 'cheap'},         'rpm': 100, 'latency': 1500, 'cost': 0.005},
    # ── Claude (Anthropic — 4.5 family) ──
    'claude-opus-4-5':               {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 5000, 'cost': 0.025},
    'claude-sonnet-4-5':             {'caps': {'text', 'vision', 'thinking'},      'rpm': 50,  'latency': 2000, 'cost': 0.015},
    # ── Claude (Anthropic — legacy: 4.0 and earlier) ──
    'claude-opus-4-20250514':        {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 5000, 'cost': 0.045},
    'claude-sonnet-4-20250514':      {'caps': {'text', 'vision', 'thinking'},      'rpm': 50,  'latency': 2000, 'cost': 0.009},
    'claude-3-5-sonnet-20241022':    {'caps': {'text', 'vision'},                  'rpm': 50,  'latency': 2000, 'cost': 0.009},
    'claude-3-opus-20240229':        {'caps': {'text', 'vision'},                  'rpm': 30,  'latency': 5000, 'cost': 0.045},
    'claude-3-5-haiku-20241022':     {'caps': {'text', 'cheap'},                   'rpm': 100, 'latency': 1500, 'cost': 0.003},

    # ── Claude (AWS / Vertex gateway-prefixed names) ──
    'aws.claude-opus-4.8':           {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 5000, 'cost': 0.015},
    'aws.claude-opus-4.7':           {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 5000, 'cost': 0.015},
    # ── Claude (Amazon Bedrock native model IDs, inference-profile form) ──
    'us.anthropic.claude-opus-4-8-v1:0':         {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 5000, 'cost': 0.015},
    'us.anthropic.claude-opus-4-7-v1:0':         {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 5000, 'cost': 0.015},
    'us.anthropic.claude-opus-4-6-v1:0':         {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 5000, 'cost': 0.015},
    'us.anthropic.claude-sonnet-4-6-v1:0':       {'caps': {'text', 'vision', 'thinking'},      'rpm': 50,  'latency': 2000, 'cost': 0.009},
    'us.anthropic.claude-sonnet-4-5-v1:0':       {'caps': {'text', 'vision', 'thinking'},      'rpm': 50,  'latency': 2000, 'cost': 0.009},
    'us.anthropic.claude-opus-4-5-v1:0':         {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 5000, 'cost': 0.015},
    'us.anthropic.claude-haiku-4-5-v1:0':        {'caps': {'text', 'vision', 'cheap'},         'rpm': 100, 'latency': 1500, 'cost': 0.003},
    'us.anthropic.claude-sonnet-4-20250514-v1:0':{'caps': {'text', 'vision', 'thinking'},      'rpm': 50,  'latency': 2000, 'cost': 0.009},
    'openai.gpt-oss-120b-1:0':                   {'caps': {'text', 'thinking', 'cheap'},       'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'openai.gpt-oss-20b-1:0':                    {'caps': {'text', 'cheap'},                   'rpm': 120, 'latency': 1500, 'cost': 0.0005},
    'aws.claude-opus-4.6':           {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 5000, 'cost': 0.025},
    'aws.claude-opus-4.6-b':         {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 5000, 'cost': 0.025},
    'vertex.claude-opus-4.6':        {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 5000, 'cost': 0.025},
    'aws.claude-sonnet-4.6':         {'caps': {'text', 'vision', 'thinking'},      'rpm': 50,  'latency': 2000, 'cost': 0.015},
    'vertex.claude-sonnet-4.6':      {'caps': {'text', 'vision', 'thinking'},      'rpm': 50,  'latency': 2000, 'cost': 0.015},

    # ── OpenAI (GPT-5.4 family — March 2026) ──
    'gpt-5.4':                       {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 3000, 'cost': 0.015},
    'gpt-5.4-pro':                   {'caps': {'text', 'vision', 'thinking'},      'rpm': 10,  'latency': 10000,'cost': 0.180},
    'gpt-5.4-mini':                  {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 60,  'latency': 2000, 'cost': 0.005},
    'gpt-5.4-nano':                  {'caps': {'text', 'vision', 'cheap'},         'rpm': 200, 'latency': 1000, 'cost': 0.001},
    # ── OpenAI (GPT-5 family) ──
    'gpt-5':                         {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 30,  'latency': 3000, 'cost': 0.010},
    'gpt-5.2':                       {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 30,  'latency': 3000, 'cost': 0.014},
    'gpt-5-mini':                    {'caps': {'text', 'vision', 'cheap'},         'rpm': 60,  'latency': 2000, 'cost': 0.002},
    'gpt-5-nano':                    {'caps': {'text', 'cheap'},                   'rpm': 200, 'latency': 1000, 'cost': 0.001},
    # ── OpenAI (o-series reasoning) ──
    'o3':                            {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 30,  'latency': 5000, 'cost': 0.010},
    'o4-mini':                       {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 30,  'latency': 3000, 'cost': 0.005},
    'o3-mini':                       {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 30,  'latency': 3000, 'cost': 0.005},
    # ── OpenAI (GPT-4 family — previous gen) ──
    'gpt-4o':                        {'caps': {'text', 'vision', 'cheap'},         'rpm': 60,  'latency': 2000, 'cost': 0.005},
    'gpt-4o-mini':                   {'caps': {'text', 'vision', 'cheap'},         'rpm': 200, 'latency': 1500, 'cost': 0.001},
    'gpt-4-turbo':                   {'caps': {'text', 'vision'},                  'rpm': 30,  'latency': 3000, 'cost': 0.020},
    'gpt-4.1':                       {'caps': {'text', 'vision', 'cheap'},         'rpm': 30,  'latency': 3000, 'cost': 0.010},
    'gpt-4.1-mini':                  {'caps': {'text', 'vision', 'cheap'},         'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'gpt-4.1-nano':                  {'caps': {'text', 'cheap'},                   'rpm': 200, 'latency': 1000, 'cost': 0.001},

    # ── DeepSeek ──
    # V4 family (Apr 2026) — 1M ctx, dual Thinking / Non-Thinking; pro=1.6T/49B, flash=284B/13B.
    'deepseek-v4-pro':               {'caps': {'text', 'thinking', 'cheap'},      'rpm': 30,  'latency': 3000, 'cost': 0.001},
    'deepseek-v4-flash':             {'caps': {'text', 'thinking', 'cheap'},      'rpm': 60,  'latency': 2000, 'cost': 0.0002},
    'deepseek-v4-flash-huawei':      {'caps': {'text', 'thinking', 'cheap'},      'rpm': 60,  'latency': 2000, 'cost': 0.0002},
    'deepseek-chat':                 {'caps': {'text', 'cheap'},                  'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'deepseek-v3.2':                 {'caps': {'text', 'cheap'},                  'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'deepseek-v3.2-tencent':         {'caps': {'text', 'cheap'},                  'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'deepseek-v3.2-baidu':           {'caps': {'text', 'cheap'},                  'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'deepseek-v3.2-huawei':          {'caps': {'text', 'cheap'},                  'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'deepseek-v3.2-doubao':          {'caps': {'text', 'cheap'},                  'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'deepseek-reasoner':             {'caps': {'text', 'thinking', 'cheap'},      'rpm': 30,  'latency': 3000, 'cost': 0.002, 'stream_only': True},

    # ── Gemini ──
    'gemini-2.5-pro':                {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 100, 'latency': 2000, 'cost': 0.005},
    'gemini-2.5-flash':              {'caps': {'text', 'vision', 'cheap'},         'rpm': 200, 'latency': 1500, 'cost': 0.001},
    'gemini-2.0-flash-lite':         {'caps': {'text', 'cheap'},                   'rpm': 200, 'latency': 1000, 'cost': 0.001},
    'gemini-3.1-flash-lite-preview': {'caps': {'text', 'vision', 'cheap'},         'rpm': 30,  'latency': 1500, 'cost': 0.001},
    'gemini-3.1-pro-preview':        {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 5,   'latency': 3000, 'cost': 0.006},
    'gemini-3-flash-preview':        {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 60,  'latency': 1500, 'cost': 0.001},
    'gemini-3.5-flash':              {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 30,  'latency': 2000, 'cost': 0.005},

    # ── Qwen (DashScope) ──
    'qwen3.6-plus':                  {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 60, 'latency': 2000, 'cost': 0.002},
    'qwen3.5-plus':                  {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 60, 'latency': 2000, 'cost': 0.001},
    'qwen3.5-flash':                 {'caps': {'text', 'thinking', 'cheap'},       'rpm': 120, 'latency': 1500, 'cost': 0.001},
    'qwen3-max':                     {'caps': {'text', 'thinking', 'cheap'},      'rpm': 30,  'latency': 3000, 'cost': 0.004},
    'qwen3-vl-plus':                 {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 30, 'latency': 3000, 'cost': 0.002},
    'qwen3-vl-flash':                {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 60, 'latency': 2000, 'cost': 0.001},
    'qwen3-coder-plus':              {'caps': {'text', 'thinking', 'cheap'},      'rpm': 30,  'latency': 3000, 'cost': 0.004},
    'qwen3-coder-flash':             {'caps': {'text', 'thinking', 'cheap'},      'rpm': 60,  'latency': 2000, 'cost': 0.002},
    'qwen-plus':                     {'caps': {'text', 'thinking', 'cheap'},      'rpm': 60,  'latency': 2000, 'cost': 0.002},
    'qwen-max':                      {'caps': {'text', 'cheap'},                  'rpm': 30,  'latency': 3000, 'cost': 0.004},
    'qwen-flash':                    {'caps': {'text', 'thinking', 'cheap'},       'rpm': 120, 'latency': 1500, 'cost': 0.001},
    'qwq-plus':                      {'caps': {'text', 'thinking', 'cheap'},      'rpm': 60,  'latency': 2000, 'cost': 0.002, 'stream_only': True},
    'qvq-max':                       {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 30, 'latency': 3000, 'cost': 0.006},
    'qvq-plus':                      {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 60, 'latency': 2000, 'cost': 0.002},
    'qwen-vl-max':                   {'caps': {'text', 'vision', 'cheap'},        'rpm': 30,  'latency': 3000, 'cost': 0.002},
    'qwen-vl-plus':                  {'caps': {'text', 'vision', 'cheap'},         'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'qwen-turbo':                    {'caps': {'text', 'cheap'},                   'rpm': 200, 'latency': 1000, 'cost': 0.001},
    'qwen-long':                     {'caps': {'text', 'cheap'},                  'rpm': 60,  'latency': 2000, 'cost': 0.001},

    # ── MiniMax ──
    # M3 (2026-06-01) — flagship: MSA sparse attn, 1M ctx, native multimodal (image+video in).
    'MiniMax-M3':                    {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 60, 'latency': 2000, 'cost': 0.002},
    'MiniMax-M2':                    {'caps': {'text', 'vision', 'cheap'},        'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'MiniMax-M2.1':                  {'caps': {'text', 'thinking', 'cheap'},      'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'MiniMax-M2.1-highspeed':        {'caps': {'text', 'thinking', 'cheap'},      'rpm': 60,  'latency': 1500, 'cost': 0.002},
    'MiniMax-M2.5':                  {'caps': {'text', 'thinking', 'cheap'},      'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'MiniMax-M2.5-highspeed':        {'caps': {'text', 'thinking', 'cheap'},      'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'MiniMax-M2.7':                  {'caps': {'text', 'thinking', 'cheap'},      'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'MiniMax-M2.7-highspeed':        {'caps': {'text', 'thinking', 'cheap'},      'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'M2-her':                        {'caps': {'text', 'cheap'},                  'rpm': 60,  'latency': 2000, 'cost': 0.001},

    # ── Doubao (Volcengine) ──
    'Doubao-Seed-2.0-pro':           {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 60, 'latency': 2000, 'cost': 0.002},
    'Doubao-Seed-2.0-lite':          {'caps': {'text', 'cheap'},                   'rpm': 120, 'latency': 1500, 'cost': 0.001},
    'Doubao-Seed-2.0-mini':          {'caps': {'text', 'cheap'},                   'rpm': 200, 'latency': 1000, 'cost': 0.001},

    # ── GLM (Zhipu AI) ──
    'glm-5.1':                       {'caps': {'text', 'thinking'},                'rpm': 60,  'latency': 3000, 'cost': 0.004},
    'glm-5':                         {'caps': {'text', 'thinking'},                'rpm': 60,  'latency': 3000, 'cost': 0.004},
    'glm-4.7':                       {'caps': {'text', 'thinking', 'cheap'},      'rpm': 60,  'latency': 2000, 'cost': 0.002},
    'glm-4.5-air':                   {'caps': {'text', 'cheap'},                   'rpm': 120, 'latency': 1500, 'cost': 0.001},
    'glm-4.5-flash':                 {'caps': {'text', 'cheap'},                   'rpm': 200, 'latency': 1000, 'cost': 0.0},
    'glm-5v-turbo':                  {'caps': {'text', 'vision', 'cheap'},         'rpm': 60,  'latency': 2000, 'cost': 0.002},

    # ── Mistral AI ──
    'mistral-large-latest':          {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 30, 'latency': 3000, 'cost': 0.008},
    'mistral-small-latest':          {'caps': {'text', 'cheap'},                   'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'codestral-latest':              {'caps': {'text', 'cheap'},                  'rpm': 60,  'latency': 2000, 'cost': 0.003},

    # ── xAI (Grok) ──
    'grok-3':                        {'caps': {'text', 'thinking'},                'rpm': 30,  'latency': 3000, 'cost': 0.010},
    'grok-3-mini':                   {'caps': {'text', 'thinking', 'cheap'},       'rpm': 60,  'latency': 2000, 'cost': 0.003},

    # ── Tencent Hunyuan ──
    'hy3-preview':                   {'caps': {'text', 'thinking', 'cheap'},       'rpm': 30,  'latency': 3000, 'cost': 0.002},
    'hunyuan-2.0-thinking-20251109': {'caps': {'text', 'thinking', 'cheap'},       'rpm': 30,  'latency': 3000, 'cost': 0.003},
    'hunyuan-2.0-instruct-20251111': {'caps': {'text', 'cheap'},                   'rpm': 30,  'latency': 2500, 'cost': 0.002},
    'hunyuan-role-latest':           {'caps': {'text', 'cheap'},                   'rpm': 30,  'latency': 2500, 'cost': 0.002},

    # ── DeepSeek (additional snapshots served by Tencent TokenHub) ──
    'deepseek-v3.1-terminus':        {'caps': {'text', 'cheap'},                   'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'deepseek-r1-0528':              {'caps': {'text', 'thinking', 'cheap'},       'rpm': 30,  'latency': 3000, 'cost': 0.002},
    'deepseek-v3-0324':              {'caps': {'text', 'cheap'},                   'rpm': 60,  'latency': 2000, 'cost': 0.001},

    # ── GLM Turbo / Kimi older / MiniMax older — TokenHub catalog ──
    'glm-5-turbo':                   {'caps': {'text', 'cheap'},                   'rpm': 60,  'latency': 2000, 'cost': 0.002},
    'kimi-k2.6':                     {'caps': {'text', 'cheap'},                   'rpm': 30,  'latency': 3000, 'cost': 0.003},
    'kimi-k2.5':                     {'caps': {'text', 'cheap'},                   'rpm': 30,  'latency': 3000, 'cost': 0.002},
    'minimax-m2.5':                  {'caps': {'text', 'cheap'},                   'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'minimax-m2.7':                  {'caps': {'text', 'cheap'},                   'rpm': 60,  'latency': 2000, 'cost': 0.001},

    # ── LongCat (internal, free) ──
    'LongCat-Flash-Thinking-2601':   {'caps': {'text', 'thinking', 'cheap'},       'rpm': 60,  'latency': 2000, 'cost': 0.0},
    'LongCat-Flash-Chat-2603':       {'caps': {'text', 'cheap'},                   'rpm': 60,  'latency': 1500, 'cost': 0.001},

    # ── OpenAI Codex (ChatGPT Plus subscription) ──
    'gpt-5.2-codex':                 {'caps': {'text', 'vision', 'thinking'},      'rpm': 10,  'latency': 5000, 'cost': 0.0},
    'gpt-5.1-codex-mini':            {'caps': {'text', 'vision', 'thinking'},      'rpm': 20,  'latency': 3000, 'cost': 0.0},
    'codex-mini':                    {'caps': {'text', 'vision', 'cheap'},         'rpm': 20,  'latency': 2000, 'cost': 0.0},

    # ── Image generation ──
    'gpt-image-1.5':                         {'caps': {'image_gen'},               'rpm': 10,  'latency': 30000, 'cost': 0.043},
    'gpt-image-2':                           {'caps': {'image_gen'},               'rpm': 10,  'latency': 30000, 'cost': 0.065},
    'gpt-image-1':                           {'caps': {'image_gen'},               'rpm': 10,  'latency': 30000, 'cost': 0.040},
    'gpt-image-1-mini':                      {'caps': {'image_gen'},               'rpm': 15,  'latency': 20000, 'cost': 0.015},
    'dall-e-3':                              {'caps': {'image_gen'},               'rpm': 5,   'latency': 30000, 'cost': 0.040},
    'gemini-3.1-flash-image-preview':        {'caps': {'image_gen'},               'rpm': 10,  'latency': 30000, 'cost': 0.015},
    'gemini-3-pro-image-preview':            {'caps': {'image_gen'},               'rpm': 10,  'latency': 30000, 'cost': 0.020},
    'gemini-2.5-flash-image':                {'caps': {'image_gen'},               'rpm': 10,  'latency': 30000, 'cost': 0.015},
    'gemini-2.0-flash-preview-image-generation': {'caps': {'image_gen'},           'rpm': 10,  'latency': 30000, 'cost': 0.010},

    # ── Embeddings ──
    'text-embedding-v4':             {'caps': {'embedding'},                       'rpm': 100, 'latency': 500,  'cost': 0.001},
    'text-embedding-3-small':        {'caps': {'embedding'},                       'rpm': 60,  'latency': 500,  'cost': 0.001},
    'text-embedding-3-large':        {'caps': {'embedding'},                       'rpm': 60,  'latency': 500,  'cost': 0.001},
}


# ══════════════════════════════════════════════════════════════
#  Model alias groups: models within the same group are interchangeable
#  When prefer_model is one of these, any model in the same group is "preferred".
#  This benefits anyone routing Claude through multiple gateway prefixes.
# ══════════════════════════════════════════════════════════════
MODEL_ALIAS_GROUPS = [
    # Claude Opus 4.8 — aws gateway + direct API + Bedrock-native IDs are interchangeable
    {'aws.claude-opus-4.8', 'claude-opus-4-8', 'us.anthropic.claude-opus-4-8-v1:0'},
    # Claude Opus 4.7 — aws gateway + direct API + Bedrock-native IDs are interchangeable
    {'aws.claude-opus-4.7', 'claude-opus-4-7', 'us.anthropic.claude-opus-4-7-v1:0'},
    # Claude Opus 4.6 — aws, vertex, direct API, Bedrock-native names are interchangeable
    {'aws.claude-opus-4.6', 'aws.claude-opus-4.6-b', 'vertex.claude-opus-4.6',
     'claude-opus-4-20250514', 'claude-opus-4-6-20250514', 'claude-opus-4-6',
     'us.anthropic.claude-opus-4-6-v1:0'},
    # Claude Sonnet 4.6 — aws gateway vs direct API name vs Bedrock-native
    {'aws.claude-sonnet-4.6', 'claude-sonnet-4-20250514', 'claude-sonnet-4-6-20250514',
     'claude-sonnet-4-6', 'us.anthropic.claude-sonnet-4-6-v1:0'},
    # DeepSeek V3.2 — Meituan gateway mirrors across Tencent/Baidu/Huawei/Doubao clouds
    {'deepseek-v3.2-tencent', 'deepseek-v3.2-baidu', 'deepseek-v3.2-huawei', 'deepseek-v3.2-doubao'},
    # DeepSeek V4 Flash — direct DeepSeek API + Meituan gateway Huawei-cloud mirror
    {'deepseek-v4-flash', 'deepseek-v4-flash-huawei'},
]

MODEL_ALIASES: dict[str, set[str]] = {}
for _group in MODEL_ALIAS_GROUPS:
    for _m in _group:
        MODEL_ALIASES[_m] = _group
