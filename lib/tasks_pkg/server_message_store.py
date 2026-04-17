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


# ── Truncation constants for old-turn tool results ──
_OLD_RESULT_MAX_CHARS = 2000
"""Max chars to keep from tool results in old turns (not the latest completed turn).
Keeps tool names + args fully visible, but truncates the bulky result content.
Set to 0 to strip all old results. Set to None to disable truncation."""


def _truncate_old_tool_results(
    messages: list[dict[str, Any]],
    max_chars: int | None = _OLD_RESULT_MAX_CHARS,
) -> int:
    """Truncate tool result content in older turns, keeping recent turn intact.

    Strategy: find the last user message (which starts the current turn).
    The turn before that is the "most recent completed turn" — keep its tool
    results intact. Everything older gets truncated.

    Returns the number of tool results truncated.
    """
    if max_chars is None:
        return 0

    # Find user message indices to identify turn boundaries
    user_indices = [i for i, m in enumerate(messages) if m.get('role') == 'user']
    if len(user_indices) < 2:
        return 0  # Only one turn, nothing to truncate

    # The last user msg is the current (new) question.
    # The second-to-last user msg starts the most recent completed turn.
    # Everything BEFORE that turn boundary is "old".
    old_boundary = user_indices[-2]

    truncated = 0
    for i, msg in enumerate(messages):
        if i >= old_boundary:
            break  # Don't touch the latest completed turn or current turn
        if msg.get('role') != 'tool':
            continue
        content = msg.get('content', '')
        if not isinstance(content, str):
            continue
        if len(content) <= max_chars:
            continue
        # Already compacted?
        if content.startswith('[') and 'compacted' in content[:80]:
            continue

        tool_name = msg.get('name', 'tool')
        original_len = len(content)
        preview = content[:max_chars]
        # Truncate at last newline for cleanliness
        last_nl = preview.rfind('\n', max_chars // 2)
        if last_nl > 0:
            preview = preview[:last_nl]
        msg['content'] = (
            f'{preview}\n\n'
            f'[... truncated — was {original_len:,} chars, '
            f'showing first {len(preview):,}. '
            f'Re-call {tool_name} if full content needed.]'
        )
        truncated += 1

    return truncated


def rebuild_messages_with_history(
    conv_id: str,
    frontend_messages: list[dict[str, Any]],
    truncate_old: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Replace frontend messages with server-stored full-history messages.

    The frontend sends:
      [system?, ...user/assistant pairs (summary-only)..., new_user_msg]

    We replace with:
      [system?, ...full messages from store..., new_user_msg]

    If truncate_old is True, old-turn tool results are truncated to
    _OLD_RESULT_MAX_CHARS to control context growth.

    Returns:
      (rebuilt_messages, stats_dict)
      stats_dict has keys: used_store, frontend_msg_count, store_msg_count,
                           tool_msgs_restored, new_user_msg_found,
                           old_results_truncated
    """
    stats = {
        'used_store': False,
        'frontend_msg_count': len(frontend_messages),
        'store_msg_count': 0,
        'tool_msgs_restored': 0,
        'new_user_msg_found': False,
        'old_results_truncated': 0,
    }

    stored = get_messages(conv_id)
    if stored is None:
        return frontend_messages, stats

    # Extract the new user message from frontend messages (the last user msg)
    new_user_msg = None
    system_msg = None

    # Find system message (if any) from frontend
    if frontend_messages and frontend_messages[0].get('role') == 'system':
        system_msg = frontend_messages[0]

    # The new user message is the LAST message from the frontend
    if frontend_messages and frontend_messages[-1].get('role') == 'user':
        new_user_msg = frontend_messages[-1]
        stats['new_user_msg_found'] = True

    if not new_user_msg:
        logger.warning('[MsgStore] conv=%s No new user message found in frontend messages — '
                       'falling back to frontend messages', conv_id[:8])
        return frontend_messages, stats

    # Build the rebuilt message list:
    # 1. System message (from frontend — may have been updated)
    # 2. Stored messages (full history, skip any leading system message)
    # 3. New user message
    rebuilt = []

    if system_msg:
        rebuilt.append(system_msg)

    # Add stored messages, skipping system messages (we use frontend's system)
    for msg in stored:
        if msg.get('role') == 'system':
            continue
        rebuilt.append(msg)

    # ── Defence-in-depth: strip orphaned trailing tool_calls ──
    # If the previous turn was aborted mid-tool-call, the stored messages
    # may end with an assistant message containing tool_calls but no matching
    # tool_result messages after it.  Claude/Anthropic API rejects this with
    # HTTP 400 "tool_use ids were found without tool_result blocks".
    # Fix: strip such trailing messages before adding the new user message.
    _orphan_stripped = 0
    while rebuilt and rebuilt[-1].get('tool_calls'):
        _popped = rebuilt.pop()
        _orphan_stripped += 1
        # Preserve any content that was alongside the tool_calls
        if _popped.get('content'):
            rebuilt.append({'role': 'assistant', 'content': _popped['content']})
    if _orphan_stripped:
        logger.warning(
            '[MsgStore] conv=%s Stripped %d orphaned trailing tool_calls message(s) '
            '(aborted turn without tool_result) — prevents HTTP 400',
            conv_id[:8], _orphan_stripped,
        )

    # Add the new user message
    rebuilt.append(new_user_msg)

    # Count tool messages restored
    tool_msg_count = sum(
        1 for msg in rebuilt
        if msg.get('tool_calls') or msg.get('role') == 'tool'
    )

    stats['used_store'] = True
    stats['store_msg_count'] = len(stored)
    stats['tool_msgs_restored'] = tool_msg_count

    # ── Truncate old-turn tool results to control context growth ──
    if truncate_old:
        n_truncated = _truncate_old_tool_results(rebuilt)
        stats['old_results_truncated'] = n_truncated
        if n_truncated:
            logger.info(
                '[MsgStore] conv=%s Truncated %d old tool results to ≤%d chars',
                conv_id[:8], n_truncated, _OLD_RESULT_MAX_CHARS,
            )

    logger.info(
        '[MsgStore] conv=%s Rebuilt messages: frontend=%d → stored=%d + new_user → total=%d '
        '(tool_msgs=%d, truncated=%d)',
        conv_id[:8], len(frontend_messages), len(stored), len(rebuilt), tool_msg_count,
        stats['old_results_truncated'],
    )

    return rebuilt, stats


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


def estimate_token_overhead(
    frontend_messages: list[dict[str, Any]],
    stored_messages: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Estimate the token overhead of using full tool history vs summary.

    Returns character counts (as a rough proxy for tokens — ~4 chars/token).

    This is for the A/B experiment: compare how much larger the full-history
    messages are compared to the frontend's summary-only messages.
    """

    # Vision-API images cost a fixed number of tokens (NOT base64 length).
    _IMG_TOKEN_COST = 800  # ~average for detail=high

    def _msg_chars(messages):
        """Total characters across all message content.

        Images are counted as fixed token cost (converted to char equivalent)
        instead of their base64 byte length — the LLM API processes images
        natively, not as text.
        """
        total = 0
        for msg in messages:
            content = msg.get('content', '')
            if isinstance(content, str):
                total += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get('type') == 'text':
                            total += len(block.get('text', ''))
                        elif block.get('type') == 'image_url':
                            # Fixed cost, not base64 length
                            total += _IMG_TOKEN_COST * 4
            # Count tool_calls arguments
            for tc in msg.get('tool_calls', []):
                fn = tc.get('function', {})
                total += len(fn.get('name', ''))
                total += len(fn.get('arguments', ''))
        return total

    frontend_chars = _msg_chars(frontend_messages)
    stored_chars = _msg_chars(stored_messages) if stored_messages else 0

    return {
        'frontend_chars': frontend_chars,
        'frontend_est_tokens': frontend_chars // 4,
        'stored_chars': stored_chars,
        'stored_est_tokens': stored_chars // 4,
        'overhead_chars': stored_chars - frontend_chars,
        'overhead_est_tokens': (stored_chars - frontend_chars) // 4,
        'ratio': round(stored_chars / max(frontend_chars, 1), 2),
    }
