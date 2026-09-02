"""Session-stable TTL latch + concurrent-conversation counter.

Owns the ``_ttl_latch`` singleton (per-task_id) and its lock. ``get_cache_diagnostics``
(in ``._prefix``) reports ``len(_ttl_latch)`` by importing it from here.
"""

from __future__ import annotations

import threading
import time

from lib.log import get_logger
from lib.tasks_pkg.cache_tracking._state import _cache_states

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  Concurrent conversation tracking
# ═══════════════════════════════════════════════════════════════════════════════

def _count_active_on_model(model: str, exclude_conv: str = '') -> int:
    """Count conversations active on the same model within the last 60s.

    NOTE (2026-04-10): A/B tested — cache contention between different
    conversations does NOT exist on Anthropic. Per-round cache_read is
    identical between solo and interleaved modes. The cache is keyed on
    exact prefix bytes, so different conversations have different keys
    and cannot evict each other.

    This function is retained for diagnostics/logging only (e.g., to
    report how many conversations are active on the same model), but
    should NOT be used to explain cache misses.

    Args:
        model: Model name to check.
        exclude_conv: Conv ID to exclude (the current conversation).

    Returns:
        Number of other active conversations on the same model.
    """
    cutoff = time.time() - 60  # consider "active" if called within last 60s
    count = 0
    for key, state in _cache_states.items():
        cid = key[1]
        if cid == exclude_conv:
            continue
        if (state.model == model
                and state.last_update_time > cutoff
                and state.call_count > 0):
            count += 1
    return count


# ═══════════════════════════════════════════════════════════════════════════════
#  Session-stable TTL latch
# ═══════════════════════════════════════════════════════════════════════════════

_ttl_latch: dict[str, bool] = {}
"""Per-task_id TTL latch. Once set, the TTL decision is fixed for the task."""

_ttl_latch_lock = threading.Lock()


def latch_extended_ttl(task_id: str) -> bool:
    """Latch the CACHE_EXTENDED_TTL decision for a task's lifetime.

    Inspired by Claude Code's session-stable TTL decision: once a task
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
