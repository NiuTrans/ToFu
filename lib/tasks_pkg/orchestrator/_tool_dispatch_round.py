"""Per-round tool dispatch: parse → sanitize → emit → heartbeat → execute.

Extracted 2026-07-31 (pt_03f4cdf1 slice 22) from
``lib/tasks_pkg/orchestrator/_run.py`` run_task stream loop.

Runs after the abort-before-tools gate and before the
consecutive-timeout circuit breaker. Four phases:

1. **Parse** (``parse_tool_calls``): parses all tool_calls from the
   assistant message, consuming round numbers from
   ``rs.tool_round_num``. ``early_announced`` passes the streaming
   accumulator's announced-tc map so tool_start events already sent
   during streaming are not re-emitted.
2. **Sanitize** (``sanitize_malformed_tool_call_args`` — slice 14):
   rewrites malformed JSON args in the stored messages so the next
   API round doesn't carry them back to the gateway (HTTP-400
   recovery).
3. **Emit** (``emit_tool_exec_phase``): execution-phase event so the
   frontend shows what is running.
4. **Execute** (``execute_tool_pipeline``): approval + parallel +
   result append. The reaper heartbeat
   (``task['_dispatch_heartbeat']``) is refreshed immediately before
   entering the pipeline — a long tool run (or a human-guidance /
   approval block inside it) emits no delta, so the positive-
   liveness clock must be refreshed or the stuck-task reaper could
   kill a healthy long tool. After the pipeline returns, the live
   ``_compact_messages`` ref is popped.

Returns the pipeline's ``_tool_timed_out`` flag so the caller can
feed the consecutive-timeout circuit breaker. Mutates ``messages``,
``rs.tool_round_num``, and the task sidecars in place.
"""

from __future__ import annotations

import time
from typing import Any

from lib.log import get_logger
from lib.tasks_pkg.tool_dispatch import (
    emit_tool_exec_phase,
    execute_tool_pipeline,
    parse_tool_calls,
)
from lib.tasks_pkg.orchestrator._sanitize_tool_call_args import (
    sanitize_malformed_tool_call_args,
)

logger = get_logger(__name__)


def run_tool_dispatch(
    task: dict[str, Any],
    rs: Any,
    messages: list[dict[str, Any]],
    all_search_results_text: list[str],
    *,
    round_num: int,
    tid: str,
    cfg: dict[str, Any],
    project_path: str | None,
    project_enabled: bool,
    tool_list: list[dict[str, Any]],
    announced_tc_map: dict[str, Any],
) -> bool:
    """Parse, sanitize, emit, and execute this round's tool calls.

    Parameters
    ----------
    task : dict[str, Any]
        Live task dict (mutated: ``_dispatch_heartbeat``,
        ``_compact_messages`` popped, tool results appended).
    rs : RoundState
        Loop-state carrier (mutated: ``tool_round_num``).
    messages : list[dict[str, Any]]
        Working message list — tool results appended in place.
    all_search_results_text : list[str]
        Accumulator for search-result text (pipeline sidecar).
    round_num : int
        Current round index (0-based).
    tid : str
        8-char task id for logging.
    cfg : dict[str, Any]
        Task config (pipeline input).
    project_path : str | None
        Project root path (pipeline input).
    project_enabled : bool
        Whether project mode is on (pipeline input).
    tool_list : list[dict[str, Any]]
        Assembled tool schema (pipeline input).
    announced_tc_map : dict[str, Any]
        Streaming accumulator's announced-tc map — tool_start events
        already sent during streaming are not re-emitted.

    Returns
    -------
    bool
        The pipeline's ``_tool_timed_out`` flag — feed to the
        consecutive-timeout circuit breaker.
    """
    # ── Phase 1: Parse all tool_calls ──
    #   Pass early_announced so parse_tool_calls skips re-emitting
    #   tool_start events that were already sent during streaming.
    parsed_tcs, rs.tool_round_num = parse_tool_calls(
        rs.assistant_msg, task, round_num, rs.tool_round_num, project_enabled,
        early_announced=announced_tc_map,
    )

    # ── Phase 1b: Sanitize tool_calls in messages so the next API
    #   round doesn't carry malformed JSON args back to the gateway.
    #   Extracted 2026-07-31 (pt_03f4cdf1 slice 14) into
    #   lib.tasks_pkg.orchestrator._sanitize_tool_call_args — see
    #   that module's docstring for the HTTP-400 recovery rationale
    #   and the RAW-args log-line evidence trail.
    sanitize_malformed_tool_call_args(
        parsed_tcs, messages,
        tid=tid, conv_id=task.get('convId', ''), model=rs.model)

    # ── Phase 2: Emit execution phase event ──
    emit_tool_exec_phase(task, parsed_tcs)

    # ── Phase 3: Execute tools (approval + parallel + result append) ──
    # ★ Reaper heartbeat: a long tool run (or a human-guidance/approval
    #   block inside it) emits no delta, so refresh the positive-
    #   liveness clock before entering the pipeline. See
    #   manager.reap_stuck_running_tasks.
    task['_dispatch_heartbeat'] = time.time()
    # The full executable catalog is the execution/validation authority.  The
    # provider-specific wire surface may contain only eager tools plus the
    # local gateways, or native deferred namespaces.
    _enabled_catalog = task.get(
        '_executable_tool_catalog', task.get('_enabled_tool_catalog'))
    _execution_catalog = list(
        tool_list or []
        if not isinstance(_enabled_catalog, list) else _enabled_catalog)
    _tool_timed_out = execute_tool_pipeline(
        task, parsed_tcs, cfg, project_path, project_enabled,
        _execution_catalog, messages, all_search_results_text, round_num,
        rs.model,
    )

    # Clean up live messages ref after tool execution
    task.pop('_compact_messages', None)

    return _tool_timed_out
