"""lib/llm_sanitize/_messages.py — Structural message-list fixes.

Replaces empty user/tool content with placeholders and merges consecutive
same-role messages — both defensive against Anthropic HTTP 400s.
"""

from lib.log import get_logger

logger = get_logger(__name__)


def _fix_empty_user_messages(messages: list) -> list:
    """Replace empty user/tool message content with a placeholder.

    Anthropic rejects requests where any user message has empty content
    (HTTP 400: ``messages.N: user messages must have non-empty content``).
    Empty content arises from:
      - sanitization stripping every block (e.g. all-image content for a
        non-vision model collapsing to ``''``)
      - failed uploads / transcription saving a blank user turn
      - compaction producing an empty placeholder
      - tool messages whose result was an empty string

    Strategy: scan all user and tool messages; for any whose ``content``
    is empty (``''``, ``[]``, all-empty text blocks, or ``None``), swap in
    a single text block with a non-empty placeholder so the request shape
    stays valid without losing the message's positional meaning (which
    matters for tool_call_id adjacency).

    Mutates messages in-place. Returns the same list for chaining.
    """
    if not messages:
        return messages

    fixed_user = 0
    fixed_tool = 0
    for idx, msg in enumerate(messages):
        role = msg.get('role')
        if role not in ('user', 'tool'):
            continue

        content = msg.get('content')
        is_empty = False
        if content is None:
            is_empty = True
        elif isinstance(content, str):
            if not content.strip():
                is_empty = True
        elif isinstance(content, list):
            if not content:
                is_empty = True
            else:
                # Empty if every block has no meaningful text/data
                has_text = False
                has_non_text = False
                for block in content:
                    if not isinstance(block, dict):
                        has_non_text = True
                        continue
                    btype = block.get('type')
                    if btype == 'text':
                        if (block.get('text') or '').strip():
                            has_text = True
                    else:
                        # image_url, tool_result, etc. — non-empty by virtue of presence
                        has_non_text = True
                if not has_text and not has_non_text:
                    is_empty = True

        if not is_empty:
            continue

        placeholder = ('[empty tool result]' if role == 'tool'
                       else '[empty message]')
        msg['content'] = placeholder
        if role == 'user':
            fixed_user += 1
        else:
            fixed_tool += 1
        logger.warning('[build_body] Replaced empty %s message at index %d '
                       'with placeholder (would trigger Anthropic HTTP 400)',
                       role, idx)

    if fixed_user or fixed_tool:
        logger.info('[build_body] Empty-content fixes: user=%d tool=%d',
                    fixed_user, fixed_tool)

    return messages


def _merge_consecutive_same_role(messages: list) -> list:
    """Merge consecutive messages with the same role (except system/tool).

    Endpoint mode can produce consecutive assistant messages (planner + worker)
    in the DB conversation.  If the frontend fails to filter the planner message,
    this backend defense-in-depth merges them by concatenating content.

    Rules:
      - system messages: never merged (each has distinct purpose)
      - tool messages: never merged (each maps to a specific tool_call_id)
      - user/assistant: consecutive same-role messages are merged with \\n\\n separator
      - Messages with tool_calls are never merged (they are function-call requests)

    Mutates nothing — returns a new list.
    """
    if not messages or len(messages) < 2:
        return list(messages)

    merged = [messages[0]]
    merge_count = 0
    for msg in messages[1:]:
        role = msg.get('role', '')
        prev_role = merged[-1].get('role', '')

        # Never merge system, tool, or messages with tool_calls
        if (role == prev_role
                and role in ('user', 'assistant')
                and not msg.get('tool_calls')
                and not merged[-1].get('tool_calls')):
            # Merge content by concatenation
            prev_content = merged[-1].get('content', '') or ''
            new_content = msg.get('content', '') or ''
            # Handle multimodal content (list of blocks)
            if isinstance(prev_content, list) or isinstance(new_content, list):
                # Convert both to list form and concatenate
                if isinstance(prev_content, str):
                    prev_content = [{'type': 'text', 'text': prev_content}] if prev_content else []
                if isinstance(new_content, str):
                    new_content = [{'type': 'text', 'text': new_content}] if new_content else []
                merged[-1] = dict(merged[-1])
                merged[-1]['content'] = prev_content + new_content
            else:
                separator = '\n\n' if prev_content and new_content else ''
                merged[-1] = dict(merged[-1])
                merged[-1]['content'] = prev_content + separator + new_content
            merge_count += 1
        else:
            merged.append(msg)

    if merge_count:
        logger.info('[build_body] Merged %d consecutive same-role message(s) '
                    '(%d → %d messages)', merge_count, len(messages), len(merged))
    return merged
