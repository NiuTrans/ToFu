"""Proxy-aware HTTP helpers and bounded anonymous search transport.

Entries: http_get/http_post for verticals; _search_http2_get for HTML engines.
The search envelope owns retries, proxy ordering, circuits and parsing. Its
HTTP/2 adapter owns one client/response lifetime, TLS verification and decoded
body limits. HTTPX loads only on demand; heavy fetching stays in fetch/.
"""

import os
import ssl
import time
from typing import Any, Optional

import requests

from tofu_search.log import get_logger

logger = get_logger(__name__)

__all__ = ['http_get', 'http_post']

_DEFAULT_TIMEOUT = 30
_DEFAULT_UA = ('Mozilla/5.0 (compatible; TofuSearch/1.0; +https://github.com/rangehow/tofu-search)')
_SEARCH_RESPONSE_BYTES = 2 * 1024 * 1024


def _search_http2_get(url, *, params, headers, timeout, proxies=None, deadline):
    """One anonymous search GET, preserving the envelope's selected route.

    Return a detached Requests response for existing parsers. HTTPX negotiates
    HTTP/2 when supported, otherwise HTTP/1.1; it does not add retries, cookies
    from another call, redirects, browser sessions or a new network path.
    """
    import httpx

    remaining = min(float(timeout), deadline - time.monotonic())
    if remaining <= 0:
        raise requests.Timeout('Search HTTP request budget exhausted')
    selected = requests.utils.get_environ_proxies(url) if proxies is None else proxies
    proxy = requests.utils.select_proxy(url, selected) or None
    ca_file = os.environ.get('REQUESTS_CA_BUNDLE') or os.environ.get('CURL_CA_BUNDLE')
    try:
        verify = ssl.create_default_context(cafile=ca_file) if ca_file else True
        # Each call already has an admitted engine worker. No persistent pool,
        # background worker or cross-query cookie jar is introduced here.
        with httpx.Client(http2=True, proxy=proxy, trust_env=False, verify=verify,
                          headers=headers, timeout=remaining, follow_redirects=False) as client:
            remaining = min(float(timeout), deadline - time.monotonic())
            if remaining <= 0:
                raise requests.Timeout('Search HTTP request budget exhausted')
            with client.stream('GET', url, params=params, timeout=remaining) as response:
                body = bytearray()
                if time.monotonic() >= deadline:
                    raise requests.Timeout('Search HTTP request budget exhausted')
                # Status/challenge classification needs only headers. In
                # particular, do not download a large rate-limit/error page.
                if (response.status_code == 200
                        and response.headers.get('cf-mitigated', '').lower() != 'challenge'):
                    length = response.headers.get('content-length')
                    if length is not None and (not length.isdigit()
                                                or int(length) > _SEARCH_RESPONSE_BYTES):
                        raise requests.RequestException('Search HTTP response length rejected')
                    for chunk in response.iter_bytes(chunk_size=16384):
                        if time.monotonic() >= deadline:
                            raise requests.Timeout('Search HTTP request budget exhausted')
                        if len(body) + len(chunk) > _SEARCH_RESPONSE_BYTES:
                            raise requests.RequestException('Search HTTP response exceeds byte budget')
                        body.extend(chunk)
                    if time.monotonic() >= deadline:
                        raise requests.Timeout('Search HTTP request budget exhausted')
                result = requests.Response()
                result.status_code = response.status_code
                result.headers.update(response.headers)
                result.url = str(response.url)
                result.encoding = response.encoding
                result._content = bytes(body)
                result._content_consumed = True
                return result
    except httpx.ConnectTimeout:
        raise requests.ConnectTimeout('Search HTTP connection timed out') from None
    except httpx.ReadTimeout:
        raise requests.ReadTimeout('Search HTTP response timed out') from None
    except httpx.TimeoutException:
        raise requests.Timeout('Search HTTP request timed out') from None
    except httpx.ProxyError:
        raise requests.exceptions.ProxyError('Search HTTP proxy connection failed') from None
    except httpx.NetworkError:
        raise requests.ConnectionError('Search HTTP network connection failed') from None
    except httpx.HTTPError:
        raise requests.RequestException('Search HTTP transport failed') from None
    except requests.RequestException:
        raise
    except (OSError, ValueError):
        # Transport exception strings may contain proxy credentials or URLs.
        raise requests.RequestException('Search HTTP client configuration failed') from None


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
