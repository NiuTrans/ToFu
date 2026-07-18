"""Message rebuild-with-history + token-overhead estimation.

``rebuild_messages_with_history`` swaps the frontend's summary-only messages
for the server-stored full-fidelity history (+ the new user message), applying
old-turn truncation. ``estimate_token_overhead`` is the A/B measurement helper.

Depends on the shared store (``get_messages``) and the truncator — dependency
direction is acyclic: _rebuild → {_store, _truncate}.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger

from lib.tasks_pkg.server_message_store._store import get_messages
from lib.tasks_pkg.server_message_store._truncate import (
    _OLD_RESULT_MAX_CHARS,
    _truncate_old_tool_results,
)

logger = get_logger(__name__)


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
        n_truncated = _truncate_old_tool_results(rebuilt, conv_id=conv_id)
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
