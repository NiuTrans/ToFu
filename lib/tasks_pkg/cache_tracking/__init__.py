# HOT_PATH — called every round in the orchestrator.
"""Prompt Cache Break Detection & Cache-Aware Microcompact.

Inspired by Claude Code's ``promptCacheBreakDetection.ts`` (727 lines).

Features:
  1. **Cache break detection**: two-phase approach (like Claude Code):
     - Phase 1 (pre-call): hash system prompt, tools, and message count
       to detect what WOULD cause a cache break.
     - Phase 2 (post-call): check API-reported cache_read_tokens to
       confirm whether a break actually occurred.
     Uses only system/tools/model/message-count changes (NOT message
     content hashes) to avoid false positives from micro-compact mutations.
  2. **Cache-aware microcompact**: when editing messages, skip those in the
     "cache prefix" (messages that were part of the last cache hit) to
     maintain byte-identical content for prompt cache stability.
  3. **Concurrent conversation tracking**: counts active conversations on
     the same model (for diagnostics only — A/B tested 2026-04-10: cache
     contention between different conversations does NOT exist).
  4. **Session-stable TTL latch**: latches the CACHE_EXTENDED_TTL decision
     once per task to prevent mid-session cache key changes from shifting
     the beta header.
  5. **Cache-aware tool result ordering**: sorts tool results by tool_call_id
     to ensure deterministic prefix for automatic prefix caching providers.

Key insight (from investigating "cache_read_tokens stays unchanged"):
  The old code hashed message PREFIX content, which changed every round due
  to micro-compact mutating cold tool results → false positive warnings.
  The new approach separates "things that break server-side cache" (system
  prompt, tools, model) from "expected content changes" (tool result
  compaction, new messages appended).

  For Anthropic: cache breakpoints must advance with the conversation tail
  to cover the growing prefix (fixed in add_cache_breakpoints).

  For OpenAI/Qwen automatic prefix caching: micro-compact must NOT mutate
  messages inside the cached prefix (enforced by get_cache_prefix_count).

────────────────────────────────────────────────────────────────────────────
This module is a FACADE PACKAGE: the implementation lives in the submodules
below and is re-exported here so every ``from lib.tasks_pkg.cache_tracking
import X`` call site keeps working byte-identically. The shared MUTABLE state
(``_cache_states`` dict, ``_cache_lock``, the ``CacheState`` class) lives in
ONE place — ``._state`` — and is imported by reference everywhere, so
``from lib.tasks_pkg.cache_tracking import _cache_states`` returns THE SAME
object the internal functions mutate.

Submodules:
  * ``._state``   — CacheState, _cache_states, _cache_lock, _state_key,
                    cleanup_cache_state, cleanup_stale_cache_states
  * ``._hashing`` — _md5, _hash_system_prompt, _hash_tools, ... , _diff_* (pure)
  * ``._detect``  — EDITABLE_TAIL_COUNT, _classify_break, _resolve_break_cause,
                    detect_cache_break
  * ``._roi``     — _emit_l2_roi, record_l2_compaction, get_session_cache_stats,
                    log_round_cache_stats, notify_compaction, notify_history_rewrite
  * ``._ttl``     — _ttl_latch, latch_extended_ttl, release_ttl_latch,
                    _count_active_on_model
  * ``._prefix``  — sort_tool_results, get_cache_prefix_count, get_cache_diagnostics
"""

from __future__ import annotations

from lib.log import audit_log, get_logger  # noqa: F401

logger = get_logger(__name__)

# ``audit_log`` is re-exported here (not just used internally) because the old
# flat ``cache_tracking.py`` exposed it as a MODULE GLOBAL, and both tests and
# any hot-reload path spy it on the package namespace
# (``lib.tasks_pkg.cache_tracking.audit_log``). ``_roi._emit_l2_roi`` resolves
# it through THIS facade at call time (see there), so patching it here takes
# effect exactly as it did against the flat module.


# ═══════════════════════════════════════════════════════════════════════════════
#  Shared mutable state + lifecycle  (._state — the SINGLETON)
# ═══════════════════════════════════════════════════════════════════════════════

from lib.tasks_pkg.cache_tracking._state import (  # noqa: E402,F401
    CacheState,
    _cache_states,
    _cache_lock,
    _state_key,
    cleanup_cache_state,
    cleanup_stale_cache_states,
    get_prev_turn_cache_read,
    get_warm_cache_read,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Hashing & diffing helpers  (._hashing — pure)
# ═══════════════════════════════════════════════════════════════════════════════

from lib.tasks_pkg.cache_tracking._hashing import (  # noqa: E402,F401
    _md5,
    _hash_system_prompt,
    _hash_tools,
    _hash_tools_per_tool,
    _diff_tool_hashes,
    _hash_prefix_content,
    _hash_prefix_fields,
    _diff_prefix_fields,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Cache break detection  (._detect)
# ═══════════════════════════════════════════════════════════════════════════════

from lib.tasks_pkg.cache_tracking._detect import (  # noqa: E402,F401
    EDITABLE_TAIL_COUNT,
    _MIN_CACHE_MISS_TOKENS,
    _MIN_NO_REUSE_TOKENS,
    _classify_break,
    _resolve_break_cause,
    classify_verdict,
    detect_cache_break,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  L2 ROI, session stats, per-round logging & notify signals  (._roi)
# ═══════════════════════════════════════════════════════════════════════════════

from lib.tasks_pkg.cache_tracking._roi import (  # noqa: E402,F401
    _emit_l2_roi,
    record_l2_compaction,
    get_session_cache_stats,
    log_round_cache_stats,
    notify_compaction,
    notify_history_rewrite,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Session-stable TTL latch + concurrent-model counter  (._ttl)
# ═══════════════════════════════════════════════════════════════════════════════

from lib.tasks_pkg.cache_tracking._ttl import (  # noqa: E402,F401
    _ttl_latch,
    _ttl_latch_lock,
    latch_extended_ttl,
    release_ttl_latch,
    _count_active_on_model,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Cache-aware tool-result ordering, prefix gate & diagnostics  (._prefix)
# ═══════════════════════════════════════════════════════════════════════════════

from lib.tasks_pkg.cache_tracking._prefix import (  # noqa: E402,F401
    sort_tool_results,
    get_cache_prefix_count,
    get_cache_diagnostics,
)


__all__ = [
    # audit re-export (facade-spied by tests / hot-reload)
    'audit_log',
    # state singleton
    'CacheState', '_cache_states', '_cache_lock', '_state_key',
    'cleanup_cache_state',
    'cleanup_stale_cache_states', 'get_prev_turn_cache_read',
    'get_warm_cache_read',
    # hashing
    '_md5', '_hash_system_prompt', '_hash_tools', '_hash_tools_per_tool',
    '_diff_tool_hashes', '_hash_prefix_content', '_hash_prefix_fields',
    '_diff_prefix_fields',
    # detection
    'EDITABLE_TAIL_COUNT', '_MIN_CACHE_MISS_TOKENS', '_MIN_NO_REUSE_TOKENS',
    '_classify_break', '_resolve_break_cause', 'classify_verdict',
    'detect_cache_break',
    # roi / stats / notify
    '_emit_l2_roi', 'record_l2_compaction', 'get_session_cache_stats',
    'log_round_cache_stats', 'notify_compaction', 'notify_history_rewrite',
    # ttl
    '_ttl_latch', '_ttl_latch_lock', 'latch_extended_ttl', 'release_ttl_latch',
    '_count_active_on_model',
    # prefix / diagnostics
    'sort_tool_results', 'get_cache_prefix_count', 'get_cache_diagnostics',
]
