"""Child invocation construction, not an alternate agent runtime.

Schedulers supply a parent, a scoped catalog and an ordered event port. Execution,
model preparation, tools, checkpoints and terminal evidence belong to invocation.
This module owns only input policy, borrowed resources and result delivery.
"""
from __future__ import annotations

import copy
from collections.abc import Callable
import threading as threading
from lib.agent_core.publication import (
    ChildPublicationFence, commit_child_publication,
)
from lib.tasks_pkg.invocation import InvocationPolicy, execute_invocation


def close_child_ingress(child):
    """Drain once with a deadline, revoke publication, detach queued closures."""
    from lib.tasks_pkg.manager._provider_ingress_guard import (
        active_provider_ingress_token, end_provider_ingress,
        detach_child_ingress,
    )
    token = active_provider_ingress_token(child)
    if token is not None:
        end_provider_ingress(child, token=token, disposing=True)
    fence = child.get('_invocationPublicationFence')
    if fence is not None:
        fence.close()
    detach_child_ingress(child)

    child['_invocationPublicationClosed'] = True

# Resolved environment/policy only: never inherit Turn/attempt IDs, checkpoint
# state, route-group disposal, kickoff, or a parent's mutable execution buffers.
_INHERITED_CONFIG = frozenset({
    'projectPath', 'projectPaths', 'readOnlyPaths', 'canonicalProjectPath', 'canonicalProjectPaths',
    'workspaceRuntimeRoots', 'project_remote', 'browserClientId',
    'runtimeScope', 'systemPromptMode', 'systemPromptBlocks',
    'contextExperiments', 'contextPolicy', 'grantedToolPermissions',
    'memoryEnabled', 'searchMode', 'fetchEnabled', 'codeExecEnabled',
    'browserEnabled', 'desktopEnabled', 'imageGenEnabled', 'schedulerEnabled',
    'thinkingEnabled', 'thinkingDepth', 'defaultThinkingDepth', 'maxTokens', 'temperature',
    'enabledPlugins', 'toolApprovals', 'toolPermissions', 'planMode',
    'workspace', 'workspacePolicy', 'compaction', 'responseFormat', '_toolOrchestration',
    'disableModelFallback', '_pinned_provider_id',
})


def inherit_child_config(parent_config: dict, overrides: dict | None = None) -> dict:
    """Copy declared input policy; never borrow runtime state or invent defaults."""
    aliases = {'thinking_enabled': 'thinkingEnabled', 'thinking_depth': 'thinkingDepth',
               'max_tokens': 'maxTokens', 'search_mode': 'searchMode'}
    config = {}
    for source in (parent_config, overrides or {}):
        config.update({key: copy.deepcopy(value) for key, value in source.items()
                       if key in _INHERITED_CONFIG})
        config.update({product: copy.deepcopy(source[key]) for key, product in aliases.items()
                       if key in source})
    return config


def build_child_messages(*, objective: str, context: str = '', system_prompt: str = '',
                         role_policy: str = '', artifact_hint: str = '',
                         checkpoint_messages: list | None = None,
                         swarm_message_binding: dict | None = None) -> list:
    """Retain dialogue and exact current-run input, never stale mailbox authority.

    The scheduler must supply the same binding during both checkpoint rebuild
    and task allocation. Its liveness/scope is revalidated at provider dispatch.
    """
    from lib.tasks_pkg.context_composer import append_injection
    fresh = []
    if system_prompt.strip():
        fresh.append({'role': 'system', 'content': system_prompt})
    append_injection(fresh, 'worker_policy', (
        f'{role_policy}\n\nWork only on the delegated objective. The supplied '
        'tool catalog is your authority; never emulate missing privileged tools. '
        'You share a workspace: preserve changes made by the user and other agents. '
        'You cannot ask the human or delegate further; report missing capabilities '
        'in your result. Return findings, evidence, artifact paths and remaining work.'))
    if checkpoint_messages is not None:
        if (not isinstance(checkpoint_messages, list) or not checkpoint_messages
                or any(not isinstance(message, dict) for message in checkpoint_messages)):
            raise ValueError('Child checkpoint requires nonempty message objects')
        mailbox = None
        if swarm_message_binding is not None:
            from lib.swarm.run_messages import RunMessageRepository
            mailbox = RunMessageRepository(swarm_message_binding).key
        dialogue = []
        for message in copy.deepcopy(checkpoint_messages):
            if message.get('_contextInjectionId') in {'worker_policy', 'worker_artifacts'}:
                continue
            if message.get('_projectMessageIds'):
                continue  # A child never owns its parent's project mailbox.
            if message.get('_swarmMessageIds'):
                if mailbox is None or message.get('_swarmMessageMailbox') != mailbox:
                    continue  # Old active steering is not a new assignment's context.
            else:
                message.pop('_swarmMessageMailbox', None)
                message.pop('_swarmMessageIds', None)
            message.pop('_projectMailbox', None)
            message.pop('_projectMessageIds', None)
            dialogue.append(message)
        if dialogue and dialogue[0].get('role') == 'system' and not dialogue[0].get('_contextInjectionId'):
            dialogue.pop(0)
        fresh.extend(dialogue)
    else:
        fresh.append({'role': 'user', 'content': objective + (
            '\n\n## Context\n' + context if context else '')})
    if artifact_hint:
        append_injection(fresh, 'worker_artifacts', artifact_hint)
    return fresh


# Derived by the ordinary launch assembly, never by model declarations. Keep
# tool visibility separate from execution authority when continuing a chat.
_CONVERSATION_TOOL_POLICY_FIELDS = frozenset({
    '_executableToolCatalog', '_toolExecutionScope', '_toolDiscoveryPolicyByName',
    '_toolSearchCatalogSize', '_toolSearchableCount', '_toolSearchMode',
    '_toolScriptSafeByName', '_executableToolNamespaceByName',
    '_executableToolSearchTextByName', '_toolContractDocumentsByName',
    '_toolExposureTelemetry', '_frontendSelectedToolNames', '_toolNamespaceByName',
})


def _conversation_tool_policy(parent: dict, tools: list) -> dict:
    from lib.swarm.routing import tool_schema_name
    source = parent.get('config') or {}
    policy = {key: copy.deepcopy(source[key])
              for key in _CONVERSATION_TOOL_POLICY_FIELDS if key in source}
    catalog = policy.get('_executableToolCatalog', tools)
    # Goal remains unattended: retaining context cannot grant human approval.
    policy['_executableToolCatalog'] = [copy.deepcopy(tool) for tool in catalog
                                       if tool_schema_name(tool) != 'ask_human']
    from lib.tools.contracts import compile_execution_contract_documents
    policy['_toolContractDocumentsByName'] = compile_execution_contract_documents(
        policy['_executableToolCatalog'], authoritative_documents_by_name=(
            policy.get('_toolContractDocumentsByName')
            or parent.get('_toolContractDocumentsByName')), namespace='invocation')
    policy['_toolSearchCatalogSize'] = len(policy['_executableToolCatalog'])
    return policy


def resolve_child_config(parent: dict, *, model: str, project_path: str = '',
                         thinking_enabled: bool | None = None) -> dict:
    """Resolve inherited model/input policy without allocating runtime resources.

    ModelRef is durable selection; native routed slots and execution carriers
    are deliberately absent. Normal child creation and Goal archives share this
    exact projection rather than interpreting parent configuration twice.
    """
    cfg = inherit_child_config(parent.get('config') or {})
    if thinking_enabled is not None:
        cfg['thinkingEnabled'] = thinking_enabled
    cfg.update(model=model, projectPath=project_path, humanGuidanceEnabled=False)
    # Native parent slots expire at turn settlement. Carry only the authorized
    # selection/policy; create_task mints a child-owned group and hard pin.
    parent_ref = (parent.get('_requestedModelRef') or
                  (parent.get('config') or {}).get('modelRef'))
    if parent_ref is None and parent.get('_pinned_provider_id'):
        # Legacy routes identify a durable provider directly. Native routes
        # below carry ModelRef instead and must never archive a slot-group pin.
        cfg.setdefault('_pinned_provider_id', parent['_pinned_provider_id'])
    if parent_ref is not None:
        parent_model = parent.get('model') or (parent.get('config') or {}).get('model')
        if model != parent_model:
            from lib.model_routing import ModelRoutingError
            raise ModelRoutingError(
                'Child model override is outside the parent route selection',
                kind='model_route_unavailable')
        cfg['modelRef'] = copy.deepcopy(parent_ref)
        for key in ('routing', 'preferredProviderId'):
            if key in (parent.get('config') or {}):
                cfg[key] = copy.deepcopy(parent['config'][key])
        cfg.pop('_pinned_provider_id', None)
    # An unresolved preset must never silently select another model at prep.
    cfg['preset'] = ''
    cfg['effort'] = ''
    return cfg


def snapshot_conversation_child_policy(parent: dict, *, model: str, tools: list,
                                       project_path: str = '',
                                       thinking_enabled: bool | None = None) -> dict:
    """Detach the ordinary conversation child's declared execution inputs.

    Never serialize principal handles, provider credentials, tool environment,
    locks or slot groups; recovery must rebind those through their owners.
    """
    return {
        'config': resolve_child_config(parent, model=model, project_path=project_path,
                                       thinking_enabled=thinking_enabled),
        'conversation_tool_policy': _conversation_tool_policy(parent, tools),
        'tool_contracts': copy.deepcopy(parent.get('_toolContractDocumentsByName') or {}),
        'tool_script_safe': copy.deepcopy(
            parent.get('_toolScriptSafeByName') or
            (parent.get('config') or {}).get('_toolScriptSafeByName') or {}),
    }


def create_child_invocation(
    parent: dict, *, model: str, tools: list, objective: str, context: str = '',
    system_prompt: str = '', role_policy: str = '', artifact_hint: str = '', project_path: str = '',
    thinking_enabled: bool | None = None, event_sink: Callable[[dict], None],
    abort_check: Callable[[], bool],
    conversation_messages: list[dict] | None = None,
    checkpoint_messages: list[dict] | None = None,
    swarm_message_binding: dict | None = None,
    checkpoint_sink: Callable[[dict, list, int], None] | None = None,
    checkpoint_result: dict | None = None,
    timeout_seconds: float = 0,
    max_429_attempts: int | None = None,
) -> dict:
    """Allocate a task-local carrier; borrow authority, never parent lifecycle."""
    from lib.identity import PrincipalContext
    from lib.tasks_pkg.manager import create_task, task_user_id
    task_user_id(parent)  # Validate the structured principal before allocation.
    if not parent.get('id') or not model:
        raise ValueError('Child invocation requires parent identity and resolved model')
    policy = InvocationPolicy.child(
        parent['id'], timeout_seconds=timeout_seconds, max_429_attempts=max_429_attempts)
    policy.validate()
    if not callable(event_sink) or not callable(abort_check) or not isinstance(tools, list):
        raise ValueError('Child invocation requires tools and callable control/evidence ports')
    frozen_policy = parent.get('_goalInvocationPolicy')
    if frozen_policy is not None:
        cfg = copy.deepcopy(frozen_policy['config'])
        if cfg.get('model') != model:
            from lib.model_routing import ModelRoutingError
            raise ModelRoutingError(
                'Child model conflicts with the archived Goal input policy',
                kind='model_route_unavailable')
        if (cfg.get('projectPath') != project_path
                or (thinking_enabled is not None
                    and cfg.get('thinkingEnabled') != thinking_enabled)):
            raise ValueError('Child launch conflicts with the archived Goal input policy')
    else:
        cfg = resolve_child_config(parent, model=model, project_path=project_path,
                                   thinking_enabled=thinking_enabled)

    # Resolve execution locations only after declared/frozen input authority.
    # Isolation changes paths, never model routing, tools or delivery ownership.
    from lib.tasks_pkg.workspace_delivery import (
        execution_repositories, ensure_execution_roots, route_project_tool_args)
    repositories = execution_repositories(parent)
    if repositories:
        selected_path = cfg.get('projectPath') or (parent.get('config') or {}).get('projectPath') or ''
        cfg['projectPath'] = route_project_tool_args(parent, 'run_command', {
            'working_dir': selected_path})['working_dir']
        for field in ('projectPaths', 'readOnlyPaths'):
            if field in cfg:
                cfg[field] = [route_project_tool_args(parent, 'read_files', {
                    'path': path})['path'] for path in cfg[field]]
    if conversation_messages is not None:
        if checkpoint_messages is not None:
            raise ValueError('Child input must have one message source')
        if not isinstance(conversation_messages, list) or not conversation_messages:
            raise ValueError('Conversation worker requires nonempty model messages')
        # Already API-ready, including native policy, media and tool pairs.
        # Never flatten it or append the same history as a delegated objective.
        messages = copy.deepcopy(conversation_messages)
        # Context borrowed from a parent is not proof of this child's mailbox
        # consumption. Keep native media/tool pairs and text, but revoke receipts.
        for message in messages:
            for key in ('_projectMessageIds', '_projectMailbox',
                        '_swarmMessageIds', '_swarmMessageMailbox'):
                message.pop(key, None)
    else:
        messages = build_child_messages(
            objective=objective, context=context, system_prompt=system_prompt,
            role_policy=role_policy, artifact_hint=artifact_hint, checkpoint_messages=checkpoint_messages,
            swarm_message_binding=swarm_message_binding)
    conversation_tool_policy = None
    if conversation_messages is not None:
        conversation_tool_policy = (
            copy.deepcopy(frozen_policy['conversation_tool_policy'])
            if frozen_policy is not None else _conversation_tool_policy(parent, tools))
    child = create_task(
        parent.get('convId') or '', messages, cfg,
        principal=PrincipalContext.from_payload(parent['_principalContext']),
        supersede=False, transient=True)
    fence = ChildPublicationFence()

    def fenced_sink(event):
        return fence.deliver(event_sink, event)

    child.update(
        _parentTaskId=parent['id'],
        _invocationTools=copy.deepcopy(tools),
        _invocationToolContracts=copy.deepcopy(
            frozen_policy['tool_contracts'] if frozen_policy is not None
            else parent.get('_toolContractDocumentsByName') or {}),
        _invocationToolScriptSafeByName=copy.deepcopy(
            frozen_policy['tool_script_safe'] if frozen_policy is not None else
            parent.get('_toolScriptSafeByName') or
            (parent.get('config') or {}).get('_toolScriptSafeByName') or {}),
        _invocationEventSink=fenced_sink,
        _invocationPublicationFence=fence,
        _invocationAbortCheck=abort_check,
        _invocationPolicy=policy,
        _invocationCheckpointSink=checkpoint_sink,
        _unattended=True,
        _tenant_id=parent.get('_tenant_id'),
        _pinned_provider_id=(child.get('_pinned_provider_id') or
                             (cfg.get('_pinned_provider_id') or ''
                              if frozen_policy is not None else
                              parent.get('_pinned_provider_id') or
                              (parent.get('config') or {}).get('_pinned_provider_id') or '')),
        _tool_env=parent.get('_tool_env'),
    )
    if swarm_message_binding is not None:
        child['_swarmMessageBinding'] = copy.deepcopy(swarm_message_binding)
    if checkpoint_result:
        child['_checkpointUsage'] = {key: checkpoint_result.get(key, 0) for key in (
            'prompt_tokens', 'completion_tokens', 'total_tokens', 'cost_usd')}
        child['toolCallCount'] = int(checkpoint_result.get('tool_calls_made') or 0)

        child['_executionToolsAdmitted'] = child['toolCallCount']

        child['usage'] = dict(child['_checkpointUsage'])
        child['_invocationCheckpointUsageMerged'] = True
        if checkpoint_result.get('actuals_complete') is False:
            child['_usageActualsIncomplete'] = True

            child['_executionActualsComplete'] = False
    if repositories:
        child['_workspaceExecution'] = {'repositories': copy.deepcopy(repositories)}
        child['_workspaceRootPolicy'] = copy.deepcopy(ensure_execution_roots(parent))
    if conversation_tool_policy is not None:
        child['_invocationConversationToolPolicy'] = conversation_tool_policy
    from lib.tasks_pkg.invocation_registration import register_child_invocation, storage_free_parent
    child['_invocationStorageFree'] = storage_free_parent(parent)
    child['_inspectorTaskId'] = f'{parent["id"]}#agent:{child["id"]}'

    from lib.agent_core.execution_budget import inherit_execution_budget,close_execution_budget
    try:
        inherit_execution_budget(parent, child)
        register_child_invocation(child, parent,discard_on_failure=False)
    except BaseException:
        from lib.tasks_pkg.manager import reject_unstarted_chat_task
        from lib.log import get_logger
        logger=get_logger(__name__)
        try:close_child_ingress(child)
        except Exception:logger.exception('Rejected child ingress cleanup failed')
        try:close_execution_budget(child)
        except Exception:logger.exception('Rejected child budget cleanup failed')
        try:
            reject_unstarted_chat_task(child,'Child budget or registration admission failed',
                cause='child_admission_failed',conv_id=child.get('convId') or '')
        except Exception:logger.exception('Rejected child terminal cleanup failed')
        raise
    return child


def run_child_invocation(child: dict, *, before_dispatch=None) -> dict:
    """Execute synchronously, retain the result for the caller, release registry."""
    import threading
    from lib.tasks_pkg.manager import discard_task
    from lib.tasks_pkg.invocation import invocation_abort_requested
    # Legacy subprocess handlers poll the carrier flag. One cancellation mirror per
    # active child connects the inherited control port even inside a long tool.
    from lib.swarm.liveness import ProgressBeacon, thread_progress_sink
    owns_beacon = child.get('_invocationProgressBeacon') is None
    beacon = child.get('_invocationProgressBeacon') or ProgressBeacon()
    progress_key = child.get('_invocationProgressKey') or child['id']
    child['_invocationProgressBeacon'] = beacon
    child['_invocationProgressKey'] = progress_key
    beacon.touch(progress_key, 'admitted')
    stop = threading.Event()
    def mirror():
        while not stop.wait(0.1):
            if invocation_abort_requested(child):
                return
    monitor = threading.Thread(target=mirror, name=f'child-abort-{child["id"][:8]}', daemon=True)
    try:
        monitor.start()
        with thread_progress_sink(beacon, progress_key):
            # Required durable admission, after preparation and
            # before the invocation can enter any model/tool work. Failure
            # follows the same ingress, budget and registry cleanup as execution.
            if before_dispatch is not None:
                before_dispatch()
            execute_invocation(child, policy=child['_invocationPolicy'])

        close_child_ingress(child)
        failure = child.get('_invocationDeliveryError')
        if failure is not None:
            # A swallowed observer exception cannot become a successful leaf.
            raise RuntimeError('Child invocation evidence delivery failed') from failure
        return child
    finally:
        stop.set()

        close_child_ingress(child)
        child.pop('_invocationEventSink', None)
        child.pop('_invocationAbortCheck', None)
        child.pop('_invocationCheckpointSink', None)
        if monitor.ident is not None:
            monitor.join(timeout=0.3)
        from lib.agent_core.execution_budget import close_execution_budget
        close_execution_budget(child)
        child.pop('_invocationProgressBeacon', None)
        child.pop('_invocationProgressKey', None)
        if owns_beacon:
            beacon.forget(progress_key)
        discard_task(child['id'])


__all__ = ['create_child_invocation', 'run_child_invocation',
           'commit_child_publication']
