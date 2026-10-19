"""Lifecycle and delegation service for reusable Swarm agents.

The durable record is an identity and memory checkpoint, not an idle executor.
Each assignment launches one ordinary Swarm child. Its current task supplies
fresh tools, policy, budget and authorization; the saved dialogue supplies only
working history. A CAS run ID serializes assignments across processes.
"""

from __future__ import annotations

import json
import uuid

from lib.swarm.retained_launch import (
    LaunchOutcome, apply_launch_outcome, classify_launch, launch_lookup,
)
from lib.swarm.retained_repository import (
    RetainedAgentError, RetainedAgentRepository, scope_for,
)


def _public(value: dict, *, memory: bool = False) -> dict:
    result = {key: value.get(key) for key in (
        'id', 'name', 'role', 'state', 'generation', 'last_status',
        'created_at', 'updated_at',
    )}
    result['message_count'] = int(value.get('message_count')
                                  if value.get('message_count') is not None
                                  else len(value.get('messages') or []))
    result['last_result'] = str(value.get('last_result') or '')[:4000]
    if memory:
        if value.get('state') == 'busy':
            result['assignment'] = {
                'agent_id': value.get('run_id') or '',
                'swarm_run_id': value.get('run_session_id') or '',
                'conversation_id': value.get('run_conversation_id') or '',
            }
            result['launch_state'] = value.get('run_launch_state') or 'unknown'
            result['launch_lookup'] = value.get('run_launch_lookup') or launch_lookup(
                run_id=value.get('run_id') or '', swarm_key=value.get('swarm_key') or '',
                session_id=value.get('run_session_id') or '')
        # Inspectable without injecting the entire transcript into the root.
        result['recent_messages'] = [
            {'role': str(item.get('role') or ''),
             'content': str(item.get('content') or '')[:2000]}
            for item in (value.get('messages') or [])[-8:]
            if isinstance(item, dict) and item.get('role') in {'user', 'assistant'}
        ]
    return result


def _require_idle(value: dict) -> None:
    if value.get('state') == 'busy':
        raise RetainedAgentError('This agent is working; wait for its result')


def _repository(task: dict, *, project_path: str, requested_scope: str = ''):
    from lib.tasks_pkg.manager import task_user_id
    if task.get('_transientRuntime') or task.get('_invocationStorageFree'):
        raise RetainedAgentError('Retained agents require durable application storage')
    owner = task_user_id(task)
    scope = scope_for(
        project_path=project_path,
        conversation_id=str(task.get('convId') or ''),
        requested=requested_scope,
    )
    return RetainedAgentRepository(
        owner, tenant_id=str(task.get('_tenant_id') or '')), scope


def complete_assignment(*, user_id: int, tenant_id: str, scope: str,
                        agent_id: str, run_id: str, messages: list,
                        result: dict, task: dict | None = None) -> bool:
    """Commit final memory; pressure waits on the exact repository receipt."""
    repository = RetainedAgentRepository(user_id, tenant_id=tenant_id,
                                         task=task, cancellable=False)

    class SupersededAssignment(Exception):
        pass

    def finish(value: dict) -> dict:
        if value.get('state') != 'busy' or value.get('run_id') != run_id:
            # A reset/abandon generation fences a late completion.
            raise SupersededAssignment
        value['last_assignment'] = {
            'agent_id': run_id, 'swarm_run_id': value.get('run_session_id') or '',
            'conversation_id': value.get('run_conversation_id') or '',
            'swarm_key': value.get('swarm_key') or '',
        }
        value.update(
            state='idle', run_id='', swarm_key='',
            last_result=str(result.get('final_answer') or '')[:4000],
            last_status=str(result.get('status') or '')[:32],
        )
        value['messages'] = list(messages)
        return value

    found = repository.get(scope, agent_id)
    if found is None or found[0].get('run_id') != run_id:
        return False
    try:
        repository.change(scope, agent_id, finish)
    except SupersededAssignment:
        return False
    return True


def _reconcile(repository: RetainedAgentRepository, scope: str, value: dict) -> dict:
    """Complete a lost callback from the durable Swarm checkpoint if settled."""
    if value.get('state') != 'busy' or not value.get('swarm_key'):
        return value
    if value.get('run_launch_state') == 'unknown' and not value.get('run_session_id'):
        # Lookup hints (including a returned unknown-launch session ID) cannot
        # settle a reservation. The ordinary child factory can bind the actual
        # session before execution; its exact terminal checkpoint may recover a
        # lost launch ACK and completion callback without replaying either.
        return value
    from lib.swarm import persistence
    session = persistence.load_session(str(value['swarm_key']))
    if not isinstance(session, dict):
        return value
    # The conversation key and child ID are reusable lookup names, not proof
    # that this checkpoint belongs to the saved assignment. Legacy incomplete
    # witnesses stay busy/unknown; never import their memory or release the run.
    run_id = value.get('run_id')
    session_id = value.get('run_session_id')
    conversation_id = value.get('run_conversation_id')
    if (not run_id or not session_id or not conversation_id
            or session.get('task_id') != session_id
            or session.get('conv_id') != conversation_id):
        return value
    config = session.get('config')
    if not isinstance(config, dict):
        return value
    parent_cfg = config.get('parent_cfg') or {}
    if (not isinstance(parent_cfg, dict)
            or str(config.get('user_id')) != str(repository.user_id)
            or str(parent_cfg.get('_tenant_id') or '') != repository.tenant_id):
        return value
    specs = session.get('specs')
    agents = session.get('agents')
    if not isinstance(specs, list) or not isinstance(agents, list):
        return value
    matching_specs = [spec for spec in specs
                      if isinstance(spec, dict) and spec.get('id') == run_id]
    if len(matching_specs) != 1:
        return value
    spec = matching_specs[0]
    if (spec.get('retained_agent_id') != value.get('id')
            or spec.get('retained_scope') != scope
            or spec.get('retained_run_id') != run_id):
        return value
    matching_children = [row for row in agents
                         if isinstance(row, dict) and row.get('agent_id') == run_id]
    if len(matching_children) != 1:
        return value
    child = matching_children[0]
    if child.get('status') in {'completed', 'failed', 'cancelled'}:
        complete_assignment(
            user_id=repository.user_id, tenant_id=repository.tenant_id,
            scope=scope, agent_id=value['id'], run_id=value['run_id'],
            messages=child.get('messages') or value.get('messages') or [],
            result=child.get('result') or {'status': child.get('status')},
        )
        found = repository.get(scope, value['id'])
        return found[0] if found else value
    return value


def execute_manage_agents(args: dict, *, task: dict, cfg: dict,
                          all_tools: list, project_path: str,
                          on_event=None, abort_check=None) -> str:
    """Execute one model-visible team operation with a bounded result."""
    try:
        action = str(args.get('action') or '').strip()
        repository, scope = _repository(
            task, project_path=project_path,
            requested_scope=str(args.get('scope') or ''),
        )
        if action == 'list':
            return json.dumps({'agents': [_public(value) for value in repository.list(scope)]},
                              ensure_ascii=False)

        if action == 'create':
            from lib.swarm.registry import resolve_spawnable_role
            role = str(args.get('role') or 'general')
            if resolve_spawnable_role(role) != role:
                raise RetainedAgentError('Unknown agent role')
            created = repository.create(
                scope, role=role,
                name=str(args.get('name') or role),
            )
            return json.dumps({'agent': _public(created)}, ensure_ascii=False)

        if action == 'promote':
            from lib.swarm import persistence
            from lib.swarm.integration._config import swarm_key_for
            from lib.swarm.registry import resolve_spawnable_role
            source_id = str(args.get('source_agent_id') or '')
            session = persistence.load_session(swarm_key_for(task))
            if (not isinstance(session, dict)
                    or str((session.get('config') or {}).get('user_id'))
                    != str(repository.user_id)
                    or str(((session.get('config') or {}).get('parent_cfg') or {})
                           .get('_tenant_id') or '') != repository.tenant_id):
                raise RetainedAgentError('No owned Swarm session is available for promotion')
            child = next((item for item in (session.get('agents') or [])
                          if item.get('agent_id') == source_id), None)
            if (not isinstance(child, dict)
                    or child.get('status') not in {'completed', 'failed', 'cancelled'}
                    or not isinstance(child.get('messages'), list)
                    or not child['messages']):
                raise RetainedAgentError('The source child has no settled memory to promote')
            role = str(child.get('role') or '')
            if resolve_spawnable_role(role) != role:
                raise RetainedAgentError('The source role is no longer available')
            result = child.get('result') or {}
            created = repository.create(
                scope, role=role,
                name=str(args.get('name') or role),
                messages=child['messages'],
                last_result=str(result.get('final_answer') or ''),
                last_status=str(child.get('status') or ''),
            )
            return json.dumps({'agent': _public(created),
                               'promoted_from': source_id}, ensure_ascii=False)

        agent_id = str(args.get('agent_id') or '')
        if not agent_id:
            raise RetainedAgentError('agent_id is required')
        found = repository.get(scope, agent_id)
        if found is None:
            raise RetainedAgentError('Agent not found in this scope')
        value = _reconcile(repository, scope, found[0])

        if action == 'inspect':
            return json.dumps({'agent': _public(value, memory=True)}, ensure_ascii=False)

        if action == 'reset':
            def reset(current):
                if current.get('state') == 'busy' and args.get('abandon') is not True:
                    raise RetainedAgentError('Agent is working; set abandon=true to fence its late result')
                current.update(
                    state='idle', generation=int(current.get('generation') or 1) + 1,
                    run_id='', swarm_key='', run_session_id='', run_conversation_id='',
                    run_launch_state='', run_launch_lookup={},
                    messages=[], last_result='', last_status='',
                )
                return current
            return json.dumps({'agent': _public(repository.change(scope, agent_id, reset))},
                              ensure_ascii=False)

        if action in {'archive', 'restore'}:
            def set_state(current):
                _require_idle(current)
                current['state'] = 'archived' if action == 'archive' else 'idle'
                return current
            return json.dumps({'agent': _public(repository.change(scope, agent_id, set_state))},
                              ensure_ascii=False)

        if action == 'delete':
            repository.delete(scope, agent_id)
            return json.dumps({'deleted': True, 'agent_id': agent_id})

        if action == 'assign':
            objective = str(args.get('objective') or '').strip()
            if not objective or len(objective) > 100_000:
                raise RetainedAgentError('objective must contain 1–100,000 characters')
            context = str(args.get('context') or '')
            if len(context) > 100_000:
                raise RetainedAgentError('context is too long')
            tools = args.get('tools') or []
            if not isinstance(tools, list) or len(tools) > 64 or any(
                    not isinstance(item, str) for item in tools):
                raise RetainedAgentError('tools must be an array of up to 64 names')
            run_id = uuid.uuid4().hex[:16]
            from lib.swarm.integration._config import swarm_key_for
            swarm_key = swarm_key_for(task)

            def reserve(current):
                if current.get('state') != 'idle':
                    raise RetainedAgentError('Agent is busy or archived')
                current.update(state='busy', run_id=run_id, swarm_key=swarm_key,
                               run_session_id='', run_launch_state='unknown',
                               run_launch_lookup=launch_lookup(run_id=run_id, swarm_key=swarm_key),
                               run_conversation_id=str(task.get('convId') or ''))
                return current

            reserved = repository.change(scope, agent_id, reserve)
            try:
                from lib.swarm.integration import execute_swarm_tool
                launched = execute_swarm_tool(
                    'spawn_agents', {'agents': [{
                        'id': run_id, 'role': reserved['role'],
                        'objective': objective, 'context': context,
                        'tools': tools, 'max_retries': 0,
                    }]}, task, cfg=cfg, all_tools=all_tools,
                    project_path=project_path, on_event=on_event,
                    abort_check=abort_check,
                    retained_binding={'scope': scope, 'agent_id': agent_id,
                                      'run_id': run_id},
                )
                outcome = classify_launch(launched, run_id=run_id, swarm_key=swarm_key,
                    conversation_id=reserved['run_conversation_id'])
            except Exception as error:
                # Worker entry can precede an exception or lost launch reply.
                # Control exceptions still propagate with the durable fence.
                outcome = LaunchOutcome('unknown', {'exception_type': type(error).__name__})
            try:
                repository.change(scope, agent_id, lambda current: apply_launch_outcome(
                    current, run_id=run_id, generation=reserved['generation'], outcome=outcome))
            except Exception:
                # Acceptance/rejection may be known locally but not durably
                # recorded. Preserve the original lookup, never replay launch.
                outcome = LaunchOutcome('unknown', outcome.handle)
            handle = dict(outcome.handle)
            if outcome.state == 'rejected':
                return json.dumps({**handle, 'submission_state': 'rejected'}, ensure_ascii=False)
            if outcome.state == 'unknown':
                return json.dumps({
                    **{key: item for key, item in handle.items() if key != 'agents'},
                    'status': 'error', 'code': 'swarm_launch_unknown',
                    'submission_state': 'unknown', 'retryable': False,
                    'retained_agent_id': agent_id,
                    **launch_lookup(run_id=run_id, swarm_key=swarm_key, handle=handle),
                    'next_action': 'Inspect manage_agents with this retained agent_id and '
                        'get_agent_result with the original requested_agent_ids; '
                        'do not launch a replacement or assume rollback.',
                    'message': 'The original assignment is reserved; launch acceptance is unconfirmed.',
                }, ensure_ascii=False)
            handle['retained_agent_id'] = agent_id
            handle['message'] = (
                f"Assigned {value['name']} ({agent_id}). The result arrives as a "
                '<swarm-update>; use manage_agents with the same agent_id later.')
            return json.dumps(handle, ensure_ascii=False)

        raise RetainedAgentError('Unknown action')
    except RetainedAgentError as error:
        return json.dumps({
            'status': 'error', 'code': 'retained_agent_error',
            'retryable': False, 'next_action': 'Correct the action or inspect the agent.',
            'message': str(error),
        }, ensure_ascii=False)
    except Exception as error:
        from lib.storage.errors import StorageError
        if not isinstance(error, StorageError):
            raise
        return json.dumps({
            'status': 'error', 'code': 'retained_agent_storage_unavailable',
            'retryable': bool(error.retryable),
            'next_action': 'Retry after durable storage is available.',
            'message': 'The agent record could not be committed.',
        }, ensure_ascii=False)


__all__ = ['execute_manage_agents', 'complete_assignment']
