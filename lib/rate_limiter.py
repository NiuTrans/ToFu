"""lib/rate_limiter.py — Rate-limiting middleware for sensitive endpoints.

Uses a simple in-memory store for tracking request counts.
No external dependencies needed.
"""

import logging
import threading
import time
from collections import defaultdict

from flask import request

logger = logging.getLogger(__name__)

# { endpoint -> { ip -> [timestamp, ...] } }
request_counts = defaultdict(lambda: defaultdict(list))
_counts_lock = threading.Lock()
_last_cleanup = 0.0
_CLEANUP_INTERVAL = 300  # purge stale entries every 5 minutes

def rate_limit(limit=10, per=60):
    """Decorator to rate-limit a Flask endpoint.

    Args:
        limit (int): Max number of requests allowed.
        per (int): Time window in seconds.
    """
    def decorator(f):
        def wrapper(*args, **kwargs):
            ip = request.remote_addr
            endpoint = request.path
            now = time.time()

            with _counts_lock:
                # Periodic full cleanup of stale entries
                global _last_cleanup
                if now - _last_cleanup > _CLEANUP_INTERVAL:
                    _last_cleanup = now
                    for ep in list(request_counts.keys()):
                        for addr in list(request_counts[ep].keys()):
                            request_counts[ep][addr] = [
                                ts for ts in request_counts[ep][addr] if now - ts < per
                            ]
                            if not request_counts[ep][addr]:
                                del request_counts[ep][addr]
                        if not request_counts[ep]:
                            del request_counts[ep]

                # Clean up old timestamps for current endpoint/ip
                request_counts[endpoint][ip] = [
                    ts for ts in request_counts[endpoint][ip] if now - ts < per
                ]

                # Check if limit is exceeded
                if len(request_counts[endpoint][ip]) >= limit:
                    logger.warning('[RateLimit] %s from %s — %d/%d in %ds window',
                                   endpoint, ip, len(request_counts[endpoint][ip]), limit, per)
                    return {"error": "Too many requests"}, 429

                # Record current request
                request_counts[endpoint][ip].append(now)

            return f(*args, **kwargs)
        # Preserve original function name for Flask's url_for
        wrapper.__name__ = f.__name__
        return wrapper
    return decorator
