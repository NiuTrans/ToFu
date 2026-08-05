"""Unified HTTP client — sync (requests) + async (httpx).

Replaces ad-hoc ``requests.get/post`` calls scattered across the
project. The unified API:

  - **Auto-applies** ``proxies_for(url)`` from ``lib.proxy`` so internal
    URLs bypass the corporate proxy and external URLs use it.
  - **Default timeout** of 30s (override per-call).
  - **Default User-Agent** of ``Tofu/<version>`` (override via headers).
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

import contextlib
import time
from typing import Any, Optional

import requests

import lib as _lib
from lib.log import get_logger
from lib.proxy import async_proxy_for
from lib.proxy import proxies_for, report_outcome as _report_outcome

logger = get_logger(__name__)


# ── Defaults ──────────────────────────────────────────────────────

_DEFAULT_TIMEOUT = 30.0
_DEFAULT_USER_AGENT = f'Tofu/{getattr(_lib, "__version__", "0.0.0-dev")}'


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
    if use_proxy and 'proxies' not in extra:
        extra['proxies'] = proxies_for(url)
    _t0 = time.monotonic()
    try:
        resp = requests.request(
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
    import httpx
    proxy = _httpx_proxy_url(url) if use_proxy else None
    async with httpx.AsyncClient(
        proxy=proxy,
        timeout=httpx.Timeout(timeout),
        follow_redirects=extra.pop('follow_redirects', True),
        verify=extra.pop('verify', True),
    ) as client:
        return await client.request(
            method, url,
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
    import httpx
    proxy = _httpx_proxy_url(url) if use_proxy else None
    follow_redirects = extra.pop('follow_redirects', True)
    verify = extra.pop('verify', True)
    async with httpx.AsyncClient(
        proxy=proxy,
        timeout=httpx.Timeout(timeout),
        follow_redirects=follow_redirects,
        verify=verify,
    ) as client:
        async with client.stream(method, url,
                                  headers=_build_headers(headers),
                                  **extra) as resp:
            yield resp


__all__ = [
    'http_request', 'http_get', 'http_post', 'http_put',
    'http_delete', 'http_head', 'http_stream',
    'async_http_request', 'async_http_get', 'async_http_post',
    'async_http_stream',
]
