"""tofu_search.http_client — Minimal proxy-aware HTTP helper.

Replaces chatui's lib.http_client for the vertical-search module. Honors the
standard HTTP(S)_PROXY env vars (requests does this by default) and applies a
default timeout + User-Agent. Kept intentionally small — the heavy fetch
pipeline lives in tofu_search.fetch.
"""

from typing import Any, Optional

import requests

from tofu_search.log import get_logger

logger = get_logger(__name__)

__all__ = ['http_get', 'http_post']

_DEFAULT_TIMEOUT = 30
_DEFAULT_UA = ('Mozilla/5.0 (compatible; TofuSearch/1.0; +https://github.com/rangehow/tofu-search)')


def http_get(url: str, *, timeout: float = _DEFAULT_TIMEOUT,
             headers: Optional[dict] = None,
             params: Optional[dict] = None,
             use_proxy: bool = True,
             **extra: Any) -> requests.Response:
    """Issue a sync GET request.

    Args:
        url: Target URL.
        timeout: Request timeout in seconds.
        headers: Optional headers (merged over a default UA).
        params: Optional query-string params.
        use_proxy: When False, bypass any env proxy for this call.
        **extra: Passed through to ``requests.get``.

    Returns:
        The ``requests.Response`` (caller checks ``.ok``).
    """
    final_headers = {'User-Agent': _DEFAULT_UA}
    if headers:
        final_headers.update(headers)
    if params is not None:
        extra['params'] = params
    if not use_proxy:
        extra['proxies'] = {'http': None, 'https': None}
    return requests.get(url, timeout=timeout, headers=final_headers, **extra)


def http_post(url: str, *, json: Optional[Any] = None,
              timeout: float = _DEFAULT_TIMEOUT,
              headers: Optional[dict] = None,
              use_proxy: bool = True,
              **extra: Any) -> requests.Response:
    """Issue a sync POST request with a JSON body.

    Args:
        url: Target URL.
        json: Object serialised as the JSON request body.
        timeout: Request timeout in seconds.
        headers: Optional headers (merged over a default UA).
        use_proxy: When False, bypass any env proxy for this call.
        **extra: Passed through to ``requests.post``.

    Returns:
        The ``requests.Response`` (caller checks ``.ok``).
    """
    final_headers = {'User-Agent': _DEFAULT_UA}
    if headers:
        final_headers.update(headers)
    if not use_proxy:
        extra['proxies'] = {'http': None, 'https': None}
    return requests.post(url, timeout=timeout, headers=final_headers,
                         json=json, **extra)
