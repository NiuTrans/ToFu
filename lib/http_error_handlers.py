"""Global Quart HTTP error mapping with stable API/HTML wire contracts."""

from __future__ import annotations

from typing import Any

from quart import current_app, make_response, request, websocket
from werkzeug.exceptions import HTTPException

from lib.api_response import (
    api_internal_error,
    api_method_not_allowed,
    api_not_found,
    api_payload_too_large,
    api_service_unavailable,
)
from lib.log import get_logger, req_id


logger = get_logger('server.lifecycle')


def is_api_request() -> bool:
    return request.path.startswith('/api/')


def safe_method_path() -> tuple[str, str]:
    """Read HTTP or websocket identity without assuming either context."""
    try:
        return request.method, request.path
    except RuntimeError:
        try:
            return 'WS', websocket.path
        except RuntimeError as error:
            logger.debug('[HTTP] no request or websocket context: %s', error)
            return '?', '?'


async def handle_not_found(_error: Any):
    """Preserve stable API and HTML 404 response shapes."""
    if request.path.startswith('/.well-known/'):
        logger.debug('404 (well-known probe): %s', request.path)
    else:
        logger.warning('404 Not Found: %s %s', request.method, request.path)
    if is_api_request():
        return api_not_found(f'Not Found: {request.path}')
    return await make_response(
        '<h2>404 — Not Found</h2><p>The requested URL was not found.</p>', 404)


async def handle_payload_too_large(_error: Any):
    if is_api_request():
        return api_payload_too_large(
            current_app.config['MAX_CONTENT_LENGTH'])
    return await make_response('<h2>413 — Payload Too Large</h2>', 413)


async def handle_method_not_allowed(_error: Any):
    if is_api_request():
        return api_method_not_allowed()
    return await make_response('<h2>405 — Method Not Allowed</h2>', 405)


async def handle_internal_error(error: BaseException):
    request_id = req_id() or '-'
    method, path = safe_method_path()
    logger.error(
        '500 ISE: [%s] %s %s', request_id, method, path, exc_info=error)
    if path.startswith('/api/'):
        return api_internal_error(error, log_traceback=False)
    return await make_response(
        f'<h2>500</h2><p>Request ID: <code>{request_id}</code></p>', 500)


async def handle_uncaught(error: BaseException):
    if isinstance(error, HTTPException):
        return error
    request_id = req_id() or '-'
    method, path = safe_method_path()

    from lib.storage import StorageError, http_status_for_storage_error
    if isinstance(error, StorageError):
        status = http_status_for_storage_error(error)
        logger.warning(
            '[%s] %d storage error code=%s operation_id=%s: %s %s',
            request_id, status, error.code, error.operation_id, method, path)
        if path.startswith('/api/'):
            from lib.api_response import api_error
            return api_error(
                error.code,
                status=status,
                message=error.message,
                retryAfter=max(1, (error.retry_after_ms or 0) // 1000),
                operationId=error.operation_id or None,
            )
        return await make_response(
            f'<h2>{status}</h2><p>Storage is unavailable. '
            f'Request ID: <code>{request_id}</code></p>', status)

    from lib.database import PoolExhaustedError
    if isinstance(error, PoolExhaustedError):
        logger.warning(
            '[%s] 503 pool-exhausted: %s %s (active=%d/%d pooled=%d '
            'tracked=%d)', request_id, method, path, error.active,
            error.max_conns, error.pooled, error.tracked)
        if path.startswith('/api/'):
            return api_service_unavailable(
                'Server busy (database pool saturated) — retry shortly',
                retry_after=2, kind='overloaded')
        return await make_response(
            f'<h2>503</h2><p>Server busy — retry shortly. '
            f'Request ID: <code>{request_id}</code></p>', 503)

    logger.error('[%s] Uncaught: %s %s: %s',
                 request_id, method, path, error, exc_info=True)
    if path.startswith('/api/'):
        return api_internal_error(error, log_traceback=False)
    return await make_response(
        f'<h2>500</h2><p>Request ID: <code>{request_id}</code></p>', 500)


def register_http_error_handlers(app: Any) -> bool:
    """Register the global exception boundary once."""
    key = 'tofu_http_error_handlers_registered'
    if app.extensions.get(key):
        return False
    app.register_error_handler(404, handle_not_found)
    app.register_error_handler(413, handle_payload_too_large)
    app.register_error_handler(405, handle_method_not_allowed)
    app.register_error_handler(500, handle_internal_error)
    app.register_error_handler(Exception, handle_uncaught)
    app.extensions[key] = True
    return True


__all__ = [
    'handle_internal_error',
    'handle_method_not_allowed',
    'handle_not_found',
    'handle_payload_too_large',
    'handle_uncaught',
    'is_api_request',
    'register_http_error_handlers',
    'safe_method_path',
]
