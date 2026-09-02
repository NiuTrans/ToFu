"""Request correlation, bounded-cardinality HTTP metrics, and access logs."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable, Sequence
from typing import Any

from quart import current_app, request, websocket

from lib.log import (
    get_logger,
    req_id,
    resolve_inbound_rid,
    set_req_id,
)


QUIET_PREFIXES = ('/api/browser/', '/api/desktop/', '/static/', '/api/task/')
# These failures already emit one domain-owned structured WARNING. Their
# per-request row remains available in access.log/DEBUG without duplicating
# every retry into the application incident plane.
SEMANTICALLY_REPORTED_CLIENT_ERRORS = frozenset({
    ('POST', '/api/browser/poll', 426),
})
SLOW_THRESHOLD_S = 2.0
HEAVY_RESPONSE_BYTES = 1_048_576
logger = get_logger('server.lifecycle')


def _routine_level() -> int:
    """常规请求行的级别。默认 DEBUG:HTTP 流水的权威记录是 access.log,
    app.log 是业务信号面——每请求两行的常规流水(实测 17 万行/天,占
    app.log INFO 量约 2/3)会把 LLM 要读的差分信号淹掉。4xx/5xx/SLOW/
    HEAVY 不受影响,仍是 WARNING/ERROR。TOFU_HTTP_REQUEST_LOG=1 恢复 INFO。"""
    return (logging.INFO if os.environ.get('TOFU_HTTP_REQUEST_LOG', '0')
            .strip().lower() in ('1', 'true', 'yes', 'on')
            else logging.DEBUG)


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
        else _routine_level())
    logger.log(level, '[%s] → %s %s', rid, request.method, request.path)


async def enforce_storage_write_fence():
    """Fail closed for new production writes while storage is unavailable."""
    if request.method in {'GET', 'HEAD', 'OPTIONS'}:
        return None
    # The file-picker browse operation uses POST for its JSON body but only
    # reads the filesystem. It must remain usable while the conversation DB is
    # recovering; treating every POST as a write made the project panel show a
    # misleading database 503.
    if request.path == '/api/v1/project/browse':
        return None
    # Factories used by tests and offline embedders do not own the production
    # Sidecar lifecycle.  Their existing explicit storage seams remain in
    # force; the process-wide fence applies only once production registered it.
    lifecycle = current_app.extensions.get('tofu_production_lifecycle')
    if lifecycle is None:
        return None
    import lib.storage as storage
    status = storage.storage_status()
    if lifecycle.get('status') == 'ready' and status.get('ready'):
        return None
    from lib.api_response import api_error
    return api_error(
        'database_unavailable',
        status=503,
        message='Storage writes are temporarily fenced',
        retryAfter=1,
        storageState=status.get('state') or 'unavailable',
    )


async def enforce_distributed_preview_write_fence():
    """Reject mutating HTTP requests before legacy route code can run."""
    if not current_app.extensions.get(
            'tofu_distributed_preview_read_only', False):
        return None
    if request.method in {'GET', 'HEAD', 'OPTIONS'}:
        return None
    from lib.api_v4 import problem_response
    return problem_response(
        status=503,
        code='distributed_preview_read_only',
        title='Distributed preview is read-only',
        detail=(
            'This build validates external storage and routing only; writes '
            'remain disabled until durable distributed execution is released.'
        ),
        instance=request.path,
        headers={'Retry-After': '3600'},
    )


async def enforce_distributed_preview_websocket_fence():
    """Reject every WebSocket handshake in the read-only preview."""
    if not current_app.extensions.get(
            'tofu_distributed_preview_read_only', False):
        return None
    from lib.api_v4 import problem_response
    return problem_response(
        status=503,
        code='distributed_preview_read_only',
        title='Distributed preview is read-only',
        detail='WebSocket sessions are disabled during the read-only preview.',
        instance=websocket.path,
        headers={'Retry-After': '3600'},
    )


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
        response_key = (request.method, request.path, status)
        if response_key in SEMANTICALLY_REPORTED_CLIENT_ERRORS:
            logger.debug('[%s] ← %s %s %d %s',
                         rid, request.method, path, status, timing)
        elif status == 404 and request.path.startswith('/.well-known/'):
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
        logger.log(_routine_level(), '[%s] ← %s %s %d %s',
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


def register_request_lifecycle(
    app: Any,
    *,
    before_storage_write_fence: Sequence[Callable[[], Any]] = (),
    distributed_preview_read_only: bool = False,
) -> bool:
    """Install ordered correlation, policy, storage, and observation hooks."""
    key = 'tofu_http_request_lifecycle_registered'
    if app.extensions.get(key):
        return False
    app.extensions['tofu_distributed_preview_read_only'] = bool(
        distributed_preview_read_only)
    app.before_request(assign_request_id_and_log)
    for hook in before_storage_write_fence:
        app.before_request(hook)
    app.before_request(enforce_distributed_preview_write_fence)
    app.before_request(enforce_storage_write_fence)
    app.before_websocket(enforce_distributed_preview_websocket_fence)
    app.after_request(log_response)
    app.teardown_request(clear_request_id)
    app.extensions[key] = True
    return True


__all__ = [
    'HEAVY_RESPONSE_BYTES',
    'QUIET_PREFIXES',
    'SEMANTICALLY_REPORTED_CLIENT_ERRORS',
    'SLOW_THRESHOLD_S',
    'assign_request_id_and_log',
    'clear_request_id',
    'enforce_distributed_preview_websocket_fence',
    'enforce_distributed_preview_write_fence',
    'enforce_storage_write_fence',
    'format_size',
    'log_response',
    'register_request_lifecycle',
    'response_size',
]
