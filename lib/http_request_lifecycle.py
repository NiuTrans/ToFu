"""Request correlation, bounded-cardinality HTTP metrics, and access logs."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from quart import request

from lib.log import (
    get_logger,
    req_id,
    resolve_inbound_rid,
    set_req_id,
)


QUIET_PREFIXES = ('/api/browser/', '/api/desktop/', '/static/', '/api/task/')
SLOW_THRESHOLD_S = 2.0
HEAVY_RESPONSE_BYTES = 1_048_576
logger = get_logger('server.lifecycle')


def response_size(response: Any) -> int | None:
    """Return declared response bytes without materializing stream bodies."""
    try:
        raw = response.headers.get('Content-Length')
    except Exception as error:
        logger.debug('[HTTP] response Content-Length unavailable: %s', error)
        return None
    if raw is None:
        return None
    try:
        size = int(raw)
    except (ValueError, TypeError) as error:
        logger.debug('[HTTP] invalid response Content-Length %r: %s', raw, error)
        return None
    return size if size >= 0 else None


def format_size(size: object) -> str:
    """Format a byte count compactly, or return empty for unknown values."""
    if not isinstance(size, int) or size < 0:
        return ''
    if size < 1024:
        return f'{size}B'
    if size < 1024 * 1024:
        return f'{size / 1024:.1f}KB'
    return f'{size / (1024 * 1024):.1f}MB'


async def assign_request_id_and_log() -> None:
    """Adopt the browser request id and stamp request timing state."""
    rid = resolve_inbound_rid(
        request.headers.get('X-Request-ID'),
        request.args.get('_rid'),
    )
    set_req_id(rid)
    request._start_time = time.time()
    request._start_monotonic = time.monotonic()
    level = (logging.DEBUG if any(
        request.path.startswith(prefix) for prefix in QUIET_PREFIXES)
        else logging.INFO)
    logger.log(level, '[%s] → %s %s', rid, request.method, request.path)


async def log_response(response: Any) -> Any:
    """Observe one request and echo its correlation id on the response."""
    rid = req_id()
    path = request.full_path.rstrip('?')
    status = response.status_code
    is_quiet = any(request.path.startswith(prefix)
                   for prefix in QUIET_PREFIXES)
    size = response_size(response)
    size_text = format_size(size)

    started_at = getattr(request, '_start_time', None)
    if started_at is None:
        elapsed = None
        timing = f'({size_text})' if size_text else '(elapsed n/a)'
    else:
        elapsed = max(0.0, time.time() - started_at)
        timing = (f'({elapsed:.3f}s, {size_text})'
                  if size_text else f'({elapsed:.3f}s)')

    try:
        from lib.observability import (
            record_http_request,
            route_template_for_request,
        )
        monotonic_start = getattr(request, '_start_monotonic', None)
        duration = (max(0.0, time.monotonic() - monotonic_start)
                    if monotonic_start is not None else (elapsed or 0.0))
        # Route templates are bounded; raw conversation/task/request ids must
        # never enter Prometheus labels.
        record_http_request(
            request.method, route_template_for_request(request), status,
            duration)
    except Exception as metrics_error:
        logger.debug('[Metrics] HTTP observation skipped: %s', metrics_error)

    if status >= 500:
        logger.error('[%s] ← %s %s %d %s',
                     rid, request.method, path, status, timing)
    elif status >= 400:
        if status == 404 and request.path.startswith('/.well-known/'):
            logger.debug('[%s] ← %s %s %d %s',
                         rid, request.method, path, status, timing)
        else:
            logger.warning('[%s] ← %s %s %d %s',
                           rid, request.method, path, status, timing)
    elif elapsed is not None and elapsed >= SLOW_THRESHOLD_S and not is_quiet:
        logger.warning('[%s] ← %s %s %d SLOW %s',
                       rid, request.method, path, status, timing)
    elif (size is not None and size >= HEAVY_RESPONSE_BYTES
          and not is_quiet):
        logger.warning('[%s] ← %s %s %d HEAVY %s',
                       rid, request.method, path, status, timing)
    elif not is_quiet:
        logger.info('[%s] ← %s %s %d %s',
                    rid, request.method, path, status, timing)
    else:
        logger.debug('[%s] ← %s %s %d %s',
                     rid, request.method, path, status, timing)

    response.headers['X-Request-ID'] = rid
    return response


async def clear_request_id(error: BaseException | None) -> None:
    """Report abnormal teardown and clear correlation context explicitly."""
    if error:
        rid = req_id()
        if isinstance(error, asyncio.CancelledError):
            logger.debug('[%s] Request teardown: client disconnected', rid)
        else:
            logger.warning('[%s] Request teardown with exception: %s',
                           rid, error)
    set_req_id('')


def register_request_lifecycle(app: Any) -> bool:
    """Install request correlation/observation hooks exactly once."""
    key = 'tofu_http_request_lifecycle_registered'
    if app.extensions.get(key):
        return False
    app.before_request(assign_request_id_and_log)
    app.after_request(log_response)
    app.teardown_request(clear_request_id)
    app.extensions[key] = True
    return True


__all__ = [
    'HEAVY_RESPONSE_BYTES',
    'QUIET_PREFIXES',
    'SLOW_THRESHOLD_S',
    'assign_request_id_and_log',
    'clear_request_id',
    'format_size',
    'log_response',
    'register_request_lifecycle',
    'response_size',
]
