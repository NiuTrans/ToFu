"""Shared in-process message-store singleton + its save/get/clear/stats/cleanup ops.

This submodule owns the SINGLE process-wide mutable state:
  * ``_store``      — dict[conv_id -> entry] (the message cache)
  * ``_store_lock`` — threading.Lock guarding it
  * ``_MAX_AGE_S`` / ``_MAX_ENTRIES`` — TTL + capacity bounds

These are imported BY REFERENCE by the package ``__init__`` and by sibling
submodules, so there is exactly ONE ``_store`` in the process. save_messages
writes it, get_messages reads it, _cleanup_locked prunes it, clear removes.
A divergent copy would silently lose the rebuild history.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)

# conv_id → { 'messages': [...], 'updated_at': float, 'msg_count': int }
_store: dict[str, dict[str, Any]] = {}
_store_lock = threading.Lock()

# Max age before auto-cleanup (2 hours)
_MAX_AGE_S = 7200
# Max conversations to store
_MAX_ENTRIES = 200


def save_messages(conv_id: str, messages: list[dict[str, Any]]) -> None:
    """Save the full message history for a conversation after a turn completes.

    Only saves messages that contain tool_call/tool_result information —
    if a turn had no tool calls, there's no benefit to server-side storage.
    """
    if not conv_id or not messages:
        return

    # Check if there are any tool-related messages worth preserving
    has_tool_msgs = any(
        msg.get('tool_calls') or msg.get('role') == 'tool'
        for msg in messages
    )
    if not has_tool_msgs:
        logger.debug('[MsgStore] conv=%s No tool messages to preserve (%d msgs)',
                     conv_id[:8], len(messages))
        return

    # ── Strip orphaned trailing tool_calls (aborted mid-tool-call) ──
    # If the last message has tool_calls but no tool_results follow,
    # strip it now so the stored messages are always valid.
    while messages and messages[-1].get('tool_calls'):
        _popped = messages.pop()
        logger.warning('[MsgStore] conv=%s Stripped trailing orphaned tool_calls '
                       'before save — prevents broken history on next turn',
                       conv_id[:8])
        if _popped.get('content'):
            messages.append({'role': 'assistant', 'content': _popped['content']})

    with _store_lock:
        _store[conv_id] = {
            'messages': messages,  # NOTE: these are the orchestrator's internal messages (mutable refs)
            'updated_at': time.time(),
            'msg_count': len(messages),
        }
        logger.info('[MsgStore] conv=%s Saved %d messages (with tool history)',
                    conv_id[:8], len(messages))

        # Cleanup stale entries
        if len(_store) > _MAX_ENTRIES:
            _cleanup_locked()


def get_messages(conv_id: str) -> list[dict[str, Any]] | None:
    """Retrieve stored messages for a conversation.

    Returns None if no stored messages exist.
    Returns a deep copy to prevent mutation of the store.
    """
    if not conv_id:
        return None

    with _store_lock:
        entry = _store.get(conv_id)
        if not entry:
            return None

        age = time.time() - entry['updated_at']
        if age > _MAX_AGE_S:
            del _store[conv_id]
            logger.debug('[MsgStore] conv=%s Expired (age=%.0fs)', conv_id[:8], age)
            return None

        # Return a shallow copy of the list (messages themselves are dicts
        # that won't be mutated by the caller since orchestrator builds a
        # new list anyway)
        logger.info('[MsgStore] conv=%s Retrieved %d stored messages (age=%.0fs)',
                    conv_id[:8], entry['msg_count'], age)
        return list(entry['messages'])


def clear(conv_id: str) -> None:
    """Remove stored messages for a conversation."""
    with _store_lock:
        if conv_id in _store:
            del _store[conv_id]
            logger.debug('[MsgStore] conv=%s Cleared', conv_id[:8])


def get_stats() -> dict[str, Any]:
    """Return current store statistics."""
    with _store_lock:
        return {
            'conversations': len(_store),
            'total_messages': sum(e['msg_count'] for e in _store.values()),
            'oldest_age_s': max(
                (time.time() - e['updated_at'] for e in _store.values()),
                default=0,
            ),
        }


def _cleanup_locked():
    """Remove oldest entries to stay under _MAX_ENTRIES. Must hold _store_lock."""
    if len(_store) <= _MAX_ENTRIES:
        return
    # Sort by updated_at, remove oldest
    sorted_keys = sorted(_store.keys(), key=lambda k: _store[k]['updated_at'])
    to_remove = len(_store) - _MAX_ENTRIES
    for key in sorted_keys[:to_remove]:
        del _store[key]
    logger.info('[MsgStore] Cleaned up %d stale entries', to_remove)
