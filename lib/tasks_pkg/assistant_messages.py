"""Canonical assembly for prose-only assistant messages between LLM rounds.

Tool-call assistant rows have their own builder in
``conv_message_builder._toolcalls``.  This leaf owns the complementary shape:
an assistant response with prose/reasoning and no tool calls.  Both terminal
finalization and system-driven continuations use it so a continuation cannot
drop the assistant response that triggered the nudge from the next request's
context.
"""

from __future__ import annotations

from typing import Any


PARTIAL_STREAM_PREFILL_MARKER = '_partialStreamPrefill'
_CONTINUATION_CONTENT_UNSET = object()


def append_assistant_message_with_partial_prefill(
    messages: list[dict[str, Any]],
    message: dict[str, Any],
    *,
    continuation_content: object = _CONTINUATION_CONTENT_UNSET,
) -> dict[str, Any]:
    """Append one assistant row or atomically consume its retry prefill.

    A lossless partial-stream retry temporarily leaves a marked assistant
    prefill at the tail of ``messages``. Once the continuation succeeds, that
    prefill and the returned assistant message are one logical provider turn.
    Replace them with one row and concatenate content byte-for-byte—never let
    the generic same-role sanitizer inject its normal ``\n\n`` separator.

    ``continuation_content`` lets structured tool-call assembly retain the raw
    returned text even though its canonical builder normally strips incidental
    inter-round whitespace.
    """
    trailing = messages[-1] if messages else None
    if (isinstance(trailing, dict)
            and trailing.get('role') == 'assistant'
            and trailing.get(PARTIAL_STREAM_PREFILL_MARKER)
            and not trailing.get('tool_calls')):
        prefix = trailing.get('content')
        prefix = prefix if isinstance(prefix, str) else str(prefix or '')
        if continuation_content is _CONTINUATION_CONTENT_UNSET:
            continuation = message.get('content')
        else:
            continuation = continuation_content
        continuation = (
            continuation if isinstance(continuation, str)
            else str(continuation or '')
        )
        merged = dict(message)
        merged.pop(PARTIAL_STREAM_PREFILL_MARKER, None)
        combined = prefix + continuation
        if combined:
            merged['content'] = combined
        else:
            merged.pop('content', None)
        messages[-1] = merged
        return merged
    messages.append(message)
    return message


def build_assistant_prose_message(
    assistant_message: dict[str, Any] | None,
    *,
    task: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return the replay-safe prose assistant row, or ``None`` if empty."""
    if not isinstance(assistant_message, dict):
        return None
    content = assistant_message.get('content') or ''
    reasoning = assistant_message.get('reasoning_content') or ''
    responses_items = assistant_message.get('_responses_items') or []
    anthropic_blocks = assistant_message.get('_anthropic_content_blocks') or []
    if not (content or reasoning or responses_items or anthropic_blocks):
        return None

    message: dict[str, Any] = {'role': 'assistant', 'content': content}
    if reasoning:
        message['reasoning_content'] = reasoning
    if responses_items:
        message['_responses_items'] = responses_items
        if task is not None:
            task['_responsesItems'] = responses_items
    if anthropic_blocks:
        message['_anthropic_content_blocks'] = anthropic_blocks
        if task is not None:
            task['_anthropicContentBlocks'] = anthropic_blocks
    return message


def append_assistant_prose_message(
    messages: list[dict[str, Any]],
    assistant_message: dict[str, Any] | None,
    *,
    task: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Build and append one prose assistant row, returning the row appended."""
    message = build_assistant_prose_message(assistant_message, task=task)
    if message is not None:
        return append_assistant_message_with_partial_prefill(messages, message)
    return message


__all__ = [
    'PARTIAL_STREAM_PREFILL_MARKER',
    'append_assistant_message_with_partial_prefill',
    'append_assistant_prose_message',
    'build_assistant_prose_message',
]
