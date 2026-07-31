"""routes/chat_side_effects.py — chat helpers with SIDE-EFFECT IO (chat.py slice 3).

**Extraction context** (board epic ``pt_04686ac6054a451e``, slice 3 of N):
Helpers that ``routes/chat.py`` calls into but which perform SIDE-EFFECT IO
across other ``lib.*`` packages (``lib.message_queue``,
``lib.tasks_pkg.server_message_store``, …).

Kept intentionally SEPARATE from:

  * ``routes/chat_helpers.py`` — that module documents its Pure invariant
    (no module-level state, stdlib + orjson + lib.log only, "zero
    circular-import risk"). Adding IO helpers that ``from lib.message_queue
    import clear_queue`` at call time would break that invariant.
  * ``routes/chat_state.py`` — that module owns process-local mutable state
    (``_send_abort_marker``). This module owns NO state; it just performs
    IO into modules that DO own state.

Together, the three sibling modules form a clean layering:

  ┌────────────────────────────┬──────────────────────────────────────┐
  │ routes/chat_helpers.py     │ Pure functions, no state, no IO      │
  │ routes/chat_state.py       │ Process-local state + its accessors  │
  │ routes/chat_side_effects.py│ IO into other lib.* packages         │
  └────────────────────────────┴──────────────────────────────────────┘

Every name in this module is re-exported from ``routes/chat.py`` so
external ``from routes.chat import _truncate_conv_history`` (there are
tests that do this — ``tests/test_regen_clears_msg_store.py``) keeps
working unchanged. Wire-parity guarded by
``tests/test_routes_chat_wire_parity.py``.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


def _truncate_conv_history(conv_id: str) -> None:
    """Discharge every server-side obligation that follows truncating a conv.

    Any route that rewrites a conversation's history to a shorter prefix
    (regenerate, edit-and-resend) MUST clear the two in-memory side-channels
    that outlive the DB write, or the next task replays stale state:

      * the message QUEUE — a previous /api/chat/send aborted mid-translate
        may have left an enqueued message; without clearing it the queue
        auto-dispatches a phantom turn after this run completes;
      * the server-side tool-history STORE
        (lib/tasks_pkg/server_message_store) — a full-fidelity in-memory copy
        of the prior turns' tool_use/tool_result rounds, keyed by conv_id and
        driven by keepToolHistory (default ON). On the next task the
        orchestrator's rebuild_messages_with_history REPLACES the DB-built
        (now-truncated) messages with that stored copy, which still holds the
        rounds we just truncated away — so every regen/edit would replay an
        ever-growing stale context instead of the truncated one. Clearing it
        forces a clean rebuild from the truncated DB state; the preserved
        turns' tool history is reconstructed from their stored toolRounds by
        conv_message_builder, so no real context is lost.

    Folding both into one helper makes the invariant impossible to
    half-apply: a future truncating route calls this once instead of
    re-deriving (and forgetting one of) the two clears. Best-effort — each
    failure is logged, never raised.

    Args:
        conv_id: The conversation whose history was just truncated.
    """
    try:
        from lib.message_queue import clear_queue
        _cleared = clear_queue(conv_id)
        if _cleared:
            logger.info('[Regen] conv=%s cleared %d stale queued message(s) before regen',
                        conv_id[:8], _cleared)
    except Exception as e:
        logger.warning('[Regen] Failed to clear queue for conv=%s: %s', conv_id[:8], e)

    try:
        from lib.tasks_pkg.server_message_store import clear as _clear_msg_store
        _clear_msg_store(conv_id)
    except Exception as e:
        logger.warning('[Regen] Failed to clear message store for conv=%s: %s', conv_id[:8], e)


__all__ = ['_truncate_conv_history']
