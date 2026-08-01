"""Round-request preamble (pt_03f4cdf1 slice 28).

Extracted 2026-07-31 from ``lib/tasks_pkg/orchestrator/_run.py``
run_task's stream loop, where the cluster ran inline once per stream
round after inbox drain and before the streaming-tool accumulator
construction. Byte-identical behaviour.

Five steps, in order:

1. Gate the tool list for this round — ``None`` once
   ``round_num >= max_tool_rounds`` so the model sees an empty tool
   surface on the forced-final round.
2. Cache-aware tool-result ordering: sort consecutive tool results by
   tool_call_id so the prefix is deterministic across rounds
   (important for automatic prefix caching on OpenAI/Qwen).
3. Emit the messages-snapshot debug event — AFTER the sort so the
   panel reflects the real outbound ordering.
4. Build the request body via the LATE-BOUND facade
   (``_o.build_body``): the facade module is bound, never the
   function, so a test/consumer that reassigns
   ``orchestrator.build_body`` steers this call (the invariant
   documented at _run.py's own facade import).
5. Attach ``body['_task_id']`` — the session-stable TTL latch in
   add_cache_breakpoints (prevents mid-session cache key shift).

Returns ``(_tools_this_round, body)``: the gated tool list is still
needed downstream by the round-checkpoint call (slice 20).
"""

from __future__ import annotations

import logging

import lib.tasks_pkg.orchestrator as _o
from lib.tasks_pkg.cache_tracking import sort_tool_results
from lib.tasks_pkg.orchestrator._messages_snapshot import (
    emit_messages_snapshot_event,
)


logger = logging.getLogger(__name__)


def build_round_request(task, rs, messages, tool_list, *,
                        round_num, tid, max_tool_rounds,
                        thinking_depth, temperature, max_tokens,
                        response_format):
    """Build this round's (gated tool list, request body) pair.

    ``task`` / ``rs`` / ``messages`` / ``tool_list`` are positional
    carriers; every scalar is keyword-only so callers cannot get
    argument order wrong. ``rs`` supplies model / preset /
    thinking_enabled (mutated across rounds by resume-state and the
    LLM-call writeback, so read fresh each round).
    """
    _tools_this_round = (
        tool_list if (tool_list and round_num < max_tool_rounds) else None)

    # Cache-aware tool result ordering: sort consecutive tool results
    # by tool_call_id so the prefix is deterministic across rounds
    # (important for automatic prefix caching on OpenAI/Qwen).
    sort_tool_results(messages, conv_id=task.get('convId', ''))

    # Emit messages snapshot for the debug panel (AFTER sort_tool_results
    # so the panel reflects the real outbound ordering). See
    # _messages_snapshot for the wire-sanitize / kind='request' /
    # endpoint-phase contracts and the best-effort try/except that
    # ensures an inspector failure never breaks the LLM round.
    emit_messages_snapshot_event(
        task, messages,
        tid=tid, round_num=round_num, model=rs.model,
        thinking_enabled=rs.thinking_enabled,
        thinking_depth=thinking_depth,
        preset=rs.preset,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format=response_format,
        tools=_tools_this_round,
    )

    body = _o.build_body(
        rs.model, messages,
        max_tokens=max_tokens,
        temperature=temperature,
        thinking_enabled=rs.thinking_enabled,
        preset=rs.preset,
        thinking_depth=thinking_depth,
        tools=_tools_this_round,
        response_format=response_format,
        stream=True,
    )
    # Attach task_id for session-stable TTL latch in
    # add_cache_breakpoints (prevents mid-session cache key shift).
    body['_task_id'] = task['id']

    return _tools_this_round, body
