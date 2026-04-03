"""lib/log.py — Centralized logging for ChatUI.

Usage in any module:
    from lib.log import get_logger, log_exception, audit_log, log_context
    logger = get_logger(__name__)

    logger.info('Normal operation')
    logger.warning('Something unexpected')
    logger.error('Failed to do X', exc_info=True)   # includes traceback
    logger.exception('Caught error')                  # shorthand for error+exc_info

    # Convenient shorthand for error + traceback
    log_exception(logger, 'Something went wrong')

    # Structured audit logging for critical events
    audit_log('user_login', user='admin', ip='1.2.3.4')

    # Context manager that logs start/end/duration/exception
    with log_context('heavy_computation'):
        do_work()

    # Decorator for route handlers — auto-logs entry, exit, status, duration
    @log_route(logger)
    def my_endpoint():
        ...

    # Context manager for external calls (APIs, DB, etc.)
    with log_external(logger, 'eastmoney_api', url='https://...'):
        resp = requests.get(...)

    # Get current request ID (for correlating logs across modules)
    rid = req_id()  # e.g. 'a3f7' — short hex, set per HTTP request

Log file layout:
    logs/app.log     — Business logic (lib.*, routes.*, server)  INFO+
                       Daily rotation, 30 days retention.  (configured in server.py)
    logs/access.log  — HTTP request log (werkzeug)  INFO+
                       Daily rotation, 14 days retention. Noisy polls filtered.  (configured in server.py)
    logs/error.log   — All WARNING/ERROR/CRITICAL from every source
                       Size rotation, 5MB × 10 backups.  (configured in server.py)
    logs/vendor.log  — Third-party libraries, WARNING+ only
                       Size rotation, 5MB × 3 backups.  (configured in server.py)
    logs/audit.log   — Structured JSON audit trail  (configured here in lib/log.py via audit_log())
"""

import functools
import json
import logging
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Lock as _Lock
from threading import local as thread_local

# ── Base directory and log paths ──
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE_DIR, 'logs')

# Primary log files
APP_LOG = os.path.join(LOG_DIR, 'app.log')
ACCESS_LOG = os.path.join(LOG_DIR, 'access.log')
ERROR_LOG = os.path.join(LOG_DIR, 'error.log')
VENDOR_LOG = os.path.join(LOG_DIR, 'vendor.log')
AUDIT_LOG_FILE = os.path.join(LOG_DIR, 'audit.log')

# Backward compat — old code that references LOG_FILE still works,
# but now points to app.log (the main business log).
LOG_FILE = APP_LOG


# ══════════════════════════════════════════
#  Request ID — per-request correlation
# ══════════════════════════════════════════

_thread_ctx = thread_local()

def set_req_id(rid: str = None) -> str:
    """Set a request ID for the current thread (called from middleware).

    Args:
        rid: Explicit request ID. If None, generates a short hex UUID.

    Returns:
        The request ID that was set.
    """
    if rid is None:
        rid = uuid.uuid4().hex[:8]
    _thread_ctx.req_id = rid
    return rid


def req_id() -> str:
    """Get the current request ID for this thread.

    Returns empty string if not in a request context (e.g. background threads).
    """
    return getattr(_thread_ctx, 'req_id', '')



def _rid_prefix() -> str:
    """Return '[rid:XXXX] ' prefix if request ID is set, else ''."""
    rid = req_id()
    return f'[rid:{rid}] ' if rid else ''


# ══════════════════════════════════════════
#  Core Logger
# ══════════════════════════════════════════

def get_logger(name: str) -> logging.Logger:
    """Get a named logger for a module.

    Args:
        name: Usually __name__, e.g. 'lib.llm_client' or 'routes.chat'.

    Returns:
        A logging.Logger instance that inherits root config from server.py.
    """
    return logging.getLogger(name)


# ══════════════════════════════════════════
#  Exception Logging
# ══════════════════════════════════════════

def log_exception(logger: logging.Logger, msg: str, *args, **kwargs) -> None:
    """Convenient shorthand: log an error message with full traceback.

    Equivalent to logger.error(msg, exc_info=True) but shorter to type
    and makes exception-logging intent explicit in code reviews.

    Automatically prepends request ID if available.

    Args:
        logger: The logger instance to use.
        msg: Error message (may contain %-style format placeholders).
        *args: Format arguments for the message.
        **kwargs: Additional keyword arguments passed to logger.error().
    """
    kwargs['exc_info'] = True
    prefix = _rid_prefix()
    logger.error('%s' + msg, prefix, *args, **kwargs)


# ══════════════════════════════════════════
#  Audit Logging
# ══════════════════════════════════════════

_audit_lock = _Lock()

def audit_log(event: str, **details) -> None:
    """Write a structured JSON entry to the separate audit log file.

    Use this for critical events that need to be easily grep-able and
    machine-parseable: user actions, security events, config changes, etc.

    Thread-safe: uses ``_audit_lock`` to serialise file writes so
    concurrent requests never interleave partial JSON lines.

    Args:
        event: Event name, e.g. 'user_login', 'model_switch', 'config_change'.
        **details: Arbitrary key-value pairs to include in the audit entry.
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    rid = req_id()
    entry = {
        'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
        'event': event,
        **details,
    }
    if rid:
        entry['request_id'] = rid
    try:
        with _audit_lock:
            with open(AUDIT_LOG_FILE, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + '\n')
    except Exception:
        # Fall back to standard logging if audit file write fails
        logging.getLogger('audit').error(
            'Failed to write audit log: %s', json.dumps(entry, default=str), exc_info=True
        )


# ══════════════════════════════════════════
#  Operation Context Manager
# ══════════════════════════════════════════

@contextmanager
def log_context(operation_name: str, logger: logging.Logger = None, level: int = logging.INFO):
    """Context manager that automatically logs start/end/duration/exception of a block.

    Usage:
        with log_context('rebuild_index'):
            do_expensive_work()

    Logs:
        - INFO on entry:  "[op:rebuild_index] started"
        - INFO on exit:   "[op:rebuild_index] completed in 1.234s"
        - ERROR on error: "[op:rebuild_index] failed after 0.567s: <exception>"

    Args:
        operation_name: Human-readable name of the operation.
        logger: Logger to use. Defaults to the 'lib.log' logger.
        level: Log level for start/completion messages (default INFO).
    """
    if logger is None:
        logger = logging.getLogger('lib.log')

    prefix = _rid_prefix()
    logger.log(level, '%s[op:%s] started', prefix, operation_name)
    start = time.monotonic()
    try:
        yield
    except Exception as exc:
        elapsed = time.monotonic() - start
        logger.error(
            '%s[op:%s] FAILED after %.3fs: %s',
            prefix, operation_name, elapsed, exc,
            exc_info=True)

        raise
    else:
        elapsed = time.monotonic() - start
        logger.log(level, '%s[op:%s] completed in %.3fs', prefix, operation_name, elapsed)


# ══════════════════════════════════════════
#  Route Logging Decorator
# ══════════════════════════════════════════

def log_route(logger: logging.Logger, log_request_body: bool = False,
              log_response_body: bool = False,
              sensitive_fields: tuple = ('password', 'token', 'secret')):
    """Decorator that auto-logs route handler entry, exit, status code, and duration.

    Usage:
        @app.route('/api/foo', methods=['POST'])
        @log_route(logger)
        def foo_handler():
            ...

    Logs:
        → [Route] POST /api/foo — entry
        ← [Route] POST /api/foo — 200 OK in 0.045s

    On error:
        ✗ [Route] POST /api/foo — 500 in 0.123s: ValueError('bad input')

    Args:
        logger: Logger instance for this module.
        log_request_body: If True, log request JSON body at DEBUG level (redacted).
        log_response_body: If True, log response body at DEBUG level (truncated).
        sensitive_fields: Field names to redact from logged request bodies.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                from flask import request as flask_req
                method = flask_req.method
                path = flask_req.path
                rid = req_id()
                rid_tag = f'[rid:{rid}] ' if rid else ''
            except RuntimeError as e:
                # Expected when called outside Flask request context
                logger.debug('log_route outside request context: %s', e, exc_info=True)
                method, path, rid_tag = '?', '?', ''

            logger.debug('%s→ [Route] %s %s', rid_tag, method, path)

            if log_request_body:
                try:
                    body = flask_req.get_json(silent=True)
                    if body and isinstance(body, dict):
                        safe_body = {k: ('***' if k in sensitive_fields else v) for k, v in body.items()}
                        logger.debug('%s  Request body: %s', rid_tag, json.dumps(safe_body, ensure_ascii=False, default=str)[:2000])
                except Exception:
                    logger.debug('Failed to log request body', exc_info=True)

            start = time.monotonic()
            try:
                result = fn(*args, **kwargs)
                elapsed = time.monotonic() - start

                # Extract status code from Flask response
                status = 200
                if hasattr(result, 'status_code'):
                    status = result.status_code
                elif isinstance(result, tuple) and len(result) >= 2:
                    status = result[1]

                if status >= 500:
                    logger.error('%s✗ [Route] %s %s — %d in %.3fs',
                                rid_tag, method, path, status, elapsed)
                elif status >= 400:
                    logger.warning('%s⚠ [Route] %s %s — %d in %.3fs',
                                  rid_tag, method, path, status, elapsed)
                else:
                    logger.debug('%s← [Route] %s %s — %d in %.3fs',
                               rid_tag, method, path, status, elapsed)

                if log_response_body:
                    try:
                        resp_data = result.get_data(as_text=True) if hasattr(result, 'get_data') else str(result)
                        logger.debug('%s  Response body: %.2000s', rid_tag, resp_data)
                    except Exception:
                        logger.debug('Failed to log response body', exc_info=True)

                return result

            except Exception as exc:
                elapsed = time.monotonic() - start
                logger.error('%s✗ [Route] %s %s — EXCEPTION after %.3fs: %s',
                            rid_tag, method, path, elapsed, exc, exc_info=True)
                raise

        return wrapper
    return decorator


# ══════════════════════════════════════════
#  External Call Context Manager
# ══════════════════════════════════════════

@contextmanager
def log_external(logger: logging.Logger, service_name: str, **context):
    """Context manager for logging external API/service calls with timing.

    Usage:
        with log_external(logger, 'eastmoney_api', symbol='000001'):
            resp = requests.get(...)

    Logs:
        [ext:eastmoney_api] calling (symbol=000001)
        [ext:eastmoney_api] OK in 0.234s
        -- or on failure --
        [ext:eastmoney_api] FAILED after 2.001s: ConnectionError(...)

    Args:
        logger: Logger instance.
        service_name: Name of the external service being called.
        **context: Key-value pairs for context (logged at entry).
    """
    prefix = _rid_prefix()
    ctx_str = ', '.join(f'{k}={v}' for k, v in context.items()) if context else ''
    ctx_display = f' ({ctx_str})' if ctx_str else ''

    logger.debug('%s[ext:%s] calling%s', prefix, service_name, ctx_display)
    start = time.monotonic()
    try:
        yield
    except Exception as exc:
        elapsed = time.monotonic() - start
        logger.error('%s[ext:%s] FAILED after %.3fs: %s',
                    prefix, service_name, elapsed, exc, exc_info=True)
        raise
    else:
        elapsed = time.monotonic() - start
        logger.debug('%s[ext:%s] OK in %.3fs', prefix, service_name, elapsed)


# ══════════════════════════════════════════
#  Safe Exception Logging for Guard Clauses
# ══════════════════════════════════════════

def log_suppressed(logger: logging.Logger, context: str, exc: Exception = None,
                   level: int = logging.WARNING):
    """Log a suppressed (swallowed) exception with context — for guard clauses.

    Use this instead of bare `except: pass` or `logger.debug('Exception caught')`.
    Provides enough info to diagnose issues without the noise of full tracebacks.

    Usage:
        try:
            risky_optional_thing()
        except Exception as e:
            log_suppressed(logger, 'optional NAV fetch', e)

    Args:
        logger: Logger instance.
        context: What was being attempted (e.g. 'NAV fetch for 000001').
        exc: The caught exception (optional, uses sys.exc_info if not provided).
        level: Log level (default WARNING). Use DEBUG for truly expected failures.
    """
    prefix = _rid_prefix()
    exc_type = type(exc).__name__ if exc else 'Unknown'
    exc_msg = str(exc)[:200] if exc else ''
    logger.log(level, '%s[suppressed] %s — %s: %s', prefix, context, exc_type, exc_msg)

# ══════════════════════════════════════════
#  Log Analysis Utilities
# ══════════════════════════════════════════
# NOTE: All log scanning, error fingerprinting, and resolution tracking
# is handled by lib/project_error_tracker.py (the universal module).
# No log-analysis utilities live here anymore.
