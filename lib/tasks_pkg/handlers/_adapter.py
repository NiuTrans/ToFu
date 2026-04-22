# HOT_PATH
"""Shared adapters for tool handlers.

This module owns two tiny DRY primitives that let most tool-handler bodies
collapse to a single `return simple_call(...)` line:

  • :func:`simple_call`      — wraps the common "log→time→execute→log→meta→
                               finalize" skeleton for sync, single-call tools.
  • :func:`run_batch_concurrent` — generic ordered-concurrent runner used by
                               the web_search / fetch_url batch handlers.

Both helpers log at INFO on entry / exit and ERROR on failure per CLAUDE.md
§2.2, and read no `_lib.*` module-level constants (hot-reload safe).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, TypeVar

from lib.log import get_logger
from lib.tasks_pkg.executor import _build_simple_meta, _finalize_tool_round

logger = get_logger(__name__)

T = TypeVar('T')
R = TypeVar('R')

__all__ = ['simple_call', 'run_batch_concurrent']


def simple_call(
    task: dict[str, Any],
    fn_name: str,
    fn_args: dict[str, Any],
    rn: int,
    round_entry: dict[str, Any],
    tc_id: str,
    *,
    executor: Callable[..., Any],
    source: str,
    icon: str = '',
    badge: str = '',
    title: str = '',
    snippet: str = '',
    extra: dict[str, Any] | None = None,
    module_tag: str = '',
    executor_kwargs: dict[str, Any] | None = None,
    post_build: Callable[[dict[str, Any], Any, dict[str, Any]], None] | None = None,
) -> tuple[str, Any, bool]:
    """Run a single synchronous tool and finalize its round.

    This is the shared body for handlers whose only work is:
    ``log_call → execute(fn_name, fn_args) → log_result → meta → finalize``.

    Parameters
    ----------
    task, fn_name, fn_args, rn, round_entry, tc_id
        Passed through from the registry's handler contract.
    executor
        Callable that performs the actual work. Signature:
        ``executor(fn_name, fn_args, **executor_kwargs) -> str | Any``.
    source
        Source label for the meta (e.g. ``'Scheduler'``).
    icon, badge, title, snippet, extra
        Passed through to :func:`_build_simple_meta`.
    module_tag
        Short label used in log prefix (defaults to *source*).
    executor_kwargs
        Extra kwargs forwarded to ``executor``.
    post_build
        Optional callback ``(meta, tool_content, fn_args) -> None`` invoked
        after meta is built but before finalization — useful for handlers
        that need to tweak the meta based on tool output (e.g. compute a
        custom badge).

    Returns
    -------
    tuple
        ``(tc_id, tool_content, is_search=False)`` — conforming to
        :class:`~lib.protocols.ToolHandler`.
    """
    tid = task.get('id', '?')[:8]
    tag = module_tag or source
    _arg_preview = str(fn_args)[:300]
    logger.info('[Task %s] [%s] %s called with args=%s', tid, tag, fn_name, _arg_preview)

    t0 = time.time()
    tool_content = executor(fn_name, fn_args, **(executor_kwargs or {}))
    elapsed = time.time() - t0

    _content_len = len(tool_content) if isinstance(tool_content, str) else len(str(tool_content))
    logger.info('[Task %s] [%s] %s completed in %.1fs (result_len=%d)',
                tid, tag, fn_name, elapsed, _content_len)

    meta = _build_simple_meta(
        fn_name, tool_content, source=source,
        icon=icon, badge=badge, title=title, snippet=snippet, extra=extra,
    )
    if post_build is not None:
        try:
            post_build(meta, tool_content, fn_args)
        except Exception as e:
            # Non-fatal: the meta is already usable without the tweak.
            logger.warning('[Task %s] [%s] post_build hook failed for %s: %s',
                           tid, tag, fn_name, e, exc_info=True)
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False


def run_batch_concurrent(
    items: list[T],
    worker: Callable[[T], R],
    *,
    max_workers: int,
    tag: str = 'batch',
) -> list[R | None]:
    """Run *worker* over *items* concurrently, preserving input order.

    Exceptions raised by the worker are logged at ``error`` and the
    corresponding slot in the output list is set to ``None`` — callers
    decide how to surface per-item failures.

    Parameters
    ----------
    items
        Input list. If empty, returns an empty list without spinning up a pool.
    worker
        Pure function ``worker(item) -> result``. Called once per item.
    max_workers
        Upper bound on thread-pool size. The actual pool uses
        ``min(len(items), max_workers)``.
    tag
        Short label used in log prefixes (e.g. ``'Search'``, ``'Fetch'``).

    Returns
    -------
    list
        Output aligned with *items* (same length, same order). A worker
        failure puts ``None`` at the corresponding index.
    """
    n = len(items)
    if n == 0:
        return []

    t0 = time.time()
    ordered: list[R | None] = [None] * n
    with ThreadPoolExecutor(max_workers=min(n, max_workers)) as pool:
        futures = {pool.submit(worker, item): i for i, item in enumerate(items)}
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                ordered[idx] = fut.result()
            except Exception as e:
                logger.error('[%s] batch worker failed at idx=%d: %s', tag, idx, e, exc_info=True)
                ordered[idx] = None
    elapsed = time.time() - t0
    logger.debug('[%s] batch of %d completed in %.1fs', tag, n, elapsed)
    return ordered
