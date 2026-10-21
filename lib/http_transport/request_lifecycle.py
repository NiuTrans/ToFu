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
# Expected-unavailable 503s: these are the readiness CONTRACT, not failures.
# ``/api/ready`` answers 503 until the boot is complete and the frontend polls
# it BY DESIGN; a boot-gated request is likewise a truthful "not yet". Logging
# them at ERROR put 41 non-incidents into a 4-minute boot window (2026-10-21)
# and diluted the lines that did mean something. The access log still records
# every one.
_EXPECTED_UNAVAILABLE_PATHS = frozenset({
    '/api/ready', '/health/ready', '/health/startup',
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
    request._start_time = time.time()
    request._start_monotonic_ns = time.monotonic_ns()
    request._start_monotonic = request._start_monotonic_ns / 1_000_000_000
    rid = resolve_inbound_rid(
        request.headers.get('X-Request-ID'),
        request.args.get('_rid'),
    )
    set_req_id(rid)
    level = (logging.DEBUG if any(
        request.path.startswith(prefix) for prefix in QUIET_PREFIXES)
        else _routine_level())
    logger.log(level, '[%s] → %s %s', rid, request.method, request.path)


def request_execution_clock():
    """Carry the server ingress capture explicitly into one new execution.

    Bare route/test assemblies without lifecycle middleware start here. The
    clock never lives in ambient context or user-supplied/durable config.
    """
    from lib.agent_core.execution_clock import ExecutionClock

    epoch = getattr(request, '_start_time', None)
    monotonic_ns = getattr(request, '_start_monotonic_ns', None)
    if epoch is None or monotonic_ns is None:
        return ExecutionClock.start()
    return ExecutionClock.start(
        origin_at_ms=int(epoch * 1000), origin_monotonic_ns=monotonic_ns)


# Recovery endpoints stay POST-able while storage is down: they ARE the
# operator's fix for a wedged database (a restart re-execs the worker, boot
# retry re-runs the failed boot phase, shutdown stops cleanly), and their own
# state never touches the fenced storage authority — lifecycle approvals live
# in a JSON file store and the boot runner is in-process. The human-approval
# gate and admin scope, not this fence, are their security boundary. Fencing
# them made the UI restart button answer database_unavailable precisely when
# a restart was the recovery action.
_STORAGE_WRITE_FENCE_EXEMPT_PATHS = frozenset({
    # The file-picker browse operation uses POST for its JSON body but only
    # reads the filesystem. It must remain usable while the conversation DB is
    # recovering; treating every POST as a write made the project panel show a
    # misleading database 503.
    '/api/v1/project/browse',
    '/api/v4/boot/retry',
    '/api/v1/update/restart',
    '/api/v4/update/restart',
    '/api/v1/update/shutdown',
    '/api/v4/update/shutdown',
})

# The human's approval decision is the second leg of the UI restart flow
# (register pending → decide → retry with token); fencing it strands an
# already-approved recovery halfway through.
_STORAGE_WRITE_FENCE_EXEMPT_DECIDE_PREFIXES = (
    '/api/v1/update/lifecycle-approvals/',
    '/api/v4/update/lifecycle-approvals/',
)


async def enforce_storage_write_fence():
    """Fail closed for new production writes while storage is unavailable."""
    if request.method in {'GET', 'HEAD', 'OPTIONS'}:
        return None
    if request.path in _STORAGE_WRITE_FENCE_EXEMPT_PATHS:
        return None
    if (request.path.endswith('/decide') and request.path.startswith(
            _STORAGE_WRITE_FENCE_EXEMPT_DECIDE_PREFIXES)):
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


# Probe/boot endpoints stay answerable while the background boot runs: the
# orchestrator, the lifecycle manager and the browser splash all depend on
# them to observe the boot itself.  Everything else under /api/ is fenced
# until the runner reports ready (default deny).
_BOOT_GATE_OPEN_PATHS = frozenset({
    '/api/health',
    '/api/ready',
    '/api/v4/boot',
    '/api/v4/boot/retry',
    '/api/client-error',
    '/health/live',
    '/health/ready',
    '/health/startup',
})


async def enforce_boot_gate():
    """Return a truthful 503 for API calls the unfinished boot cannot serve.

    The production lifespan opens the HTTP listener after the inline phases
    so the browser can paint immediately; storage/LLM/worker routes are not
    meaningful until the background boot runner finishes.  Absent a runner
    (test factories, offline embedders) the gate stays open and their
    existing storage seams remain in force — same precedent as
    ``enforce_storage_write_fence``.
    """
    runner = current_app.extensions.get('tofu_boot_runner')
    if runner is None:
        return None
    status = runner.status
    if status == 'ready':
        return None
    path = request.path
    if not path.startswith('/api/') or path in _BOOT_GATE_OPEN_PATHS:
        return None
    from lib.api_response import api_error
    if status == 'failed':
        snapshot = runner.snapshot()
        return api_error(
            'server_boot_failed',
            status=503,
            message=(
                'Server boot failed; POST /api/v4/boot/retry re-runs from the '
                'failed phase'),
            retryAfter=5,
            boot={
                'status': 'failed',
                'failedPhase': snapshot['failedPhase'],
                'error': snapshot['error'],
                'retryCount': snapshot['retryCount'],
            },
        )
    snapshot = runner.snapshot()
    return api_error(
        'server_booting',
        status=503,
        message='Server is still starting',
        retryAfter=1,
        boot={
            'status': status,
            'currentPhase': snapshot['currentPhase'],
            'phaseTotal': snapshot['phaseTotal'],
        },
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

    if status == 503 and request.path in _EXPECTED_UNAVAILABLE_PATHS:
        logger.debug('[%s] ← %s %s %d %s (expected: not ready)',
                     rid, request.method, path, status, timing)
    elif status >= 500:
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
    app.before_request(enforce_boot_gate)
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
    'enforce_boot_gate',
    'enforce_distributed_preview_websocket_fence',
    'enforce_distributed_preview_write_fence',
    'enforce_storage_write_fence',
    'format_size',
    'log_response',
    'register_request_lifecycle',
    'response_size',
]
