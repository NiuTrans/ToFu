"""orchestrator/_prefetch.py — background prefetch pool (run_task slice 3).

**Extraction context** (board epic ``pt_03f4cdf1``, slice 3):

The memory + project prefetch block that used to live inline in
``run_task`` (line 342-391 of the pre-slice ``_run.py``). It:

  1. Spawns a small ``ThreadPoolExecutor`` (max_workers=2) so the two
     FUSE-bound context loads can happen in parallel with the main
     tool-assembly path — an "Inspired by Claude Code" pattern.
  2. Conditionally submits ``_prefetch_project`` (only when the task's
     project is enabled + a path is present) and ``_prefetch_memory``
     (only when the memory toggle is on).
  3. Stashes the two futures under ``task['_prefetch_project']`` /
     ``task['_prefetch_memory']`` so ``_inject_system_contexts`` picks
     them up later in the same run.

Caller (``run_task``) still owns the teardown — a single
``pool.shutdown(wait=False)`` in the finally block after the run
completes. The pool is returned here so the caller has that handle.

Kept SEPARATE from ``_vu_startup.py`` (which owns the VU startup phase-
emit + the external-edit daemon-thread) because these two lanes
address different concerns:

  * ``_vu_startup.py`` = tiny helpers that emit ONE event or spawn ONE
    fire-and-forget daemon. No return value the caller needs.
  * ``_prefetch.py`` = a POOL the caller must shut down + futures the
    caller stashes on ``task`` for a downstream consumer. Return value
    matters.

Both preserve the strangler-fig pattern: single-definition module-level
functions + ``_run.py`` calls them at the same source sites where the
inline closures used to live.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor as _PrefetchPool
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)


def start_prefetches(
    task: dict[str, Any],
    *,
    cfg: dict[str, Any],
    project_path: str,
    project_enabled: bool,
    memory_enabled: bool,
) -> _PrefetchPool:
    """Spawn the prefetch pool and submit up-to-two prefetch jobs.

    Preserves the exact behavioural gating of the previous inline block:

      * ``project_enabled=True`` AND non-empty ``project_path`` →
        submits ``_prefetch_project`` (calls
        ``lib.project_mod.get_context_for_prompt`` with the task's
        convId scoping). Future stashed on ``task['_prefetch_project']``.
      * ``memory_enabled=True`` → submits ``_prefetch_memory`` (calls
        ``lib.memory.build_memory_context`` with the primary project
        path + any ``projectPaths[1:]`` extras). Future stashed on
        ``task['_prefetch_memory']``.
      * When a gate is off, the corresponding ``task[...]`` slot is set
        to ``None`` so the downstream consumer's
        ``if task.get('_prefetch_project'):`` check is honest.

    The pool ITSELF is created regardless (max_workers=2,
    thread_name_prefix='mem-prefetch'), and returned to the caller —
    ``run_task``'s finally block calls ``.shutdown(wait=False)`` on it
    unconditionally. The empty-pool case is a rare degenerate path
    (both flags off), matching the pre-slice behaviour where the same
    pool was created eagerly and just had no work submitted.

    Args:
        task: the live task dict — mutated with two future slots.
        cfg: the resolved task config (read for ``projectPaths`` for
            memory extra-paths).
        project_path: the primary project root (may be ``''`` if
            disabled — an empty string means "no project scope").
        project_enabled: gate for the project prefetch.
        memory_enabled: gate for the memory prefetch.

    Returns:
        The ``ThreadPoolExecutor`` the caller owns. Caller MUST call
        ``.shutdown(wait=False)`` on it after ``_inject_system_contexts``
        has consumed the futures.
    """
    _prefetch_executor = _PrefetchPool(
        max_workers=2, thread_name_prefix='mem-prefetch')
    _prefetch_project_future = None
    _prefetch_memory_future = None

    if project_enabled and project_path:
        _prefetch_conv_id = task.get('convId') or task.get('id') or ''

        def _prefetch_project():
            from lib.project_mod import get_context_for_prompt
            return get_context_for_prompt(
                project_path, conv_id=_prefetch_conv_id or None)

        _prefetch_project_future = _prefetch_executor.submit(_prefetch_project)

    # Extra workspace roots for memory scoping (multi-root session).
    # Memories are READ (listed / searched / prefetched) across the
    # primary + every extra root, unioned and de-duplicated; NEW
    # memories are still written only to the primary project_path.
    # Mirrors the projectPaths[1:] extraction used for file tools.
    _pp = project_path if project_enabled else None
    _mem_extra_paths: list[str] = []
    if project_enabled and _pp:
        _all_mem_paths = cfg.get('projectPaths') or []
        _mem_extra_paths = (
            [p for p in _all_mem_paths[1:]
             if p and p != _pp]
            if len(_all_mem_paths) > 1 else [])

    # Memory toggle gates EVERYTHING memory-related: the count-hint
    # background load, the per-turn prefetch (BM25 + cheap-LLM rerank),
    # and the accumulation instructions injected into the system prompt.
    # AI still accumulates memories in the background via the
    # search_memories / create_memory tools — only the proactive
    # injection path is muted.
    if memory_enabled:
        def _prefetch_memory():
            from lib.memory import build_memory_context
            return build_memory_context(
                project_path=_pp, extra_paths=_mem_extra_paths)

        _prefetch_memory_future = _prefetch_executor.submit(_prefetch_memory)

    # Store prefetch futures on the task for _inject_system_contexts to use
    task['_prefetch_project'] = _prefetch_project_future
    task['_prefetch_memory'] = _prefetch_memory_future

    return _prefetch_executor


__all__ = ['start_prefetches']
