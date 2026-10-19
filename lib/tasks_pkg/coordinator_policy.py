"""Dispatch-only Coordinator policy shared by catalog and execution admission.

The owner-scoped project binding selects the role, never a model/request flag.
A selected turn remains restricted if the binding changes mid-turn. Independent
worker conversations use ordinary task admission; Swarm children are not the
Coordinator execution route.
"""
from __future__ import annotations


COORDINATOR_TOOLS = frozenset({
    'ask_human', 'todo_write', 'list_conversations', 'get_conversation',
    'create_conversation', 'dispatch_project_assignment', 'get_project_assignment', 'report_to_user',
    'delegate_project_work', 'await_project_workers', 'review_project_work',
    'get_project_workers', 'cancel_project_work', 'finish_project_request',
    'send_project_message', 'get_project_messages', 'consider_project_message',
    'search_tools', 'execute_tools',
})


def coordinator_scope(task: dict, project_path: str | None = None) -> dict | None:
    """Resolve the authoritative binding, pinning restrictions for this turn.

    Lookup errors deliberately propagate: callers must fail closed, not silently
    restore general-purpose tools. Child invocations are not independent worker
    conversations and never inherit this conversational role.
    """
    from lib.tasks_pkg.invocation import is_child_invocation
    if (is_child_invocation(task) or task.get('_parentTaskId')
            or task.get('_transientRuntime') or task.get('_invocationStorageFree')
            or (task.get('config') or {}).get('_storageFreeRuntime')):
        return None
    from lib.project_dispatch.admission import is_dispatch_task
    if is_dispatch_task(task):
        from lib.project_dispatch.continuation import current_continuation
        row = current_continuation(task)
        if row is None:
            return None
        return {'project_path': row['request']['projectPath'],
                'binding_revision': row['grant']['bindingRevision'],
                'conversation_id': row['request']['coordinatorId'], 'user_id': row['grant']['owner']}
    pinned = task.get('_coordinatorDispatchScope')
    if pinned:
        return dict(pinned)
    config = task.get('config') or {}
    path = config.get('canonicalProjectPath') or project_path or config.get('projectPath')
    if not path or not task.get('convId'):
        return None
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.conversations.project_identity import normalize_project_path
    from lib.tasks_pkg.manager import task_user_id
    owner = task_user_id(task)
    path = normalize_project_path(path)
    binding = ProjectCoordinationRepository(owner).read(path)
    if (binding.get('availability') != 'available'
            or binding.get('conversationId') != task['convId']):
        return None
    result = {'project_path': path, 'binding_revision': binding['revision'],
              'conversation_id': task['convId'], 'user_id': owner}
    task['_coordinatorDispatchScope'] = result
    return dict(result)


def coordinator_call_rejection(task: dict, name: str, arguments: dict,
                               project_path: str | None = None) -> str | None:
    """Return a denial before approval, prefetch, hooks, or tool execution."""
    try:
        active = coordinator_scope(task, project_path)
    except Exception:
        return 'Coordinator policy unavailable; tool execution refused (fail closed).'
    if not active:
        return None
    if name in COORDINATOR_TOOLS:
        # Batch transport is allowed, arbitrary program execution is not. Every
        # child still crosses the same pipeline/executor admission boundary.
        if name != 'execute_tools':
            return None
        calls = arguments.get('calls')
        if (isinstance(calls, list) and calls and not arguments.get('program')
                and all(isinstance(call, dict)
                        and call.get('name') not in {'execute_tools', 'search_tools'}
                        and call.get('name') in COORDINATOR_TOOLS
                        for call in calls)):
            return None
    return (f'Coordinator dispatch-only policy: {name} is not allowed. '
            'Delegate the current human request with delegate_project_work, '
            'wait for independent worker results with await_project_workers, '
            'and follow evidence with get_project_assignment '
            'or get_conversation. Do not execute file, shell, browser, program, '
            'or temporary Swarm work in the Coordinator.')


def filter_coordinator_tools(task: dict, tools: list[dict] | None,
                             project_path: str | None = None) -> list[dict] | None:
    """Project the same policy before provider discovery can expose tools."""
    if not tools:
        return tools
    if not coordinator_scope(task, project_path):
        return tools
    return [tool for tool in tools
            if (tool.get('function') or {}).get('name') in COORDINATOR_TOOLS]


def prepare_coordinator_tool_surface(task: dict, cfg: dict, tools: list[dict] | None,
                                     project_path: str | None = None) -> list[dict] | None:
    """Keep this role's admitted tools direct, independent of intent/discovery.

    Call after ordinary registry, owner, explicit selection and Plan filters.
    The existing executable catalog is the ceiling: this never builds schemas
    or restores a denied capability. Continuation grant filtering still runs
    afterward and every tool call retains its ordinary execution gates.
    """
    if not coordinator_scope(task, project_path):
        return tools
    catalog = cfg.get('_executableToolCatalog')
    if not isinstance(catalog, list):
        catalog = tools or []
    admitted = [tool for tool in catalog
                if (tool.get('function') or {}).get('name') in COORDINATOR_TOOLS]
    allowed = {tool['function']['name'] for tool in admitted}
    cfg['_executableToolCatalog'] = admitted
    for key in ('_toolContractDocumentsByName', '_executableToolNamespaceByName',
                '_toolScriptSafeByName', '_executableToolSearchTextByName'):
        cfg[key] = {name: value for name, value in (cfg.get(key) or {}).items()
                    if name in allowed}
    cfg['_toolNamespaceByName'] = dict(cfg['_executableToolNamespaceByName'])
    cfg['_frontendSelectedToolNames'] = sorted(
        set(cfg.get('_frontendSelectedToolNames') or ()) & allowed)
    # Shared direct-wire projection protects these exact live schemas across
    # local/native discovery, schema budgets and replayed initial visibility.
    # Do not mark entire ToolSpec families eager: project_integration also owns
    # workspace mutation tools that the Coordinator must never acquire.
    cfg['_requiredDirectToolNames'] = sorted(allowed)
    cfg['_toolDiscoveryPolicyByName'] = {name: 'eager' for name in allowed}
    cfg['_toolSearchCatalogSize'] = len(admitted)
    cfg['_toolSearchableCount'] = 0
    return list(admitted)
