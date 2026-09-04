"""lib/llm_dispatch/health_local.py — Background health checker for local endpoints.

Self-hosted vLLM / SGLang / Ollama boxes have no SLA — they restart, swap
models, and die. This module owns the single local-endpoint monitor thread.
For every owner-scoped v2 ProviderAccess tagged as local it:

  1. Probes each enabled local Connection every ``HEALTH_INTERVAL`` seconds.
  2. On failure → cools down only the slots whose ``base_url`` matches the
     dead endpoint, so a single sick node doesn't take the whole fleet
     offline.
  3. On recovery, clears those slots' cooldowns and (for deterministic auto/
     managed bundles) refreshes observed models through the same revision-CAS
     registration service that created the ProviderAccess.

Cloud providers are NOT polled — that would waste quota and leak hosts. The
same cancellable loop also schedules the well-known-port discovery job in
``autodiscover_local.py``; discovery no longer retains a second idle thread.
"""

import os
from dataclasses import dataclass
import threading
import time

import requests

from lib.http_client import http_get
from lib.log import audit_log, get_logger
from lib.model_routing import (
    ModelRoutingError,
    OwnerBoundary,
    upsert_local_provider,
)
from lib.proxy import (
    register_no_proxy_url,
)

from .discovery import (
    discover_models, is_local_endpoint, is_raw_ip_host, normalize_base_url,
)

logger = get_logger(__name__)

__all__ = [
    'start_local_health_checker',
    'stop_local_health_checker',
    'wake_local_health_checker',
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
_wake_event = threading.Event()
_thread_lock = threading.Lock()
# Per (provider_id, endpoint_url) counter so RESYNC_EVERY is local to
# the box, not global.
_success_streak: dict[tuple, int] = {}


@dataclass(frozen=True, slots=True)
class _LocalHealthTarget:
    provider_id: str
    display_name: str
    endpoints: tuple[str, ...]
    configured_model_ids: frozenset[str]


def _local_health_targets(document: dict) -> list[_LocalHealthTarget]:
    """Project only monitorable, credential-free local v2 resources."""
    providers = {
        row['provider_id']: row for row in document.get('providers', [])
    }
    accesses = {
        row['provider_access_id']: row
        for row in document.get('provider_accesses', [])
        if row.get('enabled') is True
    }
    # Background probing must not resolve or retain secrets. A local-identity
    # credential explicitly proves that its authorized Connections are safe to
    # probe without an Authorization header.
    credential_free_connections = {
        connection_id
        for row in document.get('credentials', [])
        if row.get('enabled') is True and row.get('kind') == 'local_identity'
        for connection_id in (row.get('authorization') or {}).get(
            'connection_ids', [])
    }
    connections_by_access: dict[str, list[dict]] = {}
    for row in document.get('connections', []):
        access_id = row.get('provider_access_id')
        if (
            row.get('enabled') is True
            and access_id in accesses
            and row.get('connection_id') in credential_free_connections
        ):
            connections_by_access.setdefault(access_id, []).append(row)
    offerings = {
        row['offering_id']: row
        for row in document.get('offerings', [])
        if row.get('enabled') is True and not row.get('stale')
    }
    models_by_access: dict[str, set[str]] = {}
    for deployment in document.get('deployments', []):
        offering = offerings.get(deployment.get('offering_id'))
        if offering is None:
            continue
        model_id = (
            offering.get('pending_model_id')
            if offering.get('identity_state') == 'pending_identity'
            else (offering.get('model') or {}).get('model_id')
        )
        if model_id:
            models_by_access.setdefault(
                offering['provider_access_id'], set()).add(model_id)

    targets: list[_LocalHealthTarget] = []
    for access_id, rows in connections_by_access.items():
        access = accesses[access_id]
        provider = providers.get(access['provider_id'])
        if provider is None:
            continue
        endpoints = tuple(sorted({
            normalize_base_url(row['base_url'])
            for row in rows
            if is_local_endpoint(row['base_url'])
            or is_raw_ip_host(row['base_url'])
        }))
        if not endpoints:
            continue
        if provider.get('brand') != 'local' and not all(
            is_local_endpoint(endpoint) for endpoint in endpoints
        ):
            continue
        targets.append(_LocalHealthTarget(
            provider_id=provider['provider_id'],
            display_name=(access.get('display_name') or provider['name']),
            endpoints=endpoints,
            configured_model_ids=frozenset(
                models_by_access.get(access_id, set())),
        ))
    return sorted(targets, key=lambda target: target.provider_id)


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
    in the durable model-routing v2 authority), so the authority-driven sweep can't see
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


def _rebuild_dispatcher_slots():
    """Re-create the slot pool from the updated v2 authority.

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


def check_once(*, boundary: OwnerBoundary | None = None, repository=None) -> dict:
    """Run one owner-scoped pass over credential-free local Connections."""
    if boundary is None or repository is None:
        from .autodiscover_local import _runtime_context

        runtime_context = _runtime_context()
        if runtime_context is None:
            return {
                'providers': 0, 'endpoints_ok': 0, 'cooldowns': 0,
                'resynced': 0, 'owner_enumeration_required': True,
            }
        runtime_boundary, runtime_repository = runtime_context
        boundary = boundary or runtime_boundary
        repository = repository or runtime_repository
    try:
        authority = repository.get(boundary)
    except ModelRoutingError as exc:
        logger.warning('[HealthLocal] Cannot load model-routing authority: %s', exc)
        return {
            'providers': 0, 'endpoints_ok': 0, 'cooldowns': 0,
            'resynced': 0, 'authority_unavailable': True,
        }
    targets = (
        _local_health_targets(authority.document)
        if authority.revision > 0 else []
    )

    n_endpoints_ok = 0
    n_cooldown = 0
    n_resynced = 0
    rebuilt = False
    for target in targets:
        for endpoint in target.endpoints:
            result = _check_endpoint(endpoint, '')
            streak_key = (target.provider_id, endpoint)
            if not result['ok']:
                _success_streak[streak_key] = 0
                cooled = _cooldown_endpoint_slots(
                    target.provider_id, endpoint, COOLDOWN_ON_DEAD)
                if cooled:
                    n_cooldown += cooled
                    logger.warning(
                        '[HealthLocal] %s @ %s %s — cooled %d slot(s)',
                        target.provider_id, endpoint, result['status'], cooled)
                    audit_log(
                        'local_endpoint_down',
                        provider_id=target.provider_id,
                        endpoint=endpoint,
                        reason=result['status'],
                        owner_user_id=boundary.owner_user_id,
                    )
                continue

            n_endpoints_ok += 1
            effective = result.get('effective_url') or endpoint
            served_ids = set(result['served_models'])
            cleared = _clear_endpoint_cooldowns(target.provider_id, endpoint)
            if cleared:
                logger.info(
                    '[HealthLocal] %s @ %s recovered — cleared %d cooldown(s)',
                    target.provider_id, endpoint, cleared)
                audit_log(
                    'local_endpoint_recovered',
                    provider_id=target.provider_id,
                    endpoint=endpoint,
                    owner_user_id=boundary.owner_user_id,
                )
            _success_streak[streak_key] = _success_streak.get(streak_key, 0) + 1

            # Auto/managed providers are deterministic products of discovery.
            # User-authored v2 Providers may have deliberate identity/pricing/
            # enablement edits, so background health observes but never
            # rewrites them. A multi-Connection bundle likewise requires an
            # explicit placement decision because one wire ID has one factual
            # Deployment in v2.
            managed = target.provider_id.startswith(('auto_', 'managed_'))
            periodic = _success_streak[streak_key] % max(1, RESYNC_EVERY) == 0
            if (
                not managed
                or len(target.endpoints) != 1
                or (
                    served_ids == set(target.configured_model_ids)
                    and effective == endpoint
                    and not periodic
                )
            ):
                continue
            try:
                models = discover_models(effective, '')
            except Exception as exc:
                logger.warning(
                    '[HealthLocal] Discovery failed for %s: %s',
                    effective, exc, exc_info=True)
                continue
            if not models:
                # A successful cheap probe plus a failed full discovery is not
                # evidence that configured models vanished.
                continue
            try:
                mutation = upsert_local_provider(
                    repository,
                    boundary,
                    provider_id=target.provider_id,
                    display_name=target.display_name,
                    base_url=effective,
                    models=models,
                )
            except ModelRoutingError as exc:
                logger.warning(
                    '[HealthLocal] v2 refresh failed for %s: %s',
                    target.provider_id, exc, exc_info=True)
                continue
            if not mutation.changed:
                continue
            n_resynced += 1
            rebuilt = True
            new_ids = {model['model_id'] for model in models}
            added = sorted(new_ids - set(target.configured_model_ids))
            removed = sorted(set(target.configured_model_ids) - new_ids)
            logger.info(
                '[HealthLocal] Provider %s model state updated '
                '(+%d / -%d): added=%s removed=%s',
                target.provider_id, len(added), len(removed),
                added[:5], removed[:5])
            audit_log(
                'local_endpoint_models_updated',
                provider_id=target.provider_id,
                added=added,
                removed=removed,
                owner_user_id=boundary.owner_user_id,
                model_routing_revision=mutation.authority.revision,
            )

    if rebuilt:
        _rebuild_dispatcher_slots()

    # Request-owned ephemeral/BYO slots remain outside durable model-routing
    # and receive only process-local endpoint cooldown accounting.
    ephemeral = _check_ephemeral_endpoints()
    return {
        'providers': len(targets),
        'endpoints_ok': n_endpoints_ok + ephemeral['endpoints_ok'],
        'cooldowns': n_cooldown + ephemeral['cooldowns'],
        'resynced': n_resynced,
    }


def _loop():
    logger.info(
        '[HealthLocal] local-endpoint monitor started '
        '(health_interval=%ds, timeout=%ds)',
        HEALTH_INTERVAL, PROBE_TIMEOUT)
    next_health_at = time.monotonic() + 5.0
    while not _stop_event.is_set():
        now = time.monotonic()
        if now >= next_health_at:
            try:
                check_once()
            except Exception as e:
                logger.error('[HealthLocal] Cycle failed: %s', e, exc_info=True)
            next_health_at = time.monotonic() + max(1.0, HEALTH_INTERVAL)

        discovery_delay = None
        try:
            from .autodiscover_local import poll_if_due
            discovery_stats = poll_if_due(now=now)
            raw_delay = discovery_stats.get('next_poll_s')
            if raw_delay is not None:
                discovery_delay = max(0.0, float(raw_delay))
        except Exception as e:
            # Local-provider health must continue even if the optional
            # well-known-port pass has a programmer or network failure.
            logger.error('[AutoDiscover] sweep failed: %s', e, exc_info=True)

        now = time.monotonic()
        waits = [max(0.0, next_health_at - now)]
        if discovery_delay is not None:
            waits.append(discovery_delay)
        timeout = max(0.01, min(waits))
        if _wake_event.wait(timeout):
            _wake_event.clear()
    logger.info('[HealthLocal] local-endpoint monitor stopped')


def wake_local_health_checker() -> bool:
    """Wake the shared monitor after an explicit local/provider change."""
    with _thread_lock:
        thread = _thread
    if thread is None or not thread.is_alive():
        return False
    _wake_event.set()
    return True


def start_local_health_checker() -> bool:
    """Idempotently start the shared local health/discovery monitor."""
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return False
        _stop_event.clear()
        _wake_event.clear()
        _thread = threading.Thread(
            target=_loop, name='local-endpoint-monitor', daemon=True)
        _thread.start()
    return True


def stop_local_health_checker(timeout: float = 2.0) -> bool:
    """Signal and bounded-join the monitor, retaining a live owner on timeout."""
    global _thread
    _stop_event.set()
    _wake_event.set()
    with _thread_lock:
        thread = _thread
    if thread is None:
        return True
    try:
        wait_seconds = max(0.0, float(timeout))
    except (TypeError, ValueError, OverflowError) as exc:
        logger.debug('[HealthLocal] invalid stop timeout; using 2.0: %s', exc)
        wait_seconds = 2.0
    if thread is not threading.current_thread():
        thread.join(timeout=wait_seconds)
    if thread.is_alive():
        return False
    with _thread_lock:
        if _thread is thread:
            _thread = None
    return True
