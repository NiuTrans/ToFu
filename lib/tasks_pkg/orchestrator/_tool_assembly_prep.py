"""Shared task tool preparation for chat, coordinators and child invocations.

Role adapters supply capability/discovery policy. assemble_round_tools binds
execution contracts and prepares the startup admission schema through the same
gateway projector for every role. Returned candidates remain available to the
final provider projection; discovery never changes execution authority.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.tasks_pkg.model_config import _assemble_tool_list


logger = get_logger(__name__)


def _disable_tools_for_turn(task) -> None:
    """Clear every tool authority/projection field for a text-only turn."""

    task.pop('_rootToolAssemblyConfig', None)
    task.pop('_rootMcpToolNames', None)
    task['_tool_gateway_names'] = []
    task['_tool_schema'] = []
    task['_toolInitialVisibleNames'] = []
    task['_executable_tool_catalog'] = []
    task['_requiredDirectToolNames'] = []
    task['_toolExecutionScope'] = 'available'
    task['_toolDiscoveryPolicyByName'] = {}
    task['_toolSearchCatalogSize'] = 0
    task['_toolSearchableCount'] = 0
    task['_toolSearchMode'] = 'off'
    task['_toolScriptSafeByName'] = {}
    task['_executableToolNamespaceByName'] = {}
    task['_executableToolSearchTextByName'] = {}
    task['_toolContractDocumentsByName'] = {}
    task['_frontendSelectedToolNames'] = []
    task['_toolNamespaceByName'] = {}
    task.pop('_toolExposureTelemetry', None)


def attach_gateway_contract_documents(task, search_mode: str) -> bool:
    """Add contracts for the stable local gateway tools added after config.

    Registry-backed documents are assembled in model config. The local
    ``search_tools`` / ``execute_tools`` schemas are injected later at the
    provider boundary, so their execution contracts must be compiled at the
    same boundary or validation correctly fails closed on every gateway call.
    Returns whether the gateway documents were attached; ``False`` tells the
    caller to continue this otherwise valid turn without any tools.
    """
    from lib.tools.contracts import compile_execution_contract_documents
    from lib.tools.gateway import (
        gateway_tool_schemas,
    )

    from lib.tools.programmatic import ACTIVE_PROGRAMMATIC_MODES, programmatic_tier
    from lib.context_experiment_flags import normalize_context_experiment_flags
    # Compatibility clients use config.tools for schema lists. The common
    # normalizer separates those declarations from request policy settings.
    settings = normalize_context_experiment_flags(task.get('config'))['tools']
    mode = settings['programmaticCalling']
    include_program = (mode in ACTIVE_PROGRAMMATIC_MODES
                       and programmatic_tier(requested=mode) == 'program')

    from lib.tasks_pkg.coordinator_policy import coordinator_scope
    if coordinator_scope(task):
        include_program = False
    schemas = gateway_tool_schemas(
        include_search=search_mode != 'off', include_execute=True,
        include_program_action=include_program)
    task['_tool_gateway_names'] = [schema['function']['name'] for schema in schemas]

    try:
        gateway_documents = compile_execution_contract_documents(
            schemas,
            namespace='gateway',
        )
    except Exception as error:
        # These documents and schemas are derived request metadata, not the
        # authority itself. Never advertise an unvalidated gateway, but do not
        # abort an otherwise valid text turn either: the caller disables every
        # tool for this turn, which is fail-closed for side effects and
        # fail-soft for the main orchestration flow.
        task['_tool_gateway_names'] = []
        logger.error(
            '[ToolAssembly] gateway contract compilation failed; disabling '
            'tools for this turn and continuing text-only: %s',
            error, exc_info=True)
        return False
    task.setdefault('_toolContractDocumentsByName', {}).update(gateway_documents)
    return True


def assemble_round_tools(cfg, task, mcfg):
    """Assemble this turn's tool schema.

    ``cfg`` / ``task`` / ``mcfg`` are positional carriers (mcfg is the
    resolved model-config dict from Section 1). Returns the 2-tuple
    ``(tool_list, has_real_tools)``. Wire visibility remains stable for the
    entire turn. Root MCP authority refreshes before each request within the
    initial capability ceiling, independently of the frozen declarations.
    """
    from lib.project_dispatch.admission import is_dispatch_task, validate_task, repository_for, profile_for
    if is_dispatch_task(task):
        binding = validate_task(task)
        profile = profile_for(repository_for(binding).read(binding['assignmentId']))
        if profile['mode'] not in ('project', 'coordinator'):
            _disable_tools_for_turn(task)
            if profile['mode'] == 'consult':
                return [], False
            from lib.tools.registry import project_dispatch_tool_schemas
            from lib.tools.contracts import compile_execution_contract_documents
            schemas = project_dispatch_tool_schemas(profile['tools'])
            allowed_fields = {'read_files': {'path', 'start_line', 'end_line', 'reads'},
                              'write_file': {'path', 'content', 'description', 'append'},
                              'edit_file': {'description', 'edits'}}
            for tool in schemas:
                name = tool['function']['name']
                parameters = tool['function']['parameters']
                parameters['properties'] = {key: value for key, value in parameters['properties'].items()
                                            if key in allowed_fields[name]}
                parameters['additionalProperties'] = False
                for key in ('oneOf', 'anyOf', 'allOf'):
                    parameters.pop(key, None)
                parameters['required'] = (['path', 'content'] if name == 'write_file' else
                                          ['edits'] if name == 'edit_file' else [])
                if name == 'read_files':
                    parameters['oneOf'] = [{'required': ['path']}, {'required': ['reads']}]
                tool['function']['description'] += (' Only explicit project-relative text files are authorized; '
                    'no aliases, converters, external roots or content refs. Read prefixes: '
                    + ', '.join(profile['readPaths']) + '; write prefixes: ' + ', '.join(profile['writePaths']))
            documents = compile_execution_contract_documents(schemas, namespace='project')
            task['_tool_schema'] = schemas
            task['_executable_tool_catalog'] = schemas
            task['_requiredDirectToolNames'] = [tool['function']['name'] for tool in schemas]
            task['_toolContractDocumentsByName'] = documents
            task['_executableToolNamespaceByName'] = {name: 'project' for name in profile['tools']}
            return schemas, bool(schemas)
    from lib.tasks_pkg.invocation import is_child_invocation
    if is_child_invocation(task):
        # The scheduler already intersected role and parent capabilities.
        # Empty means no authority, never auto-assemble the root inventory.
        from lib.tools.contracts import compile_execution_contract_documents
        tools = list(task['_invocationTools'])
        _disable_tools_for_turn(task)
        conversation_policy = task.get('_invocationConversationToolPolicy')
        if conversation_policy is not None:
            return _apply_assembled_tool_policy(
                {**cfg, **conversation_policy}, task, tools, bool(tools))
        if not tools:
            return [], False
        contracts = compile_execution_contract_documents(
            tools, authoritative_documents_by_name=task.get('_invocationToolContracts'),
            namespace='invocation')
        discovery = task.get('_invocationToolDiscoveryPolicy') or {}
        return _apply_assembled_tool_policy({**cfg, **discovery,
            '_executableToolCatalog': tools,
            '_toolContractDocumentsByName': contracts,
            '_toolScriptSafeByName': task.get('_invocationToolScriptSafeByName') or {},
        }, task, tools, bool(tools))

    try:
        from lib.tasks_pkg.coordinator_policy import coordinator_scope
        coordinator = coordinator_scope(task, mcfg['project_path'])
        tool_list, has_real_tools = _assemble_tool_list(
            cfg, mcfg['project_path'], mcfg['project_enabled'], task['id'],
            mcfg['search_mode'], mcfg['search_enabled'],
            mcfg['fetch_enabled'],
            mcfg['code_exec_enabled'], mcfg['browser_enabled'],
            mcfg['desktop_enabled'],
            image_gen_enabled=mcfg['image_gen_enabled'],
            human_guidance_enabled=mcfg.get('human_guidance_enabled', False),
            scheduler_enabled=mcfg.get('scheduler_enabled', False),
            messages=task['messages'],
            # Headless messages carry a client job label, not a durable
            # conversation declaration authority. Preserve task-only MCP
            # selection even when that label happens to name a stored row.
            conv_id=('' if task.get('_inline_messages') or task.get('_transientRuntime')
                     else task.get('convId', '')),
        )
        if coordinator:
            from lib.project_dispatch.delegation import capture_worker_policy
            capture_worker_policy(task, cfg, cfg.get('_executableToolCatalog') or tool_list, mcfg['model'])
            from lib.tasks_pkg.coordinator_policy import prepare_coordinator_tool_surface
            from lib.context_experiment_flags import normalize_context_experiment_flags
            cfg['tools'] = {**normalize_context_experiment_flags(cfg)['tools'],
                            'programmaticCalling': 'off'}
            tool_list = prepare_coordinator_tool_surface(task, cfg, tool_list, mcfg['project_path'])
            has_real_tools = bool(tool_list)
    except Exception as error:
        from lib.agent_core.tool_errors import MCPDeclarationError
        from lib.tools.registry import ToolSpecSelectionError
        if isinstance(error, (MCPDeclarationError, ToolSpecSelectionError)):
            # Durable identity and explicit task policy are not schema caches.
            # Text-only would silently replace the requested execution contract.
            _disable_tools_for_turn(task)
            raise
        # Other schemas/lookup metadata are a derived projection. A broken
        # registry/plugin must never gain execution
        # authority, but it also need not prevent the model from answering in
        # plain text. Clear every task-owned authority field because assembly
        # may have failed after producing only part of the projection.
        logger.error(
            '[ToolAssembly] registry assembly failed; disabling tools for '
            'this turn and continuing text-only: %s',
            error, exc_info=True)
        _disable_tools_for_turn(task)
        return [], False

    if is_dispatch_task(task):
        allowed = set(profile['tools'])
        tool_list = [item for item in tool_list if item['function']['name'] in allowed]
        cfg['_executableToolCatalog'] = [item for item in cfg.get('_executableToolCatalog', [])
                                         if item['function']['name'] in allowed]
        for key in ('_toolContractDocumentsByName', '_toolNamespaceByName',
                    '_executableToolNamespaceByName', '_toolDiscoveryPolicyByName',
                    '_toolScriptSafeByName', '_executableToolSearchTextByName'):
            cfg[key] = {name: value for name, value in (cfg.get(key) or {}).items() if name in allowed}
        cfg['_requiredDirectToolNames'] = [name for name in cfg.get('_requiredDirectToolNames', []) if name in allowed]
        has_real_tools = bool(tool_list)
    result = _apply_assembled_tool_policy(cfg, task, tool_list, has_real_tools)

    if not result[1]:
        task.pop('_rootToolAssemblyConfig', None)
        task.pop('_rootMcpToolNames', None)
        return result
    task['_rootToolAssemblyConfig'] = dict(mcfg)

    task['_rootMcpToolNames'] = sorted({
        str((tool.get('function') or {}).get('name') or '')
        for tool in [*(tool_list or []),
                     *(task.get('_executable_tool_catalog') or [])]
        if str((tool.get('function') or {}).get('name') or '').startswith('mcp__')})
    return result


def refresh_root_mcp_authority(task):
    """Refresh live grants for the root's existing MCP surface, never its wire.

    Startup can precede asynchronous MCP connection. Reuse ordinary assembly
    on detached carriers so plan/selection/owner policy still decides grants.
    Child authority is an immutable parent intersection, not a root inventory.
    """
    from lib.tasks_pkg.invocation import is_child_invocation
    from lib.project_dispatch.admission import is_dispatch_task
    mcfg = task.get('_rootToolAssemblyConfig')
    if not isinstance(mcfg, dict) or is_child_invocation(task) or is_dispatch_task(task):
        return

    def name(tool):
        return str((tool.get('function') or {}).get('name') or '')

    original = task.get('_executable_tool_catalog') or []
    eligible = set(task.get('_rootMcpToolNames') or [])
    if not eligible:
        return
    cfg = dict(task.get('config') or {})
    shadow = dict(task, config=cfg)
    assemble_round_tools(cfg, shadow, mcfg)
    refreshed = [tool for tool in shadow.get('_executable_tool_catalog', [])
                 if name(tool) in eligible]
    live_names = {name(tool) for tool in refreshed}
    # Publish the complete catalog + associated policy projection before any
    # request/streaming ingestion begins. Nothing below adds wire declarations.
    updates = {'_executable_tool_catalog': [
        tool for tool in original if not name(tool).startswith('mcp__')] + refreshed}
    for key in ('_toolContractDocumentsByName', '_executableToolNamespaceByName',
                '_executableToolSearchTextByName', '_toolScriptSafeByName'):
        updates[key] = {key_name: value for key_name, value in (task.get(key) or {}).items()
                        if not key_name.startswith('mcp__')}
        updates[key].update({key_name: value for key_name, value in (shadow.get(key) or {}).items()
                             if key_name in live_names})
    # Discovery policies/counts belong to the frozen provider surface. Removing
    # an unavailable tool's searchable policy would default it to eager on wire.
    # Search/execution still intersect these hints with the live catalog.
    task.update(updates)


def _apply_assembled_tool_policy(cfg, task, tool_list, has_real_tools):
    """Prepare one ordinary authority/gateway projection from assembled facts."""
    _executable_catalog = cfg.get('_executableToolCatalog')
    task['_executable_tool_catalog'] = list(
        tool_list or []
        if not isinstance(_executable_catalog, list)
        else _executable_catalog)
    task['_toolExecutionScope'] = str(
        cfg.get('_toolExecutionScope') or 'available')
    task['_toolDiscoveryPolicyByName'] = dict(
        cfg.get('_toolDiscoveryPolicyByName') or {})
    task['_requiredDirectToolNames'] = list(
        cfg.get('_requiredDirectToolNames') or [])
    _catalog_size = cfg.get('_toolSearchCatalogSize')
    task['_toolSearchCatalogSize'] = int(
        len(task['_executable_tool_catalog'])
        if _catalog_size is None else _catalog_size)
    task['_toolSearchableCount'] = int(
        cfg.get('_toolSearchableCount') or 0)
    task['_toolSearchMode'] = str(cfg.get('_toolSearchMode') or '')
    task['_toolScriptSafeByName'] = dict(
        cfg.get('_toolScriptSafeByName') or {})
    task['_executableToolNamespaceByName'] = dict(
        cfg.get('_executableToolNamespaceByName') or
        cfg.get('_toolNamespaceByName') or {})
    task['_executableToolSearchTextByName'] = dict(
        cfg.get('_executableToolSearchTextByName') or {})
    task['_toolContractDocumentsByName'] = dict(
        cfg.get('_toolContractDocumentsByName') or {})
    from lib.context_experiment_flags import normalize_context_experiment_flags
    _search_mode = (task['_toolSearchMode']
                    or normalize_context_experiment_flags(
                        cfg)['tools']['toolSearch'])
    # Provider-boundary strategy decides whether the stable gateway schema is
    # visible. Admission remains task-owned: every nested name is resolved
    # against the immutable executable catalog and enters the ordinary
    # approval / hook / executor pipeline. A derived contract defect must not
    # advertise a gateway that rejects every call, but it also must not abort
    # an otherwise valid text turn. Disable all tools for this turn: closed for
    # side effects, soft for the main orchestration flow.
    if not attach_gateway_contract_documents(task, _search_mode):
        _disable_tools_for_turn(task)
        return [], False
    if isinstance(cfg.get('_toolExposureTelemetry'), dict):
        task['_toolExposureTelemetry'] = dict(cfg['_toolExposureTelemetry'])
    task['_frontendSelectedToolNames'] = list(
        cfg.get('_frontendSelectedToolNames') or [])
    task['_toolNamespaceByName'] = dict(
        cfg.get('_toolNamespaceByName') or {})
    tool_list = _prepare_initial_tool_surface(cfg, task, tool_list, _search_mode)
    return tool_list, has_real_tools


def _prepare_initial_tool_surface(cfg, task, tool_list, search_mode):
    """Keep provider candidates separate from the pre-route admission estimate.

    Auto discovery can compact locally before a provider has been selected.
    Retaining the candidates lets the final shared wire boundary choose native
    discovery instead, where admission checks the actual routed request again.
    """
    from lib.context_experiment_flags import normalize_context_experiment_flags
    from lib.llm.tool_wire import provider_tool_name
    from lib.tools.gateway import local_wire_tools

    authority = task['_executable_tool_catalog']
    authority_names = {provider_tool_name(tool) for tool in authority}
    required = set(task['_requiredDirectToolNames']) & authority_names
    pins = set(task['_frontendSelectedToolNames']) & authority_names
    initial = cfg.get('_toolInitialVisibleNames')
    if initial is not None:
        allowed = set(initial) | required | pins
        tool_list = [tool for tool in (tool_list or []) if provider_tool_name(tool) in allowed]
    visible_names = {provider_tool_name(tool) for tool in (tool_list or [])}
    missing = (required | pins) - visible_names
    if missing:
        tool_list = list(tool_list or []) + [tool for tool in authority if provider_tool_name(tool) in missing]
        visible_names |= missing
    task['_toolInitialVisibleNames'] = sorted(visible_names)
    policies = {**task['_toolDiscoveryPolicyByName'],
                **{name: 'eager' for name in required | pins}}
    # Bind the same effective policy for admission and final wire projection;
    # otherwise a selected searchable tool can disappear on the second pass.
    task['_toolDiscoveryPolicyByName'] = policies
    task['_toolSearchableCount'] = max(task['_toolSearchableCount'], sum(
        policies.get(provider_tool_name(tool)) == 'searchable'
        or provider_tool_name(tool) not in visible_names for tool in authority))
    task['_toolSearchMode'] = search_mode
    task['_tool_schema'] = tool_list
    if authority and search_mode in ('auto', 'local'):
        flags = normalize_context_experiment_flags(cfg)['tools']
        task['_tool_schema'] = local_wire_tools(
            tool_list, discovery_catalog_size=len(authority),
            searchable_count=task['_toolSearchableCount'],
            discovery_policy_by_name=policies,
            schema_budget_tokens=flags['schemaBudgetTokens'], model=cfg.get('model') or '',
            priority_names=pins, required_names=required)
    return tool_list
