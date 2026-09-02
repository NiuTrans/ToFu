"""Narrow HTTP compatibility adapters and static cache policy for Quart."""

from __future__ import annotations

import json
from typing import Any

from quart import request

from lib.log import get_logger


logger = get_logger('server.lifecycle')


async def method_override() -> None:
    """Unwrap proxy-double-encoded JSON bodies.

    The legacy ``?_method=`` query override is retired: in-tree callers send
    real HTTP verbs through the shared transport. The hook name remains part
    of the application-assembly lifecycle contract.
    """
    inbound_method = request.method
    content_type = request.content_type or ''
    if inbound_method not in ('POST', 'PUT') or 'json' not in content_type:
        return
    raw = (await request.get_data()).decode('utf-8', errors='replace')
    if not raw:
        return
    try:
        data = json.loads(raw)
        if isinstance(data, str):
            corrected_value = json.loads(data)
            corrected = json.dumps(corrected_value).encode('utf-8')
            # Quart owns the buffered body through its Body container; the
            # historical ``request._body`` assignment was a Flask-era no-op.
            request.body.clear()
            request.body.set_result(corrected)
            request._cached_json = {
                False: corrected_value,
                True: corrected_value,
            }
    except (json.JSONDecodeError, TypeError) as error:
        logger.debug('[method_override] body unwrap skipped: %s', error)


async def add_static_cache_headers(response: Any) -> Any:
    """Apply MIME and cache policy to the explicit offloaded static route."""
    if not request.path.startswith('/static/'):
        return response
    # A stale-asset self-heal target changes on every build and is never safe
    # to freeze as immutable.
    if 300 <= response.status_code < 400:
        response.headers['Cache-Control'] = 'no-store'
        return response

    if request.path.endswith(('.js', '.mjs')):
        response.content_type = 'text/javascript; charset=utf-8'
    elif request.path.endswith('.css'):
        response.content_type = 'text/css; charset=utf-8'

    if request.path == '/static/vite/manifest.json':
        response.headers['Cache-Control'] = 'no-store'
    elif request.path.startswith('/static/vite/assets/'):
        response.headers['Cache-Control'] = (
            'public, max-age=31536000, immutable')
    elif request.path.endswith('.html'):
        response.headers['Cache-Control'] = 'no-store'
    elif '/vendor/' in request.path:
        response.headers['Cache-Control'] = (
            'public, max-age=31536000, immutable')
    elif request.path.endswith(('.js', '.mjs', '.css')):
        if 'v=' in request.query_string.decode('ascii', errors='ignore'):
            response.headers['Cache-Control'] = (
                'public, max-age=604800, immutable')
        else:
            response.headers['Cache-Control'] = (
                'public, max-age=300, must-revalidate')
    else:
        response.headers['Cache-Control'] = 'public, max-age=86400'
    return response


def register_method_override(app: Any) -> bool:
    key = 'tofu_http_method_override_registered'
    if app.extensions.get(key):
        return False
    app.before_request(method_override)
    app.extensions[key] = True
    return True


def register_static_cache_headers(app: Any) -> bool:
    key = 'tofu_static_cache_headers_registered'
    if app.extensions.get(key):
        return False
    app.after_request(add_static_cache_headers)
    app.extensions[key] = True
    return True


__all__ = [
    'add_static_cache_headers',
    'method_override',
    'register_method_override',
    'register_static_cache_headers',
]
