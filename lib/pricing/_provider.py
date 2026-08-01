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
from lib.pricing._tables import MODEL_PRICING

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════
#  Shared State
# ══════════════════════════════════════════════════════

# Per-provider pricing overrides: PROVIDER_PRICING[provider_id][model_id] = {input, output, ...}
# Populated at server-config load time from each provider template's per-model `pricing` field.
PROVIDER_PRICING = {}
_provider_pricing_lock = threading.Lock()


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


def lookup_pricing(model_id, provider_id=None, at=None):
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
    if provider_id:
        with _provider_pricing_lock:
            prov = PROVIDER_PRICING.get(provider_id)
            if prov and model_id in prov:
                info = dict(prov[model_id])
    if info is None:
        row = MODEL_PRICING.get(model_id)
        info = dict(row) if row else None
    if info is None:
        return None
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
