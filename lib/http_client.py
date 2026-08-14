"""Unified HTTP client — sync (requests) + async (httpx).

Replaces ad-hoc ``requests.get/post`` calls scattered across the
project. The unified API:

  - **Auto-applies** ``proxies_for(url)`` from ``lib.proxy`` so internal
    URLs bypass the corporate proxy and external URLs use it.
  - **Default timeout** of 30s (override per-call).
  - **Default User-Agent** of ``Tofu/<version>`` (override via headers).
  - **Connection reuse** through one requests Session per worker thread and
    one httpx AsyncClient per event-loop/proxy/TLS tuple. Cookie jars are
    deliberately non-persistent so pooling cannot create ambient auth state.
  - **Single import** — ``from lib.http_client import http_get, http_post``.
  - **Mirrors requests/httpx** — returns the underlying response object
    so callers can use ``.json()``, ``.raise_for_status()``, ``.iter_lines()``,
    etc. exactly as before.

Public API
----------
Sync (requests):
  - ``http_get(url, *, timeout=30, headers=None, params=None, **extra)``
  - ``http_post(url, *, timeout=30, headers=None, json=None, data=None, **extra)``
  - ``http_request(method, url, **kw)``  — generic dispatcher
  - ``http_stream(method, url, **kw)``   — context manager for stream=True

Async (httpx):
  - ``await async_http_get(url, *, timeout=30, ...)``
  - ``await async_http_post(url, *, timeout=30, ...)``
  - ``async with async_http_stream(method, url, **kw) as resp: ...``

Out of scope (NOT migrated)
---------------------------
- ``lib/fetch/utils.py`` — has a specialised session pool with circuit
  breaker, retry strategy per HTTP status, multiple SSL fallback sessions.
  Keep its own pool.
- ``lib/llm/stream.py`` — custom SSE streaming with retry / 429 cycling /
  cache breakpoints. Use the LLM-layer wrapper, not this.
- ``lib/llm/astream.py`` — already async via httpx. No-op.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import time
import weakref
from typing import Any, Optional

import requests

import lib as _lib
from lib.log import get_logger
from lib.proxy import async_proxy_for, is_subscription_host
from lib.proxy import proxies_for, report_outcome as _report_outcome
from lib.proxy import (
    report_subscription_route,
    subscription_route_candidates,
)

logger = get_logger(__name__)


# ── Defaults ──────────────────────────────────────────────────────

_DEFAULT_TIMEOUT = 30.0
_DEFAULT_USER_AGENT = f'Tofu/{getattr(_lib, "__version__", "0.0.0-dev")}'


def _env_pool_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, '') or default)
    except (TypeError, ValueError) as exc:
        logger.debug('[HTTP] invalid pool setting %s; using %d: %s',
                     name, default, exc)
        value = default
    return max(low, min(high, value))


_POOL_MAX_CONNECTIONS = _env_pool_int(
    'TOFU_HTTP_POOL_MAX_CONNECTIONS', 64, 1, 1024)
_POOL_MAX_KEEPALIVE = min(
    _POOL_MAX_CONNECTIONS,
    _env_pool_int('TOFU_HTTP_POOL_MAX_KEEPALIVE', 20, 1, 512))

# requests.Session is stateful and not documented as thread-safe. Keep one per
# worker thread: urllib3 still reuses TCP/TLS connections across calls on that
# thread, while concurrent route/agent workers never mutate one shared Session.
_sync_local = threading.local()
_sync_sessions: set[requests.Session] = set()
_sync_sessions_lock = threading.Lock()

# AsyncClient is event-loop-bound. One client per (loop, proxy, TLS policy)
# preserves connection reuse without ever crossing loop ownership.
_async_clients = weakref.WeakKeyDictionary()
_async_clients_lock = threading.Lock()


def _build_headers(extra: Optional[dict]) -> dict:
    h = {'User-Agent': _DEFAULT_USER_AGENT}
    if extra:
        h.update(extra)
    return h


def _httpx_proxy_url(url: str) -> Optional[str]:
    """Proxy URL for the httpx async client — delegates to
    ``lib.proxy.async_proxy_for`` so the async transport honours env
    ``no_proxy`` exactly like the sync one (httpx alone ignores it once an
    explicit ``proxy=`` is set)."""
    return async_proxy_for(url)


def _sync_session() -> requests.Session:
    session = getattr(_sync_local, 'session', None)
    if session is None:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=_POOL_MAX_KEEPALIVE,
            pool_maxsize=_POOL_MAX_CONNECTIONS,
            pool_block=False)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        _sync_local.session = session
        with _sync_sessions_lock:
            _sync_sessions.add(session)
    return session


def _sync_request(method: str, url: str, **kwargs):
    """Issue through the thread-local pool without persisting response cookies.

    The old top-level ``requests.request`` API did not retain cookies between
    calls. Pooling must not silently turn unrelated provider calls into one
    ambient browser session, so the Session jar is cleared on both boundaries;
    explicit per-request ``cookies=`` continues to work normally.
    """
    session = _sync_session()
    session.cookies.clear()
    try:
        return session.request(method, url, **kwargs)
    finally:
        session.cookies.clear()


def _async_pool_key(proxy, verify):
    try:
        hash(verify)
        verify_key = verify
    except TypeError as exc:
        logger.debug('[HTTP] non-hashable TLS verify object; using identity: %s',
                     exc)
        verify_key = ('identity', id(verify))
    return proxy, verify_key


def _async_client(proxy=None, verify=True):
    import httpx
    loop = asyncio.get_running_loop()
    key = _async_pool_key(proxy, verify)
    with _async_clients_lock:
        per_loop = _async_clients.setdefault(loop, {})
        client = per_loop.get(key)
        if client is not None and not client.is_closed:
            return client

        class _NoPersistCookies(httpx.Cookies):
            def extract_cookies(self, response) -> None:
                # Preserve the stateless semantics of one-shot AsyncClient.
                return None

        client = httpx.AsyncClient(
            proxy=proxy,
            verify=verify,
            limits=httpx.Limits(
                max_connections=_POOL_MAX_CONNECTIONS,
                max_keepalive_connections=_POOL_MAX_KEEPALIVE,
                keepalive_expiry=30.0),
        )
        # httpx has no public "do not retain Set-Cookie" switch. Its documented
        # Cookies hook is the narrowest seam; explicit request cookies are merged
        # into a temporary jar and remain supported.
        client._cookies = _NoPersistCookies()  # type: ignore[attr-defined]
        per_loop[key] = client
        return client


def close_sync_http_clients() -> None:
    """Close every requests pool created by worker threads (idempotent)."""
    with _sync_sessions_lock:
        sessions = list(_sync_sessions)
        _sync_sessions.clear()
    for session in sessions:
        try:
            session.close()
        except Exception as e:
            logger.debug('[HTTP] sync session close failed: %s', e)
    try:
        delattr(_sync_local, 'session')
    except AttributeError as exc:
        logger.debug('[HTTP] no thread-local sync session to remove: %s', exc)


async def close_async_http_clients() -> None:
    """Close clients owned by the current event loop."""
    loop = asyncio.get_running_loop()
    with _async_clients_lock:
        clients = list((_async_clients.pop(loop, {}) or {}).values())
    for client in clients:
        try:
            await client.aclose()
        except Exception as e:
            logger.debug('[HTTP] async client close failed: %s', e)


async def close_http_clients() -> None:
    close_sync_http_clients()
    await close_async_http_clients()


def http_pool_stats() -> dict:
    with _sync_sessions_lock, _async_clients_lock:
        return {
            'sync_sessions': len(_sync_sessions),
            'async_clients': sum(len(v) for v in _async_clients.values()),
            'max_connections': _POOL_MAX_CONNECTIONS,
            'max_keepalive': _POOL_MAX_KEEPALIVE,
        }


# ══════════════════════════════════════════════════════════════════
#  Sync (requests-based)
# ══════════════════════════════════════════════════════════════════

def http_request(method: str, url: str, *,
                 timeout: float = _DEFAULT_TIMEOUT,
                 headers: Optional[dict] = None,
                 use_proxy: bool = True,
                 **extra: Any) -> requests.Response:
    """Sync HTTP request with proxy + sane defaults.

    Parameters
    ----------
    method : str
        ``'GET'``, ``'POST'``, ``'PUT'``, ``'DELETE'``, ``'HEAD'``, ``'PATCH'``.
    url : str
        Target URL.
    timeout : float
        Connect+read timeout in seconds (default 30).
    headers : dict | None
        Extra headers; merged on top of the default User-Agent. The
        default User-Agent is overridden if the caller provides one.
    use_proxy : bool
        If False, skip proxy-setting entirely (for internal localhost calls
        where ``proxies_for()`` would still go through the env-var lookup).
    **extra
        Passed straight through to ``requests.request`` (``json=``, ``data=``,
        ``params=``, ``files=``, ``stream=``, ``verify=``, ``cookies=``, etc.)

    Returns
    -------
    requests.Response
        The raw response; caller calls ``.json()``, ``.text``, etc.

    Notes
    -----
    Caller is responsible for ``.raise_for_status()`` / status-code handling.
    Caller is responsible for closing streamed responses (use http_stream
    context manager instead).
    """
    # Server-side POST/PUT/etc. must not silently follow a user-controlled
    # redirect. Apart from SSRF, requests may rewrite a 301/302 POST into GET
    # and discard the signed/request body. Public GET helpers retain their
    # historical redirect behaviour; strict fetches use lib.safe_fetch, which
    # validates every hop.
    extra.setdefault('allow_redirects', method.upper() in ('GET', 'HEAD'))
    route_plan = []
    if use_proxy and 'proxies' not in extra:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or '').lower()
        if is_subscription_host(host):
            route_plan = subscription_route_candidates(url)
        if not route_plan:
            extra['proxies'] = proxies_for(url)

    # Subscription paths are concrete and independently health-tracked.  A
    # connection-setup failure is safe to move to the next path because no
    # response headers were received; any delivered HTTP response is final
    # and is never replayed on another route.
    if route_plan:
        attempted = set()
        for route in route_plan:
            attempted.add(route.route_id)
            attempt_extra = dict(extra)
            attempt_extra['proxies'] = route.requests_proxies()
            started = time.monotonic()
            try:
                response = _sync_request(
                    method, url, timeout=timeout,
                    headers=_build_headers(headers), **attempt_extra)
            except requests.exceptions.ConnectionError as e:
                from lib.subscription_routes import is_safe_connect_failure
                if not is_safe_connect_failure(e):
                    report_subscription_route(url, route, False)
                    raise
                report_subscription_route(url, route, False)
                logger.info('[HTTP] %s connection failed via %s — trying '
                            'next subscription route', url, route.label)
                # The cold probe race returns on its first success; sibling
                # probes keep running.  Refresh now so a just-completed
                # alternative joins this same request's failover chain.
                known = {item.route_id for item in route_plan}
                for candidate in subscription_route_candidates(url):
                    if (candidate.route_id not in attempted
                            and candidate.route_id not in known):
                        route_plan.append(candidate)
                        known.add(candidate.route_id)
                continue
            except Exception:
                # Read/body timeouts are ambiguous: upstream may have accepted
                # the request, so do not replay it over a different path.
                raise
            report_subscription_route(
                url, route, True,
                (time.monotonic() - started) * 1000.0)
            return response
        raise requests.exceptions.ConnectionError(
            'all server subscription routes failed during connection '
            'setup') from None

    _t0 = time.monotonic()
    try:
        resp = _sync_request(
            method, url,
            timeout=timeout,
            headers=_build_headers(headers),
            **extra,
        )
    except Exception:
        _report_outcome(url, False)
        raise
    _report_outcome(url, True, (time.monotonic() - _t0) * 1000.0)
    return resp


def http_get(url: str, *, timeout: float = _DEFAULT_TIMEOUT,
             headers: Optional[dict] = None,
             params: Optional[dict] = None,
             use_proxy: bool = True,
             **extra: Any) -> requests.Response:
    """Sync GET. See ``http_request`` for parameter details."""
    if params is not None:
        extra['params'] = params
    return http_request('GET', url, timeout=timeout, headers=headers,
                        use_proxy=use_proxy, **extra)


def http_post(url: str, *, timeout: float = _DEFAULT_TIMEOUT,
              headers: Optional[dict] = None,
              json: Any = None,
              data: Any = None,
              files: Any = None,
              use_proxy: bool = True,
              **extra: Any) -> requests.Response:
    """Sync POST. See ``http_request`` for parameter details."""
    if json is not None:
        extra['json'] = json
    if data is not None:
        extra['data'] = data
    if files is not None:
        extra['files'] = files
    return http_request('POST', url, timeout=timeout, headers=headers,
                        use_proxy=use_proxy, **extra)


def http_put(url: str, **kw) -> requests.Response:
    return http_request('PUT', url, **kw)


def http_delete(url: str, **kw) -> requests.Response:
    return http_request('DELETE', url, **kw)


def http_head(url: str, **kw) -> requests.Response:
    return http_request('HEAD', url, **kw)


@contextlib.contextmanager
def http_stream(method: str, url: str, *,
                timeout: float = _DEFAULT_TIMEOUT,
                headers: Optional[dict] = None,
                use_proxy: bool = True,
                **extra: Any):
    """Sync streaming request as a context manager.

    Use for ``stream=True`` requests so the connection is closed
    deterministically on exit::

        with http_stream('GET', url, timeout=60) as resp:
            for chunk in resp.iter_content(8192):
                ...
    """
    extra.setdefault('stream', True)
    resp = http_request(method, url, timeout=timeout, headers=headers,
                        use_proxy=use_proxy, **extra)
    try:
        yield resp
    finally:
        try:
            resp.close()
        except Exception as e:
            logger.debug('[http] stream response close failed for %s: %s', url, e)


# ══════════════════════════════════════════════════════════════════
#  Async (httpx-based)
# ══════════════════════════════════════════════════════════════════

async def async_http_request(method: str, url: str, *,
                              timeout: float = _DEFAULT_TIMEOUT,
                              headers: Optional[dict] = None,
                              use_proxy: bool = True,
                              **extra: Any):
    """Async HTTP request via httpx. Returns ``httpx.Response``.

    httpx-specific keyword arguments map cleanly: ``json=``, ``params=``,
    ``data=``, ``content=``, ``files=``, ``cookies=``.
    """
    proxy = _httpx_proxy_url(url) if use_proxy else None
    follow_redirects = extra.pop(
        'follow_redirects', method.upper() in ('GET', 'HEAD'))
    verify = extra.pop('verify', True)
    client = _async_client(proxy=proxy, verify=verify)
    return await client.request(
        method, url,
        timeout=timeout,
        follow_redirects=follow_redirects,
        headers=_build_headers(headers),
        **extra,
    )


async def async_http_get(url: str, *, timeout: float = _DEFAULT_TIMEOUT,
                          headers: Optional[dict] = None,
                          params: Optional[dict] = None,
                          use_proxy: bool = True,
                          **extra: Any):
    if params is not None:
        extra['params'] = params
    return await async_http_request('GET', url, timeout=timeout,
                                     headers=headers, use_proxy=use_proxy,
                                     **extra)


async def async_http_post(url: str, *, timeout: float = _DEFAULT_TIMEOUT,
                           headers: Optional[dict] = None,
                           json: Any = None,
                           data: Any = None,
                           files: Any = None,
                           use_proxy: bool = True,
                           **extra: Any):
    if json is not None:
        extra['json'] = json
    if data is not None:
        extra['data'] = data
    if files is not None:
        extra['files'] = files
    return await async_http_request('POST', url, timeout=timeout,
                                     headers=headers, use_proxy=use_proxy,
                                     **extra)


@contextlib.asynccontextmanager
async def async_http_stream(method: str, url: str, *,
                              timeout: float = _DEFAULT_TIMEOUT,
                              headers: Optional[dict] = None,
                              use_proxy: bool = True,
                              **extra: Any):
    """Async streaming request as an async context manager.

    Yields ``httpx.Response`` for use with ``aiter_bytes()`` / ``aiter_lines()``::

        async with async_http_stream('GET', url) as resp:
            async for line in resp.aiter_lines():
                ...
    """
    proxy = _httpx_proxy_url(url) if use_proxy else None
    follow_redirects = extra.pop(
        'follow_redirects', method.upper() in ('GET', 'HEAD'))
    verify = extra.pop('verify', True)
    client = _async_client(proxy=proxy, verify=verify)
    async with client.stream(method, url,
                             timeout=timeout,
                             follow_redirects=follow_redirects,
                             headers=_build_headers(headers),
                             **extra) as resp:
        yield resp


__all__ = [
    'http_request', 'http_get', 'http_post', 'http_put',
    'http_delete', 'http_head', 'http_stream',
    'async_http_request', 'async_http_get', 'async_http_post',
    'async_http_stream', 'close_sync_http_clients',
    'close_async_http_clients', 'close_http_clients', 'http_pool_stats',
]
