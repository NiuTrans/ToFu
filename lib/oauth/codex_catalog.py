"""Dynamic ChatGPT subscription model catalogue.

Codex CLI does not build its ``/model`` picker from the public
``GET /v1/models`` API.  It reads the authenticated ChatGPT Codex catalogue,
caches the last good response, and refreshes it in the background.  This
module gives Tofu's managed ``oauth_codex`` provider the same lifecycle while
keeping the static table in :mod:`lib.oauth.outbound` as the cold-start
fallback.
"""

from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from lib.config_dir import config_path
from lib.json_store import read_json, write_json_atomic
from lib.log import get_logger
from lib.oauth.codex_version import codex_client_version

logger = get_logger(__name__)

CODEX_CATALOG_TTL_S = 3600
CODEX_CATALOG_REFRESH_INTERVAL_S = 180
CODEX_CATALOG_MAX_REFRESH_INTERVAL_S = 3600
CODEX_CATALOG_TIMEOUT_S = 5
CODEX_REGISTRATION_RETRY_INTERVAL_S = 3600
CODEX_REGISTRATION_MAX_RUNTIME_S = 90

_CACHE_SCHEMA_VERSION = 1
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024

_refresh_lock = threading.Lock()
_state_lock = threading.Lock()
_refresh_wake = threading.Event()
_worker_stop = threading.Event()
_worker_thread = None
_worker_started = False
_last_error = ''

_staged_catalog = threading.local()  # visible only to the provisioning thread
_pending_catalog: dict | None = None  # guarded by _refresh_lock

def _cache_path() -> str:
    return config_path('oauth', 'codex_models_cache.json')


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _account_fingerprint(stored: dict | None = None) -> str:
    if stored is None:
        from lib.oauth.token_store import load_token
        stored = load_token('codex') or {}
    account_id = str((stored or {}).get('account_id') or '').strip()
    if not account_id:
        return ''
    return hashlib.sha256(account_id.encode()).hexdigest()[:16]


def _normalise_models(payload: dict) -> list[dict]:
    """Validate and retain the model fields consumed by Tofu's projection."""
    raw_models = payload.get('models') if isinstance(payload, dict) else None
    if not isinstance(raw_models, list):
        raise ValueError('Codex model catalogue has no models array')

    if len(raw_models) > 2000:
        raise ValueError('Codex model catalogue exceeds model budget')

    rows = []
    seen = set()
    for index, raw in enumerate(raw_models):
        if not isinstance(raw, dict):
            continue
        slug = str(raw.get('slug') or '').strip()
        if not slug or slug in seen:
            continue
        seen.add(slug)

        modalities = []
        for modality in raw.get('input_modalities') or []:
            modality = str(modality or '').strip().lower()
            if modality and modality not in modalities:
                modalities.append(modality)
        if not modalities:
            modalities = ['text']

        reasoning_levels = []
        for level in raw.get('supported_reasoning_levels') or []:
            if not isinstance(level, dict):
                continue
            effort = str(level.get('effort') or '').strip().lower()
            if not effort or any(x['effort'] == effort
                                 for x in reasoning_levels):
                continue
            item = {'effort': effort}
            description = str(level.get('description') or '').strip()
            if description:
                item['description'] = description
            reasoning_levels.append(item)

            if len(reasoning_levels) > 32:
                raise ValueError('Codex reasoning levels exceed profile budget')

        try:
            priority = int(raw.get('priority', index))
        except (TypeError, ValueError) as exc:
            logger.debug('[CodexCatalog] invalid priority for %s: %s', slug, exc)
            priority = index
        visibility = str(raw.get('visibility') or 'list').strip().lower()
        rows.append({
            'slug': slug,
            'display_name': str(raw.get('display_name') or slug).strip(),
            'description': str(raw.get('description') or '').strip(),
            'visibility': 'list' if visibility == 'list' else 'hide',
            'priority': priority,
            'supported_in_api': bool(raw.get('supported_in_api', False)),
            'default_reasoning_level': str(
                raw.get('default_reasoning_level') or '').strip().lower(),
            'supported_reasoning_levels': reasoning_levels,
            'input_modalities': modalities,
        })
        from lib.model_routing.offering_metadata import declared_context_window
        window = declared_context_window(raw)
        rows[-1]['inherited_model_fields'] = list(raw.get('inherited_model_fields', []))
        if window is not None:
            rows[-1]['context_window'] = window
        elif 'context_window' not in rows[-1]['inherited_model_fields']:
            rows[-1]['inherited_model_fields'].append('context_window')

    if not rows:
        raise ValueError('Codex model catalogue is empty after validation')
    rows.sort(key=lambda row: (row['priority'], row['slug']))
    return rows


def _read_cache(*, current_account: bool = True) -> dict:
    cached = read_json(_cache_path(), default={}) or {}
    if cached.get('schema_version') != _CACHE_SCHEMA_VERSION:
        return {}
    account_degraded = False
    if current_account:
        wanted = _account_fingerprint()
        cached_account = str(cached.get('account_fingerprint') or '')
        # Reject only on a REAL account switch (both fingerprints known and
        # different). An EMPTY current fingerprint means the token is
        # expired/cleared/unreadable — auth is temporarily down, the account
        # did not change. Zeroing the catalogue there collapses the provider
        # to the static table and hides the subscription's real models for
        # the whole outage; degrading to the stale cache keeps them visible.
        if wanted and cached_account and wanted != cached_account:
            return {}
        if not wanted and cached_account:
            account_degraded = True
    try:
        models = _normalise_models(cached)
    except ValueError as exc:
        logger.debug('[CodexCatalog] cached model catalogue invalid: %s', exc)
        return {}
    out = dict(cached)
    out['models'] = models
    # Client upgrades invalidate freshness, not admitted account capabilities.
    out['client_version_stale'] = (
        cached.get('client_version') != codex_client_version())
    if account_degraded:
        out['account_degraded'] = True
    return out


def _cache_age(cache: dict) -> float:
    try:
        return max(0.0, time.time() - float(cache.get('fetched_at_unix')))
    except (TypeError, ValueError) as exc:
        logger.debug('[CodexCatalog] invalid cache timestamp: %s', exc)
        return float('inf')


def _provider_models(rows: list[dict]) -> list[dict]:

    from lib.model_routing.inference import profile_from_metadata
    models = []
    for row in rows:
        modalities = set(row.get('input_modalities') or [])
        levels = [x.get('effort') for x in
                  (row.get('supported_reasoning_levels') or [])
                  if isinstance(x, dict) and x.get('effort')]
        capabilities = ['text']
        if 'image' in modalities:
            capabilities.append('vision')
        if levels:
            capabilities.append('thinking')

        profile_metadata = dict(row)
        # This catalog is supplied by the Responses subscription adapter;
        # the ladder alone is not an executable reasoning contract.
        if levels:
            profile_metadata['thinking_format'] = 'reasoning_effort'
        if levels and row.get('default_reasoning_level') not in levels:
            # Retain the raw default below, but do not activate a rung the
            # supplier's own supported list excludes.
            profile_metadata.pop('default_reasoning_level', None)
        models.append({
            'model_id': row['slug'],
            'display_name': row.get('display_name') or row['slug'],
            'description': row.get('description') or '',
            'capabilities': capabilities,
            # The frontend currently uses this field as its thinking-capable
            # bit, despite the historical name.
            'thinking_default': bool(levels),
            'catalog_visibility': row.get('visibility') or 'hide',
            'catalog_priority': row.get('priority', 0),
            'supported_in_api': bool(row.get('supported_in_api', False)),
            'default_reasoning_level': row.get('default_reasoning_level') or '',
            'supported_reasoning_levels': levels,

            'inference_profile': profile_from_metadata(profile_metadata, source='provider_catalog'),
        })
        if 'context_window' in row:
            models[-1]['context_window'] = row['context_window']
        models[-1]['inherited_model_fields'] = list(row.get('inherited_model_fields',
            [] if 'context_window' in row else ['context_window']))
    return models


def cached_codex_provider_models() -> list[dict]:
    """Return the admitted catalog, except to the provisioning thread."""
    staged = getattr(_staged_catalog, 'rows', None)
    if staged is not None:
        return _provider_models(staged)
    cached = _read_cache()
    return _provider_models(cached.get('models') or []) if cached else []


def _header(headers: dict, name: str) -> str:
    wanted = name.lower()
    for key, value in (headers or {}).items():
        if str(key).lower() == wanted:
            return str(value or '')
    return ''


def _fetch_catalog(cache: dict, *, user_id: str = '') -> tuple[list[dict], str, bool]:
    from lib.oauth.outbound import resolve_oauth_request

    token, headers, _body = resolve_oauth_request(
        'codex', {}, None, user_id=user_id)
    headers['Authorization'] = f'Bearer {token}'
    headers['Accept'] = 'application/json'
    client_version = codex_client_version()
    version_matches = cache.get('client_version') == client_version
    if cache.get('etag') and version_matches:
        headers['If-None-Match'] = str(cache['etag'])

    from lib.oauth.codex import CODEX_OAUTH_CONFIG
    query = urlencode({'client_version': client_version})
    url = f"{CODEX_OAUTH_CONFIG['api_base'].rstrip('/')}/models?{query}"

    from lib.desktop import egress as _eg
    route = _eg.route_request(url, user_id=user_id)
    if route == 'direct':
        from lib.http_transport.client import http_get
        response = http_get(url, headers=headers,
                            timeout=CODEX_CATALOG_TIMEOUT_S)
    else:
        response = _eg.egress_http(
            url, method='GET', headers=headers,
            timeout=CODEX_CATALOG_TIMEOUT_S, user_id=user_id,
            agent_id=route)

    if response.status_code == 304:
        if not cache.get('models') or not version_matches:
            raise RuntimeError(
                'Codex catalogue returned 304 without a matching client-version cache')
        return list(cache['models']), cache.get('etag', ''), True
    if response.status_code != 200:
        raise RuntimeError(
            f'Codex catalogue request failed with HTTP {response.status_code}')
    content = getattr(response, 'content', b'') or b''
    if len(content) > _MAX_RESPONSE_BYTES:
        raise RuntimeError('Codex catalogue response exceeded 4 MiB')
    rows = _normalise_models(response.json())
    return rows, _header(getattr(response, 'headers', {}), 'etag'), False


def _write_cache(rows: list[dict], etag: str, account_fingerprint: str) -> dict:
    cached = {
        'schema_version': _CACHE_SCHEMA_VERSION,
        'fetched_at': _utc_now(),
        'fetched_at_unix': time.time(),
        'etag': etag or '',
        'client_version': codex_client_version(),
        'account_fingerprint': account_fingerprint,
        'models': rows,
    }
    write_json_atomic(_cache_path(), cached, mode=0o600)
    return cached


def _provision_from_best_available() -> bool:
    from lib.oauth.outbound import provision_oauth_provider
    return provision_oauth_provider('codex')


def _admit_catalog(rows: list[dict], etag: str, fingerprint: str) -> bool:
    """Publish fetched rows only after strict model-routing admission.

    A failed attempt keeps the old on-disk catalog (and its ETag) intact.
    The thread-local candidate is exposed only to the synchronous v2
    provisioning path; readers and concurrent refreshes see last-good data.
    """
    from lib.oauth.token_store import load_token

    if not fingerprint or _account_fingerprint(load_token('codex') or {}) != fingerprint:
        raise RuntimeError('Codex account changed during catalog admission')
    _staged_catalog.rows = rows
    try:
        changed = _provision_from_best_available()
    finally:
        del _staged_catalog.rows
    # A logout can complete during the CAS; never write a new cache for a
    # credential that is no longer current. The logout path deprovisions the
    # provider after deleting the token, so it wins a concurrent admission.
    if _account_fingerprint(load_token('codex') or {}) != fingerprint:
        raise RuntimeError('Codex account changed during catalog admission')
    _write_cache(rows, etag, fingerprint)
    return changed


def _refresh_access_token_if_due(stored: dict, *, user_id: str) -> None:
    """Keep an idle subscription alive; never attempt terminal re-login.

    The request path remains the sole OAuth refresh authority. Catalog
    polling merely reaches it before token expiry even when a local cache is
    fresh or a remote catalog fetch will fail for unrelated reasons.
    """
    import math
    from lib.oauth.codex import _TOKEN_REFRESH_BUFFER, codex_get_valid_token

    if not stored.get('refresh_token') or stored.get('refresh_invalidated_at'):
        return
    try:
        expires_at = float(stored.get('expire') or 0)
    except (TypeError, ValueError, OverflowError):
        return
    if math.isfinite(expires_at) and expires_at > 0 and (expires_at - time.time()
                                                          <= _TOKEN_REFRESH_BUFFER):
        codex_get_valid_token(user_id=user_id)


def refresh_codex_model_catalog(*, force: bool = False,
                                user_id: str = '') -> dict:
    """Refresh once, preserving the last good cache on every failure."""
    from lib.oauth.token_store import load_token

    stored = load_token('codex') or {}
    if not stored.get('access_token'):
        return {
            'ok': False,
            'skipped': 'not_authenticated',
            'catalog_changed': False,
        }

    with _refresh_lock:
        global _pending_catalog
        cache = _read_cache()
        _refresh_access_token_if_due(stored, user_id=user_id)
        # A sign-in may have replaced the account while we waited on this lock.
        current = load_token('codex') or {}
        fingerprint = _account_fingerprint(current)
        if (not current.get('access_token') or
                (_account_fingerprint(stored) and
                 fingerprint != _account_fingerprint(stored))):
            return {'ok': False, 'skipped': 'not_authenticated',
                    'catalog_changed': False}
        if (_pending_catalog is not None and
                (not fingerprint or _pending_catalog['fingerprint'] != fingerprint
                 or _pending_catalog['cache_path'] != _cache_path())):
            _pending_catalog = None
        try:
            if _pending_catalog is not None:
                rows = _pending_catalog['rows']
                etag = _pending_catalog['etag']
                changed = _admit_catalog(rows, etag, fingerprint)
                _pending_catalog = None
                catalog_changed = rows != list(cache.get('models') or [])
                not_modified = False
            elif (cache and not force and not cache.get('client_version_stale')
                  and _cache_age(cache) <= CODEX_CATALOG_TTL_S):
                changed = _provision_from_best_available()
                return dict(codex_catalog_status(), ok=True, changed=changed,
                            not_modified=True, catalog_changed=False)
            else:
                previous_rows = list(cache.get('models') or [])
                rows, etag, not_modified = _fetch_catalog(cache, user_id=user_id)
                catalog_changed = rows != previous_rows
                # Do not publish a response fetched for a logged-out or
                # replaced account. This check is after the network wait.
                latest = load_token('codex') or {}
                if (not latest.get('access_token') or
                        (_account_fingerprint(latest) != fingerprint)):
                    return {'ok': False, 'skipped': 'not_authenticated',
                            'catalog_changed': False}
                try:
                    changed = _admit_catalog(rows, etag, fingerprint)
                except Exception:
                    if (fingerprint and _account_fingerprint(
                            load_token('codex') or {}) == fingerprint):
                        _pending_catalog = {'rows': rows, 'etag': etag,
                                            'fingerprint': fingerprint,
                                            'cache_path': _cache_path()}
                    raise
            with _state_lock:
                global _last_error
                _last_error = ''
            log = logger.info if catalog_changed else logger.debug
            log('[CodexCatalog] %s %d models%s',
                'updated' if catalog_changed else 'revalidated', len(rows),
                ' (HTTP 304)' if not_modified else '')
            return dict(codex_catalog_status(), ok=True, changed=changed,
                        not_modified=not_modified,
                        catalog_changed=catalog_changed)
        except Exception as exc:
            with _state_lock:
                _last_error = str(exc)[:300]
            # A stale last-good cache remains authoritative; if this is the
            # first fetch, provisioning falls back to outbound's static table.
            if _pending_catalog is None:
                try:
                    _provision_from_best_available()
                except Exception as provision_exc:
                    logger.warning('[CodexCatalog] fallback provision failed: %s',
                                   provision_exc)
            logger.warning('[CodexCatalog] refresh failed; keeping last good '
                           'catalogue: %s', exc)
            return dict(codex_catalog_status(), ok=False,
                        error=str(exc)[:300], catalog_changed=False)


def codex_catalog_status() -> dict:
    cache = _read_cache()
    rows = cache.get('models') or []
    with _state_lock:
        error = _last_error
    return {
        'catalog_source': 'remote_cache' if rows else 'static_fallback',
        'catalog_updated_at': cache.get('fetched_at') if rows else None,
        'catalog_stale': bool(rows and (cache.get('client_version_stale')
                                      or _cache_age(cache) > CODEX_CATALOG_TTL_S)),
        'catalog_etag': cache.get('etag', '') if rows else '',
        'catalog_model_count': len(rows),
        'catalog_visible_model_count': sum(
            1 for row in rows if row.get('visibility') == 'list'),
        'catalog_error': error,
    }


def _next_refresh_interval(
    current: float,
    result: dict,
    *,
    explicit_wake: bool = False,
) -> float:
    """Back off stable/error polling; recover fast after a real change."""
    if result.get('skipped') == 'not_authenticated':
        return float(CODEX_CATALOG_MAX_REFRESH_INTERVAL_S)
    if explicit_wake or result.get('catalog_changed'):
        return float(CODEX_CATALOG_REFRESH_INTERVAL_S)

    if not result.get('ok'):
        return float(min(900, max(CODEX_CATALOG_REFRESH_INTERVAL_S, current) * 2))
    return float(min(
        CODEX_CATALOG_MAX_REFRESH_INTERVAL_S,
        max(CODEX_CATALOG_REFRESH_INTERVAL_S, current) * 2,
    ))


def _token_refresh_poll_interval(interval: float) -> float:
    """Wake the existing worker before token expiry, even after stable 304s.

    Refresh stays at the OAuth singleflight boundary in _fetch_catalog; this
    only caps the next poll. A revoked/missing refresh token never creates a
    new login, a tight loop, or a second background service.
    """
    import math
    from lib.oauth.codex import _TOKEN_REFRESH_BUFFER
    from lib.oauth.token_store import load_token

    stored = load_token('codex') or {}
    if (not stored.get('access_token') or not stored.get('refresh_token')
            or stored.get('refresh_invalidated_at')):
        return interval
    try:
        expires_at = float(stored.get('expire') or 0)
    except (TypeError, ValueError, OverflowError):
        return interval
    if not math.isfinite(expires_at) or expires_at <= 0:
        return interval
    return min(interval, max(CODEX_CATALOG_REFRESH_INTERVAL_S,
                             expires_at - time.time() - _TOKEN_REFRESH_BUFFER))


def _research_registrations(boundary) -> dict:
    """Run bounded metadata research in the existing worker, outside its lock."""
    from lib.model_catalog.registration_research import run_registration_research
    from lib.model_routing import ModelRoutingRepository

    fingerprint = _account_fingerprint()
    if not fingerprint or _worker_stop.is_set():
        return {'status': 'skipped'}
    try:
        report = run_registration_research(
            ModelRoutingRepository(), boundary, provider_id='oauth_codex',
            deadline=time.monotonic() + CODEX_REGISTRATION_MAX_RUNTIME_S,
            cancelled=lambda: (_worker_stop.is_set()
                               or _account_fingerprint() != fingerprint))
        logger.debug('[CodexCatalog] registration research: %s', report)
        return report
    except Exception as exc:
        # Optional research cannot stop last-good catalog/token polling.
        logger.warning('[CodexCatalog] registration research unavailable (%s)', type(exc).__name__)
        return {'status': 'unavailable'}


def _worker_loop(*, force_initial: bool = False, boundary=None) -> None:
    global _worker_started, _worker_thread
    interval = float(CODEX_CATALOG_REFRESH_INTERVAL_S)
    next_research = 0.0
    def research_if_due(result):
        nonlocal next_research
        if (boundary is not None and result.get('ok') and not _worker_stop.is_set()
                and (result.get('catalog_changed') or time.monotonic() >= next_research)):
            report = _research_registrations(boundary)
            if report.get('status') != 'busy':
                next_research = time.monotonic() + CODEX_REGISTRATION_RETRY_INTERVAL_S
    try:
        result = refresh_codex_model_catalog(force=force_initial)
        if result.get('skipped') == 'not_authenticated':
            return
        research_if_due(result)
        interval = _next_refresh_interval(interval, result)
        while not _worker_stop.is_set():
            explicit_wake = _refresh_wake.wait(
                _token_refresh_poll_interval(interval))
            _refresh_wake.clear()
            if _worker_stop.is_set():
                break
            result = refresh_codex_model_catalog(force=True)
            if result.get('skipped') == 'not_authenticated':
                break
            research_if_due(result)
            interval = _next_refresh_interval(
                interval, result, explicit_wake=explicit_wake)
    finally:
        current = threading.current_thread()
        with _state_lock:
            if _worker_thread is current:
                _worker_thread = None
                _worker_started = False


def start_codex_catalog_refresher(*, force_initial: bool = False,
                                  owner_user_id: int | None = None,
                                  tenant_id: str | None = None) -> bool:
    """Start the import-safe, process-wide daemon refresher once."""
    global _worker_started, _worker_thread
    from lib.oauth.token_store import load_token

    if not (load_token('codex') or {}).get('access_token'):
        return False
    from lib.oauth.outbound import _oauth_owner_boundary
    boundary = _oauth_owner_boundary(owner_user_id, tenant_id)
    with _state_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return False
        _worker_started = True
        _worker_stop.clear()
        _refresh_wake.clear()
        _worker_thread = threading.Thread(
            target=_worker_loop, kwargs={'force_initial': force_initial, 'boundary': boundary},
            daemon=True,
            name='codex-model-catalog-refresher')
        _worker_thread.start()
    return True


def forget_pending_codex_catalog() -> None:
    """Forget unadmitted rows after an explicit logout (not transient loss)."""
    global _pending_catalog
    with _refresh_lock:
        _pending_catalog = None


def stop_codex_catalog_refresher(timeout: float = 2.0) -> bool:
    """Wake, signal and bounded-join the Codex catalogue refresher."""
    global _worker_started, _worker_thread
    _worker_stop.set()
    _refresh_wake.set()
    with _state_lock:
        thread = _worker_thread
    if thread is None:
        return True
    try:
        wait_seconds = max(0.0, float(timeout))
    except (TypeError, ValueError, OverflowError) as exc:
        logger.debug('[CodexCatalog] invalid stop timeout; using 2.0: %s', exc)
        wait_seconds = 2.0
    if thread is not threading.current_thread():
        thread.join(timeout=wait_seconds)
    if thread.is_alive():
        return False
    with _state_lock:
        if _worker_thread is thread:
            _worker_thread = None
            _worker_started = False
    return True


def trigger_codex_catalog_refresh() -> None:
    """Request an immediate non-blocking refresh after a successful login."""
    with _state_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            _refresh_wake.set()
            return
    if start_codex_catalog_refresher(force_initial=True):
        return
    # Another caller may have published the persistent worker while this
    # trigger was between locks. Do not lose the explicit refresh request.
    with _state_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            _refresh_wake.set()


__all__ = [
    'CODEX_CATALOG_TTL_S',
    'CODEX_CATALOG_REFRESH_INTERVAL_S',
    'CODEX_CATALOG_MAX_REFRESH_INTERVAL_S',
    'cached_codex_provider_models',
    'codex_catalog_status',
    'refresh_codex_model_catalog',
    'start_codex_catalog_refresher',
    'stop_codex_catalog_refresher',
    'forget_pending_codex_catalog',
    'trigger_codex_catalog_refresh',
]
