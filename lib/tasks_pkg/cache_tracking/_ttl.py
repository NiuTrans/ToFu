"""Session-stable prompt-cache TTL latch.

Owns the ``_ttl_latch`` singleton (per-task_id) and its lock. ``get_cache_diagnostics``
(in ``._prefix``) reports ``len(_ttl_latch)`` by importing it from here.
"""

from __future__ import annotations

import threading


_ttl_latch: dict[str, bool] = {}
"""Per-task_id TTL latch. Once set, the TTL decision is fixed for the task."""

_ttl_latch_lock = threading.Lock()


def latch_extended_ttl(task_id: str) -> bool:
    """Latch the CACHE_EXTENDED_TTL decision for a task's lifetime.

    Session-stable TTL decision: once a task
    starts with extended TTL on/off, it stays that way for the entire
    session.  This prevents mid-session settings changes from shifting
    the beta header, which would change the cache key and evict everything.

    Args:
        task_id: The task ID to latch for.

    Returns:
        The latched TTL decision (True = use 1h for stable prefix).
    """
    with _ttl_latch_lock:
        if task_id in _ttl_latch:
            return _ttl_latch[task_id]

        import lib as _lib
        decision = getattr(_lib, 'CACHE_EXTENDED_TTL', False)
        _ttl_latch[task_id] = decision
        return decision


def release_ttl_latch(task_id: str) -> None:
    """Release the TTL latch when a task completes.

    Call from orchestrator._finalize_and_emit_done to prevent memory leak.
    """
    with _ttl_latch_lock:
        _ttl_latch.pop(task_id, None)
