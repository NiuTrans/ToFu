"""lib/rate_limiter.py — Rate-limiting middleware for sensitive endpoints.

The decorator is a thin wrapper around ``lib.rate_limit_store.get_store()``;
the actual counter lives in either ``MemoryRateLimitStore`` (default) or
``DatabaseRateLimitStore`` (set ``TOFU_RATE_LIMIT_BACKEND=db``).

Multi-worker safety: the DB backend is required when running under
gunicorn / uWSGI with N>1 workers.  See PR3c notes and
``docs/RATE_LIMITING_DOS_AUDIT_REPORT.md``.
"""

import asyncio
from functools import wraps

from flask import request

from lib.log import audit_log, get_logger
from lib.rate_limit_store import get_store

logger = get_logger(__name__)


def rate_limit(limit=10, per=60):
    """Decorator to rate-limit a Flask endpoint.

    Dual-mode: emits an ``async def`` wrapper for coroutine handlers and a sync
    wrapper otherwise. A sync passthrough around an ``async def`` view makes
    ``asyncio.iscoroutinefunction(wrapper)`` False, so Quart runs it in the
    thread pool and serializes the returned coroutine OBJECT as the response
    (broken / never-awaited). See CLAUDE.md async-migration-dual-mode-decorators.

    Args:
        limit (int): Max number of requests allowed.
        per (int): Time window in seconds.
    """
    def decorator(f):
        def _check():
            """Run the rate-limit check. Returns a 429 response tuple if the
            caller should be rejected, else None (proceed)."""
            ip = request.remote_addr or 'unknown'
            endpoint = request.path
            store = get_store()
            allowed, count = store.record_and_check(endpoint, ip, limit, per)
            if not allowed:
                logger.warning('[RateLimit] %s from %s — %d/%d in %ds window',
                               endpoint, ip, count, limit, per)
                try:
                    audit_log('rate_limit_violation',
                              ip=ip, route=endpoint,
                              limit=limit, per=per, count=count)
                except Exception as _aerr:
                    logger.debug('[RateLimit] audit_log failed: %s', _aerr)
                return {"error": "Too many requests"}, 429
            return None

        if asyncio.iscoroutinefunction(f):
            @wraps(f)
            async def async_wrapper(*args, **kwargs):
                rejection = _check()
                if rejection is not None:
                    return rejection
                return await f(*args, **kwargs)
            return async_wrapper

        @wraps(f)
        def wrapper(*args, **kwargs):
            rejection = _check()
            if rejection is not None:
                return rejection
            return f(*args, **kwargs)
        return wrapper
    return decorator
