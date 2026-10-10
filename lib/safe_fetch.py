"""Bounded, SSRF-guarded downloads for user-controlled public URLs.

This module is intentionally small and dependency-neutral: it builds on the
already-required ``requests`` package and the project's proxy configuration.
The request adapter validates *every* redirect hop immediately before it is
sent; the body reader enforces both a decoded-byte ceiling and a wall clock
deadline.  Callers that deliberately need an internal hostname must name that
hostname explicitly in their feature-specific allow-list environment variable.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import threading
import time
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

from lib.log import get_logger
from lib.proxy import proxies_for

logger = get_logger(__name__)


class SafeFetchError(RuntimeError):
    """A public-URL request was unsafe, oversized, or otherwise unusable."""


def ip_is_public(ip_string: str) -> bool:
    """Return whether an address is suitable for an untrusted public fetch."""
    try:
        ip = ipaddress.ip_address(ip_string)
    except ValueError as exc:
        logger.debug('[SafeFetch] invalid IP address %r: %s', ip_string, exc)
        return False
    mapped = getattr(ip, 'ipv4_mapped', None)
    if mapped is not None:
        ip = mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _allowed_hosts(env_name: str) -> set[str]:
    raw = (os.environ.get(env_name) or '').strip()
    return {
        host.strip().rstrip('.').lower()
        for host in raw.split(',')
        if host.strip()
    }


def validate_public_url(url: str, *, allow_hosts_env: str,
                        allow_unresolved: bool = False) -> None:
    """Reject a URL that can reach a non-public address.

    All DNS answers are checked.  An allow-list entry is an exact hostname;
    bare IP literals are never exempted because that would make metadata IPs
    one configuration typo away from exposure.
    """
    try:
        parsed = urlparse(str(url or ''))
        port = parsed.port
    except ValueError as exc:
        raise SafeFetchError(f'invalid URL/port: {exc}') from exc
    if parsed.scheme not in ('http', 'https'):
        raise SafeFetchError(
            f'unsupported scheme {parsed.scheme!r} (http/https only)')
    host = (parsed.hostname or '').rstrip('.').lower()
    if not host:
        raise SafeFetchError('missing hostname')
    if parsed.username is not None or parsed.password is not None:
        raise SafeFetchError('credentials in URL are not allowed')

    literal = None
    try:
        literal = ipaddress.ip_address(host.strip('[]'))
    except ValueError as exc:
        logger.debug('[SafeFetch] hostname %r is not an IP literal: %s', host, exc)
    if literal is None and host in _allowed_hosts(allow_hosts_env):
        return
    if literal is not None:
        if not ip_is_public(str(literal)):
            raise SafeFetchError(f'host resolves to blocked IP {literal}')
        return

    try:
        infos = socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        if allow_unresolved:
            # Registration/configuration may happen while a new personal
            # install is offline. Callers using this mode MUST revalidate at
            # the actual egress seam; a hostname that resolves now is still
            # fully checked, while a transient resolver outage does not make
            # otherwise valid configuration impossible to save.
            logger.debug('[SafeFetch] deferring DNS validation for %r: %s',
                         host, exc)
            return
        raise SafeFetchError(f'DNS resolution failed for {host!r}: {exc}') from exc
    if not infos:
        raise SafeFetchError(f'DNS returned no addresses for {host!r}')
    for info in infos:
        address = info[4][0]
        if not ip_is_public(address):
            raise SafeFetchError(
                f'host {host!r} resolves to blocked IP {address}')


class _PublicEgressAdapter(HTTPAdapter):
    """Validate the concrete request URL for the initial and redirect hops."""

    def __init__(self, allow_hosts_env: str):
        self._allow_hosts_env = allow_hosts_env
        super().__init__(pool_connections=16, pool_maxsize=16, max_retries=0)

    def send(self, request, **kwargs):
        validate_public_url(
            request.url, allow_hosts_env=self._allow_hosts_env)
        return super().send(request, **kwargs)


_local = threading.local()


def _session_for(allow_hosts_env: str) -> requests.Session:
    """One pooled session per worker thread and policy namespace."""
    sessions = getattr(_local, 'sessions', None)
    if sessions is None:
        sessions = {}
        _local.sessions = sessions
    session = sessions.get(allow_hosts_env)
    if session is None:
        session = requests.Session()
        adapter = _PublicEgressAdapter(allow_hosts_env)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        sessions[allow_hosts_env] = session
    return session


def fetch_public_bytes(
    url: str,
    *,
    max_bytes: int,
    timeout: float = 30.0,
    max_redirects: int = 3,
    allow_hosts_env: str = 'TOFU_PUBLIC_FETCH_ALLOW_HOSTS',
) -> tuple[bytes, str, str]:
    """Download a bounded public response as ``(body, content_type, final_url)``.

    ``max_bytes`` applies to decoded response bytes, so compressed transfer
    encoding cannot bypass the memory ceiling.  The response is always closed.
    """
    if max_bytes <= 0:
        raise ValueError('max_bytes must be positive')
    timeout = max(0.25, float(timeout))
    max_redirects = max(0, min(int(max_redirects), 10))
    validate_public_url(url, allow_hosts_env=allow_hosts_env)
    session = _session_for(allow_hosts_env)
    previous_redirect_cap = session.max_redirects
    session.max_redirects = max_redirects
    response = None
    started = time.monotonic()
    # A per-read timeout alone permits an endless slow trickle.  Keep a
    # generous total deadline without changing legitimate 30-second fetches.
    deadline = started + max(timeout * 2.0, timeout + 5.0)
    try:
        response = session.get(
            url,
            headers={'User-Agent': 'Tofu-SafeFetch/1.0'},
            timeout=(min(timeout, 10.0), timeout),
            stream=True,
            allow_redirects=True,
            proxies=proxies_for(url),
        )
        response.raise_for_status()
        raw_length = response.headers.get('Content-Length')
        if raw_length:
            try:
                declared = int(raw_length)
            except (TypeError, ValueError) as exc:
                logger.debug('[SafeFetch] invalid Content-Length %r: %s',
                             raw_length, exc)
                declared = -1
            if declared > max_bytes:
                raise SafeFetchError(
                    f'response too large: Content-Length={declared} > max={max_bytes}')

        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise SafeFetchError(
                    f'response too large: streamed {total} > max={max_bytes}')
            if time.monotonic() > deadline:
                raise SafeFetchError('response body exceeded total time budget')
            chunks.append(chunk)
        content_type = (response.headers.get('Content-Type') or '')
        content_type = content_type.split(';', 1)[0].strip().lower()
        return b''.join(chunks), content_type, str(response.url)
    except SafeFetchError:
        raise
    except requests.TooManyRedirects as exc:
        raise SafeFetchError(
            f'too many redirects (max {max_redirects})') from exc
    except requests.RequestException as exc:
        raise SafeFetchError(f'HTTP fetch failed: {exc}') from exc
    finally:
        session.max_redirects = previous_redirect_cap
        if response is not None:
            response.close()


__all__ = [
    'SafeFetchError', 'fetch_public_bytes', 'ip_is_public',
    'validate_public_url',
]
