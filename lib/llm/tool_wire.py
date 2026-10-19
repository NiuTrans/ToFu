"""Shared final tool visibility projection before caching/protocol translation.

project_tool_wire mutates one request copy using the existing task catalog,
selected route capabilities and orchestration decisions. It grants no execution
rights, performs no HTTP/storage work, and preserves transport diagnostics.
"""
from dataclasses import dataclass

def provider_tool_name(tool):
    if not isinstance(tool, dict):
        return ''
    function = tool.get('function')
    if isinstance(function, dict):
        return str(function.get('name') or '')
    return str(tool.get('name') or '')


_provider_tool_name = provider_tool_name

@dataclass(frozen=True, slots=True)
class ToolWireProjection:
    budget_compacted_names: object
    budget_dropped_names: object
    executable_catalog: object
    final_schema_budget: object
    multi_agent_backend: object
    ptc_backend: object
    tool_search_backend: object


def project_tool_wire(body, *, api_protocol, base_url='', oauth='', log_prefix='',
        responses_profile='compatible', inference_profile=None, activity_sink=None):
    _responses_feature_profile = responses_profile
    _route_inference_profile = inference_profile
    _request_activity_sink = activity_sink
    # Resolve discovery at the last common boundary before cache markers and
    # protocol translation.  Authorization continues to use the task-owned
    # executable catalog in the dispatch pipeline; this only controls what the
    # model sees on the wire.
    from lib.tools.gateway import preserve_background_wire_tools
    from lib.tools.direct_wire import live_direct_names, preserve_direct_wire_tools
    from lib.tools.poll import BACKGROUND_TOOL_NAMES
    _executable_catalog = body.get('_executable_tool_catalog')
    _background_authority = (
        _executable_catalog if isinstance(_executable_catalog, list)
        else body.get('tools') if isinstance(body.get('tools'), list) else [])
    _background_names = {
        _provider_tool_name(tool) for tool in _background_authority
        if _provider_tool_name(tool) in BACKGROUND_TOOL_NAMES
    }
    _direct_declarations = body.get('_required_direct_tool_names')
    _direct_names = live_direct_names(_background_authority, _direct_declarations)
    _protected_names = _background_names | _direct_names
    if _protected_names:
        # Native discovery must not defer controls for live handles. Pins
        # derive only from task authority, never search or an old projection.
        body['_frontend_selected_tool_names'] = sorted(
            set(body.get('_frontend_selected_tool_names') or ())
            | _protected_names)
        body['_tool_discovery_policy_by_name'] = {
            **(body.get('_tool_discovery_policy_by_name') or {}),
            **{name: 'eager' for name in _protected_names},
        }
    _tool_search_backend = ''
    _required_direct_names: set[str] = set(_direct_names)
    if isinstance(_executable_catalog, list):
        from lib.tools.gateway import (
            CODE_CORE_DIRECT_TOOL_NAMES,
            full_wire_tools,
            local_wire_tools,
            resolve_tool_search_backend,
        )
        if body.get('_force_local_tool_search'):
            _tool_search_backend = 'local'
        elif body.get('_tool_search_fail_open'):
            _tool_search_backend = 'full'
        else:
            _tool_search_backend = resolve_tool_search_backend(
                body.get('_tool_search_mode') or 'auto',
                protocol=api_protocol, model=body.get('model') or '',
                responses_profile=_responses_feature_profile,
                base_url=base_url or '', oauth=oauth or '',
                capabilities=body.get('_tool_search_capabilities'),
                inference_profile=_route_inference_profile)
        body['_resolved_tool_search_backend'] = _tool_search_backend
        _pins = list(body.get('_frontend_selected_tool_names') or ())
        _pin_set = {str(name) for name in _pins}
        _schema_budget = int(body.get('_tool_schema_budget_tokens') or 0)
        _choice = body.get('tool_choice')
        if isinstance(_choice, dict):
            _choice_function = _choice.get('function')
            if isinstance(_choice_function, dict) \
                    and _choice_function.get('name'):
                _pin_set.add(str(_choice_function['name']))
                _required_direct_names.add(str(_choice_function['name']))

        def _wire_catalog_tool(tool):
            if not isinstance(tool, dict):
                return False
            fn = tool.get('function')
            name = str((fn.get('name') if isinstance(fn, dict) else '')
                       or tool.get('name') or '')
            # MCP has already been searched by Tofu before this boundary.
            # Only the task-sticky active MCP schemas go to any provider;
            # inactive MCP tools remain in ``_executable_tool_catalog`` for
            # authorization and direct-name compatibility.
            return not name.startswith('mcp__') or name in _pin_set

        # Execution authority and prompt visibility are intentionally distinct.
        # The former remains live so an enabled tool can be called by name even
        # when it was not searched; the latter is this round's model-visible
        # projection and is the normal source for provider tool schemas.
        _stable_wire_catalog = body.get('_tool_wire_catalog')
        if isinstance(_stable_wire_catalog, list):
            _wire_catalog = list(_stable_wire_catalog)
        else:
            # Direct boundary callers derive visibility from the same
            # canonical authority when no narrower wire projection exists.
            _wire_catalog = [tool for tool in _executable_catalog
                             if _wire_catalog_tool(tool)]
        if _tool_search_backend == 'full' and body.get(
                '_tool_search_fail_open'):
            # A local retrieval failure is the one intentional exception: make
            # the executable directory visible so availability wins over cache.
            _wire_catalog = [tool for tool in _executable_catalog
                             if _wire_catalog_tool(tool)]
        _wire_catalog = preserve_background_wire_tools(
            _wire_catalog, authority_catalog=_background_authority)
        _wire_catalog = preserve_direct_wire_tools(
            _wire_catalog, authority_catalog=_background_authority,
            declared=_direct_declarations)
        _wire_names = {
            str(((tool.get('function') or {}).get('name')
                 if isinstance(tool.get('function'), dict)
                 else tool.get('name')) or '')
            for tool in _wire_catalog if isinstance(tool, dict)
        }
        _code_specific_floor = CODE_CORE_DIRECT_TOOL_NAMES - {'read_files'}
        if _wire_names & _code_specific_floor:
            _required_direct_names.update(
                CODE_CORE_DIRECT_TOOL_NAMES & _wire_names)
        if _tool_search_backend == 'local':
            body['tools'] = local_wire_tools(
                _wire_catalog,
                discovery_policy_by_name=body.get(
                    '_tool_discovery_policy_by_name'),
                discovery_catalog_size=body.get(
                    '_tool_search_catalog_size'),
                searchable_count=body.get('_tool_searchable_count'),
                include_search=True,
                schema_budget_tokens=_schema_budget,
                model=body.get('model') or '',
                priority_names=_pin_set,
                required_names=_required_direct_names,
                # PTC and multi-agent projection still follow. Reserve any
                # needed discovery gateway now, but fit exactly once below.
                apply_schema_budget=False,
                on_tool_isolated=_request_activity_sink)
        else:
            # Native discovery receives the complete catalog and lets the
            # provider defer searchable tools.  ``off`` also gets the full
            # catalog, without a discovery primitive.
            body['tools'] = full_wire_tools(_wire_catalog)
        if _tool_search_backend == 'native_anthropic':
            body['_anthropic_native_tool_search'] = True
        body['_frontend_selected_tool_names'] = _pins

    # Programmatic Tool Calling: resolve the per-request backend at the same
    # last common boundary (mirrors the Tool Search dual-backend precedent).
    # ``off`` leaves the wire untouched; ``native_openai`` lets the Responses
    # converter attach the hosted-only fields; ``local`` projects the
    # JSON batch gateway by default. Explicit opt-in adds JavaScript. Hosted
    # protocol support never proves proficiency on a different fallback route.
    from lib.tools.programmatic import (
        ACTIVE_PROGRAMMATIC_MODES,
        local_programmatic_tool_names,
        programmatic_tier,
        resolve_programmatic_backend,
    )
    _requested_ptc = str(
        body.get('_programmatic_tool_calling') or 'off').strip().lower()
    _local_ptc_present = bool(local_programmatic_tool_names(
        _executable_catalog if isinstance(_executable_catalog, list)
        else body.get('tools')))
    if (body.get('_force_local_programmatic')
            and _requested_ptc in ACTIVE_PROGRAMMATIC_MODES
            and _local_ptc_present):
        # Nested local-Swarm workers and a retry after an explicit native-field
        # rejection keep the program inside the application. This preserves
        # one authority/catalog and avoids nesting provider continuations.
        _ptc_backend = 'local'
    else:
        _ptc_backend = resolve_programmatic_backend(
            _requested_ptc,
            protocol=api_protocol, model=body.get('model') or '',
            responses_profile=_responses_feature_profile,
            base_url=base_url or '', oauth=oauth or '',
            eligible_present=bool(body.get('_programmatic_eligible_tools')),
            local_present=_local_ptc_present,
            inference_profile=_route_inference_profile)
    body['_resolved_programmatic_backend'] = _ptc_backend
    _orchestration_sink = body.get('_tool_orchestration_decision_sink')
    _programmatic_exposure = str(
        body.get('_programmatic_exposure') or 'additive').strip().lower()
    _record_orchestration_v2_evidence = (
        isinstance(_orchestration_sink, dict)
        and body.get('_tool_orchestration_policy_version')
        == 'tool-orchestration/v2')
    if isinstance(_orchestration_sink, dict):
        _orchestration_sink['programmaticBackend'] = _ptc_backend
        if '_programmatic_exposure' in body:
            _orchestration_sink['programmaticExposure'] = (
                _programmatic_exposure)
            _visible_names = {
                str((tool.get('function') or {}).get('name') or '')
                for tool in (body.get('tools') or ())
                if isinstance(tool, dict)
                and isinstance(tool.get('function'), dict)
            }
            _orchestration_sink['programmaticHiddenDirectToolCount'] = (
                len(_visible_names - {'execute_tools'} - BACKGROUND_TOOL_NAMES)
                if (_ptc_backend == 'local'
                    and _programmatic_exposure == 'gateway_only')
                else 0)
        if _record_orchestration_v2_evidence and _ptc_backend != 'off':
            from lib.orchestration.adoption import (
                record_orchestration_projection)
            record_orchestration_projection(
                _orchestration_sink,
                lane='programmatic', backend=_ptc_backend)
    if _ptc_backend == 'local' and isinstance(body.get('tools'), list):
        from lib.tools.gateway import ptc_local_wire_tools
        _local_tier = programmatic_tier(requested=_requested_ptc)
        if body.get('_programmatic_tier') == 'batch':
            _local_tier = 'batch'
        body['_programmatic_tier'] = _local_tier
        if isinstance(_orchestration_sink, dict):
            _orchestration_sink['programmaticTier'] = _local_tier
        body['tools'] = ptc_local_wire_tools(
            body['tools'], tier=_local_tier,
            eligible=body.get('_programmatic_eligible_tools') or (),
            exposure=_programmatic_exposure,
            projection_evidence=_orchestration_sink,
            required_direct_names=_required_direct_names,
            model=body.get('model') or '')
        if isinstance(_orchestration_sink, dict) and _programmatic_exposure == 'code_api':
            _after_names = {str((tool.get('function') or {}).get('name') or '')
                            for tool in body['tools'] if isinstance(tool, dict)}
            _orchestration_sink['programmaticHiddenDirectToolCount'] = len(
                _visible_names - _after_names - BACKGROUND_TOOL_NAMES - {'execute_program', 'execute_tools', 'search_tools'})

    # Multi-agent is an independent control plane.  Resolve it after PTC so
    # both lanes may be active in one request: local models see execute_tools
    # plus a read-only spawn_agents projection, while public GPT-5.6 Responses
    # receives the two native extensions together.  Native projection removes
    # spawn_agents from the visible surface to avoid offering two competing
    # control planes; execution authority remains task-owned and unchanged.
    from lib.swarm.routing import (
        ACTIVE_MULTI_AGENT_MODES,
        catalog_has_spawn_agents,
        project_multi_agent_wire_tools,
        resolve_multi_agent_backend,
    )
    _multi_agent_authority_catalog = (
        _executable_catalog if isinstance(_executable_catalog, list)
        else body.get('tools') if isinstance(body.get('tools'), list)
        else [])
    _local_swarm_available = catalog_has_spawn_agents(
        _multi_agent_authority_catalog)
    if (body.get('_force_local_multi_agent')
            and str(body.get('_multi_agent_mode') or '').lower()
            in ACTIVE_MULTI_AGENT_MODES):
        _multi_agent_backend = (
            'local_swarm' if _local_swarm_available else 'off')
    else:
        _multi_agent_backend = resolve_multi_agent_backend(
            body.get('_multi_agent_mode') or 'off',
            protocol=api_protocol, model=body.get('model') or '',
            responses_profile=_responses_feature_profile,
            base_url=base_url or '', oauth=oauth or '',
            local_swarm_available=_local_swarm_available,
            inference_profile=_route_inference_profile)
    body['_resolved_multi_agent_backend'] = _multi_agent_backend
    if isinstance(_orchestration_sink, dict):
        _orchestration_sink['multiAgentBackend'] = _multi_agent_backend
        if (_record_orchestration_v2_evidence
                and _multi_agent_backend != 'off'):
            from lib.orchestration.adoption import (
                record_orchestration_projection)
            record_orchestration_projection(
                _orchestration_sink,
                lane='multi_agent', backend=_multi_agent_backend)
    if isinstance(body.get('tools'), list):
        body['tools'] = project_multi_agent_wire_tools(
            body['tools'], authority_catalog=_multi_agent_authority_catalog,
            backend=_multi_agent_backend,
            max_concurrent_agents=(body.get(
                '_multi_agent_max_concurrent_agents')
                or body.get('_multi_agent_max_concurrent_subagents') or 3),
            programmatic_workers=(
                _ptc_backend in ('local', 'native_openai')))

    # Recover lifecycle controls after every visibility projection, but never
    # re-grant a control removed from the live catalog (e.g. read-only kill).
    if isinstance(body.get('tools'), list) or _background_names:
        body['tools'] = preserve_background_wire_tools(
            body.get('tools'), authority_catalog=_background_authority)
    if isinstance(body.get('tools'), list) or _direct_names:
        body['tools'] = preserve_direct_wire_tools(
            body.get('tools'), authority_catalog=_background_authority,
            declared=_direct_declarations)

    # PTC and Multi-agent may append schemas after Tool Search projection. Fit
    # an explicit, model-neutral local cost target exactly once at this final
    # common projection boundary.
    _final_schema_budget = int(body.get('_tool_schema_budget_tokens') or 0)
    _budget_dropped_names: list[str] = []
    _budget_compacted_names: list[str] = []
    if (_final_schema_budget and _tool_search_backend == 'local'
            and isinstance(body.get('tools'), list)):
        from lib.tools.gateway import fit_tool_schema_budget
        _before_budget = {
            _provider_tool_name(tool): tool for tool in body['tools']
            if _provider_tool_name(tool)
        }
        _priority_names = set(body.get('_frontend_selected_tool_names') or ())
        _required_names = set(_required_direct_names)
        if _multi_agent_backend != 'off':
            # Keep the local swarm launch/result pair together. Unified
            # poll/kill are already a mandatory budget floor when authorized;
            # never pull the replay-only await_agents into a new wire surface.
            from lib.swarm.tools import (
                GET_AGENT_RESULT_TOOL, SPAWN_AGENTS_TOOL)
            _required_names.update(
                _provider_tool_name(tool)
                for tool in (SPAWN_AGENTS_TOOL, GET_AGENT_RESULT_TOOL))
        body['tools'] = fit_tool_schema_budget(
            body['tools'], budget_tokens=_final_schema_budget,
            model=body.get('model') or '', priority_names=_priority_names,
            required_names=_required_names,
            on_tool_isolated=_request_activity_sink)
        _after_budget = {
            _provider_tool_name(tool): tool for tool in body['tools']
            if _provider_tool_name(tool)
        }
        _budget_dropped_names = [
            name for name in _before_budget if name not in _after_budget]
        _budget_compacted_names = [
            name for name, schema in _before_budget.items()
            if name in _after_budget and _after_budget[name] != schema]

    # Wire-contract invariant at the LAST common boundary: whatever shape the
    # tools array arrived in (registry assembly, latched catalogs, headless
    # custom tools, rescue re-dispatch), every element that reaches a provider
    # is an object with a usable name, and function tools carry
    # ``type='function'``.  A single ``None`` used to crash
    # ``add_cache_breakpoints`` (2026-08-19 task c9aba5d0 FATAL) and 400
    # Gemini ("'tools' array element to be an object"); a missing ``type``
    # 400s kimi ("unknown tool type: ").  Clean arrays pass through by
    # identity, so the prompt-cache hot path is untouched.
    if isinstance(body.get('tools'), list):
        from lib.tools.gateway import preflight_wire_tool_body
        preflight_wire_tool_body(
            body, log_prefix=log_prefix,
            on_tool_isolated=_request_activity_sink)

    return ToolWireProjection(_budget_compacted_names, _budget_dropped_names, _executable_catalog, _final_schema_budget, _multi_agent_backend, _ptc_backend, _tool_search_backend)
