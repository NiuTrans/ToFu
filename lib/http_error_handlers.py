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
)
from lib.log import get_logger, req_id


logger = get_logger('server.lifecycle')


def _explicit_exception_info(error: BaseException) -> tuple[type, BaseException, Any]:
    """Return logging ``exc_info`` without depending on ``sys.exc_info()``."""
    original = getattr(error, 'original_exception', None)
    failure = original if isinstance(original, BaseException) else error
    return type(failure), failure, failure.__traceback__


def _v4_problem(
    *, status: int, code: str, title: str, detail: str,
):
    from lib.api_v4 import is_api_v4_path, problem_response
    if not is_api_v4_path(request.path):
        return None
    return problem_response(
        status=status,
        code=code,
        title=title,
        detail=detail,
        instance=request.path,
    )


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
    v4_response = _v4_problem(
        status=404,
        code='not_found',
        title='Resource not found',
        detail='The requested API v4 resource does not exist.',
    )
    if v4_response is not None:
        return v4_response
    if is_api_request():
        return api_not_found(f'Not Found: {request.path}')
    return await make_response(
        '<h2>404 — Not Found</h2><p>The requested URL was not found.</p>', 404)


async def handle_payload_too_large(_error: Any):
    v4_response = _v4_problem(
        status=413,
        code='payload_too_large',
        title='Payload too large',
        detail='The request body exceeds the configured maximum size.',
    )
    if v4_response is not None:
        return v4_response
    if is_api_request():
        return api_payload_too_large(
            current_app.config['MAX_CONTENT_LENGTH'])
    return await make_response('<h2>413 — Payload Too Large</h2>', 413)


async def handle_method_not_allowed(_error: Any):
    v4_response = _v4_problem(
        status=405,
        code='method_not_allowed',
        title='Method not allowed',
        detail='This API v4 resource does not support the requested method.',
    )
    if v4_response is not None:
        return v4_response
    if is_api_request():
        return api_method_not_allowed()
    return await make_response('<h2>405 — Method Not Allowed</h2>', 405)


async def handle_internal_error(error: BaseException):
    request_id = req_id() or '-'
    method, path = safe_method_path()
    logger.error(
        '500 ISE: [%s] %s %s', request_id, method, path,
        exc_info=_explicit_exception_info(error))
    v4_response = _v4_problem(
        status=500,
        code='internal_error',
        title='Internal server error',
        detail='The server could not complete this API v4 request.',
    )
    if v4_response is not None:
        return v4_response
    if path.startswith('/api/'):
        return api_internal_error(error, log_traceback=False)
    return await make_response(
        f'<h2>500</h2><p>Request ID: <code>{request_id}</code></p>', 500)


async def handle_uncaught(error: BaseException):
    if isinstance(error, HTTPException):
        status = int(error.code or 500)
        if status >= 400:
            v4_response = _v4_problem(
                status=status,
                code='http_error',
                title=error.name or 'HTTP error',
                detail=error.description or 'The API v4 request failed.',
            )
            if v4_response is not None:
                return v4_response
        return error
    request_id = req_id() or '-'
    method, path = safe_method_path()

    from lib.storage import StorageError, http_status_for_storage_error
    if isinstance(error, StorageError):
        status = http_status_for_storage_error(error)
        storage_log = logger.error if status >= 500 else logger.warning
        storage_log(
            '[%s] %d storage error code=%s operation_id=%s retryable=%s '
            'retry_after_ms=%s message=%s: %s %s',
            request_id, status, error.code, error.operation_id,
            error.retryable, error.retry_after_ms, error.message, method, path)
        v4_response = _v4_problem(
            status=status,
            code='storage_unavailable',
            title='Storage request failed',
            detail='The storage authority could not complete this request.',
        )
        if v4_response is not None:
            return v4_response
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

    logger.error('[%s] Uncaught: %s %s: %s',
                 request_id, method, path, error,
                 exc_info=_explicit_exception_info(error))
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
