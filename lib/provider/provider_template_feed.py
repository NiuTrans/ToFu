"""Provider-owned public catalog refresh for onboarding templates.

Entry points: load_cached_feed, maybe_refresh_async, refresh_now and
merge_feed_recipes. Only allowlisted public endpoints can supply their OWN
provider template. OpenRouter's vendor prefixes are not evidence of another
provider's wire IDs, prices or availability. Old cross-provider caches are
invalidated by version. Configured account discovery remains a separate,
credentialed operation in llm_dispatch/provider_catalog_sync.py.

Budgets: one refresh in flight, 60-second pass, 8-second HTTP timeout, 4 MiB
response/cache, 4096 inspected rows and 256 recipes/source. Reads never block
on HTTP; last-good data survives partial failures, expires from use after
7 days, and failed attempts back off for 5 minutes. No secrets, arbitrary
URLs, paid probes, persistent worker pools, or user-authority writes here.
"""
from __future__ import annotations

import asyncio

import copy
import json
import math
import os
import threading
import time

from lib.config_dir import config_path, ensure_config_dir
from lib.http_transport.client import async_http_stream, close_async_http_clients
from lib.json_store import write_json_atomic
from lib.log import get_logger
from lib.model_registration import ModelRegistrationError, normalize_model_entry

logger = get_logger(__name__)
_CACHE_VERSION = 2
_CACHE_PATH = config_path('provider_template_feed.json')
_MAX_BYTES = 4 * 1024 * 1024
_MAX_SOURCE_ROWS = 4096
_MAX_FEED_ROWS_PER_TEMPLATE = 256
_MAX_STALE_SECONDS = 7 * 86400
_FETCH_TIMEOUT_S = 8
_REFRESH_BUDGET_S = 60
_RETRY_INTERVAL_S = 300

# Reviewed endpoints only, not URLs from a template or upstream payload.
# A deployment override with another base URL never receives this feed.
PUBLIC_CATALOGS = {
    'openrouter': {
        'url': 'https://openrouter.ai/api/v1/models',
        'base_url': 'https://openrouter.ai/api/v1',
    },
    'cerebras': {
        'url': 'https://api.cerebras.ai/public/v1/models',
        'base_url': 'https://api.cerebras.ai/v1',
    },
    'sambanova': {
        'url': 'https://api.sambanova.ai/v1/models',
        'base_url': 'https://api.sambanova.ai/v1',
    },
    'ovhcloud': {
        'url': 'https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/models',
        'base_url': 'https://oai.endpoints.kepler.ai.cloud.ovh.net/v1',
    },
}
_refresh_lock = threading.Lock()
_schedule_lock = threading.Lock()
_refresh_scheduled = False
_last_attempt = 0.0


def _disabled() -> bool:
    return os.environ.get('TOFU_TEMPLATE_FEED', '1').strip().lower() in (
        '0', 'false', 'no', 'off')


def _ttl_seconds() -> float:
    try:
        value = float(os.environ.get('TOFU_TEMPLATE_FEED_TTL_S', '86400'))
        if not math.isfinite(value):
            raise ValueError('nonfinite TTL')
    except (TypeError, ValueError, OverflowError):
        value = 86400.0
    return min(_MAX_STALE_SECONDS, max(3600.0, value))


def _timestamp(value) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        result = float(value)
        return result if math.isfinite(result) and result > 0 else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _amount(value):
    if isinstance(value, bool):
        return None
    try:
        amount = float(value)
        return amount if math.isfinite(amount) and amount >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _unique_logical_seeds(rows: list[dict]) -> list[dict]:
    """One seed per exact migration identity, with its own wire price intact.

    Billing channels can resolve to the same ModelRef (e.g. a contributor
    tier and the standard tier). V2 has one Offering price per ModelRef;
    combining their wire pools would misbill one tier. Prefer the shortest
    advertised spelling deterministically, not a fuzzy/date-stripped match.
    Account discovery can expose additional deployments separately.
    """
    from lib.model_catalog._creator_identity import creator_family, strip_routing_decoration

    chosen = {}
    for recipe in rows:
        wire = recipe['model_id']
        family = creator_family(wire)
        identity = (family, strip_routing_decoration(wire) if family else wire)
        previous = chosen.get(identity)
        if previous is None or (len(wire), wire) < (len(previous['model_id']), previous['model_id']):
            chosen[identity] = recipe
    return list(chosen.values())


def recipes_from_catalog(rows: list, *, template_key='openrouter') -> dict[str, list[dict]]:
    """Project exact wire IDs; never strip a vendor prefix into another API."""
    if template_key not in PUBLIC_CATALOGS:
        return {}
    built = {}
    # Public catalogs are not promised to be newest-first. Keep recent models
    # inside the seed budget, then stable ordering for undated rows.
    candidates = [r for r in rows[:_MAX_SOURCE_ROWS] if isinstance(r, dict)]
    candidates.sort(key=lambda r: (-_timestamp(r.get('created')), str(r.get('id', ''))))
    for row in candidates:
        model_id = row.get('id')
        if (not isinstance(model_id, str) or not model_id.strip()
                or len(model_id) > 240 or model_id.startswith('~')
                or ':' in model_id or any(ord(c) < 32 for c in model_id)):
            continue
        model_id = model_id.strip()
        if model_id in built:
            continue
        if row.get('deprecated') is True:
            continue
        if template_key in ('sambanova', 'ovhcloud') and (
                not _timestamp(row.get('max_completion_tokens'))
                or not _timestamp(row.get('context_length'))
                or row.get('owned_by') == 'qwen3guard'):
            continue
        architecture = row.get('architecture')
        architecture = architecture if isinstance(architecture, dict) else {}
        outputs = architecture.get('output_modalities') or ['text']
        if not isinstance(outputs, list) or 'text' not in outputs:
            continue  # This feed only seeds chat-capable routes.
        inputs = architecture.get('input_modalities') or []
        parameters = row.get('supported_parameters') or []
        caps = ['text']
        if isinstance(inputs, list) and 'image' in inputs:
            caps.append('vision')
        if isinstance(parameters, list) and 'reasoning' in parameters:
            caps.append('thinking')
        recipe = {'model_id': model_id, 'capabilities': caps}
        context = row.get('context_length')
        if template_key == 'cerebras':
            declared = row.get('capabilities') or {}
            if isinstance(declared, dict):
                if declared.get('vision') is True:
                    caps.append('vision')
                if declared.get('reasoning') is True:
                    caps.append('thinking')
            limits = row.get('limits') or {}
            if isinstance(limits, dict):
                context = limits.get('max_context_length')
        if type(context) is int and 0 < context <= 10_000_000:
            recipe['context_window'] = context
        pricing = row.get('pricing')
        if isinstance(pricing, dict) and pricing.get('currency_unit', 'USD') == 'USD':
            prompt, completion = _amount(pricing.get('prompt')), _amount(pricing.get('completion'))
            if (prompt is not None and completion is not None
                    and 0 < prompt + completion <= 1):
                recipe['pricing'] = {
                    'input': round(prompt * 1e6, 6), 'output': round(completion * 1e6, 6),
                    'currency': 'USD', 'unit': 'per_million_tokens',
                }
        try:
            built[model_id] = normalize_model_entry(recipe)
        except ModelRegistrationError:
            continue
        if len(built) >= _MAX_FEED_ROWS_PER_TEMPLATE:
            break
    return {template_key: _unique_logical_seeds(list(built.values()))} if built else {}


def load_cached_feed() -> dict | None:
    """Read a bounded v2 cache; v1's cross-provider inferences are unusable."""
    try:
        with open(_CACHE_PATH, 'rb') as handle:
            raw_bytes = handle.read(_MAX_BYTES + 1)
        if len(raw_bytes) > _MAX_BYTES:
            return None
        raw = json.loads(raw_bytes)
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get('version') != _CACHE_VERSION:
        return None
    templates, sources = raw.get('templates'), raw.get('sources')
    if not isinstance(templates, dict) or not isinstance(sources, dict):
        return None
    cleaned, metadata = {}, {}
    for key, spec in PUBLIC_CATALOGS.items():
        source, rows = sources.get(key), templates.get(key)
        if (not isinstance(source, dict) or source.get('url') != spec['url']
                or not isinstance(rows, list) or not _timestamp(source.get('fetched_at'))):
            continue
        valid = []
        for row in rows[:_MAX_FEED_ROWS_PER_TEMPLATE]:
            try:
                valid.append(normalize_model_entry(row))
            except ModelRegistrationError:
                continue
        if valid:
            cleaned[key] = _unique_logical_seeds(valid)
            metadata[key] = {'url': spec['url'], 'fetched_at': _timestamp(source['fetched_at'])}
    if not cleaned:
        return None
    return {'version': _CACHE_VERSION, 'templates': cleaned, 'sources': metadata}


async def _fetch_rows(stream, spec, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('catalog deadline')
    # Cancellation covers headers and the entire body, including a stream
    # which keeps a socket alive while never completing a full chunk.
    async with asyncio.timeout(remaining):
        async with stream('GET', spec['url'], timeout=min(_FETCH_TIMEOUT_S, remaining),
                          follow_redirects=False, headers={'Accept': 'application/json'}) as response:
            if not 200 <= response.status_code < 300:
                raise ValueError('catalog HTTP failure')
            data = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=65536):
                data.extend(chunk)
                if len(data) > _MAX_BYTES:
                    raise ValueError('catalog byte budget')
            payload = json.loads(data)
            rows = payload.get('data') if isinstance(payload, dict) else None
            if not isinstance(rows, list) or not rows:
                raise ValueError('catalog missing rows')
            return rows


async def _refresh_sources(stream, cached, deadline):
    results = {}
    try:
        for key, spec in PUBLIC_CATALOGS.items():
            if time.monotonic() >= deadline:
                results[key] = 'deadline'
                continue
            try:
                rows = await _fetch_rows(stream, spec, deadline)
                recipes = recipes_from_catalog(rows, template_key=key).get(key)
                if not recipes:
                    raise ValueError('catalog has no usable chat rows')
                cached['templates'][key] = recipes
                cached['sources'][key] = {'url': spec['url'], 'fetched_at': time.time()}
                results[key] = 'ok'
            except Exception as exc:
                logger.warning('[TemplateFeed] %s refresh failed (%s)', key, type(exc).__name__)
                results[key] = 'unavailable'
    finally:
        # This loop belongs to one ephemeral refresh, not the application.
        await close_async_http_clients()
    return results


def refresh_now(http_stream=None, *, max_runtime=_REFRESH_BUDGET_S) -> dict:
    """Refresh each source independently; failures preserve last-good data."""
    global _last_attempt
    if _disabled():
        return {'ok': False, 'disabled': True}
    if not _refresh_lock.acquire(blocking=False):
        return {'ok': False, 'status': 'in_progress'}
    try:
        _last_attempt = time.time()
        deadline = time.monotonic() + min(_REFRESH_BUDGET_S, max(1, max_runtime))
        cached = load_cached_feed() or {'version': _CACHE_VERSION, 'templates': {}, 'sources': {}}
        results = asyncio.run(_refresh_sources(http_stream or async_http_stream, cached, deadline))
        successful = sum(status == 'ok' for status in results.values())
        if successful:
            try:
                ensure_config_dir()
                if len(json.dumps(cached).encode()) > _MAX_BYTES:
                    raise ValueError('catalog cache budget')
                write_json_atomic(_CACHE_PATH, cached)
            except (OSError, ValueError):
                return {'ok': False, 'status': 'cache_write_failed', 'sources': results}
        return {'ok': successful == len(PUBLIC_CATALOGS), 'sources': results,
                'templates': successful, 'models': sum(len(r) for r in cached['templates'].values())}
    finally:
        _refresh_lock.release()


def maybe_refresh_async(feed=None, *, now=None, _spawn=None) -> bool:
    """One ephemeral refresh with a failure backoff; ordinary reads are local."""
    global _refresh_scheduled, _last_attempt
    if _disabled():
        return False
    current = time.time() if now is None else now
    sources = feed.get('sources', {}) if isinstance(feed, dict) else {}
    if all(0 <= current - _timestamp(sources.get(k, {}).get('fetched_at')) < _ttl_seconds()
           for k in PUBLIC_CATALOGS):
        return False
    with _schedule_lock:
        if _refresh_scheduled or _refresh_lock.locked() or current - _last_attempt < _RETRY_INTERVAL_S:
            return False
        _refresh_scheduled = True
        _last_attempt = current

    def run():
        global _refresh_scheduled
        try:
            refresh_now()
        finally:
            with _schedule_lock:
                _refresh_scheduled = False

    spawn = _spawn or (lambda target: threading.Thread(
        target=target, daemon=True, name='provider-template-feed').start())
    try:
        spawn(run)
    except Exception:
        with _schedule_lock:
            _refresh_scheduled = False
        return False
    return True


def merge_feed_recipes(templates, feed, *, now=None, excluded_keys=()):
    """Replace public-source seeds, never another provider's offerings or user state.

    When a complete public catalog is usable, disappeared seeds are not kept
    indefinitely. Existing per-provider rpm/capability hints survive only for
    exact IDs the source still lists. The feed's own prices/context win.
    """
    if _disabled() or not isinstance(feed, dict) or feed.get('version') != _CACHE_VERSION:
        return templates
    current = time.time() if now is None else now
    merged = []
    for template in templates:
        key = template['key']
        spec = PUBLIC_CATALOGS.get(key)
        source = feed.get('sources', {}).get(key, {})
        rows = feed.get('templates', {}).get(key)
        if (key in excluded_keys or not spec
                or template.get('base_url', '').rstrip('/') != spec['base_url']
                or source.get('url') != spec['url'] or not rows
                or not 0 <= current - _timestamp(source.get('fetched_at')) <= _MAX_STALE_SECONDS):
            merged.append(template)
            continue
        authored = {r['model_id']: r for r in template.get('offering_recipes', [])}
        retired = set(template.get('retired_model_ids', []))
        recipes = []
        for row in rows[:_MAX_FEED_ROWS_PER_TEMPLATE]:
            if row['model_id'] in retired:
                continue
            recipe = copy.deepcopy(row)
            # The live catalog owns the wire name, price and context. Neither
            # absent prices nor removed wire pools may fall back to old seeds.
            recipe['request_ids'] = [row['model_id']]
            hints = authored.get(row['model_id'], {})
            if 'rpm' in hints:
                recipe['rpm'] = hints['rpm']
            if recipe.get('capabilities') == ['text'] and hints.get('capabilities'):
                recipe['capabilities'] = copy.deepcopy(hints['capabilities'])
            recipes.append(recipe)
        out = copy.deepcopy(template)
        out['offering_recipes'] = recipes
        out['catalog_feed'] = {'url': spec['url'], 'fetched_at': source['fetched_at'],
                               'stale': current - source['fetched_at'] >= _ttl_seconds()}
        merged.append(out)
    return merged


__all__ = ['PUBLIC_CATALOGS', 'load_cached_feed', 'maybe_refresh_async',
           'merge_feed_recipes', 'recipes_from_catalog', 'refresh_now']
