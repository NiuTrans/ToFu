"""orchestrator/_prefetch.py — background provider lease (run_task slice 3).

**Extraction context** (board epic ```, slice 3):

The project-context prefetch block that used to live inline in
``run_task`` (line 342-391 of the pre-slice ``_run.py``). It:

  1. Acquires a request-local lease on the bounded process-wide context
     provider executor so the FUSE-bound project-rules load can overlap the
     main tool-assembly path without creating a task-owned thread.
  2. Conditionally submits ``_prefetch_project`` when project mode has a path.
  3. Stashes the future under ``task['_prefetch_project']`` for Composer.

Caller (``run_task``) still owns cancellation through ``stop_prefetches``. The
lease is returned here so the caller has that handle, including on abort and
exception paths that never reach context composition.

Kept SEPARATE from ``_vu_startup.py`` (which owns the VU startup phase-
emit + the external-edit daemon-thread) because these two lanes
address different concerns:

  * ``_vu_startup.py`` = tiny helpers that emit ONE event or spawn ONE
    fire-and-forget daemon. No return value the caller needs.
  * ``_prefetch.py`` = a shared-executor LEASE the caller must cancel +
    futures the caller stashes on ``task`` for a downstream consumer. Return
    value matters.

Both preserve the strangler-fig pattern: single-definition module-level
functions + ``_run.py`` calls them at the same source sites where the
inline closures used to live.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger
from lib.tasks_pkg.context_composer._provider_executor import (
    ContextProviderLease,
    create_context_provider_lease as _PrefetchPool,
)

logger = get_logger(__name__)


def start_prefetches(
    task: dict[str, Any],
    *,
    cfg: dict[str, Any],
    project_path: str,
    project_enabled: bool,
    memory_enabled: bool,
) -> ContextProviderLease:
    """Lease shared provider capacity and submit project rules when enabled.

    Preserves the exact behavioural gating of the previous inline block:

      * ``project_enabled=True`` AND non-empty ``project_path`` →
        submits ``_prefetch_project`` (calls
        ``lib.project_mod.get_context_for_prompt`` with the task's
        convId scoping). Future stashed on ``task['_prefetch_project']``.
      * Memory selection is no longer submitted here. The local, metadata-only
        selector runs after tool assembly and stashes evidence for Composer.
      * When a gate is off, the corresponding ``task[...]`` slot is set
        to ``None`` so the downstream consumer's
        ``if task.get('_prefetch_project'):`` check is honest.

    The lease itself is created regardless and returned to the caller;
    ``run_task``'s finally block cancels any unstarted future through its
    executor-compatible ``shutdown`` method. The empty-lease case starts no
    thread and allocates no separate work queue.

    Args:
        task: the live task dict — mutated with the project future slot.
        cfg: retained in the call contract; local memory selection no longer
            needs it here.
        project_path: the primary project root (may be ``''`` if
            disabled — an empty string means "no project scope").
        project_enabled: gate for the project prefetch.
        memory_enabled: retained in the call contract; memory selection is a
            later synchronous metadata-only pass.

    Returns:
        The request-local lease the caller owns. Caller MUST call
        ``.shutdown(wait=False, cancel_futures=True)`` after
        ``compose_task_context`` has consumed the futures.
    """
    del cfg, memory_enabled
    _prefetch_executor = _PrefetchPool()
    _prefetch_project_future = None

    if project_enabled and project_path:
        _prefetch_conv_id = task.get('convId') or task.get('id') or ''

        def _prefetch_project():
            from lib.project_mod import get_context_for_prompt
            return get_context_for_prompt(
                project_path, conv_id=_prefetch_conv_id or None)

        _prefetch_project_future = _prefetch_executor.submit(_prefetch_project)

    # Store prefetch futures on the task for the Context Composer to use
    task['_prefetch_project'] = _prefetch_project_future

    return _prefetch_executor


def stop_prefetches(
    task: dict[str, Any],
    prefetch_executor,
    *,
    cancel_pending: bool = False,
) -> None:
    """Release task-owned prefetch state on every orchestration exit path.

    Python cannot stop a callable that has already entered, but
    ``cancel_futures`` prevents an unconsumed queued read from starting. The
    compatibility fallback keeps lightweight test doubles usable without
    weakening normal cleanup.
    """
    task.pop('_prefetch_project', None)
    if prefetch_executor is None:
        return
    try:
        if cancel_pending:
            try:
                prefetch_executor.shutdown(wait=False, cancel_futures=True)
                return
            except TypeError:
                # Executor-compatible test doubles may expose the historical
                # ``shutdown(wait=...)`` signature only.
                pass
        prefetch_executor.shutdown(wait=False)
    except Exception as error:
        logger.debug('[orchestrator] prefetch executor shutdown failed: %s',
                     error)


__all__ = ['start_prefetches', 'stop_prefetches']
