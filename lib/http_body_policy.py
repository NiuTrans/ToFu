"""Finite Quart request-body timeout and route-size policy."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from quart import request

from lib.api_response import api_payload_too_large
from runtime_guards import resolve_resource_budget


DEFAULT_ROUTE_BODY_CAPS = (
    ('/api/v1/videos/upload', 512 * 1024 * 1024),
    # Base64 expands a 32 MiB image by 4/3; retain JSON/multipart headroom.
    ('/api/images/upload', 46 * 1024 * 1024),
    # Poll results can carry a bounded full-page screenshot, but they are not
    # an upload transport and must never inherit the 512 MiB application cap.
    ('/api/browser/poll', 32 * 1024 * 1024),
)
DEFAULT_BODY_CAP = 50 * 1024 * 1024
DEFAULT_LONG_UPLOAD_PREFIXES = (
    '/api/v1/videos/upload',
    '/api/images/upload',
    '/api/pdf/parse',
    '/api/pdf/vlm-parse',
    '/api/doc/parse',
    '/api/paper/upload',
    '/api/v1/audio/transcribe',
    '/api/v1/project/upload',
)


@dataclass(frozen=True)
class HttpBodyPolicy:
    body_timeout: int
    upload_body_timeout: int
    route_caps: tuple[tuple[str, int], ...] = DEFAULT_ROUTE_BODY_CAPS
    default_cap: int = DEFAULT_BODY_CAP
    long_upload_prefixes: tuple[str, ...] = DEFAULT_LONG_UPLOAD_PREFIXES


def bounded_http_timeout_env(
    name: str,
    default: int,
    minimum: int = 30,
    maximum: int = 7200,
    *,
    environ: Mapping[str, str] | None = None,
    logger: Any = None,
) -> int:
    """Read and clamp a finite timeout; invalid input uses the default."""
    env = os.environ if environ is None else environ
    raw = env.get(name, '') or str(default)
    try:
        value = int(raw)
    except (ValueError, TypeError) as exc:
        (logger or logging.getLogger('server')).warning(
            '[BodyTimeout] invalid %s=%r; using %ss: %s',
            name, raw, default, exc)
        return int(default)
    return max(int(minimum), min(int(maximum), value))


def build_http_body_policy(
    environ: Mapping[str, str] | None = None,
    *,
    route_caps: tuple[tuple[str, int], ...] | None = None,
    default_cap: int = DEFAULT_BODY_CAP,
    long_upload_prefixes: tuple[str, ...] = DEFAULT_LONG_UPLOAD_PREFIXES,
) -> HttpBodyPolicy:
    """Build one immutable policy snapshot for an application instance."""
    if route_caps is None:
        browser_poll_mib = resolve_resource_budget(
            'TOFU_BROWSER_POLL_BODY_MAX_MIB', environ,
            minimum=16, maximum=64)
        route_caps = tuple(
            (prefix, browser_poll_mib * 1024 * 1024)
            if prefix == '/api/browser/poll' else (prefix, cap)
            for prefix, cap in DEFAULT_ROUTE_BODY_CAPS
        )
    body_timeout = bounded_http_timeout_env(
        'TOFU_HTTP_BODY_TIMEOUT', 300, environ=environ)
    upload_timeout = bounded_http_timeout_env(
        'TOFU_HTTP_UPLOAD_BODY_TIMEOUT', 1800,
        minimum=body_timeout, environ=environ)
    return HttpBodyPolicy(
        body_timeout=body_timeout,
        upload_body_timeout=upload_timeout,
        route_caps=route_caps,
        default_cap=default_cap,
        long_upload_prefixes=long_upload_prefixes,
    )


async def enforce_http_body_policy(policy: HttpBodyPolicy):
    """Apply timeout and route-local byte limits before request parsing.

    Quart constructs its body accumulator with the broad application upload
    ceiling.  Tightening that accumulator here is what also bounds chunked
    requests whose sender omits ``Content-Length``; the declared-length check
    remains the fast path that rejects without awaiting any body bytes.
    """
    if any(request.path.startswith(prefix)
           for prefix in policy.long_upload_prefixes):
        # Quart copies the app default at ASGI-scope creation, then consults
        # this instance attribute during body/form parsing.
        request.body_timeout = policy.upload_body_timeout

    cap = policy.default_cap
    for prefix, route_cap in policy.route_caps:
        if request.path.startswith(prefix):
            cap = route_cap
            break

    body = getattr(request, 'body', None)
    if body is not None:
        current_cap = getattr(body, '_max_content_length', None)
        if current_cap is None or cap < current_cap:
            # Quart has no public per-request setter; Body.append() consults
            # this exact field for every incoming chunk.  Keep the compatibility
            # seam here, covered by a transport test, instead of duplicating an
            # ASGI request implementation.
            body._max_content_length = cap
        buffered = getattr(body, '_data', None)
        if buffered is not None and len(buffered) > cap:
            buffered_bytes = len(buffered)
            body.clear()
            logging.getLogger('server').warning(
                '[BodyCap] %s %s rejected: buffered=%d > cap=%d',
                request.method, request.path, buffered_bytes, cap)
            return api_payload_too_large(cap)

    content_length = request.content_length or 0
    if content_length <= 0:
        return None
    if content_length <= cap:
        return None

    logging.getLogger('server').warning(
        '[BodyCap] %s %s rejected: Content-Length=%d > cap=%d',
        request.method, request.path, content_length, cap)
    return api_payload_too_large(cap)


def register_http_body_policy(app: Any, policy: HttpBodyPolicy) -> bool:
    """Configure Quart and register one policy hook exactly once."""
    key = 'tofu_http_body_policy_registered'
    if app.extensions.get(key):
        return False

    async def _enforce_http_body_policy():
        return await enforce_http_body_policy(policy)

    app.config['RESPONSE_TIMEOUT'] = None
    app.config['BODY_TIMEOUT'] = policy.body_timeout
    app.before_request(_enforce_http_body_policy)
    app.extensions[key] = True
    app.extensions['tofu_http_body_policy'] = policy
    return True


__all__ = [
    'DEFAULT_BODY_CAP',
    'DEFAULT_LONG_UPLOAD_PREFIXES',
    'DEFAULT_ROUTE_BODY_CAPS',
    'HttpBodyPolicy',
    'bounded_http_timeout_env',
    'build_http_body_policy',
    'enforce_http_body_policy',
    'register_http_body_policy',
]
