# HOT_PATH — called every stream round the assistant emits tool_calls.
"""Live-tail assistant/tool_call assembly + inter-round narration
discard + incremental auto-translate submit — the "we've decided to
call tools this round" bracket.

Extracted 2026-07-31 (pt_03f4cdf1 slice 16) from
``lib/tasks_pkg/orchestrator/_run.py``'s stream loop.

**What it does**
    Right after ``rs.tool_call_happened = True``, the stream loop
    performs three sequential mutations that all belong together
    — the ``we've decided to call tools this round`` bracket:

    1. Assemble the live-tail assistant/tool_call message through the
       SHARED ``build_assistant_tool_call_message`` (same as the
       replay path ``_reconstruct_tool_call_messages`` uses) — the
       SINGLE SOURCE guarantee that keeps live vs replay structurally
       identical (WIRE PREFIX CHANGED miss avoidance). Content is
       stripped, reasoning_content is carried whenever thinking is
       present, and the thinking-block signature only when present
       (so the NEXT tool-loop turn replays a signed thinking block).
    2. Discard the inter-round narration this round streamed before
       its tool calls (via ``_discard_pretool_prose``) so the
       backend and client agree on where prose ended, tool started.
    3. Submit an incremental auto-translate for this round's prose
       segment (via ``lib.translate.submit_round_segment``) so the
       Chinese lands by task end instead of one giant stall — gated
       + isolated inside the helper; a no-op when autoTranslate is
       off. Best-effort: a translate-side ImportError never breaks
       the LLM round.

**What it does NOT do**
    Does NOT run the tool execution pipeline (that lives inline in
    ``run_task`` — starts with the ``if task['aborted']`` early-exit
    check and continues into ``execute_tool_pipeline``). This helper
    is a pure mutation prelude — no early exits, no loop-mutation, no
    control-flow crossings.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger
from lib.tasks_pkg.conv_message_builder import (
    build_assistant_tool_call_message,
)
from lib.tasks_pkg.orchestrator._finalize import _discard_pretool_prose


logger = get_logger(__name__)


def append_assistant_tool_call_message(
    task: dict[str, Any],
    messages: list[dict[str, Any]],
    *,
    round_num: int,
    tid: str,
    assistant_msg: dict[str, Any],
) -> None:
    """Assemble + append the live-tail assistant/tool_call message,
    discard the pre-tool prose, and submit an incremental auto-translate.

    Args:
        task: The live task dict; ``task['_compact_messages']`` is
            stamped here so context_compact tool handler sees the
            live message list.
        messages: The live outbound message list — MUTATED in place:
            one assistant message with ``tool_calls`` is appended.
        round_num: 0-indexed round number for this LLM call.
        tid: Task id for log lines.
        assistant_msg: The raw assistant message the stream produced
            this round (has ``tool_calls`` + ``content`` +
            ``reasoning_content`` + ``thinking_signature`` fields).
    """
    # ★ SINGLE SOURCE: assemble the live-tail assistant/tool_call
    #   message through build_assistant_tool_call_message — the SAME
    #   function the replay path (_reconstruct_tool_call_messages) uses.
    #   This makes the live tail and every replay path emit byte-
    #   identical fields for the turn, structurally: content is STRIPPED
    #   (the pre-tool prose snapshot assistantContent is persisted
    #   stripped; a raw↔stripped flip was a WIRE PREFIX CHANGED miss),
    #   reasoning_content is carried whenever thinking is present, and
    #   the thinking-block signature only when present (so the NEXT
    #   tool-loop turn replays a signed thinking block). All those gates
    #   now live in ONE place, so a future field can never re-diverge
    #   between the two paths. See build_assistant_tool_call_message.
    _builder_kwargs = dict(
        tool_calls=assistant_msg['tool_calls'],
        content=assistant_msg.get('content'),
        reasoning_content=assistant_msg.get('reasoning_content'),
        thinking_signature=assistant_msg.get('thinking_signature'))
    if assistant_msg.get('_responses_items'):
        _builder_kwargs['responses_items'] = assistant_msg['_responses_items']
    if assistant_msg.get('_anthropic_content_blocks'):
        _builder_kwargs['anthropic_content_blocks'] = (
            assistant_msg['_anthropic_content_blocks'])
    clean_msg = build_assistant_tool_call_message(**_builder_kwargs)
    messages.append(clean_msg)

    # ★ Discard the inter-round narration this round streamed before
    #   its tool calls (backend reset + client DELTA_RESET). See
    #   _discard_pretool_prose for the full rationale.
    _discard_pretool_prose(task, round_num)

    # ★ Incremental auto-translate: this round's prose segment is now
    #   self-contained (the model finished its commentary and is about
    #   to call tools). Translate it in the background so it's ready by
    #   task end instead of one big translation stall. Gated + isolated
    #   inside the helper; a no-op when autoTranslate is off.
    try:
        from lib.translate import submit_round_segment
        submit_round_segment(
            task, round_num, assistant_msg.get('content') or '')
    except Exception as _ite:
        logger.debug(
            '[%s] incremental translate submit failed (non-fatal): %s',
            tid, _ite)

    # ★ Expose live messages to context_compact tool handler
    task['_compact_messages'] = messages
