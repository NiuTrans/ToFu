"""
Pricing — provider-scoped pricing registry.

The same model_id can be exposed by multiple providers at different prices
(e.g. ``kimi-k2.6`` is ¥4/¥18 on Moonshot direct but ¥6.5/¥27 on Tencent
TokenHub). The flat ``MODEL_PRICING`` table can only carry one price per
model_id, which would silently mis-bill any second provider hosting the
same model.

Provider templates may declare a per-row ``pricing`` field (same shape as a
``MODEL_PRICING`` value). Loading code calls :func:`set_provider_pricing` to
register it; cost paths use :func:`lookup_pricing` (model_id, provider_id)
which prefers the provider-scoped entry over the global table, falling back
to ``MODEL_PRICING`` when the provider is unknown or has no override.

``PROVIDER_PRICING`` and ``_provider_pricing_lock`` are the shared mutable
state; they must live together in this one module so all mutators
(set/clear) and readers (lookup/snapshot) share the same objects by
reference.
"""

import threading

from lib.log import get_logger

from lib.pricing._peak import peak_multiplier
from lib.pricing._tables import MODEL_PRICING, QWEN_PRICING_CNY

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════
#  Shared State
# ══════════════════════════════════════════════════════

# Per-provider pricing overrides: PROVIDER_PRICING[provider_id][model_id] = {input, output, ...}
# Populated at server-config load time from each provider template's per-model `pricing` field.
PROVIDER_PRICING = {}
_provider_pricing_lock = threading.Lock()


def _legacy_context_tiers(model_id):
    """Translate the legacy QWEN_PRICING_CNY row into contextTiers.

    This is an input compatibility seam only. Runtime arithmetic consumes the
    unified rows and therefore can select exactly one tier from total prompt
    tokens for every component.
    """
    row = QWEN_PRICING_CNY.get(model_id)
    if not row and 'qwen' in (model_id or '').lower():
        row = QWEN_PRICING_CNY.get('_default')
    if not row:
        return []
    by_limit = {}
    for side in ('input', 'output'):
        for limit, price in row.get(side, ()):
            by_limit.setdefault(int(limit), {})[side] = float(price)
    tiers = []
    last = {'input': 0.0, 'output': 0.0}
    for index, limit in enumerate(sorted(by_limit)):
        last.update(by_limit[limit])
        tiers.append({
            'id': f'ctx_{limit}', 'maxPromptTokens': limit,
            'input': last['input'], 'output': last['output'],
            'currency': 'CNY', 'cacheWriteMul': 1.0,
            'cacheReadMul': 0.0, 'order': index,
        })
    return tiers


def normalize_pricing(model_id, info):
    """Return one canonical pricing row supporting flat or context tiers."""
    row = dict(info or {})
    tiers = row.get('contextTiers')
    if not tiers:
        tiers = _legacy_context_tiers(model_id)
    normalized = []
    for index, raw in enumerate(tiers or ()):
        if not isinstance(raw, dict):
            continue
        limit = raw.get('maxPromptTokens', raw.get('threshold'))
        try:
            limit = int(limit)
        except (TypeError, ValueError) as exc:
            logger.debug('[Pricing] ignoring invalid context tier model=%s '
                         'index=%d limit=%r: %s',
                         model_id, index, limit, exc)
            continue
        if limit <= 0:
            continue
        normalized.append({
            'id': str(raw.get('id') or f'ctx_{limit}'),
            'maxPromptTokens': limit,
            'input': float(raw.get('input') or 0),
            'output': float(raw.get('output') or 0),
            'currency': str(raw.get('currency') or row.get('currency') or 'USD').upper(),
            'cacheWriteMul': float(raw.get('cacheWriteMul', row.get('cacheWriteMul', 1.25))),
            'cacheReadMul': float(raw.get('cacheReadMul', row.get('cacheReadMul', 0.10))),
            'order': index,
        })
    if normalized:
        normalized.sort(key=lambda tier: tier['maxPromptTokens'])
        row['contextTiers'] = normalized
    return row


def _choose_context_tier(tiers, prompt_tokens):
    if not tiers:
        return None
    prompt = max(0, int(prompt_tokens or 0))
    for tier in tiers:
        if prompt <= tier['maxPromptTokens']:
            return dict(tier)
    return dict(tiers[-1])


def first_pricing_increase_boundary(model_id, provider_id=None):
    """Return the first prompt boundary followed by a dearer pricing tier.

    The comparison uses effective billable rates (uncached input, output,
    cache write, and cache read), rather than model names or a vendor-specific
    threshold.  A flat-emulation tier whose following tier has identical or
    cheaper effective rates is therefore ignored.  ``None`` means the active
    provider/model rate card has no proven price cliff.

    The returned boundary is the inclusive ``maxPromptTokens`` of the cheaper
    tier.  Callers should leave their own safety margin below it because the
    rendered provider prompt can grow after an early local estimate.
    """
    pricing = lookup_pricing(
        model_id, provider_id, prompt_tokens=0)
    tiers = (pricing or {}).get('contextTiers') or ()
    if len(tiers) < 2:
        return None

    def effective_rates(tier):
        input_rate = max(0.0, float(tier.get('input') or 0.0))
        output_rate = max(0.0, float(tier.get('output') or 0.0))
        return (
            input_rate,
            output_rate,
            input_rate * max(
                0.0, float(tier.get('cacheWriteMul', 1.25))),
            input_rate * max(
                0.0, float(tier.get('cacheReadMul', 0.10))),
        )

    previous = tiers[0]
    for current in tiers[1:]:
        previous_currency = str(
            previous.get('currency') or pricing.get('currency') or 'USD'
        ).upper()
        current_currency = str(
            current.get('currency') or pricing.get('currency') or 'USD'
        ).upper()
        # Cross-currency tier rows have no safe local comparison. They are an
        # invalid/ambiguous rate-card shape for this policy, so fail open.
        if previous_currency != current_currency:
            return None
        before = effective_rates(previous)
        after = effective_rates(current)
        if any(new > old + 1e-12 for old, new in zip(before, after)):
            return {
                'maxPromptTokens': int(previous['maxPromptTokens']),
                'tierId': str(previous.get('id') or ''),
                'nextTierId': str(current.get('id') or ''),
                'currency': previous_currency,
                'beforeRates': before,
                'afterRates': after,
                'pricingSource': str(
                    pricing.get('_pricingSource') or 'resolved_price'),
            }
        previous = current
    return None


def build_rate_card():
    """Export the read-only rate card from the actual resolver tables."""
    models = {}
    for model_id in sorted(set(MODEL_PRICING) | set(QWEN_PRICING_CNY) - {'_default'}):
        row = normalize_pricing(model_id, MODEL_PRICING.get(model_id))
        if row.get('contextTiers'):
            models[model_id] = {'name': row.get('name', model_id),
                                'kind': 'tiered',
                                'contextTiers': row['contextTiers']}
        else:
            models[model_id] = {
                'name': row.get('name', model_id), 'kind': 'flat',
                'currency': str(row.get('currency') or 'USD').upper(),
                'input': float(row.get('input') or 0),
                'output': float(row.get('output') or 0),
                'cacheWriteMul': float(row.get('cacheWriteMul', 1.25)),
                'cacheReadMul': float(row.get('cacheReadMul', 0.10)),
            }
    providers = {}
    for provider_id, rows in get_provider_pricing_snapshot().items():
        providers[provider_id] = {}
        for model_id, raw in sorted(rows.items()):
            row = normalize_pricing(model_id, raw)
            providers[provider_id][model_id] = {
                'kind': 'tiered' if row.get('contextTiers') else 'flat',
                **({'contextTiers': row['contextTiers']} if row.get('contextTiers') else {
                    'currency': str(row.get('currency') or 'USD').upper(),
                    'input': float(row.get('input') or 0),
                    'output': float(row.get('output') or 0),
                    'cacheWriteMul': float(row.get('cacheWriteMul', 1.25)),
                    'cacheReadMul': float(row.get('cacheReadMul', 0.10)),
                }),
            }
    return {'models': models, 'providers': providers}


def set_provider_pricing(provider_id, model_id, info):
    """Register a provider-scoped pricing override.

    Args:
        provider_id: Provider identifier (matches ``slot.provider_id``).
        model_id: Model id as exposed by that provider.
        info: Dict with at least ``input`` and ``output`` (USD per 1M tokens).
            May also include ``cacheWriteMul``, ``cacheReadMul``, ``name``.
            Pass ``None`` to clear the override.
    """
    if not provider_id or not model_id:
        return
    with _provider_pricing_lock:
        if info is None:
            PROVIDER_PRICING.get(provider_id, {}).pop(model_id, None)
            return
        PROVIDER_PRICING.setdefault(provider_id, {})[model_id] = dict(info)


def clear_provider_pricing(provider_id):
    """Drop all overrides for one provider — used when the provider is removed/disabled."""
    with _provider_pricing_lock:
        PROVIDER_PRICING.pop(provider_id, None)


def lookup_pricing(model_id, provider_id=None, at=None, prompt_tokens=None):
    """Resolve pricing for a (model, provider) pair.

    Resolution order:
      1. ``PROVIDER_PRICING[provider_id][model_id]`` if present.
      2. ``MODEL_PRICING[model_id]`` global fallback.
      3. ``None`` if neither knows about the model.

    Peak-hour schedules (``lib/pricing/_peak.py``): when the resolved row
    carries an ACTIVE ``peak`` block and *at* falls inside a peak window,
    the returned ``input``/``output`` unit prices are scaled by the peak
    multiplier (cache muls are relative to input, so all four billing
    items scale together) and a ``peakMul`` key is stamped on the copy.
    ``at`` defaults to now; historical recomputation (daily_report) must
    pass the message's own timestamp.

    Returns a *copy* of the dict so callers can mutate freely.
    """
    info = None
    source = ''
    if provider_id:
        with _provider_pricing_lock:
            prov = PROVIDER_PRICING.get(provider_id)
            if prov and model_id in prov:
                info = dict(prov[model_id])
                source = 'provider_override'
    if info is None:
        row = MODEL_PRICING.get(model_id)
        info = dict(row) if row else None
        if info is not None:
            source = 'model_table'
    if info is None and _legacy_context_tiers(model_id):
        info = {}
        source = ('qwen_default_estimate' if model_id not in QWEN_PRICING_CNY
                  else 'model_table')
    if info is None:
        return None
    info = normalize_pricing(model_id, info)
    chosen = _choose_context_tier(info.get('contextTiers'), prompt_tokens)
    if chosen:
        info.update(input=chosen['input'], output=chosen['output'],
                    cacheWriteMul=chosen['cacheWriteMul'],
                    cacheReadMul=chosen['cacheReadMul'],
                    currency=chosen['currency'], selectedTier=chosen)
    # Provenance rides the resolved copy only; the source tables stay clean.
    info['_pricingSource'] = source
    mult = peak_multiplier(info, at=at)
    if mult != 1.0:
        info['input'] = float(info.get('input') or 0) * mult
        info['output'] = float(info.get('output') or 0) * mult
        info['peakMul'] = mult
        logger.debug('[Pricing] peak x%s applied for %s (provider=%s)',
                     mult, model_id, provider_id or '-')
    return info


def get_provider_pricing_snapshot():
    """Thread-safe snapshot of the full per-provider override map."""
    with _provider_pricing_lock:
        return {pid: {mid: dict(v) for mid, v in mp.items()}
                for pid, mp in PROVIDER_PRICING.items()}
