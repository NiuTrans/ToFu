"""Round-request preamble ( slice 28).

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
4. Build the request body through the explicit ``_ports`` dependency owner.
5. Attach internal task/conversation context — the session-stable TTL latch,
   the Responses prompt-cache namespace, and the user-approved economic
   working-set threshold.  Protocol converters consume these keys; none reach
   an upstream wire unchanged.

Returns ``(_tools_this_round, body)``: the gated tool list is still
needed downstream by the round-checkpoint call (slice 20).
"""

from __future__ import annotations

import hashlib

import lib.tasks_pkg.orchestrator._ports as orchestrator_ports
from lib.log import get_logger
from lib.tasks_pkg.cache_tracking._prefix import sort_tool_results
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
    # Flow-phase contracts and the best-effort try/except that
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

    body = orchestrator_ports.build_request_body(
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
    # Request-local authority latches must never survive a failed policy
    # lookup into the next round.
    task['_ptc_local'] = None
    task['_toolOrchestration'] = None
    try:
        from lib.context_experiment_flags import (
            normalize_context_experiment_flags)
        _experiment_flags = normalize_context_experiment_flags(
            task.get('config') or {})
        body['_gpt56_breakpoint_mode'] = (
            _experiment_flags['cache']['gpt56BreakpointMode'])
        body['_tool_search_mode'] = (
            _experiment_flags['tools']['toolSearch'])
        _schema_budget = int(
            _experiment_flags['tools']['schemaBudgetTokens'] or 0)
        body['_tool_schema_budget_tokens'] = _schema_budget
        _responses_flags = _experiment_flags['responses']
        _orchestration_flags = _experiment_flags['orchestration']
        body['_responses_transport'] = _responses_flags['transport']
        body['_reasoning_mode'] = _responses_flags['reasoningMode']
        body['_text_verbosity'] = _responses_flags['verbosity']
        body['_image_detail'] = _responses_flags['imageDetail']
        from lib.tasks_pkg.tool_orchestration_policy import (
            resolve_tool_orchestration)
        from lib.tools.programmatic import ACTIVE_PROGRAMMATIC_MODES
        _optimization = resolve_tool_orchestration(
            requested_programmatic=(
                _experiment_flags['tools']['programmaticCalling']),
            requested_multi_agent=_orchestration_flags['multiAgent'],
            messages=messages, tools=_tools_this_round, round_num=round_num,
            model=rs.model,
            policy_version=_orchestration_flags['policy'])
        body['_programmatic_tool_calling'] = _optimization[
            'programmaticCalling']
        body['_programmatic_stage'] = _optimization['programmaticStage']
        # Local-backend (all-models PTC) per-round context.  The wire
        # boundary (_sse_core) resolves native_openai vs local; the gateway
        # handler gates program eligibility on the task latch below.  The
        # latch is refreshed EVERY round so a stale read-only contract can
        # never outlive the intent that activated it.
        body['_programmatic_tier'] = _optimization.get(
            'programmaticTier') or ''
        body['_programmatic_eligible_tools'] = list(
            _optimization.get('programmaticEligibleTools') or [])
        # ``programmaticSerialChain`` remains in the task-owned decision row
        # below for adoption diagnostics.  It deliberately does NOT enter the
        # request body: interpolating a growing per-round observation into the
        # execute_tools description changes the provider's cached tools prefix.
        task['_ptc_local'] = (
            {'tier': body['_programmatic_tier'],
             'eligible': body['_programmatic_eligible_tools']}
            if _optimization['programmaticCalling']
            in ACTIVE_PROGRAMMATIC_MODES else None)
        body['_multi_agent_mode'] = _optimization['multiAgent']
        body['_multi_agent_stage'] = _optimization['multiAgentStage']
        body['_multi_agent_max_concurrent_agents'] = (
            _orchestration_flags['maxConcurrentAgents'])
        body['_tool_orchestration_policy_version'] = _optimization[
            'policyVersion']
        body['_tool_orchestration_composition'] = _optimization[
            'compositionMode']
        # Generic task-owned decision.  The local Swarm handler consumes this
        # synchronously when spawn_agents is selected; provider-native fields
        # are still resolved later at the wire boundary.
        _task_orchestration = dict(_optimization)
        _task_orchestration['maxConcurrentAgents'] = (
            _orchestration_flags['maxConcurrentAgents'])
        task['_toolOrchestration'] = _task_orchestration

        _decision_history = task.get('_toolOrchestrationDecisions')
        if not isinstance(_decision_history, list):
            _decision_history = []
            task['_toolOrchestrationDecisions'] = _decision_history
        if isinstance(_decision_history, list):
            # Keep the exact history row as a shallow request sidecar. The
            # final provider boundary annotates it with the independently
            # resolved native/local backends; it is never serialized upstream.
            _decision_row = dict(_task_orchestration)
            _decision_history.append(_decision_row)
            body['_tool_orchestration_decision_sink'] = _decision_row
            # Keep task-state telemetry bounded on unusually long runs.
            del _decision_history[:-64]

    except Exception as e:
        logger.warning('[Task %s] context experiment flag lookup failed: %s',
                       tid, e, exc_info=True)
    body['_frontend_selected_tool_names'] = list(
        task.get('_frontendSelectedToolNames') or [])
    body['_tool_namespace_by_name'] = dict(
        task.get('_toolNamespaceByName') or {})
    _executable_tool_catalog = task.get('_executable_tool_catalog')
    body['_executable_tool_catalog'] = list(
        _tools_this_round or []
        if not isinstance(_executable_tool_catalog, list)
        else _executable_tool_catalog)
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
        from lib.tasks_pkg.compaction._tokens import (
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
