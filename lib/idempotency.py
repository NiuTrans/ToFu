"""lib/idempotency.py — Idempotency-Key support for POST endpoints.

Clients send ``Idempotency-Key: <client-uuid>`` on POST requests that
create resources (chat tasks, paper reports, etc.). If a duplicate
arrives within the TTL, we return the cached response instead of
re-running the side effect.

Backed by ``lib.ttl_cache.TTLCache`` (24-hour TTL by default).

Usage in routes::

    from lib.idempotency import idempotent_post

    @bp.route('/api/v1/tasks', methods=['POST'])
    @idempotent_post()
    def start_task():
        ...
        return api_ok(taskId=tid)

The decorator looks for the ``Idempotency-Key`` header on the inbound
request, salts it with the authenticated key_id (so two different API
keys can't collide), and if a hit exists, returns the cached response
WITHOUT calling the route body.
"""

from __future__ import annotations

import functools
import json
import threading

from lib.log import get_logger
from lib.ttl_cache import TTLCache

logger = get_logger(__name__)


def _response_classes() -> tuple:
    """Return the native Quart response class without importing the app."""
    classes = []
    try:
        from quart import Response as _QR
        classes.append(_QR)
    except Exception as e:
        logger.debug('[Idempotency] quart.Response unavailable: %s', e)
    return tuple(classes) or (object,)


_RESPONSE_CLASSES = _response_classes()


def _get_request():
    """Resolve the active native Quart request lazily."""
    from quart import request
    return request

_DEFAULT_TTL = 86400  # 24h
_DEFAULT_MAX = 10_000

_cache = TTLCache(ttl=_DEFAULT_TTL, max_size=_DEFAULT_MAX,
                   name='idempotency')

# A cache lookup alone is not enough for idempotency: two duplicate requests
# can miss before the first handler has returned and both execute the side
# effect.  Keep a tiny per-key single-flight registry so the first request is
# the owner and concurrent duplicates wait for its response to enter the
# replay cache.  Entries exist only while a handler is running, so this map is
# bounded by request concurrency rather than the 24-hour cache cardinality.
_inflight_lock = threading.Lock()
_inflight: dict[str, threading.Event] = {}


def _claim_inflight(cache_key: str) -> tuple[bool, threading.Event]:
    """Return ``(is_owner, event)`` for one idempotency cache key."""
    with _inflight_lock:
        event = _inflight.get(cache_key)
        if event is not None:
            return False, event
        event = threading.Event()
        _inflight[cache_key] = event
        return True, event


def _release_inflight(cache_key: str, event: threading.Event) -> None:
    """Wake duplicates after the owner cached (or failed to cache) a result."""
    with _inflight_lock:
        if _inflight.get(cache_key) is event:
            _inflight.pop(cache_key, None)
    event.set()


def _cache_key(idem_key: str) -> str:
    """Salt the idempotency key with the authenticated principal so keys
    can't collide across tenants.

    Reads ``g.auth_ctx`` (set by the Bearer auth middleware) when
    available; falls back to a generic ``'_'`` namespace.
    """
    try:
        from quart import g  # type: ignore
        ctx = getattr(g, 'auth_ctx', None)
        if ctx is not None and getattr(ctx, 'key_id', ''):
            return f'k:{ctx.key_id}:{idem_key}'
        if ctx is not None and getattr(ctx, 'via_tunnel_token', False):
            return f'tun:{idem_key}'
        if ctx is not None and getattr(ctx, 'via_open_mode', False):
            return f'open:{idem_key}'
    except Exception as e:
        logger.debug('[Idempotency] auth_ctx lookup failed: %s', e)
    return f'_:{idem_key}'


def _read_response_body_sync(resp) -> str:
    """Best-effort sync read of a Quart/Flask Response body.

    Quart's ``response.get_data()`` and ``response.data`` are async (they
    return a coroutine). Calling them from a sync context (a sync route
    handler running in Quart's thread pool, or a unit test) yields a
    coroutine, not bytes.

    This helper tries several access paths:
      1. ``response.response.data``     (Quart's DataBody → raw bytes, sync)
      2. ``response.get_data(as_text=True)`` (Flask path → str, sync)
      3. fall back to repr — caller logs and skips caching.
    """
    body_obj = getattr(resp, 'response', None)
    if body_obj is not None and hasattr(body_obj, 'data'):
        raw = body_obj.data
        if isinstance(raw, bytes):
            try:
                return raw.decode('utf-8')
            except UnicodeDecodeError as _e_audit:
                logger.debug('[idempotency] _read_response_body_sync caught %s: %s', type(_e_audit).__name__, _e_audit)
                return raw.decode('latin-1', errors='replace')
        if isinstance(raw, str):
            return raw
    try:
        out = resp.get_data(as_text=True)
        if isinstance(out, str):
            return out
    except Exception as e:
        logger.debug('[Idempotency] sync get_data failed (caching skipped): %s', e)
    return ''


def _serialise_response(rv) -> dict:
    """Convert a route's return value into a JSON-serialisable cached blob."""
    if isinstance(rv, tuple):
        body = rv[0]
        status = rv[1] if len(rv) > 1 else 200
        headers = rv[2] if len(rv) > 2 else {}
    elif isinstance(rv, _RESPONSE_CLASSES):
        body = rv
        status = rv.status_code
        headers = dict(rv.headers)
    else:
        body = rv
        status = 200
        headers = {}
    if isinstance(body, _RESPONSE_CLASSES):
        text = _read_response_body_sync(body)
        if not text:
            raise RuntimeError(
                'unable to read response body synchronously '
                '(coroutine returned); idempotency cache skipped')
        return {'kind': 'raw', 'body': text, 'status': int(status),
                'content_type': body.content_type or 'application/json',
                'headers': dict(headers) if headers else {}}
    if isinstance(body, (dict, list)):
        return {'kind': 'json', 'body': body, 'status': int(status),
                'headers': dict(headers) if headers else {}}
    if isinstance(body, str):
        return {'kind': 'text', 'body': body, 'status': int(status),
                'headers': dict(headers) if headers else {}}
    return {'kind': 'json', 'body': str(body), 'status': int(status),
            'headers': dict(headers) if headers else {}}


def _replay(blob: dict):
    """Reconstruct a Flask/Quart response from a cached blob.

    Picks the first available Response class so callers always get a
    Response that's compatible with the framework currently in use
    (Quart for the running server; either for tests).
    """
    headers = dict(blob.get('headers') or {})
    headers['Idempotency-Replay'] = 'true'
    status = int(blob['status'])
    ResponseCls = _RESPONSE_CLASSES[0]
    if blob['kind'] == 'json':
        body_text = json.dumps(blob['body'], ensure_ascii=False)
        resp = ResponseCls(body_text, status=status,
                           content_type='application/json; charset=utf-8')
    elif blob['kind'] == 'text':
        resp = ResponseCls(blob['body'], status=status,
                           content_type='text/plain; charset=utf-8')
    else:  # raw
        resp = ResponseCls(blob['body'], status=status,
                           content_type=blob.get('content_type',
                                                   'application/json'))
    for k, v in headers.items():
        resp.headers[k] = v
    return resp


def idempotent_post():
    """Decorator: cache the response for ``Idempotency-Key`` POSTs.

    Works on both sync and ``async def`` route handlers. Concurrent requests
    carrying the same key are single-flighted: only the owner executes the
    handler; duplicates wait and replay its cached 2xx response. No-op when
    the header is absent. Must be applied AFTER the route decorator (so the
    cache check runs inside the request context). Entries use the module TTL
    (``_DEFAULT_TTL``, 24h) — the cache has no per-entry TTL.
    """
    def _maybe_cache(rv, ck):
        try:
            blob = _serialise_response(rv)
            if 200 <= int(blob.get('status', 200)) < 300:
                _cache.set(ck, blob)
        except Exception as e:
            logger.debug('[Idempotency] cache store failed: %s', e)

    def decorator(fn):
        import inspect
        is_async = inspect.iscoroutinefunction(fn)

        if is_async:
            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                import asyncio

                req = _get_request()
                try:
                    idem = (req.headers.get('Idempotency-Key') or '').strip()
                except RuntimeError as e:
                    logger.debug('[Idempotency] outside request context: %s', e)
                    return await fn(*args, **kwargs)
                if not idem:
                    return await fn(*args, **kwargs)
                ck = _cache_key(idem)
                while True:
                    cached = _cache.get(ck)
                    if cached is not None:
                        logger.info('[Idempotency] HIT key=%s — replaying',
                                    idem[:8])
                        try:
                            return _replay(cached)
                        except Exception as e:
                            logger.warning('[Idempotency] replay failed for %s: %s',
                                           idem[:8], e)
                    owner, event = _claim_inflight(ck)
                    if owner:
                        break
                    # Never block the ASGI event loop on a threading.Event.
                    await asyncio.to_thread(event.wait)

                try:
                    # Close the miss→claim race with an owner that completed
                    # between our first cache read and _claim_inflight().
                    cached = _cache.get(ck)
                    if cached is not None:
                        try:
                            return _replay(cached)
                        except Exception as e:
                            logger.warning('[Idempotency] replay failed for %s: %s',
                                           idem[:8], e)
                    rv = await fn(*args, **kwargs)
                    _maybe_cache(rv, ck)
                    return rv
                finally:
                    _release_inflight(ck, event)
            return async_wrapper

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            req = _get_request()
            try:
                idem = (req.headers.get('Idempotency-Key') or '').strip()
            except RuntimeError as e:
                logger.debug('[Idempotency] outside request context: %s', e)
                return fn(*args, **kwargs)
            if not idem:
                return fn(*args, **kwargs)
            ck = _cache_key(idem)
            while True:
                cached = _cache.get(ck)
                if cached is not None:
                    logger.info('[Idempotency] HIT key=%s — replaying cached '
                                'response', idem[:8])
                    try:
                        return _replay(cached)
                    except Exception as e:
                        logger.warning('[Idempotency] replay failed for %s: %s '
                                       '— re-running handler', idem[:8], e)
                owner, event = _claim_inflight(ck)
                if owner:
                    break
                event.wait()

            try:
                # Close the miss→claim race with an owner that completed just
                # before this request installed itself as the next owner.
                cached = _cache.get(ck)
                if cached is not None:
                    try:
                        return _replay(cached)
                    except Exception as e:
                        logger.warning('[Idempotency] replay failed for %s: %s '
                                       '— re-running handler', idem[:8], e)
                rv = fn(*args, **kwargs)
                _maybe_cache(rv, ck)
                return rv
            finally:
                _release_inflight(ck, event)

        return wrapper

    return decorator


def cache_stats() -> dict:
    """Expose underlying cache stats for /metrics."""
    return _cache.stats()


__all__ = ['idempotent_post', 'cache_stats']
