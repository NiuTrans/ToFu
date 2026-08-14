"""Native-Quart static serving outside the application assembly module.

All filesystem work is injected through ``offload`` so the route never runs a
FUSE-backed stat/read on the event loop. The adapter also owns the RFC 9110
Range and conditional response policy; application assembly only supplies its
static root, timeout, logger and compatibility seams.
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
from collections.abc import Awaitable, Callable
from logging import Logger
from typing import Any
from zlib import adler32

from quart import Response, abort, request
from werkzeug.utils import safe_join


StaticRead = tuple[bytes, float, str] | None
StaticOffload = Callable[[asyncio.AbstractEventLoop, str], Awaitable[StaticRead]]
RangeGate = Callable[[Any, str, float], bool]
TimeoutProvider = Callable[[], float]


def load_static_bytes(static_dir: str, filename: str) -> StaticRead:
    """Read one path strictly beneath ``static_dir`` on a worker thread."""
    full = safe_join(static_dir, filename)
    if full is None or not os.path.isfile(full):
        return None
    with open(full, 'rb') as handle:
        data = handle.read()
    stat = os.stat(full)
    etag = '%d-%d-%d' % (
        int(stat.st_mtime), stat.st_size, adler32(data) & 0xFFFFFFFF)
    return data, stat.st_mtime, etag


def if_range_allows(if_range: Any, etag: str, mtime: float) -> bool:
    """Return whether RFC 9110 permits serving a conditional byte range."""
    if if_range is None or (if_range.etag is None and if_range.date is None):
        return True
    if if_range.etag is not None:
        return if_range.etag == etag.strip('"')
    return mtime <= if_range.date.timestamp()


async def serve_static_request(
    filename: str,
    *,
    offload: StaticOffload,
    timeout: float,
    logger: Logger,
    range_allows: RangeGate = if_range_allows,
) -> Response:
    """Build a FUSE-safe static response with exact Range semantics."""
    loop = asyncio.get_running_loop()
    try:
        result = await asyncio.wait_for(
            offload(loop, filename), timeout=timeout)
    except asyncio.TimeoutError:
        logger.critical(
            '[Static] read timed out after %.1fs for %s — FUSE stall suspected; '
            'returning 503 (loop preserved)', timeout, filename)
        abort(503)
    except OSError as exc:
        logger.error('[Static] I/O error serving %s: %s', filename, exc)
        abort(500)

    if result is None:
        abort(404)

    data, mtime, etag = result
    total = len(data)
    lower_name = filename.lower()
    if lower_name.endswith(('.js', '.mjs')):
        # Never depend on the host OS MIME database (notably macOS).
        content_type = 'text/javascript'
    else:
        content_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'

    request_range = request.range
    if (range_allows(request.if_range, etag, mtime)
            and request_range is not None
            and request_range.units == 'bytes'
            and len(request_range.ranges) == 1):
        resolved = request_range.range_for_length(total)
        if resolved is None:
            response = Response(b'', status=416, mimetype=content_type)
            response.headers['Content-Range'] = f'bytes */{total}'
            response.headers['Accept-Ranges'] = 'bytes'
            return response
        begin, end = resolved
        response = Response(data[begin:end], status=206, mimetype=content_type)
        response.headers['Content-Range'] = (
            f'bytes {begin}-{end - 1}/{total}')
        response.headers['Accept-Ranges'] = 'bytes'
        response.set_etag(etag)
        response.last_modified = mtime
        return response

    response = Response(data, mimetype=content_type)
    response.set_etag(etag)
    response.last_modified = mtime
    response.headers['Accept-Ranges'] = 'bytes'
    await response.make_conditional(
        request, accept_ranges=True, complete_length=total)
    return response


def register_static_route(
    app: Any,
    *,
    offload: StaticOffload,
    timeout: float | TimeoutProvider,
    logger: Logger,
    range_allows: RangeGate = if_range_allows,
) -> bool:
    """Register the explicit FUSE-safe Quart static route once per app.

    Providers are resolved on each request so the assembly layer can retain
    its fault-injection seams without owning a route decorator or handler.
    """
    key = 'tofu_static_route_registered'
    if app.extensions.get(key):
        return False

    async def static_route(filename: str) -> Response:
        resolved_timeout = timeout() if callable(timeout) else timeout
        return await serve_static_request(
            filename,
            offload=offload,
            timeout=float(resolved_timeout),
            logger=logger,
            range_allows=range_allows,
        )

    app.add_url_rule(
        '/static/<path:filename>',
        endpoint='tofu_static',
        view_func=static_route,
        methods=['GET'],
    )
    app.extensions[key] = True
    return True


__all__ = [
    'RangeGate', 'StaticOffload', 'TimeoutProvider', 'if_range_allows',
    'load_static_bytes', 'register_static_route', 'serve_static_request',
]
