"""Summary-call price evidence and bounded receipt aggregation.

``price_summary_call`` delegates all token/cache arithmetic to ``lib.cost``;
``summary_cost_details`` sums independently priced attempts, including failures.
USD values are metered API-equivalent estimates, never subscription cash charges.
Missing usage, route prices or exchange rates stay unknown, not zero.
"""
from __future__ import annotations

import math
from collections.abc import Mapping

from lib.log import get_logger

logger = get_logger(__name__)

_COST_BASIS = 'metered_api_equivalent'
_ESTIMATED_SOURCES = {'default_estimate', 'qwen_default_estimate'}
_COMPONENTS = ('inputCostUsd', 'outputCostUsd',
               'cacheWriteCostUsd', 'cacheReadCostUsd')


def _nonnegative_finite(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _unknown(reason):
    return {'costUsd': None, 'known': False, 'reason': reason,
            'costBasis': _COST_BASIS}


def _verified_rates(pricing, *, usd_to_cny=None):
    """Validate resolver evidence; share currency/rate checks with payback.

    Cache defaults match the cost engine. No monetary amount is calculated here.
    """
    if not isinstance(pricing, Mapping) or not pricing:
        raise ValueError('pricing_unavailable')
    source = str(pricing.get('_pricingSource') or 'resolved_price')
    currency = str(pricing.get('currency') or 'USD').upper()
    if (source in _ESTIMATED_SOURCES or currency not in {'USD', 'CNY'}
            or pricing.get('unit', 'per_million_tokens') != 'per_million_tokens'):
        raise ValueError('pricing_unavailable')
    rates = {key: _nonnegative_finite(pricing.get(key, default))
             for key, default in (('input', None), ('output', None),
                                  ('cacheReadMul', 0.10),
                                  ('cacheWriteMul', 1.25))}
    if any(value is None for value in rates.values()):
        raise ValueError('pricing_unavailable')
    exchange_rate = _nonnegative_finite(usd_to_cny)
    if currency == 'CNY' and not exchange_rate:
        raise ValueError('exchange_rate_unavailable')
    return {**rates, 'currency': currency, 'source': source,
            'usdToCny': exchange_rate if currency == 'CNY' else None}


def _lookup_verified_pricing(model, provider_id, *, prompt_tokens):
    """Reject incomplete raw overrides before the display resolver fills zeros.

    An execution pin is not a billing provider. A provider-scoped call must
    have that provider's own rate card, never another supplier's model table.
    Reads the existing in-memory registry only; does not register any prices.
    """
    from lib.pricing import lookup_pricing
    from lib.pricing._provider import get_provider_pricing_snapshot
    from lib.pricing._tables import pricing_for_model

    pricing = lookup_pricing(model, provider_id, prompt_tokens=prompt_tokens)
    if not isinstance(pricing, Mapping):
        raise ValueError('pricing_unavailable')
    source = pricing.get('_pricingSource')
    if provider_id and source != 'provider_override':
        raise ValueError('provider_pricing_unavailable')
    if source == 'provider_override':
        raw = get_provider_pricing_snapshot().get(provider_id, {}).get(model)
    elif source == 'model_table':
        raw = pricing_for_model(model)
    else:
        raise ValueError('pricing_unavailable')
    if not isinstance(raw, Mapping) or not raw:
        raise ValueError('pricing_unavailable')
    tiers = raw.get('contextTiers') or []
    if not isinstance(tiers, list) or any(not isinstance(row, Mapping) for row in tiers):
        raise ValueError('invalid_context_tiers')
    limits = []
    for row in tiers or [raw]:
        if any(_nonnegative_finite(row.get(key)) is None for key in ('input', 'output')):
            raise ValueError('pricing_unavailable')
        if tiers:
            limit = row.get('maxPromptTokens', row.get('threshold'))
            if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
                raise ValueError('invalid_context_tiers')
            limits.append(limit)
    if limits and (len(set(limits)) != len(limits) or prompt_tokens > max(limits)):
        raise ValueError('unqualified_context_tier')
    for row in [raw, *tiers]:
        currency = str(row.get('currency', raw.get('currency', 'USD'))).upper()
        if (currency not in {'USD', 'CNY'}
                or row.get('unit', raw.get('unit', 'per_million_tokens')) != 'per_million_tokens'):
            raise ValueError('pricing_unavailable')
        for key in ('input', 'output', 'cacheReadMul', 'cacheWriteMul', 'cache_read', 'cache_write'):
            if key in row and _nonnegative_finite(row[key]) is None:
                raise ValueError('pricing_unavailable')
        # lib.cost consumes cache multipliers, not absolute offering rates.
        # Never silently ignore an absolute rate that the registry failed to
        # convert, including nonzero cache prices beside a zero input price.
        for absolute, multiplier, default in (
                ('cache_read', 'cacheReadMul', .1), ('cache_write', 'cacheWriteMul', 1.25)):
            if absolute in row:
                input_rate = _nonnegative_finite(row.get('input', raw.get('input')))
                mul = _nonnegative_finite(row.get(multiplier, raw.get(multiplier, default)))
                if (input_rate is None or mul is None
                        or not math.isclose(float(row[absolute]), input_rate * mul)):
                    raise ValueError('unresolved_cache_rate')
    return pricing


def _complete_usage(usage):
    """Validate observed counters without duplicating the engine's alias map.

    Normalization fills missing/bad counts with zero, which is convenient for
    display but not evidence of a complete bill. Both input and output must be
    observed (thinking-only output is also supported by the engine).
    """
    from lib.cost import _USAGE_KEY_ALIASES, _USAGE_NESTED_ALIASES

    if not isinstance(usage, Mapping) or not usage:
        return False
    observed = {}
    for channel, aliases in _USAGE_KEY_ALIASES.items():
        values = [usage[key] for key in aliases if key in usage]
        for parent_key, child_key in _USAGE_NESTED_ALIASES.get(channel, ()):
            parent = usage.get(parent_key)
            if parent is not None and not isinstance(parent, Mapping):
                return False
            if isinstance(parent, Mapping) and child_key in parent:
                values.append(parent[child_key])
        counts = []
        for value in values:
            if value is None:
                continue
            number = _nonnegative_finite(value)
            if number is None:
                return False
            try:
                count = int(value)
            except (TypeError, ValueError, OverflowError):
                return False
            if count != number:
                return False
            counts.append(count)
        observed[channel] = counts
    if not (observed['input'] and (observed['output'] or any(observed['thinking']))):
        return False
    from lib.cost import normalize_usage, split_input_tokens
    counts = normalize_usage(usage)
    # Cached counters are a subset of OpenAI prompt totals, and additive for
    # Anthropic. Let the shared convention resolver make that distinction.
    return split_input_tokens(usage)[1] >= counts['cache_read'] + counts['cache_write']


def price_summary_call(usage, *, model, provider_id=None, declared_pricing=None):
    """Price one actual summary attempt at its own route and full prompt tier."""
    from lib.cost import compute_cost, normalize_usage, split_input_tokens
    from lib.pricing import get_pricing_data

    if not _complete_usage(usage):
        return _unknown('usage_unavailable')
    if (not isinstance(model, str) or not model
            or not isinstance(provider_id, str) or not provider_id):
        return _unknown('route_unavailable')
    raw = dict(usage)
    try:
        prompt_tokens = split_input_tokens(raw)[1]
        if declared_pricing is None:
            pricing = _lookup_verified_pricing(model, provider_id, prompt_tokens=prompt_tokens)
        else:
            from lib.tasks_pkg.compaction._summary_routing import _project_cost
            from lib.pricing._provider import normalize_pricing, _choose_context_tier
            if _project_cost({'providerId': provider_id, 'declaredPricing': declared_pricing},
                             prompt_tokens, 0) is None:
                raise ValueError('offering_pricing_unavailable')
            pricing = normalize_pricing('', declared_pricing)
            selected = _choose_context_tier(pricing.get('contextTiers'), prompt_tokens)
            if selected:
                pricing.update(selected)
                pricing['selectedTier'] = selected
            # Absolute Offering cache prices must match the cost engine's
            # multiplier vocabulary; zero-input/nonzero-cache stays unknown.
            rate_row = declared_pricing
            if declared_pricing.get('contextTiers'):
                rate_row = next(row for row in sorted(declared_pricing['contextTiers'],
                    key=lambda row: row['maxPromptTokens']) if prompt_tokens <= row['maxPromptTokens'])
            for absolute, multiplier in (('cache_read', 'cacheReadMul'), ('cache_write', 'cacheWriteMul')):
                rate = rate_row.get(absolute, declared_pricing.get(absolute))
                if rate is not None:
                    if not pricing['input'] and rate:
                        raise ValueError('unresolved_cache_rate')
                    pricing[multiplier] = rate / pricing['input'] if pricing['input'] else 0
            pricing['_pricingSource'] = 'admitted_offering_price'
        rates = _verified_rates(
            pricing, usd_to_cny=get_pricing_data().get('usdToCny'))
        cost = compute_cost(raw, model_id=model, provider_id=provider_id,
                            **({'pricing_override': pricing} if declared_pricing is not None else {}))
        if cost is None and not any(normalize_usage(raw).values()):
            # Explicit, complete zero counters are different from absent usage.
            tier = pricing.get('selectedTier') or {}
            cost = {'costUsd': 0.0, 'pricingSource': rates['source'],
                    'pricingSnapshot': {
                        'model': model, 'provider': provider_id,
                        'source': rates['source'], 'currency': rates['currency'],
                        'selectedPromptTokens': prompt_tokens,
                        'tierId': tier.get('id'),
                        'maxPromptTokens': tier.get('maxPromptTokens'),
                        'rates': {key: rates[key] for key in (
                            'input', 'output', 'cacheWriteMul', 'cacheReadMul')},
                    }}
            amount = 0.0
        else:
            if not cost or cost.get('pricingSource') in _ESTIMATED_SOURCES:
                return _unknown('pricing_unavailable')
            components = [_nonnegative_finite(cost.get(key)) for key in _COMPONENTS]
            if any(value is None for value in components):
                return _unknown('pricing_unavailable')
            # Display costUsd rounds to 4dp. Billing components retain 9dp:
            # do not turn a short/cheap summary into evidence of free service.
            amount = math.fsum(components)
        snapshot = dict(cost.get('pricingSnapshot') or {})
        if (snapshot.get('model') != model
                or (snapshot.get('provider') or None) != (provider_id or None)):
            return _unknown('route_unavailable')
        _verified_rates({**snapshot.get('rates', {}),
                         'currency': snapshot.get('currency'),
                         '_pricingSource': cost.get('pricingSource')},
                        usd_to_cny=rates['usdToCny'])
        if rates['usdToCny'] is not None:
            snapshot['usdToCny'] = rates['usdToCny']
        if _nonnegative_finite(amount) is None:
            return _unknown('pricing_unavailable')
        return {'costUsd': round(amount, 9), 'known': True,
                'pricingSource': cost.get('pricingSource'),
                'pricingSnapshot': snapshot, 'costBasis': _COST_BASIS}
    except Exception as diagnostic_error:
        # Auxiliary accounting must never fail compaction or invent free usage.
        logger.debug('price_summary_call failure handled: %s', type(diagnostic_error).__name__)
        return _unknown('pricing_unavailable')


def _recorded_cost(cost, *, model, provider, usage):
    """Keep call-time evidence; never reprice a failed/unknown historical row."""
    if not isinstance(cost, Mapping):
        return _unknown('invalid_cost')
    if cost.get('costUsd') is None:
        return _unknown(str(cost.get('reason') or 'pricing_unavailable'))
    amount = _nonnegative_finite(cost.get('costUsd'))
    snapshot = cost.get('pricingSnapshot')
    if (amount is None or cost.get('known') is False
            or cost.get('costBasis') != _COST_BASIS
            or cost.get('pricingSource') not in ('provider_override', 'admitted_offering_price')
            or not isinstance(provider, str) or not provider
            or not isinstance(snapshot, Mapping)
            or snapshot.get('source') != cost.get('pricingSource')
            or snapshot.get('currency') not in ('USD', 'CNY')
            or not isinstance(model, str) or not model
            or snapshot.get('model') != model
            or (snapshot.get('provider') or None) != (provider or None)):
        return _unknown('invalid_cost')
    try:
        from lib.cost import split_input_tokens
        prompt_tokens = snapshot.get('selectedPromptTokens')
        if (isinstance(prompt_tokens, bool) or not isinstance(prompt_tokens, int)
                or prompt_tokens != split_input_tokens(usage)[1]):
            return _unknown('invalid_cost')
        _verified_rates({**snapshot.get('rates', {}),
                         'currency': snapshot.get('currency'),
                         '_pricingSource': cost.get('pricingSource')},
                        usd_to_cny=snapshot.get('usdToCny'))
    except (TypeError, ValueError):
        return _unknown('invalid_cost')
    return {'costUsd': amount, 'known': True, 'costBasis': _COST_BASIS,
            'pricingSource': cost['pricingSource'],
            'pricingSnapshot': dict(snapshot)}


def summary_cost_details(usage, *, fallback_model='', fallback_provider=None):
    """Sum all attempts, retaining at most 16 route/price rows in a receipt.

    Explicit fallback arguments must identify the SUMMARY route, never the main
    task. A per-attempt ledger wins over aggregate counters; a malformed or
    missing attempt makes the total unknown while known spend remains visible.
    """
    raw = dict(usage) if isinstance(usage, Mapping) else {}
    calls = raw.get('_summaryCalls')
    if '_summaryCalls' not in raw:
        route = raw.get('_summaryRoute')
        route = route if isinstance(route, Mapping) else {}
        calls = [{'usage': raw, 'model': route.get('modelId') or fallback_model,
                  'provider_id': route.get('providerId') or fallback_provider}]
    elif not isinstance(calls, list) or not calls:
        calls = [None]
    identities = []
    amounts = []
    unknown_count = 0
    for row in calls:
        row = row if isinstance(row, Mapping) else {}
        call_usage = row.get('usage')
        dispatch = call_usage.get('_dispatch') if isinstance(call_usage, Mapping) else None
        dispatch = dispatch if isinstance(dispatch, Mapping) else {}
        model = dispatch.get('model') or row.get('model') or fallback_model
        provider = (dispatch.get('provider_id') or row.get('provider_id')
                    or fallback_provider)
        if not _complete_usage(call_usage):
            cost = _unknown('usage_unavailable')
        elif 'cost' in row:
            cost = _recorded_cost(row['cost'], model=model, provider=provider, usage=call_usage)
        else:
            cost = price_summary_call(call_usage, model=model, provider_id=provider)
        amount = cost.get('costUsd')
        if amount is None:
            unknown_count += 1
        else:
            amounts.append(amount)
        if len(identities) < 16:
            identities.append({'model': str(model or ''),
                               'providerId': str(provider or ''), **cost})
    try:
        total = math.fsum(amounts)
    except OverflowError:
        total = None
    known = unknown_count == 0 and total is not None
    return {'costUsd': round(total, 9) if known else None,
            'knownCostUsd': round(total, 9) if amounts and total is not None else None,
            'costBasis': _COST_BASIS, 'calls': identities, 'known': known,
            'callCount': len(calls), 'unknownCallCount': unknown_count}
