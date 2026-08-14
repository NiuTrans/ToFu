"""Canonical model-registration contract.

New models enter the application through :func:`register_model`.  The public
model shape intentionally contains only user-meaningful facts::

    {
        "model_id": "example-model",
        "capabilities": ["text", "thinking"],
        "rpm": 60,
        "context_window": 1_000_000,
        "pricing": {
            "input": 1.0,
            "output": 4.0,
            "currency": "USD",
        },
    }

``cost`` is not part of this contract.  It was a blended dispatch heuristic,
not a billable price, and exposing it beside input/output prices made it easy
to register a model that could be routed but could not be costed.  Old saved
rows may still carry ``cost``; callers can pass it separately as a legacy
fallback to :func:`routing_cost_per_1k` while the normalized row drops it.
"""

from __future__ import annotations

import copy
import threading
from typing import Any

from lib.log import get_logger


logger = get_logger(__name__)


class ModelRegistrationError(ValueError):
    """A model entry does not satisfy the canonical registration contract."""


_registry_lock = threading.RLock()
_registered_models: dict[tuple[str, str], dict] = {}


def _finite_nonnegative(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ModelRegistrationError(f'{field} must be numeric') from exc
    if number < 0 or number != number or number in (float('inf'), float('-inf')):
        raise ModelRegistrationError(f'{field} must be a finite non-negative number')
    return number


def _positive_int(value: Any, field: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ModelRegistrationError(f'{field} must be an integer') from exc
    if number <= 0:
        raise ModelRegistrationError(f'{field} must be greater than zero')
    return number


def _normalize_pricing(raw: Any) -> dict | None:
    if raw in (None, {}):
        return None
    if not isinstance(raw, dict):
        raise ModelRegistrationError('pricing must be an object')
    has_input = raw.get('input') is not None
    has_output = raw.get('output') is not None
    if has_input != has_output:
        raise ModelRegistrationError(
            'pricing.input and pricing.output must be registered together')
    if not has_input:
        return None

    pricing = copy.deepcopy(raw)
    pricing['input'] = _finite_nonnegative(raw['input'], 'pricing.input')
    pricing['output'] = _finite_nonnegative(raw['output'], 'pricing.output')
    pricing['currency'] = str(raw.get('currency') or 'USD').strip().upper()
    if pricing['currency'] not in ('USD', 'CNY'):
        raise ModelRegistrationError('pricing.currency must be USD or CNY')
    # Make the unit explicit at the registration boundary. Existing pricing
    # arithmetic already uses per-million-token rates.
    pricing['unit'] = 'per_million_tokens'
    for field in ('cacheWriteMul', 'cacheReadMul'):
        if field in pricing:
            pricing[field] = _finite_nonnegative(pricing[field], f'pricing.{field}')
    return pricing


def normalize_model_entry(entry: dict, *, reject_legacy_cost: bool = False) -> dict:
    """Return a canonical, JSON-serializable copy of one model entry.

    ``input_price`` / ``output_price`` are accepted as a discovery migration
    seam and folded into ``pricing``.  ``cost`` is always removed; strict API
    callers may reject it so new integrations learn the real contract instead
    of silently persisting the obsolete routing heuristic.
    """
    if not isinstance(entry, dict):
        raise ModelRegistrationError('model entry must be an object')
    model_id = str(entry.get('model_id') or entry.get('id') or '').strip()
    if not model_id:
        raise ModelRegistrationError('model_id is required')
    if reject_legacy_cost and 'cost' in entry:
        raise ModelRegistrationError(
            'cost is no longer a model-registration field; use '
            'pricing.input and pricing.output')

    out = copy.deepcopy(entry)
    out.pop('id', None)
    out.pop('cost', None)
    out['model_id'] = model_id

    caps = entry.get('capabilities', ['text'])
    if not isinstance(caps, (list, tuple, set)):
        raise ModelRegistrationError('capabilities must be an array')
    out['capabilities'] = list(dict.fromkeys(
        str(cap).strip() for cap in caps if str(cap).strip())) or ['text']

    if 'rpm' in entry and entry.get('rpm') is not None:
        out['rpm'] = _positive_int(entry['rpm'], 'rpm')
    else:
        out['rpm'] = 30
    if 'latency' in entry and entry.get('latency') is not None:
        out['latency'] = _finite_nonnegative(entry['latency'], 'latency')

    if 'context_window' in entry and entry.get('context_window') is not None:
        out['context_window'] = _positive_int(
            entry['context_window'], 'context_window')
    elif 'context_window' in entry:
        out['context_window'] = None

    raw_pricing = entry.get('pricing')
    if raw_pricing is None and (
            entry.get('input_price') is not None
            or entry.get('output_price') is not None):
        raw_pricing = {
            'input': entry.get('input_price'),
            'output': entry.get('output_price'),
            'currency': entry.get('price_currency') or 'USD',
        }
    pricing = _normalize_pricing(raw_pricing)
    out.pop('input_price', None)
    out.pop('output_price', None)
    out.pop('price_currency', None)
    if pricing is None:
        out.pop('pricing', None)
    else:
        out['pricing'] = pricing

    for pool_field in ('request_ids', 'aliases'):
        if pool_field not in out:
            continue
        raw_pool = out.get(pool_field)
        if not isinstance(raw_pool, list):
            raise ModelRegistrationError(f'{pool_field} must be an array')
        out[pool_field] = list(dict.fromkeys(
            str(value).strip() for value in raw_pool
            if str(value).strip()))
    return out


def _identity_ids(entry: dict) -> list[str]:
    """Logical and wire ids that must share provider-scoped metadata."""
    model_id = entry['model_id']
    try:
        from lib.llm_dispatch.model_entry import resolve_request_ids
        wire_ids = resolve_request_ids(entry)
    except Exception as exc:
        logger.debug('request-id resolution failed for %s: %s', model_id, exc)
        wire_ids = [model_id]
    return list(dict.fromkeys([model_id, *wire_ids]))


def register_model(entry: dict, *, provider_id: str = '',
                   reject_legacy_cost: bool = False) -> dict:
    """Validate and register one model through the shared application seam.

    The returned row is what may be persisted or sent to the frontend.  An
    embedded price is also installed for every logical/wire identity so cost
    accounting cannot silently become zero after a request-id pool split.
    """
    normalized = normalize_model_entry(
        entry, reject_legacy_cost=reject_legacy_cost)
    provider_key = str(provider_id or '').strip()
    with _registry_lock:
        for identity in _identity_ids(normalized):
            _registered_models[(provider_key, identity)] = copy.deepcopy(normalized)

    pricing = normalized.get('pricing')
    if isinstance(pricing, dict):
        identities = _identity_ids(normalized)
        if provider_key:
            from lib.pricing import set_provider_pricing
            for identity in identities:
                set_provider_pricing(provider_key, identity, pricing)
        else:
            # An unscoped registration is the global fallback contract. This
            # is deliberately the same entry point, so adding a standalone
            # model cannot update routing metadata while forgetting billing.
            from lib.pricing import MODEL_PRICING
            for identity in identities:
                MODEL_PRICING[identity] = copy.deepcopy(pricing)
    return normalized


def clear_provider_models(provider_id: str) -> None:
    """Remove every registered model and price owned by one provider.

    Call this before replacing a provider snapshot and when deleting one. It
    prevents removed models from leaving stale context or billable rates in a
    long-running process.
    """
    provider_key = str(provider_id or '').strip()
    with _registry_lock:
        stale = [key for key in _registered_models if key[0] == provider_key]
        for key in stale:
            _registered_models.pop(key, None)
    if provider_key:
        from lib.pricing import clear_provider_pricing
        clear_provider_pricing(provider_key)


def registered_context_profile(model_id: str, provider_id: str = '') -> dict | None:
    """Return explicitly registered context metadata, if available."""
    keys = ((str(provider_id or ''), model_id), ('', model_id))
    with _registry_lock:
        row = next((_registered_models.get(key) for key in keys
                    if _registered_models.get(key) is not None), None)
        if not row or row.get('context_window') is None:
            return None
        return {
            'window': int(row['context_window']),
            'source': str(row.get('context_window_source') or 'model_registration'),
            'exact': bool(row.get('context_window_exact', True)),
        }


def routing_cost_per_1k(entry: dict, *, provider_id: str = '',
                        wire_model_id: str = '',
                        legacy_fallback: float | None = None) -> float:
    """Derive the private dispatch heuristic from actual pricing.

    The value never belongs in a model registration or UI.  Provider pricing
    wins, followed by embedded pricing/global pricing, then an old saved
    ``cost`` value supplied explicitly by the compatibility caller.
    """
    model_id = wire_model_id or str(entry.get('model_id') or '')
    info = None
    try:
        from lib.pricing import DEFAULT_USD_CNY_RATE, lookup_pricing
        info = lookup_pricing(model_id, provider_id, prompt_tokens=0)
    except Exception as exc:
        logger.debug('pricing lookup failed for %s: %s', model_id, exc)
        DEFAULT_USD_CNY_RATE = 7.24
    if not info and isinstance(entry.get('pricing'), dict):
        info = entry['pricing']
    if info:
        selected = info.get('selectedTier')
        rates = selected if isinstance(selected, dict) else info
        if rates.get('input') is not None and rates.get('output') is not None:
            blended = (_finite_nonnegative(rates['input'], 'pricing.input')
                       + _finite_nonnegative(rates['output'], 'pricing.output')) / 2
            currency = str(rates.get('currency') or info.get('currency') or 'USD').upper()
            if currency == 'CNY':
                blended /= DEFAULT_USD_CNY_RATE
            return round(blended / 1000.0, 6)
    if legacy_fallback is not None:
        try:
            return max(0.0, float(legacy_fallback))
        except (TypeError, ValueError) as exc:
            logger.debug('legacy routing cost is invalid: %s', exc)
            pass
    return 0.01


def canonicalize_providers(providers: list, *, reject_legacy_cost: bool = False,
                           activate: bool = True) -> list:
    """Normalize every model row in a provider list without mutating input."""
    out = copy.deepcopy(providers if isinstance(providers, list) else [])
    for provider_index, provider in enumerate(out):
        if not isinstance(provider, dict):
            raise ModelRegistrationError(
                f'providers[{provider_index}] must be an object')
        provider_id = str(provider.get('id') or provider.get('key')
                          or provider.get('brand') or '')
        if activate and provider_id:
            clear_provider_models(provider_id)
        models = provider.get('models') or []
        if not isinstance(models, list):
            raise ModelRegistrationError(
                f'providers[{provider_index}].models must be an array')
        normalizer = register_model if activate else normalize_model_entry
        provider['models'] = [
            (normalizer(model, provider_id=provider_id,
                        reject_legacy_cost=reject_legacy_cost)
             if activate else normalizer(
                 model, reject_legacy_cost=reject_legacy_cost))
            for model in models
        ]
    return out


__all__ = [
    'ModelRegistrationError',
    'canonicalize_providers',
    'clear_provider_models',
    'normalize_model_entry',
    'register_model',
    'registered_context_profile',
    'routing_cost_per_1k',
]
