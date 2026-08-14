"""Round-request preamble (pt_03f4cdf1 slice 28).

Extracted 2026-07-31 from ``lib/tasks_pkg/orchestrator/_run.py``
run_task's stream loop, where the cluster ran inline once per stream
round after inbox drain and before the streaming-tool accumulator
construction. Byte-identical behaviour.

Five steps, in order:

1. Reuse the turn's stable tool list. Tools remain available on every round
   until the model naturally returns a response without tool calls.
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
5. Attach internal task/conversation context — the session-stable TTL latch,
   the Responses prompt-cache namespace, and the user-approved economic
   working-set threshold.  Protocol converters consume these keys; none reach
   an upstream wire unchanged.

Returns ``(_tools_this_round, body)``: the gated tool list is still
needed downstream by the round-checkpoint call (slice 20).
"""

from __future__ import annotations

import hashlib

import lib.tasks_pkg.orchestrator as _o
from lib.log import get_logger
from lib.tasks_pkg.cache_tracking import sort_tool_results
from lib.tasks_pkg.orchestrator._messages_snapshot import (
    emit_messages_snapshot_event,
)


logger = get_logger(__name__)


def build_round_request(task, rs, messages, tool_list, *,
                        round_num, tid,
                        thinking_depth, temperature, max_tokens,
                        response_format):
    """Build this round's (gated tool list, request body) pair.

    ``task`` / ``rs`` / ``messages`` / ``tool_list`` are positional
    carriers; every scalar is keyword-only so callers cannot get
    argument order wrong. ``rs`` supplies model / preset /
    thinking_enabled (mutated across rounds by resume-state and the
    LLM-call writeback, so read fresh each round).
    """
    _tools_this_round = tool_list

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
    body['_conv_id'] = task.get('convId') or ''
    try:
        from lib.context_experiment_flags import (
            normalize_context_experiment_flags)
        _experiment_flags = normalize_context_experiment_flags(
            task.get('config') or {})
        body['_gpt56_breakpoint_mode'] = (
            _experiment_flags['cache']['gpt56BreakpointMode'])
        body['_tool_search_mode'] = (
            _experiment_flags['tools']['toolSearch'])
        _responses_flags = _experiment_flags['responses']
        body['_responses_transport'] = _responses_flags['transport']
        body['_reasoning_mode'] = _responses_flags['reasoningMode']
        body['_text_verbosity'] = _responses_flags['verbosity']
        body['_image_detail'] = _responses_flags['imageDetail']
        from lib.tasks_pkg.gpt56_optimization import (
            resolve_gpt56_optimizations)
        _optimization = resolve_gpt56_optimizations(
            requested_programmatic=(
                _experiment_flags['tools']['programmaticCalling']),
            requested_multi_agent=_responses_flags['multiAgent'],
            messages=messages, tools=_tools_this_round, round_num=round_num)
        body['_programmatic_tool_calling'] = _optimization[
            'programmaticCalling']
        body['_programmatic_stage'] = _optimization['programmaticStage']
        body['_multi_agent_mode'] = _optimization['multiAgent']
        body['_multi_agent_stage'] = _optimization['multiAgentStage']
        body['_multi_agent_max_concurrent_subagents'] = _responses_flags[
            'maxConcurrentSubagents']
        task['_gpt56Optimization'] = _optimization
        _decision_history = task.setdefault(
            '_gpt56OptimizationDecisions', [])
        if isinstance(_decision_history, list):
            _decision_history.append(dict(_optimization))
            # Keep task-state telemetry bounded on unusually long runs.
            del _decision_history[:-64]
    except Exception as e:
        logger.warning('[Task %s] context experiment flag lookup failed: %s',
                       tid, e, exc_info=True)
    body['_frontend_selected_tool_names'] = list(
        task.get('_frontendSelectedToolNames') or [])
    body['_tool_namespace_by_name'] = dict(
        task.get('_toolNamespaceByName') or {})
    _executable_tool_catalog = task.get(
        '_executable_tool_catalog', task.get('_enabled_tool_catalog'))
    body['_executable_tool_catalog'] = list(
        _tools_this_round or []
        if not isinstance(_executable_tool_catalog, list)
        else _executable_tool_catalog)
    body['_enabled_tool_catalog'] = list(
        body['_executable_tool_catalog'])
    # Provider conversion starts from this round's visibility projection.
    # Keep it separate from execution authority because Tool Search may defer
    # schemas without removing the corresponding executable capability.
    body['_tool_wire_catalog'] = list(_tools_this_round or [])
    body['_tool_discovery_policy_by_name'] = dict(
        task.get('_toolDiscoveryPolicyByName') or {})
    _tool_search_catalog_size = task.get('_toolSearchCatalogSize')
    body['_tool_search_catalog_size'] = int(
        len(body['_executable_tool_catalog'])
        if _tool_search_catalog_size is None else _tool_search_catalog_size)
    body['_tool_searchable_count'] = int(
        task.get('_toolSearchableCount') or 0)
    body['_tool_search_mode'] = str(
        task.get('_toolSearchMode') or body.get('_tool_search_mode') or 'auto')
    body['_tool_search_capabilities'] = dict(
        (task.get('config') or {}).get('toolSearchCapabilities') or {})
    body['_tool_search_fail_open'] = bool(
        task.get('_tool_search_fail_open'))
    # OpenAI recommends a stable, privacy-preserving end-user identifier.
    # Never send a raw tenant/user value; anonymous personal-mode tasks omit
    # the field rather than inventing a cross-user identity.
    _principal = (task.get('_userId')
                  or (task.get('config') or {}).get('user') or '')
    if _principal:
        body['_safety_identifier'] = 'tofu_' + hashlib.sha256(
            ('tofu-safety:' + str(_principal)).encode('utf-8')
        ).hexdigest()[:32]
    try:
        from lib.tasks_pkg.compaction import (
            _compaction_trigger_threshold,
            _working_set_token_limit,
        )
        _working_set = _working_set_token_limit(task)
        # A positive economic ceiling is additionally clamped by the model's
        # window-safety threshold.  Sending a 2M operator override verbatim to
        # a 1M Responses model would make server compaction useless past the
        # hard limit.  Zero remains an explicit opt-out.
        body['_working_set_tokens'] = (
            _compaction_trigger_threshold(task)[0] if _working_set > 0 else 0)
    except Exception as e:
        # Request construction must remain available if the optional economic
        # policy module cannot load; the local compaction gate logs its own
        # failures and the Responses converter treats a missing value as off.
        logger.warning('[Task %s] working-set policy lookup failed: %s',
                       tid, e, exc_info=True)

    try:
        from lib.context_telemetry import capture_round_context
        capture_round_context(
            task, body.get('messages') or [], _tools_this_round,
            round_num=round_num, model=rs.model)
    except Exception as e:
        logger.debug('[Task %s] round context telemetry skipped: %s', tid, e)

    return _tools_this_round, body
