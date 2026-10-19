"""Shared task-owned orchestration intent and catalog request metadata.

attach_tool_request_metadata freezes the existing task prompt latch, records
bounded decisions, and attaches one executable/visible catalog pair. Preparation
and execution ownership remain with the task; this performs no model request.
"""
import copy
from lib.log import get_logger
logger = get_logger(__name__)


def attach_tool_request_metadata(task, body, messages, tool_list, *, model, round_num, tid):
    from lib.tasks_pkg.coordinator_policy import coordinator_scope
    # Role authority wins over both user settings and carried prompt latches.
    # Resolve outside the best-effort metadata block so lookup failure cannot
    # accidentally retain a provider-native execution route.
    _coordinator = coordinator_scope(task)
    _tools_this_round = tool_list
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
        from lib.tasks_pkg.invocation import is_child_invocation
        if is_child_invocation(task) and task.get('_invocationConversationToolPolicy') is None:
            # Delegated leaves may use the shared local tool gateway, but
            # cannot advertise provider-native or local redelegation. The
            # 'disabled' sentinel is internal-only: it is not a user-settable
            # mode and fails closed through the policy/backend chain.
            _orchestration_flags = {
                **_orchestration_flags, 'multiAgent': 'disabled'}
        body['_responses_transport'] = _responses_flags['transport']
        body['_reasoning_mode'] = _responses_flags['reasoningMode']
        body['_text_verbosity'] = _responses_flags['verbosity']
        body['_image_detail'] = _responses_flags['imageDetail']
        from lib.tasks_pkg.tool_orchestration_policy import (
            resolve_tool_orchestration)
        from lib.tools.programmatic import ACTIVE_PROGRAMMATIC_MODES
        _latched_optimization = (task.get('_toolOrchestrationPromptLatch')
                                or (task.get('config') or {}).get('_modelReplayToolLatch'))
        if (isinstance(_latched_optimization, dict) and not _coordinator
                and _latched_optimization.get('programmaticRequestedMode')
                == _experiment_flags['tools']['programmaticCalling']):
            _optimization = copy.deepcopy(_latched_optimization)
            _optimization['round'] = int(round_num)
            task['_toolOrchestrationPromptLatch'] = copy.deepcopy(_latched_optimization)
        else:
            _optimization = resolve_tool_orchestration(
                requested_programmatic=(
                    'off' if _coordinator else _experiment_flags['tools']['programmaticCalling']),
                requested_programmatic_exposure=(
                    _experiment_flags['tools']['programmaticExposure']),
                requested_multi_agent=('disabled' if _coordinator
                                       else _orchestration_flags['multiAgent']),
                messages=messages, tools=_tools_this_round, round_num=round_num,
                model=model,
                policy_version=_orchestration_flags['policy'])
            # Tool schemas are part of the provider cache prefix. Freeze the
            # first task decision so observed fan-out, later round numbers, or
            # retry state can never add/remove an orchestration schema midway
            # through one task.
            task['_toolOrchestrationPromptLatch'] = copy.deepcopy(_optimization)
        body['_programmatic_tool_calling'] = _optimization[
            'programmaticCalling']
        body['_programmatic_stage'] = _optimization['programmaticStage']
        # Local-backend (all-models PTC) per-round context. The wire boundary
        # (_sse_core) resolves native_openai vs local; this latch selects the
        # local tier and retains activation evidence, never child authority.
        # Refresh it every round so stale routing cannot outlive the intent
        # that activated it.
        body['_programmatic_tier'] = _optimization.get(
            'programmaticTier') or ''
        # A late JavaScript authoring failure is execution telemetry, not
        # permission to rewrite execute_tools in the cached tools prefix.
        body['_programmatic_eligible_tools'] = list(
            _optimization.get('programmaticEligibleTools') or [])
        _programmatic_active = (
            _optimization['programmaticCalling']
            in ACTIVE_PROGRAMMATIC_MODES)
        # Hiding ordinary tools for a one-request adoption trial made the
        # provider's tool prefix oscillate. Default to stable additive exposure;
        # the explicit code API experiment uses the same first-task latch.
        _programmatic_exposure = 'additive'
        _programmatic_exposure_reason = 'cache_stable_additive'
        if _programmatic_active and _optimization.get('programmaticExposurePolicy') == 'code_api':
            _programmatic_exposure = 'code_api'
            _programmatic_exposure_reason = 'cache_stable_code_api_experiment'
        body['_programmatic_exposure'] = _programmatic_exposure
        _optimization['programmaticExposure'] = _programmatic_exposure
        _optimization['programmaticExposureReason'] = (
            _programmatic_exposure_reason)
        # ``programmaticSerialChain`` remains in the task-owned decision row
        # below for adoption diagnostics.  It deliberately does NOT enter the
        # request body: interpolating a growing per-round observation into the
        # execute_tools description changes the provider's cached tools prefix.
        task['_ptc_local'] = (
            {'tier': body['_programmatic_tier'],
             'eligible': body['_programmatic_eligible_tools']}
            if _programmatic_active else None)
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
        if _coordinator:
            raise
    body['_frontend_selected_tool_names'] = list(
        task.get('_frontendSelectedToolNames') or [])
    body['_required_direct_tool_names'] = list(
        task.get('_requiredDirectToolNames') or [])
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
