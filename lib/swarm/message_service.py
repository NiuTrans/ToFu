"""Model-facing run messages. Session lookup stays with Swarm integration.

This service checks the caller and exact target; repository owns durable CAS.
A body cannot set owner, parent, generation, provenance or tool permissions.
"""
from __future__ import annotations

from lib.swarm.message_contract import NAMESPACE
from lib.swarm.run_messages import (
    RunMessageError, RunMessageRepository, binding_for, mailbox_key, validate_target,
)
from lib.tasks_pkg.manager import task_user_id


def _caller(task: dict) -> tuple[int, str, str]:
    from lib.tasks_pkg.invocation import is_child_invocation
    if is_child_invocation(task) or task.get('_parentTaskId'):
        raise RunMessageError('message_forbidden', 'A child cannot control or inspect sibling runs')
    if not isinstance(task.get('id'), str) or not task['id']:
        raise RunMessageError('message_forbidden', 'A current parent task identity is required')
    if task.get('_transientRuntime') or task.get('_invocationStorageFree'):
        raise RunMessageError('durable_storage_required', 'Messaging requires durable application storage')
    return task_user_id(task), str(task.get('_tenant_id') or ''), str(task.get('convId') or '')


def _arguments(args: dict, *, sending: bool) -> tuple[str, str]:
    fields = {'agent_id', 'swarm_run_id', 'content', 'kind', 'idempotency_key'} if sending else {
        'agent_id', 'swarm_run_id', 'message_id'}
    if not isinstance(args, dict) or set(args) - fields:
        raise RunMessageError('message_invalid_arguments', 'Unknown message fields')
    for key in ('agent_id', 'swarm_run_id'):
        if not isinstance(args.get(key), str) or not 1 <= len(args[key]) <= 256:
            raise RunMessageError('message_invalid_identity', f'{key} is required')
    return args['agent_id'], args['swarm_run_id']


def send(task: dict, args: dict, session) -> dict:
    owner, tenant, conversation_id = _caller(task)
    agent_id, run_id = _arguments(args, sending=True)
    if (session is None or session.task_id != run_id or session.user_id != owner
            or session.conv_id != conversation_id
            or str(session._parent_task_proxy.get('_tenant_id') or '') != tenant
            or (not conversation_id and task['id'] != run_id)):
        raise RunMessageError('message_run_not_found', 'No owned current run matches this exact handle')
    if callable(getattr(session, 'abort_check', None)) and session.abort_check():
        raise RunMessageError('message_run_ended', 'Execution cancellation authority rejected this run')
    if task.get('aborted') or session.is_terminated or session._aborted or session._restart_suspended:
        raise RunMessageError('message_run_ended', 'The run is stopped, suspended or ended; sending cannot wake it')
    with session._lock:
        spec = next((item for item in session.specs if item.id == agent_id), None)
        terminal = agent_id in session._results_by_id

        agent = getattr(session, '_agents', {}).get(agent_id)
        child = getattr(agent, '_child', None)
        if child is not None and (child.get('aborted') or
                (child.get('abort_event') is not None and child['abort_event'].is_set())):
            terminal = True
    if spec is None or terminal:
        raise RunMessageError('message_target_not_active', 'Target is absent or already settled; use a new explicit assignment')
    binding = binding_for(session._parent_task_proxy, spec, session.inbox_key)
    validate_target(binding)
    repository = RunMessageRepository(binding)
    def active():
        validate_target(binding)
        with session._lock:
            ended = agent_id in session._results_by_id
        if ended or (child is not None and child.get('aborted')):
            raise RunMessageError('message_run_ended', 'Child settled or was stopped during submission')
    item = repository.submit(
        content=args.get('content'), kind=args.get('kind', 'reference'),
        key=args.get('idempotency_key'), sender_task_id=task['id'], before_commit=active)
    try:
        active()
    except RunMessageError:
        repository.close()
        raise
    return {'agent_id': agent_id, 'swarm_run_id': run_id, 'message_id': item['id'],
            'state': item['state'], 'kind': item['kind'], 'contentChars': len(item['content']),
            'execution_started': False}


def read(task: dict, args: dict) -> dict:
    owner, tenant, conversation_id = _caller(task)
    agent_id, run_id = _arguments(args, sending=False)
    if not conversation_id and task['id'] != run_id:
        raise RunMessageError('message_forbidden', 'Unbound runs are visible only to their spawning task')
    from lib.storage import get_storage_client
    row = get_storage_client().query('record.get', {
        'namespace': NAMESPACE, 'key': mailbox_key(owner, tenant, conversation_id, run_id, agent_id)})
    binding = (row.get('value') or {}).get('binding') if isinstance(row, dict) else None
    if not isinstance(binding, dict):
        raise RunMessageError('message_run_not_found', 'No durable messages exist for this run')
    if (binding.get('owner'), binding.get('tenant'), binding.get('conversationId'),
            binding.get('swarmRunId'), binding.get('agentId')) != (owner, tenant, conversation_id, run_id, agent_id):
        raise RunMessageError('message_forbidden', 'Mailbox ownership does not match')
    validate_target(binding, active_retained=False)
    repository = RunMessageRepository(binding)
    from lib.swarm.persistence import load_session
    execution = load_session(binding['swarmKey'], required=True)
    ended = False
    if execution is not None and execution.get('task_id') == run_id:
        ended = execution.get('status') in {'terminated', 'quarantined', 'cancelled'} or any(
            item.get('agent_id') == agent_id and item.get('status') not in {'running', 'pending', 'interrupted'}
            for item in execution.get('agents') or [])
    value, _ = repository.load()
    if ended:
        value['closed'] = True
        for item in value['messages']:
            if item['state'] == 'submitted':
                item['state'] = 'unconfirmed'
    message_id = args.get('message_id')
    if message_id is not None and (not isinstance(message_id, str) or not 1 <= len(message_id) <= 128):
        raise RunMessageError('message_invalid_identity', 'Invalid message_id')
    items = value['messages']
    if message_id:
        items = [item for item in items if item['id'] == message_id]
        if not items:
            raise RunMessageError('message_not_found', 'No message matches this exact ID')
    return {
        'agent_id': agent_id, 'swarm_run_id': run_id, 'run_closed': value['closed'],
        'messages': [{**{key: item[key] for key in (
            'id', 'kind', 'state', 'senderTaskId', 'submittedAt', 'inputTaskId',
            'inputRound', 'inputConfirmedAt') if key in item},
            'content': item['content'] if message_id else item['content'][:256],
            'contentChars': len(item['content']),
            'contentTruncated': not bool(message_id) and len(item['content']) > 256,
        } for item in items],
    }
