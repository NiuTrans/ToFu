"""Synchronous local memory-prefetch gate.

The former background cheap-LLM reranker added latency, billing, late-write
races, and a second context owner. Local high-confidence selection is fast
enough to finish inline and only stashes evidence for Context Composer.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger
from lib.tasks_pkg.manager import append_event

logger = get_logger(__name__)


def maybe_run_memory_prefetch(*, task: dict[str, Any], cfg: dict[str, Any],
                              messages: list[dict[str, Any]],
                              tool_list: list[dict[str, Any]] | None,
                              project_path: str | None,
                              project_enabled: bool,
                              memory_enabled: bool,
                              has_real_tools: bool,
                              injected_tool_calls: int) -> None:
    del tool_list  # the gate already records whether real tools are available
    # My Context is independent from relevance-retrieved experience memory.
    # Interactive turns learn explicit durable facts even when memory search is
    # off; headless surfaces have already stamped preferencesEnabled=False.
    from lib.agent_core.personal_scope import resolve_preferences_enabled
    task['_profileConsolidateEligible'] = resolve_preferences_enabled(
        cfg, memory_enabled=memory_enabled)
    if not (memory_enabled and has_real_tools and not injected_tool_calls):
        task['_prefetchedMemories'] = []
        return
    try:
        from lib.memory.prefetch import run_memory_prefetch
        paths = cfg.get('projectPaths') or []
        primary = project_path if project_enabled else None
        extras = [p for p in paths if p and p != primary] if primary else []
        run_memory_prefetch(
            messages, project_path=primary, task=task,
            emit_event=lambda event: append_event(task, event),
            extra_paths=extras,
        )
    except Exception as exc:
        task['_prefetchedMemories'] = []
        logger.warning('[Task %s] local memory prefetch failed: %s',
                       str(task.get('id', '?'))[:8], exc, exc_info=True)


__all__ = ['maybe_run_memory_prefetch']
