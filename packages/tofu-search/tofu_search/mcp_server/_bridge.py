"""Thread bridge between the asyncio MCP server and this synchronous library.

``tofu_search`` is synchronous top to bottom: ``perform_web_search`` blocks for
as long as the pipeline runs (up to ``search_deadline_secs``, 45s by default)
and drives its own ``ThreadPoolExecutor`` fan-out internally. The MCP SDK is
asyncio. Calling a pipeline function directly from a tool coroutine would pin
the event loop for the whole call, serialising every other request on the
server -- so every tool body goes through :func:`run_blocking` instead.

Two ceilings matter, and only the second is ours to set:

* anyio's default worker-thread limiter (40) bounds how many blocking calls can
  sit in threads at once.
* Each in-flight ``perform_web_search`` starts an engine pool plus a 16-worker
  fetch pool, so N concurrent searches means roughly N x 22 threads and a
  matching number of open sockets. Left unbounded, a handful of concurrent
  requests is enough to exhaust file descriptors and to hammer every search
  engine hard enough to earn a rate-limit -- the failure mode that
  ``min_request_interval_ms`` exists to prevent.

So we hold our own :class:`~anyio.CapacityLimiter`, defaulting to 4 concurrent
pipeline calls, and pass it explicitly rather than relying on anyio's global
default. Requests beyond the limit wait for a slot instead of piling threads on
top of each other.
"""

from __future__ import annotations

import functools
import os
from typing import Any, Callable, TypeVar

import anyio
import anyio.to_thread

from tofu_search.log import get_logger

logger = get_logger(__name__)

T = TypeVar('T')

#: Concurrent blocking calls allowed at once. Deliberately small: each one
#: fans out into ~22 threads inside the library.
DEFAULT_MAX_CONCURRENCY = 4

_ENV_MAX_CONCURRENCY = 'TOFU_SEARCH_MCP_MAX_CONCURRENCY'

_limiter: anyio.CapacityLimiter | None = None


def _configured_concurrency() -> int:
    raw = os.environ.get(_ENV_MAX_CONCURRENCY, '').strip()
    if not raw:
        return DEFAULT_MAX_CONCURRENCY
    try:
        value = int(raw)
    except ValueError:
        logger.warning('[MCP] %s=%r is not an integer, using %d',
                       _ENV_MAX_CONCURRENCY, raw, DEFAULT_MAX_CONCURRENCY)
        return DEFAULT_MAX_CONCURRENCY
    if value < 1:
        logger.warning('[MCP] %s=%d is below 1, using 1', _ENV_MAX_CONCURRENCY, value)
        return 1
    return value


def get_limiter() -> anyio.CapacityLimiter:
    """The process-wide limiter for blocking pipeline calls.

    Created lazily because :class:`~anyio.CapacityLimiter` binds to the running
    async event loop, which does not exist at import time.
    """
    global _limiter
    if _limiter is None:
        total = _configured_concurrency()
        _limiter = anyio.CapacityLimiter(total)
        logger.info('[MCP] blocking-call concurrency limited to %d '
                    '(set %s to change)', total, _ENV_MAX_CONCURRENCY)
    return _limiter


async def run_blocking(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a synchronous library call in a worker thread, under our limiter.

    ``abandon_on_cancel`` is deliberately left at its default (False): the
    library's pipeline functions have no cancellation seam, so a cancelled
    request must still wait for the thread to finish rather than leave it
    running detached with a live fetch pool behind it.
    """
    return await anyio.to_thread.run_sync(
        functools.partial(fn, *args, **kwargs), limiter=get_limiter())
