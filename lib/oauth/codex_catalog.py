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
from lib.oauth.outbound import CODEX_CLIENT_VERSION

logger = get_logger(__name__)

CODEX_CATALOG_TTL_S = 300
CODEX_CATALOG_REFRESH_INTERVAL_S = 180
CODEX_CATALOG_TIMEOUT_S = 5

_CACHE_SCHEMA_VERSION = 1
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024

_refresh_lock = threading.Lock()
_state_lock = threading.Lock()
_refresh_wake = threading.Event()
_worker_stop = threading.Event()
_worker_thread = None
_worker_started = False
_oneshot_pending = False
_last_error = ''


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

    if not rows:
        raise ValueError('Codex model catalogue is empty after validation')
    rows.sort(key=lambda row: (row['priority'], row['slug']))
    return rows


def _read_cache(*, current_account: bool = True) -> dict:
    cached = read_json(_cache_path(), default={}) or {}
    if (cached.get('schema_version') != _CACHE_SCHEMA_VERSION
            or cached.get('client_version') != CODEX_CLIENT_VERSION):
        return {}
    if current_account:
        wanted = _account_fingerprint()
        cached_account = str(cached.get('account_fingerprint') or '')
        if wanted and cached_account and wanted != cached_account:
            return {}
    try:
        models = _normalise_models(cached)
    except ValueError as exc:
        logger.debug('[CodexCatalog] cached model catalogue invalid: %s', exc)
        return {}
    out = dict(cached)
    out['models'] = models
    return out


def _cache_age(cache: dict) -> float:
    try:
        return max(0.0, time.time() - float(cache.get('fetched_at_unix')))
    except (TypeError, ValueError) as exc:
        logger.debug('[CodexCatalog] invalid cache timestamp: %s', exc)
        return float('inf')


def _provider_models(rows: list[dict]) -> list[dict]:
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
        })
    return models


def cached_codex_provider_models() -> list[dict]:
    """Return the last good account-compatible catalogue projection."""
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
    if cache.get('etag'):
        headers['If-None-Match'] = str(cache['etag'])

    from lib.oauth.codex import CODEX_OAUTH_CONFIG
    query = urlencode({'client_version': CODEX_CLIENT_VERSION})
    url = f"{CODEX_OAUTH_CONFIG['api_base'].rstrip('/')}/models?{query}"

    from lib.desktop import egress as _eg
    route = _eg.route_request(url, user_id=user_id)
    if route == 'direct':
        from lib.http_client import http_get
        response = http_get(url, headers=headers,
                            timeout=CODEX_CATALOG_TIMEOUT_S)
    else:
        response = _eg.egress_http(
            url, method='GET', headers=headers,
            timeout=CODEX_CATALOG_TIMEOUT_S, user_id=user_id,
            agent_id=route)

    if response.status_code == 304:
        if not cache.get('models'):
            raise RuntimeError('Codex catalogue returned 304 without a cache')
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
        'client_version': CODEX_CLIENT_VERSION,
        'account_fingerprint': account_fingerprint,
        'models': rows,
    }
    write_json_atomic(_cache_path(), cached, mode=0o600)
    return cached


def _provision_from_best_available() -> bool:
    from lib.oauth.outbound import provision_oauth_provider
    return provision_oauth_provider('codex')


def refresh_codex_model_catalog(*, force: bool = False,
                                user_id: str = '') -> dict:
    """Refresh once, preserving the last good cache on every failure."""
    from lib.oauth.token_store import load_token

    stored = load_token('codex') or {}
    if not stored.get('access_token'):
        return {'ok': False, 'skipped': 'not_authenticated'}

    with _refresh_lock:
        cache = _read_cache()
        if cache and not force and _cache_age(cache) <= CODEX_CATALOG_TTL_S:
            changed = _provision_from_best_available()
            return dict(codex_catalog_status(), ok=True, changed=changed,
                        not_modified=True)
        try:
            rows, etag, not_modified = _fetch_catalog(
                cache, user_id=user_id)
            cache = _write_cache(
                rows, etag, _account_fingerprint(stored))
            changed = _provision_from_best_available()
            with _state_lock:
                global _last_error
                _last_error = ''
            logger.info('[CodexCatalog] refreshed %d models%s', len(rows),
                        ' (not modified)' if not_modified else '')
            return dict(codex_catalog_status(), ok=True, changed=changed,
                        not_modified=not_modified)
        except Exception as exc:
            with _state_lock:
                _last_error = str(exc)[:300]
            # A stale last-good cache remains authoritative; if this is the
            # first fetch, provisioning falls back to outbound's static table.
            try:
                _provision_from_best_available()
            except Exception as provision_exc:
                logger.warning('[CodexCatalog] fallback provision failed: %s',
                               provision_exc)
            logger.warning('[CodexCatalog] refresh failed; keeping last good '
                           'catalogue: %s', exc)
            return dict(codex_catalog_status(), ok=False,
                        error=str(exc)[:300])


def codex_catalog_status() -> dict:
    cache = _read_cache()
    rows = cache.get('models') or []
    with _state_lock:
        error = _last_error
    return {
        'catalog_source': 'remote_cache' if rows else 'static_fallback',
        'catalog_updated_at': cache.get('fetched_at') if rows else None,
        'catalog_stale': bool(rows and _cache_age(cache) > CODEX_CATALOG_TTL_S),
        'catalog_etag': cache.get('etag', '') if rows else '',
        'catalog_model_count': len(rows),
        'catalog_visible_model_count': sum(
            1 for row in rows if row.get('visibility') == 'list'),
        'catalog_error': error,
    }


def _worker_loop() -> None:
    refresh_codex_model_catalog(force=False)
    while not _worker_stop.is_set():
        _refresh_wake.wait(CODEX_CATALOG_REFRESH_INTERVAL_S)
        _refresh_wake.clear()
        if _worker_stop.is_set():
            break
        refresh_codex_model_catalog(force=True)


def start_codex_catalog_refresher() -> bool:
    """Start the import-safe, process-wide daemon refresher once."""
    global _worker_started, _worker_thread
    with _state_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return False
        _worker_started = True
        _worker_stop.clear()
        _refresh_wake.clear()
        _worker_thread = threading.Thread(
            target=_worker_loop, daemon=True,
            name='codex-model-catalog-refresher')
        _worker_thread.start()
    return True


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
    global _oneshot_pending, _worker_started, _worker_thread
    with _state_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            _refresh_wake.set()
            return
        # A crashed persistent worker must not leave the boolean latch routing
        # every future login refresh into an event nobody consumes.
        _worker_started = False
        _worker_thread = None
        if _oneshot_pending:
            return
        _oneshot_pending = True

    def _once():
        global _oneshot_pending
        try:
            refresh_codex_model_catalog(force=True)
        finally:
            with _state_lock:
                _oneshot_pending = False

    threading.Thread(
        target=_once, daemon=True,
        name='codex-model-catalog-refresh-once').start()


__all__ = [
    'CODEX_CATALOG_TTL_S',
    'CODEX_CATALOG_REFRESH_INTERVAL_S',
    'cached_codex_provider_models',
    'codex_catalog_status',
    'refresh_codex_model_catalog',
    'start_codex_catalog_refresher',
    'stop_codex_catalog_refresher',
    'trigger_codex_catalog_refresh',
]
