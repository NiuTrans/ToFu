"""lib/llm_dispatch/health_local.py — Background health checker for local endpoints.

Self-hosted vLLM / SGLang / Ollama boxes have no SLA — they restart, swap
models, and die. This module runs a small background thread that, for every
provider tagged as a local endpoint:

  1. Probes ``{endpoint}/models`` every ``HEALTH_INTERVAL`` seconds for each
     URL in the provider's ``endpoints`` list (or the single ``base_url``
     when no list is configured).
  2. On failure → cools down only the slots whose ``base_url`` matches the
     dead endpoint, so a single sick node doesn't take the whole fleet
     offline.
  3. On recovery, clears those slots' cooldowns and (if served-model
     drift is detected) re-runs ``discover_models`` on the live endpoints,
     unions the served-model sets, patches ``server_config.json``, and
     rebuilds the dispatcher's slot pool.

Cloud providers are NOT polled — that would waste quota and leak hosts.
"""

import os
import threading
import time

import requests

from lib.http_client import http_get
from lib.log import audit_log, get_logger
from lib.proxy import (
    register_no_proxy_url,
)

from .discovery import (
    discover_models, is_local_endpoint, is_raw_ip_host, normalize_base_url,
)

logger = get_logger(__name__)

__all__ = [
    'start_local_health_checker',
    'check_once',
]

# Tuneable via env so deployments don't have to fork code to dial them in.
HEALTH_INTERVAL = int(os.environ.get('TOFU_LOCAL_HEALTH_INTERVAL', '60'))
PROBE_TIMEOUT = int(os.environ.get('TOFU_LOCAL_HEALTH_TIMEOUT', '4'))
COOLDOWN_ON_DEAD = int(os.environ.get('TOFU_LOCAL_HEALTH_COOLDOWN', '60'))
# How often (in successful health checks) we re-run full model discovery.
RESYNC_EVERY = int(os.environ.get('TOFU_LOCAL_HEALTH_RESYNC', '10'))

_thread = None
_stop_event = threading.Event()
# Per (provider_id, endpoint_url) counter so RESYNC_EVERY is local to
# the box, not global.
_success_streak: dict[tuple, int] = {}


def _provider_endpoints(prov: dict) -> list:
    """Return the normalized URL list for a provider (multi-endpoint aware)."""
    raw = prov.get('endpoints') or []
    urls = []
    seen = set()
    if isinstance(raw, list):
        for u in raw:
            if not isinstance(u, str):
                continue
            n = normalize_base_url(u.strip())
            if n and n not in seen:
                seen.add(n)
                urls.append(n)
    if not urls and prov.get('base_url'):
        n = normalize_base_url(prov['base_url'])
        if n:
            urls.append(n)
    return urls


def _get_dispatcher():
    """Return the live dispatcher singleton, or None if not yet built."""
    try:
        from lib.llm_dispatch.factory import get_dispatcher
        return get_dispatcher()
    except Exception as e:
        logger.debug('[HealthLocal] Dispatcher unavailable: %s', e)
        return None


def _cooldown_endpoint_slots(prov_id: str, endpoint_url: str, seconds: int) -> int:
    disp = _get_dispatcher()
    if not disp:
        return 0
    deadline = time.time() + seconds
    target = endpoint_url.rstrip('/')
    n = 0
    for slot in list(disp.slots):
        if slot.provider_id != prov_id:
            continue
        if (slot.base_url or '').rstrip('/') != target:
            continue
        with slot._lock:
            if slot.cooldown_until < deadline:
                slot.cooldown_until = deadline
                n += 1
    return n


def _clear_endpoint_cooldowns(prov_id: str, endpoint_url: str) -> int:
    disp = _get_dispatcher()
    if not disp:
        return 0
    target = endpoint_url.rstrip('/')
    n = 0
    now = time.time()
    for slot in list(disp.slots):
        if slot.provider_id != prov_id:
            continue
        if (slot.base_url or '').rstrip('/') != target:
            continue
        with slot._lock:
            if slot.cooldown_until > now:
                slot.cooldown_until = 0.0
                slot.consecutive_errors = 0
                n += 1
    return n


def _ephemeral_slots_by_endpoint() -> dict:
    """Group live ephemeral/BYO slots by their (base_url, api_key).

    Ephemeral slots are injected straight into the dispatcher (not present
    in server_config.json), so the config-driven provider sweep can't see
    them. Returns ``{base_url: api_key}`` for every slot whose provider_id
    is tagged ``ephemeral:…``. Only self-hosted / raw-IP endpoints are
    included — cloud BYO endpoints have their own SLA and polling them
    would leak host names + waste round-trips.
    """
    disp = _get_dispatcher()
    if not disp:
        return {}
    out: dict = {}
    for slot in list(disp.slots):
        if not (slot.provider_id or '').startswith('ephemeral:'):
            continue
        base_url = (slot.base_url or '').rstrip('/')
        if not base_url:
            continue
        if not is_local_endpoint(base_url) and not is_raw_ip_host(base_url):
            continue
        # First slot's key wins; a homogeneous endpoint shares one key.
        out.setdefault(base_url, slot.api_key or '')
    return out


def _cooldown_ephemeral_endpoint(endpoint_url: str, seconds: int) -> int:
    """Cool down all ephemeral slots whose base_url matches *endpoint_url*."""
    disp = _get_dispatcher()
    if not disp:
        return 0
    deadline = time.time() + seconds
    target = endpoint_url.rstrip('/')
    n = 0
    for slot in list(disp.slots):
        if not (slot.provider_id or '').startswith('ephemeral:'):
            continue
        if (slot.base_url or '').rstrip('/') != target:
            continue
        with slot._lock:
            if slot.cooldown_until < deadline:
                slot.cooldown_until = deadline
                n += 1
    return n


def _clear_ephemeral_endpoint(endpoint_url: str) -> int:
    """Clear cooldown on ephemeral slots whose base_url matches *endpoint_url*."""
    disp = _get_dispatcher()
    if not disp:
        return 0
    target = endpoint_url.rstrip('/')
    n = 0
    now = time.time()
    for slot in list(disp.slots):
        if not (slot.provider_id or '').startswith('ephemeral:'):
            continue
        if (slot.base_url or '').rstrip('/') != target:
            continue
        with slot._lock:
            if slot.cooldown_until > now:
                slot.cooldown_until = 0.0
                slot.consecutive_errors = 0
                n += 1
    return n


def _check_ephemeral_endpoints() -> dict:
    """Health-check live ephemeral/BYO self-hosted endpoints.

    Mirrors the provider sweep but for slots injected via
    ``mint_ephemeral_slot``. Cools down slots whose endpoint is dead so
    the dispatcher routes around them, and clears the cooldown when the
    box recovers. No model re-discovery — ephemeral slots carry a fixed
    caller-declared model_id. Returns ``{endpoints_ok, cooldowns}``.
    """
    endpoints = _ephemeral_slots_by_endpoint()
    if not endpoints:
        return {'endpoints_ok': 0, 'cooldowns': 0}
    n_ok = 0
    n_cool = 0
    for endpoint, api_key in endpoints.items():
        result = _check_endpoint(endpoint, api_key)
        if result['ok']:
            n_ok += 1
            cleared = _clear_ephemeral_endpoint(endpoint)
            if cleared:
                logger.info('[HealthLocal] ephemeral %s recovered — '
                            'cleared %d cooldown(s)', endpoint, cleared)
                audit_log('local_endpoint_recovered', provider_id='ephemeral',
                          endpoint=endpoint)
        else:
            cooled = _cooldown_ephemeral_endpoint(endpoint, COOLDOWN_ON_DEAD)
            if cooled:
                n_cool += cooled
                logger.warning('[HealthLocal] ephemeral %s %s — cooled %d slot(s)',
                               endpoint, result['status'], cooled)
                audit_log('local_endpoint_down', provider_id='ephemeral',
                          endpoint=endpoint, reason=result['status'])
    return {'endpoints_ok': n_ok, 'cooldowns': n_cool}


def _persist_provider_models(prov_id: str, models: list[dict],
                             endpoint_models: dict | None = None,
                             endpoints: list | None = None) -> bool:
    """Update server_config.json with refreshed model state for one provider.

    Persists the model list and — when given — the per-endpoint
    served-model binding (``endpoint_models``) and the (possibly
    /v1-normalized) endpoint URL list.

    Uses ``update_json_atomic`` so this read-modify-write is serialised
    against the other concurrent writers of this shared file. The provider
    is re-found in the FRESH on-disk config under the lock, so a concurrent
    Settings save that just added/removed a provider is not clobbered.
    Returns True iff the provider was found and its models persisted.
    """
    try:
        from lib import _SERVER_CONFIG_PATH
        from lib.json_store import update_json_atomic

        found = {'ok': False}

        def _mutate(cfg):
            if not isinstance(cfg, dict):
                return None
            for p in (cfg.get('providers') or []):
                if p.get('id') == prov_id:
                    p['models'] = models
                    if endpoint_models is not None:
                        p['endpoint_models'] = endpoint_models
                    if endpoints is not None:
                        p['endpoints'] = endpoints
                        p['base_url'] = endpoints[0] if endpoints else p.get('base_url', '')
                    found['ok'] = True
                    return cfg
            return None  # provider gone — no write

        update_json_atomic(_SERVER_CONFIG_PATH, _mutate, default={})
        return found['ok']
    except Exception as e:
        logger.warning('[HealthLocal] Failed to persist models for %s: %s',
                       prov_id, e, exc_info=True)
        return False


def _rebuild_dispatcher_slots():
    """Re-create the slot pool from the (now-updated) server_config.json.

    Cheaper than restarting the process — slot stats reset, but for a
    box that just came back up that's actually what we want.
    """
    disp = _get_dispatcher()
    if not disp:
        return
    try:
        with disp._lock:
            disp.slots.clear()
            disp._initialized = False
        disp.initialize()
        logger.info('[HealthLocal] Rebuilt dispatcher: %d slots', len(disp.slots))
    except Exception as e:
        logger.error('[HealthLocal] Slot rebuild failed: %s', e, exc_info=True)


def _check_endpoint(endpoint_url: str, api_key: str) -> dict:
    """Probe a single endpoint's /models.

    Returns ``{ok, status, served_models, effective_url}``. A bare-origin
    URL answering /models with a plain 404 is retried once under ``/v1``
    (the ollama ``host:11434`` habit); ``effective_url`` carries the URL
    that actually worked so binding keys match the dispatcher's
    normalized endpoints.
    """
    if not endpoint_url:
        return {'ok': False, 'status': 'no-url'}

    headers = {'User-Agent': 'Tofu/1.0'}
    if api_key:
        headers['Authorization'] = 'Bearer %s' % api_key

    # Self-hosted endpoints often live on a private/pseudo-private IP that
    # corp proxies can't reach.  Make sure the host is bypassed.
    register_no_proxy_url(endpoint_url)

    def _get(url):
        """Single GET → (resp, None) or (None, status_str)."""
        try:
            return http_get(url, headers=headers, timeout=PROBE_TIMEOUT), None
        except requests.Timeout as e:
            logger.debug('[health_local] _get caught %s: %s', type(e).__name__, e)
            return None, 'timeout'
        except requests.RequestException as e:
            logger.debug('[health_local] _get caught %s: %s', type(e).__name__, e)
            return None, 'unreachable: %s' % e

    base = endpoint_url.rstrip('/')
    resp, err = _get(base + '/models')
    if err is not None:
        return {'ok': False, 'status': err}

    effective = base
    if not resp.ok and resp.status_code == 404:
        from urllib.parse import urlparse
        if urlparse(base).path in ('', '/'):
            resp2, err2 = _get(base + '/v1/models')
            if err2 is None and resp2.ok:
                resp = resp2
                effective = base + '/v1'
                logger.info('[HealthLocal] %s /models 404 — fell back to /v1', base)

    if not resp.ok:
        return {'ok': False, 'status': 'http-%d' % resp.status_code}

    try:
        data = resp.json()
    except (ValueError, TypeError) as e:
        logger.debug('[health_local] _check_endpoint caught %s: %s', type(e).__name__, e)
        return {'ok': False, 'status': 'bad-json: %s' % e}

    served = []
    for m in (data.get('data') or []):
        mid = (m.get('id') or '').strip()
        if mid:
            served.append(mid)
    return {'ok': True, 'status': 'ok', 'served_models': set(served),
            'effective_url': effective}


def check_once() -> dict:
    """Run one pass over all local providers. Returns a stats dict for testing."""
    try:
        from lib import _load_server_config
    except Exception as e:
        logger.debug('[HealthLocal] Cannot load server config: %s', e)
        return {'providers': 0, 'endpoints_ok': 0, 'cooldowns': 0, 'resynced': 0}

    cfg = _load_server_config()
    providers = cfg.get('providers') or []
    locals_ = []
    for p in providers:
        if not p.get('enabled', True):
            continue
        if p.get('brand') == 'local':
            locals_.append(p)
            continue
        # Legacy heuristic — pre-brand-tag local providers.
        for url in _provider_endpoints(p):
            if is_local_endpoint(url):
                locals_.append(p)
                break

    if not locals_:
        # No config-driven local providers, but ephemeral/BYO slots may
        # still need health-checking — sweep them before returning.
        eph = _check_ephemeral_endpoints()
        return {'providers': 0,
                'endpoints_ok': eph['endpoints_ok'],
                'cooldowns': eph['cooldowns'], 'resynced': 0}

    n_endpoints_ok = 0
    n_cooldown = 0
    n_resynced = 0
    rebuilt = False

    for prov in locals_:
        prov_id = prov.get('id') or 'unknown'
        endpoints = _provider_endpoints(prov)
        if not endpoints:
            continue

        api_key = (prov.get('api_keys') or [''])[0]
        configured_ids = {m.get('model_id') for m in (prov.get('models') or [])
                          if m.get('model_id')}

        live_endpoints = []
        per_ep_served: dict = {}
        effective_of: dict = {}
        union_served: set = set()
        any_ok = False

        for endpoint in endpoints:
            result = _check_endpoint(endpoint, api_key)
            streak_key = (prov_id, endpoint)

            if not result['ok']:
                _success_streak[streak_key] = 0
                cooled = _cooldown_endpoint_slots(prov_id, endpoint, COOLDOWN_ON_DEAD)
                if cooled:
                    n_cooldown += cooled
                    logger.warning('[HealthLocal] %s @ %s %s — cooled %d slot(s)',
                                   prov_id, endpoint, result['status'], cooled)
                    audit_log('local_endpoint_down', provider_id=prov_id,
                              endpoint=endpoint, reason=result['status'])
                continue

            any_ok = True
            n_endpoints_ok += 1
            ep_key = result.get('effective_url') or endpoint
            effective_of[endpoint] = ep_key
            live_endpoints.append(ep_key)
            served = result['served_models']
            per_ep_served[ep_key] = served
            union_served |= served

            cleared = _clear_endpoint_cooldowns(prov_id, endpoint)
            if cleared:
                logger.info('[HealthLocal] %s @ %s recovered — cleared %d cooldown(s)',
                            prov_id, endpoint, cleared)
                audit_log('local_endpoint_recovered', provider_id=prov_id,
                          endpoint=endpoint)
            _success_streak[streak_key] = _success_streak.get(streak_key, 0) + 1

        if not any_ok:
            continue

        # Trigger re-discovery when the configured-set drifts from the
        # union of served sets, when per-endpoint PLACEMENT drifts from the
        # persisted binding (a model moved boxes — union alone can't see
        # that), or once every RESYNC_EVERY successful cycles.
        old_binding = {}
        for bk, bv in (prov.get('endpoint_models') or {}).items():
            if isinstance(bk, str) and isinstance(bv, list):
                old_binding[normalize_base_url(bk.strip())] = sorted(
                    x for x in bv if isinstance(x, str) and x)
        binding_drift = any(
            old_binding.get(ep) != sorted(per_ep_served[ep])
            for ep in live_endpoints
        )
        max_streak = max((_success_streak.get((prov_id, e), 0)
                          for e in live_endpoints), default=0)
        needs_resync = (
            not configured_ids
            or union_served != configured_ids
            or binding_drift
            or (max_streak % RESYNC_EVERY == 0)
        )
        if not needs_resync:
            continue

        # ── Per-endpoint re-discovery (heterogeneous-fleet safe) ──
        # Each model's metadata comes from the endpoint that ACTUALLY
        # serves it. The pre-binding code discovered from live_endpoints[0]
        # alone and union-filtered, which silently dropped every model
        # hosted on the other boxes (the picker-flap bug).
        existing_by_id = {m.get('model_id'): m
                          for m in (prov.get('models') or [])
                          if m.get('model_id')}
        new_binding: dict = {}
        merged: dict = {}
        order: list = []
        for ep in live_endpoints:
            try:
                ep_models = discover_models(ep, api_key)
            except Exception as e:
                logger.warning('[HealthLocal] Discovery failed for %s: %s',
                               ep, e, exc_info=True)
                ep_models = []
            if not ep_models:
                # The health probe succeeded but full discovery failed —
                # keep the check-derived placement + existing metadata
                # rather than writing a spurious empty binding.
                ids = sorted(per_ep_served.get(ep) or [])
                for mid in ids:
                    if mid not in merged and mid in existing_by_id:
                        merged[mid] = existing_by_id[mid]
                        order.append(mid)
                new_binding[ep] = ids
                continue
            ids = []
            for m in ep_models:
                mid = m['model_id']
                ids.append(mid)
                if mid not in merged:
                    merged[mid] = m
                    order.append(mid)
            new_binding[ep] = sorted(ids)

        # A transiently-DOWN endpoint keeps its previous binding and its
        # models (restarting a box must not wipe the picker).
        for ep in endpoints:
            if ep in effective_of:
                continue
            prev = old_binding.get(ep)
            if prev:
                new_binding[ep] = prev
                for mid in prev:
                    if mid not in merged and mid in existing_by_id:
                        merged[mid] = existing_by_id[mid]
                        order.append(mid)

        # Preserve user-set per-model flags (enabled toggle, custom rpm/cost)
        # across re-discovery — discover_models() returns a fresh list with no
        # knowledge of what the user toggled in Settings.
        for mid, m in merged.items():
            prev = existing_by_id.get(mid)
            if prev is not None and prev.get('enabled') is False:
                m['enabled'] = False

        filtered = [merged[mid] for mid in order]

        new_ids = {m['model_id'] for m in filtered}
        new_endpoints = [effective_of.get(ep, ep) for ep in endpoints]
        if (new_ids == configured_ids
                and new_binding == old_binding
                and new_endpoints == endpoints):
            continue  # zero drift — rewriting would just churn the slot pool

        if _persist_provider_models(prov_id, filtered,
                                    endpoint_models=new_binding,
                                    endpoints=new_endpoints):
            n_resynced += 1
            rebuilt = True
            added = sorted(new_ids - configured_ids)
            removed = sorted(configured_ids - new_ids)
            logger.info('[HealthLocal] Provider %s model state updated '
                        '(+%d / -%d models, %d bound endpoints): added=%s removed=%s',
                        prov_id, len(added), len(removed), len(new_binding),
                        added[:5], removed[:5])
            audit_log('local_endpoint_models_updated', provider_id=prov_id,
                      added=added, removed=removed)

    if rebuilt:
        _rebuild_dispatcher_slots()

    # Ephemeral/BYO self-hosted slots aren't in server_config — sweep them
    # in the same cycle so they get the same cool-on-dead / clear-on-recover
    # treatment as configured local providers.
    eph = _check_ephemeral_endpoints()

    return {
        'providers': len(locals_),
        'endpoints_ok': n_endpoints_ok + eph['endpoints_ok'],
        'cooldowns': n_cooldown + eph['cooldowns'],
        'resynced': n_resynced,
    }


def _loop():
    logger.info('[HealthLocal] Worker started (interval=%ds, timeout=%ds)',
                HEALTH_INTERVAL, PROBE_TIMEOUT)
    if _stop_event.wait(5):
        return
    while not _stop_event.is_set():
        try:
            check_once()
        except Exception as e:
            logger.error('[HealthLocal] Cycle failed: %s', e, exc_info=True)
        if _stop_event.wait(HEALTH_INTERVAL):
            break
    logger.info('[HealthLocal] Worker stopped')


def start_local_health_checker():
    """Idempotent: spawn the background health-check thread (no-op if running)."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_loop, name='local-health', daemon=True)
    _thread.start()
