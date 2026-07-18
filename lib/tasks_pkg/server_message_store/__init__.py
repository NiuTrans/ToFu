"""Server-side conversation message store — preserves full tool_use/tool_result history.

Problem: The frontend's buildApiMessages() strips tool call details from history,
sending only `{role: "assistant", content: "final text"}` for past turns.
This means the LLM loses all context about what tools were called and what
they returned in previous turns.

Solution: This module maintains a server-side copy of the full message history
(including tool_use blocks and tool_result messages) across turns. When a new
turn starts, the orchestrator can use these preserved messages instead of the
frontend's stripped-down version.

This is an opt-in feature controlled by `config.keepToolHistory`.

Design:
  - In-memory dict: conv_id → list of messages (full fidelity)
  - Updated at the END of each run_task() with the complete message list
  - On next turn: if store has messages for this conv, replace the frontend's
    messages with the stored version + the new user message
  - TTL-based cleanup to prevent memory leaks

────────────────────────────────────────────────────────────────────────────
This module is a FACADE PACKAGE: the implementation lives in the submodules
below and is re-exported here so every ``from lib.tasks_pkg.server_message_store
import X`` call site keeps working byte-identically. The shared MUTABLE state
(``_store`` dict, ``_store_lock``, the ``_MAX_AGE_S`` / ``_MAX_ENTRIES`` bounds)
lives in ONE place — ``._store`` — and is imported by reference everywhere, so
``from lib.tasks_pkg.server_message_store import _store`` returns THE SAME
object save_messages writes / get_messages reads / _cleanup_locked prunes.

Submodules:
  * ``._store``    — _store, _store_lock, _MAX_AGE_S, _MAX_ENTRIES,
                     save_messages, get_messages, clear, get_stats,
                     _cleanup_locked  (the SINGLETON state + its ops)
  * ``._truncate`` — _OLD_RESULT_MAX_CHARS, _TRUNCATION_MARKER,
                     _truncate_old_tool_results
  * ``._rebuild``  — rebuild_messages_with_history, estimate_token_overhead
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  Shared mutable state + its ops  (._store — the SINGLETON)
# ═══════════════════════════════════════════════════════════════════════════════

from lib.tasks_pkg.server_message_store._store import (  # noqa: E402,F401
    _store,
    _store_lock,
    _MAX_AGE_S,
    _MAX_ENTRIES,
    save_messages,
    get_messages,
    clear,
    get_stats,
    _cleanup_locked,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Old-turn tool-result truncation  (._truncate)
# ═══════════════════════════════════════════════════════════════════════════════

from lib.tasks_pkg.server_message_store._truncate import (  # noqa: E402,F401
    _OLD_RESULT_MAX_CHARS,
    _TRUNCATION_MARKER,
    _truncate_old_tool_results,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Message rebuild-with-history + token-overhead estimate  (._rebuild)
# ═══════════════════════════════════════════════════════════════════════════════

from lib.tasks_pkg.server_message_store._rebuild import (  # noqa: E402,F401
    rebuild_messages_with_history,
    estimate_token_overhead,
)


__all__ = [
    # store singleton + ops
    '_store', '_store_lock', '_MAX_AGE_S', '_MAX_ENTRIES',
    'save_messages', 'get_messages', 'clear', 'get_stats', '_cleanup_locked',
    # truncation
    '_OLD_RESULT_MAX_CHARS', '_TRUNCATION_MARKER', '_truncate_old_tool_results',
    # rebuild / estimate
    'rebuild_messages_with_history', 'estimate_token_overhead',
]
