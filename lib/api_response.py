"""Unified API response helpers.

Replaces 446 ad-hoc ``return jsonify(...)`` patterns (44 multi-line /
extra-key responses are deliberately kept as raw ``jsonify`` calls) with
a small set of helpers that always emit a consistent
``{ok, error?, ...payload}`` shape. Centralises:
  - HTTP status codes (200 / 400 / 401 / 403 / 404 / 409 / 413 / 500)
  - Error envelope creation (delegates to ``lib.error_envelope``)
  - Optional ``request_id`` injection for traceability
  - Logging policy at boundaries (5xx auto-logs traceback)

Migration philosophy
====================
Existing routes use a mix of two response shapes:

  Legacy:    return jsonify({'error': 'Task not found'}), 404
             return jsonify({'ok': True})
  Envelope:  return jsonify({'ok': False, 'error': envelope_dict}), 400

The frontend tolerates *both* shapes. To migrate without breaking
clients, every helper here emits a **superset**: it always sets
``ok``, AND when the response is an error, it always sets ``error`` —
either as a string (legacy) or as an envelope (preferred). This means:

  * old frontend code that checks ``data.error`` still works
  * new frontend code can check ``data.ok`` and ``data.error.kind``

The helpers are intentionally lightweight — they do NOT enforce a
schema or validate the payload; they just standardise the success/error
contract and the HTTP status mapping.

Public API
----------
  api_ok(data=None, **extras)              → 200
  api_created(data=None, **extras)         → 201
  api_no_content()                         → 204
  api_payload(payload, status=200)         → result-dict passthrough + explicit status
  api_error(error, status=400, **extras)   → 400 / custom
  api_bad_request(error, **extras)         → 400
  api_unauthorized(error='Unauthorized')   → 401
  api_forbidden(error='Forbidden')         → 403
  api_not_found(what='not_found')          → 404
  api_conflict(error)                      → 409
  api_payload_too_large(max_bytes)         → 413
  api_internal_error(exc=None, **extras)   → 500 (auto-logs)

Each helper returns a Flask/Quart ``(response, status_code)`` tuple
ready to be returned from the view function.

Each accepts ``**extras`` for adding additional top-level fields
(e.g. ``api_ok(taskId='abc-123', cursor=42)``).
"""

from __future__ import annotations

from typing import Any

from flask import Response, jsonify

from lib.error_envelope import from_exception
from lib.log import get_logger

logger = get_logger(__name__)


# ── Server-Sent Events ────────────────────────────────────────────────

# The canonical SSE response headers. Duplicated verbatim across every
# streaming route (chat / agent-run / tasks / compat-openai / compat-
# anthropic / chat-direct) before centralisation here:
#   * ``no-cache, no-transform`` — proxies must not buffer/rewrite the body.
#   * ``X-Accel-Buffering: no``  — disable nginx response buffering.
#   * ``Connection: keep-alive`` — hold the socket open for the stream.
_SSE_HEADERS = {
    'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-cache, no-transform',
    'X-Accel-Buffering': 'no',
    'Connection': 'keep-alive',
}


def sse_response(generator, *, extra_headers: dict | None = None,
                 timeout_none: bool = False) -> Response:
    """Build a ``text/event-stream`` Response with the canonical SSE headers.

    Replaces the copy-pasted ``Response(gen, mimetype='text/event-stream',
    headers={...4 keys...})`` block at every streaming endpoint.

    Args:
        generator: The SSE frame generator (sync or async iterable) passed
            straight to the Response body.
        extra_headers: Optional headers merged on top of the canonical set
            (e.g. ``{'X-Tofu-Task-Id': task_id}`` on the task-backed routes).
        timeout_none: When True, sets ``resp.timeout = None`` to disable the
            ASGI server's response timeout for long-lived streams (matches the
            behaviour the UI chat stream relied on).

    Returns:
        A Flask/Quart ``Response`` ready to return from the view.
    """
    headers = dict(_SSE_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    resp = Response(generator, mimetype='text/event-stream', headers=headers)
    if timeout_none:
        resp.timeout = None
    return resp


# ── Internal helpers ─────────────────────────────────────────────────

def _normalize_error(error: Any, *, default_kind: str = 'generic',
                     context: str = '', source: str = '') -> Any:
    """Convert ``error`` into the shape the response should carry.

    Rules
    -----
    * ``None``  → ``None`` (caller's responsibility to provide one)
    * ``dict`` that's already an envelope → pass through unchanged
    * ``dict`` that's NOT an envelope → keep as-is (caller knows best)
    * ``BaseException`` → typed envelope via ``from_exception``.
      Special case: ``lib.request_parser.BadRequest`` → string message
      (matches the legacy frontend pattern that reads ``data.error`` as
      a human-readable string for missing/invalid fields).
    * ``str`` → preserve the string (most existing JS reads
      ``data.error`` as a string with backwards-compat fallback)

    The helper does NOT force everything into envelopes because doing so
    would break >80 frontend call sites that read string errors directly.
    Routes that want envelopes can pass an envelope dict.
    """
    if error is None:
        return None
    if isinstance(error, BaseException):
        # Special-case BadRequest from request_parser — emit as string
        # so frontend ``data.error`` reads naturally
        try:
            from lib.request_parser import BadRequest as _BadReq
            if isinstance(error, _BadReq):
                return str(error)
        except ImportError:
            pass
        return from_exception(error, context=context, source=source,
                              kind=default_kind if default_kind else None)
    if isinstance(error, dict):
        return error  # envelope or arbitrary dict
    if isinstance(error, str):
        return error
    return str(error)


def _attach_request_id(payload: dict) -> dict:
    """If a request_id is set in lib.log thread-locals, include it.

    Useful for support: a user pasting an error message gives us a
    traceable thread of log lines.
    """
    try:
        from lib.log import req_id
        rid = req_id()
        if rid:
            payload.setdefault('request_id', rid)
    except Exception as e:
        logger.debug('[api_response] request_id lookup skipped: %s', e)
    return payload


# ── Success helpers ───────────────────────────────────────────────────

def api_ok(data: Any = None, **extras):
    """Return a 200 OK with ``{ok: True, ...}``.

    Parameters
    ----------
    data : dict | None
        If a dict, its fields are merged into the response top-level.
        If None or non-dict, ignored (use ``extras`` for top-level fields).
    **extras
        Additional top-level fields, e.g. ``api_ok(taskId='abc',
        status='aborting')``. ``status`` here is treated as a body
        field, NOT the HTTP status code — use ``api_created`` /
        ``api_error(..., status=N)`` when you need a non-200 code.
    """
    payload: dict = {'ok': True}
    if isinstance(data, dict):
        payload.update(data)
    if extras:
        payload.update(extras)
    return jsonify(payload), 200


def api_created(data: Any = None, **extras):
    """Return 201 Created. Same shape as api_ok."""
    payload: dict = {'ok': True}
    if isinstance(data, dict):
        payload.update(data)
    if extras:
        payload.update(extras)
    return jsonify(payload), 201


def api_no_content():
    """Return 204 No Content (empty body)."""
    return ('', 204)


def api_payload(payload: dict, status: int = 200, **extras):
    """Return a lib-layer result dict top-level with an explicit HTTP status.

    The primitive for the **result-passthrough idiom**: the lib function
    already returned ``{ok, error?, ...}`` and the route only chooses the
    HTTP status (e.g. the Project Brain routes' ``if not result.get('ok'):
    return ... , 409|400``). ``api_error`` is the WRONG tool here — it would
    nest the result under a single ``error`` key instead of preserving its
    top-level shape.

    Wire contract (additive only):
      * ``ok`` — kept from the payload when present; otherwise defaults to
        ``status < 400``.
      * ``request_id`` — attached on 4xx/5xx, same as ``api_error``.
      * every payload key survives top-level, byte-identical.
    """
    body = dict(payload) if isinstance(payload, dict) else {'data': payload}
    body.setdefault('ok', status < 400)
    if extras:
        body.update(extras)
    if status >= 400:
        _attach_request_id(body)
    return jsonify(body), status


# ── Error helpers ─────────────────────────────────────────────────────

def api_error(error: Any, *, status: int = 400, kind: str = '',
              context: str = '', source: str = '', **extras):
    """Return a 4xx/5xx with ``{ok: False, error: ...}``.

    Parameters
    ----------
    error : str | dict | BaseException
        - ``str``: passed through verbatim (legacy)
        - ``dict``: passed through; if it's an envelope we don't re-wrap
        - ``BaseException``: converted via ``from_exception``
    status : int
        HTTP status. Defaults to 400.
    kind : str
        When ``error`` is an exception and you want a specific envelope
        kind, override it here. Otherwise auto-classified.
    context, source : str
        Diagnostic fields for the envelope.
    **extras
        Additional top-level fields (e.g. ``request_id``).
    """
    norm = _normalize_error(error, default_kind=kind, context=context, source=source)
    payload: dict = {'ok': False}
    if norm is not None:
        payload['error'] = norm
    if extras:
        payload.update(extras)
    _attach_request_id(payload)
    return jsonify(payload), status


def api_bad_request(error: Any = 'Bad request', **extras):
    """Return 400 Bad Request."""
    return api_error(error, status=400, **extras)


def api_unauthorized(error: Any = 'Unauthorized', **extras):
    """Return 401 Unauthorized."""
    return api_error(error, status=401, **extras)


def api_forbidden(error: Any = 'Forbidden', **extras):
    """Return 403 Forbidden."""
    return api_error(error, status=403, **extras)


def api_not_found(what: Any = 'not_found', **extras):
    """Return 404 Not Found.

    Examples
    --------
        return api_not_found()                     # error='not_found'
        return api_not_found('Task not found')     # legacy string
        return api_not_found('Task', task_id=tid)  # adds top-level task_id
    """
    return api_error(what, status=404, **extras)


def api_conflict(error: Any, **extras):
    """Return 409 Conflict (e.g. unique constraint, name already exists)."""
    return api_error(error, status=409, **extras)


def api_payload_too_large(max_bytes: int | None = None, **extras):
    """Return 413 Payload Too Large with a friendly message."""
    if max_bytes is not None:
        msg = 'Payload too large (max %.1f MB)' % (max_bytes / (1024 * 1024))
    else:
        msg = 'Payload too large'
    return api_error(msg, status=413, **extras)


def api_method_not_allowed(error: Any = 'Method not allowed', **extras):
    """Return 405 Method Not Allowed."""
    return api_error(error, status=405, **extras)


def api_service_unavailable(error: Any = 'Service temporarily unavailable', *,
                            retry_after: int = 2, **extras):
    """Return 503 Service Unavailable with a ``Retry-After`` header.

    The transient-overload signal (e.g. DB connection pool saturated during a
    reconnection burst). Distinct from 500: it tells a polling client to back
    off and retry rather than treat the response as a hard failure and retry
    harder — which would amplify the overload into a thundering-herd storm.
    """
    resp, status = api_error(error, status=503, **extras)
    try:
        resp.headers['Retry-After'] = str(max(1, int(retry_after)))
    except Exception as e:
        logger.debug('[api_response] could not set Retry-After: %s', e)
    return resp, status


def api_internal_error(exc: BaseException | str | None = None, *,
                       context: str = '', source: str = '',
                       log_traceback: bool = True, **extras):
    """Return 500 Internal Server Error.

    Auto-logs the exception with traceback so server logs always show
    *what* triggered a 500, even if the route handler forgot to log.
    """
    if log_traceback and isinstance(exc, BaseException):
        logger.error('[api_internal_error] %s: %s',
                     type(exc).__name__, exc, exc_info=True)
    error = exc if exc is not None else 'internal_error'
    return api_error(error, status=500, kind='internal',
                     context=context, source=source, **extras)


# ── Convenience: try-style wrapper for view functions ─────────────────

def safe_route(fn):
    """Decorator: catches uncaught exceptions and returns api_internal_error.

    Usage
    -----
    @bp.route('/api/foo', methods=['POST'])
    @safe_route
    def foo():
        ...

    Use sparingly — most routes should handle expected errors explicitly.
    This is meant for routes where any uncaught exception should be a 500
    rather than crashing the framework's default handler.
    """
    import asyncio
    import functools

    def _handle(e):
        # Special-case BadRequest from request_parser → 400 with field info
        try:
            from lib.request_parser import BadRequest as _BadReq
            if isinstance(e, _BadReq):
                extras = {'field': e.field} if e.field else {}
                return api_bad_request(str(e), **extras)
        except ImportError:
            pass
        return api_internal_error(
            e, context=fn.__module__, source=fn.__qualname__,
        )

    # Dual-mode: keep an async handler a coroutine function so Quart
    # awaits it natively instead of running the wrapper in its thread-pool
    # and trying to serialize the returned coroutine as the response.
    if asyncio.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except Exception as e:
                return _handle(e)
        return wrapper

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            return _handle(e)

    return wrapper


__all__ = [
    'api_ok', 'api_created', 'api_no_content', 'api_payload',
    'api_error', 'api_bad_request', 'api_unauthorized', 'api_forbidden',
    'api_not_found', 'api_conflict', 'api_payload_too_large',
    'api_method_not_allowed', 'api_internal_error', 'api_service_unavailable',
    'safe_route', 'sse_response',
]
